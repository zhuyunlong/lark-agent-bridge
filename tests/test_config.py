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
        self.assertEqual(config.command_prefixes, [])
        self.assertEqual(config.claude_agent.trigger_prefixes, [])
        self.assertTrue(config.knowledge.auto_probe_enabled)
        self.assertIn("模拟", config.knowledge.auto_probe_intent_terms)
        self.assertNotIn("怎么", config.knowledge.auto_probe_intent_terms)
        self.assertTrue(config.source_investigation.enabled)
        self.assertEqual(config.source_investigation.provider, "codex")
        self.assertEqual(config.source_investigation.model, "gpt-5.4")
        self.assertEqual(config.source_investigation.timeout_seconds, 120)
        self.assertEqual(config.source_investigation.max_evidence, 20)
        # Default repo_roots includes guideengine; also Napa5 if it exists on this machine
        self.assertIn(config.guideengine_repo, config.source_investigation.repo_roots)
        self.assertGreaterEqual(len(config.source_investigation.repo_roots), 1)

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
        self.assertEqual(config.data_dir, (Path(tmp) / "bridge-data").resolve())
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
drop_stale_light_interactions = false
stale_light_interaction_grace_seconds = 15
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
        self.assertFalse(config.event_consumer.drop_stale_light_interactions)
        self.assertEqual(config.event_consumer.stale_light_interaction_grace_seconds, 15)

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
auto_fallback_to_file_agent = true
force_reanalysis_terms = ["重新分析", "源码"]
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertTrue(config.bug_analysis.enabled)
        self.assertEqual(config.bug_analysis.provider, "codex")
        self.assertEqual(config.bug_analysis.command, "codex")
        self.assertEqual(config.bug_analysis.model, "gpt-5.4")
        self.assertEqual(config.bug_analysis.timeout_seconds, 99)
        self.assertEqual(config.bug_analysis.agent_summary_timeout_seconds, 45)
        self.assertEqual(config.bug_analysis.default_prompt, "分析这个bug")
        self.assertTrue(config.bug_analysis.resume_followup_sessions)
        self.assertTrue(config.bug_analysis.auto_fallback_to_file_agent)
        self.assertEqual(config.bug_analysis.force_reanalysis_terms, ["重新分析", "源码"])

    def test_bug_analysis_command_defaults_to_codex_for_openai_api_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[ai_provider]
enabled = true
preset = "yybb-codex"

[bug_analysis]
enabled = true
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.ai_provider.api_format, "openai")
        self.assertEqual(config.bug_analysis.provider, "codex")
        self.assertEqual(config.bug_analysis.command, "codex")

    def test_bug_analysis_command_defaults_to_claude_for_anthropic_api_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[ai_provider]
enabled = true
preset = "mimo-claude"

[bug_analysis]
enabled = true
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.ai_provider.api_format, "anthropic")
        self.assertEqual(config.bug_analysis.provider, "claude")
        self.assertEqual(config.bug_analysis.command, "claude")

    def test_intent_analysis_command_defaults_to_matching_provider_when_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[ai_provider]
enabled = true
preset = "yybb-codex"

[intent_analysis]
enabled = true
provider = "codex"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.intent_analysis.provider, "codex")
        self.assertEqual(config.intent_analysis.command, "codex")

    def test_explicit_bug_analysis_command_is_not_overridden_by_api_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[ai_provider]
enabled = true
preset = "yybb-codex"

