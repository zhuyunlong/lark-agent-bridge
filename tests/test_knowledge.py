from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from lark_agent_bridge.knowledge import KnowledgeService
from lark_agent_bridge.knowledge.models import KnowledgeChunk, SearchHit
from lark_agent_bridge.knowledge.source_investigation import SourceInvestigationRunner
from lark_agent_bridge.models import BridgeConfig, KnowledgeOptions, KnowledgeSourceOptions
from lark_agent_bridge.models import SourceInvestigationOptions


class KnowledgeServiceTests(unittest.TestCase):
    def test_syncs_local_adb_json_and_searches_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adb_path = root / "adb_data.json"
            adb_path.write_text(
                json.dumps(
                    {
                        "commands": [
                            {
                                "name": "开启I&D级别日志",
                                "command": "adb shell am broadcast -a com.xiaopeng.montecarlo.TEST_ENABLE_DETAIL_NAVI_LOG --ei log_enable 2",
                                "group": "开发调试",
                                "description": "打开导航详细日志",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config = BridgeConfig(
                data_dir=root,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(
                            id="guideengine-adb",
                            type="local_json",
                            path=str(adb_path),
                        )
                    ],
                ),
            )

            service = KnowledgeService(config)
            sync_result = service.sync_all()
            hits = service.search("怎么开启导航详细日志")

        self.assertEqual(sync_result["total_chunks"], 1)
        self.assertEqual(hits[0].source_id, "guideengine-adb")
        self.assertIn("开启I&D级别日志", hits[0].title)
        self.assertIn("TEST_ENABLE_DETAIL_NAVI_LOG", hits[0].content)

    def test_search_auto_syncs_fresh_index_for_http_queries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adb_path = root / "adb_data.json"
            adb_path.write_text(
                json.dumps(
                    {
                        "commands": [
                            {
                                "name": "打开Debug面板",
                                "command": "adb shell am start -a com.xiaopeng.intent.action.DEV_BOARD",
                                "group": "开发调试",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config = BridgeConfig(
                data_dir=root,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(
                            id="guideengine-adb",
                            type="local_json",
                            path=str(adb_path),
                        )
                    ],
                ),
            )

            service = KnowledgeService(config)
            hits = service.search("打开Debug面板")
            sources = service.list_sources()

        self.assertEqual(hits[0].title, "打开Debug面板")
        self.assertEqual(sources[0]["id"], "guideengine-adb")
        self.assertEqual(sources[0]["chunk_count"], 1)

    def test_signal_simulation_questions_are_routed_without_kb_prefix(self):
        service = KnowledgeService(BridgeConfig(knowledge=KnowledgeOptions(enabled=True)))

        self.assertTrue(service.should_handle("OTA信号如何模拟"))
        self.assertTrue(service.should_handle("主题信号怎么模拟"))
        self.assertTrue(service.should_handle("PB对象怎么ADB模拟"))
        self.assertTrue(service.should_handle("上下电如何模拟"))
        self.assertTrue(service.should_handle("3D天气信号如何模拟"))
        self.assertTrue(service.should_handle("3D场景信号如何模拟"))
        self.assertTrue(service.should_handle("上电P如何模拟"))
        self.assertTrue(service.should_handle("ld调试命令"))

    def test_unrelated_simulation_question_does_not_match_generic_adb_templates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guideengine = _write_minimal_guideengine_sources(root / "guideengine")
            adb_path = root / "adb_data.json"
            adb_path.write_text(
                json.dumps(
                    {
                        "commands": [
                            {
                                "name": "模拟 XPU 信号",
                                "command": "adb shell am broadcast -a com.xiaopeng.intent.action.mock.autopilot.tips --ei scene 2",
                                "group": "泊车",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=guideengine,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(id="guideengine-signals", type="guideengine_signal"),
                        KnowledgeSourceOptions(id="guideengine-adb", type="local_json", path=str(adb_path)),
                    ],
                ),
            )

            service = KnowledgeService(config)
            service.sync_all()
            hits = service.search("星际如何模拟")
            answer = service.answer("星际如何模拟")

        self.assertEqual(hits, [])
        self.assertFalse(answer.success)
        self.assertEqual(answer.error_code, "knowledge_no_hits")
        self.assertEqual(answer.details["knowledge_hits"], [])
        self.assertNotIn("SIGNAL_MCU_IG_ST", answer.message)
        self.assertNotIn("模拟 XPU 信号", answer.message)

    def test_operation_question_offers_low_confidence_command_candidate_without_topic_hardcode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adb_path = root / "adb_data.json"
            adb_path.write_text(
                json.dumps(
                    {
                        "commands": [
                            {
                                "name": "直接发送文本给小P",
                                "command": (
                                    "adb shell am broadcast -a carspeechservice.ACTION_SEND_TEXT "
                                    "--es text \"打开车窗\" --ei soundArea 2"
                                ),
                                "group": "语音",
                            },
                            {
                                "name": "模拟 XPU 信号",
                                "command": "adb shell am broadcast -a com.xiaopeng.intent.action.mock.autopilot.tips --ei scene 2",
                                "group": "泊车",
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config = BridgeConfig(
                data_dir=root,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(
                            id="guideengine-adb",
                            type="local_json",
                            path=str(adb_path),
                        )
                    ],
                ),
            )

            service = KnowledgeService(config)
            service.sync_all()
            hits = service.search("车窗如何模拟")
            answer = service.answer("车窗如何模拟")

        self.assertEqual(hits, [])
        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "low_confidence_candidates")
        self.assertIn("低置信候选", answer.message)
        self.assertIn("直接发送文本给小P", answer.message)
        self.assertIn("carspeechservice.ACTION_SEND_TEXT", answer.message)
        self.assertIn("打开车窗", answer.message)
        self.assertNotIn("模拟 XPU 信号", answer.message)

    def test_broad_operation_question_prefers_executable_candidate_over_source_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adb_path = root / "adb_data.json"
            adb_path.write_text(
                json.dumps(
                    {
                        "commands": [
                            {
                                "name": "直接发送文本给小P",
                                "command": (
                                    "adb shell am broadcast -a carspeechservice.ACTION_SEND_TEXT "
                                    "--es text \"打开车窗\" --ei soundArea 2"
                                ),
                                "group": "语音",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config = BridgeConfig(
                data_dir=root,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(
                            id="guideengine-adb",
                            type="local_json",
                            path=str(adb_path),
                        )
                    ],
                ),
            )
            service = KnowledgeService(config)
            service.sync_all()
            service.store.add_chunks(
                source_id="derived-adb-simulations",
                source_type="source_derived",
                title="车窗源码沉淀知识",
                source_ref="/repo",
                chunks=[
                    KnowledgeChunk(
                        id="derived-window",
                        source_id="derived-adb-simulations",
                        title="车窗源码沉淀知识",
                        content="车窗已有源码沉淀摘要，但不包含可直接执行命令。",
                        source_ref="/repo",
                        kind="adb_signal_template",
                    )
                ],
            )

            answer = service.answer("车窗如何模拟")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "low_confidence_candidates")
        self.assertIn("carspeechservice.ACTION_SEND_TEXT", answer.message)
        self.assertNotIn("车窗已有源码沉淀摘要", answer.message)

    def test_ld_debug_command_query_returns_replay_receiver_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adb_path = root / "adb_data.json"
            adb_path.write_text(
                json.dumps(
                    {
                        "commands": [
                            {
                                "name": "关闭I&D级别日志",
                                "command": (
                                    "adb shell am broadcast -a "
                                    "com.xiaopeng.montecarlo.TEST_ENABLE_DETAIL_NAVI_LOG --ei log_enable 0"
                                ),
                                "group": "开发调试",
                            },
                            {
                                "name": "开启I&D级别日志",
                                "command": (
                                    "adb shell am broadcast -a "
                                    "com.xiaopeng.montecarlo.TEST_ENABLE_DETAIL_NAVI_LOG --ei log_enable 2"
                                ),
                                "group": "开发调试",
                            },
                            {
                                "name": "开启I级别日志",
                                "command": (
                                    "adb shell am broadcast -a "
                                    "com.xiaopeng.montecarlo.TEST_ENABLE_DETAIL_NAVI_LOG --ei log_enable 1"
                                ),
                                "group": "开发调试",
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config = BridgeConfig(
                data_dir=root,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(
                            id="guideengine-adb",
                            type="local_json",
                            path=str(adb_path),
                        )
                    ],
                ),
            )
            service = KnowledgeService(config)
            service.sync_all()

            answer = service.answer("ld调试 命令")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "adb_signal_template")
        self.assertEqual(answer.details["canonical_key"], "adb-sim:LD_DEBUG_SHOW")
        self.assertNotIn("低置信候选", answer.message)
        self.assertIn("LD 调试显示使用 ReplayReceiver 的专用广播", answer.message)
        self.assertIn("com.xiaopeng.guide.action.hmi.showLD", answer.message)
        self.assertIn("com.xiaopeng.guide.action.hmi.showLDReset", answer.message)
        self.assertIn("SIGNAL_DEBUG_SHOW_LD_DEBUG_INFO", answer.message)
        self.assertNotIn("I&D", answer.message)
        self.assertNotIn("TEST_ENABLE_DETAIL_NAVI_LOG", answer.message)

    def test_template_matching_normalizes_spaces_and_separators_for_ld_debug_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = KnowledgeService(
                BridgeConfig(
                    data_dir=root,
                    knowledge=KnowledgeOptions(enabled=True, storage=root / "knowledge.sqlite"),
                )
            )

            variants = ["ld调试命令", "ld调试 命令", "LD 调试 命令", "ld-debug 命令", "ld_debug命令"]
            for question in variants:
                with self.subTest(question=question):
                    answer = service.answer(question)
                    self.assertTrue(answer.success)
                    self.assertEqual(answer.details["answer_type"], "adb_signal_template")
                    self.assertEqual(answer.details["canonical_key"], "adb-sim:LD_DEBUG_SHOW")
                    self.assertIn("com.xiaopeng.guide.action.hmi.showLD", answer.message)

    def test_low_confidence_candidates_do_not_use_generic_debug_term_as_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adb_path = root / "adb_data.json"
            adb_path.write_text(
                json.dumps(
                    {
                        "commands": [
                            {
                                "name": "开启I&D级别日志",
                                "command": (
                                    "adb shell am broadcast -a "
                                    "com.xiaopeng.montecarlo.TEST_ENABLE_DETAIL_NAVI_LOG --ei log_enable 2"
                                ),
                                "group": "开发调试",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            service = KnowledgeService(
                BridgeConfig(
                    data_dir=root,
                    knowledge=KnowledgeOptions(
                        enabled=True,
                        storage=root / "knowledge.sqlite",
                        sources=[
                            KnowledgeSourceOptions(
                                id="guideengine-adb",
                                type="local_json",
                                path=str(adb_path),
                            )
                        ],
                    ),
                )
            )
            service.sync_all()

            answer = service.answer("foo调试 命令")

        self.assertFalse(answer.success)
        self.assertEqual(answer.error_code, "knowledge_no_hits")
        self.assertNotIn("TEST_ENABLE_DETAIL_NAVI_LOG", answer.message)

    def test_signal_hits_are_not_replaced_by_generic_low_confidence_debug_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adb_path = root / "adb_data.json"
            adb_path.write_text(
                json.dumps(
                    {
                        "commands": [
                            {
                                "name": "开启I&D级别日志",
                                "command": (
                                    "adb shell am broadcast -a "
                                    "com.xiaopeng.montecarlo.TEST_ENABLE_DETAIL_NAVI_LOG --ei log_enable 2"
                                ),
                                "group": "开发调试",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            service = KnowledgeService(
                BridgeConfig(
                    data_dir=root,
                    knowledge=KnowledgeOptions(
                        enabled=True,
                        storage=root / "knowledge.sqlite",
                        sources=[
                            KnowledgeSourceOptions(
                                id="guideengine-adb",
                                type="local_json",
                                path=str(adb_path),
                            )
                        ],
                    ),
                )
            )
            service.sync_all()
            service.store.add_chunks(
                source_id="guideengine-signals",
                source_type="guideengine_signal",
                title="Signals",
                source_ref="",
                chunks=[
                    KnowledgeChunk(
                        id="signal-debug-foo",
                        source_id="guideengine-signals",
                        title="SIGNAL_DEBUG_FOO (190020)",
                        content="signal: SIGNAL_DEBUG_FOO\ncomment: FOO 调试信息\n",
                        source_ref="/path/signal.proto",
                        kind="signal_proto_entry",
                        metadata={"keywords": ["SIGNAL_DEBUG_FOO", "foo", "调试"]},
                    )
                ],
            )

            answer = service.answer("foo调试 命令")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "retrieval_summary")
        self.assertIn("SIGNAL_DEBUG_FOO", answer.message)
        self.assertNotIn("低置信候选", answer.message)
        self.assertNotIn("TEST_ENABLE_DETAIL_NAVI_LOG", answer.message)

    def test_answers_ota_signal_with_deterministic_adb_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guideengine = _write_minimal_guideengine_sources(root / "guideengine")
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=guideengine,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(id="guideengine-signals", type="guideengine_signal"),
                    ],
                ),
            )

            service = KnowledgeService(config)
            service.sync_all()
            answer = service.answer("SIGNAL_OTA_ST 四种组合指令")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["mode"], "knowledge_qa")
        self.assertIn("SIGNAL_OTA_ST 四种组合指令", answer.message)
        self.assertIn("--ei code 105003 --ei format 7 --es value \"[0, 0]\"", answer.message)
        self.assertIn("OTA_CAMPAIGN_SHOW", answer.message)
        self.assertIn("OTA_UPGRADE_AFTER_VIDEO", answer.message)
        self.assertEqual(len(answer.details["knowledge_hits"]), 1)
        self.assertIn("SIGNAL_OTA_ST", answer.details["knowledge_hits"][0]["title"])

    def test_answers_fuzzy_ota_signal_question_with_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guideengine = _write_minimal_guideengine_sources(root / "guideengine")
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=guideengine,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(id="guideengine-signals", type="guideengine_signal"),
                    ],
                ),
            )

            service = KnowledgeService(config)
            service.sync_all()
            answer = service.answer("OTA信号如何模拟")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "adb_signal_template")
        self.assertIn("SIGNAL_OTA_ST 四种组合指令", answer.message)
        self.assertIn("--ei code 105003 --ei format 7", answer.message)

    def test_answers_3d_weather_with_verified_datacenter_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = KnowledgeService(
                BridgeConfig(
                    data_dir=root,
                    knowledge=KnowledgeOptions(enabled=True, storage=root / "knowledge.sqlite"),
                )
            )

            with patch("lark_agent_bridge.knowledge.source_investigation.subprocess.run") as mocked_run:
                answer = service.answer("3D天气信号如何模拟")

        mocked_run.assert_not_called()
        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "adb_signal_template")
        self.assertEqual(answer.details["canonical_key"], "adb-sim:SIGNAL_XUI_WEATHER")
        self.assertIn("SIGNAL_XUI_WEATHER", answer.message)
        self.assertIn(
            "adb shell am broadcast -a com.xiaopeng.guide.action.mock.datacenter "
            "--ei code 70010 --ei format 7 --es value CLOUDY",
            answer.message,
        )
        self.assertIn("format 7", answer.message)

    def test_answers_3d_scene_with_sr_scene_type_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = KnowledgeService(
                BridgeConfig(
                    data_dir=root,
                    knowledge=KnowledgeOptions(enabled=True, storage=root / "knowledge.sqlite"),
                )
            )

            answer = service.answer("3D场景信号如何模拟")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "adb_signal_template")
        self.assertEqual(answer.details["canonical_key"], "adb-sim:SIGNAL_SR_SCENE_TYPE")
        self.assertIn("SIGNAL_SR_SCENE_TYPE", answer.message)
        self.assertIn("--ei code 100002 --ei format 3 --es value 8", answer.message)
        self.assertIn("UnitySceneTypeService", answer.message)

    def test_answers_power_on_p_scene_with_mock_scene_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = KnowledgeService(
                BridgeConfig(
                    data_dir=root,
                    knowledge=KnowledgeOptions(enabled=True, storage=root / "knowledge.sqlite"),
                )
            )

            answer = service.answer("上电临停P如何模拟")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "adb_signal_template")
        self.assertEqual(answer.details["canonical_key"], "adb-sim:POWER_ON_P_SCENE")
        self.assertIn(
            "adb shell am broadcast -a com.xiaopeng.guide.action.mock.scene --ei type 1 --es value 1",
            answer.message,
        )
        self.assertIn(
            "adb shell am broadcast -a com.xiaopeng.guide.action.mock.scene --ei type 1 --es value 0",
            answer.message,
        )
        self.assertNotEqual(answer.details["canonical_key"], "adb-sim:SIGNAL_MCU_IG_ST")

    def test_template_answer_reference_prefers_specific_signal_hit_over_generic_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = KnowledgeService(
                BridgeConfig(
                    data_dir=root,
                    knowledge=KnowledgeOptions(enabled=True, storage=root / "knowledge.sqlite"),
                )
            )
            service.store.add_chunks(
                source_id="guideengine-signals",
                source_type="guideengine_signal",
                title="Signals",
                source_ref="",
                chunks=[
                    KnowledgeChunk(
                        id="generic",
                        source_id="guideengine-signals",
                        title="OTA 信号如何模拟 DataCenterBroadcastReceiver 通用信号模拟入口",
                        content="OTA mock.datacenter code format value 通用入口。",
                        source_ref="/path/DataCenterBroadcastReceiver.java",
                        kind="guideengine_source",
                    ),
                    KnowledgeChunk(
                        id="specific",
                        source_id="guideengine-signals",
                        title="SIGNAL_OTA_ST ADB 四种常见组合",
                        content="SIGNAL_OTA_ST code 105003 format 7 OTA 模拟指令。",
                        source_ref="generated:guideengine_signal_template",
                        kind="adb_signal_template",
                        metadata={"signal": "SIGNAL_OTA_ST", "code": 105003},
                    ),
                ],
            )

            answer = service.answer("OTA信号如何模拟")

        self.assertEqual(answer.details["knowledge_hits"][0]["title"], "SIGNAL_OTA_ST ADB 四种常见组合")

    def test_theme_signal_question_lists_simulation_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guideengine = _write_minimal_guideengine_sources(root / "guideengine")
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=guideengine,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(id="guideengine-signals", type="guideengine_signal"),
                    ],
                ),
            )

            service = KnowledgeService(config)
            service.sync_all()
            answer = service.answer("主题信号如何模拟")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "adb_signal_candidates")
        self.assertIn("命中多个可能的主题模拟信号", answer.message)
        self.assertIn("SIGNAL_SR_XTHEME", answer.message)
        self.assertIn("105004", answer.message)
        self.assertIn("--ei format 18", answer.message)
        self.assertIn("timePeriod,themeMode", answer.message)
        self.assertIn("SIGNAL_SR_XTHEME_MSG", answer.message)
        self.assertIn("继续发：知识库 模拟 SIGNAL_SR_XTHEME", answer.message)

    def test_signal_question_with_proto_only_hits_returns_source_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = KnowledgeService(
                BridgeConfig(
                    data_dir=root,
                    knowledge=KnowledgeOptions(enabled=True, storage=root / "knowledge.sqlite"),
                )
            )
            service.store.add_chunks(
                source_id="guideengine-signals",
                source_type="guideengine_signal",
                title="Signals",
                source_ref="",
                chunks=[
                    KnowledgeChunk(
                        id="custom-scene",
                        source_id="guideengine-signals",
                        title="SIGNAL_CUSTOM_SPECIAL_SCENE_TYPE (100010)",
                        content=(
                            "signal: SIGNAL_CUSTOM_SPECIAL_SCENE_TYPE\n"
                            "code: 100010\n"
                            "comment: SR 综合信号 100000 - 100999 | 特殊场景信号\n"
                            "line: 652\n"
                            "tokens: signal custom special scene type"
                        ),
                        source_ref="/path/signal.proto",
                        kind="signal_proto_entry",
                        metadata={
                            "signal": "SIGNAL_CUSTOM_SPECIAL_SCENE_TYPE",
                            "code": "100010",
                            "line": 652,
                            "keywords": ["SIGNAL_CUSTOM_SPECIAL_SCENE_TYPE", "100010", "特殊场景信号"],
                        },
                    ),
                    KnowledgeChunk(
                        id="first-frame-ready",
                        source_id="guideengine-signals",
                        title="SIGNAL_X3D_CARSCENE_CAMERA_FIRST_FRAME_READY (133011)",
                        content=(
                            "signal: SIGNAL_X3D_CARSCENE_CAMERA_FIRST_FRAME_READY\n"
                            "code: 133011\n"
                            "comment: ============================3D业务信号 130000 - 139999====================== | 特殊场景首帧渲染\n"
                            "line: 889\n"
                            "tokens: signal x3d carscene camera first frame ready"
                        ),
                        source_ref="/path/signal.proto",
                        kind="signal_proto_entry",
                        metadata={
                            "signal": "SIGNAL_X3D_CARSCENE_CAMERA_FIRST_FRAME_READY",
                            "code": "133011",
                            "line": 889,
                            "keywords": [
                                "SIGNAL_X3D_CARSCENE_CAMERA_FIRST_FRAME_READY",
                                "133011",
                                "特殊场景首帧渲染",
                            ],
                        },
                    ),
                ],
            )

            answer = service.answer("知识库 特殊场景信号如何模拟")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "adb_signal_source_candidates")
        self.assertIn("当前命中 2 个可能相关的信号候选", answer.message)
        self.assertIn("SIGNAL_CUSTOM_SPECIAL_SCENE_TYPE (100010)", answer.message)
        self.assertIn("特殊场景信号", answer.message)
        self.assertIn("SIGNAL_X3D_CARSCENE_CAMERA_FIRST_FRAME_READY (133011)", answer.message)
        self.assertIn("特殊场景首帧渲染", answer.message)
        self.assertIn("继续发：知识库 模拟 SIGNAL_CUSTOM_SPECIAL_SCENE_TYPE", answer.message)
        self.assertIn("继续发：知识库 源码调查 特殊场景信号如何模拟", answer.message)
        self.assertIn("不能直接给可执行 ADB 命令", answer.message)

    def test_exact_xtheme_followup_returns_adb_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guideengine = _write_minimal_guideengine_sources(root / "guideengine")
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=guideengine,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(id="guideengine-signals", type="guideengine_signal"),
                    ],
                ),
            )

            service = KnowledgeService(config)
            service.sync_all()
            answer = service.answer("知识库 模拟 SIGNAL_SR_XTHEME")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "adb_signal_template")
        self.assertIn("SIGNAL_SR_XTHEME 常见组合指令", answer.message)
        self.assertIn("--ei code 105004 --ei format 18 --es value \"1,0\"", answer.message)

    def test_power_cycle_question_uses_source_derived_ig_template_and_records_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guideengine = _write_minimal_guideengine_sources(root / "guideengine")
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=guideengine,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(id="guideengine-signals", type="guideengine_signal"),
                    ],
                ),
            )

            service = KnowledgeService(config)
            answer = service.answer("上下电如何模拟")
            hits = service.search("上下电如何模拟")
            sources = {source["id"]: source for source in service.list_sources()}

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "adb_signal_template")
        self.assertIn("SIGNAL_MCU_IG_ST 上下电模拟指令", answer.message)
        self.assertIn("--ei code 36001 --ei format 3 --es value 0", answer.message)
        self.assertIn("--ei code 36001 --ei format 3 --es value 1", answer.message)
        self.assertIn("--ei code 36001 --ei format 3 --es value 2", answer.message)
        self.assertNotIn("com.xiaopeng.intent.action.mock.datacenter", answer.message)
        self.assertEqual(len(answer.details["knowledge_hits"]), 1)
        self.assertEqual(answer.details["knowledge_hits"][0]["source_id"], "derived-adb-simulations")
        self.assertEqual(hits[0].source_id, "derived-adb-simulations")
        self.assertNotIn("com.xiaopeng.intent.action.mock.datacenter", hits[0].content)
        self.assertIn("derived-adb-simulations", sources)
        self.assertGreaterEqual(sources["derived-adb-simulations"]["chunk_count"], 1)

    def test_business_signal_terms_are_not_hardcoded_in_production_python(self):
        root = Path(__file__).resolve().parents[1]
        production_files = [
            root / "lark_agent_bridge/knowledge/service.py",
            root / "lark_agent_bridge/knowledge/ingestors.py",
            root / "lark_agent_bridge/models.py",
        ]

        for path in production_files:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("SIGNAL_MCU_IG_ST", text)
                self.assertNotIn("36001", text)
                self.assertNotIn("上下电", text)
                self.assertNotIn("SIGNAL_OTA_ST", text)
                self.assertNotIn("105003", text)
                self.assertNotIn("主题", text)

    def test_template_aliases_and_negative_aliases_are_loaded_from_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = BridgeConfig(
                data_dir=root,
                knowledge=KnowledgeOptions(enabled=True, storage=root / "knowledge.sqlite"),
            )

            service = KnowledgeService(config)
            alias_answer = service.answer("点火状态怎么造")
            negative_answer = service.answer("上电P如何模拟")

        self.assertTrue(alias_answer.success)
        self.assertEqual(alias_answer.details["answer_type"], "adb_signal_template")
        self.assertEqual(alias_answer.details["canonical_key"], "adb-sim:SIGNAL_MCU_IG_ST")
        self.assertIn("SIGNAL_MCU_IG_ST", alias_answer.message)
        self.assertTrue(negative_answer.success)
        self.assertEqual(negative_answer.details["canonical_key"], "adb-sim:POWER_ON_P_SCENE")
        self.assertIn("mock.scene --ei type 1", negative_answer.message)

    def test_guideengine_signal_sync_indexes_full_signal_proto_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guideengine = _write_minimal_guideengine_sources(root / "guideengine")
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=guideengine,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(id="guideengine-signals", type="guideengine_signal"),
                    ],
                ),
            )

            service = KnowledgeService(config)
            service.sync_all()
            hits = service.search("展示ld debug信息", limit=3)
            answer = service.answer("知识库 展示ld debug信息")

        self.assertTrue(hits)
        self.assertEqual(hits[0].title, "SIGNAL_DEBUG_SHOW_LD_DEBUG_INFO (190015)")
        self.assertIn("signal.proto", hits[0].source_ref)
        self.assertIn("SIGNAL_DEBUG_SHOW_LD_DEBUG_INFO", hits[0].content)
        self.assertIn("190015", hits[0].content)
        self.assertIn("SR调试信号", hits[0].content)
        self.assertIn("展示 LD debug 信息", hits[0].content)
        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "retrieval_summary")
        self.assertIn("SIGNAL_DEBUG_SHOW_LD_DEBUG_INFO", answer.message)
        self.assertIn("190015", answer.message)
        self.assertIn("展示 LD debug 信息", answer.message)

    def test_search_refreshes_stale_guideengine_signal_source_without_proto_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guideengine = _write_minimal_guideengine_sources(root / "guideengine")
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=guideengine,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(id="guideengine-signals", type="guideengine_signal"),
                    ],
                ),
            )
            service = KnowledgeService(config)
            service.store.replace_source(
                source_id="guideengine-signals",
                source_type="guideengine_signal",
                title="旧信号源码索引",
                source_ref=str(guideengine),
                chunks=[
                    KnowledgeChunk(
                        id="stale",
                        source_id="guideengine-signals",
                        title="旧源码片段",
                        content="不包含完整 signal.proto 枚举",
                        source_ref=str(guideengine),
                        kind="guideengine_source",
                    )
                ],
            )

            hits = service.search("展示ld debug信息", limit=3)
            sources = {source["id"]: source for source in service.list_sources()}

        self.assertTrue(hits)
        self.assertEqual(hits[0].title, "SIGNAL_DEBUG_SHOW_LD_DEBUG_INFO (190015)")
        self.assertGreater(sources["guideengine-signals"]["chunk_count"], 1)

    def test_source_investigation_success_writes_source_derived_template(self):
        payload = {
            "answer": (
                "SIGNAL_NEW_TEST 模拟指令\n"
                "adb shell am broadcast -a com.xiaopeng.guide.action.mock.datacenter --ei code 12345 --ei format 3 --es value 1"
            ),
            "canonical_key": "adb-sim:SIGNAL_NEW_TEST",
            "confidence": 0.91,
            "commands": [
                "adb shell am broadcast -a com.xiaopeng.guide.action.mock.datacenter --ei code 12345 --ei format 3 --es value 1"
            ],
            "source_evidence": [
                {"file": "module_proto/src/main/proto/signal.proto", "line": 12, "text": "SIGNAL_NEW_TEST = 12345;"}
            ],
            "coverage_boundary": "scanned guideengine",
            "writeback_allowed": True,
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=root / "guideengine",
                knowledge=KnowledgeOptions(enabled=True, storage=root / "knowledge.sqlite"),
            )

            def fake_run(command, cwd, capture_output, text, timeout, check):
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, '{"type":"turn.completed"}\n', "")

            service = KnowledgeService(config)
            with patch("lark_agent_bridge.knowledge.source_investigation.subprocess.run", side_effect=fake_run):
                answer = service.answer("源码调查新测试信号如何模拟")
            hits = service.search("SIGNAL_NEW_TEST")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "source_investigation")
        self.assertEqual(answer.details["canonical_key"], "adb-sim:SIGNAL_NEW_TEST")
        self.assertIn("SIGNAL_NEW_TEST 模拟指令", answer.message)
        self.assertEqual(hits[0].source_id, "derived-adb-simulations")
        self.assertEqual(hits[0].metadata["canonical_key"], "adb-sim:SIGNAL_NEW_TEST")

    def test_source_investigation_runner_builds_read_only_codex_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=root / "guideengine",
                source_investigation=SourceInvestigationOptions(
                    repo_roots=[root / "guideengine"],
                    add_dirs=[root / "Napa5"],
                ),
            )
            runner = SourceInvestigationRunner(config)
            command = runner._build_command(
                question="未知信号如何模拟",
                output_path=root / "last.json",
                primary_root=root / "guideengine",
            )

        self.assertEqual(command[0:2], ["codex", "exec"])
        self.assertIn("--json", command)
        self.assertIn("--output-last-message", command)
        self.assertEqual(command[command.index("-s") + 1], "read-only")
        self.assertEqual(command[command.index("-m") + 1], "gpt-5.4")
        self.assertEqual(command[command.index("-C") + 1], str(root / "guideengine"))
        self.assertEqual(command[command.index("--add-dir") + 1], str(root / "Napa5"))

    def test_source_investigation_runner_uses_absolute_output_path_for_relative_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original_cwd = os.getcwd()
            try:
                os.chdir(root)
                config = BridgeConfig(
                    data_dir=Path("data"),
                    guideengine_repo=root / "guideengine",
                )
                runner = SourceInvestigationRunner(config)
                output_path = runner._output_path()
            finally:
                os.chdir(original_cwd)

        self.assertTrue(output_path.is_absolute())
        self.assertEqual(output_path.parent, (root / "data" / "source_investigations").resolve())

    def test_source_investigation_runner_reads_agent_message_from_json_stream(self):
        payload = {
            "answer": "3D 场景信号可通过 mock datacenter 广播模拟。",
            "canonical_key": "adb-sim:3d-scene",
            "confidence": 0.86,
            "commands": [
                "adb shell am broadcast -a com.xiaopeng.guide.action.mock.datacenter --ei code 3500003 --ei format 1 --es value 1"
            ],
            "source_evidence": [
                {"file": "module_datacenter/DataCenterBroadcastReceiver.java", "line": 110, "text": "mockSignal"}
            ],
            "coverage_boundary": "scanned guideengine datacenter mock path",
            "writeback_allowed": True,
        }
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "t1"}),
                json.dumps({"type": "turn.started"}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(payload)}}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=root / "guideengine",
            )

            def fake_run(command, cwd, capture_output, text, timeout, check):
                return subprocess.CompletedProcess(command, 0, stdout, "")

            runner = SourceInvestigationRunner(config)
            with patch("lark_agent_bridge.knowledge.source_investigation.subprocess.run", side_effect=fake_run):
                result = runner.run("3D场景信号如何模拟")

        self.assertTrue(result.success)
        self.assertEqual(result.canonical_key, "adb-sim:3d-scene")
        self.assertIn("mock datacenter", result.answer)

    def test_source_investigation_prompt_includes_prefetched_source_excerpts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guideengine = root / "guideengine"
            signal_proto = guideengine / "module_floorcenter/module_proto/src/main/proto/signal.proto"
            signal_proto.parent.mkdir(parents=True, exist_ok=True)
            signal_proto.write_text(
                "enum SignalCode {\n"
                "    SIGNAL_CTL_XPILOT_START_REMIDE_ST = 15012; // 前车起步开启状态\n"
                "    SIGNAL_X3D_DATA_SERVICE_START_REMIND = 150006; // 前车起步 int\n"
                "}\n",
                encoding="utf-8",
            )
            signal_mapping = (
                guideengine
                / "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/define/mapping/code/SignalMapping.kt"
            )
            signal_mapping.parent.mkdir(parents=True, exist_ok=True)
            signal_mapping.write_text(
                "put(CarCrlBizCode.set_StartRemind_State.value(), SignalCode.SIGNAL_CTL_XPILOT_START_REMIDE_ST)\n",
                encoding="utf-8",
            )
            receiver = (
                guideengine
                / "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/debug/broadcast/DataCenterBroadcastReceiver.java"
            )
            receiver.parent.mkdir(parents=True, exist_ok=True)
            receiver.write_text(
                'public static final String ACTION_MOCK = "com.xiaopeng.guide.action.mock.datacenter";\n',
                encoding="utf-8",
            )
            transport = (
                guideengine
                / "module_core/module_xdata_service/src/main/java/com/xiaopeng/guideengine/xdatanative/transport/XDataTransport.kt"
            )
            transport.parent.mkdir(parents=True, exist_ok=True)
            transport.write_text(
                "Signal.SignalCode.SIGNAL_CTL_XPILOT_START_REMIDE_ST,\n"
                "Signal.SignalCode.SIGNAL_X3D_DATA_SERVICE_START_REMIND,\n",
                encoding="utf-8",
            )

            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=guideengine,
                source_investigation=SourceInvestigationOptions(repo_roots=[guideengine]),
            )
            runner = SourceInvestigationRunner(config)
            hits = [
                SearchHit(
                    chunk_id="guideengine-signals:1",
                    source_id="guideengine-signals",
                    title="SIGNAL_CTL_XPILOT_START_REMIDE_ST (15012)",
                    content="",
                    source_ref=str(signal_proto),
                    kind="signal_proto_entry",
                    score=7.0,
                    metadata={"signal": "SIGNAL_CTL_XPILOT_START_REMIDE_ST", "code": "15012"},
                ),
                SearchHit(
                    chunk_id="guideengine-signals:2",
                    source_id="guideengine-signals",
                    title="SIGNAL_X3D_DATA_SERVICE_START_REMIND (150006)",
                    content="",
                    source_ref=str(signal_proto),
                    kind="signal_proto_entry",
                    score=7.0,
                    metadata={"signal": "SIGNAL_X3D_DATA_SERVICE_START_REMIND", "code": "150006"},
                ),
            ]

            prompt = runner._prompt("如何模拟 前车起步信号 源码分析", hits=hits)

        self.assertIn("预采样源码摘录", prompt)
        self.assertIn("SIGNAL_CTL_XPILOT_START_REMIDE_ST = 15012", prompt)
        self.assertIn("SIGNAL_X3D_DATA_SERVICE_START_REMIND = 150006", prompt)
        self.assertIn("ACTION_MOCK = \"com.xiaopeng.guide.action.mock.datacenter\"", prompt)
        self.assertIn("Signal.SignalCode.SIGNAL_CTL_XPILOT_START_REMIDE_ST", prompt)

    def test_source_investigation_uses_local_signal_probe_before_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guideengine = root / "guideengine"
            signal_proto = guideengine / "module_floorcenter/module_proto/src/main/proto/signal.proto"
            signal_proto.parent.mkdir(parents=True, exist_ok=True)
            signal_proto.write_text(
                "enum SignalCode {\n"
                "    SIGNAL_CTL_XPILOT_START_REMIDE_ST = 15012; // 前车起步开启状态 0 关闭 1 开启 SIGNAL_StartRemind_State\n"
                "    SIGNAL_X3D_DATA_SERVICE_START_REMIND = 150006; // 前车起步 int\n"
                "}\n",
                encoding="utf-8",
            )
            signal_mapping = (
                guideengine
                / "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/define/mapping/code/SignalMapping.kt"
            )
            signal_mapping.parent.mkdir(parents=True, exist_ok=True)
            signal_mapping.write_text(
                "put(CarCrlBizCode.set_StartRemind_State.value(), SignalCode.SIGNAL_CTL_XPILOT_START_REMIDE_ST)\n",
                encoding="utf-8",
            )
            receiver = (
                guideengine
                / "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/debug/broadcast/DataCenterBroadcastReceiver.java"
            )
            receiver.parent.mkdir(parents=True, exist_ok=True)
            receiver.write_text(
                'public static final String ACTION_MOCK = "com.xiaopeng.guide.action.mock.datacenter";\n'
                "private final List<Signal.SignalFormat> supportFormat = Arrays.asList(Signal.SignalFormat.Int32, Signal.SignalFormat.String);\n",
                encoding="utf-8",
            )
            helper = (
                guideengine
                / "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/helper/carcontrol/CarCtlXpilotHelper.kt"
            )
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.write_text(
                "private fun startRemindCallback(eventValue: EventValue) {\n"
                "    // 前车起步提醒 0 关闭 1 开启\n"
                "    onNextData(\n"
                "        SignalCode.SIGNAL_CTL_XPILOT_START_REMIDE_ST,\n"
                "        Signal.SignalFormat.Int32,\n"
                "        if (enable != null && enable) 1 else 0\n"
                "    )\n"
                "}\n",
                encoding="utf-8",
            )
            tips_biz = (
                guideengine
                / "module_core/subreality_biz/src/main/java/com/xiaopeng/ainavi/subreality_biz/tips/TipsBizService.kt"
            )
            tips_biz.parent.mkdir(parents=True, exist_ok=True)
            tips_biz.write_text(
                "Signal.SignalCode.SIGNAL_X3D_DATA_SERVICE_START_REMIND to startRemind,\n",
                encoding="utf-8",
            )
            tips_repo = (
                guideengine
                / "module_display/launcher_subreality_service/src/main/java/com/xiaopeng/ainavi/tips/TipsServiceRepository.kt"
            )
            tips_repo.parent.mkdir(parents=True, exist_ok=True)
            tips_repo.write_text(
                "private fun handleStartStateTips(tipsInfo: TipsBizState.StartState) {\n"
                "    val title = TipsMsgHelper.matchStartTipsTxt(tipsInfo.startSignal)\n"
                "}\n",
                encoding="utf-8",
            )
            tips_msg = (
                guideengine
                / "module_display/launcher_subreality_service/src/main/java/com/xiaopeng/ainavi/utils/TipsMsgHelper.kt"
            )
            tips_msg.parent.mkdir(parents=True, exist_ok=True)
            tips_msg.write_text(
                "fun matchStartTipsTxt(signal: Int): Int? {\n"
                "    return when(signal) {\n"
                "        0x01 -> R.string.Key_Tips_HU_START_REMIND\n"
                "        else -> null\n"
                "    }\n"
                "}\n",
                encoding="utf-8",
            )

            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=guideengine,
                knowledge=KnowledgeOptions(enabled=True, storage=root / "knowledge.sqlite"),
                source_investigation=SourceInvestigationOptions(repo_roots=[guideengine]),
            )
            service = KnowledgeService(config)
            service.store.add_chunks(
                source_id="guideengine-signals",
                source_type="guideengine_signal",
                title="Signals",
                source_ref="",
                chunks=[
                    KnowledgeChunk(
                        id="front-start-switch",
                        source_id="guideengine-signals",
                        title="SIGNAL_CTL_XPILOT_START_REMIDE_ST (15012)",
                        content="signal: SIGNAL_CTL_XPILOT_START_REMIDE_ST\ncode: 15012\ncomment: 前车起步开启状态 0 关闭 1 开启 SIGNAL_StartRemind_State\n",
                        source_ref=str(signal_proto),
                        kind="signal_proto_entry",
                        metadata={
                            "signal": "SIGNAL_CTL_XPILOT_START_REMIDE_ST",
                            "code": "15012",
                            "line": "2",
                            "keywords": ["SIGNAL_CTL_XPILOT_START_REMIDE_ST", "15012", "前车起步开启状态"],
                        },
                    ),
                    KnowledgeChunk(
                        id="front-start-3d",
                        source_id="guideengine-signals",
                        title="SIGNAL_X3D_DATA_SERVICE_START_REMIND (150006)",
                        content="signal: SIGNAL_X3D_DATA_SERVICE_START_REMIND\ncode: 150006\ncomment: 前车起步 int\n",
                        source_ref=str(signal_proto),
                        kind="signal_proto_entry",
                        metadata={
                            "signal": "SIGNAL_X3D_DATA_SERVICE_START_REMIND",
                            "code": "150006",
                            "line": "3",
                            "keywords": ["SIGNAL_X3D_DATA_SERVICE_START_REMIND", "150006", "前车起步 int"],
                        },
                    ),
                ],
            )

            with patch("lark_agent_bridge.knowledge.source_investigation.subprocess.run") as mocked_run:
                answer = service.answer("源码调查 前车起步信号如何模拟")

        mocked_run.assert_not_called()
        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "source_investigation")
        self.assertIn("SIGNAL_CTL_XPILOT_START_REMIDE_ST", answer.message)
        self.assertIn("SIGNAL_X3D_DATA_SERVICE_START_REMIND", answer.message)
        self.assertIn("com.xiaopeng.guide.action.mock.datacenter", answer.message)
        self.assertIn("--ei code 15012", answer.message)
        self.assertIn("0 关闭，1 开启", answer.message)

    def test_source_investigation_prompt_includes_filters_candidates_and_priority_modules(self):
        payload = {
            "answer": "前车起步信号需要继续区分车控开关态和 3D 数据服务态。",
            "canonical_key": "adb-sim:front-car-start",
            "confidence": 0.84,
            "commands": [],
            "source_evidence": [
                {"file": "module_floorcenter/module_proto/src/main/proto/signal.proto", "line": 187, "text": "SIGNAL_CTL_XPILOT_START_REMIDE_ST"}
            ],
            "coverage_boundary": "scanned datacenter and xdata transport modules",
            "writeback_allowed": False,
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=root / "guideengine",
                knowledge=KnowledgeOptions(enabled=True, storage=root / "knowledge.sqlite"),
            )
            service = KnowledgeService(config)
            service.store.add_chunks(
                source_id="guideengine-signals",
                source_type="guideengine_signal",
                title="Signals",
                source_ref="",
                chunks=[
                    KnowledgeChunk(
                        id="front-start-switch",
                        source_id="guideengine-signals",
                        title="SIGNAL_CTL_XPILOT_START_REMIDE_ST (15012)",
                        content="signal: SIGNAL_CTL_XPILOT_START_REMIDE_ST\ncode: 15012\ncomment: 前车起步开启状态\n",
                        source_ref="/Users/zhuyl/Documents/workspace/xp/guideengine/.worktrees/os6_xpdev/module_floorcenter/module_proto/src/main/proto/signal.proto",
                        kind="signal_proto_entry",
                        metadata={
                            "signal": "SIGNAL_CTL_XPILOT_START_REMIDE_ST",
                            "code": "15012",
                            "line": "187",
                            "keywords": ["SIGNAL_CTL_XPILOT_START_REMIDE_ST", "15012", "前车起步开启状态"],
                        },
                    ),
                    KnowledgeChunk(
                        id="front-start-3d",
                        source_id="guideengine-signals",
                        title="SIGNAL_X3D_DATA_SERVICE_START_REMIND (150006)",
                        content="signal: SIGNAL_X3D_DATA_SERVICE_START_REMIND\ncode: 150006\ncomment: 前车起步 int\n",
                        source_ref="/Users/zhuyl/Documents/workspace/xp/guideengine/.worktrees/os6_xpdev/module_floorcenter/module_proto/src/main/proto/signal.proto",
                        kind="signal_proto_entry",
                        metadata={
                            "signal": "SIGNAL_X3D_DATA_SERVICE_START_REMIND",
                            "code": "150006",
                            "line": "962",
                            "keywords": ["SIGNAL_X3D_DATA_SERVICE_START_REMIND", "150006", "前车起步 int"],
                        },
                    ),
                ],
            )

            def fake_run(command, cwd, capture_output, text, timeout, check):
                prompt = command[-1]
                self.assertIn("你是被主流程派发的子 agent", prompt)
                self.assertIn("不要做 memory pass", prompt)
                self.assertIn("不要使用 using-superpowers、ask、brainstorming", prompt)
                self.assertIn("最多执行 12 个命令", prompt)
                self.assertIn("第一阶段只允许读取上面的重点模块", prompt)
                self.assertIn("第二阶段若仍不足，只允许在重点模块所在目录内补充 rg", prompt)
                self.assertIn("第三阶段如果仍不能确认，直接输出低置信边界", prompt)
                self.assertIn("只要已经确认信号定义、映射/生产链、注入能力、至少一条消费/transport 证据，就立即停止搜索并输出 JSON", prompt)
                self.assertIn("候选锚点", prompt)
                self.assertIn("仅用于缩小搜索范围，不代表最终结论", prompt)
                self.assertIn("如果候选与源码不符，必须推翻候选", prompt)
                self.assertIn("SIGNAL_CTL_XPILOT_START_REMIDE_ST", prompt)
                self.assertIn("SIGNAL_X3D_DATA_SERVICE_START_REMIND", prompt)
                self.assertIn("忽略以下低价值路径或文件", prompt)
                self.assertIn("src/test", prompt)
                self.assertIn("src/androidTest", prompt)
                self.assertIn("build/", prompt)
                self.assertIn("generated/", prompt)
                self.assertIn("third_party/", prompt)
                self.assertIn("*.pb.cc", prompt)
                self.assertIn("*.pb.h", prompt)
                self.assertIn("优先阅读这些重点模块", prompt)
                self.assertIn("module_floorcenter/module_proto/src/main/proto/signal.proto", prompt)
                self.assertIn(
                    "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/define/mapping/code/SignalMapping.kt",
                    prompt,
                )
                self.assertIn(
                    "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/debug/broadcast/DataCenterBroadcastReceiver.java",
                    prompt,
                )
                self.assertIn(
                    "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/helper/carcontrol/CarCtlXpilotHelper.kt",
                    prompt,
                )
                self.assertIn(
                    "module_core/module_xdata_service/src/main/java/com/xiaopeng/guideengine/xdatanative/transport/XDataTransport.kt",
                    prompt,
                )
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("lark_agent_bridge.knowledge.source_investigation.subprocess.run", side_effect=fake_run):
                answer = service.answer("源码调查 前车起步信号如何模拟")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "source_investigation")
        self.assertEqual(answer.details["canonical_key"], "adb-sim:front-car-start")

    def test_source_investigation_runs_when_explicit_even_with_generic_source_hit(self):
        payload = {
            "answer": "SIGNAL_GENERIC_ONLY 已通过源码确认模拟方式。",
            "canonical_key": "adb-sim:SIGNAL_GENERIC_ONLY",
            "confidence": 0.88,
            "commands": [
                "adb shell am broadcast -a com.xiaopeng.guide.action.mock.datacenter --ei code 23456 --ei format 3 --es value 1"
            ],
            "source_evidence": [
                {"file": "module_proto/src/main/proto/signal.proto", "line": 13, "text": "SIGNAL_GENERIC_ONLY = 23456;"}
            ],
            "coverage_boundary": "scanned guideengine",
            "writeback_allowed": True,
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=root / "guideengine",
                knowledge=KnowledgeOptions(enabled=True, storage=root / "knowledge.sqlite"),
            )
            service = KnowledgeService(config)
            service.store.add_chunks(
                source_id="guideengine-signals",
                source_type="guideengine_signal",
                title="Signals",
                source_ref="",
                chunks=[
                    KnowledgeChunk(
                        id="generic-source",
                        source_id="guideengine-signals",
                        title="通用信号模拟入口",
                        content="未知信号如何模拟，需要确认 code format value。",
                        source_ref="/path/DataCenterBroadcastReceiver.java",
                        kind="guideengine_source",
                    )
                ],
            )

            def fake_run(command, cwd, capture_output, text, timeout, check):
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("lark_agent_bridge.knowledge.source_investigation.subprocess.run", side_effect=fake_run):
                answer = service.answer("源码调查未知信号如何模拟")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "source_investigation")
        self.assertEqual(answer.details["canonical_key"], "adb-sim:SIGNAL_GENERIC_ONLY")

    def test_signal_question_with_existing_knowledge_does_not_auto_run_source_investigation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=root / "guideengine",
                knowledge=KnowledgeOptions(enabled=True, storage=root / "knowledge.sqlite"),
            )
            service = KnowledgeService(config)
            service.store.add_chunks(
                source_id="derived-adb-simulations",
                source_type="source_derived",
                title="试验链路信号源码沉淀知识",
                source_ref="/repo",
                chunks=[
                    KnowledgeChunk(
                        id="derived-experiment",
                        source_id="derived-adb-simulations",
                        title="试验链路信号源码沉淀知识",
                        content="试验链路信号已有源码沉淀结论，可直接按知识库摘要回答。",
                        source_ref="/repo",
                        kind="adb_signal_template",
                    )
                ],
            )

            with patch("lark_agent_bridge.knowledge.source_investigation.subprocess.run") as mocked_run:
                answer = service.answer("试验链路信号如何模拟")

        mocked_run.assert_not_called()
        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "retrieval_summary")
        self.assertEqual(answer.details["knowledge_hits"][0]["source_id"], "derived-adb-simulations")
        self.assertIn("试验链路信号已有源码沉淀结论", answer.message)

    def test_source_investigation_timeout_does_not_write_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=root / "guideengine",
                knowledge=KnowledgeOptions(enabled=True, storage=root / "knowledge.sqlite"),
            )
            service = KnowledgeService(config)

            def fake_run(*args, **kwargs):
                raise subprocess.TimeoutExpired(cmd=["codex"], timeout=3)

            with patch("lark_agent_bridge.knowledge.source_investigation.subprocess.run", side_effect=fake_run):
                answer = service.answer("源码调查未知车控信号如何模拟")
            hits = service.search("未知车控信号")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "source_investigation_unavailable")
        self.assertIn("源码调查未在限定时间内完成", answer.message)
        self.assertEqual(hits, [])

    def test_source_investigation_low_confidence_does_not_write_knowledge(self):
        payload = {
            "answer": "SIGNAL_LOW_CONF 当前证据不足，只能作为候选。",
            "canonical_key": "adb-sim:SIGNAL_LOW_CONF",
            "confidence": 0.51,
            "commands": [],
            "source_evidence": [
                {"file": "module_proto/src/main/proto/signal.proto", "line": 9, "text": "候选证据"}
            ],
            "coverage_boundary": "scanned guideengine partially",
            "writeback_allowed": True,
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=root / "guideengine",
                knowledge=KnowledgeOptions(enabled=True, storage=root / "knowledge.sqlite"),
            )

            def fake_run(command, cwd, capture_output, text, timeout, check):
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            service = KnowledgeService(config)
            with patch("lark_agent_bridge.knowledge.source_investigation.subprocess.run", side_effect=fake_run):
                answer = service.answer("源码调查低置信信号如何模拟")
            hits = service.search("SIGNAL_LOW_CONF")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "source_investigation")
        self.assertEqual(answer.details["knowledge_hits"], [])
        self.assertEqual(hits, [])

    def test_source_investigation_invalid_schema_does_not_write_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=root / "guideengine",
                knowledge=KnowledgeOptions(enabled=True, storage=root / "knowledge.sqlite"),
            )

            def fake_run(command, cwd, capture_output, text, timeout, check):
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text('{"answer": "schema is incomplete"}', encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            service = KnowledgeService(config)
            with patch("lark_agent_bridge.knowledge.source_investigation.subprocess.run", side_effect=fake_run):
                answer = service.answer("源码调查未知结构信号如何模拟")
            hits = service.search("schema is incomplete")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "source_investigation_unavailable")
        self.assertIn("invalid schema", answer.message)
        self.assertEqual(hits, [])

    def test_sync_filters_obsolete_datacenter_intent_mock_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adb_path = root / "adb_data.json"
            adb_path.write_text(
                json.dumps(
                    {
                        "commands": [
                            {
                                "name": "旧信号模拟",
                                "command": (
                                    "adb shell am broadcast -a com.xiaopeng.intent.action.mock.datacenter "
                                    "--ei code 1030 --ei format 1 --es value 1"
                                ),
                                "group": "信号模拟",
                                "description": "旧入口已过时",
                            },
                            {
                                "name": "新信号模拟",
                                "command": (
                                    "adb shell am broadcast -a com.xiaopeng.guide.action.mock.datacenter "
                                    "--ei code 36001 --ei format 3 --es value 1"
                                ),
                                "group": "信号模拟",
                                "description": "当前入口",
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config = BridgeConfig(
                data_dir=root,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(
                            id="guideengine-adb",
                            type="local_json",
                            path=str(adb_path),
                        )
                    ],
                ),
            )

            service = KnowledgeService(config)
            sync_result = service.sync_all()
            chunks = service.store.all_chunks()

        self.assertEqual(sync_result["total_chunks"], 1)
        self.assertEqual(chunks[0].title, "新信号模拟")
        self.assertNotIn("com.xiaopeng.intent.action.mock.datacenter", chunks[0].content)

    def test_complex_pb_question_explains_custom_factory_boundary(self):
        service = KnowledgeService(BridgeConfig(knowledge=KnowledgeOptions(enabled=True)))

        answer = service.answer("PB对象怎么ADB模拟")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "adb_simulation_policy")
        self.assertIn("基础类型", answer.message)
        self.assertIn("mockDataFactory", answer.message)
        self.assertIn("record 回放", answer.message)
        self.assertIn("不要生成看似通用的 PB ADB 命令", answer.message)

    def test_exact_unsupported_complex_signal_does_not_emit_fake_adb_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guideengine = _write_minimal_guideengine_sources(root / "guideengine")
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=guideengine,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(id="guideengine-signals", type="guideengine_signal"),
                    ],
                ),
            )

            service = KnowledgeService(config)
            service.sync_all()
            answer = service.answer("SIGNAL_SR_PROPERTY 如何模拟")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "adb_signal_custom_required")
        self.assertIn("SIGNAL_SR_PROPERTY", answer.message)
        self.assertIn("ByteArray/PB", answer.message)
        self.assertIn("新增 mockDataFactory", answer.message)
        self.assertIn("record 回放", answer.message)
        self.assertNotIn("--ei code 105007 --ei format 10", answer.message)

    def test_feishu_base_wiki_url_resolves_bitable_token_before_listing_records(self):
        calls = []

        def fake_run(command):
            calls.append(command)
            if command[:3] == ["lark-cli", "wiki", "+node-get"]:
                return {
                    "data": {
                        "obj_type": "bitable",
                        "obj_token": "bascnKnowledgeBase",
                    }
                }
            if command[:3] == ["lark-cli", "base", "+record-list"]:
                base_token = command[command.index("--base-token") + 1]
                if base_token != "bascnKnowledgeBase":
                    return {"items": []}
                return {
                    "items": [
                        {
                            "record_id": "rec1",
                            "fields": {
                                "名称": "OTA 模拟",
                                "命令": "adb shell am broadcast ...",
                            },
                        }
                    ]
                }
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = BridgeConfig(
                data_dir=root,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(
                            id="guideengine-base",
                            type="feishu_base",
                            url=(
                                "https://xiaopeng.feishu.cn/wiki/Tog2wE6sxij4SjkNp6UcRK6Xn6l"
                                "?table=tblHjB1Nm6m9EbkL&view=vewmlXCvgy"
                            ),
                        )
                    ],
                ),
            )

            service = KnowledgeService(config)
            with patch("lark_agent_bridge.knowledge.ingestors._run_lark_json", side_effect=fake_run):
                sync_result = service.sync_all()
            hits = service.search("OTA 模拟")

        self.assertEqual(calls[0][:3], ["lark-cli", "wiki", "+node-get"])
        self.assertEqual(calls[1][:3], ["lark-cli", "base", "+record-list"])
        self.assertEqual(sync_result["total_chunks"], 1)
        self.assertEqual(hits[0].source_id, "guideengine-base")
        self.assertIn("adb shell", hits[0].content)

    def test_feishu_base_sync_parses_tabular_record_list_payload(self):
        calls = []

        def fake_run(command):
            calls.append(command)
            if command[:3] == ["lark-cli", "base", "+record-list"]:
                offset = int(command[command.index("--offset") + 1]) if "--offset" in command else 0
                if offset > 0:
                    return {
                        "data": {
                            "fields": ["命令编号", "命令名称", "命令"],
                            "data": [],
                            "record_id_list": [],
                            "has_more": False,
                        }
                    }
                return {
                    "data": {
                        "fields": ["命令编号", "命令名称", "命令"],
                        "data": [
                            ["ADB-003", "切换语音云端环境", "adb shell am broadcast -a carspeechservice.ACTION"]
                        ],
                        "record_id_list": ["rec003"],
                        "has_more": True,
                    }
                }
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = BridgeConfig(
                data_dir=root,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(
                            id="guideengine-base",
                            type="feishu_base",
                            url="A9gEb3Ng7aqeXQsETehc8qponxd",
                            table_id="tblHjB1Nm6m9EbkL",
                            view_id="vewmlXCvgy",
                        )
                    ],
                ),
            )

            service = KnowledgeService(config)
            with patch("lark_agent_bridge.knowledge.ingestors._run_lark_json", side_effect=fake_run):
                sync_result = service.sync_all()
            hits = service.search("语音云端环境")

        self.assertEqual(sync_result["total_chunks"], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(hits[0].title, "切换语音云端环境")
        self.assertIn("carspeechservice.ACTION", hits[0].content)


def _write_minimal_guideengine_sources(root: Path) -> Path:
    receiver = (
        root
        / "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/debug/broadcast"
        / "DataCenterBroadcastReceiver.java"
    )
    receiver.parent.mkdir(parents=True, exist_ok=True)
    receiver.write_text(
        """
public class DataCenterBroadcastReceiver {
    public static final String ACTION_MOCK = "com.xiaopeng.guide.action.mock.datacenter";
    private final Map<Integer, Function<String, Object>> mockDataFactory = new HashMap<Integer, Function<String, Object>>() {{
        put(SIGNAL_SR_XTHEME_VALUE, DataCenterBroadcastReceiver.this::mockXTheme);
    }};
    public void onReceive(Intent intent) {
        int code = intent.getIntExtra("code", -1);
        int format = intent.getIntExtra("format", -1);
        String value = intent.getStringExtra("value");
    }
    private XTheme mockXTheme(String value) {
        String[] strings = value.split(",");
        TimePeriod timePeriod = TimePeriod.Companion.fromInt(Integer.parseInt(strings[0]));
        ThemeMode themeMode = ThemeMode.Companion.fromInt(Integer.parseInt(strings[1]));
        return new XTheme(timePeriod, themeMode);
    }
}
""",
        encoding="utf-8",
    )
    ota = root / "module_core/subreality_biz/src/main/java/com/xiaopeng/ainavi/subreality_biz/ota/SrOtaService.kt"
    ota.parent.mkdir(parents=True, exist_ok=True)
    ota.write_text(
        """
private fun sendMsgToUnity(campaign: Int, upgrade: Int) {
    val msg = arrayOf(campaign, upgrade).contentToString()
    unityServiceInstance.sendMsgToUnity(SignalCode.SIGNAL_OTA_ST, SignalFormat.String, msg)
}
enum class OtaCampaign {
    OTA_CAMPAIGN_NONE,
    OTA_CAMPAIGN_SHOW,
}
enum class OtaUpgrade {
    OTA_UPGRADE_NONE,
    OTA_UPGRADE_SHOW,
    OTA_UPGRADE_AFTER_VIDEO,
}
""",
        encoding="utf-8",
    )
    proto = root / "module_floorcenter/module_proto/src/main/proto/signal.proto"
    proto.parent.mkdir(parents=True, exist_ok=True)
    proto.write_text(
        """
enum SignalFormat {
    String = 7;
    ByteArray = 10;
    JavaObject = 18;
    ProtoObject = 19;
}
// OTA状态
SIGNAL_OTA_ST = 105003;
// SR主题是否隐藏
SIGNAL_CLOUD_HIDE_SR_THEME = 102055;
// 车型主题是否隐藏
SIGNAL_CLOUD_HIDE_CAR_MODEL_THEME = 102056;
// XThemeMsg 当前主题及皮肤信息
// themeMode: 0白天，1黑夜
// timePeriod: 0早晨，1白天，2傍晚，3夜晚
SIGNAL_SR_XTHEME = 105004;
SIGNAL_SR_XTHEME_MSG = 105009;
// set_Property PB bytes
SIGNAL_SR_PROPERTY = 105007;
// 上下电  0下电 1上电 2远程上电 SIGNAL_McuIGStatus
SIGNAL_MCU_IG_ST = 36001;
// SR调试信号 190000 - 199999
SIGNAL_DEBUG_SHOW_LD_TILE = 190014;
// 展示 LD debug 信息
SIGNAL_DEBUG_SHOW_LD_DEBUG_INFO = 190015;
SIGNAL_DEBUG_LD_TILES_INFO = 190016; // C++层LD调试信息
""",
        encoding="utf-8",
    )
    replay_receiver = root / "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/debug/broadcast/ReplayReceiver.kt"
    replay_receiver.write_text(
        """
class ReplayReceiver {
    companion object {
        const val REPLAY_FILE = "com.xiaopeng.guide.action.hmi.replay"
        const val REPLAY_FATH = "com.xiaopeng.guide.action.hmi.replayPath"
        const val START_RECORD = "com.xiaopeng.guide.action.hmi.startRecord"
    }
}
""",
        encoding="utf-8",
    )
    protocol_file = root / "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/debug/protolFile/ProtocolFile.java"
    protocol_file.parent.mkdir(parents=True, exist_ok=True)
    protocol_file.write_text(
        """
public class ProtocolFile {
    private Object convertBytesToMsg(Signal.SignalFormat signalFormat, byte[] raw) {
        if (signalFormat == Signal.SignalFormat.ByteArray) {
            return raw;
        }
        return null;
    }
    private void dispatch(byte[] raw) {
        FloorCenterManager.Companion.getInstance().dataCenter.mockSignal(
            new XDataPropertyValue.Builder().setFormat(Signal.SignalFormat.ByteArray).setValue(raw).build()
        );
    }
}
""",
        encoding="utf-8",
    )
    return root
