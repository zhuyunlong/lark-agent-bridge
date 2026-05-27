"""Smart log analyzer for intelligent log localization and reverse tracing.

This module provides SmartLogAnalyzer class that can:
1. Find target process PID based on problem time
2. Find all related log files based on PID
3. Reverse trace log clues from problem time
4. Identify problem type based on log content
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .log import get_logger

logger = get_logger("log_analyzer")


# Log line format: "05-25 16:50:41.123  1234  5678 I TAG: message"
#                                  ↑    ↑    ↑
#                               时间戳  PID  TID
LOG_LINE_PID_RE = re.compile(
    r"(?P<ts>\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d{3})?)"
    r"\s+"
    r"(?P<pid>\d+)"
    r"\s+"
    r"(?P<tid>\d+)"
)

# Full datetime format: "2026-05-25 16:50:41"
FULL_DATETIME_RE = re.compile(
    r"(?P<year>20\d{2})[-/](?P<month>\d{1,2})[-/](?P<day>\d{1,2})"
    r"[ T]"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2})"
)

# Short datetime format (without year): "05-25 16:50:41"
SHORT_DATETIME_RE = re.compile(
    r"(?P<month>\d{2})-(?P<day>\d{2})\s+(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
)

# Log file name patterns (support variable prefixes like user0_main_...)
LOG_FILE_PATTERN = re.compile(
    r"(?:\w+_)?main_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})\.(alog|xlog)"
)


class SmartLogAnalyzer:
    """智能日志分析器

    核心能力：
    1. 根据问题时间点定位目标进程的 PID
    2. 基于 PID 找出所有相关的日志文件
    3. 从问题时间点反推日志线索
    4. 根据日志内容识别问题类型
    """

    def __init__(
        self,
        process_name: str = "com.xiaopeng.montecarlo",
        time_window_minutes: int = 1,
        max_reverse_lines: int = 1000,
        workspace_root: Path | None = None,
    ) -> None:
        """
        Args:
            process_name: 目标进程名称
            time_window_minutes: 定位 PID 的时间窗口（分钟）
            max_reverse_lines: 反推的最大行数
            workspace_root: 工作区根目录（用于查找 log-decoder 工具）
        """
        self.process_name = process_name
        self.time_window = timedelta(minutes=time_window_minutes)
        self.max_reverse_lines = max_reverse_lines
        self.workspace_root = workspace_root or Path(".")

        # log-decoder 工具路径
        self._log_decoder_jar = self.workspace_root / ".ai/skills/log-decoder/tools/decryptFile.jar"
        self._log_decoder_available = self._log_decoder_jar.exists()

        if not self._log_decoder_available:
            logger.warning("log-decoder 工具不可用: %s", self._log_decoder_jar)

    def find_target_pid(
        self,
        log_dir: Path,
        target_time: datetime,
    ) -> int | None:
        """根据问题时间点定位目标进程的 PID

        算法：
        1. 找到包含目标时间的日志文件
        2. 在目标时间 ± time_window 内搜索
        3. 统计 PID 出现频率
        4. 返回出现频率最高的 PID（排除系统进程）

        Args:
            log_dir: 日志根目录
            target_time: 问题时间点

        Returns:
            目标进程的 PID，如果未找到返回 None
        """
        logger.info("开始定位进程号: time=%s, process=%s", target_time, self.process_name)

        # 1. 找到包含目标时间的日志文件
        log_files = self._find_log_files_for_time(log_dir, target_time)
        if not log_files:
            logger.warning("未找到包含目标时间的日志文件")
            return None

        logger.info("找到 %d 个日志文件", len(log_files))

        # 2. 在目标时间 ± time_window 内搜索
        start_time = target_time - self.time_window
        end_time = target_time + self.time_window

        # 3. 统计 PID 出现频率
        pid_counter: Counter[int] = Counter()
        reference_year = target_time.year

        for log_file in log_files:
            try:
                for line in self._read_log_file(log_file):
                    ts = self._extract_timestamp(line, reference_year)
                    if ts and start_time <= ts <= end_time:
                        pid = self._extract_pid(line)
                        if pid and pid > 1000:  # 排除系统进程
                            pid_counter[pid] += 1
            except Exception as e:
                logger.warning("读取日志文件失败: %s, error=%s", log_file, e)
                continue

        # 4. 返回出现频率最高的 PID
        if pid_counter:
            target_pid, count = pid_counter.most_common(1)[0]
            logger.info("定位到进程号: pid=%d, 出现次数=%d", target_pid, count)
            return target_pid

        logger.warning("未在目标时间窗口内找到进程号")
        return None

    def find_all_log_files(
        self,
        log_dir: Path,
        target_pid: int,
    ) -> list[Path]:
        """基于进程号找出所有相关的日志文件

        扫描逻辑：
        1. 扫描应用日志: app/{process_name}/*_YYYY-MM-DD_HH-MM.*
        2. 扫描系统日志: logd/*.txt
        3. 检查每个文件是否包含目标 PID

        Args:
            log_dir: 日志根目录
            target_pid: 目标进程号

        Returns:
            包含目标 PID 的所有日志文件列表
        """
        logger.info("开始查找相关日志文件: pid=%d", target_pid)

        relevant_files: list[Path] = []

        # 1. 扫描应用日志
        app_log_dir = log_dir / "app" / self.process_name
        if app_log_dir.exists():
            for log_file in self._iter_app_log_files(app_log_dir):
                if self._file_contains_pid(log_file, target_pid):
                    relevant_files.append(log_file)
                    logger.debug("找到应用日志: %s", log_file)

        # 2. 扫描系统日志
        logd_dir = log_dir / "logd"
        if logd_dir.exists():
            for log_file in logd_dir.glob("*.txt"):
                if self._file_contains_pid(log_file, target_pid):
                    relevant_files.append(log_file)
                    logger.debug("找到系统日志: %s", log_file)

        # 按修改时间排序（最新的在前）
        relevant_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        logger.info("共找到 %d 个相关日志文件", len(relevant_files))
        return relevant_files

    def reverse_trace(
        self,
        log_files: list[Path],
        target_time: datetime,
        target_pid: int,
    ) -> list[dict[str, Any]]:
        """从问题时间点反推日志线索

        算法：
        1. 从目标时间点开始，向前反推
        2. 提取目标 PID 的所有日志
        3. 识别关键事件和模式
        4. 构建事件时间线

        Args:
            log_files: 日志文件列表
            target_time: 问题时间点
            target_pid: 目标进程号

        Returns:
            事件列表，按时间倒序排列
        """
        logger.info("开始反推日志线索: time=%s, pid=%d", target_time, target_pid)

        events: list[dict[str, Any]] = []
        reference_year = target_time.year

        for log_file in log_files:
            try:
                lines = self._read_log_file(log_file)

                # 找到目标时间点的位置
                target_idx = self._find_time_index(lines, target_time, reference_year)

                # 从目标时间点向前反推
                start_idx = max(0, target_idx - self.max_reverse_lines)
                for i in range(target_idx, start_idx, -1):
                    line = lines[i]
                    pid = self._extract_pid(line)

                    if pid == target_pid:
                        event = self._parse_log_line(line, reference_year)
                        if event:
                            events.append(event)
            except Exception as e:
                logger.warning("反推日志失败: %s, error=%s", log_file, e)
                continue

        # 按时间排序（反推顺序，最新的在前）
        events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)

        logger.info("反推完成，共找到 %d 个事件", len(events))
        return events

    def identify_problem_type(self, timeline: list[dict[str, Any]]) -> str:
        """根据日志线索识别问题类型

        关键词映射：
        - 3D 卡顿: Watchdog kick/fatal, UnityRequest count, UnityMain thread block
        - 场景信号: SIGNAL_SR_SCENE_TYPE, OnSceneChanged
        - 启动问题: displayChanged, startRender, UnityReady
        - 感知数据: VHALHelper, MapDataHandler
        - XTheme: 105004, 105009, XuiConditionHelper

        Args:
            timeline: 事件时间线

        Returns:
            建议的分析 Skill 名称
        """
        logger.info("开始识别问题类型")

        # 关键词映射
        keyword_mapping = {
            # 3D 卡顿
            "Watchdog kick": "3d-stuck-investigate",
            "Watchdog fatal": "3d-stuck-investigate",
            "UnityRequest count": "3d-stuck-investigate",
            "UnityMain thread block": "3d-stuck-investigate",
            "kill self for unity start dead": "3d-stuck-investigate",
            # 场景信号
            "SIGNAL_SR_SCENE_TYPE": "scene-signal-diagnosis",
            "OnSceneChanged": "scene-signal-diagnosis",
            "收到场景变化": "scene-signal-diagnosis",
            "SIGNAL_CUSTOM_GEAR_ST": "scene-signal-diagnosis",
            "SIGNAL_CUSTOM_PK_HMI_MODE": "scene-signal-diagnosis",
            # 启动问题
            "displayChanged": "unity-startup-lifecycle-check",
            "startRender": "unity-startup-lifecycle-check",
            "stopRender": "unity-startup-lifecycle-check",
            "surfaceCreated": "unity-startup-lifecycle-check",
            "surfaceDestroyed": "unity-startup-lifecycle-check",
            "UnityReady": "unity-startup-lifecycle-check",
            # 感知数据
            "VHALHelper": "perception-data-summary",
            "MapDataHandler": "perception-data-summary",
            "X3DCB": "perception-data-summary",
            "XDataNativeProxy": "perception-data-summary",
            # XTheme
            "105004": "xtheme-analyzer",
            "105009": "xtheme-analyzer",
            "XuiConditionHelper": "xtheme-analyzer",
        }

        # 统计关键词出现次数
        skill_counter: Counter[str] = Counter()
        for event in timeline:
            message = event.get("message", "")
            for keyword, skill in keyword_mapping.items():
                if keyword in message:
                    skill_counter[skill] += 1

        # 返回出现次数最多的 Skill
        if skill_counter:
            suggested_skill, count = skill_counter.most_common(1)[0]
            logger.info("识别到问题类型: skill=%s, 命中次数=%d", suggested_skill, count)
            return suggested_skill

        logger.info("未识别到特定问题类型，使用通用分析")
        return "general"

    # ============================================================
    # 内部方法
    # ============================================================

    def _find_log_files_for_time(
        self,
        log_dir: Path,
        target_time: datetime,
    ) -> list[Path]:
        """找到包含目标时间的日志文件

        日志文件命名规则：
        - 应用日志: {prefix}_{yyyy-MM-dd}_{HH}-{MM}.alog / .xlog / .log / .txt
        - 文件名前缀不固定，如 main_ 或 user0_main_
        - 每小时一个文件

        Args:
            log_dir: 日志根目录
            target_time: 目标时间

        Returns:
            包含目标时间的日志文件列表
        """
        log_files: list[Path] = []

        # 扫描应用日志目录
        app_log_dir = log_dir / "app" / self.process_name
        if not app_log_dir.exists():
            return log_files

        # 遍历所有能从文件名解析出时间段的应用日志
        for log_file in self._iter_app_log_files(app_log_dir):
            file_time = self._parse_log_file_time(log_file.name)
            if file_time:
                # 检查文件时间是否在目标时间附近
                # 日志文件每小时一个，所以检查 ±1 小时
                time_diff = abs((target_time - file_time).total_seconds())
                if time_diff < 3600:  # 1 小时 = 3600 秒
                    log_files.append(log_file)

        # 也扫描系统日志（可能包含目标时间）
        logd_dir = log_dir / "logd"
        if logd_dir.exists():
            for log_file in logd_dir.glob("*.txt"):
                log_files.append(log_file)

        return log_files

    def _iter_app_log_files(self, app_log_dir: Path) -> list[Path]:
        """列出命名里带固定时间段的应用日志文件"""
        log_files: list[Path] = []
        for log_file in app_log_dir.iterdir():
            if not log_file.is_file():
                continue
            if log_file.suffix.lower() not in {".alog", ".xlog", ".log", ".txt"}:
                continue
            if self._parse_log_file_time(log_file.name) is None:
                continue
            log_files.append(log_file)
        return log_files

    def _parse_log_file_time(self, filename: str) -> datetime | None:
        """从日志文件名解析时间

        支持格式：
        - main_{yyyy-MM-dd}_{HH}-{MM}.alog
        - user0_main_{yyyy-MM-dd}_{HH}-{MM}.alog
        - {prefix}_main_{yyyy-MM-dd}_{HH}-{MM}.alog

        Args:
            filename: 日志文件名

        Returns:
            解析的时间，失败返回 None
        """
        # 使用正则表达式匹配时间部分（支持任意前缀）
        match = LOG_FILE_PATTERN.search(filename)
        if match:
            try:
                date_str = match.group(1)
                hour = int(match.group(2))
                minute = int(match.group(3))
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                return dt.replace(hour=hour, minute=minute)
            except ValueError:
                return None
        return None

    def _read_log_file(self, log_file: Path) -> list[str]:
        """读取日志文件

        支持 .alog, .xlog, .txt 格式
        对于 .alog/.xlog 文件，会先尝试使用 log-decoder 解密

        Args:
            log_file: 日志文件路径

        Returns:
            日志行列表
        """
        # 1. 如果是二进制日志文件，先尝试解密
        if log_file.suffix.lower() in {".alog", ".xlog"}:
            decoded_file = self._decode_log_file(log_file)
            if decoded_file and decoded_file.exists():
                log_file = decoded_file

        # 2. 尝试作为文本文件读取
        try:
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                return f.readlines()
        except Exception as e:
            logger.warning("无法读取日志文件: %s, error=%s", log_file, e)
            return []

    def _decode_log_file(self, log_file: Path) -> Path | None:
        """使用 log-decoder 解密日志文件

        Args:
            log_file: 加密的日志文件路径

        Returns:
            解密后的日志文件路径，失败返回 None
        """
        # 解密后的文件路径：去掉扩展名，添加 .log 后缀
        decoded_file = log_file.with_suffix(".log")

        # 如果已经解密过，直接返回
        if decoded_file.exists():
            return decoded_file

        # 如果 log-decoder 不可用，返回 None
        if not self._log_decoder_available:
            logger.debug("log-decoder 不可用，跳过解密: %s", log_file)
            return None

        try:
            logger.info("解密日志文件: %s", log_file)

            # 使用 log-decoder 解密
            completed = subprocess.run(
                ["java", "-jar", str(self._log_decoder_jar), log_file.name],
                cwd=log_file.parent,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

            if completed.returncode != 0:
                logger.warning("日志解密失败: %s, error=%s", log_file, completed.stderr)
                return None

            # 检查解密后的文件是否存在
            if decoded_file.exists():
                logger.info("日志解密成功: %s -> %s", log_file, decoded_file)
                return decoded_file

            # 尝试其他可能的输出文件名
            for suffix in [".txt", ".log", ""]:
                candidate = log_file.with_suffix(suffix)
                if candidate.exists() and candidate != log_file:
                    logger.info("日志解密成功: %s -> %s", log_file, candidate)
                    return candidate

            logger.warning("日志解密后未找到输出文件: %s", log_file)
            return None

        except subprocess.TimeoutExpired:
            logger.warning("日志解密超时: %s", log_file)
            return None
        except Exception as e:
            logger.warning("日志解密异常: %s, error=%s", log_file, e)
            return None

    def _file_contains_pid(self, log_file: Path, target_pid: int) -> bool:
        """检查日志文件是否包含目标 PID

        Args:
            log_file: 日志文件路径
            target_pid: 目标进程号

        Returns:
            是否包含目标 PID
        """
        try:
            lines = self._read_log_file(log_file)
            # 只检查前 1000 行，避免读取整个大文件
            for line in lines[:1000]:
                pid = self._extract_pid(line)
                if pid == target_pid:
                    return True
            return False
        except Exception:
            return False

    def _extract_timestamp(self, line: str, reference_year: int) -> datetime | None:
        """从日志行提取时间戳

        支持格式：
        - "2026-05-25 16:50:41.123"
        - "05-25 16:50:41.123"

        Args:
            line: 日志行
            reference_year: 参考年份（用于短格式）

        Returns:
            解析的时间，失败返回 None
        """
        # 尝试完整格式
        match = FULL_DATETIME_RE.search(line)
        if match:
            try:
                return datetime(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                    int(match.group("hour")),
                    int(match.group("minute")),
                    int(match.group("second")),
                )
            except ValueError:
                pass

        # 尝试短格式
        match = SHORT_DATETIME_RE.search(line)
        if match:
            try:
                return datetime(
                    reference_year,
                    int(match.group("month")),
                    int(match.group("day")),
                    int(match.group("hour")),
                    int(match.group("minute")),
                    int(match.group("second")),
                )
            except ValueError:
                pass

        return None

    def _extract_pid(self, line: str) -> int | None:
        """从日志行提取 PID

        日志行格式: "05-25 16:50:41.123  1234  5678 I TAG: message"
                                        ↑
                                     PID

        Args:
            line: 日志行

        Returns:
            PID，失败返回 None
        """
        match = LOG_LINE_PID_RE.search(line)
        if match:
            try:
                return int(match.group("pid"))
            except ValueError:
                pass
        return None

    def _find_time_index(
        self,
        lines: list[str],
        target_time: datetime,
        reference_year: int,
    ) -> int:
        """找到目标时间点在日志中的位置

        使用二分查找定位目标时间

        Args:
            lines: 日志行列表
            target_time: 目标时间
            reference_year: 参考年份

        Returns:
            目标时间点的位置索引
        """
        left, right = 0, len(lines) - 1
        result = len(lines) - 1

        while left <= right:
            mid = (left + right) // 2
            ts = self._extract_timestamp(lines[mid], reference_year)

            if ts is None:
                # 跳过无法解析的时间戳
                left = mid + 1
                continue

            if ts < target_time:
                left = mid + 1
            else:
                result = mid
                right = mid - 1

        return result

    def _parse_log_line(self, line: str, reference_year: int) -> dict[str, Any] | None:
        """解析日志行

        提取时间戳、PID、TID、日志级别、TAG、消息

        Args:
            line: 日志行
            reference_year: 参考年份

        Returns:
            解析结果字典，失败返回 None
        """
        ts = self._extract_timestamp(line, reference_year)
        pid = self._extract_pid(line)

        if ts is None or pid is None:
            return None

        # 尝试提取更多信息
        parts = line.split()
        tid = None
        level = None
        tag = None
        message = line

        # 日志行格式: "05-25 16:50:41.123  1234  5678 I TAG: message"
        # parts:      [0]     [1]          [2]  [3] [4] [5]  [6:]
        if len(parts) >= 4:
            try:
                tid = int(parts[3])  # TID 在第 4 个位置
            except ValueError:
                pass

        if len(parts) >= 5:
            level = parts[4]  # 日志级别在第 5 个位置

        if len(parts) >= 6:
            tag = parts[5].rstrip(":")  # TAG 在第 6 个位置
            message = " ".join(parts[6:])  # 消息从第 7 个位置开始

        return {
            "timestamp": ts.isoformat(),
            "pid": pid,
            "tid": tid,
            "level": level,
            "tag": tag,
            "message": message,
            "raw": line.strip(),
        }
