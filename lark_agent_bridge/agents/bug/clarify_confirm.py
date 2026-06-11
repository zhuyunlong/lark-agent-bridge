from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared
from ...parser import parse_addr2line_request


class _BugClarifyConfirmMixin:
    """Bug 澄清与确认：时间/堆栈/方向澄清结果与技能确认选项构造（与 ResolveSourceMixin 共享 self 状态）。"""

    def _needs_general_direction(
        self,
        selection: "BugAnalysisSelection",
        *,
        prompt_text: str,
    ) -> bool:
        if selection.skill_name != "general":
            return False
        if any(plan.kind != "general" for plan in selection.plans):
            return False
        return not self._has_explicit_general_scope(prompt_text)
    def _has_explicit_general_scope(self, prompt_text: str) -> bool:
        prompt = self._normalize_general_prompt(prompt_text)
        if not prompt:
            return False
        if prompt.casefold() in _GENERIC_BUG_PROMPT_TERMS:
            return False
        return any(pattern.search(prompt) for pattern in _GENERAL_SCOPE_PATTERNS)
    def _normalize_general_prompt(self, prompt_text: str) -> str:
        prompt = re.sub(r"https?://\S+", " ", prompt_text or "")
        prompt = re.sub(r"\s+", " ", prompt).strip(" \t\r\n，。；;、:：")
        return prompt
    def _general_direction_needed_result(
        self,
        *,
        context,
        selection: "BugAnalysisSelection",
        started: float,
        request_text: str,
        bug_url: str,
        title: str,
        progress_callback: Callable[[dict[str, object]], None] | None,
    ) -> TaskResult:
        self._emit_progress(
            progress_callback,
            stage="bug_need_analysis_direction",
            message="未命中专用预设，等待用户补充明确分析方向",
            job_id=context.job_id,
            bug_url=bug_url,
            title=title,
            classification_skill=selection.skill_name,
            classification_source=selection.source,
            classification_reason=selection.reason,
        )
        message = (
            "当前没有命中专用分析预设，且请求中缺少明确分析方向。\n"
            "为了避免盲扫源码/日志后给出不可靠结论，我没有继续自动分析。\n\n"
            "请补充一个可约束的方向，例如：\n"
            "- 启动 / 卡顿 / Crash / 感知数据 / 主题切换 / 场景信号\n"
            "- 具体 SignalCode、枚举名、类名、函数名、进程号、包名或日志关键词\n"
            "- 只检查日志材料是否完整，或指定要看的时间窗口\n\n"
            "如果仍没有明确方向，目前能力不足以给出可靠根因。"
        )
        return TaskResult(
            success=True,
            message=message,
            skipped=True,
            job_id=context.job_id,
            job_dir=context.job_dir,
            duration_seconds=time.monotonic() - started,
            details={
                "mode": "bug_clarification",
                "analysis_kind": "general",
                "analysis_kinds": ["general"],
                "analysis_skill": "general",
                "analysis_skill_label": "通用问题分诊",
                "classification_source": selection.source,
                "classification_reason": selection.reason,
                "classification_provider": selection.provider,
                "bug_url": bug_url,
                "user_request_text": request_text,
                "needs_user_direction": True,
                "supported_bug_skills": self.supported_primary_bug_skills(),
            },
        )
    def _bug_time_clarification_result(
        self,
        *,
        context,
        started: float,
        request_text: str,
        bug_url: str,
        time_context: BugTimeContext,
        status: str,
        progress_callback: Callable[[dict[str, object]], None] | None,
        log_coverage: LogCoverage | None = None,
    ) -> TaskResult:
        self._emit_progress(
            progress_callback,
            stage="bug_time_gate_blocked",
            message="问题时间或日志覆盖不满足分析前置条件",
            job_id=context.job_id,
            bug_url=bug_url,
            time_gate_status=status,
            fault_time=time_context.fault_time,
            time_source=time_context.source,
            log_start=log_coverage.start_time if log_coverage else "",
            log_end=log_coverage.end_time if log_coverage else "",
        )
        if status == "missing_fault_time":
            message = (
                "缺少明确问题时间：请补充几月几日 几点几分，越精确越好。\n"
                "我已检查用户输入、Bug 标题和描述，但没有找到可用于定位日志的完整时间点。\n"
                "补充示例：`问题时间 2026-05-11 23:12:30，分析3D卡顿`。"
            )
        elif status == "log_time_unknown":
            message = (
                f"已识别问题时间 `{time_context.fault_time}`，但当前日志无法解析出有效时间范围。\n"
                "请补充包含该时间点附近的已解密文本日志，或确认附件是否为正确日志包。"
            )
        else:
            coverage_text = (
                f"{log_coverage.start_time} ~ {log_coverage.end_time}"
                if log_coverage and log_coverage.start_time
                else "未识别"
            )
            message = (
                f"日志时间范围未覆盖问题时间 `{time_context.fault_time}`。\n"
                f"当前日志覆盖范围：`{coverage_text}`。\n"
                "请补充覆盖该问题时间前后约 10 分钟的日志，或修正问题时间后再继续分析。"
            )
        return TaskResult(
            success=True,
            message=message,
            skipped=True,
            job_id=context.job_id,
            job_dir=context.job_dir,
            duration_seconds=time.monotonic() - started,
            details={
                "mode": "bug_time_clarification",
                "bug_url": bug_url,
                "user_request_text": request_text,
                "time_gate_status": status,
                "fault_time": time_context.fault_time,
                "fault_time_source": time_context.source,
                "fault_time_note": time_context.note,
                "log_coverage_start": log_coverage.start_time if log_coverage else "",
                "log_coverage_end": log_coverage.end_time if log_coverage else "",
                "log_coverage_scanned_files": log_coverage.scanned_files if log_coverage else 0,
                "log_coverage_scanned_lines": log_coverage.scanned_lines if log_coverage else 0,
            },
        )
    def _bug_stack_clarification_result(
        self,
        *,
        context,
        started: float,
        request_text: str,
        bug_url: str,
        progress_callback: Callable[[dict[str, object]], None] | None,
    ) -> TaskResult:
        self._emit_progress(
            progress_callback,
            stage="bug_stack_gate_blocked",
            message="缺少可反解堆栈，等待用户补充 tombstone/backtrace",
            job_id=context.job_id,
            bug_url=bug_url,
            stack_gate_status="missing_stack_payload",
        )
        message = (
            "缺少可反解堆栈：当前请求要求反解堆栈，但 Bug 详情没有堆栈文本，也没有可下载附件。\n"
            "请在同一条消息下回复 tombstone/backtrace 地址（形如 `#00 pc 000000... /xxx/libxxx.so`），"
            "或把包含 crash/tombstone 的日志附件上传到 Bug 后再回复继续。"
        )
        return TaskResult(
            success=True,
            message=message,
            skipped=True,
            job_id=context.job_id,
            job_dir=context.job_dir,
            duration_seconds=time.monotonic() - started,
            details={
                "mode": "bug_stack_clarification",
                "bug_url": bug_url,
                "user_request_text": request_text,
                "stack_gate_status": "missing_stack_payload",
            },
        )
    def _bug_time_context_payload(self, time_context: BugTimeContext | None) -> dict[str, object]:
        if time_context is None:
            return {}
        return {
            "fault_time": time_context.fault_time,
            "source": time_context.source,
            "note": time_context.note,
            "has_full_datetime": time_context.has_full_datetime,
            "candidates": time_context.candidates,
        }
    def _event_reference_time_text(self, event: LarkEvent | None) -> str:
        if event is None:
            return datetime.now().astimezone().isoformat(timespec="seconds")
        for raw in (event.create_time, event.timestamp):
            text = str(raw or "").strip()
            if not text:
                continue
            if text.isdigit():
                try:
                    return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc).astimezone().isoformat(
                        timespec="seconds"
                    )
                except (OverflowError, OSError, ValueError):
                    continue
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone().isoformat(timespec="seconds")
        return datetime.now().astimezone().isoformat(timespec="seconds")
    def _bug_skill_confirmation_options(
        self,
        selection: "BugAnalysisSelection",
        *,
        request_text: str = "",
    ) -> list[dict[str, object]]:
        if self._has_explicit_3d_lifecycle_intent(request_text):
            options: list[dict[str, object]] = []

            def add_skill(index: int, skill_name: str, label: str, aliases: list[str]) -> None:
                options.append(
                    {
                        "index": index,
                        "type": "skill",
                        "skill_name": skill_name,
                        "label": label,
                        "aliases": aliases,
                    }
                )

            add_skill(
                1,
                "unity-startup-lifecycle-check",
                "3D启动/Surface生命周期分析",
                ["3D启动时序分析", "生命周期", "3D生命周期", "启动时序", "Surface生命周期"],
            )
            add_skill(
                2,
                "3d-stuck-investigate",
                "3D卡顿/黑屏渲染分析",
                ["3D卡顿分析", "卡顿", "黑屏", "渲染黑屏"],
            )
            options.append(
                {
                    "index": 3,
                    "type": "plans",
                    "label": "两个方向都跑",
                    "plan_kinds": ["startup", "stuck"],
                    "skill_name": "startup+stuck",
                    "aliases": ["都跑", "两个都跑", "全部", "两个方向都跑", "启动和卡顿"],
                }
            )
            return options

        label = selection.skill_label or selection.skill_name
        return [
            {
                "index": 1,
                "type": "skill",
                "skill_name": selection.skill_name,
                "label": label,
                "aliases": [label],
            }
        ]
    def _needs_skill_confirmation(self, selection: "BugAnalysisSelection") -> bool:
        """Gate a low-confidence specific-skill pick before the expensive run."""
        if not self.config.bug_analysis.confirm_low_confidence_skill:
            return False
        if selection.confidence != "low":
            return False
        if selection.skill_name in {"", "general"}:
            return False  # general is already handled by _needs_general_direction
        return not any(plan.kind == "general" for plan in selection.plans)
    def _skill_confirmation_needed_result(
        self,
        *,
        context,
        selection: "BugAnalysisSelection",
        started: float,
        request_text: str,
        bug_url: str,
        title: str,
        progress_callback: Callable[[dict[str, object]], None] | None,
    ) -> TaskResult:
        self._emit_progress(
            progress_callback,
            stage="bug_skill_confirmation_needed",
            message="Skill 分类置信度偏低，先请用户确认分析方向",
            job_id=context.job_id,
            bug_url=bug_url,
            title=title,
            classification_skill=selection.skill_name,
            classification_source=selection.source,
            classification_reason=selection.reason,
        )
        label = selection.skill_label or selection.skill_name
        options = self._bug_skill_confirmation_options(selection, request_text=request_text)
        option_lines = "\n".join(
            f"{option['index']}. {option['label']}" for option in options
        )
        message = (
            f"我初步判定使用 **{label}** 分析"
            + (f"（理由：{selection.reason}）" if selection.reason else "")
            + "，但置信度偏低、可能选错预设。\n"
            "为避免错跑较久的分析，先请你确认方向。\n"
            "请直接回复下面任一选项的序号，或直接回复对应文本：\n"
            f"{option_lines}"
        )
        return TaskResult(
            success=True,
            message=message,
            skipped=True,
            job_id=context.job_id,
            job_dir=context.job_dir,
            duration_seconds=time.monotonic() - started,
            details={
                "mode": "bug_skill_confirmation",
                "analysis_kind": selection.plans[0].kind if selection.plans else "general",
                "analysis_kinds": [item.kind for item in selection.plans],
                "analysis_skill": selection.skill_name,
                "analysis_skill_label": label,
                "classification_source": selection.source,
                "classification_reason": selection.reason,
                "classification_provider": selection.provider,
                "classification_confidence": selection.confidence,
                "bug_url": bug_url,
                "user_request_text": request_text,
                "intent_options": options,
                "needs_user_direction": True,
                "supported_bug_skills": self.supported_primary_bug_skills(),
            },
        )
