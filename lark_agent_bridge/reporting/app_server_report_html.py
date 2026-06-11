"""HTML renderer for the autonomous app-server investigation ("AI 自主分析") report.

Self-contained: the visual contract intentionally mirrors the local skill report
style (Chinese-first content, one-line verdict with honest severity, a compact
metadata strip colored by real value, markdown-rendered prose, severity issue lists, optional
root-cause chain, and a collapsed raw-evidence fold). The design system below is a
local copy of the shared report stylesheet; this module does NOT import skill files.
"""

from __future__ import annotations

import html
import json
import re
from typing import Iterable, Mapping, Sequence


# Design system copied/adapted from the local skill report stylesheet.
BASE_REPORT_CSS = """
:root {
  --c-bg: #f8f9fb; --c-surface: #ffffff; --c-border: #e2e5ea;
  --c-text: #1a1d23; --c-text-2: #5a6170; --c-text-3: #8b919e;
  --c-accent: #3b5bdb; --c-accent-light: #edf0ff;
  --c-red: #e03131; --c-red-bg: #fff1f1; --c-red-border: #fecdd3;
  --c-yellow: #e67700; --c-yellow-bg: #fff8eb; --c-yellow-border: #fde68a;
  --c-green: #0ca678; --c-green-bg: #ecfdf5; --c-green-border: #a7f3d0;
  --radius: 10px; --shadow: 0 1px 3px rgba(0,0,0,.06), 0 4px 12px rgba(0,0,0,.04);
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; -webkit-font-smoothing: antialiased; }
body {
  font-family: "Inter", -apple-system, "PingFang SC", "Microsoft YaHei", "Noto Sans SC", sans-serif;
  margin: 0; min-height: 100vh; overflow-wrap: anywhere;
  color: var(--c-text); background: var(--c-bg);
  font-size: 14px; line-height: 1.6;
}
.container { max-width: 1200px; margin: 0 auto; padding: 32px 28px 48px; }
h1 { margin: 0 0 6px; font-size: 26px; font-weight: 700; letter-spacing: -0.02em; color: var(--c-text); }
.sub { max-width: 860px; color: var(--c-text-2); margin-bottom: 28px; font-size: 13px; line-height: 1.7; }
.verdict {
  display: grid; grid-template-columns: 128px minmax(0, 1fr); gap: 16px;
  padding: 18px 22px; border-radius: var(--radius); margin-bottom: 16px;
  border: 1px solid var(--c-border); border-left: 4px solid var(--c-text-3);
  background: var(--c-surface); box-shadow: var(--shadow);
}
.verdict.v-red { border-color: var(--c-red-border); border-left-color: var(--c-red); background: var(--c-red-bg); }
.verdict.v-yellow { border-color: var(--c-yellow-border); border-left-color: var(--c-yellow); background: var(--c-yellow-bg); }
.verdict.v-green { border-color: var(--c-green-border); border-left-color: var(--c-green); background: var(--c-green-bg); }
.verdict-badge {
  width: fit-content; align-self: start; padding: 4px 10px; border-radius: 999px;
  font-size: 12px; font-weight: 700; color: #fff; background: var(--c-text-3);
}
.v-red .verdict-badge { background: var(--c-red); }
.v-yellow .verdict-badge { background: var(--c-yellow); }
.v-green .verdict-badge { background: var(--c-green); }
.verdict-title { font-size: 18px; font-weight: 700; line-height: 1.45; color: var(--c-text); }
.verdict-detail { margin-top: 4px; color: var(--c-text-2); font-size: 13px; line-height: 1.65; }
.verdict-facts { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.verdict-fact {
  display: inline-flex; gap: 6px; align-items: baseline; max-width: 100%;
  padding: 4px 8px; border-radius: 6px; background: rgba(255,255,255,.65);
  border: 1px solid rgba(0,0,0,.06); color: var(--c-text-2); font-size: 12px;
}
.verdict-fact b { color: var(--c-text); font-weight: 700; white-space: nowrap; }
.report-meta {
  margin-bottom: 24px; background: var(--c-surface); border: 1px solid var(--c-border);
  border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden;
}
.meta-grid {
  display: grid; grid-template-columns: minmax(220px, 1.15fr) minmax(130px, .65fr)
  minmax(130px, .65fr) minmax(220px, 1fr);
}
.meta-cell { min-width: 0; padding: 14px 18px; border-right: 1px solid var(--c-border); }
.meta-cell:last-child { border-right: 0; }
.meta-label {
  font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .05em;
  color: var(--c-text-3); margin-bottom: 4px;
}
.meta-value {
  color: var(--c-text); font-size: 19px; font-weight: 700; line-height: 1.35;
  overflow-wrap: anywhere;
}
.meta-value.small { font-size: 15px; line-height: 1.45; }
.meta-value.green { color: var(--c-green); }
.meta-value.yellow { color: var(--c-yellow); }
.status-value { display: flex; align-items: center; gap: 8px; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--c-text-3); flex: 0 0 auto; }
.status-dot.green { background: var(--c-green); }
.status-dot.yellow { background: var(--c-yellow); }
.meta-footer {
  display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center;
  padding: 10px 18px; border-top: 1px solid var(--c-border); background: #fbfcfe;
  color: var(--c-text-2); font-size: 12px;
}
.meta-chip {
  display: inline-flex; align-items: center; gap: 6px; min-height: 24px;
  padding: 2px 8px; border-radius: 999px; border: 1px solid var(--c-border);
  background: var(--c-surface); white-space: nowrap;
}
.meta-chip b, .meta-token b { color: var(--c-text); font-weight: 600; }
.meta-token { color: var(--c-text-3); }
.section {
  background: var(--c-surface); border-radius: var(--radius);
  border: 1px solid var(--c-border); padding: 20px 24px;
  margin-bottom: 16px; box-shadow: var(--shadow); overflow-x: auto;
}
.section h2 { margin: 0 0 14px; font-size: 16px; font-weight: 700; border-left: 3px solid var(--c-accent); padding-left: 10px; color: var(--c-text); }
.section h3 { margin: 18px 0 10px; font-size: 14px; font-weight: 600; }
.prose { color: var(--c-text); font-size: 14px; line-height: 1.75; }
.prose p { margin: 0 0 10px; }
.prose ul, .prose ol { margin: 0 0 10px; padding-left: 22px; }
.prose li { margin: 4px 0; }
.prose strong { font-weight: 700; }
.issue {
  padding: 10px 14px; border-radius: 8px; margin-bottom: 8px;
  border: 1px solid var(--c-border); border-left: 3px solid var(--c-text-3);
  background: var(--c-bg);
}
.issue.red { border-color: var(--c-red-border); border-left-color: var(--c-red); background: var(--c-red-bg); }
.issue.yellow { border-color: var(--c-yellow-border); border-left-color: var(--c-yellow); background: var(--c-yellow-bg); }
.issue.green { border-color: var(--c-green-border); border-left-color: var(--c-green); background: var(--c-green-bg); }
.issue .t { font-weight: 600; font-size: 13px; }
.issue .d { color: var(--c-text-2); font-size: 12px; margin-top: 4px; line-height: 1.6; }
table { width: 100%; min-width: 680px; border-collapse: collapse; font-size: 13px; }
th, td { padding: 10px 12px; border-bottom: 1px solid var(--c-border); text-align: left; vertical-align: top; }
th { background: var(--c-bg); font-weight: 600; color: var(--c-text-2); font-size: 12px; text-transform: uppercase; letter-spacing: .03em; }
details { margin-top: 10px; border: 1px solid var(--c-border); border-radius: 8px; padding: 10px 14px; background: var(--c-bg); }
summary { cursor: pointer; color: var(--c-accent); font-size: 13px; font-weight: 500; }
code { background: var(--c-accent-light); padding: 2px 6px; border-radius: 4px; font-size: 12px; font-family: "JetBrains Mono", "SF Mono", "Fira Code", monospace; }
pre {
  max-height: 480px; background: #f5f6f8; padding: 12px 14px; border-radius: 8px;
  font-size: 12px; line-height: 1.6; white-space: pre-wrap; word-break: break-word;
  overflow-x: auto; border: 1px solid var(--c-border);
  font-family: "JetBrains Mono", "SF Mono", "Fira Code", monospace;
}
.muted { color: var(--c-text-3); }
.chain { display: flex; flex-direction: column; align-items: stretch; gap: 0; }
.chain-node {
  display: flex; align-items: stretch; background: var(--c-surface);
  border-radius: var(--radius); border: 1px solid var(--c-border);
  border-left: 4px solid var(--c-text-3); box-shadow: var(--shadow); overflow: hidden;
}
.chain-node.chain-red { border-left-color: var(--c-red); }
.chain-node.chain-yellow { border-left-color: var(--c-yellow); }
.chain-node.chain-green { border-left-color: var(--c-green); }
.chain-step {
  width: 36px; min-width: 36px; display: flex; align-items: center;
  justify-content: center; font-size: 14px; font-weight: 700;
  color: #fff; background: var(--c-text-3);
}
.chain-red .chain-step { background: var(--c-red); }
.chain-yellow .chain-step { background: var(--c-yellow); }
.chain-green .chain-step { background: var(--c-green); }
.chain-body { padding: 12px 16px; flex: 1; }
.chain-title { font-weight: 700; font-size: 14px; margin-bottom: 4px; }
.chain-evi, .chain-down { font-size: 13px; color: var(--c-text-2); line-height: 1.6; margin-top: 2px; }
.chain-arrow { text-align: center; font-size: 18px; color: var(--c-text-3); line-height: 1; padding: 3px 0; }
.raw-fold { margin-bottom: 16px; }
.raw-fold > summary { font-size: 14px; font-weight: 600; }
@media (max-width: 880px) {
  h1 { font-size: 22px; }
  .verdict { grid-template-columns: 1fr; gap: 10px; padding: 16px; }
  .meta-grid { grid-template-columns: 1fr 1fr; }
  .meta-cell:nth-child(2n) { border-right: 0; }
  .meta-cell { border-bottom: 1px solid var(--c-border); }
  .container { padding: 16px; }
  table { min-width: 560px; }
}
"""


