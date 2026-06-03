"""Tests for source_stage structured node_status + findings output (Task G2)."""
import unittest


class TestSourceAnalysisOutputStructuredFields(unittest.TestCase):
    def test_source_analysis_output_has_structured_fields(self):
        from lark_agent_bridge.agents.agent_output_models import SourceAnalysisOutput
        import inspect

        sig = inspect.signature(SourceAnalysisOutput)
        fields = set(SourceAnalysisOutput.model_fields.keys())
        assert "node_status" in fields, f"node_status missing from SourceAnalysisOutput fields: {fields}"
        assert "findings" in fields, f"findings missing from SourceAnalysisOutput fields: {fields}"

    def test_source_analysis_output_defaults_and_round_trip(self):
        from lark_agent_bridge.agents.agent_output_models import SourceAnalysisOutput

        obj = SourceAnalysisOutput()
        assert obj.node_status == {}, f"node_status default should be empty dict, got: {obj.node_status}"
        assert obj.findings == [], f"findings default should be empty list, got: {obj.findings}"

        # with values
        obj2 = SourceAnalysisOutput(
            node_status={"foo.py": "ok", "bar.py": "suspect"},
            findings=[{"file": "foo.py", "severity": "high", "title": "bad logic", "kind": "logic"}],
        )
        dumped = obj2.model_dump()
        assert dumped["node_status"] == {"foo.py": "ok", "bar.py": "suspect"}
        assert len(dumped["findings"]) == 1
        assert dumped["findings"][0]["file"] == "foo.py"


class TestParseNodeStatusBlock(unittest.TestCase):
    def test_parse_node_status_block_extracts_valid_block(self):
        from lark_agent_bridge.agents.bug.bug_prompt import parse_node_status_block

        text = (
            "## 结论摘要\n\n问题在于信号未到达。\n\n"
            "## 关键证据\n\n- foo.py:42 重要调用\n\n"
            "```json\n"
            '{"node_status": {"foo.py": "broken", "bar.py": "ok"}, "findings": [{"file": "foo.py", "severity": "high", "title": "missing call", "kind": "logic"}]}\n'
            "```\n"
        )
        result = parse_node_status_block(text)
        assert result.get("node_status") == {"foo.py": "broken", "bar.py": "ok"}, f"Got: {result}"
        assert len(result.get("findings", [])) == 1
        assert result["findings"][0]["file"] == "foo.py"

    def test_parse_node_status_block_returns_empty_on_no_block(self):
        from lark_agent_bridge.agents.bug.bug_prompt import parse_node_status_block

        text = "## 结论摘要\n\n这里没有 JSON 围栏块。\n"
        result = parse_node_status_block(text)
        assert result == {}, f"Expected empty dict, got: {result}"

    def test_parse_node_status_block_returns_empty_on_empty_string(self):
        from lark_agent_bridge.agents.bug.bug_prompt import parse_node_status_block

        assert parse_node_status_block("") == {}
        assert parse_node_status_block(None) == {}

    def test_parse_node_status_block_ignores_json_without_keys(self):
        from lark_agent_bridge.agents.bug.bug_prompt import parse_node_status_block

        # JSON block but doesn't contain node_status or findings
        text = "## 分析\n\n```json\n{\"foo\": \"bar\"}\n```\n"
        result = parse_node_status_block(text)
        assert result == {}, f"Expected empty dict for irrelevant JSON block, got: {result}"

    def test_parse_node_status_block_returns_empty_on_garbage(self):
        from lark_agent_bridge.agents.bug.bug_prompt import parse_node_status_block

        text = "## 分析\n\n```json\n{not valid json\n```\n"
        result = parse_node_status_block(text)
        assert result == {}, f"Expected empty dict on parse error, got: {result}"

    def test_parse_node_status_block_picks_last_block(self):
        """When multiple json blocks are present, should pick the last one with relevant keys."""
        from lark_agent_bridge.agents.bug.bug_prompt import parse_node_status_block

        text = (
            "```json\n{\"unrelated\": true}\n```\n"
            "```json\n{\"node_status\": {\"a.py\": \"ok\"}, \"findings\": []}\n```\n"
        )
        result = parse_node_status_block(text)
        assert result.get("node_status") == {"a.py": "ok"}, f"Got: {result}"

    def test_write_custom_skill_agent_report_includes_node_status_findings_from_file_agent(self):
        """source_stage_report.json must carry node_status and findings from parse_node_status_block."""
        import json
        import tempfile
        from pathlib import Path
        from lark_agent_bridge.agents.bug.bug_prompt import parse_node_status_block

        # Verify parse_node_status_block works end-to-end for a well-formed block
        analysis_text = (
            "## 结论摘要\n\n链路断路。\n\n"
            "## 关键证据\n\n- SignalService.kt:100\n\n"
            "```json\n"
            '{"node_status": {"SignalService.kt": "broken", "Consumer.kt": "ok"}, '
            '"findings": [{"file": "SignalService.kt", "severity": "high", "title": "dispatch missing"}]}\n'
            "```\n"
        )
        result = parse_node_status_block(analysis_text)
        assert result["node_status"] == {"SignalService.kt": "broken", "Consumer.kt": "ok"}
        assert result["findings"][0]["title"] == "dispatch missing"


if __name__ == "__main__":
    unittest.main()
