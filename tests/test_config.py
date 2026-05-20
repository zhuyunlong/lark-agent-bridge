from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lark_agent_bridge.config import load_config
from lark_agent_bridge.models import BridgeConfig


class ConfigTests(unittest.TestCase):
    def test_load_defaults_without_file(self):
        config = load_config()

        self.assertTrue(config.dry_run)
        self.assertIn("LD normal", config.signal_aliases)
        self.assertEqual(config.download.timeout_seconds, 60)
        self.assertTrue(config.report_server.enabled)
        self.assertFalse(config.approval.enabled)
        self.assertEqual(config.job_retention.bug_cache_max_age_hours, 24)

    def test_toml_overrides_are_resolved_relative_to_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
dry_run = false
data_dir = "bridge-data"
allowed_users = ["ou_1"]

[download]
max_bytes = 12
timeout_seconds = 3

[lark]
bot_open_id = "ou_bot"
bot_name = "Test Bot"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertFalse(config.dry_run)
        self.assertEqual(config.allowed_users, ["ou_1"])
        self.assertEqual(config.download.max_bytes, 12)
        self.assertEqual(config.download.timeout_seconds, 3)
        self.assertEqual(config.data_dir, Path(tmp) / "bridge-data")
        self.assertEqual(config.lark.bot_open_id, "ou_bot")
        self.assertEqual(config.lark.bot_name, "Test Bot")

    def test_load_agent_and_omlx_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[claude_agent]
enabled = true
command = "claude"
trigger_prefixes = ["/skill"]
allowed_tools = ["Read", "Grep"]
timeout_seconds = 12
upload_result_file = false

[omlx_chat]
enabled = true
base_url = "http://127.0.0.1:8000/v1"
model = "gemma-4-26b-a4b-it-4bit"
api_key = "test-api-key"
timeout_seconds = 9
followup_max_context_chars = 4096

[report_server]
enabled = true
bind_host = "0.0.0.0"
port = 9000
public_base_url = "https://bridge.example.com/reports"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertTrue(config.claude_agent.enabled)
        self.assertEqual(config.claude_agent.trigger_prefixes, ["/skill"])
        self.assertEqual(config.claude_agent.allowed_tools, ["Read", "Grep"])
        self.assertEqual(config.claude_agent.timeout_seconds, 12)
        self.assertFalse(config.claude_agent.upload_result_file)
        self.assertTrue(config.omlx_chat.enabled)
        self.assertEqual(config.omlx_chat.model, "gemma-4-26b-a4b-it-4bit")
        self.assertEqual(config.omlx_chat.api_key, "test-api-key")
        self.assertEqual(config.omlx_chat.timeout_seconds, 9)
        self.assertEqual(config.omlx_chat.followup_max_context_chars, 4096)
        self.assertEqual(config.report_server.bind_host, "0.0.0.0")
        self.assertEqual(config.report_server.port, 9000)
        self.assertEqual(config.report_server.public_base_url, "https://bridge.example.com/reports")

    def test_load_job_retention_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[job_retention]
enabled = true
max_age_hours = 4
bug_cache_max_age_hours = 24
purge_all_on_listen_start = false
cleanup_interval_seconds = 30
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertTrue(config.job_retention.enabled)
        self.assertEqual(config.job_retention.max_age_hours, 4)
        self.assertEqual(config.job_retention.bug_cache_max_age_hours, 24)
        self.assertFalse(config.job_retention.purge_all_on_listen_start)
        self.assertEqual(config.job_retention.cleanup_interval_seconds, 30)

    def test_load_event_consumer_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[event_consumer]
event_key = "im.message.receive_v1"
ready_timeout_seconds = 5
restart_on_failure = true
max_restarts = 2
restart_initial_delay_seconds = 0.5
restart_max_delay_seconds = 8
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.event_consumer.event_key, "im.message.receive_v1")
        self.assertEqual(config.event_consumer.ready_timeout_seconds, 5)
        self.assertTrue(config.event_consumer.restart_on_failure)
        self.assertEqual(config.event_consumer.max_restarts, 2)
        self.assertEqual(config.event_consumer.restart_initial_delay_seconds, 0.5)
        self.assertEqual(config.event_consumer.restart_max_delay_seconds, 8)

    def test_load_approval_and_workflow_archive_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[approval]
enabled = false

[workflow_archive]
enabled = true
base_token = "base_token"
table_id = "tbl_case"
drive_folder_token = "fld_reports"
doc_parent_token = "fld_docs"

