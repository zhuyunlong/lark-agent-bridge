from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


class _SummaryUsageMixin:
    """Agent 总结审计与用量：审计文件写出、session id 与 token usage 提取（与 AgentSummaryMixin 共享 self 状态）。"""

    def _write_bug_agent_summary_audit(
        self,
        invocation: dict[str, object],
        output_path: Path,
    ) -> tuple[Path | None, Path | None]:
        prompt = str(invocation.get("prompt") or "")
        embedded_files = invocation.get("embedded_files")
        if not isinstance(embedded_files, list):
            embedded_files = []
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_file = output_path.parent / "bug_agent_summary_prompt.md"
            context_file = output_path.parent / "bug_agent_summary_context.json"
            prompt_file.write_text(prompt, encoding="utf-8")
            manifest = {
                "provider": str(invocation.get("provider") or ""),
                "resumed": bool(invocation.get("resumed")),
                "session_id": str(invocation.get("session_id") or ""),
                "output_path": str(output_path),
                "prompt_file": str(prompt_file),
                "prompt_chars": len(prompt),
                "embedded_files": self._embedded_file_manifest(embedded_files),
            }
            context_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            return prompt_file, context_file
        except OSError:
            return None, None
    def _extract_bug_agent_session_id(self, provider: str, output: str, *, fallback: str = "") -> str:
        if fallback.strip():
            return fallback.strip()
        if provider != "codex":
            return ""
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            found = self._search_bug_agent_session_id_in_payload(payload)
            if found:
                return found
        return ""
    def _search_bug_agent_session_id_in_payload(self, payload: object) -> str:
        if isinstance(payload, dict):
            for key in ("session_id", "sessionId", "conversation_id", "conversationId", "thread_id", "threadId"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for key in ("session", "conversation", "thread"):
                nested = payload.get(key)
                if isinstance(nested, dict):
                    value = nested.get("id")
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            for value in payload.values():
                found = self._search_bug_agent_session_id_in_payload(value)
                if found:
                    return found
            return ""
        if isinstance(payload, list):
            for item in payload:
                found = self._search_bug_agent_session_id_in_payload(item)
                if found:
                    return found
        return ""
    def _extract_bug_agent_usage(self, provider: str, stdout: str, stderr: str) -> tuple[dict[str, int], str]:
        usage, scope = self._extract_usage_from_json_lines(stdout)
        if usage:
            return usage, scope
        if provider == "codex":
            usage, scope = self._extract_usage_from_json_lines(stderr)
            if usage:
                return usage, scope
        return self._extract_usage_from_text("\n".join(part for part in (stdout, stderr) if part)), "cumulative"
    def _extract_usage_from_json_lines(self, text: str) -> tuple[dict[str, int], str]:
        latest: dict[str, int] = {}
        latest_delta: dict[str, int] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            for candidate in self._iter_usage_objects_from_keys(
                payload,
                keys=("delta_usage", "deltaUsage", "usage_delta", "usageDelta"),
            ):
                parsed = self._parse_usage_object(candidate)
                if parsed:
                    latest_delta = parsed
            for candidate in self._iter_usage_objects(payload):
                parsed = self._parse_usage_object(candidate)
                if parsed:
                    latest = parsed
        if latest_delta:
            return latest_delta, "delta"
        return latest, "cumulative" if latest else ""
    def _iter_usage_objects_from_keys(self, value: object, *, keys: tuple[str, ...]) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        if isinstance(value, dict):
            for key in keys:
                nested = value.get(key)
                if isinstance(nested, dict):
                    found.append(nested)
            for nested in value.values():
                found.extend(self._iter_usage_objects_from_keys(nested, keys=keys))
        elif isinstance(value, list):
            for item in value:
                found.extend(self._iter_usage_objects_from_keys(item, keys=keys))
        return found
    def _iter_usage_objects(self, value: object) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        if isinstance(value, dict):
            if any(any(alias in value for alias in aliases) for aliases in _TOKEN_USAGE_KEYS.values()):
                found.append(value)
            for nested in value.values():
                found.extend(self._iter_usage_objects(nested))
        elif isinstance(value, list):
            for item in value:
                found.extend(self._iter_usage_objects(item))
        return found
    def _parse_usage_object(self, value: object) -> dict[str, int]:
        if not isinstance(value, dict):
            return {}
        return normalize_token_usage(value)
    def _extract_usage_from_text(self, text: str) -> dict[str, int]:
        patterns = {
            "input_tokens": (r"input[_ ]tokens?\s*[:=]\s*(\d+)", r"prompt[_ ]tokens?\s*[:=]\s*(\d+)"),
            "cached_input_tokens": (
                r"cached[_ ]input[_ ]tokens?\s*[:=]\s*(\d+)",
                r"cached[_ ]prompt[_ ]tokens?\s*[:=]\s*(\d+)",
            ),
            "output_tokens": (r"output[_ ]tokens?\s*[:=]\s*(\d+)", r"completion[_ ]tokens?\s*[:=]\s*(\d+)"),
            "total_tokens": (r"total[_ ]tokens?\s*[:=]\s*(\d+)",),
        }
        usage: dict[str, int] = {}
        for target_key, candidates in patterns.items():
            for pattern in candidates:
                match = re.search(pattern, text, flags=re.IGNORECASE)
                if match:
                    usage[target_key] = int(match.group(1))
                    break
        if "total_tokens" not in usage and {"input_tokens", "output_tokens"}.issubset(usage):
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        return usage
