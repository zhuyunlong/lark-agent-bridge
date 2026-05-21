"""Simple admin authentication for the report server.

Supports two modes that can be combined:
- **Static token**: set ``admin_token`` in config; all write requests must
  include ``Authorization: Bearer <token>``.
- **User accounts**: managed via ``admin_users.json`` in the data directory;
  users log in with username/password and receive a session token.

Passwords are hashed with PBKDF2-HMAC-SHA256 (100 000 iterations + random
salt).  Session tokens expire after 24 hours.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from .log import get_logger

logger = get_logger("auth")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class AdminUser:
    username: str
    password_hash: str  # hex(salt):hex(hash)
    role: str  # "admin" | "viewer"


@dataclass(slots=True)
class AdminSession:
    token: str
    username: str
    role: str
    created_at: float
    expires_at: float


# ---------------------------------------------------------------------------
# Auth manager
# ---------------------------------------------------------------------------

_PBKDF2_ITERATIONS = 100_000
_SESSION_TTL_SECONDS = 86400  # 24 h
_MAX_SESSIONS = 256


class AdminAuth:
    """Manages admin authentication state (tokens + users + sessions)."""

    def __init__(self, data_dir: Path, *, admin_token: str = "") -> None:
        self._data_dir = data_dir
        self._admin_token: str = admin_token.strip()
        self._users_file: Path = data_dir / "admin_users.json"
        self._sessions: dict[str, AdminSession] = {}
        self._users: dict[str, AdminUser] = {}
        self._load_users()

    # -- public properties ---------------------------------------------------

    @property
    def auth_required(self) -> bool:
        return bool(self._admin_token) or bool(self._users)

    @property
    def has_users(self) -> bool:
        return bool(self._users)

    # -- token validation ----------------------------------------------------

    def check_token(self, token: str) -> dict[str, str] | None:
        """Return ``{"username": …, "role": …}`` if *token* is valid."""
        token = token.strip()
        if not token:
            return None
        if self._admin_token and hmac.compare_digest(token, self._admin_token):
            return {"username": "_token", "role": "admin"}
        session = self._sessions.get(token)
        if session is None:
            return None
        if session.expires_at <= time.time():
            self._sessions.pop(token, None)
            return None
        return {"username": session.username, "role": session.role}

    # -- login / logout ------------------------------------------------------

    def login(self, username: str, password: str) -> AdminSession | None:
        user = self._users.get(username)
        if user is None or not _verify_password(password, user.password_hash):
            logger.warning("login failed for user %r", username)
            return None
        token = secrets.token_urlsafe(32)
        now = time.time()
        session = AdminSession(
            token=token,
            username=username,
            role=user.role,
            created_at=now,
            expires_at=now + _SESSION_TTL_SECONDS,
        )
        self._sessions[token] = session
        self._prune_sessions()
        logger.info("user %r logged in", username)
        return session

    def logout(self, token: str) -> bool:
        return self._sessions.pop(token.strip(), None) is not None

    # -- user management -----------------------------------------------------

    def create_user(self, username: str, password: str, role: str = "admin") -> AdminUser:
        if not username or not password:
            raise ValueError("用户名和密码不能为空")
        if username.startswith("_"):
            raise ValueError("用户名不能以 _ 开头")
        if len(password) < 6:
            raise ValueError("密码长度不能少于 6 位")
        user = AdminUser(
            username=username,
            password_hash=_hash_password(password),
            role=role if role in {"admin", "viewer"} else "admin",
        )
        self._users[username] = user
        self._save_users()
        logger.info("created user %r (role=%s)", username, user.role)
        return user

    def delete_user(self, username: str) -> bool:
        if username not in self._users:
            return False
        del self._users[username]
        self._save_users()
        stale = [t for t, s in self._sessions.items() if s.username == username]
        for t in stale:
            del self._sessions[t]
        logger.info("deleted user %r", username)
        return True

    def list_users(self) -> list[dict[str, str]]:
        return [{"username": u.username, "role": u.role} for u in self._users.values()]

    def change_password(self, username: str, old_password: str, new_password: str) -> bool:
        user = self._users.get(username)
        if user is None or not _verify_password(old_password, user.password_hash):
            return False
        if len(new_password) < 6:
            raise ValueError("密码长度不能少于 6 位")
        self._users[username] = AdminUser(
            username=user.username,
            password_hash=_hash_password(new_password),
            role=user.role,
        )
        self._save_users()
        stale = [t for t, s in self._sessions.items() if s.username == username]
        for t in stale:
            del self._sessions[t]
        logger.info("password changed for user %r", username)
        return True

    # -- internals -----------------------------------------------------------

    def _load_users(self) -> None:
        if not self._users_file.exists():
            return
        try:
            raw = self._users_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            for entry in data.get("users", []):
                user = AdminUser(
                    username=entry["username"],
                    password_hash=entry["password_hash"],
                    role=entry.get("role", "admin"),
                )
                self._users[user.username] = user
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            logger.warning("failed to load admin users: %s", exc)

    def _save_users(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "users": [
                {"username": u.username, "password_hash": u.password_hash, "role": u.role}
                for u in self._users.values()
            ]
        }
        self._users_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _prune_sessions(self) -> None:
        now = time.time()
        expired = [t for t, s in self._sessions.items() if s.expires_at <= now]
        for t in expired:
            del self._sessions[t]
        if len(self._sessions) > _MAX_SESSIONS:
            by_age = sorted(self._sessions.items(), key=lambda kv: kv[1].created_at)
            for token, _ in by_age[: len(self._sessions) - _MAX_SESSIONS]:
                del self._sessions[token]


# ---------------------------------------------------------------------------
# Password helpers (module-level for testability)
# ---------------------------------------------------------------------------

def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}:{h.hex()}"


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_hex, hash_hex = stored_hash.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)
