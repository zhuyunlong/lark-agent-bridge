"""Publish generated HTML reports and serve them over HTTP."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import shutil
import socket
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

from .admin_ui import render_admin_page
from .auth import AdminAuth
from .case_store import CaseStore
from .health import ProcessWatchdog
from .knowledge import KnowledgeService
from .log import get_logger

logger = get_logger("server")
from .models import BridgeConfig, TaskResult
from .report_version import ReportVersionStore
from .state import AgentActivityStore, ConversationContextStore
from .skill_manager import SkillManager, SkillManagerError


@dataclass(slots=True)
class PublishedReport:
    slug: str
    url: str
    directory: Path
    index_path: Path
    report_paths: list[Path]
    source_report_paths: list[Path]
    context_excerpt: str


class HtmlReportPublisher:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config

    @property
    def root_dir(self) -> Path:
        return self.config.data_dir / "published_reports"

    @property
    def public_base_url(self) -> str:
        return resolve_public_base_url(
            self.config.report_server.public_base_url,
            port=self.config.report_server.port,
            bind_host=self.config.report_server.bind_host,
        )

    def publish_result(self, result: TaskResult) -> PublishedReport | None:
        if not self.config.report_server.enabled:
            return None
        html_paths = self._collect_html_paths(result)
        if not html_paths:
            return None
        slug = _safe_slug(result.job_id or "manual-report")
        target_dir = self.root_dir / slug
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        copied_paths: list[Path] = []
        for index, source in enumerate(html_paths, start=1):
            if not source.is_file():
                continue
            name = "report.html" if len(html_paths) == 1 else f"report-{index}.html"
            destination = target_dir / name
            shutil.copy2(source, destination)
            copied_paths.append(destination)
        if not copied_paths:
            return None

        context_excerpt = self._build_context_excerpt(result.message, copied_paths)
        index_path = target_dir / "index.html"
        index_path.write_text(
            self._render_index(
                summary_text=result.message,
                mode=str(result.details.get("mode", "")),
                reports=copied_paths,
                result=result,
            ),
            encoding="utf-8",
        )
        (target_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "job_id": result.job_id,
                    "mode": result.details.get("mode", ""),
                    "source_reports": [str(path) for path in html_paths],
                    "published_reports": [path.name for path in copied_paths],
                    "analysis_skill": result.details.get("analysis_skill", ""),
                    "classification_source": result.details.get("classification_source", ""),
                    "classification_provider": result.details.get("classification_provider", ""),
                    "agent_summary_provider": result.details.get("agent_summary_provider", ""),
                    "agent_summary_duration_seconds": result.details.get("agent_summary_duration_seconds"),
                    "agent_summary_input_tokens": result.details.get("agent_summary_input_tokens"),
                    "agent_summary_output_tokens": result.details.get("agent_summary_output_tokens"),
                    "agent_summary_total_tokens": result.details.get("agent_summary_total_tokens"),
                    "duration_seconds": result.duration_seconds,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return PublishedReport(
            slug=slug,
            url=self._url_for_slug(slug),
            directory=target_dir,
            index_path=index_path,
            report_paths=copied_paths,
            source_report_paths=html_paths,
            context_excerpt=context_excerpt,
        )

    def purge_all_reports(self) -> int:
        root = self.root_dir
        if not root.exists():
            return 0
        removed = 0
        for child in root.iterdir():
            if not child.is_dir():
                continue
            shutil.rmtree(child)
            removed += 1
        return removed

    def cleanup_expired_reports(self, *, max_age_hours: int) -> int:
        if max_age_hours <= 0:
            return 0
        root = self.root_dir
        if not root.exists():
            return 0
        cutoff_seconds = max_age_hours * 3600
        current_time = _current_timestamp()
        removed = 0
        for child in root.iterdir():
            if not child.is_dir():
                continue
            age_seconds = current_time - _latest_mtime(child)
            if age_seconds <= cutoff_seconds:
                continue
            shutil.rmtree(child)
            removed += 1
        return removed

    def _collect_html_paths(self, result: TaskResult) -> list[Path]:
        candidates: list[Path] = []
        if result.html_report is not None:
            candidates.append(Path(result.html_report))
        combined_report = result.details.get("combined_report_html")
        if isinstance(combined_report, str) and combined_report:
            candidates.append(Path(combined_report))
        files_to_send = result.details.get("files_to_send", [])
        if isinstance(files_to_send, list):
            for item in files_to_send:
                path = Path(item)
                if path.suffix.lower() == ".html":
                    candidates.append(path)
        unique: list[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            resolved = candidate.expanduser().resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            unique.append(resolved)
        return unique

    def _build_context_excerpt(self, summary_text: str, reports: list[Path]) -> str:
        parts: list[str] = []
        cleaned_summary = summary_text.strip()
        if cleaned_summary:
            parts.append("结果摘要:\n" + cleaned_summary)
        parser = _HtmlTextExtractor()
        for report in reports:
            parser.reset_text()
            parser.feed(report.read_text(encoding="utf-8", errors="replace"))
            text = parser.text().strip()
            if not text:
                continue
            parts.append(f"报告摘录（{report.name}）:\n{text}")
        combined = "\n\n".join(parts).strip()
        limit = max(500, int(self.config.omlx_chat.followup_max_context_chars))
        if len(combined) <= limit:
            return combined
        return combined[: limit - 1].rstrip() + "…"

    def _render_index(self, *, summary_text: str, mode: str, reports: list[Path], result: TaskResult) -> str:
        preview_text = _summary_preview_text(summary_text)
        report_sections = "\n".join(
            (
                "<section class=\"report-card\">"
                f"<h2>{escape(_report_title(mode, index, len(reports)))}</h2>"
                "<p class=\"muted\">完整报告较长，包含详细证据、图表和运行信息；首页只保留摘要和入口，避免重复嵌套展示。请在新窗口打开完整报告。</p>"
                f"<p><a class=\"report-link\" href=\"{quote(report.name)}\" target=\"_blank\" rel=\"noreferrer\">打开 HTML 报告</a></p>"
                "</section>"
            )
            for index, report in enumerate(reports, start=1)
        )
        runtime_items = _runtime_summary_items(result)
        runtime_html = ""
        if runtime_items:
            runtime_html = (
                "<section class=\"summary runtime\">"
                "<h2>运行信息</h2>"
                "<dl class=\"meta-grid\">"
                + "".join(
                    f"<div class=\"meta-item\"><dt>{escape(label)}</dt><dd>{escape(value)}</dd></div>"
                    for label, value in runtime_items
                )
                + "</dl></section>"
            )
        return (
            "<!doctype html>\n"
            "<html lang=\"zh-CN\">\n"
            "<head>\n"
            "  <meta charset=\"utf-8\" />\n"
            "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />\n"
            "  <title>Lark Agent Bridge Report</title>\n"
            "  <style>\n"
            "    :root { --bg:#eef3ff; --panel:#fff; --text:#14213d; --muted:#5f6b85; --border:#d8e1f4; --blue:#2563eb; --purple:#7c3aed; --cyan:#0891b2; }\n"
            "    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 24px; background: radial-gradient(circle at top left, rgba(37,99,235,.10), transparent 28%), radial-gradient(circle at top right, rgba(124,58,237,.10), transparent 26%), linear-gradient(180deg, #f4f7ff 0%, var(--bg) 100%); color: var(--text); }\n"
            "    .shell { max-width: 1240px; margin: 0 auto; }\n"
            "    .summary, .report-card { background: linear-gradient(180deg, rgba(255,255,255,.96), rgba(248,251,255,.96)); border-radius: 18px; box-shadow: 0 14px 36px rgba(15, 23, 42, 0.09); padding: 22px; margin-bottom: 20px; border: 1px solid var(--border); }\n"
            "    .runtime h2 { margin-bottom: 16px; }\n"
            "    .meta-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 0; }\n"
            "    .meta-item { background: linear-gradient(180deg, rgba(255,255,255,.98), rgba(238,244,255,.96)); border: 1px solid var(--border); border-radius: 14px; padding: 12px 14px; }\n"
            "    .meta-item dt { font-size: 12px; color: var(--muted); margin-bottom: 4px; }\n"
            "    .meta-item dd { margin: 0; font-size: 16px; font-weight: 700; color: var(--text); word-break: break-word; }\n"
            "    .lead { font-size: 16px; line-height: 1.75; color: var(--text); margin-bottom: 12px; white-space: pre-wrap; }\n"
            "    .muted { color: var(--muted); line-height: 1.7; }\n"
            "    .report-link { display: inline-flex; align-items: center; justify-content: center; padding: 10px 14px; border-radius: 10px; border: 1px solid rgba(37,99,235,.28); background: rgba(37,99,235,.08); text-decoration: none; }\n"
            "    h1, h2 { margin-top: 0; }\n"
            "    h1 { color: var(--blue); font-size: 28px; }\n"
            "    h2 { color: var(--cyan); }\n"
            "    a { color: var(--blue); font-weight: 600; }\n"
            "  </style>\n"
            "</head>\n"
            "<body>\n"
            "  <main class=\"shell\">\n"
            "    <section class=\"summary\">\n"
            "      <h1>分析结果</h1>\n"
            f"      <div class=\"lead\">{escape(preview_text)}</div>\n"
            "    </section>\n"
            f"    {runtime_html}\n"
            f"    {report_sections}\n"
            "  </main>\n"
            "</body>\n"
            "</html>\n"
        )

    def _url_for_slug(self, slug: str) -> str:
        base = self.public_base_url.rstrip("/")
        return f"{base}/{quote(slug)}/"


class ReportHttpServer:
    def __init__(
        self,
        config: BridgeConfig,
        *,
        activity_store: AgentActivityStore | None = None,
        case_store: CaseStore | None = None,
        skill_manager: SkillManager | None = None,
        health_monitor: object | None = None,
        process_watchdog: ProcessWatchdog | None = None,
        conversation_store: ConversationContextStore | None = None,
        version_store: ReportVersionStore | None = None,
        knowledge_service: KnowledgeService | None = None,
    ) -> None:
        self.config = config
        self.activity_store = activity_store
        self.case_store = case_store
        self.skill_manager = skill_manager
        self.health_monitor = health_monitor
        self.process_watchdog = process_watchdog
        self.conversation_store = conversation_store
        self.version_store = version_store
        self.knowledge_service = knowledge_service
        self._admin_auth: AdminAuth | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.config.report_server.enabled or self._server is not None:
            return
        root_dir = self.config.data_dir / "published_reports"
        root_dir.mkdir(parents=True, exist_ok=True)
        self._admin_auth = AdminAuth(
            self.config.data_dir,
            admin_token=self.config.report_server.admin_token,
        )
        prefix = _url_prefix(
            resolve_public_base_url(
                self.config.report_server.public_base_url,
                port=self.config.report_server.port,
                bind_host=self.config.report_server.bind_host,
            )
        )
        handler = _build_handler(
            root_dir,
            prefix,
            self.activity_store,
            self.case_store,
            self.skill_manager,
            self.health_monitor,
            self.process_watchdog,
            self.conversation_store,
            self.version_store,
            self.knowledge_service,
            self._admin_auth,
        )
        self._server = ThreadingHTTPServer(
            (resolve_bind_host(self.config.report_server.bind_host), self.config.report_server.port),
            handler,
        )
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="report-http-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self._parts.append(text)

    def reset_text(self) -> None:
        self._parts = []
        self.reset()

    def text(self) -> str:
        return "\n".join(self._parts)


def _build_handler(
    root_dir: Path,
    prefix: str,
    activity_store: AgentActivityStore | None = None,
    case_store: CaseStore | None = None,
    skill_manager: SkillManager | None = None,
    health_monitor: object | None = None,
    process_watchdog: ProcessWatchdog | None = None,
    conversation_store: ConversationContextStore | None = None,
    version_store: ReportVersionStore | None = None,
    knowledge_service: KnowledgeService | None = None,
    admin_auth: AdminAuth | None = None,
):
    class _ReportHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root_dir), **kwargs)

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
                self._send_json({"users": admin_auth.list_users() if admin_auth else []})
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
            ):
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                return
            if not self._rewrite_report_path():
                self.send_error(404)
                return
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
                if admin_auth and admin_auth.delete_user(unquote(username)):
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

        def _rewrite_report_path(self) -> bool:
            parsed = urlsplit(self.path)
            request_path = unquote(parsed.path)
            normalized_prefix = prefix.rstrip("/") or "/"
            if normalized_prefix != "/" and not request_path.startswith(normalized_prefix):
                return False
            stripped = request_path[len(normalized_prefix) :] if normalized_prefix != "/" else request_path
            stripped = "/" + stripped.lstrip("/")
            if parsed.query:
                stripped = f"{stripped}?{parsed.query}"
            self.path = stripped
            return True

        def _list_sessions(self) -> list[dict[str, object]]:
            if activity_store is None:
                return []
            return activity_store.list_sessions(limit=200)

        def _list_cases(self, query: str) -> list[dict[str, object]]:
            if case_store is None:
                return []
            params = parse_qs(query)
            limit = _query_int(params, "limit", 200)
            keyword = _query_str(params, "keyword")
            problem_type = _query_str(params, "problem_type")
            analysis_mode = _query_str(params, "analysis_mode")
            root_cause_tag = _query_str(params, "root_cause_tag")
            if any([keyword, problem_type, analysis_mode, root_cause_tag]):
                cases = case_store.search(
                    keyword=keyword,
                    problem_type=problem_type,
                    analysis_mode=analysis_mode,
                    root_cause_tag=root_cause_tag,
                    limit=limit,
                )
            elif _query_str(params, "all") in {"1", "true", "yes"}:
                cases = case_store.list_recent(limit=limit)
            else:
                cases = case_store.list_latest_by_bug(limit=limit)
            return [case.to_dict() for case in cases]

        def _list_analysis_history(self, query: str) -> list[dict[str, object]]:
            if activity_store is None:
                return []
            params = parse_qs(query)
            limit = _query_int(params, "limit", 200)
            keyword = _query_str(params, "keyword").casefold()
            mode = _query_str(params, "mode")
            status = _query_str(params, "status")
            items: list[dict[str, object]] = []
            for session in activity_store.list_sessions(limit=None):
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
            if activity_store is None:
                return None
            session = activity_store.get_session(session_id)
            if session is None:
                return None
            if not _is_analysis_history_mode(str(session.get("mode") or "")):
                return None
            return session

        def _delete_analysis_history(self, session_id: str) -> tuple[dict[str, object], int]:
            if activity_store is None:
                return {"ok": False, "error": "activity store not configured"}, 503
            session = activity_store.get_session(session_id)
            if session is None:
                return {"ok": False, "error": "analysis history not found"}, 404
            if not _is_analysis_history_mode(str(session.get("mode") or "")):
                return {"ok": False, "error": "not an analysis history record"}, 409
            authorization = _delete_authorization_placeholder()
            deleted_session = activity_store.delete_session(session_id) or session
            job_id = str(session.get("job_id") or "").strip()
            deleted_case: dict[str, object] | None = None
            if case_store is not None:
                case = case_store.delete_by_job_id(job_id) if job_id else None
                if case is None:
                    case_id = str(session.get("case_id") or "").strip()
                    case = case_store.delete(case_id) if case_id else None
                deleted_case = case.to_dict() if case is not None else None
            removed_contexts = 0
            if conversation_store is not None:
                removed_contexts += conversation_store.delete(session_id)
                details = session.get("details")
                if isinstance(details, dict):
                    root_message_id = str(details.get("conversation_root_message_id") or "").strip()
                    if root_message_id and root_message_id != session_id:
                        removed_contexts += conversation_store.delete(root_message_id)
            removed_versions = version_store.delete_by_job_id(job_id) if version_store is not None and job_id else 0
            removed_paths: list[str] = []
            for path in _history_paths_to_delete(session, root_dir=root_dir):
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
            if case_store is None:
                return None
            case = case_store.get(case_id)
            return case.to_dict() if case is not None else None

        def _confirm_case(self, case_id: str, payload: dict[str, object]) -> dict[str, object] | None:
            if case_store is None:
                return None
            confirmed = bool(payload.get("confirmed", True))
            notes = str(payload.get("notes") or "")
            case = case_store.update_human_confirmation(case_id, confirmed=confirmed, notes=notes)
            return case.to_dict() if case is not None else None

        def _list_skills(self) -> list[dict[str, object]]:
            if skill_manager is None:
                return []
            return [item.to_dict() for item in skill_manager.list_skills()]

        def _get_skill(self, name: str) -> dict[str, object]:
            if skill_manager is None:
                raise SkillManagerError("skill manager not configured", status_code=503)
            return skill_manager.get_skill(name).to_dict(include_content=True)

        def _create_skill(self, payload: dict[str, object]) -> dict[str, object]:
            if skill_manager is None:
                raise SkillManagerError("skill manager not configured", status_code=503)
            name = str(payload.get("name") or "")
            content = str(payload.get("content") or "")
            description = str(payload.get("description") or "")
            label = str(payload.get("label") or "")
            return skill_manager.create_skill(
                name=name,
                content=content,
                description=description,
                label=label,
            ).to_dict(include_content=True)

        def _update_skill(self, name: str, payload: dict[str, object]) -> dict[str, object]:
            if skill_manager is None:
                raise SkillManagerError("skill manager not configured", status_code=503)
            content = str(payload.get("content") or "")
            if not content.strip():
                raise ValueError("content 不能为空")
            return skill_manager.update_skill(name, content=content).to_dict(include_content=True)

        def _route_skill(self, name: str, payload: dict[str, object]) -> dict[str, object]:
            if skill_manager is None:
                raise SkillManagerError("skill manager not configured", status_code=503)
            role = str(payload.get("role") or "")
            kind = str(payload.get("kind") or "")
            requires_logs_value = payload.get("requires_logs")
            requires_logs = requires_logs_value if isinstance(requires_logs_value, bool) else None
            return skill_manager.set_skill_route(
                name,
                role=role,
                kind=kind,
                requires_logs=requires_logs,
            ).to_dict(include_content=True)

        def _delete_skill(self, name: str) -> dict[str, object]:
            if skill_manager is None:
                raise SkillManagerError("skill manager not configured", status_code=503)
            return skill_manager.delete_skill(name).to_dict(include_content=False)

        def _debug_skill(self, name: str, payload: dict[str, object]) -> dict[str, object]:
            if skill_manager is None:
                raise SkillManagerError("skill manager not configured", status_code=503)
            sample_text = str(payload.get("sample_text") or "")
            return skill_manager.debug_skill(name, sample_text=sample_text)

        def _get_session(self, session_id: str) -> dict[str, object] | None:
            if activity_store is None:
                return None
            return activity_store.get_session(session_id)

        def _terminate_session(self, session_id: str) -> tuple[dict[str, object], int]:
            if activity_store is None:
                return {"ok": False, "error": "activity store not configured"}, 503
            session = activity_store.get_session(session_id)
            if session is None:
                return {"ok": False, "error": "session not found"}, 404
            if not bool(session.get("can_terminate")):
                return {
                    "ok": False,
                    "error": "session is not running",
                    "session": session,
                }, 409
            terminated: list[dict[str, object]] = []
            if process_watchdog is not None:
                terminated = process_watchdog.terminate_session(session_id)
            cancelled = activity_store.cancel_session(
                session_id,
                reason="后台管理页请求终止任务。",
                terminated_processes=terminated,
            )
            return {"ok": True, "terminated": terminated, "session": cancelled or session}, 200

        def _get_daemon_status(self) -> dict[str, object]:
            if activity_store is None:
                return {}
            return activity_store.get_daemon_status()

        def _get_health(self) -> dict[str, object]:
            if health_monitor is None:
                return {"healthy": True, "note": "health monitor not configured"}
            check_method = getattr(health_monitor, "check_health", None)
            if check_method is None:
                return {"healthy": True, "note": "health monitor has no check_health method"}
            status = check_method()
            to_dict = getattr(status, "to_dict", None)
            if to_dict is not None:
                return to_dict()
            return {"healthy": True}

        def _knowledge_sources(self) -> list[dict[str, object]]:
            if knowledge_service is None:
                return []
            return knowledge_service.list_sources()

        def _knowledge_search(self, query: str) -> list[dict[str, object]]:
            if knowledge_service is None:
                return []
            params = parse_qs(query)
            q = _query_str(params, "q") or _query_str(params, "query")
            limit = _query_int(params, "limit", 20)
            return [hit.to_dict() for hit in knowledge_service.search(q, limit=limit)]

        def _knowledge_sync(self) -> dict[str, object]:
            if knowledge_service is None:
                return {"source_count": 0, "total_chunks": 0, "sources": []}
            return knowledge_service.sync_all()

        def _knowledge_register_source(self, payload: dict[str, object]) -> dict[str, object]:
            if knowledge_service is None:
                raise ValueError("knowledge service not configured")
            return knowledge_service.register_source(
                source_id=str(payload.get("source_id") or payload.get("id") or ""),
                source_type=str(payload.get("type") or "manual"),
                title=str(payload.get("title") or ""),
                source_ref=str(payload.get("source_ref") or payload.get("url") or payload.get("path") or ""),
            )

        def _knowledge_add_item(self, payload: dict[str, object]) -> dict[str, object]:
            if knowledge_service is None:
                raise ValueError("knowledge service not configured")
            return knowledge_service.add_text(
                source_id=str(payload.get("source_id") or "manual"),
                title=str(payload.get("title") or ""),
                content=str(payload.get("content") or ""),
                source_ref=str(payload.get("source_ref") or ""),
            )

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload: object, *, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("请求体不是合法 JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return payload

        # -- auth helpers ------------------------------------------------

        def _get_bearer_token(self) -> str:
            header = self.headers.get("Authorization") or ""
            if header.lower().startswith("bearer "):
                return header[7:].strip()
            return ""

        def _check_write_auth(self) -> bool:
            """Return True if write is allowed; sends 401/403 and returns False otherwise."""
            if admin_auth is None or not admin_auth.auth_required:
                return True
            token = self._get_bearer_token()
            user = admin_auth.check_token(token)
            if user is None:
                self._send_json({"error": "未认证，请先登录"}, status=401)
                return False
            if user.get("role") == "viewer":
                self._send_json({"error": "权限不足，viewer 角色不能执行写操作"}, status=403)
                return False
            return True

        def _auth_status(self) -> dict[str, object]:
            if admin_auth is None:
                return {"required": False, "has_users": False, "has_token": False}
            return {
                "required": admin_auth.auth_required,
                "has_users": admin_auth.has_users,
                "has_token": bool(admin_auth._admin_token),
            }

        def _handle_login(self) -> None:
            try:
                payload = self._read_json_body()
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            # Static token login
            token = str(payload.get("token") or "").strip()
            if token and admin_auth is not None:
                user = admin_auth.check_token(token)
                if user is not None:
                    self._send_json({"ok": True, "token": token, **user})
                    return
            # Username/password login
            username = str(payload.get("username") or "").strip()
            password = str(payload.get("password") or "")
            if username and admin_auth is not None:
                session = admin_auth.login(username, password)
                if session is not None:
                    self._send_json({
                        "ok": True,
                        "token": session.token,
                        "username": session.username,
                        "role": session.role,
                        "expires_at": session.expires_at,
                    })
                    return
            self._send_json({"error": "认证失败"}, status=401)

        def _handle_logout(self) -> None:
            token = self._get_bearer_token()
            if token and admin_auth is not None:
                admin_auth.logout(token)
            self._send_json({"ok": True})

        def _handle_create_user(self) -> None:
            if admin_auth is None:
                self._send_json({"error": "auth not configured"}, status=503)
                return
            try:
                payload = self._read_json_body()
                username = str(payload.get("username") or "").strip()
                password = str(payload.get("password") or "")
                role = str(payload.get("role") or "admin")
                user = admin_auth.create_user(username, password, role)
                self._send_json({"ok": True, "user": {"username": user.username, "role": user.role}}, status=201)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)

        def _handle_change_password(self) -> None:
            if admin_auth is None:
                self._send_json({"error": "auth not configured"}, status=503)
                return
            try:
                payload = self._read_json_body()
                username = str(payload.get("username") or "").strip()
                old_password = str(payload.get("old_password") or "")
                new_password = str(payload.get("new_password") or "")
                if admin_auth.change_password(username, old_password, new_password):
                    self._send_json({"ok": True})
                else:
                    self._send_json({"error": "原密码错误或用户不存在"}, status=401)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)

        def _handle_skill_update(self) -> None:
            parsed = urlsplit(self.path)
            request_path = unquote(parsed.path).rstrip("/")
            if not request_path.startswith("/api/skills/"):
                self._send_json({"error": "unsupported endpoint"}, status=404)
                return
            skill_name = request_path.removeprefix("/api/skills/").strip("/")
            try:
                payload = self._read_json_body()
                self._send_json({"skill": self._update_skill(unquote(skill_name), payload)})
            except SkillManagerError as exc:
                self._send_json({"error": str(exc)}, status=exc.status_code)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)

    return _ReportHandler


def _query_str(params: dict[str, list[str]], key: str) -> str:
    values = params.get(key) or []
    return str(values[0]).strip() if values else ""


def _query_int(params: dict[str, list[str]], key: str, default: int) -> int:
    raw = _query_str(params, key)
    if not raw:
        return default
    try:
        return max(1, min(int(raw), 1000))
    except ValueError:
        return default


_ANALYSIS_HISTORY_MODES = {
    "bug_analysis",
    "direct_analysis",
    "signal_lifecycle",
    "perception_summary",
}


def _is_analysis_history_mode(mode: str) -> bool:
    return mode.strip() in _ANALYSIS_HISTORY_MODES


def _history_search_text(session: dict[str, object]) -> str:
    values = [
        session.get("session_id"),
        session.get("event_id"),
        session.get("chat_id"),
        session.get("mode"),
        session.get("status"),
        session.get("content"),
        session.get("message"),
        session.get("job_id"),
        session.get("report_url"),
    ]
    return "\n".join(str(value or "") for value in values)


def _delete_authorization_placeholder() -> dict[str, object]:
    return {
        "checked": True,
        "scope": "analysis_history.delete",
        "note": "写操作已通过 admin_auth 鉴权保护。",
    }


def _history_paths_to_delete(session: dict[str, object], *, root_dir: Path) -> list[Path]:
    data_dir = root_dir.parent
    jobs_root = data_dir / "jobs"
    raw_candidates: list[Path] = []
    job_dir = str(session.get("job_dir") or "").strip()
    job_id = str(session.get("job_id") or "").strip()
    if job_dir:
        raw_candidates.append(Path(job_dir))
    if job_id:
        raw_candidates.append(jobs_root / job_id)
        raw_candidates.append(root_dir / _safe_slug(job_id))
    for key in ("published_report_index", "published_report_dir"):
        details = session.get("details")
        if isinstance(details, dict):
            value = str(details.get(key) or "").strip()
            if value:
                raw_candidates.append(Path(value))

    candidates: list[Path] = []
    seen: set[Path] = set()
    for path in raw_candidates:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            resolved = path.expanduser()
        if not (_path_within(resolved, jobs_root) or _path_within(resolved, root_dir)):
            continue
        if resolved in {jobs_root.resolve(), root_dir.resolve()}:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        candidates.append(resolved)
    return candidates


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _remove_history_path(path: Path) -> bool:
    try:
        if not path.exists():
            return False
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return True
    except OSError:
        return False


def _render_sessions_page() -> str:
    return render_admin_page()


def _safe_slug(value: str) -> str:
    cleaned = []
    for char in value:
        if char.isalnum() or char in {"-", "_", "."}:
            cleaned.append(char)
        else:
            cleaned.append("-")
    slug = "".join(cleaned).strip("-._")
    return slug or "report"


def _current_timestamp() -> float:
    return time.time()


def resolve_bind_host(bind_host: str) -> str:
    value = (bind_host or "").strip()
    if not value:
        return "127.0.0.1"
    return value


def resolve_public_base_url(public_base_url: str, *, port: int, bind_host: str = "") -> str:
    raw = (public_base_url or "").strip()
    resolved_bind = resolve_bind_host(bind_host)
    bind_is_loopback = _is_loopback_host(resolved_bind)
    if not raw:
        # When bound to loopback only, the LAN IP is unreachable — use loopback.
        host = "127.0.0.1" if bind_is_loopback else _detect_lan_ip()
        raw = f"http://{host}:{port}/reports"
    parts = urlsplit(raw)
    host = parts.hostname or ""
    if not _should_replace_public_host(host):
        return raw.rstrip("/")
    scheme = parts.scheme or "http"
    path = parts.path or "/reports"
    resolved_host = "127.0.0.1" if bind_is_loopback else _detect_lan_ip()
    effective_port = parts.port or port
    netloc = f"{resolved_host}:{effective_port}"
    if parts.username:
        auth = parts.username
        if parts.password:
            auth += f":{parts.password}"
        netloc = f"{auth}@{netloc}"
    return urlunsplit((scheme, netloc, path.rstrip("/"), parts.query, parts.fragment)).rstrip("/")


def _latest_mtime(path: Path) -> float:
    latest = path.stat().st_mtime
    for child in path.rglob("*"):
        try:
            child_mtime = child.stat().st_mtime
        except OSError:
            continue
        if child_mtime > latest:
            latest = child_mtime
    return latest


def _report_title(mode: str, index: int, total: int) -> str:
    if total == 1:
        return "HTML 报告"
    return f"{mode or 'analysis'} 报告 {index}"


def _runtime_summary_items(result: TaskResult) -> list[tuple[str, str]]:
    details = result.details if isinstance(result.details, dict) else {}
    items: list[tuple[str, str]] = []
    skill = str(details.get("analysis_skill") or "").strip()
    if skill:
        items.append(("命中 Skill", skill))
    classification_source = str(details.get("classification_source") or "").strip()
    if classification_source:
        items.append(("分类来源", classification_source))
    classification_provider = str(details.get("classification_provider") or "").strip()
    if classification_provider:
        items.append(("分类 Agent", classification_provider))
    provider = str(details.get("agent_summary_provider") or details.get("provider") or "").strip()
    if provider:
        items.append(("Agent 类型", provider))
    input_tokens = details.get("agent_summary_input_tokens")
    output_tokens = details.get("agent_summary_output_tokens")
    total_tokens = details.get("agent_summary_total_tokens")
    usage_scope = str(details.get("agent_summary_usage_scope") or "").strip()
    if any(isinstance(value, int) for value in (input_tokens, output_tokens, total_tokens)):
        items.append(
            (
                "本轮 Agent Token" if usage_scope == "delta" else "累计 Agent Token",
                f"{_format_token_millions(input_tokens)} / "
                f"{_format_token_millions(output_tokens)} / "
                f"{_format_token_millions(total_tokens)}",
            )
        )
    agent_duration = details.get("agent_summary_duration_seconds")
    if isinstance(agent_duration, (int, float)):
        items.append(("Agent 耗时", f"{float(agent_duration):.1f} 秒"))
    if isinstance(result.duration_seconds, (int, float)):
        items.append(("总耗时", f"{float(result.duration_seconds):.1f} 秒"))
    return items


def _summary_preview_text(summary_text: str) -> str:
    lines = [line.strip() for line in summary_text.splitlines() if line.strip()]
    if not lines:
        return "分析完成"
    filtered: list[str] = []
    for line in lines:
        if line == "Bug 分析完成":
            continue
        if line.startswith("## "):
            if filtered:
                break
            continue
        normalized = line.lstrip("-*• \t")
        if normalized.startswith("诉求：") or normalized.startswith("诉求:"):
            continue
        filtered.append(line)
        if len(filtered) >= 4:
            break
    if not filtered:
        filtered = lines[:3]
    preview = "\n".join(filtered)
    if len(preview) <= 320:
        return preview
    return preview[:319].rstrip() + "…"


def _format_token_millions(value: object) -> str:
    if not isinstance(value, int):
        return "-"
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.2f}K"
    return f"{value / 1_000_000:.2f}M"


def _url_prefix(public_base_url: str) -> str:
    path = urlsplit(public_base_url).path or "/"
    return "/" + path.strip("/")


def _should_replace_public_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized in {"", "localhost", "0.0.0.0", "127.0.0.1", "::1"}:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _is_loopback_host(host: str) -> bool:
    """Return True if *host* is a loopback address (127.x.x.x, ::1, localhost)."""
    normalized = (host or "").strip().lower()
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _detect_lan_ip() -> str:
    for target_host in ("8.8.8.8", "1.1.1.1", "192.0.2.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((target_host, 80))
                candidate = sock.getsockname()[0]
        except OSError:
            continue
        if _is_usable_ip(candidate):
            return candidate
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        infos = []
    for info in infos:
        candidate = info[4][0]
        if _is_usable_ip(candidate):
            return candidate
    return "127.0.0.1"


def _is_usable_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (
        address.version == 4
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_unspecified
    )
