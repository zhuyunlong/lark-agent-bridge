"""Routing terms for bug and scene analysis."""

from __future__ import annotations

STARTUP_ROUTE_TERMS = (
    "启动",
    "时序",
    "首帧",
    "unityready",
    "readyprepare",
    "displaychanged",
    "startrender",
    "surfacecreated",
    "surfacechanged",
    "createunityplayeronmainthread",
    "onunityready",
    "unitymainfirstframereadyrendermsg",
)

STARTUP_BLOCK_ROUTE_TERMS = (
    "打不开",
    "无法打开",
    "进不去",
    "无法进入",
    "未拉起",
    "没拉起",
    "没起来",
    "起不来",
    "黑屏只有logo",
    "黑屏只有 logo",
    "只有logo",
    "只有 logo",
    "只显示logo",
    "只显示 logo",
)

STUCK_ROUTE_TERMS = (
    "卡顿",
    "卡住",
    "卡死",
    "掉帧",
    "黑屏",
    "不刷新",
    "无响应",
    "anr",
    "3d卡",
    "unity卡",
    "montecarlo卡",
)

SIGNAL_ROUTE_TERMS = (
    "信号",
    "没到unity",
    "没到 unity",
    "有没有到unity",
    "有没有到 unity",
    "数据链",
    "链路",
    "x3dcb",
    "signaldispatcher",
    "vhalhelper",
)

SCENE_SIGNAL_ROUTE_TERMS = (
    "3d场景信号",
    "3d 场景信号",
    "场景信号",
    "scenetype",
    "scene type",
    "unityscenetypeservice",
    "signal_sr_scene_type",
    "signal_custom_gear_st",
    "signal_custom_pk_hmi_mode",
    "signal_custom_special_scene_type",
    "上电p",
    "上电 p",
    "临停p",
    "临停 p",
    "特殊场景",
    "场景管理",
    "小憩",
    "露营",
    "洗车",
    "充电场景",
    "放电场景",
    "场景选择",
    "离车舒享",
    "行车场景",
    "泊车场景",
    "onhandlecustomspecialscenetype",
    "pkhmimode",
    "onhandlecustomgearst",
    "xsrscenestatebase",
    "xsrscenestatemachine",
    "xsrscenenmanager",
    "getpkhmimodemsg",
    "getmeterdatamsg",
    "set_ready",
    "onldstatechange",
)

SCENE_SIGNAL_CONTEXT_TERMS = (
    "3d场景",
    "3d 场景",
    "sr场景",
    "sr 场景",
    "大车模",
)

SCENE_SIGNAL_HINT_TERMS = (
    "信号",
    "链路",
    "源码",
    "日志",
    "scene",
    "scenetype",
    "unity",
    "sr",
)

PERCEPTION_ROUTE_TERMS = (
    "当前感知数据",
    "感知数据总结",
    "感知数据",
    "感知统计",
    "无感知",
    "sr无感知",
    "vhalhelper",
    "mapdatahandler",
    "x3dcb",
    "xdatanativeproxy",
    "unity收到的数据统计",
)

CRASH_ROUTE_TERMS = (
    "闪退",
    "crash",
    "tombstone",
    "fatal exception",
    "异常退出",
    "崩溃",
    "sigsegv",
    "abort",
    "native crash",
)

XTHEME_ROUTE_TERMS = (
    "xtheme",
    "signal_sr_xtheme",
    "105004",
    "105009",
    "时光主题",
    "时光变化",
    "晨曦",
    "傍晚",
    "黄昏",
    "日出日落",
    "主题切换",
    "黑白夜",
    "xuiconditionhelper",
)


CORE_SCENE_SIGNALS = {
    "100002",
    "100008",
    "100009",
    "100010",
    "signal_sr_scene_type",
    "signal_custom_gear_st",
    "signal_custom_pk_hmi_mode",
    "signal_custom_special_scene_type",
}

STRONG_SCENE_SIGNAL_INTENT_TERMS = (
    "3d场景信号",
    "3d 场景信号",
    "场景信号",
    "scenetype",
    "scene type",
    "unityscenetypeservice",
    "场景管理",
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
