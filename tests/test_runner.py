from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from lark_agent_bridge.models import BridgeConfig
from lark_agent_bridge.runner import SignalChainRunner


class RunnerTests(unittest.TestCase):
    def test_dry_run_returns_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp))
            result = SignalChainRunner(config).run(
                signal="132002",
                log_path="/tmp/logs",
                output_dir=Path(tmp) / "out",
                since="13-14",
            )

        self.assertTrue(result.success)
        self.assertIn("--signal-code", result.command)
        self.assertIn("--since", result.command)

    def test_timeout_is_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), runner_timeout_seconds=1)
            runner = SignalChainRunner(config)
            with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["cmd"], timeout=1)):
                result = runner.run(signal="132002", log_path="/tmp/logs", output_dir=Path(tmp) / "out")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "runner_timeout")


if __name__ == "__main__":
    unittest.main()

