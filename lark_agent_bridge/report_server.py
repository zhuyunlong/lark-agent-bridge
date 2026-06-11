"""Publish generated HTML reports and serve them over HTTP (facade over report_http/).

The handler/rendering/history internals live in :mod:`lark_agent_bridge.report_http`.
This module keeps the public classes plus the public-URL/bind-host resolution
chain, because tests patch ``lark_agent_bridge.report_server._detect_lan_ip``
and that patch must stay visible to ``resolve_public_base_url``.
All former top-level symbols are re-exported for backwards compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import re
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
from .token_usage import extract_first_prefixed_token_usage, extract_prefixed_token_usage

from .report_http.history import (
    _ANALYSIS_HISTORY_MODES,
    _delete_authorization_placeholder,
    _history_paths_to_delete,
    _history_search_text,
    _is_analysis_history_mode,
    _path_within,
    _remove_history_path,
)
from .report_http.index_page import (
    _HtmlTextExtractor,
    _extract_report_title,
    _format_token_millions,
    _normalize_summary_preview_line,
    _report_title,
    _runtime_summary_items,
    _summary_preview_text,
    render_index_page,
)
from .report_http.knowledge_export import (
    _KNOWLEDGE_EXPORT_REPORT_NAME,
    _KNOWLEDGE_EXPORT_REPORT_ROUTES,
    _knowledge_export_report_path,
    _knowledge_export_report_status,
)
from .report_http.paths import _current_timestamp, _latest_mtime, _safe_slug
from .report_http.query import _query_int, _query_str
from .report_http.request_handler import (
    ReportRequestHandler,
    _build_handler,
    _render_sessions_page,
)


@dataclass(slots=True)
class PublishedReport:
    slug: str
    url: str
    directory: Path
    index_path: Path
    report_paths: list[Path]
    source_report_paths: list[Path]
    context_excerpt: str
    version: int = 0


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

    def publish_result(self, result: TaskResult, *, version: int | None = None) -> PublishedReport | None:
        if not self.config.report_server.enabled:
            return None
        html_paths = self._collect_html_paths(result)
        if not html_paths:
            return None
        slug = _safe_slug(result.job_id or "manual-report")
        slug_parts = [slug]
        normalized_version = int(version or 0)
        if normalized_version > 0:
            slug_parts.append(f"v{normalized_version}")
        published_slug = "/".join(slug_parts)
        target_dir = self.root_dir.joinpath(*slug_parts)
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
                    "report_version": normalized_version or result.details.get("report_version") or "",
                    "agent_summary_duration_seconds": result.details.get("agent_summary_duration_seconds"),
                    "agent_summary_input_tokens": result.details.get("agent_summary_input_tokens"),
                    "agent_summary_output_tokens": result.details.get("agent_summary_output_tokens"),
                    "agent_summary_total_tokens": result.details.get("agent_summary_total_tokens"),
                    "app_server_input_tokens": result.details.get("app_server_input_tokens"),
                    "app_server_cached_input_tokens": result.details.get("app_server_cached_input_tokens"),
                    "app_server_output_tokens": result.details.get("app_server_output_tokens"),
                    "app_server_total_tokens": result.details.get("app_server_total_tokens"),
                    "duration_seconds": result.duration_seconds,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return PublishedReport(
            slug=published_slug,
            url=self._url_for_slug(published_slug, primary_report=copied_paths[0] if len(copied_paths) == 1 else None),
            directory=target_dir,
            index_path=index_path,
            report_paths=copied_paths,
            source_report_paths=html_paths,
            context_excerpt=context_excerpt,
            version=normalized_version,
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
        return render_index_page(summary_text=summary_text, mode=mode, reports=reports, result=result)

    def _url_for_slug(self, slug: str, *, primary_report: Path | None = None) -> str:
        base = self.public_base_url.rstrip("/")
        slug_path = "/".join(quote(part) for part in slug.split("/"))
        if primary_report is not None:
            return f"{base}/{slug_path}/{quote(primary_report.name)}"
        return f"{base}/{slug_path}/"


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
        lifecycle_store: object | None = None,
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
        self.lifecycle_store = lifecycle_store
        # Set by cli after the dispatcher starts (the report server is created
        # earlier, in BridgeApp.__init__, so this can't be a constructor arg).
        self._dispatcher_metrics_provider = None
        self._admin_auth: AdminAuth | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def set_dispatcher_metrics_provider(self, provider) -> None:
        """Wire the live EventDispatcher.metrics callable for /api/health."""
        self._dispatcher_metrics_provider = provider

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
            self.config,
            self.activity_store,
            self.case_store,
            self.skill_manager,
            self.health_monitor,
            self.process_watchdog,
            self.conversation_store,
            self.version_store,
            self.knowledge_service,
            self._admin_auth,
            self.lifecycle_store,
            lambda: (self._dispatcher_metrics_provider() if self._dispatcher_metrics_provider else None),
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