[workflow_archive.base_field_map]
job_id = "任务ID"
mode = "分析类型"
summary = "结论摘要"

[notifications]
enabled = true
report_ready = false

[dual_agent]
enabled = true
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertFalse(config.approval.enabled)
        self.assertTrue(config.workflow_archive.enabled)
        self.assertEqual(config.workflow_archive.base_token, "base_token")
        self.assertEqual(config.workflow_archive.table_id, "tbl_case")
        self.assertEqual(config.workflow_archive.drive_folder_token, "fld_reports")
        self.assertEqual(config.workflow_archive.doc_parent_token, "fld_docs")
        self.assertEqual(config.workflow_archive.base_field_map["summary"], "结论摘要")
        self.assertTrue(config.notifications.enabled)
        self.assertFalse(config.notifications.report_ready)
        self.assertTrue(config.dual_agent.enabled)

    def test_load_bug_analysis_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[bug_analysis]
enabled = true
provider = "codex"
command = "codex"
timeout_seconds = 99
agent_summary_timeout_seconds = 45
default_prompt = "分析这个bug"
resume_followup_sessions = true
force_reanalysis_terms = ["重新分析", "源码"]
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertTrue(config.bug_analysis.enabled)
        self.assertEqual(config.bug_analysis.provider, "codex")
        self.assertEqual(config.bug_analysis.command, "codex")
        self.assertEqual(config.bug_analysis.timeout_seconds, 99)
        self.assertEqual(config.bug_analysis.agent_summary_timeout_seconds, 45)
        self.assertEqual(config.bug_analysis.default_prompt, "分析这个bug")
        self.assertTrue(config.bug_analysis.resume_followup_sessions)
        self.assertEqual(config.bug_analysis.force_reanalysis_terms, ["重新分析", "源码"])

    def test_default_bug_analysis_prompt_is_empty(self):
        config = load_config()

        self.assertEqual(config.bug_analysis.default_prompt, "")

    def test_load_intent_analysis_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[intent_analysis]
enabled = true
provider = "codex"
command = "codex"
timeout_seconds = 45
max_prompt_chars = 6000
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertTrue(config.intent_analysis.enabled)
        self.assertEqual(config.intent_analysis.provider, "codex")
        self.assertEqual(config.intent_analysis.command, "codex")
        self.assertEqual(config.intent_analysis.timeout_seconds, 45)
        self.assertEqual(config.intent_analysis.max_prompt_chars, 6000)

    def test_environment_overrides_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
allowed_chats = ["oc_file"]
allowed_users = ["ou_file"]

[lark]
bot_open_id = "ou_file_bot"
bot_name = "File Bot"

[omlx_chat]
api_key = "file-api-key"
""",
                encoding="utf-8",
            )

            with mock.patch.dict(
                "os.environ",
                {
                    "LARK_AGENT_BRIDGE_ALLOWED_CHATS": "oc_env_a,oc_env_b",
                    "LARK_AGENT_BRIDGE_ALLOWED_USERS": "ou_env_a,ou_env_b",
                    "LARK_AGENT_BRIDGE_BOT_OPEN_ID": "ou_env_bot",
                    "LARK_AGENT_BRIDGE_BOT_NAME": "Env Bot",
                    "LARK_AGENT_BRIDGE_OMLX_API_KEY": "env-api-key",
                    "LARK_AGENT_BRIDGE_REPORT_PUBLIC_BASE_URL": "https://env.example.com/reports",
                },
                clear=False,
            ):
                config = load_config(config_path)

        self.assertEqual(config.allowed_chats, ["oc_env_a", "oc_env_b"])
        self.assertEqual(config.allowed_users, ["ou_env_a", "ou_env_b"])
        self.assertEqual(config.lark.bot_open_id, "ou_env_bot")
        self.assertEqual(config.lark.bot_name, "Env Bot")
        self.assertEqual(config.omlx_chat.api_key, "env-api-key")
        self.assertEqual(config.report_server.public_base_url, "https://env.example.com/reports")

    def test_loads_local_resource_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            downloads = Path(tmp) / "downloads"
            config_path.write_text(
                f"""
[local_resources]
enabled = true
require_allowed_user = true
allowed_dirs = ["{downloads}"]
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertTrue(config.local_resources.enabled)
        self.assertTrue(config.local_resources.require_allowed_user)
        self.assertEqual(config.local_resources.allowed_dirs, [downloads])


if __name__ == "__main__":
    unittest.main()
