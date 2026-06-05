"""Tests for the autonomous-analysis ("AI 自主分析") HTML renderer.

Guards the redesign that fixed the old report: honest verdict severity (no
hardcoded green), markdown rendered as readable prose (no <pre>/raw markdown in
the visible body), a compact metadata strip, and a collapsed raw-evidence fold.
"""

import re

from lark_agent_bridge.reporting.app_server_report_html import (
    _markdown_to_html,
    _verdict_severity,
    render_app_server_report,
)

# Representative of a real app-server autonomous-analysis markdown: a skill-trigger
# meta line, an inconclusive conclusion that mentions "卡死" but cannot prove it,
# evidence subsections, a source-side judgement, an evidence gap, and actions.
SAMPLE_MD = """## 结论摘要

- 触发 `3d-stuck-investigate`，并补充 `scene-signal-diagnosis`。
- 当前日志**不能证明 montecarlo / Unity 渲染线程在 16:07 卡死**：缺少目标小时应用日志。
- 可确认的是：问题时间处于本地上电初期，更像是 P 挡场景显示策略差异。

## 关键证据

### 1. 目标时间处于本地上电初期

- `logd/main.txt.01:7350`：`Mcu ig status changed:1`
- `logd/main.txt.01:8602`：`ID_MCU_IG_STATUS event :1`

## 源码侧判断

### P/D 场景映射不同

- `UnitySceneTypeService.kt:217`：P 挡映射 IMMERSIVE_P。

## 证据缺口

- montecarlo 目标小时应用日志缺失，无法确认 Watchdog / UnityRequest。

## 最可能原因

当前可信度：中低。

最可能是**上电初期 P 挡 SR 场景显示未稳定**，缺少核心日志不能定到渲染层。

## 建议动作

1. 补采 `com.xiaopeng.montecarlo` 完整应用日志。
2. 重点搜索：
   - `XPEDriveSurfaceView`
   - `Watchdog kick`
"""


def _render(md=SAMPLE_MD, **meta_over):
    meta = {
        "bug_label": "BUG-1",
        "fault_time": "2026-04-26 16:07",
        "trigger_term": "自主分析",
        "selected_skill": "3d-stuck-investigate",
        "has_logs": True,
        "evidence_count": 2,
        "token_text": "输入 1 / 输出 2",
        "skill_count": 5,
        "context_path": "/ctx.json",
        "skill_inventory_path": "/inv.json",
        "context_payload": {"trigger_term": "自主分析"},
        "inventory_payload": {"skills": [{"name": "x", "description": "d"}]},
        "prepared_input": "/p",
        "focused_log_input": "/f",
        "request_text": "结合源码分析 P 挡 SR 是否卡死",
        "prompt_text": "",
        "description": "车机大屏在 P 挡 SR 页面无法显示",
    }
    meta.update(meta_over)
    return render_app_server_report(
        title="AI 自主分析报告", summary_markdown="", full_markdown=md, meta=meta
    )


def _visible_body(html_out: str) -> str:
    """The on-screen report body: between the container and the collapsed raw fold."""
    start = html_out.find('<div class="container">')
    end = html_out.find("展开原始与上下文证据")
    assert start >= 0 and end > start, "container / raw-fold markers missing"
    return html_out[start:end]


def test_verdict_reflects_low_confidence_not_green():
    # The sample is inconclusive (缺少核心日志 / 可信度中低): honest color is yellow,
    # never the old hardcoded green, and not a false red just because "卡死" appears.
    out = _render()
    match = re.search(r'<div class="verdict (v-\w+)">', out)
    assert match is not None
    assert match.group(1) == "v-yellow"


def test_verdict_panel_prioritizes_judgement_and_evidence_boundary():
    out = _render()
    assert '<div class="verdict-title">倾向：上电初期 P 挡 SR 场景显示未稳定</div>' in out
    assert "当前证据暂不能定性为 montecarlo / Unity 渲染线程卡死" in out
    assert "<b>已确认</b>" in out
    assert "问题时间处于本地上电初期，更像是 P 挡场景显示策略差异。" in out
    assert "<b>主要缺口</b>" in out
    assert "缺目标小时 montecarlo 应用日志" in out


def test_no_markdown_leak_in_visible_body():
    visible = _visible_body(_render())
    assert "## " not in visible
    assert "**" not in visible
    assert "`" not in visible  # inline code is converted to <code>, no raw backticks


def test_conclusion_is_prose_not_pre():
    visible = _visible_body(_render())
    assert '<div class="prose">' in visible
    assert "<pre>" not in visible  # raw <pre> lives only inside the collapsed fold


def test_visible_evidence_prioritizes_log_body_over_log_path():
    visible = _visible_body(_render())
    assert "logd/main.txt.01:7350" not in visible
    assert "logd/main.txt.01:8602" not in visible
    assert "Mcu ig status changed:1" in visible
    assert "ID_MCU_IG_STATUS event :1" in visible


def test_metadata_strip_present_and_skill_shown():
    out = _render()
    assert 'class="report-meta"' in out
    assert 'class="cards"' not in out
    assert "命中 Skill" in out
    assert "3d-stuck-investigate" in out
    assert "现场日志" in out
    assert "AI Token" in out


def test_chain_rendered_from_source_and_gap():
    out = _render()
    assert 'class="chain"' in out
    assert "证据缺口" in out


def test_raw_evidence_is_collapsed_fold():
    out = _render()
    assert 'details class="raw-fold"' in out
    assert "<pre>" in out  # the raw markdown is preserved, just inside the fold


def test_severity_paths():
    # confirmed fault + logs + no low-confidence signal -> red
    assert (
        _verdict_severity(
            summary_text="渲染线程卡死 Watchdog fatal 已坐实",
            cause_text="GPU 显存申请失败",
            has_logs=True,
            has_pending=False,
        )
        == "red"
    )
    # no logs -> yellow
    assert (
        _verdict_severity(summary_text="一切正常", cause_text="", has_logs=False, has_pending=False)
        == "yellow"
    )
    # low-confidence signal overrides scary fault words -> yellow
    assert (
        _verdict_severity(
            summary_text="不能证明卡死，可信度：中低，缺少核心日志",
            cause_text="",
            has_logs=True,
            has_pending=False,
        )
        == "yellow"
    )
    # confident conclusion + logs, no fault / no low-confidence -> green
    assert (
        _verdict_severity(
            summary_text="场景切换与渲染链路完整，结论明确",
            cause_text="",
            has_logs=True,
            has_pending=False,
        )
        == "green"
    )


def test_markdown_to_html_escapes_injection():
    out = _markdown_to_html("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_markdown_to_html_inline_formatting():
    out = _markdown_to_html("**粗体** 与 `代码`")
    assert "<strong>粗体</strong>" in out
    assert "<code>代码</code>" in out
    assert "**" not in out
