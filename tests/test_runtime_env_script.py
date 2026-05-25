from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-runtime-env.sh"


class RuntimeEnvScriptTests(unittest.TestCase):
    def test_script_exists_and_is_executable(self):
        self.assertTrue(SCRIPT.exists(), "scripts/check-runtime-env.sh should exist")
        self.assertTrue(SCRIPT.stat().st_mode & 0o111, "runtime check script should be executable")

    def test_script_covers_proxy_and_secret_safe_checks(self):
        content = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("current_shell_proxy", content)
        self.assertIn("proxy_on", content)
        self.assertIn("event_bus_proxy", content)
        self.assertIn("bridge_proxy", content)
        self.assertIn("lark-cli event status --json", content)
        self.assertIn("meegle auth status", content)
        self.assertIn("LARK_AGENT_BRIDGE_AI_API_KEY", content)
        self.assertIn("sha256", content)
        self.assertNotIn("echo $LARK_AGENT_BRIDGE_AI_API_KEY", content)

    def test_script_supports_optional_ai_probe(self):
        content = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("--probe-ai", content)
        self.assertIn("load_config", content)
        self.assertIn("api_format", content)
        self.assertIn("base_url", content)
        self.assertIn("fast_model", content)
        self.assertNotIn('url = "https://token-plan-cn.xiaomimimo.com/anthropic/v1/messages"', content)


if __name__ == "__main__":
    unittest.main()
