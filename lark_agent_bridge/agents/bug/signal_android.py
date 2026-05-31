from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _SignalAndroidMixin:
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

    def _signal_android_data_link_checks(
        self,
        signal_payload: dict[str, object],
        focus_scope: dict[str, object],
        *,
        selected_input: Path | None,
        source_evidence_path: Path | None,
    ) -> list[dict[str, object]]:
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        context = lifecycle.get("context", {}) if isinstance(lifecycle, dict) else {}
        chain_edges = signal_payload.get("chain_edges", []) if isinstance(signal_payload, dict) else []
        is_android_datacenter = bool(context) or any(
            isinstance(edge, dict) and "datacenter" in str(edge.get("target") or "").casefold()
            for edge in chain_edges
        )
        if not is_android_datacenter:
            return []
        source_facts = self._signal_android_source_facts(signal_payload, source_evidence_path)
        source_facts = self._signal_merge_cached_keywords(signal_payload, source_facts)
        scan_terms = self._signal_android_scan_terms(signal_payload, source_facts)
        log_hits = self._signal_scan_log_terms(signal_payload, selected_input, scan_terms)
        self._signal_store_keyword_profile(
            signal_payload,
            source_facts,
            scan_terms=scan_terms,
            log_hits=log_hits,
            selected_input=selected_input,
            source_evidence_path=source_evidence_path,
        )
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        signal_name = str(signal.get("name") or "").strip()
        event_ids = [str(item) for item in source_facts.get("event_ids", []) if str(item).strip()]
        event_id_text = " / ".join(event_ids) or "上游事件"
        helper_class = str(context.get("helper_class") or "Helper") if isinstance(context, dict) else "Helper"
        helper_file = str(context.get("helper_file") or "") if isinstance(context, dict) else ""
        helper_full_class = self._signal_kotlin_class_from_file(helper_file, helper_class)
        controller_class = str(context.get("controller_class") or "Controller") if isinstance(context, dict) else "Controller"

        package = str(focus_scope.get("package") or "")
        pid = str(focus_scope.get("pid") or "")
        init_terms = [f"injectSignalProvider[{helper_full_class}" if helper_full_class else "", helper_class, controller_class, "injectSignalProvider["]
        init_hit = self._signal_best_log_hit(log_hits, init_terms, pid=pid, package=package)
        init_runtime = self._signal_find_runtime_event(signal_payload, package, pid, "injectSignalProvider")
        init_status = "通过" if init_hit is not None or init_runtime else "证据不足"
        init_evidence = self._signal_log_hit_text(init_runtime) or self._signal_log_hit_text(init_hit) or self._signal_runtime_evidence_text(signal_payload, "injectSignalProvider")

        register_terms = []
        for event_id in event_ids:
            register_terms.extend([f"register {event_id} indeed", f"registerRemoteListener: event:{event_id}", f"registerEventListener: event:{event_id}"])
        register_hit = self._signal_best_log_hit(log_hits, register_terms, pid=pid, package=package)
        unsupported_hit = self._signal_best_log_hit(
            log_hits,
            [f"not support id:{event_id}" for event_id in event_ids] + [f"register not support id:{event_id}" for event_id in event_ids],
            pid=pid,
            package=package,
        )
        if unsupported_hit is not None:
            register_status = "未通过"
            register_conclusion = f"上游 {event_id_text} 明确返回不支持。"
        elif register_hit is not None:
            register_status = "通过"
            register_conclusion = f"上游 {event_id_text} 已完成监听注册，未看到 not support。"
        else:
            register_status = "证据不足"
            register_conclusion = f"源码可定位到 {event_id_text}，但当前日志未证明上游监听注册成功。"

        producer_terms = [str(item) for item in source_facts.get("producer_terms", []) if str(item).strip()]
        producer_hit = self._signal_best_log_hit(log_hits, producer_terms, pid=pid, package=package)
        target_datacenter_hit = self._signal_find_stage_example(
            signal_payload,
            "datacenter",
            package,
            pid,
            prefer_hmi=False,
            target_only=True,
        )
        if producer_hit is not None:
            upstream_status = "通过"
            upstream_conclusion = f"已看到 {helper_class} 收到上游数据并进入目标信号处理。"
            upstream_evidence = self._signal_log_hit_text(producer_hit)
        else:
            upstream_status = "未通过"
            if event_ids:
                upstream_conclusion = f"未看到 {event_id_text} 回调数据进入 {helper_class}。"
            else:
                upstream_conclusion = f"未看到上游回调数据进入 {helper_class}。"
            source_ref_text = self._signal_source_fact_text(source_facts, "producer_refs")
            missing_terms = "、".join(producer_terms[:4])
            upstream_evidence = source_ref_text or "当前日志未看到上游回调和 onNextData。"
            if missing_terms:
                upstream_evidence = f"{upstream_evidence}；日志未命中关键字：{missing_terms}"
            if target_datacenter_hit is not None:
                upstream_evidence = f"{upstream_evidence}；仅看到取流/订阅：{self._signal_log_hit_text(target_datacenter_hit)}"

        business_terms = [str(item) for item in source_facts.get("consumer_terms", []) if str(item).strip()]
        business_register_hit = self._signal_best_log_hit(
            log_hits,
            [f"getSignalFlow: signalCode={signal_name}", signal_name],
            pid=pid,
            package=package,
        )
        if target_datacenter_hit is not None or business_register_hit is not None or source_facts.get("consumer_refs"):
            business_register_status = "通过"
            business_register_conclusion = "业务已注册目标信号。"
            business_register_evidence = (
                self._signal_log_hit_text(target_datacenter_hit)
                or self._signal_log_hit_text(business_register_hit)
                or self._signal_source_fact_text(source_facts, "consumer_refs")
            )
        else:
            business_register_status = "证据不足"
            business_register_conclusion = "未看到业务注册目标信号的源码或日志证据。"
            business_register_evidence = ""

        business_hit = self._signal_best_log_hit(log_hits, business_terms, pid=pid, package=package)
        business_stage_hits = self._signal_target_stage_hits(
            signal_payload,
            (signal_payload.get("log_report", {}) or {}).get("stages", {}) if isinstance(signal_payload.get("log_report", {}), dict) else {},
            "android_business",
        )
        if business_hit is not None or business_stage_hits:
            business_receive_status = "通过"
            business_receive_conclusion = "业务已收到目标信号。"
            business_receive_evidence = self._signal_log_hit_text(business_hit) or f"目标信号业务消费命中 {business_stage_hits} 条。"
        else:
            business_receive_status = "未通过"
            business_receive_conclusion = "业务未收到目标信号。"
            business_receive_evidence = (
                self._signal_source_fact_text(source_facts, "consumer_refs")
                or "源码存在消费分支，但日志未出现对应业务 update/collect 输出。"
            )

        checks = [
            self._signal_android_check(
                "1",
                "module_datacenter 初始化 / 上游 SDK 链接",
                init_status,
                f"{helper_class} / {controller_class} 初始化链路可见。" if init_status == "通过" else "未看到完整初始化链路日志。",
                init_evidence,
            ),
            self._signal_android_check(
                "2",
                "向上游注册信号 / 上游支持性",
                register_status,
                register_conclusion,
                self._signal_log_hit_text(unsupported_hit) or self._signal_log_hit_text(register_hit) or self._signal_source_fact_text(source_facts, "producer_refs"),
            ),
            self._signal_android_check(
                "3",
                "上游数据进入 DataCenter",
                upstream_status,
                upstream_conclusion,
                upstream_evidence,
            ),
            self._signal_android_check(
                "4",
                "业务注册目标信号",
                business_register_status,
                business_register_conclusion,
                business_register_evidence,
            ),
            self._signal_android_check(
                "5",
                "业务收到目标信号",
                business_receive_status,
                business_receive_conclusion,
                business_receive_evidence,
            ),
        ]
        for check in checks:
            check["source_keywords"] = source_facts.get("keywords", [])
            check["keyword_cache"] = source_facts.get("keyword_cache", {})
        return checks

    def _signal_android_check(self, step: str, checkpoint: str, status: str, conclusion: str, evidence: str) -> dict[str, object]:
        sev = "green" if status == "通过" else "red" if status == "未通过" else "yellow"
        return {
            "step": step,
            "checkpoint": checkpoint,
            "status": status,
            "sev": sev,
            "conclusion": conclusion,
            "evidence": evidence or "未提取到直接证据。",
        }

    def _signal_merge_android_conclusion(self, composition: ReportComposition, checks: list[dict[str, object]]) -> None:
        likely_cause = self._signal_android_likely_cause(checks)
        if not likely_cause:
            return
        failed = [item for item in checks if item.get("status") == "未通过"]
        item = {
            "sev": "red" if failed else "yellow",
            "title": "当前最可能卡点",
            "detail": likely_cause,
        }
        for section in composition.sections:
            if section.title == "结论摘要" and section.kind == "issues":
                section.items = [item, *section.items]
                return
        composition.sections.insert(0, ReportSection(kind="issues", title="结论摘要", items=[item]))

    def _signal_insert_sections_after(
        self,
        composition: ReportComposition,
        *,
        title: str,
        sections: list[ReportSection],
    ) -> None:
        if not sections:
            return
        for index, section in enumerate(composition.sections):
            if section.title == title:
                composition.sections[index + 1 : index + 1] = sections
                return
        composition.sections[0:0] = sections

    def _signal_android_data_link_sections(self, checks: list[dict[str, object]]) -> list[ReportSection]:
        nodes = []
        for item in checks:
            status = str(item.get("status") or "").strip()
            checkpoint = str(item.get("checkpoint") or "").strip()
            conclusion = str(item.get("conclusion") or "").strip()
            evidence = str(item.get("evidence") or "").strip()
            sev = "red" if status == "未通过" else "green" if status == "通过" else "yellow"
            step = str(item.get("step") or "").strip()
            title = f"{step}. {checkpoint}" if step else checkpoint
            nodes.append(
                {
                    "sev": sev,
                    "title": f"{title}：{status or '未识别'}",
                    "evidence": conclusion,
                    "downstream": self._signal_shorten(evidence, 260),
                }
            )
        return [
            ReportSection(
                kind="chain",
                title="Android 数据链路排查",
                description="按初始化、上游注册、上游入数、业务注册、业务接收逐段收敛；完整原始日志放入后续证据区。",
                nodes=nodes,
                empty_text="当前信号不是 Android module_datacenter 链路。",
            )
        ]

    def _signal_android_likely_cause(self, checks: list[dict[str, object]]) -> str:
        if not checks:
            return ""
        by_step = {str(item.get("step") or ""): item for item in checks}
        upstream = by_step.get("3", {})
        business_register = by_step.get("4", {})
        business_receive = by_step.get("5", {})
        if upstream.get("status") == "未通过" and business_register.get("status") == "通过":
            return (
                f"{upstream.get('conclusion') or '未看到上游数据进入 DataCenter'}"
                f" {business_receive.get('conclusion') or '业务未收到目标信号'}"
                " 结合现有证据，最可能卡点在上游事件回调没有下发有效载荷，或载荷缺少目标信号需要的 value key。"
            )
        failed = [item for item in checks if item.get("status") == "未通过"]
        if failed:
            return "；".join(str(item.get("conclusion") or item.get("checkpoint") or "") for item in failed if item)
        return ""

    def _signal_android_source_facts(
        self,
        signal_payload: dict[str, object],
        source_evidence_path: Path | None,
    ) -> dict[str, object]:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        signal_name = str(signal.get("name") or "").strip()
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        context = lifecycle.get("context", {}) if isinstance(lifecycle, dict) else {}
        helper_file = str(context.get("helper_file") or "") if isinstance(context, dict) else ""
        facts: dict[str, object] = {
            "event_ids": [],
            "producer_terms": [],
            "consumer_terms": [],
            "producer_refs": [],
            "consumer_refs": [],
            "keywords": [],
        }
        event_ids: list[str] = []
        producer_terms: list[str] = []
        producer_refs: list[str] = []
        helper_path = self._repo_relative_path(helper_file)
        if helper_path is not None:
            helper_text = self._read_text_quiet(helper_path)
            helper_lines = helper_text.splitlines()
            consts = self._signal_kotlin_string_constants(helper_text)
            for line_no, line in enumerate(helper_lines, 1):
                if signal_name and signal_name in line:
                    matched_event = False
                    for nearby_no in range(line_no, min(len(helper_lines), line_no + 8) + 1):
                        nearby = helper_lines[nearby_no - 1]
                        match = re.search(r"\b(\d{4,})\b\s*(?:to|,)\s*SignalCode\.([A-Z0-9_]+)", nearby)
                        if match:
                            event_ids.append(match.group(1))
                            producer_refs.append(f"{helper_file}:{nearby_no} {nearby.strip()}")
                            matched_event = True
                            break
                    if not matched_event:
                        match = re.search(r"\b(\d{4,})\b\s*(?:to|,)\s*SignalCode\.([A-Z0-9_]+)", line)
                        if match:
                            event_ids.append(match.group(1))
                    producer_refs.append(f"{helper_file}:{line_no} {line.strip()}")
            for event_id in list(dict.fromkeys(event_ids)):
                callback_name = self._signal_callback_name_for_event(helper_text, event_id)
                if callback_name:
                    callback_lines = self._signal_kotlin_function_block(helper_lines, callback_name)
                    producer_lines = self._signal_kotlin_target_branch(callback_lines, signal_name)
                    for offset, line in producer_lines:
                        if signal_name and signal_name in line:
                            producer_refs.append(f"{helper_file}:{offset} {line.strip()}")
                        for token in re.findall(r"\b(?:EVENT_KEY|VALUE_KEY)_[A-Z0-9_]+\b", line):
                            producer_terms.append(token)
                            if token in consts:
                                producer_terms.append(consts[token])
                        literal = self._signal_log_literal_from_source_line(line)
                        if literal:
                            producer_terms.append(literal)
        refs = []
        refs.extend(signal_payload.get("source_references", []) if isinstance(signal_payload, dict) else [])
        refs.extend(self._parse_source_evidence_entries(source_evidence_path))
        refs.extend(self._signal_repo_signal_references(signal_name))
        consumer_refs: list[str] = []
        consumer_terms: list[str] = []
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            file_text = str(ref.get("file") or "")
            line_text = str(ref.get("text") or "")
            if signal_name and signal_name in line_text and "module_proto" not in file_text and "module_datacenter" not in file_text:
                consumer_refs.append(f"{file_text}:{ref.get('line') or ''} {line_text}".strip())
                path = self._repo_relative_path(file_text)
                if path is not None:
                    consumer_terms.extend(self._signal_consumer_log_terms(path, signal_name))
        keywords = self._unique_nonempty([*event_ids, *producer_terms, *consumer_terms, signal_name])
        facts["event_ids"] = self._unique_nonempty(event_ids)
        facts["producer_terms"] = self._unique_nonempty(producer_terms)
        facts["consumer_terms"] = self._unique_nonempty(consumer_terms)
        facts["producer_refs"] = self._unique_nonempty(producer_refs)
        facts["consumer_refs"] = self._unique_nonempty(consumer_refs)
        facts["keywords"] = keywords
        facts["source_signature"] = self._signal_keyword_source_signature(facts)
        return facts

    def _signal_merge_cached_keywords(
        self,
        signal_payload: dict[str, object],
        source_facts: dict[str, object],
    ) -> dict[str, object]:
        cache_entry = self._signal_load_keyword_profile(signal_payload)
        current_signature = str(source_facts.get("source_signature") or "")
        cache_status = "miss"
        cache_updated_at = ""
        if cache_entry:
            cached_signature = str(cache_entry.get("source_signature") or "")
            cache_updated_at = str(cache_entry.get("updated_at") or "")
            if current_signature and cached_signature == current_signature:
                cache_status = "hit"
                for key in ("event_ids", "producer_terms", "consumer_terms", "producer_refs", "consumer_refs"):
                    cached_values = cache_entry.get(key, [])
                    if isinstance(cached_values, list):
                        source_facts[key] = self._unique_nonempty([*(source_facts.get(key, []) or []), *cached_values])
                cached_keywords = cache_entry.get("keywords", [])
                source_facts["keywords"] = self._unique_nonempty([*(source_facts.get("keywords", []) or []), *(cached_keywords if isinstance(cached_keywords, list) else [])])
            elif not current_signature:
                cache_status = "fallback"
                for key in ("event_ids", "producer_terms", "consumer_terms", "producer_refs", "consumer_refs", "keywords"):
                    cached_values = cache_entry.get(key, [])
                    if isinstance(cached_values, list):
                        source_facts[key] = self._unique_nonempty([*(source_facts.get(key, []) or []), *cached_values])
                source_facts["source_signature"] = cached_signature
            else:
                cache_status = "stale_refresh"
        source_facts["keyword_cache"] = {
            "status": cache_status,
            "updated_at": cache_updated_at,
            "source_signature": str(source_facts.get("source_signature") or ""),
        }
        return source_facts

    def _signal_keyword_source_signature(self, source_facts: dict[str, object]) -> str:
        payload = {
            key: source_facts.get(key, [])
            for key in ("event_ids", "producer_refs", "consumer_refs", "producer_terms", "consumer_terms")
        }
        if not any(isinstance(value, list) and value for value in payload.values()):
            return ""
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _signal_keyword_cache_path(self) -> Path:
        return Path(self.config.data_dir).expanduser().resolve() / "signal_keyword_cache.json"

    def _signal_keyword_cache_key(self, signal_payload: dict[str, object]) -> str:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        code = str(signal.get("code") or "").strip()
        name = str(signal.get("name") or "").strip()
        return code or name or "unknown"

    def _signal_load_keyword_profile(self, signal_payload: dict[str, object]) -> dict[str, object] | None:
        path = self._signal_keyword_cache_path()
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        entry = payload.get(self._signal_keyword_cache_key(signal_payload))
        return entry if isinstance(entry, dict) else None

    def _signal_store_keyword_profile(
        self,
        signal_payload: dict[str, object],
        source_facts: dict[str, object],
        *,
        scan_terms: list[str],
        log_hits: dict[str, list[dict[str, object]]],
        selected_input: Path | None,
        source_evidence_path: Path | None,
    ) -> None:
        cache_key = self._signal_keyword_cache_key(signal_payload)
        if not cache_key or cache_key == "unknown":
            return
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        verified_terms = [term for term in self._unique_nonempty(scan_terms) if log_hits.get(term)]
        profile = {
            "signal_code": str(signal.get("code") or ""),
            "signal_name": str(signal.get("name") or ""),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "source_signature": str(source_facts.get("source_signature") or ""),
            "event_ids": source_facts.get("event_ids", []),
            "producer_terms": source_facts.get("producer_terms", []),
            "consumer_terms": source_facts.get("consumer_terms", []),
            "producer_refs": source_facts.get("producer_refs", []),
            "consumer_refs": source_facts.get("consumer_refs", []),
            "keywords": source_facts.get("keywords", []),
            "verified_terms": verified_terms,
            "selected_input": str(selected_input) if selected_input else "",
            "source_evidence_file": str(source_evidence_path) if source_evidence_path else "",
        }
        path = self._signal_keyword_cache_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    payload = {}
            else:
                payload = {}
            payload[cache_key] = profile
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            return

    def _signal_android_scan_terms(self, signal_payload: dict[str, object], source_facts: dict[str, object]) -> list[str]:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        signal_name = str(signal.get("name") or "").strip()
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        context = lifecycle.get("context", {}) if isinstance(lifecycle, dict) else {}
        helper_class = str(context.get("helper_class") or "").strip() if isinstance(context, dict) else ""
        helper_file = str(context.get("helper_file") or "").strip() if isinstance(context, dict) else ""
        helper_full_class = self._signal_kotlin_class_from_file(helper_file, helper_class)
        controller_class = str(context.get("controller_class") or "").strip() if isinstance(context, dict) else ""
        terms: list[str] = [
            signal_name,
            f"getSignalFlow: signalCode={signal_name}" if signal_name else "",
            f"injectSignalProvider[{helper_full_class}" if helper_full_class else "",
            helper_class,
            controller_class,
            "injectSignalProvider[",
        ]
        for event_id in source_facts.get("event_ids", []) if isinstance(source_facts, dict) else []:
            event_id_text = str(event_id)
            terms.extend(
                [
                    f"register {event_id_text} indeed",
                    f"registerRemoteListener: event:{event_id_text}",
                    f"registerEventListener: event:{event_id_text}",
                    f"not support id:{event_id_text}",
                    f"register not support id:{event_id_text}",
                ]
            )
        for key in ("producer_terms", "consumer_terms"):
            terms.extend(str(item) for item in source_facts.get(key, []) if str(item).strip())
        return self._unique_nonempty(terms)

    def _signal_scan_log_terms(
        self,
        signal_payload: dict[str, object],
        selected_input: Path | None,
        terms: list[str],
        *,
        max_hits_per_term: int = 20,
        max_files: int = 80,
        max_lines_per_file: int = 200000,
    ) -> dict[str, list[dict[str, object]]]:
        clean_terms = self._unique_nonempty(terms)
        if not clean_terms:
            return {}
        files: list[Path] = []
        for item in self._signal_evidence_items(signal_payload):
            path = Path(str(item.get("file") or "")).expanduser()
            if path.exists() and path.is_file():
                files.append(path)
        if selected_input is not None and selected_input.exists():
            files.extend(self._iter_log_coverage_files(selected_input)[:max_files])
        unique_files: list[Path] = []
        seen_files: set[str] = set()
        for path in files:
            key = str(path)
            if key in seen_files:
                continue
            seen_files.add(key)
            unique_files.append(path)
            if len(unique_files) >= max_files:
                break
        hits: dict[str, list[dict[str, object]]] = {term: [] for term in clean_terms}
        remaining = set(clean_terms)
        for path in unique_files:
            if not remaining:
                break
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line_no, line in enumerate(handle, 1):
                        if line_no > max_lines_per_file:
                            break
                        for term in list(remaining):
                            if term and term in line:
                                hits[term].append({"term": term, "file": str(path), "line": line_no, "text": line.rstrip()})
                                if len(hits[term]) >= max_hits_per_term:
                                    remaining.discard(term)
                        if not remaining:
                            break
            except OSError:
                continue
        return {term: items for term, items in hits.items() if items}

    def _signal_best_log_hit(
        self,
        hits: dict[str, list[dict[str, object]]],
        terms: list[str],
        *,
        pid: str = "",
        package: str = "",
    ) -> dict[str, object] | None:
        ordered_terms = self._unique_nonempty(terms)
        if pid or package:
            for term in ordered_terms:
                for item in hits.get(term, []) or []:
                    text = str(item.get("text") or "")
                    file_text = str(item.get("file") or "")
                    item_pid = self._signal_pid_from_text(text)
                    if pid and item_pid and item_pid != pid:
                        continue
                    if package and package not in file_text and package not in text:
                        item_package = self._signal_package_from_path(file_text)
                        if item_package and item_package != package:
                            continue
                    return item
        for term in ordered_terms:
            items = hits.get(term)
            if items:
                return items[0]
        return None

    def _signal_log_hit_text(self, hit: dict[str, object] | None) -> str:
        if not isinstance(hit, dict):
            return ""
        file_name = Path(str(hit.get("file") or "")).name
        line = str(hit.get("line") or "")
        text = self._signal_shorten(str(hit.get("text") or ""), 180)
        return f"{file_name}:{line} {text}".strip()

    def _signal_runtime_evidence_text(self, signal_payload: dict[str, object], keyword: str) -> str:
        event = self._signal_find_runtime_event(signal_payload, "", "", keyword)
        if event is None:
            return ""
        return self._signal_log_hit_text(event)

    def _signal_source_fact_text(self, source_facts: dict[str, object], key: str) -> str:
        values = source_facts.get(key, []) if isinstance(source_facts, dict) else []
        if not isinstance(values, list):
            return ""
        return "；".join(str(item) for item in values[:3] if str(item).strip())

    def _repo_relative_path(self, file_text: str) -> Path | None:
        if not file_text:
            return None
        path = Path(file_text)
        if path.is_absolute():
            return path if path.exists() else None
        candidate = self.config.guideengine_repo / file_text
        return candidate if candidate.exists() else None

    def _read_text_quiet(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _signal_repo_signal_references(self, signal_name: str, *, max_refs: int = 80) -> list[dict[str, str]]:
        if not signal_name:
            return []
        repo = Path(self.config.guideengine_repo).expanduser()
        if not repo.exists():
            return []
        try:
            completed = subprocess.run(
                ["rg", "-n", "--fixed-strings", signal_name, str(repo)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        refs: list[dict[str, str]] = []
        for raw_line in completed.stdout.splitlines():
            if len(refs) >= max_refs:
                break
            match = re.match(r"(.+?):(\d+):(.*)", raw_line)
            if not match:
                continue
            path = Path(match.group(1))
            try:
                file_text = str(path.relative_to(repo))
            except ValueError:
                file_text = str(path)
            refs.append({"file": file_text, "line": match.group(2), "text": match.group(3).strip()})
        return refs

    def _signal_kotlin_class_from_file(self, file_text: str, class_name: str) -> str:
        if not file_text or not class_name:
            return ""
        normalized = file_text.replace("\\", "/")
        marker = "/src/main/java/"
        if marker in normalized:
            normalized = normalized.split(marker, 1)[1]
        elif "src/main/java/" in normalized:
            normalized = normalized.split("src/main/java/", 1)[1]
        else:
            return ""
        normalized = re.sub(r"\.(kt|java)$", "", normalized)
        dotted = normalized.replace("/", ".")
        return dotted if dotted.endswith(f".{class_name}") or dotted == class_name else ""

    def _signal_kotlin_string_constants(self, text: str) -> dict[str, str]:
        constants: dict[str, str] = {}
        for match in re.finditer(r"const\s+val\s+([A-Z0-9_]+)\s*(?::\s*String)?\s*=\s*\"([^\"]+)\"", text):
            constants[match.group(1)] = match.group(2)
        return constants

    def _signal_callback_name_for_event(self, text: str, event_id: str) -> str:
        match = re.search(rf"\b{re.escape(event_id)}\s+to\s+this::([A-Za-z0-9_]+)", text)
        return match.group(1) if match else ""

    def _signal_kotlin_function_block(self, lines: list[str], function_name: str, *, max_lines: int = 140) -> list[tuple[int, str]]:
        start_index = -1
        pattern = re.compile(rf"\bfun\s+{re.escape(function_name)}\b")
        for index, line in enumerate(lines):
            if pattern.search(line):
                start_index = index
                break
        if start_index < 0:
            return []
        block: list[tuple[int, str]] = []
        brace_depth = 0
        opened = False
        for index in range(start_index, min(len(lines), start_index + max_lines)):
            line = lines[index]
            block.append((index + 1, line))
            brace_line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
            opens = brace_line.count("{")
            closes = brace_line.count("}")
            if opens:
                opened = True
            if opened:
                brace_depth += opens - closes
                if brace_depth <= 0 and index > start_index:
                    break
        return block

    def _signal_kotlin_target_branch(self, callback_lines: list[tuple[int, str]], signal_name: str) -> list[tuple[int, str]]:
        if not signal_name:
            return callback_lines
        target_indexes = [index for index, (_, line) in enumerate(callback_lines) if signal_name in line]
        if not target_indexes:
            return callback_lines
        result: list[tuple[int, str]] = []
        seen_lines: set[int] = set()
        for target_index in target_indexes:
            start_index = target_index
            for index in range(target_index, -1, -1):
                line = callback_lines[index][1]
                if "->" in line and re.search(r"\b(?:EVENT_KEY|VALUE_KEY)_[A-Z0-9_]+\b|\"[^\"]+\"", line):
                    start_index = index
                    break
            brace_depth = 0
            opened = False
            for index in range(start_index, len(callback_lines)):
                line_no, line = callback_lines[index]
                if line_no not in seen_lines:
                    seen_lines.add(line_no)
                    result.append((line_no, line))
                brace_line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
                opens = brace_line.count("{")
                closes = brace_line.count("}")
                if opens:
                    opened = True
                if opened:
                    brace_depth += opens - closes
                    if brace_depth <= 0 and index > start_index:
                        break
                elif index > target_index:
                    break
        return result or callback_lines

    def _signal_consumer_log_terms(self, path: Path, signal_name: str) -> list[str]:
        text = self._read_text_quiet(path)
        if not text:
            return []
        lines = text.splitlines()
        terms: list[str] = []
        for index, line in enumerate(lines):
            if signal_name not in line:
                continue
            nearby_case = lines[max(0, index - 3) : min(len(lines), index + 4)]
            if any("->" in item for item in nearby_case):
                source_lines = [item for _, item in self._signal_kotlin_target_branch([(line_no + 1, value) for line_no, value in enumerate(lines)], signal_name)]
            elif any("getSignalFlow" in item or ".collect" in item for item in lines[index : min(len(lines), index + 16)]):
                source_lines = lines[index : min(len(lines), index + 40)]
            else:
                continue
            for nearby in source_lines:
                literal = self._signal_log_literal_from_source_line(nearby)
                if literal and self._signal_log_literal_matches_signal(literal, signal_name):
                    terms.append(literal)
        return self._unique_nonempty(terms)

    def _signal_log_literal_matches_signal(self, literal: str, signal_name: str) -> bool:
        literal_key = re.sub(r"[^a-z0-9]+", "", literal.casefold())
        if not literal_key:
            return False
        parts = [
            part.casefold()
            for part in re.split(r"[_\W]+", signal_name)
            if len(part) >= 4 and part.casefold() not in {"signal", "powercenter", "change"}
        ]
        return any(part in literal_key for part in parts)

    def _signal_log_literal_from_source_line(self, line: str) -> str:
        match = re.search(r"L\.[idwe]\([^,]+,\s*\"([^\"]+)\"", line)
        if not match:
            return ""
        literal = match.group(1).strip()
        literal = re.split(r"\$\{?|\{", literal, maxsplit=1)[0].strip()
        return literal if len(literal) >= 4 else ""

    def _unique_nonempty(self, values: list[object]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    def _signal_stage_label(self, item: dict[str, object]) -> str:
        label = str(item.get("label") or item.get("title") or "")
        text = str(item.get("text") or "")
        lowered = text.casefold()
        if "injectsignalprovider" in lowered:
            return "注入 Provider"
        if "registersignal" in lowered:
            return "注册信号"
        if "getsignalflow" in lowered:
            return "DataCenter 取流"
        generic_label = self._signal_log_semantic_label(text)
        if generic_label:
            return generic_label
        return label or "日志命中"

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
