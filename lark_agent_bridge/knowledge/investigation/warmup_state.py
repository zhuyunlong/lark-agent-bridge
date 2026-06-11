"""codegraph 预热的文件状态与跨进程锁。"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
from typing import Any, TYPE_CHECKING
from ...models import BridgeConfig


_CODEGRAPH_WARMUP_COOLDOWN_SECONDS = 1800.0


_CODEGRAPH_WARMUP_RUNNING_STALE_SECONDS = 900.0


def _warmup_repo_key(repo: Path) -> Path:
    try:
        return repo.expanduser().resolve()
    except OSError:
        return repo.expanduser()


def _codegraph_warmup_state_dir(config: BridgeConfig) -> Path:
    return config.data_dir / "state" / "codegraph_warmup"


def _codegraph_repo_slug(repo: Path) -> str:
    return hashlib.sha1(str(_warmup_repo_key(repo)).encode("utf-8")).hexdigest()[:16]


def _codegraph_repo_state_path(config: BridgeConfig, repo: Path) -> Path:
    return _codegraph_warmup_state_dir(config) / f"{_codegraph_repo_slug(repo)}.json"


def _codegraph_repo_lock_path(config: BridgeConfig, repo: Path) -> Path:
    return _codegraph_warmup_state_dir(config) / f"{_codegraph_repo_slug(repo)}.lock"


def _read_codegraph_repo_state(config: BridgeConfig, repo: Path) -> dict[str, Any]:
    path = _codegraph_repo_state_path(config, repo)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_codegraph_repo_state(config: BridgeConfig, repo: Path, payload: dict[str, Any]) -> None:
    path = _codegraph_repo_state_path(config, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _as_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _should_skip_codegraph_warmup_state(state: dict[str, Any], *, now: float) -> bool:
    finished_at = _as_float(state.get("finished_at"))
    if bool(state.get("success")) and finished_at is not None and now - finished_at < _CODEGRAPH_WARMUP_COOLDOWN_SECONDS:
        return True
    started_at = _as_float(state.get("started_at"))
    if started_at is not None and finished_at is None and now - started_at < _CODEGRAPH_WARMUP_RUNNING_STALE_SECONDS:
        return True
    return False


@contextmanager
def _try_repo_warmup_lock(config: BridgeConfig, repo: Path):
    lock_path = _codegraph_repo_lock_path(config, repo)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield None
            return
        yield handle
