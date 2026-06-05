"""Tests for bridge-owned HTML report primitives."""

from __future__ import annotations

from lark_agent_bridge.reporting.combined_bug_html import render_cards


def test_render_cards_compacts_long_values_and_paths():
    html = render_cards(
        [
            ("信号", "16042", "green", "SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE"),
            (
                "焦点进程",
                "com.xiaopeng.montecarlo",
                "green",
                "PID 11311",
            ),
            (
                "原始输入",
                "/Users/zhuyl/Documents/workspace/tools/lark-agent-bridge/data/bug_cache/xpfailuremgmt_6995163459/attachments/20260519.7z",
                "green",
                "",
            ),
        ]
    )

    assert 'class="report-meta"' in html
    assert 'class="meta-cell green">' in html
    assert 'class="meta-cell green meta-compact">' in html
    assert "20260519.7z" in html
    assert "data/bug_cache/xpfailuremgmt_6995163459" in html


def test_render_cards_does_not_treat_human_slash_text_as_path():
    text = "目标信号已进入 DataCenter，但业务消费证据不足（com.xiaopeng.montecarlo / PID 11311）。"

    html = render_cards([("进程内可见性", text, "yellow", "")])

    assert '<div class="meta-value">目标信号已进入 DataCenter' in html
    assert '<div class="meta-value"> PID 11311）。</div>' not in html
