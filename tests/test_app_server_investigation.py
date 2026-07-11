import tempfile
import unittest
from pathlib import Path

from lark_agent_bridge.agents.codex_app_server_runtime import CodexAppServerTurnController
from lark_agent_bridge.app import BridgeApp
from lark_agent_bridge.models import TaskResult

from tests._app_base import BridgeConfig, FakeBugRunner, FakeLarkClient, event


class FakeAppServerInvestigationRunner:
    def __init__(self, html_path: Path):
        self.html_path = html_path
        self.requests = []

    def run(self, request, *, event=None, progress_callback=None, control=None):
        self.requests.append(
            {"request": request, "event": event, "progress_callback": progress_callback, "control": control}
        )
        self.html_path.write_text("<html><body>app-server investigation</body></html>", encoding="utf-8")
        return TaskResult(
            success=True,
            message="AI 自主分析完成",
            job_id=event.event_id if event else "app_server_job",
            html_report=self.html_path,
            details={
                "mode": "app_server_investigation",
                "source_mode": "app_server_autonomous",
                "context_profile": "app_server_autonomous",
                "classification_source": "configured_app_server_investigation",
                "files_to_send": [self.html_path],
                "trigger_term": request.trigger_term,
                "trigger_mode": request.trigger_mode,
                "bug_url": request.bug_url,
            },
        )


class FakeRunningAppServerControl:
    def __init__(self):
        self.steers = []
        self.cancels = []
        self.steer_result = True

    def steer(self, text: str, *, source_message_id: str = "", actor_id: str = "") -> bool:
        self.steers.append({"text": text, "source_message_id": source_message_id, "actor_id": actor_id})
        return self.steer_result

    def cancel(self, reason: str = "", *, source_message_id: str = "", actor_id: str = "") -> bool:
        self.cancels.append({"reason": reason, "source_message_id": source_message_id, "actor_id": actor_id})
        return True


