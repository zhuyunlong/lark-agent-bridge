import importlib
import subprocess
from unittest import mock

from _app_base import *  # noqa: F401,F403
from _app_base import _AppTestBase


addr2line_runner_module = importlib.import_module("lark_agent_bridge.agents.addr2line_runner")


class AppAddr2lineRunnerTests(_AppTestBase):
    def test_direct_analysis_can_use_authorized_download_dir_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            downloads = Path(tmp) / "downloads"
            local_log = downloads / "Log.zip"
            downloads.mkdir()
            local_log.write_text("log", encoding="utf-8")
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            route_content = "日志我下载到服务器的下载目录了 Log.zip 基于这个日志分析"
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp) / "data",
                    allowed_chats=["oc_denied"],
                    allowed_users=["ou_me"],
                    local_resources=LocalResourceOptions(allowed_dirs=[downloads]),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                intent_runner=FakeIntentRunner(
                    {
                        route_content: IntentDecision(
                            route="direct_analysis",
                            reason="用户明确授权使用下载目录文件",
                            confidence="high",
                        )
                    }
                ),
            )

            result = app.handle_event(event(content=f"@bot {route_content}", sender_id="ou_me"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].resources[0].kind, "local")
        self.assertEqual(Path(fake_bug.requests[0].resources[0].value), local_log.resolve())
    def test_direct_analysis_rejects_download_dir_file_from_non_allowed_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "downloads"
            downloads.mkdir()
            (downloads / "Log.zip").write_text("log", encoding="utf-8")
            route_content = "日志我下载到服务器的下载目录了 Log.zip 基于这个日志分析"
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp) / "data",
                    allowed_chats=["oc_denied"],
                    allowed_users=["ou_me"],
                    local_resources=LocalResourceOptions(allowed_dirs=[downloads]),
                ),
                lark_client=FakeLarkClient(),
                intent_runner=FakeIntentRunner(
                    {
                        route_content: IntentDecision(
                            route="direct_analysis",
                            reason="用户明确授权使用下载目录文件",
                            confidence="high",
                        )
                    }
                ),
            )

            result = app.handle_event(event(content=f"@bot {route_content}", sender_id="ou_other"))

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_log")
    def test_signal_followup_walks_reply_chain_to_find_prepared_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            (log_dir / "main.log").write_text("05-20 12:00:00 signal 132002", encoding="utf-8")
            route_content = "那就看132002 信号吧"
            fake_lark = FakeLarkClient()
            fake_lark.fetched_messages["om_signal_b"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_signal_b",
                                "reply_to": "om_bug_a",
                            }
                        ]
                    }
                }
            )
            fake_handler = FakeSignalHandler()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp) / "data",
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                handler=fake_handler,
                intent_runner=FakeIntentRunner(
                    {
                        route_content: IntentDecision(
                            route="signal",
                            reason="继续查看具体信号",
                            confidence="high",
                        )
                    }
                ),
            )
            original_event = event(
                event_id="evt_bug_original",
                message_id="om_bug_a",
                content="@bot bug 分析完成",
            )
            app.activity_store.record_event(original_event)
            app.activity_store.record_result(
                original_event,
                TaskResult(
                    success=True,
                    message="上一轮分析完成",
                    details={
                        "mode": "bug_reanalysis",
                        "prepared_log_input": str(log_dir),
                        "selected_log_input": str(log_dir),
                    },
                ),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_a",
                chat_id="oc_denied",
                mode="bug_reanalysis",
                request_text="上一轮 bug 分析",
                summary_text="已有日志",
                report_url="",
                report_excerpt="",
            )

            result = app.handle_event(
                event(
                    event_id="evt_signal_followup",
                    message_id="om_signal_c",
                    reply_to="om_signal_b",
                    content=f"@bot {route_content}",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(len(fake_handler.requests), 1)
        resources = fake_handler.requests[0].resources
        self.assertEqual(resources[0].kind, "local")
        self.assertEqual(Path(resources[0].value), log_dir.resolve())
    def test_rom_version_lookup_preempts_signal_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            fake_handler = FakeSignalHandler()
            text = (
                "@bot XMARTM3EUD03E5_V6.2.2.6808_20260424002644.3_REV01_USERDEBUG "
                "调用rom-version skill 找下导航版本"
            )
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                handler=fake_handler,
                rom_version_runner=fake_rom,
            )

            result = app.handle_event(event(content=text))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "rom_version_lookup")
        self.assertEqual(len(fake_rom.requests), 1)
        self.assertEqual(fake_handler.requests, [])
        self.assertEqual(len(fake_lark.replies), 1)
        self.assertEqual(fake_lark.replies[0]["message_id"], "om_1")
        self.assertIn("导航版本", fake_lark.replies[0]["text"])
    def test_addr2line_request_routes_to_runner_with_rom(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            fake_addr2line = FakeAddr2LineRunner()
            text = (
                "@bot XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release 反解地址\n"
                "#05 pc 0000000000f385e4 /system/app/xp_envirodrive/lib/arm64/libunity.so"
            )
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"], lark=LarkOptions(bot_name="bot")),
                lark_client=fake_lark,
                rom_version_runner=fake_rom,
                addr2line_runner=fake_addr2line,
            )

            result = app.handle_event(event(content=text))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "addr2line_resolve")
        self.assertEqual(len(fake_addr2line.requests), 1)
        self.assertIn("libunity.so", fake_addr2line.requests[0].addr_text)
        self.assertEqual(
            fake_addr2line.requests[0].rom_version,
            "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
        )
        self.assertEqual(
            fake_addr2line.requests[0].symbol_table_url,
            "http://maven.xiaopeng.local/service/rest/repository/browse/"
            "xp_android_release/com/xiaopeng/lib/envirodrive_so/V6.1.0_20260327175820_Release/",
        )
        self.assertEqual(
            fake_addr2line.requests[0].napa5_download_url,
            "http://10.99.26.55/rom/napa/lib_napa5/6.1.0-test",
        )
        self.assertEqual(fake_addr2line.requests[0].apk_version, "V6.1.0_20260327175820_Release")
    def test_rom_plus_symbol_table_phrase_without_stack_routes_to_rom_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            fake_addr2line = FakeAddr2LineRunner()
            rom = "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release"
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"], lark=LarkOptions(bot_name="bot")),
                lark_client=fake_lark,
                rom_version_runner=fake_rom,
                addr2line_runner=fake_addr2line,
            )

            result = app.handle_event(
                event(
                    content=f"@bot ROM版本号{rom} 反解导航符号表"
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "rom_version_lookup")
        self.assertEqual(len(fake_rom.requests), 1)
        self.assertEqual(len(fake_addr2line.requests), 0)
        self.assertIn("导航版本", fake_lark.replies[-1]["text"])
    def test_addr2line_request_resolves_navigation_version_before_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            fake_addr2line = FakeAddr2LineRunner()
            rom = "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release"
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"], lark=LarkOptions(bot_name="bot")),
                lark_client=fake_lark,
                rom_version_runner=fake_rom,
                addr2line_runner=fake_addr2line,
            )

            result = app.handle_event(
                event(
                    content=(
                        f"@bot ROM版本号{rom} 反解导航符号表\n"
                        "#05 pc 0000000000f385e4 /system/app/xp_envirodrive/lib/arm64/libunity.so"
                    )
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "addr2line_resolve")
        self.assertEqual(len(fake_rom.requests), 1)
        self.assertEqual(len(fake_addr2line.requests), 1)
        self.assertEqual(fake_addr2line.requests[0].rom_version, rom)
        self.assertEqual(fake_addr2line.requests[0].apk_version, "V6.1.0_20260327175820_Release")
        self.assertEqual(
            fake_addr2line.requests[0].symbol_table_url,
            "http://maven.xiaopeng.local/service/rest/repository/browse/"
            "xp_android_release/com/xiaopeng/lib/envirodrive_so/V6.1.0_20260327175820_Release/",
        )
        self.assertEqual(
            fake_addr2line.requests[0].napa5_download_url,
            "http://10.99.26.55/rom/napa/lib_napa5/6.1.0-test",
        )

    def test_addr2line_runner_routes_navi_and_napa5_symbol_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / ".ai/skills/addr2line-resolve/scripts/addr2line_resolve.py"
            script.parent.mkdir(parents=True)
            script.write_text("# fake script\n", encoding="utf-8")
            runner = Addr2LineRunner(
                BridgeConfig(dry_run=False, data_dir=root / "data", workspace_root=root, guideengine_repo=root)
            )
            captured_script_commands = []
            captured_il2cpp = []

            def fake_run_tracked(command, **kwargs):
                captured_script_commands.append(command)
                payload = {
                    "apk_version": "V6.1.0_20260327175820_Release",
                    "so_artifact": "envirodrive_so",
                    "results": [
                        {
                            "so": "libxdata_sdk.so",
                            "addr": "0x338a3c",
                            "function": "X3D_Protocol::DrivingSensorPullOverParser::Parse",
                            "location": "driving_sensor_pullover_parser.cpp:14",
                        }
                    ],
                }
                return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

            def fake_download_napa_so(download_url, so_name, context):
                self.assertEqual(download_url, "http://napa.example/6.1.0-test")
                self.assertEqual(so_name, "libil2cpp.so")
                path = root / "libil2cpp.so"
                path.write_bytes(b"\x7fELF")
                return path

            def fake_local_addr2line(llvm_path, sym_path, addresses):
                captured_il2cpp.append((llvm_path, sym_path, addresses))
                return [
                    {
                        "addr": "0x1234",
                        "function": "Il2CppCrashFunction",
                        "location": "il2cppOutput/Assembly-CSharp.cpp:88",
                    }
                ]

            with (
                mock.patch.object(runner, "_script_path", return_value=script),
                mock.patch.object(runner, "_find_llvm_addr2line_binary", return_value="/ndk/llvm-addr2line"),
                mock.patch.object(runner, "_download_napa_so", side_effect=fake_download_napa_so),
                mock.patch.object(runner, "_run_local_addr2line", side_effect=fake_local_addr2line),
                mock.patch.object(addr2line_runner_module, "run_tracked_process", side_effect=fake_run_tracked),
            ):
                result = runner.run_resolve(
                    Addr2LineRequest(
                        addr_text="\n".join(
                            [
                                "#00 pc 0000000000338a3c /system/app/xp_envirodrive-mainland/lib/arm64/libxdata_sdk.so",
                                "#01 pc 0000000000001234 /system/app/xp_envirodrive-mainland/lib/arm64/libil2cpp.so",
                            ]
                        ),
                        rom_version="ROM",
                        apk_version="V6.1.0_20260327175820_Release",
                        symbol_table_url="http://maven.example/envirodrive_so/V6.1.0_20260327175820_Release/",
                        napa5_download_url="http://napa.example/6.1.0-test",
                        triggered=True,
                    )
                )

        self.assertTrue(result.success)
        self.assertEqual(result.details["result_count"], 2)
        self.assertEqual(result.details["symbol_sources"]["libxdata_sdk.so"], "navi")
        self.assertEqual(result.details["symbol_sources"]["libil2cpp.so"], "napa5")
        self.assertEqual(len(captured_script_commands), 1)
        self.assertIn("libxdata_sdk.so", " ".join(captured_script_commands[0]))
        self.assertNotIn("libil2cpp.so", " ".join(captured_script_commands[0]))
        self.assertEqual(captured_il2cpp[0][2], ["0x1234"])
        self.assertIn("libxdata_sdk.so 0x338a3c -> X3D_Protocol::DrivingSensorPullOverParser::Parse", result.message)
        self.assertIn("libil2cpp.so 0x1234 -> Il2CppCrashFunction", result.message)

    def test_addr2line_runner_dry_run_plans_all_symbol_sources_without_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / ".ai/skills/addr2line-resolve/scripts/addr2line_resolve.py"
            script.parent.mkdir(parents=True)
            script.write_text("# fake script\n", encoding="utf-8")
            unity_symbol = root / "libunity.sym.so"
            unity_symbol.write_bytes(b"\x7fELF")
            runner = Addr2LineRunner(
                BridgeConfig(dry_run=True, data_dir=root / "data", workspace_root=root, guideengine_repo=root)
            )

            with (
                mock.patch.object(runner, "_script_path", return_value=script),
                mock.patch.object(runner, "_find_builtin_unity_symbol_path", return_value=unity_symbol),
                mock.patch.object(runner, "_find_llvm_addr2line_binary", side_effect=AssertionError("dry-run should not resolve")),
                mock.patch.object(runner, "_download_napa_so", side_effect=AssertionError("dry-run should not download")),
            ):
                result = runner.run_resolve(
                    Addr2LineRequest(
                        addr_text="\n".join(
                            [
                                "#00 pc 0000000000f385e4 /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                                "#01 pc 0000000000338a3c /system/app/xp_envirodrive-mainland/lib/arm64/libxdata_sdk.so",
                                "#02 pc 0000000000001234 /system/app/xp_envirodrive-mainland/lib/arm64/libil2cpp.so",
                            ]
                        ),
                        apk_version="V6.1.0_20260327175820_Release",
                        symbol_table_url="http://maven.example/envirodrive_so/V6.1.0_20260327175820_Release/",
                        napa5_download_url="http://napa.example/6.1.0-test",
                        triggered=True,
                    )
                )

        self.assertTrue(result.success)
        self.assertEqual(
            result.details["symbol_sources"],
            {
                "libil2cpp.so": "napa5",
                "libunity.so": "builtin_unity",
                "libxdata_sdk.so": "navi",
            },
        )
        self.assertEqual(result.details["result_count"], 0)
        self.assertIn("dry-run", result.message)

    def test_addr2line_runner_resolves_unity_navi_and_napa5_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / ".ai/skills/addr2line-resolve/scripts/addr2line_resolve.py"
            script.parent.mkdir(parents=True)
            script.write_text("# fake script\n", encoding="utf-8")
            unity_symbol = root / "libunity.sym.so"
            unity_symbol.write_bytes(b"\x7fELF")
            napa_symbol = root / "libil2cpp.so"
            napa_symbol.write_bytes(b"\x7fELF")
            runner = Addr2LineRunner(
                BridgeConfig(dry_run=False, data_dir=root / "data", workspace_root=root, guideengine_repo=root)
            )
            captured_script_commands = []
            captured_local = []

            def fake_run_tracked(command, **kwargs):
                captured_script_commands.append(command)
                payload = {
                    "apk_version": "V6.1.0_20260327175820_Release",
                    "so_artifact": "envirodrive_so",
                    "results": [
                        {
                            "so": "libxdata_sdk.so",
                            "addr": "0x338a3c",
                            "function": "X3D_Protocol::DrivingSensorPullOverParser::Parse",
                            "location": "driving_sensor_pullover_parser.cpp:14",
                        }
                    ],
                }
                return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

            def fake_run_local(llvm_path, sym_path, addresses):
                captured_local.append((sym_path.name, addresses))
                if sym_path.name == "libunity.sym.so":
                    return [
                        {
                            "addr": "0xf385e4",
                            "function": "UnityBuiltinFunction",
                            "location": "Runtime/Unity.cpp:42",
                        }
                    ]
                return [
                    {
                        "addr": "0x1234",
                        "function": "Il2CppCrashFunction",
                        "location": "il2cppOutput/Assembly-CSharp.cpp:88",
                    }
                ]

            with (
                mock.patch.object(runner, "_script_path", return_value=script),
                mock.patch.object(runner, "_find_builtin_unity_symbol_path", return_value=unity_symbol),
                mock.patch.object(runner, "_find_llvm_addr2line_binary", return_value="/ndk/llvm-addr2line"),
                mock.patch.object(runner, "_download_napa_so", return_value=napa_symbol),
                mock.patch.object(runner, "_run_local_addr2line", side_effect=fake_run_local),
                mock.patch.object(addr2line_runner_module, "run_tracked_process", side_effect=fake_run_tracked),
            ):
                result = runner.run_resolve(
                    Addr2LineRequest(
                        addr_text="\n".join(
                            [
                                "#00 pc 0000000000f385e4 /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                                "#01 pc 0000000000338a3c /system/app/xp_envirodrive-mainland/lib/arm64/libxdata_sdk.so",
                                "#02 pc 0000000000001234 /system/app/xp_envirodrive-mainland/lib/arm64/libil2cpp.so",
                            ]
                        ),
                        apk_version="V6.1.0_20260327175820_Release",
                        symbol_table_url="http://maven.example/envirodrive_so/V6.1.0_20260327175820_Release/",
                        napa5_download_url="http://napa.example/6.1.0-test",
                        triggered=True,
                    )
                )

        self.assertTrue(result.success)
        self.assertEqual(result.details["result_count"], 3)
        self.assertEqual(result.details["symbol_sources"]["libunity.so"], "builtin_unity")
        self.assertEqual(result.details["symbol_sources"]["libxdata_sdk.so"], "navi")
        self.assertEqual(result.details["symbol_sources"]["libil2cpp.so"], "napa5")
        self.assertEqual(len(captured_script_commands), 1)
        command_text = " ".join(captured_script_commands[0])
        self.assertIn("libxdata_sdk.so", command_text)
        self.assertNotIn("libunity.so", command_text)
        self.assertNotIn("libil2cpp.so", command_text)
        self.assertIn(("libunity.sym.so", ["0xf385e4"]), captured_local)
        self.assertIn(("libil2cpp.so", ["0x1234"]), captured_local)
        self.assertIn("libunity.so 0xf385e4 -> UnityBuiltinFunction", result.message)
        self.assertIn("libxdata_sdk.so 0x338a3c -> X3D_Protocol::DrivingSensorPullOverParser::Parse", result.message)
        self.assertIn("libil2cpp.so 0x1234 -> Il2CppCrashFunction", result.message)
    def test_addr2line_request_fails_when_rom_lookup_cannot_resolve_navigation_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            failing_rom = FailingRomVersionRunner(message="SCM 查询失败")
            fake_addr2line = FakeAddr2LineRunner()
            rom = "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release"
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"], lark=LarkOptions(bot_name="bot")),
                lark_client=fake_lark,
                rom_version_runner=failing_rom,
                addr2line_runner=fake_addr2line,
            )

            result = app.handle_event(
                event(
                    content=(
                        f"@bot ROM版本号{rom} 反解导航符号表\n"
                        "#05 pc 0000000000f385e4 /system/app/xp_envirodrive/lib/arm64/libunity.so"
                    )
                )
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "addr2line_symbol_version_lookup_failed")
        self.assertEqual(len(failing_rom.requests), 1)
        self.assertEqual(len(fake_addr2line.requests), 0)
        self.assertIn("SCM 查询失败", result.message)
    def test_addr2line_request_prefers_recent_navigation_version_for_same_rom(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            fake_addr2line = FakeAddr2LineRunner()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"], lark=LarkOptions(bot_name="bot")),
                lark_client=fake_lark,
                rom_version_runner=fake_rom,
                addr2line_runner=fake_addr2line,
            )
            rom = "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release"
            app.handle_event(
                event(
                    event_id="evt_rom",
                    message_id="om_rom",
                    content=f"@bot ROM版本号{rom} 找下导航版本",
                )
            )

            result = app.handle_event(
                event(
                    event_id="evt_stack",
                    message_id="om_stack",
                    content=(
                        f"@bot ROM版本号{rom} 反解crash.txt堆栈\n"
                        "#05 pc 0000000000f385e4 /system/app/xp_envirodrive/lib/arm64/libunity.so"
                    ),
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "addr2line_resolve")
        self.assertEqual(len(fake_addr2line.requests), 1)
        self.assertEqual(fake_addr2line.requests[0].rom_version, rom)
        self.assertEqual(fake_addr2line.requests[0].apk_version, "V6.1.0_20260327175820_Release")
        self.assertIn("addr2line 反解完成", fake_lark.replies[-1]["text"])
    def test_addr2line_request_reuses_recent_same_chat_rom_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            fake_addr2line = FakeAddr2LineRunner()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"], lark=LarkOptions(bot_name="bot")),
                lark_client=fake_lark,
                rom_version_runner=fake_rom,
                addr2line_runner=fake_addr2line,
            )
            rom_text = (
                "@bot ROM版本号XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release "
                "找下导航版本"
            )
            app.handle_event(event(event_id="evt_rom", message_id="om_rom", content=rom_text))

            result = app.handle_event(
                event(
                    event_id="evt_stack",
                    message_id="om_stack",
                    content=(
                        "@bot 反解地址\n"
                        "#05 pc 0000000000f385e4 /system/app/xp_envirodrive/lib/arm64/libunity.so"
                    ),
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "addr2line_resolve")
        self.assertEqual(len(fake_addr2line.requests), 1)
        self.assertEqual(
            fake_addr2line.requests[0].rom_version,
            "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
        )
        self.assertEqual(
            fake_addr2line.requests[0].symbol_table_url,
            "http://maven.xiaopeng.local/service/rest/repository/browse/"
            "xp_android_release/com/xiaopeng/lib/envirodrive_so/V6.1.0_20260327175820_Release/",
        )
        self.assertEqual(
            fake_addr2line.requests[0].napa5_download_url,
            "http://10.99.26.55/rom/napa/lib_napa5/6.1.0-test",
        )
    def test_addr2line_file_reply_routes_with_referenced_file_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            fake_addr2line = FakeAddr2LineRunner()
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": {"file_key": "file_crash_txt"},
                            }
                        ]
                    }
                }
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="bot"),
                ),
                lark_client=fake_lark,
                rom_version_runner=fake_rom,
                addr2line_runner=fake_addr2line,
            )
            text = (
                "@bot XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release "
                "反解导航符号表"
            )

            result = app.handle_event(event(content=text, reply_to="om_file_msg"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "addr2line_resolve")
        self.assertEqual(len(fake_rom.requests), 1)
        self.assertEqual(len(fake_addr2line.requests), 1)
        request = fake_addr2line.requests[0]
        self.assertEqual(request.resources[0].kind, "file")
        self.assertEqual(request.resources[0].value, "file_crash_txt")
        self.assertEqual(request.resources[0].source_message_id, "om_file_msg")
        self.assertEqual(request.addr_text, "")
        self.assertEqual(
            request.rom_version,
            "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
        )
        self.assertEqual(request.apk_version, "V6.1.0_20260327175820_Release")
    def test_addr2line_file_reply_accepts_symbol_decomposition_wording(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            fake_addr2line = FakeAddr2LineRunner()
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": {"file_key": "file_crash_zip"},
                            }
                        ]
                    }
                }
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="bot"),
                ),
                lark_client=fake_lark,
                rom_version_runner=fake_rom,
                addr2line_runner=fake_addr2line,
            )
            rom = "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release"
            app.handle_event(event(event_id="evt_rom", message_id="om_rom", content=f"@bot ROM版本号{rom} 查导航版本"))

            result = app.handle_event(
                event(
                    event_id="evt_addr2line_symbol_decomposition",
                    message_id="om_addr2line_symbol_decomposition",
                    content="@bot 分解符号表",
                    reply_to="om_file_msg",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "addr2line_resolve")
        self.assertEqual(len(fake_addr2line.requests), 1)
        request = fake_addr2line.requests[0]
        self.assertEqual(request.resources[0].kind, "file")
        self.assertEqual(request.resources[0].value, "file_crash_zip")
        self.assertEqual(request.rom_version, rom)
        self.assertEqual(request.apk_version, "V6.1.0_20260327175820_Release")
    def test_addr2line_followup_recovers_original_file_from_reply_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            fake_addr2line = FakeAddr2LineRunner()
            fake_lark.fetched_messages["om_current"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_current",
                                "reply_to": "om_previous_text",
                            }
                        ]
                    }
                }
            )
            fake_lark.fetched_messages["om_previous_text"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_previous_text",
                                "reply_to": "om_file_msg",
                                "content": {"text": "ROM版本号XMART... 反解crash.txt堆栈"},
                            }
                        ]
                    }
                }
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": {"file_key": "file_crash_zip"},
                            }
                        ]
                    }
                }
            )
            rom = "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release"
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="bot"),
                ),
                lark_client=fake_lark,
                rom_version_runner=fake_rom,
                addr2line_runner=fake_addr2line,
            )

            result = app.handle_event(
                event(
                    event_id="evt_addr2line_followup",
                    message_id="om_current",
                    content=f"@bot ROM版本号{rom} 反解导航符号表",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "addr2line_resolve")
        self.assertEqual(len(fake_rom.requests), 1)
        self.assertEqual(len(fake_addr2line.requests), 1)
        request = fake_addr2line.requests[0]
        self.assertEqual(request.resources[0].kind, "file")
        self.assertEqual(request.resources[0].value, "file_crash_zip")
        self.assertEqual(request.resources[0].source_message_id, "om_file_msg")
        self.assertEqual(request.addr_text, "")
        self.assertEqual(request.rom_version, rom)
        self.assertEqual(request.apk_version, "V6.1.0_20260327175820_Release")
    def test_addr2line_runner_extracts_last_montecarlo_stack_from_logd_crash_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / ".ai/skills/addr2line-resolve/scripts/addr2line_resolve.py"
            script.parent.mkdir(parents=True)
            script.write_text("# fake script\n", encoding="utf-8")
            crash_file = root / "zip_src/data/Log/log0/logd/crash.txt.01"
            crash_file.parent.mkdir(parents=True)
            crash_file.write_text(
                "\n".join(
                    [
                        "--------- beginning of crash",
                        "05-19 15:27:55.000  1000  1000 F DEBUG   : Cmdline: /system/bin/other",
                        "05-19 15:27:55.000  1000  1000 F DEBUG   :       #00 pc 0000000000012340  /system/lib64/libother.so",
                        "05-19 15:28:40.041  9497  9497 F DEBUG   : Cmdline: /system/app/xp_envirodrive-mainland/xp_envirodrive-mainland",
                        "05-19 15:28:40.041  9497  9497 F DEBUG   : pid: 2531, tid: 9497, name: UnityMain  >>> com.xiaopeng.montecarlo <<<",
                        "05-19 15:28:40.041  9497  9497 F DEBUG   :       #00 pc 0000000000f385e4  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        "05-19 15:28:40.041  9497  9497 F DEBUG   :       #01 pc 00000000010fa7f0  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        "05-19 15:32:57.535 11311 11311 F DEBUG   : Cmdline: /system/app/xp_envirodrive-mainland/xp_envirodrive-mainland",
                        "05-19 15:32:57.535 11311 11311 F DEBUG   : pid: 11311, tid: 11311, name: UnityMain  >>> com.xiaopeng.montecarlo <<<",
                        "05-19 15:32:57.535 11311 11311 F DEBUG   :       #00 pc 00000000010f5948  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        "05-19 15:32:57.535 11311 11311 F DEBUG   :       #01 pc 0000000002020202  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                    ]
                ),
                encoding="utf-8",
            )
            archive = root / "crash_bundle.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.write(crash_file, "data/Log/log0/logd/crash.txt.01")
                zf.writestr(
                    "dfx.txt",
                    "\n".join(
                        [
                            '"processName" : "com.xiaopeng.montecarlo",',
                            '"stack" : "      #00 pc 00000000dfdfdfdf  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so\\n",',
                        ]
                    ),
                )
            runner = Addr2LineRunner(
                BridgeConfig(dry_run=True, data_dir=root / "data", workspace_root=root, guideengine_repo=root)
            )

            result = runner.run_resolve(
                Addr2LineRequest(
                    addr_text="",
                    rom_version="XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
                    resources=[DownloadResource(kind="local", value=str(archive))],
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        command_text = " ".join(result.command or [])
        self.assertIn("00000000010f5948", command_text)
        self.assertIn("0000000002020202", command_text)
        self.assertNotIn("0000000000f385e4", command_text)
        self.assertNotIn("dfdfdfdf", command_text)
        self.assertTrue(str(result.details["addr_source"]).endswith("data/Log/log0/logd/crash.txt.01"))
    def test_addr2line_runner_extracts_last_navigation_stack_from_crash_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / ".ai/skills/addr2line-resolve/scripts/addr2line_resolve.py"
            script.parent.mkdir(parents=True)
            script.write_text("# fake script\n", encoding="utf-8")
            crash_file = root / "crash.txt"
            crash_file.write_text(
                "\n".join(
                    [
                        "05-16 12:00:00.000  1000  1000 F DEBUG   : Cmdline: /system/bin/other",
                        "05-16 12:00:00.000  1000  1000 F DEBUG   :       #00 pc 0000000000012340  /system/lib64/libother.so",
                        "05-16 12:18:40.041  9497  9497 F DEBUG   : Cmdline: /system/app/xp_envirodrive/xp_envirodrive",
                        "05-16 12:18:40.041  9497  9497 F DEBUG   :       #05 pc 0000000000f385e4  /system/app/xp_envirodrive/lib/arm64/libunity.so",
                        "05-16 12:18:40.041  9497  9497 F DEBUG   :       #06 pc 00000000010fa7f0  /system/app/xp_envirodrive/lib/arm64/libunity.so",
                        "05-16 12:18:41.041  9497  9497 F DEBUG   : Cmdline: /system/app/xp_envirodrive/xp_envirodrive",
                        "05-16 12:18:41.041  9497  9497 F DEBUG   :       #05 pc 00000000010f5948  /system/app/xp_envirodrive/lib/arm64/libunity.so",
                    ]
                ),
                encoding="utf-8",
            )
            runner = Addr2LineRunner(
                BridgeConfig(dry_run=True, data_dir=root / "data", workspace_root=root, guideengine_repo=root)
            )

            result = runner.run_resolve(
                Addr2LineRequest(
                    addr_text="",
                    rom_version="XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
                    resources=[DownloadResource(kind="local", value=str(crash_file))],
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        self.assertIn("00000000010f5948", " ".join(result.command or []))
        self.assertNotIn("0000000000f385e4", " ".join(result.command or []))
        self.assertEqual(result.details["addr_source"], str(crash_file))
    def test_addr2line_runner_infers_rom_from_lowest_log_prop_when_request_has_no_symbol_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / ".ai/skills/addr2line-resolve/scripts/addr2line_resolve.py"
            script.parent.mkdir(parents=True)
            script.write_text("# fake script\n", encoding="utf-8")
            rom_log1 = "XMARTQGZHE29E5_V6.1.0.1111_20260327111111.1_REV01_USER_Release"
            rom_log2 = "XMARTQGZHE29E5_V6.1.0.2222_20260327222222.1_REV01_USER_Release"
            log1_crash = root / "zip_src/data/Log/log1/logd/crash.txt"
            log2_crash = root / "zip_src/data/Log/log2/logd/crash.txt"
            for path, text in (
                (
                    log1_crash,
                    "\n".join(
                        [
                            "05-19 15:28:40.041  9497  9497 F DEBUG   : Cmdline: /system/app/xp_envirodrive-mainland/xp_envirodrive-mainland",
                            "05-19 15:28:40.041  9497  9497 F DEBUG   : pid: 2531, tid: 9497, name: UnityMain  >>> com.xiaopeng.montecarlo <<<",
                            "05-19 15:28:40.041  9497  9497 F DEBUG   :       #00 pc 0000000000f385e4  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        ]
                    ),
                ),
                (
                    log2_crash,
                    "\n".join(
                        [
                            "05-19 15:32:57.535 11311 11311 F DEBUG   : Cmdline: /system/app/xp_envirodrive-mainland/xp_envirodrive-mainland",
                            "05-19 15:32:57.535 11311 11311 F DEBUG   : pid: 11311, tid: 11311, name: UnityMain  >>> com.xiaopeng.montecarlo <<<",
                            "05-19 15:32:57.535 11311 11311 F DEBUG   :       #00 pc 00000000010f5948  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        ]
                    ),
                ),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            (log1_crash.parent / "prop.txt").write_text(f"ro.build.version={rom_log1}\n", encoding="utf-8")
            (log2_crash.parent / "prop.txt").write_text(f"ro.build.version={rom_log2}\n", encoding="utf-8")
            archive = root / "crash_bundle.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.write(log1_crash, "data/Log/log1/logd/crash.txt")
                zf.write(log2_crash, "data/Log/log2/logd/crash.txt")
                zf.write(log1_crash.parent / "prop.txt", "data/Log/log1/logd/prop.txt")
                zf.write(log2_crash.parent / "prop.txt", "data/Log/log2/logd/prop.txt")
            fake_rom = FakeRomVersionRunner()
            runner = Addr2LineRunner(
                BridgeConfig(dry_run=True, data_dir=root / "data", workspace_root=root, guideengine_repo=root),
                rom_version_runner=fake_rom,
            )

            result = runner.run_resolve(
                Addr2LineRequest(
                    addr_text="",
                    resources=[DownloadResource(kind="local", value=str(archive))],
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(len(fake_rom.requests), 1)
        self.assertEqual(fake_rom.requests[0].rom_version, rom_log1)
        self.assertTrue(str(result.details["addr_source"]).endswith("data/Log/log1/logd/crash.txt"))
    def test_addr2line_runner_prefers_requested_log_folder_for_prop_and_crash_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / ".ai/skills/addr2line-resolve/scripts/addr2line_resolve.py"
            script.parent.mkdir(parents=True)
            script.write_text("# fake script\n", encoding="utf-8")
            rom_log1 = "XMARTQGZHE29E5_V6.1.0.3333_20260327333333.1_REV01_USER_Release"
            rom_log2 = "XMARTQGZHE29E5_V6.1.0.4444_20260327444444.1_REV01_USER_Release"
            log1_crash = root / "zip_src/data/Log/log1/logd/crash.txt"
            log2_crash = root / "zip_src/data/Log/log2/logd/crash.txt"
            for path, text in (
                (
                    log1_crash,
                    "\n".join(
                        [
                            "05-19 15:28:40.041  9497  9497 F DEBUG   : Cmdline: /system/app/xp_envirodrive-mainland/xp_envirodrive-mainland",
                            "05-19 15:28:40.041  9497  9497 F DEBUG   : pid: 2531, tid: 9497, name: UnityMain  >>> com.xiaopeng.montecarlo <<<",
                            "05-19 15:28:40.041  9497  9497 F DEBUG   :       #00 pc 0000000000f385e4  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        ]
                    ),
                ),
                (
                    log2_crash,
                    "\n".join(
                        [
                            "05-19 15:32:57.535 11311 11311 F DEBUG   : Cmdline: /system/app/xp_envirodrive-mainland/xp_envirodrive-mainland",
                            "05-19 15:32:57.535 11311 11311 F DEBUG   : pid: 11311, tid: 11311, name: UnityMain  >>> com.xiaopeng.montecarlo <<<",
                            "05-19 15:32:57.535 11311 11311 F DEBUG   :       #00 pc 00000000010f5948  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        ]
                    ),
                ),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            (log1_crash.parent / "prop.txt").write_text(f"ro.build.version={rom_log1}\n", encoding="utf-8")
            (log2_crash.parent / "prop.txt").write_text(f"ro.build.version={rom_log2}\n", encoding="utf-8")
            archive = root / "crash_bundle.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.write(log1_crash, "data/Log/log1/logd/crash.txt")
                zf.write(log2_crash, "data/Log/log2/logd/crash.txt")
                zf.write(log1_crash.parent / "prop.txt", "data/Log/log1/logd/prop.txt")
                zf.write(log2_crash.parent / "prop.txt", "data/Log/log2/logd/prop.txt")
            fake_rom = FakeRomVersionRunner()
            runner = Addr2LineRunner(
                BridgeConfig(dry_run=True, data_dir=root / "data", workspace_root=root, guideengine_repo=root),
                rom_version_runner=fake_rom,
            )

            result = runner.run_resolve(
                Addr2LineRequest(
                    addr_text="",
                    resources=[DownloadResource(kind="local", value=str(archive))],
                    log_folder="log1",
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(len(fake_rom.requests), 1)
        self.assertEqual(fake_rom.requests[0].rom_version, rom_log1)
        self.assertTrue(str(result.details["addr_source"]).endswith("data/Log/log1/logd/crash.txt"))
    def test_addr2line_runner_uses_fault_time_to_pick_matching_log_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / ".ai/skills/addr2line-resolve/scripts/addr2line_resolve.py"
            script.parent.mkdir(parents=True)
            script.write_text("# fake script\n", encoding="utf-8")
            rom_log1 = "XMARTQGZHE29E5_V6.1.0.5555_20260327555555.1_REV01_USER_Release"
            rom_log2 = "XMARTQGZHE29E5_V6.1.0.6666_20260327666666.1_REV01_USER_Release"
            log1_crash = root / "zip_src/data/Log/log1/logd/crash.txt"
            log2_crash = root / "zip_src/data/Log/log2/logd/crash.txt"
            for path, text in (
                (
                    log1_crash,
                    "\n".join(
                        [
                            "05-19 15:28:40.041  9497  9497 F DEBUG   : Cmdline: /system/app/xp_envirodrive-mainland/xp_envirodrive-mainland",
                            "05-19 15:28:40.041  9497  9497 F DEBUG   : pid: 2531, tid: 9497, name: UnityMain  >>> com.xiaopeng.montecarlo <<<",
                            "05-19 15:28:40.041  9497  9497 F DEBUG   :       #00 pc 0000000000f385e4  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        ]
                    ),
                ),
                (
                    log2_crash,
                    "\n".join(
                        [
                            "05-19 15:32:57.535 11311 11311 F DEBUG   : Cmdline: /system/app/xp_envirodrive-mainland/xp_envirodrive-mainland",
                            "05-19 15:32:57.535 11311 11311 F DEBUG   : pid: 11311, tid: 11311, name: UnityMain  >>> com.xiaopeng.montecarlo <<<",
                            "05-19 15:32:57.535 11311 11311 F DEBUG   :       #00 pc 00000000010f5948  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        ]
                    ),
                ),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            (log1_crash.parent / "prop.txt").write_text(f"ro.build.version={rom_log1}\n", encoding="utf-8")
            (log2_crash.parent / "prop.txt").write_text(f"ro.build.version={rom_log2}\n", encoding="utf-8")
            archive = root / "crash_bundle.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.write(log1_crash, "data/Log/log1/logd/crash.txt")
                zf.write(log2_crash, "data/Log/log2/logd/crash.txt")
                zf.write(log1_crash.parent / "prop.txt", "data/Log/log1/logd/prop.txt")
                zf.write(log2_crash.parent / "prop.txt", "data/Log/log2/logd/prop.txt")
            fake_rom = FakeRomVersionRunner()
            runner = Addr2LineRunner(
                BridgeConfig(dry_run=True, data_dir=root / "data", workspace_root=root, guideengine_repo=root),
                rom_version_runner=fake_rom,
            )

            result = runner.run_resolve(
                Addr2LineRequest(
                    addr_text="",
                    resources=[DownloadResource(kind="local", value=str(archive))],
                    fault_time="05-19 15:28",
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(len(fake_rom.requests), 1)
        self.assertEqual(fake_rom.requests[0].rom_version, rom_log1)
        self.assertTrue(str(result.details["addr_source"]).endswith("data/Log/log1/logd/crash.txt"))
    def test_addr2line_runner_parses_subrealitytrace_threads_when_symbol_version_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace_file = root / "subrealitytrace_2026-05-24-20-04-00"
            trace_file.write_text(
                "\n".join(
                    [
                        "----- pid 24454 at 2026-05-24 20:04:13.754987528+0800 -----",
                        '"peng.montecarlo" sysTid=24454',
                        "    #00 pc 0000000000085a9c  /apex/com.android.runtime/lib64/bionic/libc.so (syscall+28)",
                        "    #01 pc 00000000030fa9bc  /system/framework/arm64/boot-framework.oat (android.app.ActivityThread.main+732)",
                        '"UnityMain" sysTid=25293',
                        "    #00 pc 00000000012ae2a0  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        "    #01 pc 000000000072afb4  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        '"XPD_LD" sysTid=24839',
                        "    #00 pc 00000000000264f0  /system/app/xp_envirodrive-mainland/lib/arm64/libxdata_native.so",
                        "    #01 pc 0000000000402480  /system/app/xp_envirodrive-mainland/lib/arm64/libxdata_sdk.so",
                        '"JniSurfaceTexLoop" sysTid=24859',
                        "    #00 pc 00000000000175a4  /system/app/xp_envirodrive-mainland/lib/arm64/libRenderExtend.so",
                        "    #01 pc 00000000000179dc  /system/app/xp_envirodrive-mainland/lib/arm64/libRenderExtend.so",
                        '"RenderThread" sysTid=24525',
                        "    #00 pc 00000000003c7138  /system/lib64/libhwui.so (android::uirenderer::renderthread::RenderThread::threadLoop()+76)",
                        '"GLThread 883" sysTid=24542',
                        "    #00 pc 000000000153d768  /system/framework/arm64/boot-framework.oat (android.opengl.GLSurfaceView$GLThread.guardedRun+1944)",
                    ]
                ),
                encoding="utf-8",
            )
            runner = Addr2LineRunner(
                BridgeConfig(dry_run=True, data_dir=root / "data", workspace_root=root, guideengine_repo=root)
            )

            result = runner.run_resolve(
                Addr2LineRequest(
                    addr_text="",
                    resources=[DownloadResource(kind="local", value=str(trace_file))],
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details.get("analysis_mode"), "single_trace_thread_parse")
        self.assertEqual(result.details.get("trace_source"), str(trace_file))
        counts = result.details.get("thread_category_counts") or {}
        self.assertGreaterEqual(counts.get("UnityMain", 0), 1)
        self.assertGreaterEqual(counts.get("XPD_*", 0), 1)
        self.assertGreaterEqual(counts.get("JniSurfaceTex*", 0), 1)
        self.assertGreaterEqual(counts.get("主线程", 0), 1)
        self.assertGreaterEqual(counts.get("渲染相关线程", 0), 1)
        self.assertIn("UnityMain", result.message)
        self.assertIn("场景/渲染相关 so:", result.message)
        self.assertIn("UnityClassic::Baselib_SystemFutex_Wait", result.message)
        self.assertIn("Semaphore::WaitForSignal", result.message)
        self.assertIn("JniSurfaceTexLoop", result.message)
        self.assertNotIn("#00 pc", result.message)
    def test_addr2line_runner_keeps_missing_symbol_version_for_non_trace_single_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / ".ai/skills/addr2line-resolve/scripts/addr2line_resolve.py"
            script.parent.mkdir(parents=True)
            script.write_text("# fake script\n", encoding="utf-8")
            crash_file = root / "crash.txt"
            crash_file.write_text(
                "\n".join(
                    [
                        "#00 pc 0000000000f385e4 /system/app/xp_envirodrive/lib/arm64/libunity.so",
                        "#01 pc 00000000010fa7f0 /system/app/xp_envirodrive/lib/arm64/libunity.so",
                    ]
                ),
                encoding="utf-8",
            )
            runner = Addr2LineRunner(
                BridgeConfig(dry_run=True, data_dir=root / "data", workspace_root=root, guideengine_repo=root)
            )

            result = runner.run_resolve(
                Addr2LineRequest(
                    addr_text="",
                    resources=[DownloadResource(kind="local", value=str(crash_file))],
                    triggered=True,
                )
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_symbol_version")
    def test_scene_signal_prompt_preempts_generic_signal_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            route_content = "分析3D场景信号"
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_handler = FakeSignalHandler()
            fake_lark.fetched_messages["om_file_msg"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_file_msg",
        "content": "{\\"file_key\\":\\"file_scene_log\\"}"
      }
    ]
  }
}
"""
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                handler=fake_handler,
                intent_runner=FakeIntentRunner(
                    {
                        route_content: IntentDecision(
                            route="signal",
                            reason="旧路由误判为信号生命周期",
                            confidence="high",
                        )
                    }
                ),
            )

            result = app.handle_event(event(reply_to="om_file_msg", content=f"@bot {route_content}"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_handler.requests), 0)
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, route_content)
        self.assertEqual(fake_bug.requests[0].resources[0].kind, "file")
        self.assertEqual(fake_bug.requests[0].resources[0].value, "file_scene_log")
    def test_scene_signal_new_request_does_not_require_latest_chat_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            route_content = "分析 3D场景信号"
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                intent_runner=FakeIntentRunner(
                    {
                        route_content: IntentDecision(
                            route="analysis_followup",
                            reason="同群最近一次场景信号主题",
                            confidence="high",
                            followup_action="context_chat",
                            context_source="latest_chat",
                        )
                    }
                ),
            )
            app.conversation_store.remember(
                root_message_id="om_previous_scene_report",
                chat_id="oc_denied",
                mode="direct_analysis",
                request_text="分析下 3D场景信号",
                summary_text="已有场景信号报告",
                report_url="http://report",
                report_excerpt="3D场景信号分析报告",
            )

            result = app.handle_event(
                event(
                    event_id="evt_scene_new_request",
                    message_id="om_scene_new_request",
                    content=f"@bot {route_content}",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertNotEqual(result.error_code, "missing_followup_reply")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, route_content)
    def test_reply_to_file_intent_followup_misroute_falls_back_to_direct_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            route_content = "问题时间 2026-05-29 17:16，分析启动和卡顿"
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": '{"file_key":"file_lane_level_log"}',
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                intent_runner=FakeIntentRunner(
                    {
                        route_content: IntentDecision(
                            route="analysis_followup",
                            reason="误把回复文件当成续聊",
                            confidence="high",
                            followup_action="context_chat",
                            context_source="none",
                        )
                    }
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_direct_analysis_reply_file",
                    message_id="om_direct_analysis_reply_file",
                    reply_to="om_file_msg",
                    content=f"@bot {route_content}",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertNotEqual(result.error_code, "missing_followup_reply")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, route_content)
        self.assertEqual(fake_bug.requests[0].resources[0].kind, "file")
        self.assertEqual(fake_bug.requests[0].resources[0].value, "file_lane_level_log")
    def test_followup_keywords_with_reply_file_still_reach_intent_router_direct_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            route_content = "问题时间 2026-05-29 17:16，分析车道级 @朱云龙的飞书 CLI"
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_current"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_current",
                                "content": route_content,
                                "reply_to": "om_file_msg",
                                "mentions": [
                                    {
                                        "id": "cli_a976baa2cdfadcc7",
                                        "key": "@_user_1",
                                        "name": "朱云龙的飞书 CLI",
                                    }
                                ],
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": '<file key="file_v3_00125_xxx" name="main_2026-05-29_17-00.alog"/>',
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                allowed_chats=["oc_denied"],
            )
            config.lark.bot_name = "朱云龙的飞书 CLI"
            app = BridgeApp(
                config,
                lark_client=fake_lark,
                bug_runner=fake_bug,
                intent_runner=FakeIntentRunner(
                    {
                        "问题时间 2026-05-29 17:16，分析车道级": IntentDecision(
                            route="analysis_followup",
                            reason="误把回复文件当成续聊",
                            confidence="high",
                            followup_action="context_chat",
                            context_source="none",
                        )
                    }
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_reply_file_followup_keywords",
                    message_id="om_current",
                    content=route_content,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertNotEqual(result.error_code, "missing_followup_reply")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].resources[0].value, "file_v3_00125_xxx")
    def test_intent_bug_misroute_with_reply_file_falls_back_to_direct_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            route_content = "@朱云龙的飞书 CLI 问题时间 2026-05-29 17:16，分析车道级"
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_current"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_current",
                                "content": route_content,
                                "reply_to": "om_file_msg",
                                "mentions": [
                                    {
                                        "id": "cli_a976baa2cdfadcc7",
                                        "key": "@_user_1",
                                        "name": "朱云龙的飞书 CLI",
                                    }
                                ],
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": '<file key="file_v3_00125_bug_misroute" name="main_2026-05-29_17-00.alog"/>',
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                allowed_chats=["oc_denied"],
            )
            config.lark.bot_name = "朱云龙的飞书 CLI"
            app = BridgeApp(
                config,
                lark_client=fake_lark,
                bug_runner=fake_bug,
                intent_runner=FakeIntentRunner(
                    {
                        "问题时间 2026-05-29 17:16，分析车道级": IntentDecision(
                            route="bug",
                            reason="误判成 bug 重分析",
                            confidence="high",
                            followup_action="reanalysis",
                            context_source="explicit",
                        )
                    }
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_reply_file_bug_misroute",
                    message_id="om_current",
                    content=route_content,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertNotEqual(result.error_code, "missing_bug_url")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].resources[0].value, "file_v3_00125_bug_misroute")
    def test_signal_followup_fetches_current_message_when_event_omits_reply_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            (log_dir / "main.log").write_text("05-20 12:00:00 signal 132002", encoding="utf-8")
            route_content = "132002 信号"
            fake_lark = FakeLarkClient()
            fake_lark.fetched_messages["om_signal_c"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_signal_c",
                                "reply_to": "om_signal_b",
                            }
                        ]
                    }
                }
            )
            fake_lark.fetched_messages["om_signal_b"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_signal_b",
                                "reply_to": "om_bug_a",
                            }
                        ]
                    }
                }
            )
            fake_handler = FakeSignalHandler()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp) / "data",
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                handler=fake_handler,
                intent_runner=FakeIntentRunner(
                    {
                        route_content: IntentDecision(
                            route="signal",
                            reason="查看具体信号",
                            confidence="high",
                        )
                    }
                ),
            )
            original_event = event(
                event_id="evt_bug_original",
                message_id="om_bug_a",
                content="@bot bug 分析完成",
            )
            app.activity_store.record_event(original_event)
            app.activity_store.record_result(
                original_event,
                TaskResult(
                    success=True,
                    message="上一轮分析完成",
                    details={
                        "mode": "bug_reanalysis",
                        "prepared_log_input": str(log_dir),
                        "selected_log_input": str(log_dir),
                    },
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_signal_followup_no_reply_field",
                    message_id="om_signal_c",
                    content=f"@bot {route_content}",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(len(fake_handler.requests), 1)
        resources = fake_handler.requests[0].resources
        self.assertEqual(resources[0].kind, "local")
        self.assertEqual(Path(resources[0].value), log_dir.resolve())
