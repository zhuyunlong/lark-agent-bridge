"""Assembled report HTTP request handler and its factory."""

from __future__ import annotations

from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ..admin_ui import render_admin_page
from ..auth import AdminAuth
from ..case_store import CaseStore
from ..health import ProcessWatchdog
from ..knowledge import KnowledgeService
from ..models import BridgeConfig
from ..report_version import ReportVersionStore
from ..skill_manager import SkillManager, SkillManagerError
from ..state import AgentActivityStore, ConversationContextStore
from .admin_api import AdminDataApiMixin
from .http_io import HttpIoAuthMixin
from .knowledge_export import _KNOWLEDGE_EXPORT_REPORT_ROUTES, _knowledge_export_report_status
from .skills_knowledge_api import SkillsKnowledgeApiMixin


def _render_sessions_page() -> str:
    return render_admin_page()


class ReportRequestHandler(
    AdminDataApiMixin,
    SkillsKnowledgeApiMixin,
    HttpIoAuthMixin,
    SimpleHTTPRequestHandler,
):
    """Report/admin HTTP handler; collaborators are injected as class attributes.

    ``_build_handler`` creates a per-server subclass carrying the actual
    ``root_dir`` / ``bridge_config`` / store instances (the previous
    implementation captured them in a closure).
    """

    # Injected by _build_handler ----------------------------------------
    root_dir: Path
    url_prefix: str = "/"
    bridge_config: BridgeConfig
    activity_store: AgentActivityStore | None = None
    case_store: CaseStore | None = None
    skill_manager: SkillManager | None = None
    health_monitor: object | None = None
    process_watchdog: ProcessWatchdog | None = None
    conversation_store: ConversationContextStore | None = None
    version_store: ReportVersionStore | None = None
    knowledge_service: KnowledgeService | None = None
    admin_auth: AdminAuth | None = None
    lifecycle_store: object | None = None
    get_dispatcher_metrics = None

    def __init__(self, *args, **kwargs):
        self._disable_cache_for_response = False
        super().__init__(*args, directory=str(self.root_dir), **kwargs)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        request_path = unquote(parsed.path)
        if request_path in {"", "/"}:
            self.send_response(302)
            self.send_header("Location", "/admin")
            self.end_headers()
            return
        if request_path in {"/sessions", "/sessions/", "/admin", "/admin/"}:
            self._send_html(_render_sessions_page())
            return
        # -- auth endpoints (no auth required) --
        if request_path == "/api/auth/status":
            self._send_json(self._auth_status())
            return
        if request_path == "/api/auth/users":
            if not self._check_write_auth():
                return
            self._send_json({"users": self.admin_auth.list_users() if self.admin_auth else []})
            return
        if request_path == "/api/sessions":
            self._send_json({"sessions": self._list_sessions()})
            return
        if request_path == "/api/cases":
            self._send_json({"cases": self._list_cases(parsed.query)})
            return
        if request_path == "/api/analysis-history":
            self._send_json({"items": self._list_analysis_history(parsed.query)})
            return
        if request_path == "/api/skills":
            self._send_json({"skills": self._list_skills()})
            return
        if request_path == "/api/daemon":
            self._send_json({"daemon": self._get_daemon_status()})
            return
        if request_path == "/api/health":
            self._send_json(self._get_health())
            return
        if request_path == "/api/knowledge/sources":
            self._send_json({"sources": self._knowledge_sources()})
            return
        if request_path == "/api/knowledge/search":
            self._send_json({"hits": self._knowledge_search(parsed.query)})
            return
        if request_path == "/api/knowledge/export-report":
            self._send_json(_knowledge_export_report_status(self.bridge_config))
            return
        if request_path in _KNOWLEDGE_EXPORT_REPORT_ROUTES:
            self._send_knowledge_export_report()
            return
        if request_path.startswith("/api/cases/"):
            case_id = request_path.removeprefix("/api/cases/").strip("/")
            case = self._get_case(unquote(case_id))
            if case is None:
                self._send_json({"error": "case not found"}, status=404)
                return
            self._send_json({"case": case})
            return
        if request_path.startswith("/api/analysis-history/"):
            session_id = request_path.removeprefix("/api/analysis-history/").strip("/")
            item = self._get_analysis_history_item(unquote(session_id))
            if item is None:
                self._send_json({"error": "analysis history not found"}, status=404)
                return
            self._send_json({"item": item})
            return
        if request_path.startswith("/api/skills/"):
            skill_name = request_path.removeprefix("/api/skills/").strip("/")
            try:
                self._send_json({"skill": self._get_skill(unquote(skill_name))})
            except SkillManagerError as exc:
                self._send_json({"error": str(exc)}, status=exc.status_code)
            return
        if request_path.startswith("/api/sessions/"):
            session_id = request_path.removeprefix("/api/sessions/").strip("/")
            session = self._get_session(unquote(session_id))
            if session is None:
                self._send_json({"error": "session not found"}, status=404)
                return
            self._send_json({"session": session})
            return
        if not self._rewrite_report_path():
            self.send_error(404)
            return
        self._disable_cache_for_response = True
        super().do_GET()

    def do_HEAD(self) -> None:
        parsed = urlsplit(self.path)
        request_path = unquote(parsed.path)
        if request_path in {"/sessions", "/sessions/", "/admin", "/admin/"}:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            return
        if (
            request_path == "/api/sessions"
            or request_path.startswith("/api/sessions/")
            or request_path == "/api/cases"
            or request_path.startswith("/api/cases/")
            or request_path == "/api/analysis-history"
            or request_path.startswith("/api/analysis-history/")
            or request_path == "/api/skills"
            or request_path.startswith("/api/skills/")
            or request_path == "/api/daemon"
            or request_path == "/api/health"
            or request_path == "/api/knowledge/sources"
            or request_path == "/api/knowledge/search"
            or request_path == "/api/knowledge/export-report"
        ):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            return
        if request_path in _KNOWLEDGE_EXPORT_REPORT_ROUTES:
            status = _knowledge_export_report_status(self.bridge_config)
            self.send_response(200 if status["exists"] else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if status["exists"]:
                self.send_header("Content-Length", str(status["size_bytes"]))
            self.end_headers()
            return
        if not self._rewrite_report_path():
            self.send_error(404)
            return
        self._disable_cache_for_response = True
        super().do_HEAD()

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        request_path = unquote(parsed.path).rstrip("/")
        # -- auth endpoints (no auth gate) --
        if request_path == "/api/auth/login":
            self._handle_login()
            return
        if request_path == "/api/auth/logout":
            self._handle_logout()
            return
        if request_path == "/api/auth/users":
            if not self._check_write_auth():
                return
            self._handle_create_user()
            return
        if request_path == "/api/auth/change-password":
            if not self._check_write_auth():
                return
            self._handle_change_password()
            return
        # -- write endpoints (require auth) --
        if not self._check_write_auth():
            return
        if request_path == "/api/skills":
            try:
                payload = self._read_json_body()
                self._send_json({"skill": self._create_skill(payload)}, status=201)
            except SkillManagerError as exc:
                self._send_json({"error": str(exc)}, status=exc.status_code)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
            return
        if request_path == "/api/knowledge/sync":
            self._send_json(self._knowledge_sync())
            return
        if request_path == "/api/knowledge/sources":
            try:
                payload = self._read_json_body()
                self._send_json({"source": self._knowledge_register_source(payload)}, status=201)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
            return
        if request_path == "/api/knowledge/items":
            try:
                payload = self._read_json_body()
                self._send_json({"item": self._knowledge_add_item(payload)}, status=201)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
            return
        if request_path.startswith("/api/skills/") and request_path.endswith("/route"):
            skill_name = request_path.removeprefix("/api/skills/").removesuffix("/route").strip("/")
            try:
                payload = self._read_json_body()
                self._send_json({"skill": self._route_skill(unquote(skill_name), payload)})
            except SkillManagerError as exc:
                self._send_json({"error": str(exc)}, status=exc.status_code)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
            return
        if request_path.startswith("/api/skills/") and request_path.endswith("/debug"):
            skill_name = request_path.removeprefix("/api/skills/").removesuffix("/debug").strip("/")
            try:
                payload = self._read_json_body()
                self._send_json(self._debug_skill(unquote(skill_name), payload))
            except SkillManagerError as exc:
                self._send_json({"error": str(exc)}, status=exc.status_code)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
            return
        if request_path.startswith("/api/cases/") and request_path.endswith("/confirm"):
            case_id = request_path.removeprefix("/api/cases/").removesuffix("/confirm").strip("/")
            try:
                payload = self._read_json_body()
                case = self._confirm_case(unquote(case_id), payload)
                if case is None:
                    self._send_json({"error": "case not found"}, status=404)
                    return
                self._send_json({"case": case})
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
            return
        if request_path.startswith("/api/sessions/") and request_path.endswith("/terminate"):
            session_id = request_path.removeprefix("/api/sessions/").removesuffix("/terminate").strip("/")
            payload, status = self._terminate_session(unquote(session_id))
            self._send_json(payload, status=status)
            return
        self._send_json({"error": "unsupported endpoint"}, status=404)

    def do_PUT(self) -> None:
        if not self._check_write_auth():
            return
        self._handle_skill_update()

    def do_PATCH(self) -> None:
        if not self._check_write_auth():
            return
        self._handle_skill_update()

    def do_DELETE(self) -> None:
        if not self._check_write_auth():
            return
        parsed = urlsplit(self.path)
        request_path = unquote(parsed.path).rstrip("/")
        if request_path.startswith("/api/auth/users/"):
            username = request_path.removeprefix("/api/auth/users/").strip("/")
            if self.admin_auth and self.admin_auth.delete_user(unquote(username)):
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "user not found"}, status=404)
            return
        if request_path.startswith("/api/analysis-history/"):
            session_id = request_path.removeprefix("/api/analysis-history/").strip("/")
            payload, status = self._delete_analysis_history(unquote(session_id))
            self._send_json(payload, status=status)
            return
        if not request_path.startswith("/api/skills/"):
            self._send_json({"error": "unsupported endpoint"}, status=404)
            return
        skill_name = request_path.removeprefix("/api/skills/").strip("/")
        try:
            self._send_json({"deleted": self._delete_skill(unquote(skill_name))})
        except SkillManagerError as exc:
            self._send_json({"error": str(exc)}, status=exc.status_code)

    def log_message(self, format: str, *args) -> None:
        return

    def end_headers(self) -> None:
        if self._disable_cache_for_response:
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def _rewrite_report_path(self) -> bool:
        parsed = urlsplit(self.path)
        request_path = unquote(parsed.path)
        normalized_prefix = self.url_prefix.rstrip("/") or "/"
        if normalized_prefix != "/" and not request_path.startswith(normalized_prefix):
            return False
        stripped = request_path[len(normalized_prefix) :] if normalized_prefix != "/" else request_path
        stripped = "/" + stripped.lstrip("/")
        if parsed.query:
            stripped = f"{stripped}?{parsed.query}"
        self.path = stripped
        return True


