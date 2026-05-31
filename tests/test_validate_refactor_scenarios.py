from pathlib import Path
import json
import tempfile
import unittest

from lark_agent_bridge.models import BridgeConfig
import scripts.validate_refactor_scenarios as scenarios


class ValidateRefactorScenariosTests(unittest.TestCase):
    def _config_with_ld_skill(self, tmp: str, route: dict[str, object] | None) -> BridgeConfig:
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
        payload = {"ld-lane-level-log-analysis-portable": route} if route is not None else {}
        route_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return BridgeConfig(dry_run=False, workspace_root=root, data_dir=data_dir)

    def test_runtime_route_checks_reject_stale_ld_custom_skill_route(self):
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
        self.assertIn("kind=source_code_skill", checks[0]["reason"])
        self.assertFalse(scenarios._validation_passed({"runtime_checks": checks}))

    def test_runtime_route_checks_accept_builtin_ld_kind_without_custom_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config_with_ld_skill(tmp, None)

            checks = scenarios._runtime_route_checks(config)

        self.assertTrue(checks[0]["ok"])
        self.assertEqual(checks[0]["kind"], "ld_lane_level")
        self.assertEqual(checks[0]["route_status"], "bug_primary")
        self.assertTrue(checks[0]["selectable"])
        self.assertTrue(scenarios._validation_passed({"runtime_checks": checks}))


if __name__ == "__main__":
    unittest.main()
