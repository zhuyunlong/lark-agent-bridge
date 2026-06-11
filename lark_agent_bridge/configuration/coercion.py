"""配置值类型转换与校验辅助。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _resolve_path(value: str | Path, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _bool_value(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean environment value: {value}")


def _bool_like(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _bool_value(value, default)
    return bool(value)


def _string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return list(value)


def _env_string_list(env_name: str, default: Any, field_name: str) -> list[str]:
    env_value = os.environ.get(env_name)
    if env_value is None:
        return _string_list(default, field_name)
    return [item.strip() for item in env_value.split(",") if item.strip()]


def _string_dict(value: Any, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError(f"{field_name} must be a string-to-string table")
    return dict(value)


def _optional_str(value: Any, field_name: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _optional_path(value: Any, base_dir: Path, field_name: str = "path") -> Path | None:
    if value is None or value == "":
        return None
    if not isinstance(value, (str, Path)):
        raise ValueError(f"{field_name} must be a path string")
    return _resolve_path(value, base_dir)


def _path_list(value: Any, base_dir: Path, field_name: str) -> list[Path]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of path strings")
    return [_resolve_path(item, base_dir) for item in value]
