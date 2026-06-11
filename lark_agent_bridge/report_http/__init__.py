"""Report HTTP internals split out of report_server.py.

Submodules:
- ``paths``: slug/mtime filesystem helpers shared by publisher and history.
- ``query``: query-string parsing helpers for API endpoints.
- ``knowledge_export``: knowledge export report location/status.
- ``history``: analysis-history search text and deletion path resolution.
- ``index_page``: published report index page rendering.
- ``http_io``: handler mixin for HTTP responses and admin auth endpoints.
- ``admin_api``: handler mixin for sessions/cases/history/daemon/health APIs.
- ``skills_knowledge_api``: handler mixin for skills and knowledge APIs.
- ``request_handler``: assembled request handler class and factory.

The original module path ``lark_agent_bridge.report_server`` remains the
public facade; the URL/bind-host resolution chain (``resolve_public_base_url``
-> ``_detect_lan_ip``) intentionally stays there so existing
``mock.patch("lark_agent_bridge.report_server._detect_lan_ip")`` keeps working.
"""

from .request_handler import ReportRequestHandler, _build_handler

__all__ = [
    "ReportRequestHandler",
    "_build_handler",
]
