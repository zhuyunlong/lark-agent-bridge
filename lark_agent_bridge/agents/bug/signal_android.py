from __future__ import annotations

from ._shared import *  # noqa: F401,F403


from .signal_data_link import _SignalDataLinkMixin
from .signal_keywords import _SignalKeywordsMixin
from .signal_kotlin_source import _SignalKotlinSourceMixin
from .signal_stage_view import _SignalStageViewMixin


class _SignalAndroidMixin(_SignalDataLinkMixin, _SignalKeywordsMixin, _SignalKotlinSourceMixin, _SignalStageViewMixin):
    def _signal_runtime_events(self, signal_payload: dict[str, object]) -> list[dict[str, object]]:
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        runtime = lifecycle.get("runtime", {}) if isinstance(lifecycle, dict) else {}
        events = runtime.get("events", []) if isinstance(runtime, dict) else []
        return [event for event in events if isinstance(event, dict)]
    def _signal_item_matches_scope(self, item: dict[str, object], package: str, pid: str) -> bool:
        if package:
            item_package = self._signal_package_from_path(str(item.get("file") or ""))
            if item_package and item_package != package:
                return False
        if pid:
            item_pid = self._signal_pid_from_text(str(item.get("text") or ""))
            if item_pid and item_pid != pid:
                return False
        return True
    def _signal_item_matches_target(self, signal_payload: dict[str, object], item: dict[str, object]) -> bool:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        code = str(signal.get("code") or "").strip()
        name = str(signal.get("name") or "").strip()
        item_code = str(item.get("code") or "").strip()
        if item_code:
            return bool(code and item_code == code)
        text = str(item.get("text") or "")
        return bool((code and re.search(rf"(?<!\d){re.escape(code)}(?!\d)", text)) or (name and name in text))
    def _signal_find_runtime_event(
        self,
        signal_payload: dict[str, object],
        package: str,
        pid: str,
        keyword: str,
    ) -> dict[str, object] | None:
        lowered_keyword = keyword.casefold()
        for event in self._signal_runtime_events(signal_payload):
            if not self._signal_item_matches_scope(event, package, pid):
                continue
            haystack = f"{event.get('label') or ''}\n{event.get('text') or ''}".casefold()
            if lowered_keyword in haystack:
                return event
        return None
    def _signal_find_stage_example(
        self,
        signal_payload: dict[str, object],
        stage_key: str,
        package: str,
        pid: str,
        *,
        prefer_hmi: bool,
        target_only: bool = False,
    ) -> dict[str, object] | None:
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        stage = stages.get(stage_key) if isinstance(stages, dict) else None
        if not isinstance(stage, dict):
            return None
        for example in stage.get("examples", []) or []:
            if not isinstance(example, dict):
                continue
            if target_only and not self._signal_item_matches_target(signal_payload, example):
                continue
            if package:
                item_package = self._signal_package_from_path(str(example.get("file") or ""))
                if item_package and item_package != package:
                    continue
            if pid:
                item_pid = self._signal_pid_from_text(str(example.get("text") or ""))
                if item_pid and item_pid != pid:
                    continue
            text = str(example.get("text") or "")
            is_hmi = "hmi" in text.casefold() or "update battery level" in text.casefold()
            if prefer_hmi and not is_hmi:
                continue
            if not prefer_hmi and is_hmi and stage_key == "android_business":
                continue
            return example
        return None
    def _signal_meta_from_item(
        self,
        item: dict[str, object],
        start_dt: datetime | None,
        reference_date: str,
    ) -> str:
        timestamp = self._signal_parse_datetime(
            text=str(item.get("text") or ""),
            time_text=str(item.get("time") or ""),
            reference_date=reference_date,
        )
        when = str(item.get("time") or "")
        if not when and timestamp is not None:
            when = self._signal_format_datetime(timestamp)
        pid = self._signal_pid_from_text(str(item.get("text") or ""))
        tid = self._signal_tid_from_text(str(item.get("text") or ""))
        meta = when
        if start_dt is not None and timestamp is not None:
            meta = f"{when} · {self._signal_relative_text(start_dt, timestamp)}"
        if pid:
            meta += f" · PID {pid}"
        if tid:
            meta += f" / TID {tid}"
        return meta.strip(" ·")
    def _signal_relative_text(self, start_dt: datetime, current_dt: datetime) -> str:
        delta = max(0.0, (current_dt - start_dt).total_seconds())
        if delta < 1:
            return f"+{int(delta * 1000)}ms"
        return f"+{delta:.3f}s"
    def _signal_evidence_rows(
        self,
        signal_payload: dict[str, object],
        focus_scope: dict[str, object],
    ) -> list[tuple[str, str, str, str, str]]:
        package = str(focus_scope.get("package") or "")
        pid = str(focus_scope.get("pid") or "")
        reference_date = str(focus_scope.get("reference_date") or "")
        start_dt = None
        start_event = self._signal_find_runtime_event(signal_payload, package, pid, "process begin")
        if start_event is not None:
            start_dt = self._signal_parse_datetime(
                text=str(start_event.get("text") or ""),
                time_text=str(start_event.get("time") or ""),
                reference_date=reference_date,
            )
        items: list[dict[str, object]] = []
        for event in self._signal_runtime_events(signal_payload):
            if self._signal_item_matches_scope(event, package, pid):
                items.append(event)
        for stage_key in ("datacenter", "android_business", "other"):
            match = self._signal_find_stage_example(signal_payload, stage_key, package, pid, prefer_hmi=False, target_only=True)
            if match is not None:
                items.append(match)
        hmi = self._signal_find_stage_example(signal_payload, "android_business", package, pid, prefer_hmi=True, target_only=True)
        if hmi is not None:
            items.append(hmi)
        rows: list[tuple[str, str, str, str, str]] = []
        seen: set[str] = set()
        for item in items:
            text = str(item.get("text") or "")
            key = f"{item.get('line')}::{text}"
            if key in seen:
                continue
            seen.add(key)
            timestamp = self._signal_parse_datetime(
                text=text,
                time_text=str(item.get("time") or ""),
                reference_date=reference_date,
            )
            when = str(item.get("time") or (self._signal_format_datetime(timestamp) if timestamp is not None else ""))
            relative = "-"
            if start_dt is not None and timestamp is not None:
                relative = self._signal_relative_text(start_dt, timestamp)
            row_pid = self._signal_pid_from_text(text) or pid or "-"
            row_tid = self._signal_tid_from_text(text)
            pid_tid = f"PID {row_pid}"
            if row_tid:
                pid_tid += f" / TID {row_tid}"
            rows.append(
                (
                    when or "-",
                    relative,
                    pid_tid,
                    self._signal_stage_label(item),
                    self._signal_shorten(text, 120),
                )
            )
        return rows[:8]
    def _signal_package_from_path(self, path_text: str) -> str:
        match = re.search(r"/app/([^/]+)/", path_text)
        if match:
            return match.group(1)
        match = re.search(r"_app_([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+){2,})_", path_text)
        if match:
            return match.group(1)
        match = re.search(r"/([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+){2,})(?:/|$)", path_text)
        return match.group(1) if match else ""
    def _signal_pid_from_text(self, text: str) -> str:
        match = re.match(r"\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+\s+(\d+)\s+", text)
        if match:
            return match.group(1)
        match = re.search(r"\[(\d+),\d+\]\[20\d{2}-\d{2}-\d{2}", text)
        return match.group(1) if match else ""
    def _signal_tid_from_text(self, text: str) -> str:
        match = re.match(r"\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+(\d+)\s+", text)
        return match.group(1) if match else ""
    def _signal_shorten(self, text: str, limit: int) -> str:
        normalized = text.strip()
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 1].rstrip() + "…"
    def _select_target_session(self, sessions: object, fault_time: str) -> dict[str, object] | None:
        if not isinstance(sessions, list) or not sessions:
            return None
        fault_dt = self._parse_fault_datetime(fault_time)
        if fault_dt is None:
            return None
        target_epoch = time.mktime(fault_dt)
        ranked: list[tuple[float, dict[str, object]]] = []
        for session in sessions:
            if not isinstance(session, dict):
                continue
            start = session.get("start")
            if not isinstance(start, str):
                continue
            try:
                session_epoch = time.mktime(time.strptime(start[:16], "%Y-%m-%dT%H:%M"))
            except ValueError:
                continue
            ranked.append((abs(session_epoch - target_epoch), session))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0])
        return ranked[0][1]
