from pathlib import Path
import io
import json
import subprocess
import tempfile
import unittest
from unittest import mock

from lark_agent_bridge.lark_client import CommandResult, EventConsumerError, LarkClient
from lark_agent_bridge.models import BridgeConfig, EventConsumerOptions, LarkEvent, LarkOptions


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
    def test_consume_events_waits_for_ready_marker_and_keeps_stdin_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                event_consumer=EventConsumerOptions(restart_on_failure=False, ready_timeout_seconds=1),
            )
            client = LarkClient(config)
            process = FakeProcess(
                stdout_lines=[
                    json.dumps(
                        {
                            "event_id": "evt_1",
                            "message_id": "om_1",
                            "chat_id": "oc_1",
                            "chat_type": "group",
                            "sender_id": "ou_1",
                            "message_type": "text",
                            "content": "@bot 你是谁",
                        }
                    )
                    + "\n"
                ],
                stderr_lines=[
                    "[event] ready event_key=im.message.receive_v1\n",
                    "[event] exited — received 1 event(s) in 0.1s (reason: signal)\n",
                ],
                returncode=0,
            )
            statuses = []

            with mock.patch("lark_agent_bridge.lark_client.subprocess.Popen", return_value=process) as popen:
                events = list(client.consume_events(status_callback=statuses.append))

        self.assertEqual(events[0].event_id, "evt_1")
        popen.assert_called_once()
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(statuses[0]["stage"], "event_consumer_starting")
        self.assertIn("event_consumer_ready", [item["stage"] for item in statuses])

    def test_consume_payloads_preserves_card_action_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                event_consumer=EventConsumerOptions(restart_on_failure=False, ready_timeout_seconds=1),
            )
            client = LarkClient(config)
            payload = {
                "header": {"event_id": "evt_card"},
                "event": {
                    "context": {"open_message_id": "om_card", "open_chat_id": "oc_1"},
                    "action": {"value": {"action": "approve", "request_id": "apr_1"}},
                },
            }
            process = FakeProcess(
                stdout_lines=[json.dumps(payload) + "\n"],
                stderr_lines=[
                    "[event] ready event_key=card.action.trigger\n",
                    "[event] exited — received 1 event(s) in 0.1s (reason: signal)\n",
                ],
                returncode=0,
            )

            with mock.patch("lark_agent_bridge.lark_client.subprocess.Popen", return_value=process):
                payloads = list(client.consume_payloads())

        self.assertEqual(payloads, [payload])

    def test_consume_events_raises_when_ready_marker_never_arrives(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                event_consumer=EventConsumerOptions(
                    restart_on_failure=False,
                    ready_timeout_seconds=0.01,
                ),
            )
            client = LarkClient(config)
            process = FakeProcess(
                stdout_lines=[],
                stderr_lines=["Error: missing event permission\n"],
                returncode=2,
            )
            statuses = []

            with mock.patch("lark_agent_bridge.lark_client.subprocess.Popen", return_value=process):
                with self.assertRaises(EventConsumerError):
                    list(client.consume_events(status_callback=statuses.append))

        self.assertIn("event_consumer_startup_failed", [item["stage"] for item in statuses])

    def test_consume_events_restarts_after_unexpected_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                event_consumer=EventConsumerOptions(
                    restart_on_failure=True,
                    max_restarts=1,
                    restart_initial_delay_seconds=0,
                    restart_max_delay_seconds=0,
                    ready_timeout_seconds=1,
                ),
            )
            client = LarkClient(config)
            failed = FakeProcess(
                stdout_lines=[],
                stderr_lines=[
                    "[event] ready event_key=im.message.receive_v1\n",
                    "Error: bus crashed\n",
                ],
                returncode=1,
            )
            recovered = FakeProcess(
                stdout_lines=[
                    json.dumps(
                        {
                            "event_id": "evt_after_restart",
                            "message_id": "om_2",
                            "chat_id": "oc_1",
                            "chat_type": "group",
                            "sender_id": "ou_1",
                            "message_type": "text",
                            "content": "@bot 你是谁",
                        }
                    )
                    + "\n"
                ],
                stderr_lines=[
                    "[event] ready event_key=im.message.receive_v1\n",
                    "[event] exited — received 1 event(s) in 0.1s (reason: signal)\n",
                ],
                returncode=0,
            )
            statuses = []

            with mock.patch(
                "lark_agent_bridge.lark_client.subprocess.Popen",
                side_effect=[failed, recovered],
            ) as popen:
                events = list(client.consume_events(status_callback=statuses.append))

        self.assertEqual([event.event_id for event in events], ["evt_after_restart"])
        self.assertEqual(popen.call_count, 2)
        self.assertIn("event_consumer_restarting", [item["stage"] for item in statuses])

    def test_reply_uses_message_reply_and_thread_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = LarkClient(
                BridgeConfig(
                    dry_run=True,
                    data_dir=Path(tmp),
                    lark=LarkOptions(reply_in_thread=True),
                )
            )

            result = client.reply("om_1", "处理完成")

        self.assertEqual(
            result.command,
            [
                "lark-cli",
                "im",
                "+messages-reply",
                "--as",
                "bot",
                "--message-id",
                "om_1",
                "--text",
                "处理完成",
                "--reply-in-thread",
            ],
        )

    def test_update_card_uses_generic_patch_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = LarkClient(BridgeConfig(dry_run=True, data_dir=Path(tmp)))

            result = client.update_card("om_card_1", '{"config":{}}')

        self.assertEqual(
            result.command,
            [
                "lark-cli",
                "api",
                "PATCH",
                "/open-apis/im/v1/messages/om_card_1",
                "--as",
                "bot",
                "--data",
                '{"msg_type":"interactive","content":"{\\"config\\":{}}"}',
            ],
        )

    def test_reply_card_uses_interactive_content_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = LarkClient(
                BridgeConfig(
                    dry_run=True,
                    data_dir=Path(tmp),
                    lark=LarkOptions(reply_in_thread=True),
                )
            )

            result = client.reply_card("om_1", '{"config":{}}')

        self.assertEqual(
            result.command,
            [
                "lark-cli",
                "im",
                "+messages-reply",
                "--as",
                "bot",
                "--message-id",
                "om_1",
                "--msg-type",
                "interactive",
                "--content",
                '{"config":{}}',
                "--reply-in-thread",
            ],
        )

    def test_send_card_response_uses_interactive_content_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = LarkClient(BridgeConfig(dry_run=True, data_dir=Path(tmp)))

            result = client.send_card_response(event(), '{"config":{}}')

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
                "--msg-type",
                "interactive",
                "--content",
                '{"config":{}}',
            ],
        )

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

    def test_download_resource_uses_relative_output_from_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = LarkClient(BridgeConfig(dry_run=False, data_dir=Path(tmp)))
            target = Path(tmp) / "jobs" / "job1" / "input" / "file_v3_0011s_abc.zip"
            target.parent.mkdir(parents=True)

            with mock.patch.object(
                client,
                "_run",
                return_value=CommandResult(command=[], returncode=0),
            ) as mocked_run:
                client.download_resource(
                    message_id="om_file_msg",
                    file_key="file_v3_0011s_abc",
                    resource_type="file",
                    output=target,
                )

        mocked_run.assert_called_once()
        command = mocked_run.call_args.args[0]
        cwd = mocked_run.call_args.kwargs["cwd"]
        self.assertEqual(
            command,
            [
                "lark-cli",
                "im",
                "+messages-resources-download",
                "--as",
                "bot",
                "--message-id",
                "om_file_msg",
                "--file-key",
                "file_v3_0011s_abc",
                "--type",
                "file",
                "--output",
                "./file_v3_0011s_abc.zip",
            ],
        )
        self.assertEqual(Path(cwd).resolve(), target.parent.resolve())

    def test_download_drive_folder_uses_relative_local_dir_from_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = LarkClient(BridgeConfig(dry_run=False, data_dir=Path(tmp)))
            target_dir = Path(tmp) / "jobs" / "job1" / "input" / "fldcnlog123"
            target_dir.parent.mkdir(parents=True)

            with mock.patch.object(
                client,
                "_run",
                return_value=CommandResult(command=[], returncode=0),
            ) as mocked_run:
                client.download_drive_folder(folder_token="fldcnlog123", output_dir=target_dir)

        mocked_run.assert_called_once()
        command = mocked_run.call_args.args[0]
        cwd = mocked_run.call_args.kwargs["cwd"]
        self.assertEqual(
            command,
            [
                "lark-cli",
                "drive",
                "+pull",
                "--as",
                "bot",
                "--folder-token",
                "fldcnlog123",
                "--local-dir",
                "./fldcnlog123",
                "--if-exists",
                "smart",
                "--on-duplicate-remote",
                "rename",
            ],
        )
        self.assertEqual(Path(cwd).resolve(), target_dir.parent.resolve())


class FakeProcess:
    def __init__(self, *, stdout_lines: list[str], stderr_lines: list[str], returncode: int) -> None:
        self.stdout = io.StringIO("".join(stdout_lines))
        self.stderr = io.StringIO("".join(stderr_lines))
        self.stdin = io.StringIO()
        self.pid = 12345
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True


if __name__ == "__main__":
    unittest.main()
