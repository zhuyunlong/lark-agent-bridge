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
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .models import BridgeConfig, TaskResult
from .state import AgentActivityStore


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

    def _render_index(self, *, summary_text: str, mode: str, reports: list[Path]) -> str:
        report_sections = "\n".join(
            (
                "<section class=\"report-card\">"
                f"<h2>{escape(_report_title(mode, index, len(reports)))}</h2>"
                f"<p><a href=\"{quote(report.name)}\" target=\"_blank\" rel=\"noreferrer\">打开原始 HTML</a></p>"
                f"<iframe src=\"{quote(report.name)}\" loading=\"lazy\"></iframe>"
                "</section>"
            )
            for index, report in enumerate(reports, start=1)
        )
        return (
            "<!doctype html>\n"
            "<html lang=\"zh-CN\">\n"
            "<head>\n"
            "  <meta charset=\"utf-8\" />\n"
            "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />\n"
            "  <title>Lark Agent Bridge Report</title>\n"
            "  <style>\n"
            "    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 24px; background: #f5f7fb; color: #1f2937; }\n"
            "    .shell { max-width: 1200px; margin: 0 auto; }\n"
            "    .summary, .report-card { background: #fff; border-radius: 14px; box-shadow: 0 10px 30px rgba(15, 23, 42, 0.08); padding: 20px; margin-bottom: 20px; }\n"
            "    pre { white-space: pre-wrap; word-break: break-word; margin: 0; }\n"
            "    iframe { width: 100%; min-height: 900px; border: 1px solid #dbe2f0; border-radius: 10px; background: #fff; }\n"
            "    h1, h2 { margin-top: 0; }\n"
            "    a { color: #2563eb; }\n"
            "  </style>\n"
            "</head>\n"
            "<body>\n"
            "  <main class=\"shell\">\n"
            "    <section class=\"summary\">\n"
            "      <h1>分析结果</h1>\n"
            f"      <pre>{escape(summary_text.strip() or '分析完成')}</pre>\n"
            "    </section>\n"
            f"    {report_sections}\n"
            "  </main>\n"
            "</body>\n"
            "</html>\n"
        )

    def _url_for_slug(self, slug: str) -> str:
        base = self.public_base_url.rstrip("/")
        return f"{base}/{quote(slug)}/"


class ReportHttpServer:
    def __init__(self, config: BridgeConfig, *, activity_store: AgentActivityStore | None = None) -> None:
        self.config = config
        self.activity_store = activity_store
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.config.report_server.enabled or self._server is not None:
            return
        root_dir = self.config.data_dir / "published_reports"
        root_dir.mkdir(parents=True, exist_ok=True)
        prefix = _url_prefix(resolve_public_base_url(self.config.report_server.public_base_url, port=self.config.report_server.port))
        handler = _build_handler(root_dir, prefix, self.activity_store)
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