def _build_handler(
    root_dir: Path,
    prefix: str,
    config: BridgeConfig,
    activity_store: AgentActivityStore | None = None,
    case_store: CaseStore | None = None,
    skill_manager: SkillManager | None = None,
    health_monitor: object | None = None,
    process_watchdog: ProcessWatchdog | None = None,
    conversation_store: ConversationContextStore | None = None,
    version_store: ReportVersionStore | None = None,
    knowledge_service: KnowledgeService | None = None,
    admin_auth: AdminAuth | None = None,
    lifecycle_store: object | None = None,
    get_dispatcher_metrics=None,
):
    namespace = {
        "root_dir": root_dir,
        "url_prefix": prefix,
        "bridge_config": config,
        "activity_store": activity_store,
        "case_store": case_store,
        "skill_manager": skill_manager,
        "health_monitor": health_monitor,
        "process_watchdog": process_watchdog,
        "conversation_store": conversation_store,
        "version_store": version_store,
        "knowledge_service": knowledge_service,
        "admin_auth": admin_auth,
        "lifecycle_store": lifecycle_store,
        # staticmethod keeps the zero-arg provider from being bound as a method.
        "get_dispatcher_metrics": staticmethod(get_dispatcher_metrics) if get_dispatcher_metrics is not None else None,
    }
    return type("_ReportHandler", (ReportRequestHandler,), namespace)
