from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


class _ReanalysisPlanMixin:
    """重分析计划恢复：从上轮会话详情/job 产物推导分析计划（与 BugCacheMixin 共享 self 状态）。"""

    def _path_from_details(self, details: dict[str, object], key: str) -> Path | None:
        value = details.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        path = Path(value).expanduser()
        return path if path.exists() else None
    def _plans_from_previous_details(self, details: dict[str, object], *, fallback_text: str) -> list["BugAnalysisPlan"]:
        raw_kinds = details.get("analysis_kinds")
        kinds: list[str] = []
        if isinstance(raw_kinds, list):
            kinds = [str(item) for item in raw_kinds if str(item)]
        elif isinstance(details.get("analysis_kind"), str):
            kinds = [str(details["analysis_kind"])]
        plans = [
            BugAnalysisPlan(
                kind=kind,
                signal_code=str(details.get("signal_code") or "") or None,
            )
            for kind in kinds
            if kind in PLAN_KIND_REGISTRY
        ]
        if plans:
            return plans
        return self.classify_requests(prompt_text=fallback_text, title="", description="")
    def _plans_for_reanalysis(
        self,
        details: dict[str, object],
        *,
        request_text: str,
        followup_text: str,
    ) -> list["BugAnalysisPlan"]:
        return self._plans_from_previous_details(details, fallback_text=request_text)
    def _followup_explicitly_requests_source_analysis(self, request_text: str, followup_text: str) -> bool:
        if self._source_analysis_shortcut(request_text, followup_text):
            return True
        if self._should_collect_source_evidence(request_text, followup_text):
            return True
        return False
    def _fallback_non_source_plans_from_job_output(self, output_dir: Path) -> list["BugAnalysisPlan"]:
        candidates: list[tuple[float, str]] = []
        for kind in (
            "ld_lane_level",
            "startup",
            "stuck",
            "crash",
            "scene_signal",
            "signal",
            "xtheme",
            "perception",
            "general",
            "custom_skill",
            SOURCE_CODE_SKILL_KIND,
        ):
            report_json = output_dir / self._report_name(kind, "json")
            report_html = output_dir / self._report_name(kind, "html")
            existing = report_json if report_json.exists() else report_html if report_html.exists() else None
            if existing is None:
                continue
            try:
                mtime = existing.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, kind))
        candidates.sort(reverse=True)
        if not candidates:
            return []
        return [BugAnalysisPlan(kind=candidates[0][1])]
    def _extract_signal_code_for_reanalysis(self, text: str) -> str:
        request = parse_signal_request(
            text,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        )
        return request.signal or ""
    def _forced_reanalysis_kinds(
        self,
        plans: list["BugAnalysisPlan"],
        *,
        request_text: str,
        followup_text: str,
        force_rerun: bool = False,
    ) -> set[str]:
        if force_rerun:
            return {plan.kind for plan in plans}
        lowered = f"{request_text}\n{followup_text}".casefold()
        signal_followup_terms = tuple(term.casefold() for term in self.config.bug_analysis.force_reanalysis_terms)
        force: set[str] = set()
        if any(plan.kind == "signal" for plan in plans) and any(term in lowered for term in signal_followup_terms):
            force.add("signal")
        return force
