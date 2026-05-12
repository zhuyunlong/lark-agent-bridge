from pathlib import Path
import tempfile
import unittest

from lark_agent_bridge.downloader import LogDownloader
from lark_agent_bridge.handlers.signal_lifecycle import SignalLifecycleHandler
from lark_agent_bridge.lark_client import LarkClient
from lark_agent_bridge.models import BridgeConfig, DownloadResource, LarkEvent, SignalRequest
from lark_agent_bridge.runner import SignalChainRunner


class SignalHandlerTests(unittest.TestCase):
    def _handler(self, tmp):
        config = BridgeConfig(dry_run=True, data_dir=Path(tmp))
        return SignalLifecycleHandler(config, LogDownloader(config, LarkClient(config)), SignalChainRunner(config))

    def test_dry_run_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = self._handler(tmp)
            event = LarkEvent(
                event_id="evt_1",
                message_id="om_1",
                chat_id="oc_1",
                chat_type="group",
                sender_id="ou_1",
                message_type="text",
                content="/signal 132002 https://example.com/log.zip",
            )
            request = SignalRequest(
                signal="132002",
                resources=[DownloadResource(kind="url", value="https://example.com/log.zip")],
                triggered=True,
            )

            result = handler.handle(request, event=event)

        self.assertTrue(result.success)
        self.assertIn("dry-run", result.message)
        self.assertIn("analyze_signal_chain.py", " ".join(result.command))

    def test_missing_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._handler(tmp).handle(SignalRequest(signal=None, triggered=True))

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_signal")

    def test_missing_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._handler(tmp).handle(SignalRequest(signal="132002", triggered=True))

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_log")


if __name__ == "__main__":
    unittest.main()

