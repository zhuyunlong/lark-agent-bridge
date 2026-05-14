import io
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from lark_agent_bridge.cli import main


class CliTests(unittest.TestCase):
    def test_run_signal_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(f'dry_run = true\ndata_dir = "{tmp}/data"\n', encoding="utf-8")
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
            config.write_text(f'dry_run = true\ndata_dir = "{tmp}/data"\n', encoding="utf-8")
            event.write_text(
                """
{
  "event_id": "evt_cli",
  "chat_id": "oc_1",
  "chat_type": "group",
  "message_id": "om_1",
  "sender_id": "ou_1",
  "message_type": "text",
  "content": "@bot /signal 132002 https://example.com/log.zip"
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
            config.write_text(f'dry_run = true\ndata_dir = "{tmp}/data"\n', encoding="utf-8")
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

    def test_handle_unsupported_event_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            event = Path(tmp) / "event.json"
            config.write_text(f'dry_run = true\ndata_dir = "{tmp}/data"\n', encoding="utf-8")
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

    def test_listen_purges_jobs_on_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(f'dry_run = false\ndata_dir = "{tmp}/data"\n', encoding="utf-8")
            fake_app = mock.Mock()
            fake_app.lark_client.consume_events.return_value = iter(())

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
                fake_app.lark_client.consume_events.return_value = iter(())
                bridge_app.return_value = fake_app
                exit_code = main(["listen", "--config", str(config)])

        self.assertEqual(exit_code, 0)
        self.assertTrue(callable(bridge_app.call_args.kwargs["progress_callback"]))

    def test_listen_starts_and_stops_report_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(f'dry_run = false\ndata_dir = "{tmp}/data"\n', encoding="utf-8")
            fake_app = mock.Mock()
            fake_app.lark_client.consume_events.return_value = iter(())

            with mock.patch("lark_agent_bridge.cli.BridgeApp", return_value=fake_app):
                exit_code = main(["listen", "--config", str(config)])

        self.assertEqual(exit_code, 0)
        fake_app.start_report_server.assert_called_once()
        fake_app.stop_report_server.assert_called_once()


if __name__ == "__main__":
    unittest.main()
