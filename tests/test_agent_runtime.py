"""Tests for provider_capabilities, agent_output_models, agent_tools, and agent_runtime."""

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import dataclass


@dataclass(slots=True)
class _FakeAIOptions:
    enabled: bool = True
    preset: str = ""
    api_format: str = "openai"
    primary_model: str = "gpt-5.4"
    fast_model: str = "gpt-5.4-mini"
    fallback_model: str = ""
    base_url: str = "https://api.example.com/v1"
    api_key: str = "test-key"
    fallback_base_url: str = ""
    fallback_api_key: str = ""
    profile_type: str = ""
    agent_provider: str = ""
    agent_command: str = ""
    requires_api_key: bool = False
    precondition: str = ""
    intent_temperature: float = 0.0
    intent_max_tokens: int = 1024
    intent_timeout_seconds: float = 30
    intent_max_retries: int = 2
    summary_temperature: float = 0.3
    summary_max_tokens: int = 4096
    summary_timeout_seconds: float = 120


class TestProviderCapabilities(unittest.TestCase):
    def test_openai_format_has_full_capabilities(self):
        from lark_agent_bridge.agents.provider_capabilities import detect_capabilities

        opts = _FakeAIOptions(api_format="openai", primary_model="gpt-5.4")
        caps = detect_capabilities(opts)
        self.assertTrue(caps.supports_structured_output)
        self.assertTrue(caps.supports_function_tools)
        self.assertTrue(caps.supports_multi_step_tools)
        self.assertEqual(caps.api_format, "openai")

    def test_anthropic_format_has_full_capabilities(self):
        from lark_agent_bridge.agents.provider_capabilities import detect_capabilities

        opts = _FakeAIOptions(api_format="anthropic", primary_model="claude-sonnet-4-6")
        caps = detect_capabilities(opts)
        self.assertTrue(caps.supports_structured_output)
        self.assertTrue(caps.supports_function_tools)
        self.assertEqual(caps.api_format, "anthropic")

    def test_no_tool_model_detected(self):
        from lark_agent_bridge.agents.provider_capabilities import detect_capabilities

        opts = _FakeAIOptions(api_format="openai", primary_model="gemma-4-26b")
        caps = detect_capabilities(opts)
        self.assertTrue(caps.supports_structured_output)
        self.assertFalse(caps.supports_function_tools)
        self.assertEqual(caps.source, "no_tool_model")

    def test_empty_format_returns_unknown(self):
        from lark_agent_bridge.agents.provider_capabilities import detect_capabilities

        opts = _FakeAIOptions(api_format="", base_url="")
        caps = detect_capabilities(opts)
        self.assertFalse(caps.supports_structured_output)
        self.assertEqual(caps.source, "unknown_format")

    def test_url_heuristic_anthropic(self):
        from lark_agent_bridge.agents.provider_capabilities import detect_capabilities

        opts = _FakeAIOptions(api_format="", base_url="https://api.example.com/anthropic")
        caps = detect_capabilities(opts)
        self.assertEqual(caps.api_format, "anthropic")
        self.assertTrue(caps.supports_function_tools)

    def test_select_runtime_path(self):
        from lark_agent_bridge.agents.provider_capabilities import (
            ProviderCapabilities,
            select_runtime_path,
        )

        full = ProviderCapabilities(supports_structured_output=True, supports_function_tools=True)
        self.assertEqual(select_runtime_path(full), "pydantic_ai_agent")

        structured_only = ProviderCapabilities(supports_structured_output=True, supports_function_tools=False)
        self.assertEqual(select_runtime_path(structured_only), "pydantic_ai_structured")

        api_only = ProviderCapabilities(supports_structured_output=False, api_format="openai")
        self.assertEqual(select_runtime_path(api_only), "direct_api")

        nothing = ProviderCapabilities()
        self.assertEqual(select_runtime_path(nothing), "subprocess")


class TestAgentOutputModels(unittest.TestCase):
    def test_source_analysis_output_to_markdown(self):
        from lark_agent_bridge.agents.agent_output_models import SourceAnalysisOutput

        output = SourceAnalysisOutput(
            conclusion="Root cause is X",
            root_cause="Detailed analysis of X",
            evidence=[
                {"file": "foo.kt", "line": "42", "content": "crash here", "relevance": "high"},
            ],
            pending_items=["Check Y"],
            suggested_actions=["Fix Z"],
            confidence="high",
        )
        md = output.to_markdown()
        self.assertIn("Root cause is X", md)
        self.assertIn("foo.kt:42", md)
        self.assertIn("crash here", md)
        self.assertIn("Check Y", md)
        self.assertIn("Fix Z", md)

    def test_source_analysis_output_empty(self):
        from lark_agent_bridge.agents.agent_output_models import SourceAnalysisOutput

        output = SourceAnalysisOutput()
        md = output.to_markdown()
        self.assertIsInstance(md, str)

    def test_bug_analysis_output_to_markdown(self):
        from lark_agent_bridge.agents.agent_output_models import BugAnalysisOutput

        output = BugAnalysisOutput(
            summary="Bug summary",
            analysis="Detailed",
            evidence=[{"file": "bar.py", "content": "issue"}],
            suggested_fix="Do this",
        )
        md = output.to_markdown()
        self.assertIn("Bug summary", md)
        self.assertIn("bar.py", md)
        self.assertIn("Do this", md)


