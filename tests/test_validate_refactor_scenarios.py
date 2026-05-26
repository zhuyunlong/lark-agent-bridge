from pathlib import Path
import json
import tempfile
import unittest

from lark_agent_bridge.models import BridgeConfig
import scripts.validate_refactor_scenarios as scenarios


class ValidateRefactorScenariosTests(unittest.TestCase):
    def _config_with_ld_skill(self, tmp: str, route: dict[str, object]) -> BridgeConfig:
        root = Path(tmp) / "workspace"
        data_dir = Path(tmp) / "data"
        skill_dir = root / ".ai" / "skills" / "ld-lane-level-log-analysis-portable"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: LD Lane Level Log Analysis\n"
            "description: 分析车道级、退无图和 LD 状态日志。\n"
            "---\n\n"
            "# LD Lane\n",
            encoding="utf-8",
        )
        route_file = data_dir / "state" / "skill_routes.json"
        route_file.parent.mkdir(parents=True)
        route_file.write_text(
            json.dumps({"ld-lane-level-log-analysis-portable": route}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return BridgeConfig(dry_run=False, workspace_root=root, data_dir=data_dir)

    def test_runtime_route_checks_reject_ld_custom_skill_without_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config_with_ld_skill(
                tmp,
                {
                    "kind": "custom_skill",
                    "label": "ld-lane-level-log-analysis",
                    "requires_logs": True,
                    "role": "primary",
                },
            )

            checks = scenarios._runtime_route_checks(config)

        self.assertFalse(checks[0]["ok"])
        self.assertEqual(checks[0]["skill"], "ld-lane-level-log-analysis-portable")
        self.assertIn("executor=file_agent", checks[0]["reason"])
        self.assertFalse(scenarios._validation_passed({"runtime_checks": checks}))

    def test_runtime_route_checks_accept_ld_file_agent_ready_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config_with_ld_skill(
                tmp,
                {
                    "executor": "file_agent",
                    "kind": "custom_skill",
                    "label": "ld-lane-level-log-analysis",
                    "requires_logs": True,
                    "role": "primary",
                },
            )

            checks = scenarios._runtime_route_checks(config)

        self.assertTrue(checks[0]["ok"])
        self.assertEqual(checks[0]["route_status"], "bug_primary_agent_ready")
        self.assertTrue(checks[0]["selectable"])
        self.assertTrue(scenarios._validation_passed({"runtime_checks": checks}))


if __name__ == "__main__":
    unittest.main()
