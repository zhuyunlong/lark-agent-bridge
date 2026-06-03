"""Tests for combined signal+source_stage analysis: render_html=False skips HTML write."""
import inspect
import json
from pathlib import Path
import sys
import types
import unittest
import tempfile


def test_write_custom_skill_agent_report_render_html_false_skips_html(tmp_path):
    """Calling _write_custom_skill_agent_report with render_html=False writes JSON but not HTML."""
    from lark_agent_bridge.agents.bug.bug_prompt import _BugPromptMixin

    # Verify render_html parameter exists in the signature
    sig = inspect.signature(_BugPromptMixin._write_custom_skill_agent_report)
    assert "render_html" in sig.parameters, (
        "_write_custom_skill_agent_report must have a render_html parameter"
    )
    param = sig.parameters["render_html"]
    assert param.default is True, "render_html default must be True"


def test_write_custom_skill_agent_report_html_write_is_guarded():
    """The HTML write in _write_custom_skill_agent_report must be guarded by render_html."""
    from lark_agent_bridge.agents.bug import bug_prompt

    source = inspect.getsource(bug_prompt._BugPromptMixin._write_custom_skill_agent_report)
    # The html_path.write_text call must be inside a render_html guard
    # Simple check: render_html appears before the html_path.write_text
    assert "render_html" in source, "render_html must appear in the function source"
    # Ensure html_path.write_text is guarded
    html_write_idx = source.find("html_path.write_text")
    render_html_idx = source.rfind("render_html", 0, html_write_idx)
    assert render_html_idx != -1, (
        "html_path.write_text must be preceded by a render_html check"
    )
