"""配置加载期的可降级 logger（日志模块异常时不阻塞配置加载）。"""

from __future__ import annotations

from typing import Any

try:
    from ..log import get_logger
except Exception:  # pragma: no cover - logging is optional at config load time
    class _FallbackLogger:
        def warning(self, *_args: Any, **_kwargs: Any) -> None: ...
        def info(self, *_args: Any, **_kwargs: Any) -> None: ...
        def debug(self, *_args: Any, **_kwargs: Any) -> None: ...

    def get_logger(_name: str):  # type: ignore[override]
        return _FallbackLogger()

__all__ = ["get_logger"]
