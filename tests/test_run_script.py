from pathlib import Path
import unittest
import tomllib


ROOT = Path(__file__).resolve().parents[1]
RUN_SCRIPT = ROOT / "run.sh"
PRESETS = ROOT / "config" / "presets.toml"


class RunScriptTests(unittest.TestCase):
    def test_run_sh_prefers_project_venv_python(self):
        content = RUN_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('pwd -P', content)
        self.assertIn('VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"', content)
        self.assertIn('[ -x "$VENV_PYTHON" ]', content)
        self.assertIn('PYTHON_BIN="$VENV_PYTHON"', content)
        self.assertIn('PYTHON_BIN="python3"', content)
        self.assertIn('exec "$PYTHON_BIN" -m lark_agent_bridge listen --config "$CONFIG"', content)

    def test_profiles_are_centralized_in_presets_toml(self):
        content = RUN_SCRIPT.read_text(encoding="utf-8")
        registry = tomllib.loads(PRESETS.read_text(encoding="utf-8"))
        profiles = {name for name in registry if name != "defaults"}
        expected = {
            "cc-switch-deepseek-claude",
            "cc-switch-mimo-claude",
            "cc-switch-yybb-claude",
            "cc-switch-yybb-codex",
            "cc-switch-scihub-claude",
            "deepseek-claude",
            "mimo-claude",
            "yybb-claude",
            "yybb-codex",
            "panda-codex",
            "codex-offi",
            "claude-offi",
        }

        self.assertEqual(profiles, expected)
        self.assertNotIn("API" + "_PRE" + "SETS=", content)
        self.assertNotIn("DIRECT" + "_API" + "_PRE" + "SETS=", content)
        self.assertNotIn("OFFICIAL" + "_PROFILES=", content)
        self.assertIn('PRESETS="$PROJECT_DIR/config/presets.toml"', content)
        self.assertIn("lark_agent_bridge.profile_registry", content)

    def test_run_sh_warns_when_direct_api_key_is_missing(self):
        content = RUN_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('PROFILE_REQUIRES_API_KEY', content)
        self.assertIn("requires a direct API key", content)
        self.assertIn("LARK_AGENT_BRIDGE_AI_API_KEY", content)
        self.assertIn("[ai_provider].api_key", content)
        self.assertIn("unset LARK_AGENT_BRIDGE_AI_API_KEY", content)
        self.assertIn("launchctl unsetenv LARK_AGENT_BRIDGE_AI_API_KEY", content)

    def test_run_sh_uses_only_config_toml_with_preset_override(self):
        content = RUN_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('CONFIG="$PROJECT_DIR/config.toml"', content)
        self.assertIn("eval \"$PROFILE_ENV\"", content)
        self.assertNotIn("config.$PROFILE.toml", content)
        self.assertNotIn("config." + "cc-switch", content)
        self.assertNotIn("config." + "openai", content)
        self.assertNotIn("config." + "claude", content)

    def test_run_openai_entrypoint_is_removed(self):
        removed_entry = "run-" + "openai.sh"
        self.assertFalse((ROOT / removed_entry).exists())

    def test_repository_keeps_one_runtime_config_and_centralized_templates(self):
        runtime_configs = {path.name for path in ROOT.glob("config*.toml")}
        self.assertLessEqual(runtime_configs, {"config.toml"})

        example = ROOT / "config" / "config.example.toml"
        self.assertTrue(example.exists())
        content = example.read_text(encoding="utf-8")

        self.assertIn('preset = "cc-switch-deepseek-claude"', content)
        self.assertIn('api_key = ""', content)
        self.assertNotIn("ou_", content)
        self.assertNotIn("oc_", content)
        self.assertNotIn("https://", content)
        self.assertIn("/config.*.toml", (ROOT / ".gitignore").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
