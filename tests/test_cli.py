import io
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from lark_agent_bridge.cli import main


class CliTests(unittest.TestCase):
    def test_check_disables_codegraph_warmup(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(f'dry_run = true\ndata_dir = "{tmp}/data"\n', encoding="utf-8")
            fake_app = mock.Mock()
            fake_app.check.return_value = {"ok": True}

            with (
                redirect_stdout(io.StringIO()),
                mock.patch("lark_agent_bridge.cli.BridgeApp", return_value=fake_app) as bridge_app,
            ):
                exit_code = main(["check", "--config", str(config), "--dry-run"])

        self.assertEqual(exit_code, 0)
        self.assertIs(bridge_app.call_args.kwargs["warmup_codegraph"], False)

    def test_run_signal_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(
                f'dry_run = true\ndata_dir = "{tmp}/data"\n\n[lark]\nbot_name = "bot"\n',
                encoding="utf-8",
            )
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = main(
                    [
                        "run-signal",
                        "--config",
                        str(config),
                        "--signal",
                        "132002",
                        "--log-path",
                        "/tmp/logs",
                        "--dry-run",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertIn("analyze_signal_chain.py", output.getvalue())

    def test_handle_event_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            event = Path(tmp) / "event.json"
            config.write_text(
                f'dry_run = true\ndata_dir = "{tmp}/data"\n\n[lark]\nbot_name = "bot"\n',
                encoding="utf-8",
            )
            event.write_text(
                """
{
  "event_id": "evt_cli",
  "chat_id": "oc_1",
  "chat_type": "group",
  "message_id": "om_1",
  "sender_id": "ou_1",
  "message_type": "text",
  "content": "@bot 调查 132002 信号链路 https://example.com/log.zip"
}
""",
                encoding="utf-8",
            )
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = main(["handle-event", "--config", str(config), "--event", str(event), "--dry-run"])

        self.assertEqual(exit_code, 0)
        self.assertIn("dry-run", output.getvalue())

    def test_handle_basic_chat_event_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            event = Path(tmp) / "event.json"
            config.write_text(
                f'dry_run = true\ndata_dir = "{tmp}/data"\n\n[lark]\nbot_name = "bot"\n',
                encoding="utf-8",
            )
            event.write_text(
                """
{
  "event_id": "evt_chat",
  "chat_id": "oc_1",
  "chat_type": "group",
  "message_id": "om_1",
  "sender_id": "ou_1",
  "message_type": "text",
  "content": "@bot 你是谁"
}
""",
                encoding="utf-8",
            )
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = main(["handle-event", "--config", str(config), "--event", str(event), "--dry-run"])

        self.assertEqual(exit_code, 0)
        self.assertIn("Lark Agent Bridge", output.getvalue())

    def test_knowledge_cli_sync_and_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adb_json = root / "adb_data.json"
            adb_json.write_text(
                """
{"commands":[{"name":"打开Debug面板","command":"adb shell am start -a com.xiaopeng.intent.action.DEV_BOARD","group":"开发调试"}]}
""",
                encoding="utf-8",
            )
            config = root / "config.toml"
            config.write_text(
                f"""
data_dir = "{root}/data"

[knowledge]
enabled = true
storage = "{root}/data/knowledge.sqlite"

[[knowledge.sources]]
id = "adb"
type = "local_json"
path = "{adb_json}"
""",
                encoding="utf-8",
            )
            sync_output = io.StringIO()
            answer_output = io.StringIO()

            with redirect_stdout(sync_output):
                sync_code = main(["knowledge", "sync", "--config", str(config)])
            with redirect_stdout(answer_output):
                answer_code = main(["knowledge", "answer", "--config", str(config), "怎么打开Debug面板"])

        self.assertEqual(sync_code, 0)
        self.assertEqual(answer_code, 0)
        self.assertIn('"total_chunks": 1', sync_output.getvalue())
        self.assertIn("打开Debug面板", answer_output.getvalue())

    def test_knowledge_sync_avoids_bridge_app_and_disables_codegraph_warmup(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(
                f"""
data_dir = "{tmp}/data"

[knowledge]
enabled = true
storage = "{tmp}/data/knowledge.sqlite"
""",
                encoding="utf-8",
            )
            fake_service = mock.Mock()
            fake_service.sync_all.return_value = {"source_count": 0, "total_chunks": 0, "sources": []}

            with (
                redirect_stdout(io.StringIO()),
                mock.patch("lark_agent_bridge.cli.BridgeApp") as bridge_app,
                mock.patch("lark_agent_bridge.cli.KnowledgeService", return_value=fake_service) as knowledge_service,
            ):
                exit_code = main(["knowledge", "sync", "--config", str(config)])

        self.assertEqual(exit_code, 0)
        bridge_app.assert_not_called()
        self.assertIs(knowledge_service.call_args.kwargs["warmup_codegraph"], False)

    def test_handle_unsupported_event_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            event = Path(tmp) / "event.json"
            config.write_text(
                f'dry_run = true\ndata_dir = "{tmp}/data"\n\n[lark]\nbot_name = "bot"\n',
                encoding="utf-8",
            )
            event.write_text(
                """
{
  "event_id": "evt_unknown",
  "chat_id": "oc_1",
  "chat_type": "group",
  "message_id": "om_1",
  "sender_id": "ou_1",
  "message_type": "text",
  "content": "@bot 这是一个未支持的普通请求"
}
""",
                encoding="utf-8",
            )
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = main(["handle-event", "--config", str(config), "--event", str(event), "--dry-run"])

        self.assertEqual(exit_code, 0)
        self.assertIn('"message": "not a handled request"', output.getvalue())
        self.assertIn('"skipped": true', output.getvalue())

    def test_listen_preserves_jobs_on_start_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(f'dry_run = false\ndata_dir = "{tmp}/data"\n', encoding="utf-8")
            fake_app = mock.Mock()
            fake_app.lark_client.consume_payloads.return_value = iter(())

            with mock.patch("lark_agent_bridge.cli.BridgeApp", return_value=fake_app):
                exit_code = main(["listen", "--config", str(config)])

        self.assertEqual(exit_code, 0)
        fake_app.purge_all_jobs.assert_not_called()

    def test_cli_warns_when_direct_api_preset_has_no_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(
                f"""
dry_run = true
data_dir = "{tmp}/data"

[ai_provider]
enabled = true
preset = "test-direct"

[provider_presets.test-direct]
type = "direct_api"
api_format = "anthropic"
base_url = "https://example.invalid"
primary_model = "test-model"
requires_api_key = true
""",
                encoding="utf-8",
            )

            env_keys = [
                "LARK_AGENT_BRIDGE_AI_API_KEY",
                "LARK_AGENT_BRIDGE_AI_FALLBACK_API_KEY",
                "LARK_AGENT_BRIDGE_AI_BASE_URL",
                "LARK_AGENT_BRIDGE_AI_FALLBACK_BASE_URL",
            ]
            with (
                mock.patch.dict(os.environ, {key: "" for key in env_keys}),
                self.assertLogs("bridge.cli", level="WARNING") as logs,
            ):
                exit_code = main(["listen", "--config", str(config), "--dry-run"])

        self.assertEqual(exit_code, 0)
        self.assertIn("requires a direct API key", "\n".join(logs.output))
        self.assertIn("LARK_AGENT_BRIDGE_AI_API_KEY", "\n".join(logs.output))
        self.assertIn("[ai_provider].api_key", "\n".join(logs.output))

    def test_listen_purges_jobs_on_start_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(
                f"""
dry_run = false
data_dir = "{tmp}/data"

[job_retention]
purge_all_on_listen_start = true
""",
                encoding="utf-8",
            )
            fake_app = mock.Mock()
            fake_app.lark_client.consume_payloads.return_value = iter(())

            with mock.patch("lark_agent_bridge.cli.BridgeApp", return_value=fake_app):
                exit_code = main(["listen", "--config", str(config)])

        self.assertEqual(exit_code, 0)
        fake_app.purge_all_jobs.assert_called_once()

    def test_listen_passes_progress_callback_to_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(f'dry_run = false\ndata_dir = "{tmp}/data"\n', encoding="utf-8")

            with mock.patch("lark_agent_bridge.cli.BridgeApp") as bridge_app:
                fake_app = mock.Mock()
                fake_app.lark_client.consume_payloads.return_value = iter(())
                bridge_app.return_value = fake_app
                exit_code = main(["listen", "--config", str(config)])

        self.assertEqual(exit_code, 0)
        self.assertTrue(callable(bridge_app.call_args.kwargs["progress_callback"]))

    def test_listen_starts_and_stops_report_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(f'dry_run = false\ndata_dir = "{tmp}/data"\n', encoding="utf-8")
            fake_app = mock.Mock()
            fake_app.lark_client.consume_payloads.return_value = iter(())

            with mock.patch("lark_agent_bridge.cli.BridgeApp", return_value=fake_app):
                exit_code = main(["listen", "--config", str(config)])

        self.assertEqual(exit_code, 0)
        fake_app.start_report_server.assert_called_once()
        fake_app.stop_report_server.assert_called_once()

    def test_listen_records_event_consumer_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(f'dry_run = false\ndata_dir = "{tmp}/data"\n', encoding="utf-8")
            fake_app = mock.Mock()

            def consume_payloads(*, status_callback=None):
                assert status_callback is not None
                status_callback({"stage": "event_consumer_ready", "event_key": "im.message.receive_v1"})
                return iter(())

            fake_app.lark_client.consume_payloads.side_effect = consume_payloads

            with mock.patch("lark_agent_bridge.cli.BridgeApp", return_value=fake_app):
                exit_code = main(["listen", "--config", str(config)])

        self.assertEqual(exit_code, 0)
        fake_app.record_daemon_status.assert_called_once()
        self.assertEqual(fake_app.record_daemon_status.call_args.args[0]["stage"], "event_consumer_ready")


if __name__ == "__main__":
    unittest.main()
