from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _SignalDataLinkMixin:
    """Android 数据链路检查：逐级检查点、结论合并与报告小节生成（与 SignalAndroidMixin 共享 self 状态）。"""

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
