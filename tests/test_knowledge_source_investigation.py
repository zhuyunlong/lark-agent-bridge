from _knowledge_base import *  # noqa: F401,F403
from _knowledge_base import _KnowledgeTestBase


class KnowledgeSourceInvestigationTests(_KnowledgeTestBase):
    def test_source_investigation_single_question_does_not_write_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=root / "guideengine",
                knowledge=KnowledgeOptions(enabled=True, storage=root / "knowledge.sqlite"),
                source_investigation=SourceInvestigationOptions(repo_roots=[root / "guideengine"], code_index_enabled=False, codegraph_enabled=False),
            )
            service = KnowledgeService(config)
            question = "源码调查 一次性火箭雨提示信号如何模拟"
            hits = [
                SearchHit(
                    chunk_id="guideengine-signals:1",
                    source_id="guideengine-signals",
                    title="SIGNAL_CUSTOM_ALPHA (15012)",
                    content="signal: SIGNAL_CUSTOM_ALPHA\ncode: 15012\ncomment: 火箭雨提示主链路\n",
                    source_ref=str(root / "missing" / "signal.proto"),
                    kind="signal_proto_entry",
                    score=7.0,
                    metadata={"signal": "SIGNAL_CUSTOM_ALPHA", "code": "15012", "line": "187"},
                )
            ]
            payload = {
                "answer": "一次性源码调查结果。",
                "canonical_key": "",
                "confidence": 0.61,
                "commands": [],
                "source_evidence": [],
                "coverage_boundary": "single independent question",
                "writeback_allowed": False,
            }

            def fake_run(command, cwd, capture_output, text, timeout, check):
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch.object(service, "search", return_value=hits),
                patch("lark_agent_bridge.knowledge.source_investigation.subprocess.run", side_effect=fake_run),
            ):
                result = service.answer(question)
                self.assertTrue(result.success)
                self.assertFalse((root / "source_investigations" / "snapshots").exists())
                self.assertFalse((root / "source_investigations" / "repeat_question_registry.json").exists())
    def test_source_investigation_same_question_text_with_different_hits_does_not_reuse_snapshot_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=root / "guideengine",
                knowledge=KnowledgeOptions(enabled=True, storage=root / "knowledge.sqlite"),
                source_investigation=SourceInvestigationOptions(repo_roots=[root / "guideengine"], code_index_enabled=False, codegraph_enabled=False),
            )
            service = KnowledgeService(config)
            question = "源码调查 火箭雨提示信号如何模拟"
            first_hits = [
                SearchHit(
                    chunk_id="guideengine-signals:1",
                    source_id="guideengine-signals",
                    title="SIGNAL_CUSTOM_ALPHA (15012)",
                    content="signal: SIGNAL_CUSTOM_ALPHA\ncode: 15012\ncomment: 火箭雨提示主链路\n",
                    source_ref=str(root / "missing" / "signal.proto"),
                    kind="signal_proto_entry",
                    score=7.0,
                    metadata={"signal": "SIGNAL_CUSTOM_ALPHA", "code": "15012", "line": "187"},
                )
            ]
            second_hits = [
                SearchHit(
                    chunk_id="guideengine-signals:9",
                    source_id="guideengine-signals",
                    title="SIGNAL_OTHER_FAMILY (25001)",
                    content="signal: SIGNAL_OTHER_FAMILY\ncode: 25001\ncomment: 火箭雨提示下载提示链路\n",
                    source_ref=str(root / "missing" / "signal.proto"),
                    kind="signal_proto_entry",
                    score=7.2,
                    metadata={"signal": "SIGNAL_OTHER_FAMILY", "code": "25001", "line": "199"},
                )
            ]
            payload = {
                "answer": "源码调查结果。",
                "canonical_key": "",
                "confidence": 0.61,
                "commands": [],
                "source_evidence": [],
                "coverage_boundary": "single independent question",
                "writeback_allowed": False,
            }
            prompts: list[str] = []

            def fake_run(command, cwd, capture_output, text, timeout, check):
                prompts.append(command[-1])
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            service._source_investigation_runner._pending_snapshot_max_entries = 8
            with (
                patch.object(service, "search", side_effect=[first_hits, second_hits]),
                patch("lark_agent_bridge.knowledge.source_investigation.subprocess.run", side_effect=fake_run),
            ):
                first = service.answer(question)
                second = service.answer(question)

            self.assertTrue(first.success)
            self.assertTrue(second.success)
            self.assertNotIn("### 调查事实快照", prompts[1])
            self.assertFalse((root / "source_investigations" / "snapshots").exists())
    def test_source_investigation_pending_cleanup_evicts_old_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=root / "guideengine",
                knowledge=KnowledgeOptions(enabled=True, storage=root / "knowledge.sqlite"),
                source_investigation=SourceInvestigationOptions(repo_roots=[root / "guideengine"], code_index_enabled=False, codegraph_enabled=False),
            )
            service = KnowledgeService(config)
            runner = service._source_investigation_runner
            runner._pending_snapshot_max_entries = 1
            question_a = "源码调查 火箭雨提示信号如何模拟"
            question_b = "源码调查 彗星雨提示信号如何模拟"
            hits_a = [
                SearchHit(
                    chunk_id="a",
                    source_id="guideengine-signals",
                    title="SIGNAL_CUSTOM_ALPHA (15012)",
                    content="signal: SIGNAL_CUSTOM_ALPHA\ncode: 15012\ncomment: 火箭雨提示主链路\n",
                    source_ref=str(root / "missing" / "signal.proto"),
                    kind="signal_proto_entry",
                    score=7.0,
                    metadata={"signal": "SIGNAL_CUSTOM_ALPHA", "code": "15012", "line": "187"},
                )
            ]
            hits_b = [
                SearchHit(
                    chunk_id="b",
                    source_id="guideengine-signals",
                    title="SIGNAL_CUSTOM_GAMMA (18001)",
                    content="signal: SIGNAL_CUSTOM_GAMMA\ncode: 18001\ncomment: 彗星雨提示主链路\n",
                    source_ref=str(root / "missing" / "signal.proto"),
                    kind="signal_proto_entry",
                    score=7.0,
                    metadata={"signal": "SIGNAL_CUSTOM_GAMMA", "code": "18001", "line": "287"},
                )
            ]
            payload = {
                "answer": "源码调查结果。",
                "canonical_key": "",
                "confidence": 0.61,
                "commands": [],
                "source_evidence": [],
                "coverage_boundary": "single independent question",
                "writeback_allowed": False,
            }
            prompts: list[str] = []

            def fake_run(command, cwd, capture_output, text, timeout, check):
                prompts.append(command[-1])
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch.object(service, "search", side_effect=[hits_a, hits_b, hits_a]),
                patch("lark_agent_bridge.knowledge.source_investigation.subprocess.run", side_effect=fake_run),
            ):
                service.answer(question_a)
                service.answer(question_b)
                service.answer(question_a)

            self.assertNotIn("### 调查事实快照", prompts[2])
    def test_source_investigation_question_family_normalization_keeps_xiadian_and_xiazai_distinct(self):
        runner = SourceInvestigationRunner(BridgeConfig())

        power_off = runner._normalize_source_question_family("源码调查 下电信号如何模拟")
        download = runner._normalize_source_question_family("源码调查 下载信号如何模拟")

        self.assertNotEqual(power_off, download)
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
                self.assertFalse((root / "source_investigations" / "snapshots").exists())
                self.assertFalse((root / "source_investigations" / "repeat_question_registry.json").exists())

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
                source_investigation=SourceInvestigationOptions(code_index_enabled=False, codegraph_enabled=False),
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
