"""Tests for SmartLogAnalyzer."""

import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lark_agent_bridge.log_analyzer import SmartLogAnalyzer


@pytest.fixture
def temp_log_dir(tmp_path):
    """创建临时日志目录结构"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    # 创建应用日志目录
    app_dir = log_dir / "app" / "com.xiaopeng.montecarlo"
    app_dir.mkdir(parents=True)

    # 创建系统日志目录
    logd_dir = log_dir / "logd"
    logd_dir.mkdir()

    # 创建模拟的应用日志文件（支持 user0_ 前缀）
    log_file = app_dir / "user0_main_2026-05-25_16-00.alog"
    log_content = """05-25 16:50:40.123  1234  5678 I XPEDriveSurfaceView: Watchdog kick: gap=50ms
05-25 16:50:40.456  1234  5678 I XPEDriveSurfaceView: UnityRequest called pending=0,count=25 in last 1000ms
05-25 16:50:41.789  1234  5678 I SrUnityMonitor: UnityMain updateCheck once.
05-25 16:50:42.123  1234  5678 E VHALHelper: [41001][0] signal lost
05-25 16:50:43.456  1234  5678 I MapDataHandler: [handle_map_data][count=100|max_interval_ms=50|duration_s=5.0]
05-25 16:50:44.789  5678  9012 I OtherProcess: some log
05-25 16:50:45.123  1234  5678 W X3DCB: [X3DCB-132000][50]
"""
    log_file.write_text(log_content, encoding="utf-8")

    # 创建系统日志文件
    main_txt = logd_dir / "main.txt"
    main_content = """05-25 16:50:40.100  1234  5678 D WindowManager: focus changed
