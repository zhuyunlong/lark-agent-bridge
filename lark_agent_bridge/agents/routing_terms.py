"""Routing terms for bug and scene analysis.

Terms are loaded from the centralized ``config/routing_terms.toml`` file.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_BUILTIN_TERMS_PATH = Path(__file__).resolve().parents[2] / "config" / "routing_terms.toml"


def _load_terms(path: Path | None = None) -> dict[str, object]:
    """Load the TOML terms file, returning the parsed dict."""
    target = path or _BUILTIN_TERMS_PATH
    if not target.exists():
        return {}
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            return {}
    try:
        with open(target, "rb") as fh:
            return tomllib.load(fh)
    except Exception:
        logger.warning("failed to load routing terms from %s", target, exc_info=True)
        return {}


def _get_tuple(data: dict[str, object], section: str, key: str = "terms") -> tuple[str, ...]:
    sec = data.get(section)
    if isinstance(sec, dict):
        val = sec.get(key)
        if isinstance(val, list):
            return tuple(str(v) for v in val)
    return ()


def _get_set(data: dict[str, object], section: str, key: str = "values") -> set[str]:
    sec = data.get(section)
    if isinstance(sec, dict):
        val = sec.get(key)
        if isinstance(val, list):
            return {str(v) for v in val}
    return set()


# ---- load on import -------------------------------------------------------

_data = _load_terms()

STARTUP_ROUTE_TERMS = _get_tuple(_data, "startup")
STARTUP_BLOCK_ROUTE_TERMS = _get_tuple(_data, "startup_block")
STUCK_ROUTE_TERMS = _get_tuple(_data, "stuck")
SIGNAL_ROUTE_TERMS = _get_tuple(_data, "signal")
SCENE_SIGNAL_ROUTE_TERMS = _get_tuple(_data, "scene_signal")
SCENE_SIGNAL_CONTEXT_TERMS = _get_tuple(_data, "scene_signal_context")
SCENE_SIGNAL_HINT_TERMS = _get_tuple(_data, "scene_signal_hint")
PERCEPTION_ROUTE_TERMS = _get_tuple(_data, "perception")
CRASH_ROUTE_TERMS = _get_tuple(_data, "crash")
XTHEME_ROUTE_TERMS = _get_tuple(_data, "xtheme")
CORE_SCENE_SIGNALS = _get_set(_data, "core_scene_signals")
STRONG_SCENE_SIGNAL_INTENT_TERMS = _get_tuple(_data, "strong_scene_signal_intent")


def _is_core_scene_signal(signal: str) -> bool:
    return signal.strip().casefold() in CORE_SCENE_SIGNALS


def _has_strong_scene_signal_intent(lowered_text: str) -> bool:
    return any(term in lowered_text for term in STRONG_SCENE_SIGNAL_INTENT_TERMS)


def looks_like_scene_signal_request(text: str) -> bool:
    lowered = (text or "").casefold()
    if any(term in lowered for term in SCENE_SIGNAL_ROUTE_TERMS):
        return True
    return any(term in lowered for term in SCENE_SIGNAL_CONTEXT_TERMS) and any(
        hint in lowered for hint in SCENE_SIGNAL_HINT_TERMS
    )
