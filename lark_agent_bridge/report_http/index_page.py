"""Rendering of the published report index page and its text helpers."""

from __future__ import annotations

from html import escape
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import quote

from ..models import TaskResult
from ..token_usage import extract_first_prefixed_token_usage


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self._parts.append(text)

    def reset_text(self) -> None:
        self._parts = []
        self.reset()

    def text(self) -> str:
        return "\n".join(self._parts)


def render_index_page(*, summary_text: str, mode: str, reports: list[Path], result: TaskResult) -> str:
    preview_text = _summary_preview_text(summary_text)
    report_sections = "\n".join(
        (
            "<section class=\"report-card\">"
            f"<h2>{escape(_report_title(mode, index, len(reports), report))}</h2>"
            "<p class=\"muted\">完整报告较长，包含详细证据、图表和运行信息；首页只保留摘要和入口，避免重复嵌套展示。点击进入完整报告。</p>"
            f"<p><a class=\"report-link\" href=\"{quote(report.name)}\">打开 HTML 报告</a></p>"
            "</section>"
        )
        for index, report in enumerate(reports, start=1)
    )
    runtime_items = _runtime_summary_items(result)
    runtime_html = ""
    if runtime_items:
        runtime_html = (
            "<section class=\"summary runtime\">"
            "<h2>运行信息</h2>"
            "<dl class=\"meta-grid\">"
            + "".join(
                f"<div class=\"meta-item\"><dt>{escape(label)}</dt><dd>{escape(value)}</dd></div>"
                for label, value in runtime_items
            )
            + "</dl></section>"
        )
    return (
        "<!doctype html>\n"
        "<html lang=\"zh-CN\">\n"
        "<head>\n"
        "  <meta charset=\"utf-8\" />\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />\n"
        "  <title>Lark Agent Bridge Report</title>\n"
        "  <style>\n"
        "    :root { --bg:#eef3ff; --panel:#fff; --text:#14213d; --muted:#5f6b85; --border:#d8e1f4; --blue:#2563eb; --purple:#7c3aed; --cyan:#0891b2; }\n"
        "    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 24px; background: radial-gradient(circle at top left, rgba(37,99,235,.10), transparent 28%), radial-gradient(circle at top right, rgba(124,58,237,.10), transparent 26%), linear-gradient(180deg, #f4f7ff 0%, var(--bg) 100%); color: var(--text); }\n"
        "    .shell { max-width: 1240px; margin: 0 auto; }\n"
        "    .summary, .report-card { background: linear-gradient(180deg, rgba(255,255,255,.96), rgba(248,251,255,.96)); border-radius: 18px; box-shadow: 0 14px 36px rgba(15, 23, 42, 0.09); padding: 22px; margin-bottom: 20px; border: 1px solid var(--border); }\n"
        "    .runtime h2 { margin-bottom: 16px; }\n"
        "    .meta-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 0; }\n"
        "    .meta-item { background: linear-gradient(180deg, rgba(255,255,255,.98), rgba(238,244,255,.96)); border: 1px solid var(--border); border-radius: 14px; padding: 12px 14px; }\n"
        "    .meta-item dt { font-size: 12px; color: var(--muted); margin-bottom: 4px; }\n"
        "    .meta-item dd { margin: 0; font-size: 16px; font-weight: 700; color: var(--text); word-break: break-word; }\n"
        "    .lead { font-size: 16px; line-height: 1.75; color: var(--text); margin-bottom: 12px; white-space: pre-wrap; }\n"
        "    .muted { color: var(--muted); line-height: 1.7; }\n"
        "    .report-link { display: inline-flex; align-items: center; justify-content: center; padding: 10px 14px; border-radius: 10px; border: 1px solid rgba(37,99,235,.28); background: rgba(37,99,235,.08); text-decoration: none; }\n"
        "    h1, h2 { margin-top: 0; }\n"
        "    h1 { color: var(--blue); font-size: 28px; }\n"
        "    h2 { color: var(--cyan); }\n"
        "    a { color: var(--blue); font-weight: 600; }\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        "  <main class=\"shell\">\n"
        "    <section class=\"summary\">\n"
        "      <h1>分析结果</h1>\n"
        f"      <div class=\"lead\">{escape(preview_text)}</div>\n"
        "    </section>\n"
        f"    {runtime_html}\n"
        f"    {report_sections}\n"
        "  </main>\n"
        "</body>\n"
        "</html>\n"
    )


def _report_title(mode: str, index: int, total: int, report_path: Path | None = None) -> str:
    extracted = _extract_report_title(report_path) if report_path is not None else ""
    if extracted:
        return extracted
    if total == 1:
        return "HTML 报告"
    return f"{mode or 'analysis'} 报告 {index}"