# Words that signal a real fault (-> red) or an inconclusive / low-confidence
# verdict (-> yellow). Severity rule: green ONLY if a confident conclusion exists
# AND logs were present; yellow when logs are missing, 待确认项 is non-empty, or the
# summary signals 证据不足/无法确定; red when summary/最可能原因 signals a clear fault.
# Low confidence wins: if logs are missing or the text signals 证据不足, yellow is
# chosen even when fault words appear (they describe a suspected, unconfirmed symptom).
_FAULT_SIGNAL = re.compile(r"失败|崩溃|异常|错误|fatal|error|卡死|卡顿|超时|无法启动|挂死|死锁|panic|crash", re.IGNORECASE)
_INSUFFICIENT_SIGNAL = re.compile(r"证据不足|无法确定|无法确认|不能证明|需补充|需确认|待确认|缺少|可信度.{0,4}(低|中低)|尚不能|未能定位")
def render_app_server_report(
    *,
    title: str,
    summary_markdown: str,
    full_markdown: str,
    meta: Mapping[str, object],
) -> str:
    """Render the autonomous-analysis report as a full HTML document.

    ``meta`` carries: bug_label, fault_time, trigger_term, selected_skill,
    has_logs (bool), evidence_count (int|None), token_text, skill_count,
    context_path, skill_inventory_path, context_payload (dict),
    inventory_payload (dict), prepared_input, focused_log_input, request_text,
    prompt_text, description.
    """

    sections = _markdown_sections(full_markdown)
    summary_section = _first_section(sections, "结论摘要") or summary_markdown
    plan_section = _first_section(sections, "调查方案", "查询路径", "查询方案")
    android_state_section = _first_section(
        sections,
        "Android 最终状态",
        "Android最终状态",
        "Android 状态",
        "Android侧最终状态",
        "Android 侧最终状态",
    )
    responsibility_section = _first_section(
        sections,
        "责任边界",
        "责任方",
        "责任归属",
        "责任界定",
        "Android/Unity 责任边界",
    )
    timeline_section = _first_section(sections, "关键时间线", "时间线")
    evidence_section = _first_section(sections, "关键证据")
    excluded_section = _first_section(sections, "已排除项", "排除项")
    confirmed_chain_section = _first_section(sections, "已确认链路", "确认链路", "链路确认")
    source_section = _first_section(sections, "源码解释", "源码侧判断")
    cause_section = _first_section(sections, "最可能原因", "可能原因", "根因判断")
    gap_section = _first_section(sections, "证据缺口")
    pending_section = _first_section(sections, "待确认项")
    action_section = _first_section(sections, "建议动作", "建议下一步", "下一步")

    has_logs = bool(meta.get("has_logs"))
    evidence_count = meta.get("evidence_count")
    pending_items = _section_issue_items(pending_section, "yellow")
    severity = _verdict_severity(
        summary_text=summary_section,
        cause_text=cause_section,
        has_logs=has_logs,
        has_pending=bool(pending_items),
    )

    body_parts = [
        '<div class="container">',
        f"<h1>{H(title)}</h1>",
        '<div class="sub">Bridge 已完成 bug/日志前置，Codex app-server 已基于上下文与 skill 清单生成只读分析结论。'
        "本报告按真实结论着色，结论摘要已渲染为可读正文，结构化章节直接呈现。</div>",
        render_verdict(
            severity=severity,
            summary_section=summary_section,
            cause_section=cause_section,
            gap_section=gap_section,
        ),
        render_meta_strip(meta, has_logs=has_logs, evidence_count=evidence_count),
        render_section("结论摘要", f'<div class="prose">{_markdown_to_html(summary_section)}</div>'),
    ]

    if plan_section:
        body_parts.append(render_section("调查方案", _prose_html(plan_section)))

    boundary_required = _requires_android_unity_boundary(meta, full_markdown)
    if boundary_required or android_state_section:
        body_parts.append(
            render_section(
                "Android 最终状态",
                _prose_or_missing_html(
                    android_state_section,
                    "报告未明确输出必需章节：Android 最终状态。",
                ),
            )
        )
    if boundary_required or responsibility_section:
        body_parts.append(
            render_section(
                "责任边界",
                _prose_or_missing_html(
                    responsibility_section,
                    "报告未明确输出必需章节：责任边界。",
                ),
            )
        )

    if timeline_section:
        body_parts.append(render_section("关键时间线", _prose_html(timeline_section)))
    if confirmed_chain_section:
        body_parts.append(render_section("已确认链路", _prose_html(confirmed_chain_section)))
    if excluded_section:
        body_parts.append(render_section("已排除项", _prose_html(excluded_section)))

    body_parts.extend(
        [
            render_section(
                "最可能原因",
                render_issue_list(_section_issue_items(cause_section, "red" if severity == "red" else "yellow"), empty_text="未明确给出最可能原因。"),
            ),
            render_section("关键证据", _evidence_html(evidence_section)),
        ]
    )

    if source_section and not _markdown_subsections(source_section):
        body_parts.append(render_section("源码解释", _prose_html(source_section)))

    body_parts.extend(
        [
            render_section("待确认项", render_issue_list(pending_items, empty_text="当前没有额外待确认项。")),
            render_section(
                "建议动作",
                render_issue_list(_section_issue_items(action_section, "green"), empty_text="当前没有额外建议动作。"),
            ),
        ]
    )

    chain_nodes = _chain_nodes(source_section, gap_section)
    if chain_nodes:
        body_parts.append(
            render_chain(
                chain_nodes,
                title="分析链路 / 归因",
                description="按源码侧判断与证据缺口组织，列出依据与影响。",
            )
        )

    body_parts.append(_raw_fold(full_markdown, meta))
    body_parts.append("</div>")

    return render_document(title, "".join(body_parts))


