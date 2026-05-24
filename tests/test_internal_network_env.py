import importlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from lark_agent_bridge.agents import Addr2LineRunner, RomVersionLookupRunner
from lark_agent_bridge.models import (
    Addr2LineRequest,
    BridgeConfig,
    InternalNetworkEnvOptions,
    RomVersionLookupRequest,
)


addr2line_runner_module = importlib.import_module("lark_agent_bridge.agents.addr2line_runner")
rom_version_runner_module = importlib.import_module("lark_agent_bridge.agents.rom_version_runner")


class InternalNetworkEnvTests(unittest.TestCase):
    def test_rom_version_runner_uses_configured_internal_network_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = RomVersionLookupRunner(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    internal_network_env=InternalNetworkEnvOptions(
                        inherit_env=["PATH", "HOME"],
                        unset_env=["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"],
                    ),
                )
            )
            script = Path(tmp) / "lookup_rom_version.py"
            script.write_text("# stub\n", encoding="utf-8")
            captured: dict[str, object] = {}

            def fake_run(command, **kwargs):
                captured["command"] = command
                captured["env"] = kwargs.get("env")
                return subprocess.CompletedProcess(command, 0, "{}", "")

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "PATH": "/usr/bin:/bin",
                        "HOME": "/Users/tester",
                        "HTTP_PROXY": "http://127.0.0.1:7897",
                        "HTTPS_PROXY": "http://127.0.0.1:7897",
                        "NO_PROXY": "localhost",
                        "SHOULD_DROP": "1",
                    },
                    clear=True,
                ),
                mock.patch.object(runner, "_script_path", return_value=script),
                mock.patch.object(rom_version_runner_module, "run_tracked_process", side_effect=fake_run),
            ):
                result = runner.run_lookup(
                    RomVersionLookupRequest(
                        rom_version="XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
                        prompt="查导航版本",
                        raw_text="ROM版本号XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release 查导航版本",
                        triggered=True,
                    )
                )

        self.assertTrue(result.success)
        env = captured["env"]
        self.assertIsInstance(env, dict)
        self.assertEqual(env.get("PATH"), "/usr/bin:/bin")
        self.assertEqual(env.get("HOME"), "/Users/tester")
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertNotIn("NO_PROXY", env)
        self.assertNotIn("SHOULD_DROP", env)

    def test_addr2line_runner_uses_apk_version_and_configured_internal_network_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Addr2LineRunner(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    internal_network_env=InternalNetworkEnvOptions(
                        inherit_env=["PATH"],
                        unset_env=["HTTP_PROXY", "HTTPS_PROXY"],
                    ),
                )
            )
            script = Path(tmp) / "addr2line_resolve.py"
            script.write_text("# stub\n", encoding="utf-8")
            captured: dict[str, object] = {}

            def fake_run(command, **kwargs):
                captured["command"] = command
                captured["env"] = kwargs.get("env")
                return subprocess.CompletedProcess(command, 0, '{"results": []}', "")

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "PATH": "/usr/bin:/bin",
                        "HTTP_PROXY": "http://127.0.0.1:7897",
                        "HTTPS_PROXY": "http://127.0.0.1:7897",
                    },
                    clear=True,
                ),
                mock.patch.object(runner, "_script_path", return_value=script),
                mock.patch.object(addr2line_runner_module, "run_tracked_process", side_effect=fake_run),
            ):
                result = runner.run_resolve(
                    Addr2LineRequest(
                        addr_text="#05 pc 0000000000f385e4 /system/app/xp_envirodrive/lib/arm64/libunity.so",
                        raw_text="ROM版本号XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release 反解导航符号表",
                        rom_version="XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
                        apk_version="V6.1.0_20260327175820_Release",
                        prompt="反解导航符号表",
                        triggered=True,
                    )
                )

        self.assertTrue(result.success)
        self.assertEqual(result.details["symbol_version"], "V6.1.0_20260327175820_Release")
        command = captured["command"]
        self.assertIn("--apk", command)
        self.assertNotIn("--rom", command)
        env = captured["env"]
        self.assertIsInstance(env, dict)
        self.assertEqual(env.get("PATH"), "/usr/bin:/bin")
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("HTTPS_PROXY", env)


if __name__ == "__main__":
    unittest.main()
