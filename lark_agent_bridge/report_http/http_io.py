"""Handler mixin: HTTP response writing and admin auth endpoints."""

from __future__ import annotations

import json

from .knowledge_export import _knowledge_export_report_path, _knowledge_export_report_status


class HttpIoAuthMixin:
    """Response/body helpers and /api/auth/* handling for the report handler.

    Expects the composed class to provide ``BaseHTTPRequestHandler`` plumbing
    (``headers``, ``rfile``, ``wfile``, ``send_response`` ...) plus the
    ``bridge_config`` and ``admin_auth`` class attributes.
    """

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

    def _send_knowledge_export_report(self) -> None:
        report_path = _knowledge_export_report_path(self.bridge_config)
        if not report_path.is_file():
            self._send_json(
                {
                    "error": "knowledge export report not found",
                    "status": _knowledge_export_report_status(self.bridge_config),
                },
                status=404,
            )
            return
        body = report_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
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
        if self.admin_auth is None or not self.admin_auth.auth_required:
            return True
        token = self._get_bearer_token()
        user = self.admin_auth.check_token(token)
        if user is None:
            self._send_json({"error": "未认证，请先登录"}, status=401)
            return False
        if user.get("role") == "viewer":
            self._send_json({"error": "权限不足，viewer 角色不能执行写操作"}, status=403)
            return False
        return True

    def _auth_status(self) -> dict[str, object]:
        if self.admin_auth is None:
            return {"required": False, "has_users": False, "has_token": False}
        return {
            "required": self.admin_auth.auth_required,
            "has_users": self.admin_auth.has_users,
            "has_token": bool(self.admin_auth._admin_token),
        }

    def _handle_login(self) -> None:
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        # Static token login
        token = str(payload.get("token") or "").strip()
        if token and self.admin_auth is not None:
            user = self.admin_auth.check_token(token)
            if user is not None:
                self._send_json({"ok": True, "token": token, **user})
                return
        # Username/password login
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        if username and self.admin_auth is not None:
            session = self.admin_auth.login(username, password)
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
        if token and self.admin_auth is not None:
            self.admin_auth.logout(token)
        self._send_json({"ok": True})

    def _handle_create_user(self) -> None:
        if self.admin_auth is None:
            self._send_json({"error": "auth not configured"}, status=503)
            return
        try:
            payload = self._read_json_body()
            username = str(payload.get("username") or "").strip()
            password = str(payload.get("password") or "")
            role = str(payload.get("role") or "admin")
            user = self.admin_auth.create_user(username, password, role)
            self._send_json({"ok": True, "user": {"username": user.username, "role": user.role}}, status=201)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)

    def _handle_change_password(self) -> None:
        if self.admin_auth is None:
            self._send_json({"error": "auth not configured"}, status=503)
            return
        try:
            payload = self._read_json_body()
            username = str(payload.get("username") or "").strip()
            old_password = str(payload.get("old_password") or "")
            new_password = str(payload.get("new_password") or "")
            if self.admin_auth.change_password(username, old_password, new_password):
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "原密码错误或用户不存在"}, status=401)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
