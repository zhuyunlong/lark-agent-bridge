from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _SignalStageViewMixin:
    """信号链路阶段视图：边界问题、范围/覆盖文案、阶段标题与源码引用挑选（与 SignalAndroidMixin 共享 self 状态）。"""

    def _signal_boundary_issues(
        self,
        signal_payload: dict[str, object],
        focus_scope: dict[str, object],
        fault_time: str,
    ) -> list[dict[str, object]]:
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        datacenter_hits = self._signal_target_stage_hits(signal_payload, stages, "datacenter")
        business_hits = self._signal_target_stage_hits(signal_payload, stages, "android_business")
        vhal_hits = self._signal_target_stage_hits(signal_payload, stages, "vhal")
        unity_hits = self._signal_target_stage_hits(signal_payload, stages, "unity_received")
        alignment = self._signal_fault_alignment(fault_time, focus_scope)
        issues = [
            {
                "sev": "green" if datacenter_hits and business_hits else "yellow",
                "title": "当前日志能证明的范围",
                "detail": self._signal_visible_scope_text(signal_payload, focus_scope),
            },
            {
                "sev": alignment["sev"],
                "title": "当前日志不能直接证明现场",
                "detail": alignment["detail"],
            },
            {
                "sev": "yellow",
                "title": "未覆盖段",
                "detail": (
                    f"VHAL/CarService 原始输入命中 {vhal_hits}，Unity 接收命中 {unity_hits}。"
                    " 现有样本更适合判断焦点进程内是否走通，不适合下跨层丢失定案。"
                ),
            },
        ]
        stats = signal_payload.get("detailed_stats", {}) if isinstance(signal_payload, dict) else {}
        signal_code = str((signal_payload.get("signal", {}) or {}).get("code") or "")
        stat = stats.get(signal_code) if isinstance(stats, dict) else None
        if isinstance(stat, dict):
            issues.append(
                {
                    "sev": "yellow",
                    "title": "为什么不再单独做“损失分析”结论",
                    "detail": (
                        f"当前 drop / unity counter 基本为 0（unity_drop_total={stat.get('unity_drop_total', 0)}，"
                        f"unity_recv_total={stat.get('unity_recv_total', 0)}），这更像“没有足够下游埋点”而不是“已经证明无损失”。"
                    ),
                }
            )
        return issues
    def _signal_visible_scope_text(self, signal_payload: dict[str, object], focus_scope: dict[str, object]) -> str:
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        datacenter_hits = self._signal_target_stage_hits(signal_payload, stages, "datacenter")
        business_hits = self._signal_target_stage_hits(signal_payload, stages, "android_business")
        package = str(focus_scope.get("package") or "目标进程")
        pid = str(focus_scope.get("pid") or "未识别")
        chain = self._signal_coverage_note(signal_payload, focus_scope)
        if datacenter_hits and business_hits:
            return f"已看到 {package} / PID {pid} 内的目标信号 {chain}。"
        if datacenter_hits:
            return f"目标信号已进入 DataCenter，但业务消费证据不足（{package} / PID {pid}）。"
        return "当前样本还没有锁定到目标进程内的有效链路。"
    def _signal_coverage_note(self, signal_payload: dict[str, object], focus_scope: dict[str, object]) -> str:
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        parts = ["DataCenter"]
        if self._signal_target_stage_hits(signal_payload, stages, "android_business"):
            parts.append("业务消费")
        if self._signal_has_receiver_like_evidence(signal_payload):
            parts.append("状态消费")
        return " -> ".join(parts)
    def _signal_scoped_summary_text(self, signal_payload: dict[str, object]) -> str:
        raw_summary = str(signal_payload.get("summary") or "").strip()
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        code = str(signal.get("code") or "").strip()
        name = str(signal.get("name") or "").strip()
        target = " ".join(part for part in (code, name) if part).strip() or "目标信号"
        datacenter_hits = self._signal_target_stage_hits(signal_payload, stages, "datacenter")
        business_hits = self._signal_target_stage_hits(signal_payload, stages, "android_business")
        android_unity_hits = self._signal_target_stage_hits(signal_payload, stages, "android_unity")
        unity_hits = self._signal_target_stage_hits(signal_payload, stages, "unity_received")
        if not any((datacenter_hits, business_hits, android_unity_hits, unity_hits)):
            return raw_summary
        if datacenter_hits and not any((business_hits, android_unity_hits, unity_hits)):
            return (
                f"目标信号 {target} 当前仅看到 DataCenter 取流/订阅命中，"
                "未看到目标信号业务消费、AndroidUnityProxy 或 Unity 接收证据；"
                "其它 signal 的业务日志不计入本链路。"
            )
        reached = ["DataCenter"] if datacenter_hits else []
        if business_hits:
            reached.append("业务消费")
        if android_unity_hits:
            reached.append("AndroidUnityProxy")
        if unity_hits:
            reached.append("Unity 接收")
        return f"目标信号 {target} 当前已命中 {' -> '.join(reached)}，仍需按证据边界确认未覆盖段。"
    def _signal_has_receiver_like_evidence(self, signal_payload: dict[str, object]) -> bool:
        refs = signal_payload.get("source_references", []) if isinstance(signal_payload, dict) else []
        if self._signal_select_state_receiver_reference(refs) is not None:
            return True
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        stage = stages.get("android_business") if isinstance(stages, dict) else None
        if not isinstance(stage, dict):
            return False
        for example in stage.get("examples", []) or []:
            if not isinstance(example, dict):
                continue
            if not self._signal_item_matches_target(signal_payload, example):
                continue
            label = self._signal_log_semantic_label(str(example.get("text") or ""))
            if label == "状态消费":
                return True
        return False
    def _signal_provider_injection_title(self, signal_payload: dict[str, object]) -> str:
        context = (signal_payload.get("lifecycle_report", {}) or {}).get("context", {})
        helper_class = ""
        if isinstance(context, dict):
            helper_class = str(context.get("helper_class") or "").strip()
        return f"DataCenter 注入 {helper_class}" if helper_class else "DataCenter 注入 Provider"
    def _signal_registration_title(self, signal_payload: dict[str, object]) -> str:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        code = str(signal.get("code") or "").strip()
        return f"注册 {code} 到 XData" if code else "注册信号到 XData"
    def _signal_datacenter_stage_title(self, signal_payload: dict[str, object]) -> str:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        code = str(signal.get("code") or "").strip()
        return f"DataCenter 读取 {code} Flow" if code else "DataCenter 读取信号 Flow"
    def _signal_business_stage_title(self, signal_payload: dict[str, object]) -> str:
        return "业务侧消费到有效值"
    def _signal_consumer_stage_title(self, item: dict[str, object]) -> str:
        stage = self._signal_stage_label(item)
        if stage == "状态消费":
            return "状态消费已更新"
        if stage == "业务消费":
            return "业务消费已更新"
        return "下游状态已更新"
    def _signal_data_center_title(self) -> str:
        return "DataCenter.dispatchSignal / getSignalFlow"
    def _signal_select_consumer_reference(self, refs: object) -> dict[str, str] | None:
        if not isinstance(refs, list):
            return None
        ranked: list[tuple[tuple[int, int, int], dict[str, str]]] = []
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            file_text = str(ref.get("file") or "")
            line_text = str(ref.get("text") or "")
            lowered_file = file_text.casefold()
            lowered_line = line_text.casefold()
            if "getsignalflow" not in lowered_line and "collect {" not in lowered_line:
                continue
            score = (
                1 if any(token in lowered_file for token in ("collector", "service", "manager", "config", "viewmodel", "receiver")) else 0,
                0 if "datacenter" in lowered_file else 1,
                1 if "/business/" in lowered_file or "/manager_" in lowered_file else 0,
            )
            ranked.append(
                (
                    score,
                    {
                        "file": file_text,
                        "line": str(ref.get("line") or ""),
                        "text": line_text,
                    },
                )
            )
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1]
    def _signal_select_state_receiver_reference(self, refs: object) -> dict[str, str] | None:
        if not isinstance(refs, list):
            return None
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            file_text = str(ref.get("file") or "")
            lowered = file_text.casefold()
            if any(token in lowered for token in ("receiver", "observer", "viewmodel", "state")):
                return {
                    "file": file_text,
                    "line": str(ref.get("line") or ""),
                    "text": str(ref.get("text") or ""),
                }
        return None
    def _signal_consumer_reference_title(self, ref: dict[str, str]) -> str:
        stem = Path(ref["file"]).stem
        text = ref["text"].casefold()
        if "receiver" in stem.casefold() or "observer" in stem.casefold():
            return f"{stem} 更新状态"
        if "viewmodel" in stem.casefold():
            return f"{stem} 消费状态"
        if "collector" in stem.casefold() or "manager" in stem.casefold() or "config" in stem.casefold():
            return f"{stem} 消费信号"
        if "collect {" in text or "getsignalflow" in text:
            return f"{stem} 消费信号"
        return stem
    def _signal_log_semantic_label(self, text: str) -> str:
        lowered = text.casefold()
        if any(token in lowered for token in ("state applied", "state updated", "update ", "receiver")):
            return "状态消费"
        if any(token in lowered for token in ("collect", "collector", "value=", "statevalue=", " it.value=")):
            return "业务消费"
        return ""
    def _signal_target_stage_hits(self, signal_payload: dict[str, object], stages: object, stage_key: str) -> int:
        if not isinstance(stages, dict):
            return 0
        stage = stages.get(stage_key)
        if not isinstance(stage, dict):
            return 0
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        code = str(signal.get("code") or "").strip()
        codes = stage.get("codes")
        if code and isinstance(codes, dict):
            value = codes.get(code)
            if isinstance(value, int):
                return value
        count = 0
        for example in stage.get("examples", []) or []:
            if isinstance(example, dict) and self._signal_item_matches_target(signal_payload, example):
                count += 1
        return count
    def _signal_source_rows(
        self,
        signal_payload: dict[str, object],
        source_evidence_path: Path | None,
    ) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        lifecycle_refs = lifecycle.get("source_references", []) if isinstance(lifecycle, dict) else []
        for ref in lifecycle_refs[:4]:
            if not isinstance(ref, dict):
                continue
            rows.append(
                (
                    "生命周期",
                    f"{ref.get('file') or ''}:{ref.get('line') or ''}",
                    self._signal_shorten(str(ref.get("text") or ""), 90),
                )
            )
        business_entry = self._signal_select_business_entry(source_evidence_path, prompt_text="", signal_payload=signal_payload)
        if business_entry is not None:
            rows.append(
                (
                    "业务判定",
                    f"{business_entry['file']}:{business_entry['line']}",
                    self._signal_shorten(business_entry["text"], 90),
                )
            )
        return rows[:6]
    def _signal_select_business_entry(
        self,
        source_evidence_path: Path | None,
        *,
        prompt_text: str,
        signal_payload: dict[str, object] | None = None,
    ) -> dict[str, str] | None:
        entries = self._parse_source_evidence_entries(source_evidence_path)
        if not entries:
            return None
        if signal_payload:
            target_entries = [entry for entry in entries if self._signal_business_entry_matches_target(entry, signal_payload)]
            if target_entries:
                entries = target_entries
            else:
                entries = [entry for entry in entries if self._signal_business_entry_is_meaningful_fallback(entry)]
                if not entries:
                    return None
        prompt_terms = self._signal_prompt_terms(prompt_text)
        priority_tokens = ("dispatcher", "action", "viewmodel", "receiver", "fragment", "service", "scene")

        def score(entry: dict[str, str]) -> tuple[int, int, int, int]:
            haystack = f"{entry['file']}\n{entry['text']}".casefold()
            hint_rank = 0
            for index, token in enumerate(priority_tokens, start=1):
                if token in haystack:
                    hint_rank = len(priority_tokens) - index + 1
                    break
            prompt_match = any(term.casefold() in haystack for term in prompt_terms)
            is_constant_only = 1 if any(token in haystack for token in ("constants", "eventid", "strings.xml")) else 0
            return (
                1 if prompt_match else 0,
                hint_rank,
                0 if is_constant_only else 1,
                1 if "carvcuhelper" not in haystack and "datacenter" not in haystack else 0,
            )
        ranked = sorted(entries, key=score, reverse=True)
        return ranked[0] if ranked else None
    def _signal_business_entry_matches_target(self, entry: dict[str, str], signal_payload: dict[str, object]) -> bool:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        signal_name = str(signal.get("name") or "").strip()
        signal_code = str(signal.get("code") or "").strip()
        haystack = f"{entry.get('file') or ''}\n{entry.get('text') or ''}"
        lower_file = str(entry.get("file") or "").casefold()
        if lower_file.endswith(".xml") or "/res/" in lower_file:
            return False
        if "module_proto" in lower_file or "module_datacenter" in lower_file:
            return False
        return bool((signal_name and signal_name in haystack) or (signal_code and re.search(rf"\b{re.escape(signal_code)}\b", haystack)))
    def _signal_business_entry_is_meaningful_fallback(self, entry: dict[str, str]) -> bool:
        file_text = str(entry.get("file") or "")
        line_text = str(entry.get("text") or "")
        lower_file = file_text.casefold()
        lower_line = line_text.strip().casefold()
        if lower_file.endswith(".xml") or "/res/" in lower_file:
            return False
        if "module_proto" in lower_file or "module_datacenter" in lower_file:
            return False
        if lower_line.startswith(("import ", "package ", "see ", "*", "//")):
            return False
        haystack = f"{lower_file}\n{lower_line}"
        if not any(token in haystack for token in ("collector", "receiver", "viewmodel", "dispatcher", "service", "scene", "action", "judge", "flow")):
            return False
        return any(token in lower_line for token in ("signal", "state", "flow", "collect", "register", "update", "value", "invalid"))
    def _parse_source_evidence_entries(self, source_evidence_path: Path | None) -> list[dict[str, str]]:
        if source_evidence_path is None or not source_evidence_path.exists():
            return []
        entries: list[dict[str, str]] = []
        current_file = ""
        try:
            lines = source_evidence_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            if line.startswith("## "):
                current_file = line[3:].strip()
                continue
            match = re.match(r"- L(\d+): `(.+)`", line.strip())
            if not match or not current_file:
                continue
            entries.append({"file": current_file, "line": match.group(1), "text": match.group(2)})
        return entries
    def _signal_prompt_terms(self, prompt_text: str) -> list[str]:
        raw_terms = re.findall(r"[A-Za-z_]{4,}|[\u4e00-\u9fff]{2,8}", prompt_text or "")
        ignored = {"分析", "源码", "参考", "信号链路", "主要是", "请参考"}
        terms: list[str] = []
        for term in raw_terms:
            cleaned = term.strip()
            if not cleaned or cleaned in ignored:
                continue
            if cleaned not in terms:
                terms.append(cleaned)
        return terms[:8]
    def _signal_find_source_reference(
        self,
        refs: object,
        predicate: Callable[[str, str], bool],
    ) -> dict[str, str] | None:
        if not isinstance(refs, list):
            return None
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            file_text = str(ref.get("file") or "").casefold()
            line_text = str(ref.get("text") or "").casefold()
            if predicate(file_text, line_text):
                return {
                    "file": str(ref.get("file") or ""),
                    "line": str(ref.get("line") or ""),
                    "text": str(ref.get("text") or ""),
                }
        return None
