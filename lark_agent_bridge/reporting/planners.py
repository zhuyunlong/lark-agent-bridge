"""Section planners for bridge-owned reports."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .composition import ReportComposition, ReportSection, ReportVerdict

SUMMARY_EVIDENCE_COLS = ["证据类型", "定位", "对象", "原始现象", "支持判断"]


def build_structured_summary_sections(
    *,
    conclusions: list[dict[str, object]],
    evidence_rows: list[tuple[object, ...]] | None = None,
    causes: list[dict[str, object]] | None = None,
    confirmations: list[dict[str, object]] | None = None,
    actions: list[dict[str, object]] | None = None,
) -> list[ReportSection]:
    """Build the common conclusion-first summary sections for bridge reports."""

    return [
        ReportSection(
            kind="issues",
            title="结论摘要",
            items=_clean_items(conclusions),
            empty_text="当前证据不足，暂无法形成稳定结论。",
        ),
        ReportSection(
            kind="table",
            title="关键证据",
            rows=_fit_evidence_rows(evidence_rows or []),
            cols=SUMMARY_EVIDENCE_COLS,
            empty_text="未提取到可直接支撑结论的关键证据。",
        ),
        ReportSection(
            kind="issues",
            title="最可能原因",
            items=_clean_items(causes or []),
            empty_text="当前证据不足，暂不强行归因。",
        ),
        ReportSection(
            kind="issues",
            title="待确认项",
            items=_clean_items(confirmations or []),
            empty_text="当前摘要层未识别额外待确认项。",
        ),
        ReportSection(
            kind="issues",
            title="建议动作",
            items=_clean_items(actions or []),
            empty_text="当前摘要层未生成额外动作建议。",
        ),
    ]


def _clean_items(items: list[dict[str, object]]) -> list[dict[str, object]]:
    cleaned: list[dict[str, object]] = []
    for item in items:
        title = str(item.get("title") or "").strip()
        detail = str(item.get("detail") or "").strip()
        if not title and not detail:
            continue
        cleaned.append(
            {
                "sev": str(item.get("sev") or "green"),
                "title": title or detail,
                "detail": detail,
            }
        )
    return cleaned


def _fit_evidence_rows(rows: list[tuple[object, ...]]) -> list[tuple[object, ...]]:
    fitted: list[tuple[object, ...]] = []
    for row in rows:
        values = tuple(row)
        if not any(str(value).strip() for value in values):
            continue
        if len(values) >= len(SUMMARY_EVIDENCE_COLS):
            fitted.append(values[: len(SUMMARY_EVIDENCE_COLS)])
        else:
            fitted.append(values + ("",) * (len(SUMMARY_EVIDENCE_COLS) - len(values)))
    return fitted


def plan_startup_stuck_report(
    *,
    prompt_text: str,
    fault_time: str,
    startup_html_name: str,
    stuck_html_name: str,
    selected_input: str,
    startup_message: str,
    startup_sev: str,
    stuck_message: str,
    stuck_sev: str,
    focus_pid: str,
    focus_session_index: str,
    boot_relation_is_near_boot: bool,
    boot_relation_note: str,
    startup_load_value: str,
    startup_load_desc: str,
    startup_load_sev: str,
    issues: list[dict[str, object]],
    chain_nodes: list[dict[str, object]],
    target_rows: list[tuple[object, ...]],
    system_rows: list[tuple[object, ...]],
    system_cols: list[str],
) -> ReportComposition:
    summary_sections = build_structured_summary_sections(
        conclusions=[
            {"sev": startup_sev, "title": "启动链路", "detail": startup_message},
            {"sev": stuck_sev, "title": "卡顿窗口", "detail": stuck_message},
            {
                "sev": "yellow" if boot_relation_is_near_boot else "green",
                "title": "ROM 启动邻近性",
                "detail": boot_relation_note,
            },
        ],
        evidence_rows=_startup_stuck_evidence_rows(
            fault_time=fault_time,
            focus_pid=focus_pid,
            target_rows=target_rows,
            system_rows=system_rows,
            startup_load_value=startup_load_value,
            startup_load_desc=startup_load_desc,
        ),
        causes=_cause_items_from_chain(chain_nodes),
        confirmations=_startup_stuck_confirmations(
            fault_time=fault_time,
            focus_pid=focus_pid,
            system_rows=system_rows,
            issues=issues,
        ),
        actions=_startup_stuck_actions(
            startup_sev=startup_sev,
            stuck_sev=stuck_sev,
            chain_nodes=chain_nodes,
        ),
    )
    cards = [
        ("分析类型", "3D启动卡顿综合", "green", "启动链路与卡顿窗口合并输出"),
        ("故障时间", fault_time or "未识别", "green" if fault_time else "yellow", ""),
        ("主会话 PID", focus_pid or "未识别", "green" if focus_pid else "yellow", f"Session {focus_session_index or '?'}"),
        ("ROM 启动邻近", "是" if boot_relation_is_near_boot else "否", "yellow" if boot_relation_is_near_boot else "green", boot_relation_note),
        ("启动时刻系统负载", startup_load_value, startup_load_sev, startup_load_desc),
        ("启动链路", startup_sev.upper(), startup_sev, startup_message),
        ("卡顿窗口", stuck_sev.upper(), stuck_sev, stuck_message),
    ]
    return ReportComposition(
        title="3D启动卡顿综合报告",
        heading="3D启动卡顿综合报告",
        subtitle=(
            f"分析请求：<code>{prompt_text}</code><br/>"
            f"启动报告：<code>{startup_html_name}</code> · "
            f"卡顿报告：<code>{stuck_html_name}</code>"
        ),
        verdict=ReportVerdict(
            sev=startup_sev if startup_sev == "red" else stuck_sev,
            text=f"🎯 {startup_message}；{stuck_message}",
        ),
        cards=cards,
        sections=summary_sections
        + [
            ReportSection(kind="issues", title="异常摘要", items=issues, empty_text="未发现明显异常"),
            ReportSection(
                kind="chain",
                title="启动卡顿综合链路",
                description="先锁主会话，再串 ROM/上下电/启动链/卡顿窗口系统压力。",
                nodes=chain_nodes,
            ),
            ReportSection(kind="table", title="目标时间窗与主会话", rows=target_rows, cols=["项目", "内容"]),
            ReportSection(
                kind="table",
                title="启动时刻系统负载",
                rows=system_rows,
                cols=system_cols,
                empty_text="未命中 DFX-SystemMonitor 同时间窗样本",
            ),
            ReportSection(
                kind="text",
                title="单报告说明",
                class_name="muted",
                text="底层仍保留单独的 startup / stuck 报告，当前综合报告只负责把“启动链路异常”和“卡顿窗口证据”收口成一份可上传的 HTML。",
            ),
        ],
    )


def plan_signal_report(
    *,
    title_suffix: str,
    prompt_text: str,
    raw_signal_report_name: str,
    verdict_sev: str,
    verdict_text: str,
    judgement_text: str,
    cards: list[tuple[object, ...]],
    lifecycle_nodes: list[dict[str, object]],
    dataflow_nodes: list[dict[str, object]],
    evidence_rows: list[tuple[object, ...]],
    boundary_issues: list[dict[str, object]],
    summary_text: str,
    source_rows: list[tuple[object, ...]],
    render_summary_html: Callable[[str], str],
    render_source_rows_html: Callable[[list[tuple[object, ...]]], str],
) -> ReportComposition:
    sections = build_structured_summary_sections(
        conclusions=[
            {"sev": verdict_sev, "title": "链路判断", "detail": judgement_text or verdict_text},
            {"sev": verdict_sev, "title": "结论边界", "detail": verdict_text},
        ],
        evidence_rows=_signal_summary_evidence_rows(
            evidence_rows=evidence_rows,
            source_rows=source_rows,
            judgement_text=judgement_text,
        ),
        causes=_signal_cause_items(
            lifecycle_nodes=lifecycle_nodes,
            dataflow_nodes=dataflow_nodes,
            summary_text=summary_text,
            verdict_sev=verdict_sev,
        ),
        confirmations=boundary_issues,
        actions=_signal_action_items(
            evidence_rows=evidence_rows,
            source_rows=source_rows,
            boundary_issues=boundary_issues,
        ),
    ) + [
        ReportSection(
            kind="flow",
            title="生命周期流图",
            description="只围绕当前可见的 package / PID 组织，避免把别的进程混进来。",
            nodes=lifecycle_nodes,
        ),
        ReportSection(
            kind="flow",
            title="数据流图",
            description="先看源码映射和分发，再看业务消费与最终判定落点。",
            nodes=dataflow_nodes,
        ),
        ReportSection(
            kind="table",
            title="关键日志证据",
            rows=evidence_rows,
            cols=["时间", "相对启动", "进程/线程", "阶段", "证据"],
            empty_text="未提取到可用日志证据",
        ),
        ReportSection(
            kind="issues",
            title="证据边界与未覆盖段",
            items=boundary_issues,
            empty_text="未识别明显边界问题",
        ),
    ]
    if summary_text or source_rows:
        detail_blocks: list[str] = []
        if summary_text:
            detail_blocks.append(render_summary_html(summary_text))
        if source_rows:
            detail_blocks.append(render_source_rows_html(source_rows))
        sections.append(
            ReportSection(
                kind="details",
                title="补充证据",
                summary="展开脚本结论与源码引用",
                body_html="".join(detail_blocks),
            )
        )
    return ReportComposition(
        title=f"信号链路总览 - {title_suffix}",
        heading="信号链路总览报告",
        subtitle=(
            f"分析请求：<code>{prompt_text}</code><br/>"
            f"原始信号报告：<code>{raw_signal_report_name}</code>"
        ),
        verdict=ReportVerdict(sev=verdict_sev, text=verdict_text),
        cards=cards,
        sections=sections,
    )


def _startup_stuck_evidence_rows(
    *,
    fault_time: str,
    focus_pid: str,
    target_rows: list[tuple[object, ...]],
    system_rows: list[tuple[object, ...]],
    startup_load_value: str,
    startup_load_desc: str,
) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    if fault_time:
        rows.append(("故障时间", fault_time, "用户请求/缺陷描述", "已识别目标时间窗", "限定报告聚焦窗口"))
    if focus_pid:
        rows.append(("主会话", focus_pid, "目标进程", "已锁定主会话 PID", "避免混入无关进程"))
    for item in target_rows[:3]:
        if len(item) >= 2:
            rows.append(("目标时间窗", item[0], "主会话", item[1], "支撑会话聚焦"))
    if startup_load_value:
        rows.append(("系统负载", startup_load_value, "启动时刻", startup_load_desc, "判断系统压力影响"))
    for item in system_rows[:3]:
        rows.append(("系统样本", item[0] if item else "", "系统负载", " / ".join(str(value) for value in item[1:]), "支撑卡顿窗口判断"))
    return rows[:8]


def _cause_items_from_chain(chain_nodes: list[dict[str, object]]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for node in chain_nodes[:4]:
        title = str(node.get("title") or "").strip()
        evidence = str(node.get("evidence") or "").strip()
        downstream = str(node.get("downstream") or "").strip()
        detail = "；".join(part for part in [evidence, downstream] if part)
        items.append({"sev": str(node.get("sev") or "yellow"), "title": title, "detail": detail})
    return items


def _startup_stuck_confirmations(
    *,
    fault_time: str,
    focus_pid: str,
    system_rows: list[tuple[object, ...]],
    issues: list[dict[str, object]],
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    if not fault_time:
        items.append({"sev": "yellow", "title": "故障时间", "detail": "未识别到精确故障时间，结论只能按可见日志窗口收敛。"})
    if not focus_pid:
        items.append({"sev": "yellow", "title": "主进程", "detail": "未锁定主会话 PID，需要补充目标进程或更精确时间。"})
    if not system_rows:
        items.append({"sev": "yellow", "title": "系统负载", "detail": "未命中同窗口系统负载样本，无法确认系统侧压力占比。"})
    for issue in issues:
        if str(issue.get("sev") or "") in {"red", "yellow"}:
            items.append({"sev": issue.get("sev", "yellow"), "title": issue.get("title", ""), "detail": issue.get("detail", "")})
    return items[:5]


def _startup_stuck_actions(
    *,
    startup_sev: str,
    stuck_sev: str,
    chain_nodes: list[dict[str, object]],
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    risky_nodes = [node for node in chain_nodes if str(node.get("sev") or "") in {"red", "yellow"}]
    if risky_nodes:
        sev = "red" if "red" in {startup_sev, stuck_sev} else "yellow"
        actions.append(
            {
                "sev": sev,
                "title": "优先复核异常链路",
                "detail": "按根因链中红黄节点逐项复核对应日志和源码落点。",
            }
        )
    actions.append(
        {
            "sev": "green",
            "title": "保留聚焦边界",
            "detail": "后续追问继续沿当前故障时间、主会话 PID 和同窗口系统负载展开。",
        }
    )
    return actions


def _signal_summary_evidence_rows(
    *,
    evidence_rows: list[tuple[object, ...]],
    source_rows: list[tuple[object, ...]],
    judgement_text: str,
) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for row in evidence_rows[:6]:
        values = tuple(row)
        time_text = values[0] if len(values) > 0 else ""
        relative = values[1] if len(values) > 1 else ""
        actor = values[2] if len(values) > 2 else ""
        stage = values[3] if len(values) > 3 else "日志"
        evidence = values[4] if len(values) > 4 else ""
        rows.append((stage, f"{time_text} {relative}".strip(), actor, evidence, judgement_text or "支撑当前链路判断"))
    for row in source_rows[:2]:
        values = tuple(row)
        file_name = values[0] if len(values) > 0 else ""
        line = values[1] if len(values) > 1 else ""
        text = values[2] if len(values) > 2 else ""
        rows.append(("源码", f"{file_name} {line}".strip(), "业务源码", text, "支撑数据流/消费落点判断"))
    return rows[:8]


def _signal_cause_items(
    *,
    lifecycle_nodes: list[dict[str, object]],
    dataflow_nodes: list[dict[str, object]],
    summary_text: str,
    verdict_sev: str,
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    if summary_text.strip():
        items.append({"sev": verdict_sev, "title": "脚本综合判断", "detail": summary_text.strip()})
    for node in (dataflow_nodes + lifecycle_nodes)[:3]:
        title = str(node.get("title") or "").strip()
        meta = str(node.get("meta") or "").strip()
        note = str(node.get("note") or "").strip()
        if title or meta or note:
            items.append({"sev": "yellow", "title": title or meta, "detail": "；".join(part for part in [meta, note] if part)})
    return items[:4]


def _signal_action_items(
    *,
    evidence_rows: list[tuple[object, ...]],
    source_rows: list[tuple[object, ...]],
    boundary_issues: list[dict[str, object]],
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    if evidence_rows:
        actions.append({"sev": "green", "title": "回放关键日志", "detail": "优先沿关键证据表中的时间、进程和阶段回放链路。"})
    if source_rows:
        actions.append({"sev": "green", "title": "沿源码落点追踪", "detail": "按源码引用继续确认生产者、分发器和业务消费侧。"})
    if boundary_issues:
        actions.append({"sev": "yellow", "title": "补齐证据边界", "detail": "优先补充待确认项涉及的日志片段、精确时间或源码上下文。"})
    return actions
