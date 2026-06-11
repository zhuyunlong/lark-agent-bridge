from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _SignalReportMixin:
    """信号类 Bug 报告：报告计划、概览渲染、时间窗口对齐与生命周期/数据流节点（与 GeneralSummaryMixin 共享 self 状态）。"""

    def _plan_signal_report(
        self,
        *,
        signal_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        selected_input: Path | None,
        source_evidence_path: Path | None,
        focus_scope: dict[str, object],
        android_data_link: list[dict[str, object]] | None = None,
    ) -> ReportComposition:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        signal_name = str(signal.get("name") or signal.get("code") or "未知信号")
        signal_code = str(signal.get("code") or "")
        signal_comment = str(signal.get("comment") or "")
        package = str(focus_scope.get("package") or "未识别")
        pid = str(focus_scope.get("pid") or "未识别")
        time_window = focus_scope.get("time_window", {}) if isinstance(focus_scope, dict) else {}
        alignment = self._signal_fault_alignment(fault_time, focus_scope)
        lifecycle_nodes = self._signal_lifecycle_nodes(signal_payload, focus_scope, fault_time, source_evidence_path)
        dataflow_nodes = self._signal_dataflow_nodes(signal_payload, prompt_text, source_evidence_path)
        evidence_rows = self._signal_evidence_rows(signal_payload, focus_scope)
        boundary_issues = self._signal_boundary_issues(signal_payload, focus_scope, fault_time)
        source_rows = self._signal_source_rows(signal_payload, source_evidence_path)
        summary_text = self._signal_scoped_summary_text(signal_payload)
        visible_scope = self._signal_visible_scope_text(signal_payload, focus_scope)
        verdict_sev = alignment["sev"]
        if verdict_sev == "green" and any(issue.get("sev") == "yellow" for issue in boundary_issues):
            verdict_sev = "yellow"
        cards = [
            ("信号", signal_code or signal_name, "green", signal_name if signal_code else signal_comment),
            ("焦点进程", package, "green" if package != "未识别" else "yellow", f"PID {pid}" if pid != "未识别" else ""),
            ("日志时间窗", str(time_window.get("display") or "未识别"), "green" if time_window else "yellow", ""),
            ("现场一致性", alignment["label"], alignment["sev"], alignment["detail"]),
            ("进程内可见性", visible_scope, "green" if "已看到" in visible_scope else "yellow", ""),
            ("原始输入", str(selected_input or "无"), "green" if selected_input else "yellow", ""),
        ]
        title_suffix = signal_comment or signal_name
        composition = plan_signal_report(
            title_suffix=title_suffix,
            prompt_text=combined_bug_html.H(prompt_text),
            raw_signal_report_name=combined_bug_html.H(self._report_name("signal", "html")),
            verdict_sev=verdict_sev,
            verdict_text=alignment["headline"],
            judgement_text=alignment["judgement"],
            cards=cards,
            lifecycle_nodes=lifecycle_nodes,
            dataflow_nodes=dataflow_nodes,
            evidence_rows=evidence_rows,
            boundary_issues=boundary_issues,
            summary_text=summary_text,
            source_rows=source_rows,
            render_summary_html=lambda summary: f'<div class="insight" style="margin-top:12px">{combined_bug_html.H(summary)}</div>',
            render_source_rows_html=lambda rows: combined_bug_html.render_table(
                rows,
                ["层级", "位置", "说明"],
                empty_text="未提取到额外源码引用",
            ),
        )
        if android_data_link:
            self._signal_merge_android_conclusion(composition, android_data_link)
            self._signal_insert_sections_after(
                composition,
                title="结论摘要",
                sections=self._signal_android_data_link_sections(android_data_link),
            )
        return composition
    def _signal_overview_report_name(self, suffix: str) -> str:
        return f"bug_signal_overview_report.{suffix}"
    def _build_signal_overview_summary_text(
        self,
        signal_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        focus_scope: dict[str, object],
        android_data_link: list[dict[str, object]] | None = None,
    ) -> str:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        signal_name = str(signal.get("name") or signal.get("code") or "未知信号")
        signal_code = str(signal.get("code") or "")
        package = str(focus_scope.get("package") or "未识别")
        pid = str(focus_scope.get("pid") or "未识别")
        time_window = focus_scope.get("time_window", {}) if isinstance(focus_scope, dict) else {}
        start = str(time_window.get("start") or "")
        end = str(time_window.get("end") or "")
        alignment = self._signal_fault_alignment(fault_time, focus_scope)
        boundary = self._signal_boundary_issues(signal_payload, focus_scope, fault_time)
        top_issue = boundary[0]["detail"] if boundary else "已生成信号链路总览报告。"
        likely_cause = self._signal_android_likely_cause(android_data_link or [])
        if likely_cause:
            top_issue = likely_cause
        coverage_note = self._signal_coverage_note(signal_payload, focus_scope)
        time_note = "未识别"
        if start and end:
            time_note = f"{start} ~ {end}"
        elif start:
            time_note = start
        return (
            "Bug 分析完成\n"
            "类型: 信号链路总览报告\n"
            f"描述: {prompt_text}\n"
            f"信号: {signal_code or '-'} {signal_name}\n"
            f"焦点进程: {package} / PID {pid}\n"
            f"日志时间窗: {time_note}\n"
            f"现场一致性: {alignment['label']}\n"
            f"当前判断: {top_issue}\n"
            f"进程内链路: {coverage_note}\n"
            f"HTML: {self._signal_overview_report_name('html')}"
        )
    def _render_signal_bug_overview_html(
        self,
        *,
        signal_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        selected_input: Path | None,
        source_evidence_path: Path | None,
        focus_scope: dict[str, object],
        android_data_link: list[dict[str, object]] | None = None,
    ) -> str:
        composition = self._plan_signal_report(
            signal_payload=signal_payload,
            prompt_text=prompt_text,
            fault_time=fault_time,
            selected_input=selected_input,
            source_evidence_path=source_evidence_path,
            focus_scope=focus_scope,
            android_data_link=android_data_link or [],
        )
        return combined_bug_html.render_report_shell(**composition_to_renderer_payload(composition))
    def _signal_focus_scope(self, signal_payload: dict[str, object], fault_time: str = "") -> dict[str, object]:
        evidence_items = self._signal_evidence_items(signal_payload)
        reference_date = self._signal_reference_date(evidence_items)
        target_items = [
            item
            for item in evidence_items
            if str(item.get("stage") or "") != "lifecycle" and self._signal_item_matches_target(signal_payload, item)
        ]
        scoped_items = target_items or evidence_items
        fault_dt = self._parse_bug_datetime(fault_time)
        nearest: tuple[float, tuple[str, str]] | None = None
        if fault_dt is not None:
            for item in scoped_items:
                package = self._signal_package_from_path(str(item.get("file") or ""))
                pid = self._signal_pid_from_text(str(item.get("text") or ""))
                if not package and not pid:
                    continue
                timestamp = self._signal_parse_datetime(
                    text=str(item.get("text") or ""),
                    time_text=str(item.get("time") or ""),
                    reference_date=reference_date,
                )
                if timestamp is None:
                    continue
                distance = abs((timestamp - fault_dt).total_seconds())
                key = (package, pid)
                if nearest is None or distance < nearest[0]:
                    nearest = (distance, key)
        counts: dict[tuple[str, str], int] = {}
        for item in scoped_items:
            package = self._signal_package_from_path(str(item.get("file") or ""))
            pid = self._signal_pid_from_text(str(item.get("text") or ""))
            if not package and not pid:
                continue
            counts[(package, pid)] = counts.get((package, pid), 0) + 1
        package = ""
        pid = ""
        if nearest is not None:
            package, pid = nearest[1]
        elif counts:
            (package, pid), _ = sorted(
                counts.items(),
                key=lambda item: (item[1], bool(item[0][0]), bool(item[0][1]), item[0][0], item[0][1]),
                reverse=True,
            )[0]
        time_window = self._signal_time_window(scoped_items, reference_date, package, pid)
        return {
            "package": package,
            "pid": pid,
            "reference_date": reference_date,
            "time_window": time_window,
        }
    def _signal_evidence_items(self, signal_payload: dict[str, object]) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        runtime = lifecycle.get("runtime", {}) if isinstance(lifecycle, dict) else {}
        for event in runtime.get("events", []) if isinstance(runtime, dict) else []:
            if not isinstance(event, dict):
                continue
            items.append(
                {
                    "stage": "lifecycle",
                    "title": str(event.get("label") or "生命周期"),
                    "file": str(event.get("file") or ""),
                    "line": event.get("line"),
                    "text": str(event.get("text") or ""),
                    "time": str(event.get("time") or ""),
                    "delta": str(event.get("delta") or ""),
                }
            )
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        for stage_key, stage_payload in stages.items():
            if not isinstance(stage_payload, dict):
                continue
            title = str(stage_payload.get("title") or stage_key)
            for example in stage_payload.get("examples", []) or []:
                if not isinstance(example, dict):
                    continue
                items.append(
                    {
                        "stage": str(stage_key),
                        "title": title,
                        "code": str(example.get("code") or ""),
                        "file": str(example.get("file") or ""),
                        "line": example.get("line"),
                        "text": str(example.get("text") or ""),
                    }
                )
        return items
    def _signal_reference_date(self, evidence_items: list[dict[str, object]]) -> str:
        for item in evidence_items:
            for value in (str(item.get("text") or ""), str(item.get("file") or ""), str(item.get("time") or "")):
                match = re.search(r"(20\d{2}-\d{2}-\d{2})", value)
                if match:
                    return match.group(1)
        return ""
    def _signal_time_window(
        self,
        evidence_items: list[dict[str, object]],
        reference_date: str,
        package: str,
        pid: str,
    ) -> dict[str, object]:
        matched: list[datetime] = []
        for item in evidence_items:
            if package:
                item_package = self._signal_package_from_path(str(item.get("file") or ""))
                if item_package and item_package != package:
                    continue
            if pid:
                item_pid = self._signal_pid_from_text(str(item.get("text") or ""))
                if item_pid and item_pid != pid:
                    continue
            timestamp = self._signal_parse_datetime(
                text=str(item.get("text") or ""),
                time_text=str(item.get("time") or ""),
                reference_date=reference_date,
            )
            if timestamp is not None:
                matched.append(timestamp)
        if not matched:
            return {}
        matched.sort()
        start = matched[0]
        end = matched[-1]
        display = self._signal_format_datetime(start)
        if end != start:
            display += " ~ " + self._signal_format_datetime(end)
        return {
            "start": self._signal_format_datetime(start),
            "end": self._signal_format_datetime(end),
            "display": display,
        }
    def _signal_parse_datetime(self, *, text: str, time_text: str, reference_date: str) -> datetime | None:
        full_match = re.search(r"\[(20\d{2}-\d{2}-\d{2}) \+\d{4} (\d{2}:\d{2}:\d{2})\]", text)
        if full_match:
            try:
                return datetime.strptime(f"{full_match.group(1)} {full_match.group(2)}", "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        value = time_text or text
        month_day_match = re.search(r"(\d{2})-(\d{2}) (\d{2}:\d{2}:\d{2}(?:\.\d{3})?)", value)
        if not month_day_match or not reference_date:
            return None
        dt_text = f"{reference_date[:4]}-{month_day_match.group(1)}-{month_day_match.group(2)} {month_day_match.group(3)}"
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(dt_text, fmt)
            except ValueError:
                continue
        return None
    def _signal_format_datetime(self, value: datetime) -> str:
        if value.microsecond:
            return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return value.strftime("%Y-%m-%d %H:%M:%S")
    def _signal_fault_alignment(self, fault_time: str, focus_scope: dict[str, object]) -> dict[str, str]:
        time_window = focus_scope.get("time_window", {}) if isinstance(focus_scope, dict) else {}
        start = str(time_window.get("start") or "")
        if not fault_time:
            return {
                "sev": "yellow",
                "label": "未识别",
                "detail": "请求里没有可比对的故障时间。",
                "headline": "当前报告只说明可见日志内的链路状态，无法和现场时间做严格对齐。",
                "judgement": "没有可比对的故障时间，只能把这份报告当作日志样本说明，不应直接当成现场定案。",
            }
        if not start:
            return {
                "sev": "yellow",
                "label": "未知",
                "detail": "当前报告没有解析出明确日志时间窗。",
                "headline": "当前报告缺少明确日志时间窗，不能直接拿来证明现场结论。",
                "judgement": "需要补充目标时间窗日志，才能把这份链路报告和现场结论绑定起来。",
            }
        fault_hour = fault_time[:13]
        start_hour = start[:13]
        if fault_hour == start_hour:
            return {
                "sev": "green",
                "label": "一致",
                "detail": f"日志时间窗命中了请求故障小时 {fault_hour}。",
                "headline": "当前日志时间窗和请求故障时间一致，可以把下面的链路证据直接用于现场判断。",
                "judgement": "时间窗一致，这份报告可直接回答“现场这条链路当时有没有走通”。",
            }
        if fault_time[:10] == start[:10]:
            return {
                "sev": "yellow",
                "label": "同日不同小时",
                "detail": f"请求故障时间是 {fault_time}，当前日志主时间窗是 {start}。",
                "headline": "当前日志和请求是同一天，但不是同一小时，结论只能作为同版本同流程样本。",
                "judgement": "这能说明代码和样本日志里的链路行为，但还不能直接证明目标时刻的现场现象。",
            }
        return {
            "sev": "yellow",
            "label": "不一致",
            "detail": f"请求故障时间是 {fault_time}，当前日志主时间窗是 {start}。",
            "headline": "当前可用日志不是目标现场时间窗，下面的证据更适合回答“链路设计和样本运行是否走通”，不适合直接下现场定案。",
            "judgement": "这份报告最能说明的是样本日志里目标信号在当前焦点进程内走到了哪里，而不是请求里的目标时刻一定发生了什么。",
        }
    def _signal_lifecycle_nodes(
        self,
        signal_payload: dict[str, object],
        focus_scope: dict[str, object],
        fault_time: str,
        source_evidence_path: Path | None,
    ) -> list[dict[str, str]]:
        package = str(focus_scope.get("package") or "")
        pid = str(focus_scope.get("pid") or "")
        reference_date = str(focus_scope.get("reference_date") or "")
        start_dt = None
        nodes: list[dict[str, str]] = []
        for event in self._signal_runtime_events(signal_payload):
            if not self._signal_item_matches_scope(event, package, pid):
                continue
            label = str(event.get("label") or "")
            text = str(event.get("text") or "")
            if label == "进程启动":
                start_dt = self._signal_parse_datetime(text=text, time_text=str(event.get("time") or ""), reference_date=reference_date)
                nodes.append(
                    {
                        "tag": "日志",
                        "title": f"{package or '目标进程'} 启动",
                        "meta": f"{event.get('time') or ''} · PID {pid or self._signal_pid_from_text(text) or '未识别'}",
                        "note": text,
                    }
                )
                break
        for keyword, title in (
            ("injectSignalProvider", self._signal_provider_injection_title(signal_payload)),
            ("registerSignal", self._signal_registration_title(signal_payload)),
        ):
            match = self._signal_find_runtime_event(signal_payload, package, pid, keyword)
            if match is None:
                continue
            nodes.append(
                {
                    "tag": "日志",
                    "title": title,
                    "meta": self._signal_meta_from_item(match, start_dt, reference_date),
                    "note": str(match.get("text") or ""),
                }
            )
        for stage_key, title in (
            ("datacenter", self._signal_datacenter_stage_title(signal_payload)),
            ("android_business", self._signal_business_stage_title(signal_payload)),
        ):
            match = self._signal_find_stage_example(signal_payload, stage_key, package, pid, prefer_hmi=False, target_only=True)
            if match is None:
                continue
            nodes.append(
                {
                    "tag": "日志",
                    "title": title,
                    "meta": self._signal_meta_from_item(match, start_dt, reference_date),
                    "note": str(match.get("text") or ""),
                }
            )
        hmi_match = self._signal_find_stage_example(signal_payload, "android_business", package, pid, prefer_hmi=True, target_only=True)
        if hmi_match is not None:
            nodes.append(
                {
                    "tag": "日志",
                    "title": self._signal_consumer_stage_title(hmi_match),
                    "meta": self._signal_meta_from_item(hmi_match, start_dt, reference_date),
                    "note": str(hmi_match.get("text") or ""),
                }
            )
        business_entry = self._signal_select_business_entry(source_evidence_path, prompt_text=fault_time, signal_payload=signal_payload)
        if business_entry is not None:
            nodes.append(
                {
                    "tag": "源码",
                    "title": "业务判定落点",
                    "meta": f"{business_entry['file']}:{business_entry['line']}",
                    "note": business_entry["text"],
                }
            )
        return nodes[:6]
    def _signal_dataflow_nodes(
        self,
        signal_payload: dict[str, object],
        prompt_text: str,
        source_evidence_path: Path | None,
    ) -> list[dict[str, str]]:
        refs = signal_payload.get("source_references", []) if isinstance(signal_payload, dict) else []
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        lifecycle_refs = lifecycle.get("source_references", []) if isinstance(lifecycle, dict) else []
        chain_edges = signal_payload.get("chain_edges", []) if isinstance(signal_payload, dict) else []
        nodes: list[dict[str, str]] = []
        source_edge = None
        context = lifecycle.get("context", {}) if isinstance(lifecycle, dict) else {}
        if isinstance(context, dict):
            candidate = context.get("source_edge")
            if isinstance(candidate, dict):
                source_edge = candidate
        if source_edge is None:
            for edge in chain_edges:
                if isinstance(edge, dict) and str(edge.get("source") or "").startswith("CARSERVICE:"):
                    source_edge = edge
                    break
        if isinstance(source_edge, dict):
            nodes.append(
                {
                    "tag": "映射",
                    "title": str(source_edge.get("source") or "上游信号"),
                    "meta": f"{source_edge.get('file') or ''}:{source_edge.get('line') or ''}",
                    "note": str(source_edge.get("note") or ""),
                }
            )
        on_change_ref = self._signal_find_source_reference(
            lifecycle_refs,
            lambda file_text, line_text: "carvcuhelper.kt" in file_text and "onchangeevent" in line_text,
        )
        if on_change_ref is not None:
            nodes.append(
                {
                    "tag": "Helper",
                    "title": "CarVcuHelper.onChangeEvent",
                    "meta": f"{on_change_ref['file']}:{on_change_ref['line']}",
                    "note": on_change_ref["text"],
                }
            )
        data_center_edge = None
        for edge in chain_edges:
            if isinstance(edge, dict) and "datacenter" in str(edge.get("target") or "").casefold():
                data_center_edge = edge
                break
        if isinstance(data_center_edge, dict):
            nodes.append(
                {
                    "tag": "分发",
                    "title": self._signal_data_center_title(),
                    "meta": f"{data_center_edge.get('file') or ''}:{data_center_edge.get('line') or ''}",
                    "note": str(data_center_edge.get("note") or ""),
                }
            )
        consumer_ref = self._signal_select_consumer_reference(refs)
        if consumer_ref is not None:
            nodes.append(
                {
                    "tag": "业务",
                    "title": self._signal_consumer_reference_title(consumer_ref),
                    "meta": f"{consumer_ref['file']}:{consumer_ref['line']}",
                    "note": consumer_ref["text"],
                }
            )
        hmi_ref = self._signal_select_state_receiver_reference(refs)
        if hmi_ref is not None:
            nodes.append(
                {
                    "tag": "HMI",
                    "title": self._signal_consumer_reference_title(hmi_ref),
                    "meta": f"{hmi_ref['file']}:{hmi_ref['line']}",
                    "note": hmi_ref["text"],
                }
            )
        business_entry = self._signal_select_business_entry(source_evidence_path, prompt_text=prompt_text, signal_payload=signal_payload)
        if business_entry is not None:
            nodes.append(
                {
                    "tag": "落点",
                    "title": Path(business_entry["file"]).stem,
                    "meta": f"{business_entry['file']}:{business_entry['line']}",
                    "note": business_entry["text"],
                }
            )
        return nodes[:6]
