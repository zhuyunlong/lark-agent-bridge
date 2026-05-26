from pathlib import Path
import tempfile
import unittest


from lark_agent_bridge.models import BridgeConfig
from lark_agent_bridge.skill_manager import SkillManager, SkillManagerError


class SkillManagerTests(unittest.TestCase):
    def test_skill_manager_lists_primary_and_custom_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / ".ai" / "skills" / "custom-check"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Custom Check\ndescription: 自定义检查\n---\n\n# Custom Check\n",
                encoding="utf-8",
            )
            primary_dir = root / ".ai" / "skills" / "xtheme-analyzer"
            primary_dir.mkdir(parents=True)
            (primary_dir / "SKILL.md").write_text(
                "---\nname: XTheme Analyzer\ndescription: 主题分析\n---\n\n# XTheme\n",
                encoding="utf-8",
            )

            manager = SkillManager(BridgeConfig(workspace_root=root, data_dir=root / "data"))
            skills = {item.name: item for item in manager.list_skills()}

            self.assertIn("custom-check", skills)
            self.assertEqual(skills["custom-check"].role, "custom")
            self.assertEqual(skills["custom-check"].route_status, "custom_unrouted")
            self.assertFalse(skills["custom-check"].selectable_in_report_card)
            self.assertIn("xtheme-analyzer", skills)
            self.assertEqual(skills["xtheme-analyzer"].role, "primary")
            self.assertEqual(skills["xtheme-analyzer"].route_status, "bug_primary")
            self.assertTrue(skills["xtheme-analyzer"].selectable_in_report_card)
            self.assertIn("scene-signal-diagnosis", skills)
            self.assertEqual(skills["scene-signal-diagnosis"].role, "primary")
            self.assertEqual(skills["scene-signal-diagnosis"].route_status, "configured_missing")
            self.assertFalse(skills["scene-signal-diagnosis"].selectable_in_report_card)

            routed = manager.set_skill_route("custom-check", role="primary")
            self.assertEqual(routed.role, "primary")
            self.assertEqual(routed.route_status, "bug_primary_unready")
            self.assertFalse(routed.selectable_in_report_card)
            self.assertEqual(routed.executor, "")
            self.assertIn("没有可执行分析器", routed.routing_note)
            self.assertIn("custom-check", manager.primary_skill_map())
            self.assertEqual(manager.primary_skill_map()["custom-check"][0], "custom_skill")

            legacy_routed = manager.set_skill_route("custom-check", role="primary", kind="general")
            self.assertEqual(legacy_routed.kind, "custom_skill")
            self.assertEqual(legacy_routed.route_status, "bug_primary_unready")
            self.assertFalse(legacy_routed.selectable_in_report_card)
            self.assertEqual(manager.primary_skill_map()["custom-check"][0], "custom_skill")

            agent_routed = manager.set_skill_route("custom-check", role="primary", executor="file_agent")
            self.assertEqual(agent_routed.kind, "custom_skill")
            self.assertEqual(agent_routed.executor, "file_agent")
            self.assertEqual(agent_routed.route_status, "bug_primary_agent_ready")
            self.assertTrue(agent_routed.selectable_in_report_card)
            self.assertIn("文件 Agent", agent_routed.routing_note)
            self.assertEqual(manager.custom_skill_executor_for("custom-check"), "file_agent")

            auxiliary = manager.set_skill_route("custom-check", role="auxiliary")
            self.assertEqual(auxiliary.role, "auxiliary")
            self.assertEqual(auxiliary.executor, "")
            self.assertIn("custom-check", manager.auxiliary_skill_names())
            self.assertNotIn("custom-check", manager.primary_skill_map())

            restored = manager.set_skill_route("custom-check", role="custom")
            self.assertEqual(restored.role, "custom")
            self.assertFalse(restored.selectable_in_report_card)

    def test_skill_manager_crud_and_debug(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SkillManager(BridgeConfig(workspace_root=Path(tmp), data_dir=Path(tmp) / "data"))

            created = manager.create_skill(
                name="traffic-skill",
                label="Traffic Skill",
                description="路况 traffic 分析",
            )
            self.assertIs(created.skill_md_exists, True)
            self.assertIn("argument-hint:", created.content)
            self.assertIn("user-invocable: true", created.content)
            self.assertIn("skill_id: TRAFFIC_SKILL", created.content)
            self.assertIn("## 标准执行规范", created.content)

            updated = manager.update_skill(
                "traffic-skill",
                content="---\nname: Traffic Skill\ndescription: traffic 路况\n---\n\n# Traffic\n",
            )
            self.assertIn("traffic 路况", updated.description)

            debug = manager.debug_skill("traffic-skill", sample_text="请分析 traffic 路况数据")
            self.assertIs(debug["sample"]["would_consider"], True)
            self.assertIs(debug["checks"][1]["ok"], True)
            self.assertIn("报告卡片可选", [item["label"] for item in debug["checks"]])

            deleted = manager.delete_skill("traffic-skill")
            self.assertEqual(deleted.name, "traffic-skill")
            with self.assertRaises(SkillManagerError):
                manager.get_skill("traffic-skill")

    def test_skill_manager_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SkillManager(BridgeConfig(workspace_root=Path(tmp), data_dir=Path(tmp) / "data"))
            with self.assertRaises(SkillManagerError):
                manager.create_skill(name="../bad")


if __name__ == "__main__":
    unittest.main()