[bug_analysis]
enabled = true
provider = "codex"
command = "custom-codex"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.ai_provider.api_format, "openai")
        self.assertEqual(config.bug_analysis.command, "custom-codex")

    def test_repository_config_defaults_to_cc_switch_deepseek_claude_when_present(self):
        root_config = Path(__file__).resolve().parents[1] / "config.toml"
        if not root_config.exists():
            self.skipTest("local config.toml is ignored and may be absent in clean checkouts")

        config = load_config(root_config)

        self.assertEqual(config.ai_provider.preset, "cc-switch-deepseek-claude")
        self.assertEqual(config.ai_provider.api_format, "anthropic")
        self.assertEqual(config.ai_provider.base_url, "http://127.0.0.1:15721")
        self.assertEqual(config.ai_provider.api_key, "PROXY_MANAGED")
        self.assertEqual(config.bug_analysis.provider, "claude")
        self.assertEqual(config.bug_analysis.command, "claude")
        self.assertEqual(config.intent_analysis.provider, "claude")
        self.assertEqual(config.intent_analysis.command, "claude")
        self.assertEqual(config.source_investigation.provider, "claude")
        self.assertEqual(config.source_investigation.command, "claude")

    def test_preset_override_drives_agent_defaults_from_protocol(self):
        cases = [
            ("cc-switch-deepseek-claude", "anthropic", "claude"),
            ("cc-switch-mimo-claude", "anthropic", "claude"),
            ("cc-switch-yybb-claude", "anthropic", "claude"),
            ("cc-switch-scihub-claude", "anthropic", "claude"),
            ("cc-switch-yybb-codex", "openai", "codex"),
            ("deepseek-claude", "anthropic", "claude"),
            ("mimo-claude", "anthropic", "claude"),
            ("yybb-claude", "anthropic", "claude"),
            ("yybb-codex", "openai", "codex"),
            ("panda-codex", "openai", "codex"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[ai_provider]
enabled = true
preset = "cc-switch-deepseek-claude"

[bug_analysis]
enabled = true
provider = ""
command = ""

[intent_analysis]
enabled = true
provider = ""
command = ""

[source_investigation]
enabled = true
provider = ""
command = ""
""",
                encoding="utf-8",
            )

            for preset, api_format, agent in cases:
                with self.subTest(preset=preset):
                    with mock.patch.dict(
                        "os.environ",
                        {"LARK_AGENT_BRIDGE_AI_PRESET": preset},
                        clear=False,
                    ):
                        config = load_config(config_path)

                    self.assertEqual(config.ai_provider.preset, preset)
                    self.assertEqual(config.ai_provider.api_format, api_format)
                    self.assertEqual(config.bug_analysis.provider, agent)
                    self.assertEqual(config.bug_analysis.command, agent)
                    self.assertEqual(config.intent_analysis.provider, agent)
                    self.assertEqual(config.intent_analysis.command, agent)
                    self.assertEqual(config.source_investigation.provider, agent)
                    self.assertEqual(config.source_investigation.command, agent)

    def test_agent_provider_env_override_supports_official_cli_login_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[ai_provider]
enabled = true
preset = "cc-switch-deepseek-claude"

[bug_analysis]
enabled = true
provider = ""
command = ""

[intent_analysis]
enabled = true
provider = ""
command = ""

[source_investigation]
enabled = true
provider = ""
command = ""
""",
                encoding="utf-8",
            )

            with mock.patch.dict(
                "os.environ",
                {
                    "LARK_AGENT_BRIDGE_AI_ENABLED": "false",
                    "LARK_AGENT_BRIDGE_AGENT_PROVIDER": "codex",
                    "LARK_AGENT_BRIDGE_AGENT_COMMAND": "codex",
                },
                clear=False,
            ):
                config = load_config(config_path)

        self.assertFalse(config.ai_provider.enabled)
        self.assertEqual(config.bug_analysis.provider, "codex")
        self.assertEqual(config.bug_analysis.command, "codex")
        self.assertEqual(config.intent_analysis.provider, "codex")
        self.assertEqual(config.intent_analysis.command, "codex")
        self.assertEqual(config.source_investigation.provider, "codex")
        self.assertEqual(config.source_investigation.command, "codex")

    def test_relative_paths_are_resolved_to_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "configs" / "bridge"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.toml"
            config_path.write_text(
                """
workspace_root = "../.."
guideengine_repo = "../../guideengine"
data_dir = "data"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertTrue(config.workspace_root.is_absolute())
        self.assertTrue(config.guideengine_repo.is_absolute())
        self.assertTrue(config.data_dir.is_absolute())
        self.assertNotIn("..", str(config.workspace_root))

    def test_load_source_investigation_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
guideengine_repo = "guideengine"

[source_investigation]
enabled = true
provider = "codex"
command = "codex"
model = "gpt-5.3-codex-spark"
fallback_model = "gpt-5.3-codex"
timeout_seconds = 66
max_evidence = 7
repo_roots = ["guideengine"]
add_dirs = ["Napa5"]
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertTrue(config.source_investigation.enabled)
        self.assertEqual(config.source_investigation.provider, "codex")
        self.assertEqual(config.source_investigation.command, "codex")
        self.assertEqual(config.source_investigation.model, "gpt-5.3-codex-spark")
        self.assertEqual(config.source_investigation.fallback_model, "gpt-5.3-codex")
        self.assertEqual(config.source_investigation.timeout_seconds, 66)
        self.assertEqual(config.source_investigation.max_evidence, 7)
        self.assertEqual(config.source_investigation.repo_roots, [(Path(tmp) / "guideengine").resolve()])
        self.assertEqual(config.source_investigation.add_dirs, [(Path(tmp) / "Napa5").resolve()])

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
allow_subprocess_fallback = true
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertTrue(config.intent_analysis.enabled)
        self.assertEqual(config.intent_analysis.provider, "codex")
        self.assertEqual(config.intent_analysis.command, "codex")
        self.assertEqual(config.intent_analysis.model, "gpt-5.4")
        self.assertEqual(config.intent_analysis.timeout_seconds, 45)
        self.assertEqual(config.intent_analysis.max_prompt_chars, 6000)
        self.assertTrue(config.intent_analysis.allow_subprocess_fallback)

    def test_load_knowledge_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[knowledge]
enabled = true
storage = "kb/knowledge.sqlite"
max_hits = 7
trigger_prefixes = ["/kb", "知识库"]
answer_provider = "omlx"

[[knowledge.sources]]
id = "adb"
type = "local_json"
path = "adb_data.json"

[[knowledge.sources]]
id = "wiki"
type = "feishu_doc"
url = "https://example.feishu.cn/wiki/doc"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertTrue(config.knowledge.enabled)
        self.assertEqual(config.knowledge.storage, (Path(tmp) / "kb/knowledge.sqlite").resolve())
        self.assertEqual(config.knowledge.max_hits, 7)
        self.assertEqual(config.knowledge.trigger_prefixes, ["/kb", "知识库"])
        self.assertEqual(config.knowledge.sources[0].id, "adb")
        self.assertEqual(config.knowledge.sources[0].path, str((Path(tmp) / "adb_data.json").resolve()))
        self.assertEqual(config.knowledge.sources[1].url, "https://example.feishu.cn/wiki/doc")

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
        self.assertEqual(config.local_resources.allowed_dirs, [downloads.resolve()])


if __name__ == "__main__":
    unittest.main()