class AppServerInvestigationRouteTests(unittest.TestCase):
    BUG_URL = "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118"

    def _config(self, tmp: str):
        config = BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"])
        config.bug_analysis.app_server_investigation.enabled = True
        config.bug_analysis.app_server_investigation.prompt_template = "context={context_path}\ninventory={skill_inventory_path}\nout={output_path}"
        return config

    def test_auto_bug_route_uses_app_server_investigation_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark = FakeLarkClient()
            fake_runner = FakeAppServerInvestigationRunner(Path(tmp) / "app_server.html")
            app = BridgeApp(
                self._config(tmp),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                app_server_investigation_runner=fake_runner,
            )

            result = app.handle_event(
                event(
                    event_id="evt_app_auto",
                    message_id="om_app_auto",
                    content=f"@bot auto {self.BUG_URL} 调查下 3D 生命周期",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "app_server_investigation")
        self.assertEqual(len(fake_runner.requests), 1)
        self.assertEqual(fake_bug.requests, [])
        request = fake_runner.requests[0]["request"]
        self.assertEqual(request.bug_url, self.BUG_URL)
        self.assertEqual(request.prompt, "调查下 3D 生命周期")
        self.assertEqual(request.trigger_mode, "auto")

    def test_app_server_followup_keeps_original_conversation_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_runner = FakeAppServerInvestigationRunner(Path(tmp) / "app_server.html")
            app = BridgeApp(
                self._config(tmp),
                lark_client=fake_lark,
                app_server_investigation_runner=fake_runner,
            )
            original_text = f"{self.BUG_URL} 自主分析 调查3D生命周期"
            app.conversation_store.remember(
                root_message_id="om_app_server_root",
                chat_id="oc_denied",
                mode="app_server_investigation",
                request_text=original_text,
                summary_text="首次分析完成",
                report_url="",
                report_excerpt="",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_app_server_result",
                root_message_id="om_app_server_root",
            )

            result = app.handle_event(
                event(
                    event_id="evt_app_server_followup",
                    message_id="om_app_server_followup",
                    reply_to="om_app_server_result",
                    content="@bot 自主分析 继续查3D生命周期",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "app_server_investigation")
        self.assertEqual(result.details["conversation_root_message_id"], "om_app_server_root")
        self.assertIsNone(app.activity_store.get_session("om_app_server_followup"))

    def test_free_term_bug_route_uses_app_server_investigation_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark = FakeLarkClient()
            fake_runner = FakeAppServerInvestigationRunner(Path(tmp) / "app_server.html")
            app = BridgeApp(
                self._config(tmp),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                app_server_investigation_runner=fake_runner,
            )

            result = app.handle_event(
                event(
                    event_id="evt_app_free",
                    message_id="om_app_free",
                    content=f"@bot {self.BUG_URL} 全技能分析 调查下 3D 生命周期",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "app_server_investigation")
        self.assertEqual(len(fake_runner.requests), 1)
        self.assertEqual(fake_bug.requests, [])
        self.assertEqual(fake_runner.requests[0]["request"].trigger_term, "全技能分析")

    def test_non_independent_free_term_falls_back_to_normal_bug_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark = FakeLarkClient()
            fake_runner = FakeAppServerInvestigationRunner(Path(tmp) / "app_server.html")
            app = BridgeApp(
                self._config(tmp),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                app_server_investigation_runner=fake_runner,
            )

            result = app.handle_event(
                event(
                    event_id="evt_app_free_non_independent",
                    message_id="om_app_free_non_independent",
                    content=f"@bot {self.BUG_URL} 全技能分析一下 调查下 3D 生命周期",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_runner.requests, [])

    def test_file_key_with_auto_trigger_routes_to_app_server_investigation_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark = FakeLarkClient()
            fake_runner = FakeAppServerInvestigationRunner(Path(tmp) / "app_server.html")
            app = BridgeApp(
                self._config(tmp),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                app_server_investigation_runner=fake_runner,
            )

            result = app.handle_event(
                event(
                    event_id="evt_app_file",
                    message_id="om_app_file",
                    content="@bot auto 问题现象：进入 SR 后黑屏 问题时间：2026-05-25 16:50:41 file_abc123",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "app_server_investigation")
        self.assertEqual(len(fake_runner.requests), 1)
        self.assertEqual(fake_bug.requests, [])
        request = fake_runner.requests[0]["request"]
        self.assertEqual([item.value for item in request.resources], ["file_abc123"])
        self.assertEqual(request.trigger_mode, "auto")

    def test_free_term_followup_without_link_inherits_bug_url_from_latest_chat_context(self):
        """续聊：先分析过某 bug，再裸发「自主分析」(无链接/无附件/无回复)，应从本群最近分析上下文恢复 bug_url。"""
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark = FakeLarkClient()
            fake_runner = FakeAppServerInvestigationRunner(Path(tmp) / "app_server.html")
            app = BridgeApp(
                self._config(tmp),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                app_server_investigation_runner=fake_runner,
            )
            # 上一轮：同一个群里分析过 BUG_URL，落下可恢复的对话上下文。
            app.conversation_store.remember(
                root_message_id="om_prior_bug",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text=f"{self.BUG_URL} 调查3D启动时序",
                summary_text="根因是首帧超时",
                report_url="http://127.0.0.1:8765/reports/om_prior_bug/",
                report_excerpt="关键证据：displayChanged 后无后续",
            )

            # 这一轮：裸触发「自主分析」，不带链接、不带附件、不使用飞书回复。
            result = app.handle_event(
                event(
                    event_id="evt_followup_auto",
                    message_id="om_followup_auto",
                    content="@bot 自主分析",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "app_server_investigation")
        self.assertEqual(len(fake_runner.requests), 1)
        self.assertEqual(fake_bug.requests, [])
        request = fake_runner.requests[0]["request"]
        self.assertEqual(request.trigger_term, "自主分析")
        # 核心：缺失的 bug 链接应从本群最近一次分析上下文继承。
        self.assertEqual(request.bug_url, self.BUG_URL)

    def test_reply_to_running_app_server_root_steers_existing_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark = FakeLarkClient()
            fake_runner = FakeAppServerInvestigationRunner(Path(tmp) / "app_server.html")
            app = BridgeApp(
                self._config(tmp),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                app_server_investigation_runner=fake_runner,
            )
            control = FakeRunningAppServerControl()
            app._register_app_server_control("om_app_root", control)

            result = app.handle_event(
                event(
                    event_id="evt_app_steer",
                    message_id="om_app_steer",
                    content="@bot 优先检查 XTheme 输入，不要继续泛查日志",
                    reply_to="om_app_root",
                )
            )

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "app_server_control")
        self.assertEqual(result.details["action"], "steer")
        self.assertEqual(fake_runner.requests, [])
        self.assertEqual(len(control.steers), 1)
        self.assertEqual(control.steers[0]["text"], "优先检查 XTheme 输入，不要继续泛查日志")
        self.assertEqual(control.steers[0]["source_message_id"], "om_app_steer")
        root_session = app.activity_store.get_session("om_app_root")
        self.assertIsNotNone(root_session)
        assert root_session is not None
        stages = [item["stage"] for item in root_session.get("progress", [])]
        self.assertIn("app_server_investigation_steer_received", stages)

    def test_reply_to_running_app_server_root_cancels_existing_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark = FakeLarkClient()
            fake_runner = FakeAppServerInvestigationRunner(Path(tmp) / "app_server.html")
            app = BridgeApp(
                self._config(tmp),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                app_server_investigation_runner=fake_runner,
            )
            control = FakeRunningAppServerControl()
            app._register_app_server_control("om_app_root", control)

            result = app.handle_event(
                event(
                    event_id="evt_app_cancel",
                    message_id="om_app_cancel",
                    content="@bot 停止",
                    reply_to="om_app_root",
                )
            )

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "app_server_control")
        self.assertEqual(result.details["action"], "cancel")
        self.assertEqual(fake_runner.requests, [])
        self.assertEqual(len(control.cancels), 1)
        root_session = app.activity_store.get_session("om_app_root")
        self.assertIsNotNone(root_session)
        assert root_session is not None
        self.assertEqual(root_session["status"], "cancelled")
        self.assertEqual(root_session["error_code"], "cancelled_by_user")
        self.assertIn("停止", root_session["message"])
        stages = [item["stage"] for item in root_session.get("progress", [])]
        self.assertIn("app_server_investigation_cancel_requested", stages)

    def test_reply_after_app_server_cancel_is_not_acknowledged_as_steer(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                self._config(tmp),
                lark_client=fake_lark,
                bug_runner=FakeBugRunner(metadata, html),
                app_server_investigation_runner=FakeAppServerInvestigationRunner(Path(tmp) / "app_server.html"),
            )
            control = CodexAppServerTurnController(session_id="om_app_root")
            control.cancel("用户要求停止", source_message_id="om_cancel", actor_id="ou_1")
            app._register_app_server_control("om_app_root", control)

            result = app.handle_event(
                event(
                    event_id="evt_app_steer_after_cancel",
                    message_id="om_app_steer_after_cancel",
                    content="@bot 补充检查 XTheme",
                    reply_to="om_app_root",
                )
            )

        self.assertTrue(result.skipped)
        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "app_server_control")
        self.assertEqual(result.details["action"], "ignored")
        self.assertEqual(result.details["reason"], "cancelled")
        self.assertFalse(any("并入当前 AI 自主分析" in item["text"] for item in fake_lark.replies))
        self.assertEqual(control.take_action()["kind"], "cancel")
        self.assertIsNone(control.take_action())

    def test_running_app_server_control_does_not_false_cancel_english_steer(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark = FakeLarkClient()
            fake_runner = FakeAppServerInvestigationRunner(Path(tmp) / "app_server.html")
            app = BridgeApp(
                self._config(tmp),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                app_server_investigation_runner=fake_runner,
            )
            control = FakeRunningAppServerControl()
            app._register_app_server_control("om_app_root", control)

            result = app.handle_event(
                event(
                    event_id="evt_app_false_cancel",
                    message_id="om_app_false_cancel",
                    content="@bot stop focusing on broad logs and inspect XTheme",
                    reply_to="om_app_root",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "app_server_control")
        self.assertEqual(result.details["action"], "steer")
        self.assertEqual(len(control.steers), 1)
        self.assertEqual(control.cancels, [])

    def test_app_server_cancel_text_requires_cancel_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(
                self._config(tmp),
                lark_client=FakeLarkClient(),
                bug_runner=FakeBugRunner(Path(tmp) / "analysis.md", Path(tmp) / "analysis.html"),
                app_server_investigation_runner=FakeAppServerInvestigationRunner(Path(tmp) / "app_server.html"),
            )

            self.assertTrue(app._is_app_server_cancel_text("停止"))
            self.assertTrue(app._is_app_server_cancel_text("请停止当前分析"))
            self.assertTrue(app._is_app_server_cancel_text("cancel current job"))
            self.assertFalse(app._is_app_server_cancel_text("stop focusing on broad logs"))
            self.assertFalse(app._is_app_server_cancel_text("check whether the stopwatch is correct"))
            self.assertFalse(app._is_app_server_cancel_text("don't cancel the current job"))


if __name__ == "__main__":
    unittest.main()
