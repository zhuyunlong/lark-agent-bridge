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
    summary_timeout_seconds: float = 300


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


class TestLLMClient(unittest.TestCase):
    def test_openai_client_disables_sdk_retries_for_bridge_timeouts(self):
        from lark_agent_bridge.agents.llm_client import LLMClient

        opts = _FakeAIOptions(
            api_format="openai",
            base_url="https://api.example.com/v1",
            api_key="test-key",
            intent_timeout_seconds=30,
            summary_timeout_seconds=300,
        )
        with patch("openai.OpenAI") as openai_cls:
            LLMClient(opts)._get_openai_client(opts.base_url, opts.api_key)

        self.assertEqual(openai_cls.call_args.kwargs["max_retries"], 0)
        self.assertEqual(openai_cls.call_args.kwargs["timeout"], 310)


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

    def test_search_large_log_finds_matches_past_small_file_limit(self):
        from lark_agent_bridge.agents.agent_tools import search_large_log

        large_log = self.workspace / "logs" / "user0_main_2026-05-28_11-00.alog.log"
        large_log.parent.mkdir(parents=True)
        filler = "05-28 11:00:00 100 200 I Filler: " + ("x" * 180) + "\n"
        content = filler * 1800
        content += "05-28 11:18:00 13379 1 I NAV_SrSM_UnityStarting: startCheck timeout, not onMainActivityCreate, so kill self\n"
        large_log.write_text(content, encoding="utf-8")

        result = search_large_log(
            "startCheck timeout",
            workspace=self.workspace,
            path="logs/user0_main_2026-05-28_11-00.alog.log",
            max_results=10,
        )

        self.assertIn("logs/user0_main_2026-05-28_11-00.alog.log", result)
        self.assertIn("startCheck timeout", result)
        self.assertIn("not onMainActivityCreate", result)

    def test_register_tools_registers_search_large_log(self):
        from lark_agent_bridge.agents.agent_tools import register_tools

        class FakeAgent:
            def __init__(self):
                self.tools = {}

            def tool_plain(self, func):
                self.tools[func.__name__] = func
                return func

        agent = FakeAgent()
        register_tools(agent, self.workspace)

        self.assertIn("search_large_log", agent.tools)

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

    def test_anthropic_settings_enable_prompt_cache_breakpoints(self):
        """Anthropic path must set cache_control breakpoints (system/tools/conversation)."""
        from lark_agent_bridge.agents.agent_runtime import AgentRuntime

        runtime = AgentRuntime(_FakeAIOptions(api_format="anthropic", base_url="http://x/anthropic"))
        settings = runtime._build_model_settings(8192)
        d = dict(settings)
        self.assertIn("anthropic_cache_instructions", d)
        self.assertIn("anthropic_cache_tool_definitions", d)
        self.assertIn("anthropic_cache", d)
        # Static prefixes cached for 1h, moving conversation breakpoint for 5m.
        self.assertEqual(d["anthropic_cache_instructions"], "1h")
        self.assertEqual(d["anthropic_cache_tool_definitions"], "1h")
        self.assertEqual(d["anthropic_cache"], "5m")
        self.assertEqual(d["max_tokens"], 8192)

    def test_openai_settings_use_seed_and_store(self):
        """OpenAI path relies on automatic prefix caching helped by seed + store."""
        from lark_agent_bridge.agents.agent_runtime import AgentRuntime

        runtime = AgentRuntime(_FakeAIOptions(api_format="openai"))
        settings = runtime._build_model_settings(8192)
        d = dict(settings)
        self.assertEqual(d.get("seed"), 42)
        self.assertEqual(d.get("extra_body"), {"store": True})
        self.assertNotIn("anthropic_cache", d)

    def test_extract_usage_includes_cache_tokens(self):
        from lark_agent_bridge.agents.agent_runtime import _extract_usage

        class _Usage:
            input_tokens = 1000
            output_tokens = 50
            request_tokens = 1000
            response_tokens = 50
            total_tokens = 1050
            cache_read_tokens = 900
            cache_write_tokens = 100

        class _Result:
            usage = _Usage()  # property-style (non-callable) access

        out = _extract_usage(_Result())
        self.assertEqual(out["cache_read_tokens"], 900)
        self.assertEqual(out["cache_write_tokens"], 100)
        self.assertEqual(out["request_tokens"], 1000)

    def test_runtime_stream_validation_error_never_reaches_stream_path_for_structured_output(self):
        from lark_agent_bridge.agents.agent_runtime import AgentRuntime, RuntimeResult
        from lark_agent_bridge.agents.agent_output_models import SourceAnalysisOutput

        opts = _FakeAIOptions(api_format="openai")
        runtime = AgentRuntime(opts)

        stream_error = RuntimeResult(
            ok=False,
            error="Output validation failed during streaming, and retries are not supported in `run_stream()`",
            error_code="pydantic_ai_stream_error",
            runtime_path="pydantic_ai_agent",
        )
        non_stream_success = RuntimeResult(
            ok=True,
            markdown="## 结论摘要\n\n重试成功\n",
            runtime_path="pydantic_ai_agent",
        )

        with (
            patch("lark_agent_bridge.agents.agent_runtime._check_pydantic_ai", return_value=True),
            patch.object(runtime, "_run_pydantic_ai_stream", return_value=stream_error) as stream_mock,
            patch.object(runtime, "_run_pydantic_ai", return_value=non_stream_success) as non_stream_mock,
        ):
            result = runtime.run(
                output_type=SourceAnalysisOutput,
                system_prompt="Test",
                user_prompt="Analyze",
                strict_tools=True,
                stream=True,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.markdown, "## 结论摘要\n\n重试成功\n")
        stream_mock.assert_not_called()
        non_stream_mock.assert_called_once()

    def test_runtime_structured_output_bypasses_stream_path(self):
        from lark_agent_bridge.agents.agent_runtime import AgentRuntime, RuntimeResult
        from lark_agent_bridge.agents.agent_output_models import SourceAnalysisOutput

        opts = _FakeAIOptions(api_format="openai")
        runtime = AgentRuntime(opts)
        non_stream_success = RuntimeResult(
            ok=True,
            markdown="## 结论摘要\n\n稳定结果\n",
            runtime_path="pydantic_ai_agent",
        )

        with (
            patch("lark_agent_bridge.agents.agent_runtime._check_pydantic_ai", return_value=True),
            patch.object(runtime, "_run_pydantic_ai_stream") as stream_mock,
            patch.object(runtime, "_run_pydantic_ai", return_value=non_stream_success) as non_stream_mock,
        ):
            result = runtime.run(
                output_type=SourceAnalysisOutput,
                system_prompt="Test",
                user_prompt="Analyze",
                tools_enabled=True,
                strict_tools=True,
                stream=True,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.markdown, "## 结论摘要\n\n稳定结果\n")
        stream_mock.assert_not_called()
        non_stream_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