def _build_handler(root_dir: Path, prefix: str, activity_store: AgentActivityStore | None = None):
    class _ReportHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root_dir), **kwargs)

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            request_path = unquote(parsed.path)
            if request_path in {"", "/"}:
                self.send_response(302)
                self.send_header("Location", "/sessions")
                self.end_headers()
                return
            if request_path in {"/sessions", "/sessions/"}:
                self._send_html(_render_sessions_page())
                return
            if request_path == "/api/sessions":
                self._send_json({"sessions": self._list_sessions()})
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
            if request_path in {"/sessions", "/sessions/"}:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                return
            if request_path == "/api/sessions" or request_path.startswith("/api/sessions/"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                return
            if not self._rewrite_report_path():
                self.send_error(404)
                return
            super().do_HEAD()

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

        def _get_session(self, session_id: str) -> dict[str, object] | None:
            if activity_store is None:
                return None
            return activity_store.get_session(session_id)

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

    return _ReportHandler


def _render_sessions_page() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Lark Agent Bridge Sessions</title>
  <style>
    :root { color-scheme: light; }
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f3f6fb; color: #172033; }
    header { padding: 18px 24px; background: #101827; color: #fff; display: flex; justify-content: space-between; align-items: center; }
    h1 { font-size: 20px; margin: 0; }
    button { border: 0; border-radius: 8px; padding: 8px 12px; background: #2563eb; color: #fff; cursor: pointer; }
    main { display: grid; grid-template-columns: minmax(320px, 420px) 1fr; gap: 16px; padding: 16px; }
    .panel { background: #fff; border-radius: 14px; box-shadow: 0 8px 28px rgba(15, 23, 42, 0.08); overflow: hidden; }
    .list { max-height: calc(100vh - 104px); overflow: auto; }
    .item { display: block; width: 100%; text-align: left; color: #172033; background: #fff; border: 0; border-bottom: 1px solid #e5eaf3; border-radius: 0; padding: 14px 16px; }
    .item:hover, .item.active { background: #eff6ff; }
    .row { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
    .title { font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .meta, .muted { color: #64748b; font-size: 12px; }
    .badge { border-radius: 999px; padding: 2px 8px; font-size: 12px; background: #e2e8f0; color: #334155; }
    .badge.succeeded { background: #dcfce7; color: #166534; }
    .badge.failed { background: #fee2e2; color: #991b1b; }
    .badge.running { background: #dbeafe; color: #1d4ed8; }
    .badge.skipped { background: #fef3c7; color: #92400e; }
    .detail { padding: 18px; max-height: calc(100vh - 104px); overflow: auto; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin: 14px 0; }
    .card { border: 1px solid #e5eaf3; border-radius: 10px; padding: 10px; background: #f8fafc; }
    pre { white-space: pre-wrap; word-break: break-word; background: #0f172a; color: #e2e8f0; border-radius: 10px; padding: 12px; overflow: auto; }
    .timeline { border-left: 2px solid #dbeafe; margin-left: 9px; padding-left: 16px; }
    .step { position: relative; padding-bottom: 14px; }
    .step::before { content: ''; position: absolute; left: -22px; top: 4px; width: 10px; height: 10px; border-radius: 999px; background: #2563eb; }
    a { color: #2563eb; }
    @media (max-width: 860px) { main { grid-template-columns: 1fr; } .list, .detail { max-height: none; } }
  </style>
</head>
<body>
  <header>
    <h1>Lark Agent Bridge 会话控制台</h1>
    <button id="refresh">刷新</button>
  </header>
  <main>
    <section class="panel list" id="sessions"></section>
    <section class="panel detail" id="detail"><p class="muted">选择左侧会话查看后台 agent 过程。</p></section>
  </main>
  <script>
    let selected = "";
    const sessionsEl = document.getElementById("sessions");
    const detailEl = document.getElementById("detail");
    const refreshButton = document.getElementById("refresh");

    function text(value) {
      return value === undefined || value === null || value === "" ? "-" : String(value);
    }

    function badge(status) {
      const span = document.createElement("span");
      span.className = "badge " + (status || "");
      span.textContent = status || "unknown";
      return span;
    }

    function renderSessions(items) {
      sessionsEl.textContent = "";
      if (!items.length) {
        const empty = document.createElement("p");
        empty.className = "muted";
        empty.style.padding = "16px";
        empty.textContent = "暂无会话。";
        sessionsEl.appendChild(empty);
        return;
      }
      for (const item of items) {
        const button = document.createElement("button");
        button.className = "item" + (item.session_id === selected ? " active" : "");
        button.dataset.sessionId = item.session_id || "";
        button.onclick = () => loadDetail(item.session_id);
        const row = document.createElement("div");
        row.className = "row";
        const title = document.createElement("div");
        title.className = "title";
        title.textContent = item.content || item.message || item.session_id;
        row.appendChild(title);
        row.appendChild(badge(item.status));
        const meta = document.createElement("div");
        meta.className = "meta";
        meta.textContent = [item.mode || "unknown", item.chat_type || "-", item.updated_at || item.started_at || ""].join(" · ");
        button.appendChild(row);
        button.appendChild(meta);
        sessionsEl.appendChild(button);
      }
    }

    function kv(label, value) {
      const card = document.createElement("div");
      card.className = "card";
      const name = document.createElement("div");
      name.className = "meta";
      name.textContent = label;
      const body = document.createElement("div");
      body.textContent = text(value);
      card.appendChild(name);
      card.appendChild(body);
      return card;
    }

    function renderDetail(session) {
      detailEl.textContent = "";
      const top = document.createElement("div");
      top.className = "row";
      const title = document.createElement("h2");
      title.textContent = session.mode || "会话详情";
      top.appendChild(title);
      top.appendChild(badge(session.status));
      detailEl.appendChild(top);

      const cards = document.createElement("div");
      cards.className = "cards";
      cards.appendChild(kv("session", session.session_id));
      cards.appendChild(kv("event", session.event_id));
      cards.appendChild(kv("chat", session.chat_id));
      cards.appendChild(kv("job", session.job_id));
      cards.appendChild(kv("updated", session.updated_at));
      detailEl.appendChild(cards);

      if (session.report_url) {
        const link = document.createElement("a");
        link.href = session.report_url;
        link.target = "_blank";
        link.rel = "noreferrer";
        link.textContent = "打开报告链接";
        detailEl.appendChild(link);
      }

      const requestTitle = document.createElement("h3");
      requestTitle.textContent = "用户请求";
      detailEl.appendChild(requestTitle);
      const request = document.createElement("pre");
      request.textContent = text(session.content);
      detailEl.appendChild(request);

      const resultTitle = document.createElement("h3");
      resultTitle.textContent = "回复结果";
      detailEl.appendChild(resultTitle);
      const result = document.createElement("pre");
      result.textContent = text(session.message);
      detailEl.appendChild(result);

      const progressTitle = document.createElement("h3");
      progressTitle.textContent = "后台 agent 过程";
      detailEl.appendChild(progressTitle);
      const timeline = document.createElement("div");
      timeline.className = "timeline";
      for (const step of session.progress || []) {
        const item = document.createElement("div");
        item.className = "step";
        const row = document.createElement("div");
        row.className = "row";
        const strong = document.createElement("strong");
        strong.textContent = step.stage || "progress";
        row.appendChild(strong);
        const ts = document.createElement("span");
        ts.className = "meta";
        ts.textContent = step.timestamp || "";
        row.appendChild(ts);
        const msg = document.createElement("div");
        msg.textContent = step.message || "";
        item.appendChild(row);
        item.appendChild(msg);
        if (step.details && Object.keys(step.details).length) {
          const pre = document.createElement("pre");
          pre.textContent = JSON.stringify(step.details, null, 2);
          item.appendChild(pre);
        }
        timeline.appendChild(item);
      }
      if (!(session.progress || []).length) {
        const empty = document.createElement("p");
        empty.className = "muted";
        empty.textContent = "暂无进度事件。";
        timeline.appendChild(empty);
      }
      detailEl.appendChild(timeline);
    }

    async function loadSessions() {
      const response = await fetch("/api/sessions", { cache: "no-store" });
      const data = await response.json();
      renderSessions(data.sessions || []);
      if (!selected && data.sessions && data.sessions.length) {
        await loadDetail(data.sessions[0].session_id);
      } else if (selected) {
        await loadDetail(selected);
      }
    }

    async function loadDetail(id) {
      selected = id;
      const response = await fetch("/api/sessions/" + encodeURIComponent(id), { cache: "no-store" });
      if (!response.ok) {
        detailEl.textContent = "会话不存在或已过期。";
        return;
      }
      const data = await response.json();
      renderDetail(data.session);
      for (const button of sessionsEl.querySelectorAll(".item")) {
        button.classList.toggle("active", button.dataset.sessionId === id);
      }
    }

    refreshButton.onclick = loadSessions;
    loadSessions();
    setInterval(loadSessions, 3000);
  </script>
</body>
</html>
"""


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
    if value in {"", "127.0.0.1", "localhost", "::1"}:
        return "0.0.0.0"
    return value


def resolve_public_base_url(public_base_url: str, *, port: int) -> str:
    raw = (public_base_url or "").strip()
    if not raw:
        raw = f"http://{_detect_lan_ip()}:{port}/reports"
    parts = urlsplit(raw)
    host = parts.hostname or ""
    if not _should_replace_public_host(host):
        return raw.rstrip("/")
    scheme = parts.scheme or "http"
    path = parts.path or "/reports"
    resolved_host = _detect_lan_ip()
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
