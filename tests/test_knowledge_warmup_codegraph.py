from _knowledge_base import *  # noqa: F401,F403
from _knowledge_base import _KnowledgeTestBase


class KnowledgeWarmupCodegraphTests(_KnowledgeTestBase):
    def test_constructor_can_skip_codegraph_warmup(self):
        config = BridgeConfig(knowledge=KnowledgeOptions(enabled=True))

        with patch.object(SourceInvestigationRunner, "warmup_codegraph") as warmup_codegraph:
            KnowledgeService(config, warmup_codegraph=False)

        warmup_codegraph.assert_not_called()
    def test_warmup_codegraph_skips_duplicate_inflight_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "guideengine"
            (repo / ".codegraph").mkdir(parents=True)
            config = BridgeConfig(
                guideengine_repo=repo,
                source_investigation=SourceInvestigationOptions(
                    enabled=True,
                    repo_roots=[repo],
                    codegraph_enabled=True,
                ),
            )
            runner = SourceInvestigationRunner(config)
            fake_cg = unittest.mock.Mock()
            fake_cg.is_indexed.return_value = True
            started_threads: list[object] = []

            class FakeThread:
                def __init__(self, *, target, args, name, daemon):
                    self.target = target
                    self.args = args
                    self.name = name
                    self.daemon = daemon

                def start(self):
                    started_threads.append(self)

            with (
                patch.object(SourceInvestigationRunner, "_get_codegraph", return_value=fake_cg),
                patch("lark_agent_bridge.knowledge.source_investigation.threading.Thread", FakeThread),
            ):
                runner.warmup_codegraph()
                runner.warmup_codegraph()
                started_threads[0].target(*started_threads[0].args)

        self.assertEqual(len(started_threads), 1)
    def test_warmup_codegraph_skips_recent_successful_repo_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "guideengine"
            (repo / ".codegraph").mkdir(parents=True)
            config = BridgeConfig(
                data_dir=root / "data",
                guideengine_repo=repo,
                source_investigation=SourceInvestigationOptions(
                    enabled=True,
                    repo_roots=[repo],
                    codegraph_enabled=True,
                ),
            )
            runner = SourceInvestigationRunner(config)
            state_path = source_investigation_module._codegraph_repo_state_path(config, repo)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            now = time.time()
            state_path.write_text(
                json.dumps(
                    {
                        "repo": str(repo),
                        "started_at": now - 30,
                        "finished_at": now - 5,
                        "success": True,
                    }
                ),
                encoding="utf-8",
            )
            fake_cg = unittest.mock.Mock()
            fake_cg.is_indexed.return_value = True
            started_threads: list[object] = []

            class FakeThread:
                def __init__(self, *, target, args, name, daemon):
                    self.target = target
                    self.args = args
                    self.name = name
                    self.daemon = daemon

                def start(self):
                    started_threads.append(self)

            with (
                patch.object(SourceInvestigationRunner, "_get_codegraph", return_value=fake_cg),
                patch("lark_agent_bridge.knowledge.source_investigation.threading.Thread", FakeThread),
            ):
                runner.warmup_codegraph()

        self.assertEqual(len(started_threads), 0)
    def test_warmup_codegraph_skips_recent_running_repo_sync_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "guideengine"
            (repo / ".codegraph").mkdir(parents=True)
            config = BridgeConfig(
                data_dir=root / "data",
                guideengine_repo=repo,
                source_investigation=SourceInvestigationOptions(
                    enabled=True,
                    repo_roots=[repo],
                    codegraph_enabled=True,
                ),
            )
            runner = SourceInvestigationRunner(config)
            state_path = source_investigation_module._codegraph_repo_state_path(config, repo)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            now = time.time()
            state_path.write_text(
                json.dumps(
                    {
                        "repo": str(repo),
                        "started_at": now - 10,
                        "finished_at": None,
                        "success": False,
                    }
                ),
                encoding="utf-8",
            )
            fake_cg = unittest.mock.Mock()
            fake_cg.is_indexed.return_value = True
            started_threads: list[object] = []

            class FakeThread:
                def __init__(self, *, target, args, name, daemon):
                    self.target = target
                    self.args = args
                    self.name = name
                    self.daemon = daemon

                def start(self):
                    started_threads.append(self)

            with (
                patch.object(SourceInvestigationRunner, "_get_codegraph", return_value=fake_cg),
                patch("lark_agent_bridge.knowledge.source_investigation.threading.Thread", FakeThread),
            ):
                runner.warmup_codegraph()

        self.assertEqual(len(started_threads), 0)
    def test_warmup_codegraph_skips_when_repo_lock_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "guideengine"
            (repo / ".codegraph").mkdir(parents=True)
            config = BridgeConfig(
                data_dir=Path(tmp) / "data",
                guideengine_repo=repo,
                source_investigation=SourceInvestigationOptions(
                    enabled=True,
                    repo_roots=[repo],
                    codegraph_enabled=True,
                ),
            )
            runner = SourceInvestigationRunner(config)
            fake_cg = unittest.mock.Mock()
            fake_cg.is_indexed.return_value = True
            started_threads: list[object] = []

            class FakeThread:
                def __init__(self, *, target, args, name, daemon):
                    self.target = target
                    self.args = args
                    self.name = name
                    self.daemon = daemon

                def start(self):
                    started_threads.append(self)

            @contextmanager
            def fake_lock(_config, _repo):
                yield None

            with (
                patch.object(SourceInvestigationRunner, "_get_codegraph", return_value=fake_cg),
                patch("lark_agent_bridge.knowledge.source_investigation.threading.Thread", FakeThread),
                patch("lark_agent_bridge.knowledge.source_investigation._try_repo_warmup_lock", fake_lock),
            ):
                runner.warmup_codegraph()

        self.assertEqual(len(started_threads), 0)
    def test_warmup_codegraph_limits_startup_to_first_indexed_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo1 = root / "guideengine"
            repo2 = root / "Napa5"
            (repo1 / ".codegraph").mkdir(parents=True)
            (repo2 / ".codegraph").mkdir(parents=True)
            config = BridgeConfig(
                data_dir=root / "data",
                guideengine_repo=repo1,
                source_investigation=SourceInvestigationOptions(
                    enabled=True,
                    repo_roots=[repo1, repo2],
                    codegraph_enabled=True,
                ),
            )
            runner = SourceInvestigationRunner(config)
            fake_cg = unittest.mock.Mock()
            fake_cg.is_indexed.return_value = True
            started_threads: list[object] = []

            class FakeThread:
                def __init__(self, *, target, args, name, daemon):
                    self.target = target
                    self.args = args
                    self.name = name
                    self.daemon = daemon

                def start(self):
                    started_threads.append(self)

            with (
                patch.object(SourceInvestigationRunner, "_get_codegraph", return_value=fake_cg),
                patch("lark_agent_bridge.knowledge.source_investigation.threading.Thread", FakeThread),
            ):
                runner.warmup_codegraph()

        self.assertEqual(len(started_threads), 1)
        self.assertEqual(started_threads[0].args[1], repo1)
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
    def test_source_investigation_repeat_question_uses_fact_snapshot_prefix(self):
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
            drifted_hits = [
                SearchHit(
                    chunk_id="guideengine-signals:2",
                    source_id="guideengine-signals",
                    title="SIGNAL_CUSTOM_BETA (150006)",
                    content="signal: SIGNAL_CUSTOM_BETA\ncode: 150006\ncomment: 火箭雨提示备用链路\n",
                    source_ref=str(root / "missing" / "signal.proto"),
                    kind="signal_proto_entry",
                    score=6.8,
                    metadata={"signal": "SIGNAL_CUSTOM_BETA", "code": "150006", "line": "188"},
                ),
                hits[0],
            ]
            payload = {
                "answer": "火箭雨提示信号可通过 mock datacenter 广播模拟。",
                "canonical_key": "adb-sim:front-car-start",
                "confidence": 0.84,
                "commands": [
                    "adb shell am broadcast -a com.xiaopeng.guide.action.mock.datacenter --ei code 15012 --ei format 3 --es value 1"
                ],
                "source_evidence": [
                    {
                        "file": "module_floorcenter/module_proto/src/main/proto/signal.proto",
                        "line": 187,
                        "text": "SIGNAL_CUSTOM_ALPHA = 15012",
                    }
                ],
                "coverage_boundary": "scanned datacenter and proto definition path",
                "writeback_allowed": False,
            }
            prompts: list[str] = []

            def fake_run(command, cwd, capture_output, text, timeout, check):
                prompts.append(command[-1])
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch.object(service, "search", side_effect=[hits, drifted_hits]),
                patch("lark_agent_bridge.knowledge.source_investigation.subprocess.run", side_effect=fake_run),
            ):
                first = service.answer(question)
                self.assertFalse((root / "source_investigations" / "snapshots").exists())
                self.assertFalse((root / "source_investigations" / "repeat_question_registry.json").exists())
                self.assertNotIn("### 调查事实快照", prompts[0])
                second = service.answer(question)

            snapshot_dir = root / "source_investigations" / "snapshots"
            self.assertTrue(first.success)
            self.assertTrue(second.success)
            self.assertIn("### 调查事实快照", prompts[1])
            self.assertIn("### 当前问题增量", prompts[1])
            self.assertIn("SIGNAL_CUSTOM_ALPHA", prompts[1])
            self.assertTrue(snapshot_dir.exists())
            self.assertTrue(any(snapshot_dir.glob("*.json")))