class TestAgentTools(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmpdir = tempfile.mkdtemp()
        self.workspace = Path(self.tmpdir)
        (self.workspace / "test.txt").write_text("hello world\nfoo bar\n", encoding="utf-8")
        (self.workspace / "subdir").mkdir()
        (self.workspace / "subdir" / "nested.py").write_text("import os\ndef main(): pass\n", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_read_file(self):
        from lark_agent_bridge.agents.agent_tools import read_file

        content = read_file("test.txt", workspace=self.workspace)
        self.assertIn("hello world", content)

    def test_read_file_outside_workspace(self):
        from lark_agent_bridge.agents.agent_tools import read_file

        content = read_file("../../etc/passwd", workspace=self.workspace)
        self.assertIn("Error", content)

    def test_read_file_not_found(self):
        from lark_agent_bridge.agents.agent_tools import read_file

        content = read_file("nonexistent.txt", workspace=self.workspace)
        self.assertIn("Error", content)

    def test_grep_text(self):
        from lark_agent_bridge.agents.agent_tools import grep_text

        result = grep_text("hello", workspace=self.workspace)
        self.assertIn("test.txt", result)
        self.assertIn("hello world", result)

    def test_grep_text_tolerates_non_utf8_rg_output(self):
        from unittest import mock

        from lark_agent_bridge.agents.agent_tools import grep_text

        raw_line = (
            f"{self.workspace}/legacy/DBCmd.csv:25:switchtheme,"
            "ADBHelper,AdbSwitchTheme,"
        ).encode("utf-8") + b"\xc7\xd0\xbb\xbb\xd6\xf7\xcc\xe2\n"

        with mock.patch("shutil.which", return_value="/usr/bin/rg"), \
             mock.patch("subprocess.run") as run_mock:
            run_mock.return_value = mock.Mock(returncode=0, stdout=raw_line, stderr=b"")

            result = grep_text("switchtheme", workspace=self.workspace)

        self.assertIn("legacy/DBCmd.csv:25:switchtheme", result)
        self.assertIn("AdbSwitchTheme", result)
        self.assertIn("\ufffd", result)

    def test_grep_no_match(self):
        from lark_agent_bridge.agents.agent_tools import grep_text

        result = grep_text("zzzznotfound", workspace=self.workspace)
        self.assertIn("No matches", result)

    def test_glob_paths(self):
        from lark_agent_bridge.agents.agent_tools import glob_paths

        result = glob_paths("**/*.py", workspace=self.workspace)
        self.assertIn("nested.py", result)

    def test_glob_paths_tolerates_non_utf8_rg_output(self):
        from unittest import mock

        from lark_agent_bridge.agents.agent_tools import glob_paths

        raw_paths = (
            f"{self.workspace}/src/normal.py\n".encode("utf-8")
            + f"{self.workspace}/src/".encode("utf-8")
            + b"\xc7\xd0.py\n"
        )

        with mock.patch("shutil.which", return_value="/usr/bin/rg"), \
             mock.patch("subprocess.run") as run_mock:
            run_mock.return_value = mock.Mock(returncode=0, stdout=raw_paths, stderr=b"")

            result = glob_paths("**/*.py", workspace=self.workspace)

        self.assertIn("src/normal.py", result)
        self.assertIn("src/\ufffd\ufffd.py", result)

    def test_list_dir(self):
        from lark_agent_bridge.agents.agent_tools import list_dir

        result = list_dir(".", workspace=self.workspace)
        self.assertIn("test.txt", result)
        self.assertIn("subdir/", result)

    def test_read_bug_context(self):
        from lark_agent_bridge.agents.agent_tools import read_bug_context

        ctx = read_bug_context(
            title="Crash on start",
            description="App crashes",
            fault_time="2026-05-27",
            request_text="分析一下",
        )
        self.assertIn("Crash on start", ctx)
        self.assertIn("2026-05-27", ctx)


class TestAgentRuntime(unittest.TestCase):
    def test_runtime_init(self):
        from lark_agent_bridge.agents.agent_runtime import AgentRuntime

        runtime = AgentRuntime(_FakeAIOptions())
        self.assertIn(runtime.preferred_path, {"pydantic_ai_agent", "pydantic_ai_structured", "direct_api", "subprocess"})

    def test_runtime_direct_api_fallback(self):
        from lark_agent_bridge.agents.agent_runtime import AgentRuntime

        # Mock LLMClient
        opts = _FakeAIOptions(api_format="openai")
        runtime = AgentRuntime(opts)

        mock_response = MagicMock()
        mock_response.content = "# Analysis Result\nThis is the analysis."
        mock_response.model = "gpt-5.4"
        mock_response.usage = {"total_tokens": 100}

        with patch("lark_agent_bridge.agents.agent_runtime._check_pydantic_ai", return_value=False):
            with patch("lark_agent_bridge.agents.agent_runtime.AgentRuntime._run_direct_api") as mock_direct:
                mock_direct.return_value = MagicMock(ok=True, markdown="result", runtime_path="direct_api")
                # Force direct_api path
                runtime._preferred_path = "direct_api"
                result = runtime.run(
                    system_prompt="Test",
                    user_prompt="Analyze this",
                )
                mock_direct.assert_called_once()

    def test_runtime_not_available_when_no_config(self):
        from lark_agent_bridge.agents.agent_runtime import AgentRuntime

        opts = _FakeAIOptions(api_format="", base_url="", primary_model="")
        runtime = AgentRuntime(opts)
        self.assertFalse(runtime.is_available())

    def test_runtime_result_defaults(self):
        from lark_agent_bridge.agents.agent_runtime import RuntimeResult

        result = RuntimeResult()
        self.assertFalse(result.ok)
        self.assertEqual(result.markdown, "")
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(result.tool_trace, [])


if __name__ == "__main__":
    unittest.main()