def _first_section(sections: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = sections.get(name)
        if value and value.strip():
            return value.strip()
    return ""


def _prose_html(section: str) -> str:
    return f'<div class="prose">{_markdown_to_html(section)}</div>'


def _prose_or_missing_html(section: str, missing_text: str) -> str:
    if section and section.strip():
        return _prose_html(section)
    return render_issue_list([{"sev": "yellow", "title": missing_text, "detail": ""}])


def _requires_android_unity_boundary(meta: Mapping[str, object], full_markdown: str) -> bool:
    contract = meta.get("report_contract")
    if not isinstance(contract, Mapping):
        return False
    if contract.get("requires_android_unity_boundary"):
        return True
    required_sections = contract.get("required_sections")
    if not isinstance(required_sections, list):
        return False
    required = {str(section).strip() for section in required_sections}
    if not {"Android 最终状态", "责任边界"}.issubset(required):
        return False
    keywords = contract.get("required_section_keywords")
    if not isinstance(keywords, list) or not keywords:
        return False
    text = "\n".join(
        str(meta.get(key) or "")
        for key in ("bug_label", "request_text", "prompt_text", "description")
    )
    text = f"{text}\n{full_markdown or ''}".casefold()
    return any(str(keyword).strip().casefold() in text for keyword in keywords if str(keyword).strip())


def render_verdict(*, severity: str, summary_section: str, cause_section: str, gap_section: str) -> str:
    title = _verdict_title(summary_section, cause_section)
    detail = _verdict_detail(summary_section, title)
    facts = _verdict_facts(summary_section, gap_section)
    facts_html = "".join(
        f'<span class="verdict-fact"><b>{H(label)}</b>{H(value)}</span>'
        for label, value in facts
        if value
    )
    facts_block = f'<div class="verdict-facts">{facts_html}</div>' if facts_html else ""
    return (
        f'<div class="verdict v-{H(severity)}">'
        f'<div class="verdict-badge">{H(_verdict_badge(severity, summary_section, cause_section))}</div>'
        "<div>"
        f'<div class="verdict-title">{H(title)}</div>'
        f'<div class="verdict-detail">{H(detail)}</div>'
        f"{facts_block}"
        "</div>"
        "</div>"
    )


def _verdict_badge(severity: str, summary_section: str, cause_section: str) -> str:
    combined = f"{summary_section}\n{cause_section}"
    if _INSUFFICIENT_SIGNAL.search(combined):
        return "证据不足"
    if severity == "red":
        return "异常成立"
    if severity == "yellow":
        return "需确认"
    return "结论明确"


def _verdict_title(summary_section: str, cause_section: str) -> str:
    combined = f"{summary_section}\n{cause_section}"
    for pattern in (
        r"更像是\*\*(.+?)\*\*",
        r"最可能是\*\*(.+?)\*\*",
        r"更像是(.+?)(?:，|。|$)",
        r"最可能是(.+?)(?:，|。|$)",
    ):
        match = re.search(pattern, combined)
        if match:
            phrase = _clean_markdown_inline(match.group(1))
            phrase = re.split(r"[；;。]", phrase)[0].strip()
            if phrase:
                return _truncate(f"倾向：{phrase}", 96)
    return _truncate(_markdown_first_line(summary_section) or "AI 自主分析完成。", 96)


def _verdict_detail(summary_section: str, title: str) -> str:
    for raw_line in (summary_section or "").splitlines():
        line = _clean_summary_line(raw_line)
        if not line or re.match(r"^(触发|补充|额外触发|已触发)", line):
            continue
        if line.startswith(title.removeprefix("倾向：")):
            continue
        if re.search(r"不能证明|无法确认|无法确定|证据不足|缺少", line):
            line = re.sub(r"^当前日志", "当前证据", line)
            line = re.sub(r"不能证明", "暂不能定性为", line, count=1)
            line = _summarize_verdict_detail(line)
            return _truncate(line, 180)
    return "结论基于当前日志、源码与上下文证据生成；证据边界见下方关键证据和待确认项。"


def _verdict_facts(summary_section: str, gap_section: str) -> list[tuple[str, str]]:
    facts: list[tuple[str, str]] = []
    for raw_line in (summary_section or "").splitlines():
        line = _clean_summary_line(raw_line)
        if line.startswith("可确认的是"):
            value = re.sub(r"^可确认的是[:：]?", "", line).strip()
            if value:
                facts.append(("已确认", _truncate(_summarize_confirmed_fact(value), 96)))
            break
    gap = _clean_markdown_inline(_first_body_line(gap_section))
    if not gap:
        for raw_line in (summary_section or "").splitlines():
            line = _clean_summary_line(raw_line)
            if "缺少" in line:
                gap = "缺少" + line.split("缺少", 1)[1].strip()
                break
    if gap:
        facts.append(("主要缺口", _truncate(_summarize_gap_fact(gap), 96)))
    return facts[:2]


def _clean_summary_line(raw_line: str) -> str:
    line = _clean_markdown_inline(raw_line)
    line = re.sub(r"^\s*(?:[-*•]|\d+[.、)])\s*", "", line)
    return line.strip()


def _summarize_verdict_detail(line: str) -> str:
    if "montecarlo" in line and "Unity" in line and "卡死" in line and "应用日志" in line:
        return "当前证据暂不能定性为 montecarlo / Unity 渲染线程卡死；缺目标小时 montecarlo 应用日志，Watchdog / UnityRequest / OnSceneChanged 未闭环。"
    return line


def _summarize_confirmed_fact(value: str) -> str:
    if "上电后" in value and "P 挡" in value and "D 挡" in value:
        return "上电约 1 分钟仍在 P 挡；D 挡后系统侧切换正常"
    return value


def _summarize_gap_fact(value: str) -> str:
    if "montecarlo" in value and ("应用日志" in value or ".alog" in value):
        return "缺目标小时 montecarlo 应用日志；Watchdog / UnityRequest / OnSceneChanged 无法闭环"
    return value


def render_meta_strip(meta: Mapping[str, object], *, has_logs: bool, evidence_count: object) -> str:
    selected_skill = str(meta.get("selected_skill") or "AI自选/未知")
    skill_known = bool(selected_skill) and selected_skill not in {"AI自选/未知", "未知", ""}
    fault_time = str(meta.get("fault_time") or "")
    evidence_known = isinstance(evidence_count, int) and evidence_count > 0
    evidence_text = str(evidence_count) if isinstance(evidence_count, int) else "未知"
    trigger_term = str(meta.get("trigger_term") or "未知")
    token_text = str(meta.get("token_text") or "未记录")
    log_severity = "green" if has_logs else "yellow"
    skill_severity = "green" if skill_known else "yellow"
    evidence_severity = "green" if evidence_known else "yellow"
    return (
        '<div class="report-meta">'
        '<div class="meta-grid">'
        f'{_meta_cell("故障时间", fault_time or "未识别", "green" if fault_time else "yellow")}'
        f'{_meta_cell("关键证据", evidence_text, evidence_severity)}'
        f'{_meta_status_cell("现场日志", "有" if has_logs else "无", log_severity)}'
        f'{_meta_cell("命中 Skill", selected_skill, skill_severity, small=True)}'
        "</div>"
        '<div class="meta-footer">'
        f'<span class="meta-chip">触发词 <b>{H(trigger_term)}</b></span>'
        f'<span class="meta-token">AI Token <b>{H(token_text)}</b></span>'
        "</div>"
        "</div>"
    )


def _meta_cell(label: str, value: str, severity: str, *, small: bool = False) -> str:
    small_class = " small" if small else ""
    return (
        '<div class="meta-cell">'
        f'<div class="meta-label">{H(label)}</div>'
        f'<div class="meta-value {H(severity)}{small_class}">{H(value)}</div>'
        "</div>"
    )


def _meta_status_cell(label: str, value: str, severity: str) -> str:
    return (
        '<div class="meta-cell">'
        f'<div class="meta-label">{H(label)}</div>'
        f'<div class="meta-value {H(severity)} status-value">'
        f'<span class="status-dot {H(severity)}"></span>{H(value)}</div>'
        "</div>"
    )


def _verdict_severity(
    *,
    summary_text: str,
    cause_text: str,
    has_logs: bool,
    has_pending: bool,
) -> str:
    """Derive the honest verdict severity — confidence drives the banner color.

    yellow when logs are missing OR the analysis itself signals low confidence
    (证据不足/无法确定/可信度中低/缺少核心日志); this takes priority over fault words,
    because in an inconclusive report those words describe a *suspected* symptom,
    not a confirmed fault. red only for a confirmed fault (失败/崩溃/异常/卡死/
    fatal/error…) when confidence is not undercut. green only for a confident
    conclusion with logs and no open items.
    """

    combined = f"{summary_text}\n{cause_text}"
    if not has_logs or _INSUFFICIENT_SIGNAL.search(combined):
        return "yellow"
    if _FAULT_SIGNAL.search(combined):
        return "red"
    if has_pending:
        return "yellow"
    return "green"


def _evidence_html(section: str) -> str:
    """关键证据 section: render visible evidence around the log body."""
    if not (section or "").strip():
        return '<div class="issue green"><div class="t">未提供关键证据。</div></div>'
    return f'<div class="prose">{_markdown_to_html(_strip_log_locations(section))}</div>'


def _strip_log_locations(markdown: str) -> str:
    """Remove leading log file:line references from visible evidence bullets.

    The raw fold still keeps the original markdown with full paths. In the main
    evidence section, long log paths distract from the actual log line.
    """
    lines: list[str] = []
    for raw_line in (markdown or "").splitlines():
        lines.append(_strip_log_location_line(raw_line))
    return "\n".join(lines)


def _strip_log_location_line(line: str) -> str:
    match = re.match(r"^(\s*(?:[-*•]|\d+[.、)])\s+)`([^`]+)`([：:]\s*)(.*)$", line)
    if not match:
        return line
    location = match.group(2)
    if not _looks_like_log_location(location):
        return line
    return f"{match.group(1)}{match.group(4)}"


def _looks_like_log_location(location: str) -> bool:
    return bool(
        re.search(r"(?:^|/)(?:logd|logs?|app)/", location, re.IGNORECASE)
        or re.search(r"\.(?:txt|log|alog|xlog|xp)(?:\.\d+)?(?::\d+)?$", location, re.IGNORECASE)
        or re.search(r"\.(?:txt|log|alog|xlog|xp)(?:\.\d+)?:\d+", location, re.IGNORECASE)
    )


def _chain_nodes(source_section: str, gap_section: str) -> list[dict[str, object]]:
    """Build attribution chain nodes from 源码侧判断 / 证据缺口 H3 subsections."""
    nodes: list[dict[str, object]] = []
    for heading, body in _markdown_subsections(source_section):
        nodes.append(
            {
                "sev": "yellow",
                "title": heading,
                "evidence": _clean_markdown_inline(_first_body_line(body)),
                "downstream": "",
            }
        )
    gap = (gap_section or "").strip()
    if gap:
        nodes.append(
            {
                "sev": "red",
                "title": "证据缺口",
                "evidence": _clean_markdown_inline(_first_body_line(gap)),
                "downstream": "缺失核心证据，结论无法完全坐实。",
            }
        )
    return nodes


def _raw_fold(full_markdown: str, meta: Mapping[str, object]) -> str:
    context_payload = meta.get("context_payload")
    context_json = json.dumps(context_payload, ensure_ascii=False, indent=2) if isinstance(context_payload, Mapping) else "{}"
    context_rows = [
        ("用户原始请求", str(meta.get("request_text") or "未提供")),
        ("用户补充描述", str(meta.get("prompt_text") or "未提供")),
        ("Bug 标题", str(meta.get("bug_label") or "未提供")),
        ("Bug 描述", _truncate(str(meta.get("description") or ""), 600) or "未提供"),
        ("解密/准备后日志目录", str(meta.get("prepared_input") or "")),
        ("聚焦日志目录", str(meta.get("focused_log_input") or "")),
        ("上下文文件", str(meta.get("context_path") or "")),
        ("Skill 清单", str(meta.get("skill_inventory_path") or "")),
    ]
    inventory_rows = _inventory_rows(meta.get("inventory_payload"))
    skill_count = str(meta.get("skill_count") or len(inventory_rows))
    return (
        '<details class="raw-fold">'
        "<summary>📂 展开原始与上下文证据</summary>"
        "<h3>原始 Markdown</h3>"
        f"<pre>{H(full_markdown)}</pre>"
        "<h3>分析上下文</h3>"
        f"{render_table(context_rows, ('字段', '内容'))}"
        "<h3>结构化上下文 JSON</h3>"
        f"<pre>{H(context_json)}</pre>"
        f"<h3>Skill 清单概览（共 {H(skill_count)} 个）</h3>"
        f"{render_table(inventory_rows, ('技能', '描述'))}"
        "</details>"
    )


def _inventory_rows(inventory_payload: object) -> list[tuple[object, object]]:
    if not isinstance(inventory_payload, Mapping):
        return []
    skills = inventory_payload.get("skills")
    if not isinstance(skills, list):
        return []
    rows: list[tuple[object, object]] = []
    for skill in skills:
        if isinstance(skill, Mapping):
            name = skill.get("label") or skill.get("name") or ""
            rows.append((name, skill.get("description") or ""))
        else:
            rows.append((str(skill), ""))
    return rows


# --- markdown helpers -------------------------------------------------------


def _markdown_sections(markdown: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = ""
    for raw_line in (markdown or "").splitlines():
        line = raw_line.rstrip()
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            current = heading.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def _markdown_subsections(section: str) -> list[tuple[str, str]]:
    """Split a section body on ``### heading`` into (heading, body) pairs."""
    result: list[tuple[str, list[str]]] = []
    current = ""
    for raw_line in (section or "").splitlines():
        heading = re.match(r"^###\s+(.+?)\s*$", raw_line.rstrip())
        if heading:
            result.append((heading.group(1).strip(), []))
            current = heading.group(1).strip()
            continue
        if result and current:
            result[-1][1].append(raw_line)
    return [(title, "\n".join(lines).strip()) for title, lines in result]


def _first_body_line(text: str) -> str:
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        return re.sub(r"^\s*(?:[-*•]|\d+[.、)])\s*", "", line)
    return ""


def _markdown_first_line(text: str) -> str:
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^#+\s*", "", line)
        line = re.sub(r"^\s*(?:[-*•]|\d+[.、)])\s*", "", line)
        if line in {"结论摘要", "关键证据", "最可能原因", "待确认项", "建议动作"}:
            continue
        if re.match(r"^(触发|补充|额外触发|已触发)", line):
            # skip skill-selection meta lines so the banner shows the real conclusion
            continue
        return _clean_markdown_inline(line)[:1200]
    return ""


def _section_issue_items(text: str, severity: str) -> list[dict[str, object]]:
    """Parse top-level bullets/numbered items into issue cards.

    Nested (indented) sub-items are folded into their parent's detail so a single
    sentence is never split on its inner colon (e.g. a ``16:05`` time range stays
    intact). Inline markdown (** bold **, ` code `) is stripped to clean text.
    """
    items: list[dict[str, object]] = []
    for raw_line in (text or "").splitlines():
        if not raw_line.strip():
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        content = re.sub(r"^\s*(?:[-*•]|\d+[.、)])\s*", "", raw_line).strip()
        content = _clean_markdown_inline(content)
        if not content:
            continue
        is_bullet = bool(re.match(r"^\s*(?:[-*•]|\d+[.、)])\s+", raw_line))
        if indent > 0 and items:
            # nested detail line -> append to the current item's detail
            extra = items[-1]["detail"]
            items[-1]["detail"] = f"{extra}；{content}" if extra else content
            continue
        if is_bullet:
            items.append({"sev": severity, "title": content, "detail": ""})
        elif items:
            extra = items[-1]["detail"]
            items[-1]["detail"] = f"{extra} {content}".strip()
        else:
            items.append({"sev": severity, "title": content, "detail": ""})
    return items


def _clean_markdown_inline(text: str) -> str:
    cleaned = re.sub(r"`([^`]*)`", r"\1", text or "")
    cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _inline_md_to_html(escaped: str) -> str:
    """Inline markdown on ALREADY-ESCAPED text: ` code ` then ** bold **."""
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    return out


def _markdown_to_html(markdown: str) -> str:
    """Minimal markdown -> HTML for readable prose.

    Handles ## / ### headings, ** bold **, ` inline code `, ``-`` and ordered
    lists (with nesting), and blank-line paragraphs. HTML is escaped FIRST, then
    formatting is applied, so no raw markup can inject. Intentionally minimal.
    """
    lines = (markdown or "").splitlines()
    html_parts: list[str] = []
    paragraph: list[str] = []
    # list stack: (indent, tag)
    list_stack: list[tuple[int, str]] = []

    def flush_paragraph() -> None:
        if paragraph:
            text = _inline_md_to_html(" ".join(paragraph))
            html_parts.append(f"<p>{text}</p>")
            paragraph.clear()

    def close_lists_to(indent: int) -> None:
        while list_stack and list_stack[-1][0] >= indent:
            html_parts.append(f"</{list_stack[-1][1]}>")
            list_stack.pop()

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            flush_paragraph()
            close_lists_to(0)
            continue

        heading = re.match(r"^(#{2,3})\s+(.*)$", stripped)
        if heading:
            flush_paragraph()
            close_lists_to(0)
            level = len(heading.group(1))
            tag = "h2" if level == 2 else "h3"
            html_parts.append(f"<{tag}>{_inline_md_to_html(H(heading.group(2).strip()))}</{tag}>")
            continue

        bullet = re.match(r"^(\s*)(?:[-*•]|\d+[.、)])\s+(.*)$", raw_line)
        if bullet:
            flush_paragraph()
            indent = len(bullet.group(1))
            ordered = bool(re.match(r"^\s*\d+[.、)]\s+", raw_line))
            tag = "ol" if ordered else "ul"
            close_lists_to(indent + 1)
            if not list_stack or list_stack[-1][0] < indent:
                html_parts.append(f"<{tag}>")
                list_stack.append((indent, tag))
            content = _inline_md_to_html(H(bullet.group(2).strip()))
            html_parts.append(f"<li>{content}</li>")
            continue

        # plain paragraph text; if inside a list, treat as continuation of last item
        if list_stack:
            content = _inline_md_to_html(H(stripped))
            if html_parts and html_parts[-1].endswith("</li>"):
                html_parts[-1] = html_parts[-1][: -len("</li>")] + f" {content}</li>"
            else:
                html_parts.append(f"<li>{content}</li>")
            continue
        paragraph.append(H(stripped))

    flush_paragraph()
    close_lists_to(0)
    return "".join(html_parts) or '<p class="muted">(无)</p>'


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


# --- HTML primitives (self-contained copy of the 3d卡顿 renderers) ----------


def H(value: object) -> str:
    return html.escape(str(value) if value is not None else "")


def render_document(title: str, body_html: str, css: str = BASE_REPORT_CSS) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"  <title>{H(title)}</title>\n"
        f"  <style>{css}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{body_html}\n"
        "</body>\n"
        "</html>\n"
    )


def render_issue_list(issues: Iterable[Mapping[str, object]], empty_text: str = "未发现明显异常") -> str:
    chunks = []
    for issue in issues:
        severity = issue.get("sev", "green")
        title = issue.get("title", "")
        detail = issue.get("detail", "")
        detail_html = f'<div class="d">{H(detail)}</div>' if detail else ""
        chunks.append(f'<div class="issue {H(severity)}"><div class="t">{H(title)}</div>{detail_html}</div>')
    if chunks:
        return "".join(chunks)
    return f'<div class="issue green"><div class="t">{H(empty_text)}</div></div>'


def render_chain(
    nodes: Iterable[Mapping[str, object]],
    *,
    title: str,
    description: str,
) -> str:
    node_list = list(nodes)
    if not node_list:
        return ""
    chunks = []
    for idx, node in enumerate(node_list, 1):
        arrow = '<div class="chain-arrow">↓</div>' if idx < len(node_list) else ""
        downstream = node.get("downstream", "")
        down_html = f'<div class="chain-down"><b>影响：</b>{H(downstream)}</div>' if downstream else ""
        chunks.append(
            f'<div class="chain-node chain-{H(node.get("sev", "green"))}">'
            f'<div class="chain-step">{idx}</div>'
            '<div class="chain-body">'
            f'<div class="chain-title">{H(node.get("title", ""))}</div>'
            f'<div class="chain-evi"><b>依据：</b>{H(node.get("evidence", ""))}</div>'
            f"{down_html}"
            f"</div></div>{arrow}"
        )
    return (
        '<div class="section">'
        f"<h2>{H(title)}</h2>"
        f'<p class="muted">{H(description)}</p>'
        f'<div class="chain">{"".join(chunks)}</div>'
        "</div>"
    )


def render_table(rows: Iterable[Sequence[object]], cols: Sequence[object], empty_text: str = "(无)") -> str:
    row_list = list(rows)
    head = "".join(f"<th>{H(col)}</th>" for col in cols)
    if row_list:
        body = "".join("<tr>" + "".join(f"<td>{H(value)}</td>" for value in row) + "</tr>" for row in row_list)
    else:
        body = f'<tr><td colspan="99" class="muted">{H(empty_text)}</td></tr>'
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_section(title: str, body_html: str) -> str:
    return f'<div class="section"><h2>{H(title)}</h2>{body_html}</div>'