def _runtime_summary_items(result: TaskResult) -> list[tuple[str, str]]:
    details = result.details if isinstance(result.details, dict) else {}
    items: list[tuple[str, str]] = []
    skill = str(details.get("analysis_skill") or "").strip()
    if skill:
        items.append(("命中 Skill", skill))
    classification_source = str(details.get("classification_source") or "").strip()
    if classification_source:
        items.append(("分类来源", classification_source))
    classification_provider = str(details.get("classification_provider") or "").strip()
    if classification_provider:
        items.append(("分类 Agent", classification_provider))
    provider = str(details.get("agent_summary_provider") or details.get("provider") or "").strip()
    if provider:
        items.append(("Agent 类型", provider))
    usage_prefix, usage = extract_first_prefixed_token_usage(details, ("agent_summary_", "app_server_"))
    input_tokens = usage.get("input_tokens")
    cached_input_tokens = usage.get("cached_input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    usage_scope = str(
        details.get("agent_summary_usage_scope" if usage_prefix == "agent_summary_" else "app_server_usage_scope") or ""
    ).strip()
    if any(isinstance(value, int) for value in (input_tokens, cached_input_tokens, output_tokens, total_tokens)):
        parts = [_format_token_millions(input_tokens)]
        if isinstance(cached_input_tokens, int):
            parts.append(_format_token_millions(cached_input_tokens))
        parts.extend(
            [
                _format_token_millions(output_tokens),
                _format_token_millions(total_tokens),
            ]
        )
        items.append(
            (
                (
                    "本轮 AI Token"
                    if usage_scope == "delta" and usage_prefix == "app_server_"
                    else "累计 AI Token"
                    if usage_prefix == "app_server_"
                    else "本轮 Agent Token"
                    if usage_scope == "delta"
                    else "累计 Agent Token"
                ),
                " / ".join(parts),
            )
        )
    agent_duration = details.get("agent_summary_duration_seconds")
    if not isinstance(agent_duration, (int, float)):
        agent_duration = details.get("duration_seconds") if usage_prefix == "app_server_" else None
    if isinstance(agent_duration, (int, float)):
        items.append(("AI 耗时" if usage_prefix == "app_server_" else "Agent 耗时", f"{float(agent_duration):.1f} 秒"))
    if isinstance(result.duration_seconds, (int, float)):
        items.append(("总耗时", f"{float(result.duration_seconds):.1f} 秒"))
    return items


def _summary_preview_text(summary_text: str) -> str:
    lines = [line.strip() for line in summary_text.splitlines() if line.strip()]
    if not lines:
        return "分析完成"
    filtered: list[str] = []
    for line in lines:
        if line == "Bug 分析完成":
            continue
        if line.startswith("## "):
            if filtered:
                break
            continue
        normalized = _normalize_summary_preview_line(line)
        if not normalized:
            continue
        if normalized.startswith("诉求：") or normalized.startswith("诉求:"):
            continue
        filtered.append(normalized)
        if len(filtered) >= 4:
            break
    if not filtered:
        filtered = [_normalize_summary_preview_line(line) for line in lines[:3]]
        filtered = [line for line in filtered if line]
    preview = "\n".join(filtered)
    if len(preview) <= 320:
        return preview
    return preview[:319].rstrip() + "…"


def _normalize_summary_preview_line(line: str) -> str:
    normalized = line.strip()
    normalized = re.sub(r"^\s*#+\s*", "", normalized)
    normalized = re.sub(r"^\s*(?:[-*•]|\d+\.)\s+", "", normalized)
    normalized = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", normalized)
    normalized = re.sub(r"`([^`]+)`", r"\1", normalized)
    normalized = re.sub(r"\*\*([^*]+)\*\*", r"\1", normalized)
    normalized = re.sub(r"\*([^*]+)\*", r"\1", normalized)
    normalized = re.sub(r"__([^_]+)__", r"\1", normalized)
    normalized = re.sub(r"_([^_]+)_", r"\1", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _extract_report_title(report_path: Path | None) -> str:
    if report_path is None or not report_path.exists():
        return ""
    try:
        text = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for pattern in (
        r"(?is)<title[^>]*>(.*?)</title>",
        r"(?is)<h1[^>]*>(.*?)</h1>",
    ):
        match = re.search(pattern, text)
        if not match:
            continue
        title = re.sub(r"(?s)<[^>]+>", " ", match.group(1))
        title = re.sub(r"\s+", " ", title).strip()
        if title:
            return title
    return ""


def _format_token_millions(value: object) -> str:
    if not isinstance(value, int):
        return "-"
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.2f}K"
    return f"{value / 1_000_000:.2f}M"
