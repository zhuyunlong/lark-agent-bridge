from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


class _RuntimeMetadataMixin:
    """Agent 运行时明细：details/metadata 落盘与报告 HTML 运行时标注（与 AgentSummaryMixin 共享 self 状态）。"""

    def _apply_agent_runtime_details(self, details: dict[str, object], agent_summary_result: dict[str, object]) -> None:
        if agent_summary_result["command"]:
            details["agent_summary_command"] = list(agent_summary_result["command"])
        if agent_summary_result["error"]:
            details["agent_summary_error"] = str(agent_summary_result["error"])
        if agent_summary_result["provider"]:
            provider = str(agent_summary_result["provider"])
            details["agent_summary_provider"] = provider
            details.setdefault("provider", provider)
        model = self._agent_summary_model(agent_summary_result)
        if model:
            details["agent_summary_model"] = model
        if agent_summary_result["session_id"]:
            details["agent_summary_session_id"] = str(agent_summary_result["session_id"])
        if agent_summary_result["resumed"]:
            details["agent_summary_resumed"] = True
        if agent_summary_result.get("usage_scope"):
            details["agent_summary_usage_scope"] = str(agent_summary_result["usage_scope"])
        if agent_summary_result.get("execution_backend"):
            details["agent_summary_execution_backend"] = str(agent_summary_result["execution_backend"])
        if agent_summary_result.get("backend_reason"):
            details["agent_summary_backend_reason"] = str(agent_summary_result["backend_reason"])
        if agent_summary_result.get("fallback_from"):
            details["agent_summary_fallback_from"] = str(agent_summary_result["fallback_from"])
        if agent_summary_result.get("prompt_file"):
            details["agent_summary_prompt_file"] = str(agent_summary_result["prompt_file"])
        if agent_summary_result.get("context_file"):
            details["agent_summary_context_file"] = str(agent_summary_result["context_file"])
        if agent_summary_result.get("timeout_seconds"):
            details["agent_summary_timeout_seconds"] = agent_summary_result["timeout_seconds"]
        if agent_summary_result.get("timed_out"):
            details["agent_summary_timed_out"] = True
        tool_calls = agent_summary_result.get("tool_calls")
        if isinstance(tool_calls, int):
            details["agent_summary_tool_calls"] = tool_calls
        tool_trace = agent_summary_result.get("tool_trace")
        if isinstance(tool_trace, list):
            details["agent_summary_tool_trace"] = tool_trace[:20]
        duration = agent_summary_result.get("duration_seconds")
        if isinstance(duration, (int, float)):
            details["agent_summary_duration_seconds"] = float(duration)
        usage = agent_summary_result.get("usage")
        if isinstance(usage, dict):
            normalized_usage = normalize_token_usage(usage)
            for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
                value = normalized_usage.get(key)
                if isinstance(value, int):
                    details[f"agent_summary_{key}"] = value
    def _append_agent_runtime_metadata(
        self,
        metadata_path: Path,
        *,
        agent_summary_result: dict[str, object],
        total_duration_seconds: float,
    ) -> None:
        provider = str(agent_summary_result.get("provider") or "").strip()
        model = self._agent_summary_model(agent_summary_result)
        usage = agent_summary_result.get("usage")
        duration = agent_summary_result.get("duration_seconds")
        session_id = str(agent_summary_result.get("session_id") or "").strip()
        resumed = bool(agent_summary_result.get("resumed"))
        usage_scope = str(agent_summary_result.get("usage_scope") or "").strip()
        execution_backend = str(agent_summary_result.get("execution_backend") or "").strip()
        backend_reason = str(agent_summary_result.get("backend_reason") or "").strip()
        fallback_from = str(agent_summary_result.get("fallback_from") or "").strip()
        if not provider and not model and not isinstance(usage, dict) and not isinstance(duration, (int, float)):
            return
        lines = ["", "## Agent 执行信息", ""]
        lines.append(f"- Agent 类型: `{provider or '未知'}`")
        if execution_backend:
            lines.append(f"- 执行后端: `{execution_backend}`")
        if backend_reason:
            lines.append(f"- 后端选择原因: `{backend_reason}`")
        if fallback_from:
            lines.append(f"- fallback_from: `{fallback_from}`")
        if model:
            lines.append(f"- Agent 模型: `{model}`")
        if session_id:
            lines.append(f"- Agent 会话ID: `{session_id}`")
        lines.append(f"- 续会话: `{'是' if resumed else '否'}`")
        if isinstance(usage, dict):
            normalized_usage = normalize_token_usage(usage)
            input_tokens = normalized_usage.get("input_tokens")
            cached_input_tokens = normalized_usage.get("cached_input_tokens")
            output_tokens = normalized_usage.get("output_tokens")
            total_tokens = normalized_usage.get("total_tokens")
            if any(
                isinstance(value, int)
                for value in (input_tokens, cached_input_tokens, output_tokens, total_tokens)
            ):
                token_label = "本轮 Agent Token" if usage_scope == "delta" else "累计 Agent Token"
                parts = [self._format_token_millions(input_tokens)]
                if isinstance(cached_input_tokens, int):
                    parts.append(self._format_token_millions(cached_input_tokens))
                parts.extend(
                    [
                        self._format_token_millions(output_tokens),
                        self._format_token_millions(total_tokens),
                    ]
                )
                lines.append(
                    f"- {token_label}: `{' / '.join(parts)}`"
                )
        if isinstance(duration, (int, float)):
            lines.append(f"- Agent 耗时: `{float(duration):.1f} 秒`")
        tool_calls = agent_summary_result.get("tool_calls")
        if isinstance(tool_calls, int):
            lines.append(f"- Agent 工具调用数: `{tool_calls}`")
        tool_trace = agent_summary_result.get("tool_trace")
        if isinstance(tool_trace, list) and tool_trace:
            trace_items: list[str] = []
            for item in tool_trace[:5]:
                if not isinstance(item, dict):
                    continue
                tool_name = str(item.get("tool") or "?")
                args = str(item.get("args") or "").replace("\n", " ")[:120]
                trace_items.append(f"{tool_name}({args})")
            if trace_items:
                lines.append(f"- Agent 工具轨迹: `{'; '.join(trace_items)}`")
        if agent_summary_result.get("timeout_seconds"):
            lines.append(f"- Agent 总结超时: `{agent_summary_result['timeout_seconds']} 秒`")
        if agent_summary_result.get("timed_out") and agent_summary_result.get("message"):
            lines.append("- 超时处理: `已采用 Agent 写出的总结文件，未跨 provider fallback`")
        if agent_summary_result.get("prompt_file"):
            lines.append(f"- Agent Prompt: `{agent_summary_result['prompt_file']}`")
        if agent_summary_result.get("context_file"):
            lines.append(f"- Agent 输入清单: `{agent_summary_result['context_file']}`")
        lines.append(f"- 总耗时: `{total_duration_seconds:.1f} 秒`")
        try:
            original = metadata_path.read_text(encoding="utf-8")
        except OSError:
            original = ""
        metadata_path.write_text(original.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n", encoding="utf-8")
    def _annotate_html_reports(
        self,
        html_paths: list[Path],
        *,
        agent_summary_result: dict[str, object],
        total_duration_seconds: float,
    ) -> None:
        snippet = self._build_agent_runtime_html(agent_summary_result, total_duration_seconds=total_duration_seconds)
        if not snippet:
            return
        for path in html_paths:
            if not path.exists() or path.suffix.lower() != ".html":
                continue
            try:
                html_text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            updated = self._inject_runtime_html(html_text, snippet)
            if updated == html_text:
                continue
            try:
                path.write_text(updated, encoding="utf-8")
            except OSError:
                continue
    def _build_agent_runtime_html(self, agent_summary_result: dict[str, object], *, total_duration_seconds: float) -> str:
        provider = str(agent_summary_result.get("provider") or "").strip()
        model = self._agent_summary_model(agent_summary_result)
        usage = agent_summary_result.get("usage")
        duration = agent_summary_result.get("duration_seconds")
        session_id = str(agent_summary_result.get("session_id") or "").strip()
        resumed = bool(agent_summary_result.get("resumed"))
        usage_scope = str(agent_summary_result.get("usage_scope") or "").strip()
        execution_backend = str(agent_summary_result.get("execution_backend") or "").strip()
        backend_reason = str(agent_summary_result.get("backend_reason") or "").strip()
        fallback_from = str(agent_summary_result.get("fallback_from") or "").strip()
        if not provider and not model and not isinstance(usage, dict) and not isinstance(duration, (int, float)):
            return ""
        rows = [
            ("Agent 类型", provider or "未知"),
            ("续会话", "是" if resumed else "否"),
        ]
        if execution_backend:
            rows.append(("执行后端", execution_backend))
        if backend_reason:
            rows.append(("后端选择原因", backend_reason))
        if fallback_from:
            rows.append(("Fallback From", fallback_from))
        if model:
            rows.append(("Agent 模型", model))
        if session_id:
            rows.append(("Agent 会话ID", session_id))
        normalized_usage = normalize_token_usage(usage) if isinstance(usage, dict) else {}
        if normalized_usage:
            parts = [self._format_token_millions(normalized_usage.get("input_tokens"))]
            cached_input_tokens = normalized_usage.get("cached_input_tokens")
            if isinstance(cached_input_tokens, int):
                parts.append(self._format_token_millions(cached_input_tokens))
            parts.extend(
                [
                    self._format_token_millions(normalized_usage.get("output_tokens")),
                    self._format_token_millions(normalized_usage.get("total_tokens")),
                ]
            )
            rows.append(
                (
                    "本轮 Agent Token" if usage_scope == "delta" else "累计 Agent Token",
                    " / ".join(parts),
                )
            )
        if isinstance(duration, (int, float)):
            rows.append(("Agent 耗时", f"{float(duration):.1f} 秒"))
        rows.append(("总耗时", f"{total_duration_seconds:.1f} 秒"))
        items = "".join(
            "<div class=\"lagent-runtime-item\">"
            f"<div class=\"lagent-runtime-label\">{self._escape_html(label)}</div>"
            f"<div class=\"lagent-runtime-value\">{self._escape_html(value)}</div>"
            "</div>"
            for label, value in rows
        )
        style = (
            "<style>"
            ".lagent-runtime{margin:16px 0 20px;padding:16px 18px;border:1px solid #dbe2f0;border-radius:12px;"
            "background:#f8fafc;box-shadow:0 1px 3px rgba(15,23,42,.06)}"
            ".lagent-runtime h2{margin:0 0 12px;font-size:18px;color:#0f172a}"
            ".lagent-runtime-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}"
            ".lagent-runtime-item{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px}"
            ".lagent-runtime-label{font-size:12px;color:#64748b;margin-bottom:4px}"
            ".lagent-runtime-value{font-size:16px;font-weight:600;color:#0f172a;word-break:break-word}"
            "</style>"
        )
        return (
            f"{_RUNTIME_HTML_MARKER_START}{style}"
            "<section class=\"lagent-runtime\">"
            "<h2>Agent 运行信息</h2>"
            f"<div class=\"lagent-runtime-grid\">{items}</div>"
            "</section>"
            f"{_RUNTIME_HTML_MARKER_END}"
        )
    def _agent_summary_model(self, agent_summary_result: dict[str, object]) -> str:
        model = str(agent_summary_result.get("model") or "").strip()
        if model:
            return model
        provider = str(agent_summary_result.get("provider") or "").strip().casefold()
        if provider == "direct_api":
            return str(self.config.ai_provider.primary_model or "").strip()
        if provider == "omlx":
            return str(self.config.omlx_chat.model or "").strip()
        if provider == "codex":
            return str(self.config.bug_analysis.model or "").strip()
        if provider in {"claude", "claude-code", "claude_code"}:
            return str(self.config.claude_agent.model or "").strip()
        return ""
    def _format_token_millions(self, value: object) -> str:
        if not isinstance(value, int):
            return "-"
        if value < 1_000:
            return str(value)
        if value < 1_000_000:
            return f"{value / 1_000:.2f}K"
        return f"{value / 1_000_000:.2f}M"
    def _inject_runtime_html(self, html_text: str, runtime_html: str) -> str:
        pattern = re.compile(
            rf"{re.escape(_RUNTIME_HTML_MARKER_START)}.*?{re.escape(_RUNTIME_HTML_MARKER_END)}",
            flags=re.DOTALL,
        )
        if pattern.search(html_text):
            return pattern.sub(runtime_html, html_text, count=1)
        body_match = re.search(r"<body[^>]*>", html_text, flags=re.IGNORECASE)
        if body_match:
            index = body_match.end()
            return html_text[:index] + runtime_html + html_text[index:]
        html_match = re.search(r"<html[^>]*>", html_text, flags=re.IGNORECASE)
        if html_match:
            index = html_match.end()
            return html_text[:index] + "<body>" + runtime_html + "</body>" + html_text[index:]
        return runtime_html + html_text
    def _escape_html(self, value: object) -> str:
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