05-25 16:50:41.200  1234  5678 I ActivityManager: process started
05-25 16:50:42.300  9999  1111 I SystemServer: system log
"""
    main_txt.write_text(main_content, encoding="utf-8")

    return log_dir


@pytest.fixture
def analyzer(tmp_path):
    """创建 SmartLogAnalyzer 实例"""
    return SmartLogAnalyzer(
        process_name="com.xiaopeng.montecarlo",
        time_window_minutes=1,
        max_reverse_lines=100,
        workspace_root=tmp_path,
    )


class TestFindTargetPid:
    """测试 find_target_pid 方法"""

    def test_find_pid_success(self, analyzer, temp_log_dir):
        """测试成功找到进程号"""
        target_time = datetime(2026, 5, 25, 16, 50, 41)
        pid = analyzer.find_target_pid(temp_log_dir, target_time)

        # 应该找到 PID 1234（出现频率最高）
        assert pid == 1234

    def test_find_pid_no_logs(self, analyzer, tmp_path):
        """测试没有日志文件的情况"""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        target_time = datetime(2026, 5, 25, 16, 50, 41)
        pid = analyzer.find_target_pid(empty_dir, target_time)

        assert pid is None

    def test_find_pid_outside_time_window(self, analyzer, temp_log_dir):
        """测试目标时间在日志时间窗口之外"""
        # 日志记录在 16:50，查询 17:00 应该找不到
        target_time = datetime(2026, 5, 25, 17, 0, 0)
        pid = analyzer.find_target_pid(temp_log_dir, target_time)

        assert pid is None

    def test_find_pid_accepts_variable_prefix_before_fixed_timestamp(self, analyzer, tmp_path):
        """测试日志名前缀可变时仍能按固定时间段定位 PID"""
        log_dir = tmp_path / "logs"
        app_dir = log_dir / "app" / "com.xiaopeng.montecarlo"
        app_dir.mkdir(parents=True)
        log_file = app_dir / "user0_main_2026-05-25_16-00.alog"
        log_file.write_text(
            "05-25 16:50:41.000  1234  5678 I Unity: target line\n",
            encoding="utf-8",
        )

        pid = analyzer.find_target_pid(log_dir, datetime(2026, 5, 25, 16, 50, 41))

        assert pid == 1234


class TestFindAllLogFiles:
    """测试 find_all_log_files 方法"""

    def test_find_files_success(self, analyzer, temp_log_dir):
        """测试成功找到相关日志文件"""
        target_pid = 1234
        log_files = analyzer.find_all_log_files(temp_log_dir, target_pid)

        # 应该找到应用日志和系统日志
        assert len(log_files) >= 2

        # 检查文件路径（支持 user0_ 前缀）
        file_names = [f.name for f in log_files]
        assert any("main_2026-05-25_16-00.alog" in name for name in file_names)
        assert "main.txt" in file_names

    def test_find_files_no_pid(self, analyzer, temp_log_dir):
        """测试日志中不包含目标 PID"""
        target_pid = 9999  # 不存在的 PID
        log_files = analyzer.find_all_log_files(temp_log_dir, target_pid)

        # 可能找到系统日志（因为系统日志包含 9999）
        # 但应用日志不包含 9999
        app_files = [f for f in log_files if "com.xiaopeng.montecarlo" in str(f)]
        assert len(app_files) == 0

    def test_find_files_empty_dir(self, analyzer, tmp_path):
        """测试空目录"""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        target_pid = 1234
        log_files = analyzer.find_all_log_files(empty_dir, target_pid)

        assert len(log_files) == 0

    def test_find_files_accepts_variable_prefix_before_fixed_timestamp(self, analyzer, tmp_path):
        """测试相关日志扫描不依赖 main_ 固定前缀"""
        log_dir = tmp_path / "logs"
        app_dir = log_dir / "app" / "com.xiaopeng.montecarlo"
        app_dir.mkdir(parents=True)
        log_file = app_dir / "user0_main_2026-05-25_16-00.alog"
        log_file.write_text(
            "05-25 16:50:41.000  1234  5678 I Unity: target line\n",
            encoding="utf-8",
        )

        log_files = analyzer.find_all_log_files(log_dir, 1234)

        assert log_files == [log_file]


class TestReverseTrace:
    """测试 reverse_trace 方法"""

    def test_reverse_trace_success(self, analyzer, temp_log_dir):
        """测试成功反推日志线索"""
        target_time = datetime(2026, 5, 25, 16, 50, 43)
        target_pid = 1234

        # 先找到日志文件
        log_files = analyzer.find_all_log_files(temp_log_dir, target_pid)
        assert len(log_files) > 0

        # 反推日志线索
        events = analyzer.reverse_trace(log_files, target_time, target_pid)

        # 应该找到多个事件
        assert len(events) > 0

        # 检查事件内容
        for event in events:
            assert "timestamp" in event
            assert "pid" in event
            assert event["pid"] == target_pid

    def test_reverse_trace_empty_files(self, analyzer, temp_log_dir):
        """测试空日志文件"""
        target_time = datetime(2026, 5, 25, 16, 50, 43)
        target_pid = 1234

        # 使用空文件列表
        events = analyzer.reverse_trace([], target_time, target_pid)

        assert len(events) == 0


class TestIdentifyProblemType:
    """测试 identify_problem_type 方法"""

    def test_identify_3d_stuck(self, analyzer):
        """测试识别 3D 卡顿问题"""
        timeline = [
            {"message": "Watchdog kick: gap=50ms", "timestamp": "2026-05-25T16:50:40"},
            {"message": "UnityRequest called pending=0,count=5", "timestamp": "2026-05-25T16:50:41"},
        ]

        skill = analyzer.identify_problem_type(timeline)

        assert skill == "3d-stuck-investigate"

    def test_identify_scene_signal(self, analyzer):
        """测试识别场景信号问题"""
        timeline = [
            {"message": "SIGNAL_SR_SCENE_TYPE: 0", "timestamp": "2026-05-25T16:50:40"},
            {"message": "OnSceneChanged: Driving_Scene", "timestamp": "2026-05-25T16:50:41"},
        ]

        skill = analyzer.identify_problem_type(timeline)

        assert skill == "scene-signal-diagnosis"

    def test_identify_perception(self, analyzer):
        """测试识别感知数据问题"""
        timeline = [
            {"message": "VHALHelper: [41001][0] signal lost", "timestamp": "2026-05-25T16:50:40"},
            {"message": "MapDataHandler: [handle_map_data][count=0]", "timestamp": "2026-05-25T16:50:41"},
        ]

        skill = analyzer.identify_problem_type(timeline)

        assert skill == "perception-data-summary"

    def test_identify_general(self, analyzer):
        """测试通用问题（无法识别）"""
        timeline = [
            {"message": "some generic log", "timestamp": "2026-05-25T16:50:40"},
        ]

        skill = analyzer.identify_problem_type(timeline)

        assert skill == "general"

    def test_identify_empty_timeline(self, analyzer):
        """测试空时间线"""
        skill = analyzer.identify_problem_type([])

        assert skill == "general"


class TestInternalMethods:
    """测试内部方法"""

    def test_parse_log_file_time(self, analyzer):
        """测试解析日志文件名时间"""
        filename = "main_2026-05-25_16-00.alog"
        dt = analyzer._parse_log_file_time(filename)

        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 5
        assert dt.day == 25
        assert dt.hour == 16
        assert dt.minute == 0

    def test_parse_log_file_time_accepts_variable_prefix(self, analyzer):
        """测试只依赖固定时间段，不依赖固定前缀"""
        filename = "user0_main_2026-05-25_16-30.xlog"
        dt = analyzer._parse_log_file_time(filename)

        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 5
        assert dt.day == 25
        assert dt.hour == 16
        assert dt.minute == 30

    def test_parse_log_file_time_invalid(self, analyzer):
        """测试无效的日志文件名"""
        filename = "invalid_filename.log"
        dt = analyzer._parse_log_file_time(filename)

        assert dt is None

    def test_extract_timestamp_full(self, analyzer):
        """测试提取完整时间戳"""
        line = "2026-05-25 16:50:41.123  1234  5678 I TAG: message"
        ts = analyzer._extract_timestamp(line, 2026)

        assert ts is not None
        assert ts.year == 2026
        assert ts.month == 5
        assert ts.day == 25
        assert ts.hour == 16
        assert ts.minute == 50
        assert ts.second == 41

    def test_extract_timestamp_short(self, analyzer):
        """测试提取短格式时间戳"""
        line = "05-25 16:50:41.123  1234  5678 I TAG: message"
        ts = analyzer._extract_timestamp(line, 2026)

        assert ts is not None
        assert ts.year == 2026
        assert ts.month == 5
        assert ts.day == 25

    def test_extract_pid(self, analyzer):
        """测试提取 PID"""
        line = "05-25 16:50:41.123  1234  5678 I TAG: message"
        pid = analyzer._extract_pid(line)

        assert pid == 1234

    def test_extract_pid_invalid(self, analyzer):
        """测试无效的 PID"""
        line = "invalid log line"
        pid = analyzer._extract_pid(line)

        assert pid is None

    def test_find_time_index(self, analyzer):
        """测试时间索引查找"""
        lines = [
            "05-25 16:50:40.000  1234  5678 I TAG: msg1",
            "05-25 16:50:41.000  1234  5678 I TAG: msg2",
            "05-25 16:50:42.000  1234  5678 I TAG: msg3",
            "05-25 16:50:43.000  1234  5678 I TAG: msg4",
        ]
        target_time = datetime(2026, 5, 25, 16, 50, 42)

        idx = analyzer._find_time_index(lines, target_time, 2026)

        # 应该找到索引 2（16:50:42）
        assert idx == 2

    def test_parse_log_line(self, analyzer):
        """测试解析日志行"""
        line = "05-25 16:50:41.123  1234  5678 I TAG: message content"
        event = analyzer._parse_log_line(line, 2026)

        assert event is not None
        assert event["pid"] == 1234
        # 注意：TID 的提取依赖于日志格式，这里验证基本结构
        assert "tid" in event
        assert event["level"] == "I"
        assert event["tag"] == "TAG"
        assert "message content" in event["message"]

    def test_parse_log_line_invalid(self, analyzer):
        """测试解析无效日志行"""
        line = "invalid log line"
        event = analyzer._parse_log_line(line, 2026)

        assert event is None

    def test_decode_log_file_not_available(self, analyzer, tmp_path):
        """测试 log-decoder 不可用的情况"""
        # 创建一个模拟的 .alog 文件
        alog_file = tmp_path / "test.alog"
        alog_file.write_bytes(b'\x00\x01\x02\x03')

        # log-decoder 工具不存在，应该返回 None
        result = analyzer._decode_log_file(alog_file)

        assert result is None

    def test_decode_log_file_already_decoded(self, analyzer, tmp_path):
        """测试已解密的日志文件"""
        # 创建一个模拟的 .alog 文件和对应的 .log 文件
        alog_file = tmp_path / "test.alog"
        alog_file.write_bytes(b'\x00\x01\x02\x03')

        log_file = tmp_path / "test.log"
        log_file.write_text("decoded log content", encoding="utf-8")

        # 应该直接返回已存在的 .log 文件
        result = analyzer._decode_log_file(alog_file)

        assert result == log_file


class TestIntegration:
    """集成测试"""

    def test_full_workflow(self, analyzer, temp_log_dir):
        """测试完整工作流"""
        target_time = datetime(2026, 5, 25, 16, 50, 42)

        # Step 1: 定位进程号
        target_pid = analyzer.find_target_pid(temp_log_dir, target_time)
        assert target_pid == 1234

        # Step 2: 找出所有相关日志
        log_files = analyzer.find_all_log_files(temp_log_dir, target_pid)
        assert len(log_files) >= 2

        # Step 3: 反推日志线索
        timeline = analyzer.reverse_trace(log_files, target_time, target_pid)
        assert len(timeline) > 0

        # Step 4: 识别问题类型
        skill = analyzer.identify_problem_type(timeline)
        # 根据日志内容，应该识别为 3D 卡顿或感知数据
        assert skill in ["3d-stuck-investigate", "perception-data-summary", "general"]

        # 验证事件内容
        for event in timeline:
            assert "timestamp" in event
            assert "pid" in event
            assert event["pid"] == target_pid
