from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _CombinedReportMixin:
    """组合报告：启动+卡顿 combined 工件构建、汇总文本与 HTML 渲染（与 GeneralSummaryMixin 共享 self 状态）。"""

    def _build_combined_report_artifacts(
        self,
        *,
        plans: list["BugAnalysisPlan"],
        prompt_text: str,
        fault_time: str,
        output_dir: Path,
        html_paths: list[Path],
        report_jsons: dict[str, Path | None],
        selected_input: Path | None,
        source_evidence_path: Path | None = None,
        intent: str = "",
    ) -> dict[str, object] | None:
        kinds = [plan.kind for plan in plans]
        if kinds == ["startup", "stuck"]:
            startup_json_path = report_jsons.get("startup")
            stuck_json_path = report_jsons.get("stuck")
            if startup_json_path is None or stuck_json_path is None:
                return None
            if not startup_json_path.exists() or not stuck_json_path.exists():
                return None

            startup_payload = json.loads(startup_json_path.read_text(encoding="utf-8"))
            stuck_payload = json.loads(stuck_json_path.read_text(encoding="utf-8"))
            summary = self._build_combined_summary_text(startup_payload, stuck_payload, prompt_text, fault_time)
            html_path = output_dir / self._combined_report_name("html")
            json_path = output_dir / self._combined_report_name("json")
            html_path.write_text(
                self._render_combined_startup_stuck_html(
                    startup_payload=startup_payload,
                    stuck_payload=stuck_payload,
                    prompt_text=prompt_text,
                    fault_time=fault_time,
                    startup_html=next((path for path in html_paths if path.name == self._report_name("startup", "html")), None),
                    stuck_html=next((path for path in html_paths if path.name == self._report_name("stuck", "html")), None),
                    selected_input=selected_input,
                ),
                encoding="utf-8",
            )
            combined_payload = {
                "mode": "startup_stuck_combined",
                "prompt_text": prompt_text,
                "fault_time": fault_time,
                "selected_input": str(selected_input) if selected_input else "",
                "summary": summary,
                "startup": startup_payload,
                "stuck": stuck_payload,
            }
            json_path.write_text(json.dumps(combined_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return {
                "html_path": html_path,
                "json_path": json_path,
                "summary": summary,
            }

        if "source_stage" in kinds and "signal" in kinds:
            signal_json_path = report_jsons.get("signal")
            if signal_json_path is None or not signal_json_path.exists():
                return None
            source_stage_data = None
            ss_json = report_jsons.get("source_stage")
            if ss_json is not None and ss_json.exists():
                try:
                    ss_payload = json.loads(ss_json.read_text(encoding="utf-8"))
                    source_stage_data = {
                        "node_status": ss_payload.get("node_status") or {},
                        "findings": ss_payload.get("findings") or [],
                        "verdict": ss_payload.get("verdict") or {},
                    }
                except Exception:
                    source_stage_data = None
            from ...reporting.source_signal_report_html import build_combined_from_signal_json
            html, graph_dict = build_combined_from_signal_json(
                signal_json_path,
                request_text=prompt_text,
                has_logs=selected_input is not None,
                intent=intent,
                source_stage_data=source_stage_data,
            )
            html_path = output_dir / self._combined_report_name("html")
            json_path = output_dir / self._combined_report_name("json")
            html_path.write_text(html, encoding="utf-8")
            json_path.write_text(json.dumps(graph_dict, ensure_ascii=False, indent=2), encoding="utf-8")
            return {
                "html_path": html_path,
                "json_path": json_path,
                "summary": graph_dict.get("verdict", {}).get("headline", ""),
            }

        if kinds == ["signal"]:
            signal_json_path = report_jsons.get("signal")
            if signal_json_path is None or not signal_json_path.exists():
                return None
            signal_payload = json.loads(signal_json_path.read_text(encoding="utf-8"))
            focus_scope = self._signal_focus_scope(signal_payload, fault_time)
            android_data_link = self._signal_android_data_link_checks(
                signal_payload,
                focus_scope,
                selected_input=selected_input,
                source_evidence_path=source_evidence_path,
            )
            summary = self._build_signal_overview_summary_text(
                signal_payload,
                prompt_text,
                fault_time,
                focus_scope,
                android_data_link=android_data_link,
            )
            signal_overview_payload = dict(signal_payload)
            signal_overview_payload["summary"] = self._signal_scoped_summary_text(signal_payload)
            html_path = output_dir / self._signal_overview_report_name("html")
            json_path = output_dir / self._signal_overview_report_name("json")
            html_path.write_text(
                self._render_signal_bug_overview_html(
                    signal_payload=signal_payload,
                    prompt_text=prompt_text,
                    fault_time=fault_time,
                    selected_input=selected_input,
                    source_evidence_path=source_evidence_path,
                    focus_scope=focus_scope,
                    android_data_link=android_data_link,
                ),
                encoding="utf-8",
            )
            json_path.write_text(
                json.dumps(
                    {
                        "mode": "signal_overview_combined",
                        "prompt_text": prompt_text,
                        "fault_time": fault_time,
                        "selected_input": str(selected_input) if selected_input else "",
                        "source_evidence_file": str(source_evidence_path) if source_evidence_path else "",
                        "summary": summary,
                        "focus_scope": focus_scope,
                        "android_data_link": android_data_link,
                        "signal": signal_overview_payload,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return {
                "html_path": html_path,
                "json_path": json_path,
                "summary": summary,
            }

        return None
    def _build_combined_summary_text(
        self,
        startup_payload: dict[str, object],
        stuck_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
    ) -> str:
        startup_verdict = startup_payload.get("verdict", {}) if isinstance(startup_payload, dict) else {}
        stuck_target_verdict = stuck_payload.get("target_verdict", {}) if isinstance(stuck_payload, dict) else {}
        stuck_verdict = stuck_payload.get("verdict", {}) if isinstance(stuck_payload, dict) else {}
        focus_pid = startup_payload.get("focus_session_pid", "")
        boot_relation = startup_payload.get("boot_relation", {}) if isinstance(startup_payload, dict) else {}
        system_load = startup_payload.get("system_load", {}) if isinstance(startup_payload, dict) else {}
        startup_message = str(startup_verdict.get("message", "已生成启动分析"))
        if isinstance(stuck_target_verdict, dict) and stuck_target_verdict.get("message"):
            stuck_message = str(stuck_target_verdict.get("message"))
        else:
            stuck_message = str(stuck_verdict.get("verdict_msg", "已生成卡顿分析"))
        load_text = "未命中"
        if isinstance(system_load, dict) and system_load:
            load_text = (
                f"Total {system_load.get('total_cpu', '?')}% / "
                f"System {system_load.get('system_cpu', '?')}% / "
                f"iow {system_load.get('iow_cpu', '?')}%"
            )
        return (
            "Bug 分析完成\n"
            "类型: 3D启动卡顿综合报告\n"
            f"描述: {prompt_text}\n"
            f"故障时间: {fault_time or '未识别'}\n"
            f"主会话 PID: {focus_pid or '未识别'}\n"
            f"启动结论: {startup_message}\n"
            f"卡顿结论: {stuck_message}\n"
            f"ROM/Boot: {boot_relation.get('note', '未识别')}\n"
            f"启动时刻系统负载: {load_text}\n"
            f"HTML: {self._combined_report_name('html')}"
        )
    def _render_combined_startup_stuck_html(
        self,
        *,
        startup_payload: dict[str, object],
        stuck_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        startup_html: Path | None,
        stuck_html: Path | None,
        selected_input: Path | None,
    ) -> str:
        composition = self._plan_startup_stuck_report(
            startup_payload=startup_payload,
            stuck_payload=stuck_payload,
            prompt_text=prompt_text,
            fault_time=fault_time,
            startup_html=startup_html,
            stuck_html=stuck_html,
            selected_input=selected_input,
        )
        return combined_bug_html.render_report_shell(**composition_to_renderer_payload(composition))
    def _combined_ig_text(self, ig_context: object, power_context: object) -> str:
        after = ig_context.get("after") if isinstance(ig_context, dict) else None
        if isinstance(after, dict) and after.get("timestamp") and after.get("value") is not None:
            return f"启动后最近 IG={after.get('value')} @ {after.get('timestamp')}"
        render_ctx = power_context.get("render_anomaly_context", {}) if isinstance(power_context, dict) else {}
        if isinstance(render_ctx, dict) and render_ctx.get("category"):
            return f"卡顿侧上下电分类={render_ctx.get('category')}"
        return "未识别到明确上下电样本"
    def _combined_stuck_context_text(
        self,
        stuck_payload: dict[str, object],
        target_context: object,
        app_pid_filter: object,
    ) -> str:
        stuck_verdict = stuck_payload.get("verdict", {}) if isinstance(stuck_payload, dict) else {}
        target_verdict = stuck_payload.get("target_verdict", {}) if isinstance(stuck_payload, dict) else {}
        pid_desc = ""
        if isinstance(app_pid_filter, dict) and app_pid_filter.get("selected_pid"):
            pid_desc = f"应用层 PID={app_pid_filter.get('selected_pid')}；"
        if isinstance(target_context, dict) and target_context.get("target"):
            pid_desc += f"目标时间窗={target_context.get('target')}；"
        message = (
            str(target_verdict.get("message"))
            if isinstance(target_verdict, dict) and target_verdict.get("message")
            else str(stuck_verdict.get("verdict_msg", "已生成卡顿报告"))
        )
        return pid_desc + message
    def _plan_startup_stuck_report(
        self,
        *,
        startup_payload: dict[str, object],
        stuck_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        startup_html: Path | None,
        stuck_html: Path | None,
        selected_input: Path | None,
    ) -> ReportComposition:
        startup_verdict = startup_payload.get("verdict", {}) if isinstance(startup_payload, dict) else {}
        stuck_target_verdict = stuck_payload.get("target_verdict", {}) if isinstance(stuck_payload, dict) else {}
        stuck_verdict = stuck_payload.get("verdict", {}) if isinstance(stuck_payload, dict) else {}
        startup_message = str(startup_verdict.get("message", "已生成启动分析"))
        startup_sev = str(startup_verdict.get("severity", "yellow"))
        stuck_message = (
            str(stuck_target_verdict.get("message"))
            if isinstance(stuck_target_verdict, dict) and stuck_target_verdict.get("message")
            else str(stuck_verdict.get("verdict_msg", "已生成卡顿分析"))
        )
        stuck_sev = (
            str(stuck_target_verdict.get("sev", "yellow"))
            if isinstance(stuck_target_verdict, dict) and stuck_target_verdict.get("sev")
            else str(stuck_verdict.get("verdict_sev", "yellow"))
        )
        focus_pid = startup_payload.get("focus_session_pid", "")
        focus_session_index = startup_payload.get("focus_session_index", "")
        boot_relation = startup_payload.get("boot_relation", {}) if isinstance(startup_payload, dict) else {}
        system_load = startup_payload.get("system_load", {}) if isinstance(startup_payload, dict) else {}
        ig_context = startup_payload.get("ig_context", {}) if isinstance(startup_payload, dict) else {}
        power_context = stuck_payload.get("power_context", {}) if isinstance(stuck_payload, dict) else {}
        app_pid_filter = stuck_payload.get("app_pid_filter", {}) if isinstance(stuck_payload, dict) else {}
        target_context = stuck_payload.get("target_context", {}) if isinstance(stuck_payload, dict) else {}
        cards = [
            ("分析类型", "3D启动卡顿综合", "green", "启动链路与卡顿窗口合并输出"),
            ("故障时间", fault_time or "未识别", "green" if fault_time else "yellow", ""),
            ("主会话 PID", focus_pid or "未识别", "green" if focus_pid else "yellow", f"Session {focus_session_index or '?'}"),
            ("ROM 启动邻近", "是" if boot_relation.get("is_near_boot") else "否", "yellow" if boot_relation.get("is_near_boot") else "green", str(boot_relation.get("note", ""))),
            (
                "启动时刻系统负载",
                (
                    f"Total {system_load.get('total_cpu', '?')}% / iow {system_load.get('iow_cpu', '?')}%"
                    if isinstance(system_load, dict) and system_load
                    else "未命中"
                ),
                "yellow" if isinstance(system_load, dict) and int(system_load.get("total_cpu", 0) or 0) >= 70 else "green",
                (
                    f"进程 CPU {system_load.get('process_cpu', '?')}% / RSS {system_load.get('process_mem_rss_kb', '?')}KB"
                    if isinstance(system_load, dict) and system_load
                    else ""
                ),
            ),
            ("启动链路", startup_sev.upper(), startup_sev, startup_message),
            ("卡顿窗口", stuck_sev.upper(), stuck_sev, stuck_message),
        ]
        issues: list[dict[str, object]] = []
        for item in startup_verdict.get("issues", []) if isinstance(startup_verdict, dict) else []:
            if isinstance(item, dict):
                issues.append(item)
        if isinstance(stuck_target_verdict, dict) and stuck_target_verdict.get("message"):
            issues.append({"sev": stuck_sev, "title": "目标时间窗卡顿结论", "detail": stuck_message})
        elif isinstance(stuck_verdict, dict) and stuck_verdict.get("verdict_msg"):
            issues.append({"sev": stuck_sev, "title": "卡顿结论", "detail": stuck_message})
        chain_nodes = [
            {
                "sev": "green" if focus_pid else "yellow",
                "title": "目标时间锁主会话与主 PID",
                "evidence": f"故障时间 {fault_time or '未识别'} -> Session {focus_session_index or '?'} / PID {focus_pid or '未识别'}",
                "downstream": "后续启动链与卡顿证据统一围绕同一主会话展开，避免把恢复后的新进程混入。",
            },
            {
                "sev": "yellow" if boot_relation.get("is_near_boot") else "green",
                "title": "ROM 启动邻近与上下电上下文",
                "evidence": (
                    str(boot_relation.get("note", "未识别"))
                    + "；"
                    + self._combined_ig_text(ig_context, power_context)
                ),
                "downstream": "如果问题发生在整机刚启动或特殊上下电阶段，启动卡顿结论需要附带环境说明，避免误判稳定期异常。",
            },
            {
                "sev": startup_sev,
                "title": "启动链路主卡点",
                "evidence": startup_message,
                "downstream": "用于判断 Application / Surface / UnityReady / 首帧 哪一段真正断开。",
            },
            {
                "sev": stuck_sev,
                "title": "卡顿窗口系统与渲染压力",
                "evidence": self._combined_stuck_context_text(stuck_payload, target_context, app_pid_filter),
                "downstream": "补充目标时间窗内的 Watchdog / UnityRequest / 系统 iow / CPU 压力，判断是不是启动后继续卡住。",
            },
        ]
        target_rows = [
            ("故障时间", fault_time or "未识别"),
            ("主会话 PID", str(focus_pid or "未识别")),
            ("会话选择", str(startup_payload.get("focus_reason", "未记录"))),
            ("启动报告", startup_html.name if startup_html else self._report_name("startup", "html")),
            ("卡顿报告", stuck_html.name if stuck_html else self._report_name("stuck", "html")),
            ("原始日志输入", str(selected_input or "")),
        ]
        system_rows = []
        if isinstance(system_load, dict) and system_load:
            system_rows.append(
                (
                    system_load.get("timestamp", ""),
                    f"{system_load.get('total_cpu', '?')}%",
                    f"{system_load.get('user_cpu', '?')}%",
                    f"{system_load.get('system_cpu', '?')}%",
                    f"{system_load.get('iow_cpu', '?')}%",
                    f"{system_load.get('process_cpu', '?')}%",
                )
            )
        return plan_startup_stuck_report(
            prompt_text=combined_bug_html.H(prompt_text),
            fault_time=fault_time,
            startup_html_name=combined_bug_html.H(startup_html or self._report_name("startup", "html")),
            stuck_html_name=combined_bug_html.H(stuck_html or self._report_name("stuck", "html")),
            selected_input=str(selected_input or ""),
            startup_message=startup_message,
            startup_sev=startup_sev,
            stuck_message=stuck_message,
            stuck_sev=stuck_sev,
            focus_pid=str(focus_pid or ""),
            focus_session_index=str(focus_session_index or ""),
            boot_relation_is_near_boot=bool(boot_relation.get("is_near_boot")),
            boot_relation_note=str(boot_relation.get("note", "")),
            startup_load_value=(
                f"Total {system_load.get('total_cpu', '?')}% / iow {system_load.get('iow_cpu', '?')}%"
                if isinstance(system_load, dict) and system_load
                else "未命中"
            ),
            startup_load_desc=(
                f"进程 CPU {system_load.get('process_cpu', '?')}% / RSS {system_load.get('process_mem_rss_kb', '?')}KB"
                if isinstance(system_load, dict) and system_load
                else ""
            ),
            startup_load_sev="yellow" if isinstance(system_load, dict) and int(system_load.get("total_cpu", 0) or 0) >= 70 else "green",
            issues=issues,
            chain_nodes=chain_nodes,
            target_rows=target_rows,
            system_rows=system_rows,
            system_cols=["时间", "Total", "User", "System", "iow", "进程 CPU"],
        )
