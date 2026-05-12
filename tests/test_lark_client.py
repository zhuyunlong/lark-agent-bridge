from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lark_agent_bridge.lark_client import LarkClient
from lark_agent_bridge.models import BridgeConfig, LarkEvent, LarkOptions


def event(**overrides):
    values = {
        "event_id": "evt_1",
        "message_id": "om_1",
        "chat_id": "oc_1",
        "chat_type": "group",
        "sender_id": "ou_1",
        "message_type": "text",
        "content": "hello",
    }
    values.update(overrides)
    return LarkEvent(**values)


class LarkClientTests(unittest.TestCase):
    def test_group_response_uses_send_and_mentions_sender(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = LarkClient(
                BridgeConfig(
                    dry_run=True,
                    data_dir=Path(tmp),
                    lark=LarkOptions(reply_in_thread=False, mention_sender_in_group=True),
                )
            )

            result = client.send_response(event(), "处理完成")

        self.assertEqual(
            result.command,
            [
                "lark-cli",
                "im",
                "+messages-send",
                "--as",
                "bot",
                "--chat-id",
                "oc_1",
                "--text",
                '<at user_id="ou_1"></at> 处理完成',
            ],
        )

    def test_p2p_response_uses_user_send_without_mention(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = LarkClient(BridgeConfig(dry_run=True, data_dir=Path(tmp)))

            result = client.send_response(event(chat_type="p2p", chat_id="ou_chat_1"), "你好")

        self.assertEqual(
            result.command,
            [
                "lark-cli",
                "im",
                "+messages-send",
                "--as",
                "bot",
                "--user-id",
                "ou_1",
                "--text",
                "你好",
            ],
        )

    def test_send_file_uses_relative_path_from_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = LarkClient(BridgeConfig(dry_run=False, data_dir=Path(tmp)))
            html_path = Path(tmp) / "reports" / "bug_3d_startup_report.html"
            html_path.parent.mkdir(parents=True)
            html_path.write_text("<html></html>", encoding="utf-8")

            with mock.patch.object(client, "_run", return_value=__import__("lark_agent_bridge.lark_client", fromlist=["CommandResult"]).CommandResult(command=[], returncode=0)) as mocked_run:
                client.send_file_response(event(), html_path)

        mocked_run.assert_called_once()
        command = mocked_run.call_args.args[0]
        cwd = mocked_run.call_args.kwargs["cwd"]
        self.assertEqual(
            command,
            [
                "lark-cli",
                "im",
                "+messages-send",
                "--as",
                "bot",
                "--chat-id",
                "oc_1",
                "--file",
                "./bug_3d_startup_report.html",
            ],
        )
        self.assertEqual(Path(cwd).resolve(), html_path.parent.resolve())


if __name__ == "__main__":
    unittest.main()
