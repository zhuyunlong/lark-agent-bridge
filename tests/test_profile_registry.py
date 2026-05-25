import io
import unittest
from contextlib import redirect_stderr

from lark_agent_bridge.profile_registry import load_profile_specs, main, resolve_profile, shell_exports


class ProfileRegistryTests(unittest.TestCase):
    def test_default_profile_and_shell_exports_come_from_presets_toml(self):
        name, spec = resolve_profile("default")

        self.assertEqual(name, "cc-switch-deepseek-claude")
        self.assertEqual(spec["agent_provider"], "claude")
        self.assertFalse(spec["requires_api_key"])

        exports = shell_exports("default")
        self.assertIn("PROFILE=cc-switch-deepseek-claude", exports)
        self.assertIn("export LARK_AGENT_BRIDGE_AI_PRESET=cc-switch-deepseek-claude", exports)
        self.assertIn("export LARK_AGENT_BRIDGE_AGENT_PROVIDER=claude", exports)

    def test_registry_marks_direct_api_and_official_profiles(self):
        profiles = load_profile_specs()

        self.assertTrue(profiles["mimo-claude"]["requires_api_key"])
        self.assertEqual(profiles["mimo-claude"]["type"], "direct-api")
        self.assertEqual(profiles["mimo-claude"]["agent_provider"], "claude")
        self.assertEqual(profiles["yybb-codex"]["agent_provider"], "codex")
        self.assertFalse(profiles["codex-offi"]["ai_enabled"])

        official_exports = shell_exports("codex-offi")
        self.assertIn("export LARK_AGENT_BRIDGE_AI_ENABLED=false", official_exports)
        self.assertIn("unset LARK_AGENT_BRIDGE_AI_PRESET", official_exports)
        self.assertIn("export LARK_AGENT_BRIDGE_AGENT_PROVIDER=codex", official_exports)

    def test_unknown_profile_reports_supported_profiles_to_stderr(self):
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            exit_code = main(["missing-profile", "--shell"])

        self.assertEqual(exit_code, 2)
        self.assertIn("Unsupported profile: missing-profile", stderr.getvalue())
        self.assertIn("cc-switch-deepseek-claude", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
