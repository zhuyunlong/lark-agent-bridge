"""Routing terms for bug and scene analysis.

Terms are loaded from ``routing_terms.toml`` (same directory).  The TOML file
is the authoritative source — the inline defaults here are kept only as a
fallback so the module never fails to import.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_BUILTIN_TERMS_PATH = Path(__file__).with_name("routing_terms.toml")


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

# Inline fallbacks mirror the TOML file so the module works without it.
_FALLBACK_STARTUP = (
    "启动", "时序", "首帧", "unityready", "readyprepare", "displaychanged",
    "startrender", "surfacecreated", "surfacechanged",
    "createunityplayeronmainthread", "onunityready",
    "unitymainfirstframereadyrendermsg",
)
_FALLBACK_STARTUP_BLOCK = (
    "打不开", "无法打开", "进不去", "无法进入", "未拉起", "没拉起",
    "没起来", "起不来", "黑屏只有logo", "黑屏只有 logo",
    "只有logo", "只有 logo", "只显示logo", "只显示 logo",
)
_FALLBACK_STUCK = (
    "卡顿", "卡住", "卡死", "掉帧", "黑屏", "不刷新", "无响应", "anr",
    "3d卡", "unity卡", "montecarlo卡",
)
_FALLBACK_SIGNAL = (
    "信号", "没到unity", "没到 unity", "有没有到unity", "有没有到 unity",
    "数据链", "链路", "x3dcb", "signaldispatcher", "vhalhelper",
)
_FALLBACK_SCENE_SIGNAL = (
    "3d场景信号", "3d 场景信号", "场景信号", "scenetype", "scene type",
    "unityscenetypeservice", "signal_sr_scene_type", "signal_custom_gear_st",
    "signal_custom_pk_hmi_mode", "signal_custom_special_scene_type",
    "上电p", "上电 p", "临停p", "临停 p", "特殊场景", "场景管理",
    "小憩", "露营", "洗车", "充电场景", "放电场景", "场景选择", "离车舒享",
    "行车场景", "泊车场景", "onhandlecustomspecialscenetype", "pkhmimode",
    "onhandlecustomgearst", "xsrscenestatebase", "xsrscenestatemachine",
    "xsrscenenmanager", "getpkhmimodemsg", "getmeterdatamsg",
    "set_ready", "onldstatechange",
)
_FALLBACK_SCENE_SIGNAL_CONTEXT = ("3d场景", "3d 场景", "sr场景", "sr 场景", "大车模")
_FALLBACK_SCENE_SIGNAL_HINT = ("信号", "链路", "源码", "日志", "scene", "scenetype", "unity", "sr")
_FALLBACK_PERCEPTION = (
    "当前感知数据", "感知数据总结", "感知数据", "感知统计", "无感知", "sr无感知",
    "vhalhelper", "mapdatahandler", "x3dcb", "xdatanativeproxy", "unity收到的数据统计",
)
_FALLBACK_CRASH = (
    "闪退", "crash", "tombstone", "fatal exception", "异常退出", "崩溃",
    "sigsegv", "abort", "native crash",
)
_FALLBACK_XTHEME = (
    "xtheme", "signal_sr_xtheme", "105004", "105009", "时光主题", "时光变化",
    "晨曦", "傍晚", "黄昏", "日出日落", "主题切换", "黑白夜", "xuiconditionhelper",
)
_FALLBACK_CORE_SCENE_SIGNALS = {
    "100002", "100008", "100009", "100010",
    "signal_sr_scene_type", "signal_custom_gear_st",
    "signal_custom_pk_hmi_mode", "signal_custom_special_scene_type",
}
_FALLBACK_STRONG_SCENE_SIGNAL_INTENT = (
    "3d场景信号", "3d 场景信号", "场景信号", "scenetype", "scene type",
    "unityscenetypeservice", "场景管理",
)

STARTUP_ROUTE_TERMS = _get_tuple(_data, "startup") or _FALLBACK_STARTUP
STARTUP_BLOCK_ROUTE_TERMS = _get_tuple(_data, "startup_block") or _FALLBACK_STARTUP_BLOCK
STUCK_ROUTE_TERMS = _get_tuple(_data, "stuck") or _FALLBACK_STUCK
SIGNAL_ROUTE_TERMS = _get_tuple(_data, "signal") or _FALLBACK_SIGNAL
SCENE_SIGNAL_ROUTE_TERMS = _get_tuple(_data, "scene_signal") or _FALLBACK_SCENE_SIGNAL
SCENE_SIGNAL_CONTEXT_TERMS = _get_tuple(_data, "scene_signal_context") or _FALLBACK_SCENE_SIGNAL_CONTEXT
SCENE_SIGNAL_HINT_TERMS = _get_tuple(_data, "scene_signal_hint") or _FALLBACK_SCENE_SIGNAL_HINT
PERCEPTION_ROUTE_TERMS = _get_tuple(_data, "perception") or _FALLBACK_PERCEPTION
CRASH_ROUTE_TERMS = _get_tuple(_data, "crash") or _FALLBACK_CRASH
XTHEME_ROUTE_TERMS = _get_tuple(_data, "xtheme") or _FALLBACK_XTHEME
CORE_SCENE_SIGNALS = _get_set(_data, "core_scene_signals") or _FALLBACK_CORE_SCENE_SIGNALS
STRONG_SCENE_SIGNAL_INTENT_TERMS = (
    _get_tuple(_data, "strong_scene_signal_intent") or _FALLBACK_STRONG_SCENE_SIGNAL_INTENT
)


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
