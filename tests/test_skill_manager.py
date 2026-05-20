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

            manager = SkillManager(BridgeConfig(workspace_root=root))
            skills = {item.name: item for item in manager.list_skills()}

            self.assertIn("custom-check", skills)
            self.assertEqual(skills["custom-check"].role, "custom")
            self.assertIn("xtheme-analyzer", skills)
            self.assertEqual(skills["xtheme-analyzer"].role, "primary")
            self.assertIn("scene-signal-diagnosis", skills)
            self.assertEqual(skills["scene-signal-diagnosis"].role, "primary")

    def test_skill_manager_crud_and_debug(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SkillManager(BridgeConfig(workspace_root=Path(tmp)))

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

            deleted = manager.delete_skill("traffic-skill")
            self.assertEqual(deleted.name, "traffic-skill")
            with self.assertRaises(SkillManagerError):
                manager.get_skill("traffic-skill")

    def test_skill_manager_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SkillManager(BridgeConfig(workspace_root=Path(tmp)))
            with self.assertRaises(SkillManagerError):
                manager.create_skill(name="../bad")


if __name__ == "__main__":
    unittest.main()
