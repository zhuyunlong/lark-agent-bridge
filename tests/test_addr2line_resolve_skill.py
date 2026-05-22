from pathlib import Path
import importlib.util
import unittest
from unittest import mock


SCRIPT_PATH = Path("/Users/zhuyl/Documents/workspace/.ai/skills/addr2line-resolve/scripts/addr2line_resolve.py")


def load_addr2line_resolve_module():
    spec = importlib.util.spec_from_file_location("addr2line_resolve_under_test", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Addr2LineResolveSkillTests(unittest.TestCase):
    def test_rom_resolution_falls_back_to_envirodrive_so_by_rom_date(self):
        module = load_addr2line_resolve_module()
        calls = []

        def fake_search(group, name, version_prefix, timeout=15):
            calls.append((group, name, version_prefix))
            if group == "com.xiaopeng.apk" and name == "envirodrive-mainland":
                return []
            if (
                group == "com.xiaopeng.lib"
                and name == "envirodrive_so"
                and version_prefix == "*20260327*"
            ):
                return [
                    {"version": "V6.1.0_20260327172000_Release", "assets": []},
                    {"version": "V6.1.0_20260327175820_Release", "assets": []},
                ]
            return []

        with mock.patch.object(module, "search_nexus_versions", side_effect=fake_search):
            apk_version, so_artifact = module.resolve_apk_version(
                "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
                None,
            )

        self.assertEqual(apk_version, "V6.1.0_20260327175820_Release")
        self.assertEqual(so_artifact, "envirodrive_so")
        self.assertIn(("com.xiaopeng.apk", "envirodrive-mainland", "V6.1.0"), calls)
        self.assertIn(("com.xiaopeng.lib", "envirodrive_so", "*20260327*"), calls)


if __name__ == "__main__":
    unittest.main()
