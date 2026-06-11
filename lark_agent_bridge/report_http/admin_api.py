"""Handler mixin: sessions, cases, analysis history, daemon and health APIs."""

from __future__ import annotations

from urllib.parse import parse_qs

from .history import (
    _delete_authorization_placeholder,
    _history_paths_to_delete,
    _history_search_text,
    _is_analysis_history_mode,
    _remove_history_path,
)
from .query import _query_int, _query_str


class AdminDataApiMixin:
    """Admin data endpoints backed by the activity/case/conversation stores.

    Expects the composed class to provide ``root_dir``, ``activity_store``,
    ``case_store``, ``conversation_store``, ``version_store``,
    ``health_monitor``, ``process_watchdog``, ``lifecycle_store`` and
    ``get_dispatcher_metrics`` class attributes.
    """

    def _list_sessions(self) -> list[dict[str, object]]:
        if self.activity_store is None:
            return []
        return self.activity_store.list_sessions(limit=200)

    def _list_cases(self, query: str) -> list[dict[str, object]]:
        if self.case_store is None:
            return []
        params = parse_qs(query)
        limit = _query_int(params, "limit", 200)
        keyword = _query_str(params, "keyword")
        problem_type = _query_str(params, "problem_type")
        analysis_mode = _query_str(params, "analysis_mode")
        root_cause_tag = _query_str(params, "root_cause_tag")
        if any([keyword, problem_type, analysis_mode, root_cause_tag]):
            cases = self.case_store.search(
                keyword=keyword,
                problem_type=problem_type,
                analysis_mode=analysis_mode,
                root_cause_tag=root_cause_tag,
                limit=limit,
            )
        elif _query_str(params, "all") in {"1", "true", "yes"}:
            cases = self.case_store.list_recent(limit=limit)
        else:
            cases = self.case_store.list_latest_by_bug(limit=limit)
        return [case.to_dict() for case in cases]

    def _list_analysis_history(self, query: str) -> list[dict[str, object]]:
        if self.activity_store is None:
            return []
        params = parse_qs(query)
        limit = _query_int(params, "limit", 200)
        keyword = _query_str(params, "keyword").casefold()
        mode = _query_str(params, "mode")
        status = _query_str(params, "status")
        items: list[dict[str, object]] = []
        for session in self.activity_store.list_sessions(limit=None):
            session_mode = str(session.get("mode") or "")
            if not _is_analysis_history_mode(session_mode):
                continue
            if mode and session_mode != mode:
                continue
            if status and str(session.get("status") or "") != status:
                continue
            if keyword and keyword not in _history_search_text(session).casefold():
                continue
            items.append(session)
            if len(items) >= limit:
                break
        return items

    def _get_analysis_history_item(self, session_id: str) -> dict[str, object] | None:
        if self.activity_store is None:
            return None
        session = self.activity_store.get_session(session_id)
        if session is None:
            return None
        if not _is_analysis_history_mode(str(session.get("mode") or "")):
            return None
        return session

    def _delete_analysis_history(self, session_id: str) -> tuple[dict[str, object], int]:
        if self.activity_store is None:
            return {"ok": False, "error": "activity store not configured"}, 503
        session = self.activity_store.get_session(session_id)
        if session is None:
            return {"ok": False, "error": "analysis history not found"}, 404
        if not _is_analysis_history_mode(str(session.get("mode") or "")):
            return {"ok": False, "error": "not an analysis history record"}, 409
        authorization = _delete_authorization_placeholder()
        deleted_session = self.activity_store.delete_session(session_id) or session
        job_id = str(session.get("job_id") or "").strip()
        deleted_case: dict[str, object] | None = None
        if self.case_store is not None:
            case = self.case_store.delete_by_job_id(job_id) if job_id else None
            if case is None:
                case_id = str(session.get("case_id") or "").strip()
                case = self.case_store.delete(case_id) if case_id else None
            deleted_case = case.to_dict() if case is not None else None
        removed_contexts = 0
        if self.conversation_store is not None:
            removed_contexts += self.conversation_store.delete(session_id)
            details = session.get("details")
            if isinstance(details, dict):
                root_message_id = str(details.get("conversation_root_message_id") or "").strip()
                if root_message_id and root_message_id != session_id:
                    removed_contexts += self.conversation_store.delete(root_message_id)
        removed_versions = (
            self.version_store.delete_by_job_id(job_id) if self.version_store is not None and job_id else 0
        )
        removed_paths: list[str] = []
        for path in _history_paths_to_delete(session, root_dir=self.root_dir):
            if _remove_history_path(path):
                removed_paths.append(str(path))
        return {
            "ok": True,
            "item": deleted_session,
            "case": deleted_case,
            "removed_paths": removed_paths,
            "removed_contexts": removed_contexts,
            "removed_versions": removed_versions,
            "authorization": authorization,
        }, 200

    def _get_case(self, case_id: str) -> dict[str, object] | None:
        if self.case_store is None:
            return None
        case = self.case_store.get(case_id)
        return case.to_dict() if case is not None else None

    def _confirm_case(self, case_id: str, payload: dict[str, object]) -> dict[str, object] | None:
        if self.case_store is None:
            return None
        confirmed = bool(payload.get("confirmed", True))
        notes = str(payload.get("notes") or "")
        case = self.case_store.update_human_confirmation(case_id, confirmed=confirmed, notes=notes)
        return case.to_dict() if case is not None else None

    def _get_session(self, session_id: str) -> dict[str, object] | None:
        if self.activity_store is None:
            return None
        return self.activity_store.get_session(session_id)

    def _terminate_session(self, session_id: str) -> tuple[dict[str, object], int]:
        if self.activity_store is None:
            return {"ok": False, "error": "activity store not configured"}, 503
        session = self.activity_store.get_session(session_id)
        if session is None:
            return {"ok": False, "error": "session not found"}, 404
        if not bool(session.get("can_terminate")):
            return {
                "ok": False,
                "error": "session is not running",
                "session": session,
            }, 409
        terminated: list[dict[str, object]] = []
        if self.process_watchdog is not None:
            terminated = self.process_watchdog.terminate_session(session_id)
        cancelled = self.activity_store.cancel_session(
            session_id,
            reason="后台管理页请求终止任务。",
            terminated_processes=terminated,
        )
        return {"ok": True, "terminated": terminated, "session": cancelled or session}, 200

    def _get_daemon_status(self) -> dict[str, object]:
        if self.activity_store is None:
            return {}
        return self.activity_store.get_daemon_status()

    def _get_health(self) -> dict[str, object]:
        if self.health_monitor is None:
            result = {"healthy": True, "note": "health monitor not configured"}
        else:
            check_method = getattr(self.health_monitor, "check_health", None)
            if check_method is None:
                result = {"healthy": True, "note": "health monitor has no check_health method"}
            else:
                status = check_method()
                to_dict = getattr(status, "to_dict", None)
                result = to_dict() if to_dict is not None else {"healthy": True}
        components = result.setdefault("components", {})
        # Concurrency dispatcher metrics (Phase 3) — read live via provider.
        get_dispatcher_metrics = self.get_dispatcher_metrics
        if get_dispatcher_metrics is not None:
            try:
                metrics = get_dispatcher_metrics()
                if metrics is not None:
                    components["dispatcher"] = metrics
            except Exception:
                pass
        # Active job lifecycles (Phase 4) — summary only, no transitions list
        # (avoids iterating a list a worker thread may be appending to).
        if self.lifecycle_store is not None:
            try:
                active = self.lifecycle_store.list_active()
                components["lifecycle"] = {
                    "active_count": len(active),
                    "active_jobs": [
                        {
                            "lifecycle_id": lc.lifecycle_id,
                            "state": lc.state.value,
                            "chat_id": lc.chat_id,
                            "request_text": lc.request_text,
                            "elapsed_seconds": round(lc.elapsed_seconds, 1),
                        }
                        for lc in active[:20]
                    ],
                }
            except Exception:
                pass
        return result
