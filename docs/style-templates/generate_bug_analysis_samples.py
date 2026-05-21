#!/usr/bin/env python3
"""Generate sample HTML reports for repo-local bug analysis skills."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "bug-analysis"
REPORT_HTML = Path(
    "/Users/zhuyl/Documents/workspace/.ai/skills/3d-stuck-investigate/scripts/report_html.py"
)


def load_report_html() -> Any:
    spec = importlib.util.spec_from_file_location("skill_report_html", REPORT_HTML)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {REPORT_HTML}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tag(text: str, sev: str) -> str:
    return text


REPORTS: list[dict[str, Any]] = [
    {
        "slug": "3d-stuck-investigate",
        "title": "3D 卡顿与黑屏分析报告",
        "skill": "3d-stuck-investigate",
        "status": "red",
        "verdict": "结论：目标时间窗内 UnityRequest 连续低帧，Surface 与 UnityReady 已到达，主卡点更接近 Unity 渲染线程阻塞而非 Android 启动链路缺失。",
        "cards": [
            ("故障类型", "渲染卡顿", "red", "连续 18 秒低帧"),
            ("主进程 PID", "3250", "green", "com.xiaopeng.montecarlo"),
            ("问题时间", "2026-05-18 07:51", "green", "T±120s"),
            ("最早异常", "UnityRequest count<20", "red", "07:50:43 起"),
            ("系统负载", "CPU 74% / iow 3%", "yellow", "放大因素"),
            ("报告可信度", "高", "green", "同 PID 证据闭环"),
        ],
        "issues": [
            {"sev": "red", "title": "关键问题：渲染请求不足", "detail": "目标窗口内 UnityRequest 帧请求持续低于阈值，用户可感知为 3D 卡顿或画面不刷新。"},
            {"sev": "yellow", "title": "放大因素：系统负载偏高", "detail": "CPU 总占用偏高，但未出现足以单独解释卡死的 iow 峰值。"},
            {"sev": "green", "title": "排除项：启动链路完整", "detail": "Activity、Surface、UnityReady 和首帧节点均已命中。"},
        ],
        "chain": [
            {"sev": "green", "title": "锁定同一 PID", "evidence": "07:49-07:53 均为 PID 3250", "downstream": "避免跨进程拼接旧证据。"},
            {"sev": "green", "title": "启动与 Surface 正常", "evidence": "surfaceChanged、startRender、UnityReady 已出现", "downstream": "问题不属于启动黑屏首帧缺失。"},
            {"sev": "red", "title": "渲染请求低帧", "evidence": "UnityRequest count<20 连续出现", "downstream": "最终表现为 3D 画面卡顿或刷新不足。"},
            {"sev": "yellow", "title": "系统负载放大", "evidence": "CPU 74%，montecarlo 42%", "downstream": "可能放大耗时，但不是唯一主因。"},
        ],
        "timeline": [
            ("07:50:30", "会话确认", "PID 3250 活跃", tag("正常", "green")),
            ("07:50:43", "渲染请求", "UnityRequest count=12", tag("异常", "red")),
            ("07:51:06", "Surface", "surfaceChanged 已命中", tag("正常", "green")),
            ("07:51:22", "系统负载", "CPU total=74%", tag("风险", "yellow")),
        ],
        "matrix": [
            ("Activity/Surface", "正常", "surfaceChanged、startRender 均命中", "不作为主因"),
            ("Unity 渲染", "异常", "UnityRequest 连续低帧", "主卡点"),
            ("系统资源", "风险", "CPU 偏高", "放大因素"),
        ],
        "evidence": "07:50:43.128 I UnityRequest count=12 cost=86ms\n07:50:44.130 I UnityRequest count=10 cost=92ms\n07:51:06.044 I SurfaceManager surfaceChanged w=1920 h=720\n07:51:22.500 I cpu total=74 iow=3 montecarlo=42",
    },
    {
        "slug": "unity-startup-lifecycle-check",
        "title": "Unity 启动生命周期分析报告",
        "skill": "unity-startup-lifecycle-check",
        "status": "yellow",
        "verdict": "结论：Android Activity 与 Surface 创建完成，但 UnityReady 到首帧耗时超阈值，启动慢主要卡在 Unity 内部初始化到首帧阶段。",
        "cards": [
            ("故障类型", "首帧慢", "yellow", "Ready 到首帧 12.4s"),
            ("启动会话", "Session 2", "green", "PID 3250"),
            ("Surface", "已绑定", "green", "1920x720"),
            ("UnityReady", "已到达", "green", "07:51:09"),
            ("首帧", "超时", "yellow", "07:51:21"),
            ("报告可信度", "中高", "green", "关键节点齐全"),
        ],
        "issues": [
            {"sev": "yellow", "title": "关键问题：Ready 到首帧耗时偏长", "detail": "Surface 已可用后，UnityReady 到 FirstFrame 仍耗时 12.4 秒。"},
            {"sev": "green", "title": "排除项：Activity 生命周期没有缺口", "detail": "onCreate、onResume、surfaceCreated、surfaceChanged 均命中。"},
            {"sev": "yellow", "title": "待确认：Napa 资源加载耗时", "detail": "需要结合 Unity 内部资源加载日志确认是否 shader/cache/地图资源导致。"},
        ],
        "chain": [
            {"sev": "green", "title": "Activity 启动", "evidence": "MainActivity onCreate/onResume", "downstream": "Android 容器已进入前台。"},
            {"sev": "green", "title": "Surface 创建", "evidence": "surfaceCreated -> surfaceChanged", "downstream": "渲染承载面已准备。"},
            {"sev": "yellow", "title": "UnityReady 到首帧慢", "evidence": "Ready 07:51:09，FirstFrame 07:51:21", "downstream": "用户看到启动慢或短时黑屏。"},
        ],
        "timeline": [
            ("07:50:57.102", "onCreate", "MainActivity 创建", tag("正常", "green")),
            ("07:51:01.443", "Surface", "surfaceChanged 1920x720", tag("正常", "green")),
            ("07:51:09.006", "UnityReady", "onUnityReady", tag("正常", "green")),
            ("07:51:21.432", "FirstFrame", "UnityMainFirstFrameReadyRenderMsg", tag("慢", "yellow")),
        ],
        "matrix": [
            ("Android Activity", "正常", "生命周期完整", "非卡点"),
            ("Surface/Render", "正常", "Surface 已绑定", "非卡点"),
            ("Unity 内部", "偏慢", "Ready 到首帧超阈值", "主卡点"),
        ],
        "evidence": "07:50:57.102 I MainActivity onCreate\n07:51:01.443 I XPEDriveSurfaceView surfaceChanged 1920x720\n07:51:09.006 I UnityPlayer onUnityReady\n07:51:21.432 I UnityMainFirstFrameReadyRenderMsg",
    },
    {
        "slug": "scene-signal-diagnosis",
        "title": "3D 场景信号诊断报告",
        "skill": "scene-signal-diagnosis",
        "status": "red",
        "verdict": "结论：Android 侧已生产并发送 SR 场景信号，Unity 已收到但状态机没有退出 IMMERSIVE_P，卡点在 Unity 场景消费后的状态切换。",
        "cards": [
            ("主信号", "SIGNAL_SR_SCENE_TYPE", "green", "100002"),
            ("Android 生产", "已命中", "green", "UnitySceneTypeService"),
            ("Unity 接收", "已命中", "green", "OnHandleSceneType"),
            ("最终状态", "未退出 P 场景", "red", "IMMERSIVE_P 残留"),
            ("相关信号", "4 个", "green", "Gear / PK / Special / SR"),
            ("报告可信度", "高", "green", "生产与消费均有证据"),
        ],
        "issues": [
            {"sev": "red", "title": "关键问题：Unity 状态机未完成退出", "detail": "SR 场景信号已到 Unity，但 XSRSceneStateMachine 仍保持 IMMERSIVE_P。"},
            {"sev": "green", "title": "排除项：Android 生产和发送链路正常", "detail": "UnitySceneTypeRepository 与 UnityTransport 均有发送证据。"},
            {"sev": "yellow", "title": "待确认：特殊场景互斥优先级", "detail": "需要看 SpecialScene 与 Gear 信号是否在同一窗口反复覆盖。"},
        ],
        "chain": [
            {"sev": "green", "title": "业务输入变化", "evidence": "Gear=D，PKHMIMode=NORMAL", "downstream": "满足退出 P 场景的上游条件。"},
            {"sev": "green", "title": "Android 发送 SR 场景", "evidence": "SIGNAL_SR_SCENE_TYPE=DRIVING", "downstream": "Android 到 Unity 通道成立。"},
            {"sev": "red", "title": "Unity 状态未切换", "evidence": "OnHandleSceneType 已收到，StateMachine 仍 IMMERSIVE_P", "downstream": "用户看到大车模/上电 P 未退出。"},
        ],
        "timeline": [
            ("11:27:02", "Gear", "SIGNAL_CUSTOM_GEAR_ST=D", tag("正常", "green")),
            ("11:27:03", "Android", "send SIGNAL_SR_SCENE_TYPE=DRIVING", tag("正常", "green")),
            ("11:27:03", "Unity", "OnHandleSceneType DRIVING", tag("正常", "green")),
            ("11:27:05", "State", "current=IMMERSIVE_P", tag("异常", "red")),
        ],
        "matrix": [
            ("车辆/业务输入", "正常", "Gear/PK/Special 均有快照", "非卡点"),
            ("Android 发送", "正常", "SIGNAL_SR_SCENE_TYPE 已发", "非卡点"),
            ("Unity 状态机", "异常", "状态未退出", "主卡点"),
        ],
        "evidence": "11:27:02.101 I UnitySceneTypeRepository gear=D pk=NORMAL\n11:27:03.018 I UnitySceneTypeService send SIGNAL_SR_SCENE_TYPE=DRIVING\n11:27:03.042 I XSRScenenManager OnHandleSceneType DRIVING\n11:27:05.550 I XSRSceneStateMachine current=IMMERSIVE_P",
    },
    {
        "slug": "signal-chain-analyzer",
        "title": "DataCenter 信号链路分析报告",
        "skill": "signal-chain-analyzer",
        "status": "yellow",
        "verdict": "结论：信号在 signal.proto 中定义，上游 SDK 支持并已 dispatch 到 DataCenter；目标业务未命中 registerObserver 证据，当前卡点倾向业务消费侧。",
        "cards": [
            ("SignalCode", "100010", "green", "SIGNAL_CUSTOM_SPECIAL_SCENE_TYPE"),
            ("proto 定义", "存在", "green", "signal.proto"),
            ("上游注册", "成功", "green", "SDK support=true"),
            ("DataCenter", "已分发", "green", "dispatchSignal"),
            ("业务注册", "未命中", "yellow", "目标窗口缺观察者日志"),
            ("报告可信度", "中", "yellow", "消费侧日志不足"),
        ],
        "issues": [
            {"sev": "yellow", "title": "关键问题：业务消费侧证据缺口", "detail": "DataCenter 已 dispatch，但目标业务 registerObserver / callback 日志未命中。"},
            {"sev": "green", "title": "健康项：上游 SDK 支持该信号", "detail": "module_datacenter 初始化、上游连接和信号注册均有成功日志。"},
            {"sev": "yellow", "title": "待确认：业务是否按当前 signal.proto 枚举名注册", "detail": "需结合源码确认业务使用方是否仍引用旧枚举或派生信号。"},
        ],
        "chain": [
            {"sev": "green", "title": "信号定义", "evidence": "signal.proto 定义 SIGNAL_CUSTOM_SPECIAL_SCENE_TYPE=100010", "downstream": "可以据此枚举源码使用方。"},
            {"sev": "green", "title": "DataCenter 初始化", "evidence": "module_datacenter connect upstream success", "downstream": "上游 SDK 链接可用。"},
            {"sev": "green", "title": "DataCenter 分发", "evidence": "dispatchSignal code=100010", "downstream": "数据已进入中心分发层。"},
            {"sev": "yellow", "title": "业务消费缺证据", "evidence": "目标业务未命中 callback", "downstream": "现象可能表现为业务状态未更新。"},
        ],
        "timeline": [
            ("09:12:01", "Init", "module_datacenter connect success", tag("正常", "green")),
            ("09:12:02", "Register", "register signal 100010 support=true", tag("正常", "green")),
            ("09:15:14", "Dispatch", "DataCenter.dispatchSignal 100010", tag("正常", "green")),
            ("09:15:14-09:16:14", "Business", "未命中目标 callback", tag("缺口", "yellow")),
        ],
        "matrix": [
            ("module_datacenter 初始化", "正常", "connect/register success", "非卡点"),
            ("上游到 DataCenter", "正常", "dispatchSignal 命中", "非卡点"),
            ("业务注册/接收", "证据不足", "registerObserver 未命中", "疑似卡点"),
        ],
        "evidence": "09:12:01.002 I ModuleDataCenter connectUpstreamSdk success\n09:12:02.341 I ModuleDataCenter registerSignal code=100010 support=true\n09:15:14.208 I DataCenter dispatchSignal code=100010 size=24\n09:15:14-09:16:14 W Analyzer business callback not found in target window",
    },
    {
        "slug": "xtheme-analyzer",
        "title": "XTheme 时光主题分析报告",
        "skill": "xtheme-analyzer",
        "status": "red",
        "verdict": "结论：问题前系统 UI mode 已切到 NIGHT，但 XThemeStrategy 最后一次发送仍为 DAY，卡点在 ThemeHelper 输入刷新或 XTheme 重新计算调度。",
        "cards": [
            ("系统主题", "NIGHT", "green", "问题前最后状态"),
            ("UI mode", "carNight=true", "green", "系统输入已变化"),
            ("XTheme 输出", "DAY", "red", "与输入不一致"),
            ("日出日落", "非早晚专项", "green", "描述未涉及晨昏"),
            ("发送信号", "SIGNAL_SR_XTHEME", "red", "发送值未更新"),
            ("报告可信度", "高", "green", "输入输出均有日志"),
        ],
        "issues": [
            {"sev": "red", "title": "关键问题：XTheme 输出未跟随系统输入", "detail": "系统夜间输入已更新，但 XThemeStrategy 仍发送 DAY。"},
            {"sev": "green", "title": "排除项：非晨曦/黄昏专项", "detail": "用户描述没有晨昏关键词，默认不展开早晚时段专项。"},
            {"sev": "yellow", "title": "待确认：ThemeHelper 监听是否漏触发", "detail": "需要源码和日志共同确认 UI mode 变化后是否调用 computeXTheme。"},
        ],
        "chain": [
            {"sev": "green", "title": "系统输入变化", "evidence": "UiModeManager night=true", "downstream": "XTheme 应重新计算为夜间相关主题。"},
            {"sev": "yellow", "title": "ThemeHelper 快照滞后", "evidence": "lastTheme=DAY", "downstream": "策略层拿到旧输入。"},
            {"sev": "red", "title": "发送值不一致", "evidence": "SIGNAL_SR_XTHEME payload=DAY", "downstream": "Unity 继续显示白天主题。"},
        ],
        "timeline": [
            ("18:42:10", "System", "UiMode night=true", tag("正常", "green")),
            ("18:42:11", "ThemeHelper", "lastSystemTheme=DAY", tag("滞后", "yellow")),
            ("18:42:12", "XThemeStrategy", "send SrThemeSkin=DAY", tag("异常", "red")),
            ("18:42:13", "Unity", "OnHandleXTheme DAY", tag("结果异常", "red")),
        ],
        "matrix": [
            ("系统输入", "正常", "night=true", "非卡点"),
            ("ThemeHelper", "异常", "快照未刷新", "疑似卡点"),
            ("XTheme 发送", "异常", "仍发送 DAY", "结果"),
        ],
        "evidence": "18:42:10.120 I NAV_XuiConditionHelper uiMode night=true\n18:42:11.006 I NAV_ThemeHelper lastSystemTheme=DAY\n18:42:12.488 I NAV_XThemeStrategy send SIGNAL_SR_XTHEME SrThemeSkin=DAY\n18:42:13.002 I Unity OnHandleXTheme DAY",
    },
    {
        "slug": "perception-data-summary",
        "title": "感知数据链路统计报告",
        "skill": "perception-data-summary",
        "status": "green",
        "verdict": "结论：目标窗口内上游、Native、Unity 发送与 Unity 接收计数递进合理，未发现感知数据链路断点；建议转向消费侧渲染或业务阈值判断。",
        "cards": [
            ("统计窗口", "10 分钟", "green", "T±300s"),
            ("上游输入", "12000 条", "green", "频率稳定"),
            ("Native 处理", "11996 条", "green", "丢失率 0.03%"),
            ("Unity 发送", "11992 条", "green", "递进合理"),
            ("Unity 接收", "11990 条", "green", "未见突降"),
            ("报告可信度", "高", "green", "四层计数完整"),
        ],
        "issues": [
            {"sev": "green", "title": "健康项：链路频率稳定", "detail": "上游到 Unity 接收各层频率一致，没有持续缺口。"},
            {"sev": "green", "title": "健康项：无异常突降", "detail": "T±300s 内未出现单层计数断崖。"},
            {"sev": "yellow", "title": "后续方向：消费侧判断", "detail": "若用户仍感知异常，应转向 Unity 消费、阈值过滤或渲染侧。"},
        ],
        "chain": [
            {"sev": "green", "title": "上游输入稳定", "evidence": "20Hz 平均稳定", "downstream": "数据源不是主因。"},
            {"sev": "green", "title": "Native 处理正常", "evidence": "处理计数与输入基本一致", "downstream": "Native 过滤无异常。"},
            {"sev": "green", "title": "Unity 发送/接收正常", "evidence": "发送 11992，接收 11990", "downstream": "传输链路没有断点。"},
            {"sev": "yellow", "title": "转向消费侧", "evidence": "链路统计正常但现象仍存在", "downstream": "需要看业务阈值和渲染呈现。"},
        ],
        "timeline": [
            ("07:45-07:55", "Upstream", "12000 events", tag("正常", "green")),
            ("07:45-07:55", "Native", "11996 events", tag("正常", "green")),
            ("07:45-07:55", "UnityTransport", "11992 events", tag("正常", "green")),
            ("07:45-07:55", "UnityConsumer", "11990 events", tag("正常", "green")),
        ],
        "matrix": [
            ("上游数据", "正常", "频率稳定", "非卡点"),
            ("Native 处理", "正常", "计数递进", "非卡点"),
            ("Unity 通道", "正常", "接收稳定", "非卡点"),
        ],
        "evidence": "window=2026-05-18 07:45:00~07:55:00\nupstream_count=12000 native_count=11996 unity_send=11992 unity_recv=11990\nmax_gap=82ms avg_fps=19.98",
    },
    {
        "slug": "ld-lane-level-log-analysis",
        "title": "LD 车道级无图分析报告",
        "skill": "ld-lane-level-log-analysis-portable",
        "status": "red",
        "verdict": "结论：132002 接收、MapDataHandler normal 数据和 tile 请求均正常，最终未进 LD 是 LDDataModel 的 LDConf=false，直接失败位为 nearNew=128。",
        "cards": [
            ("LD 状态", "LD=false", "red", "持续无图"),
            ("LDConf", "false", "red", "门禁失败"),
            ("nearNew", "128", "red", "HD 覆盖/数据可用性失败"),
            ("132002", "收到且放弃 0", "green", "传输正常"),
            ("MapDataHandler", "valid=1", "green", "normal 数据正常"),
            ("tile", "有 add/del", "green", "请求返回存在"),
        ],
        "issues": [
            {"sev": "red", "title": "关键问题：LDConf 被 nearNew=128 卡住", "detail": "LD AND gate 中 conf=false，导致最终 LD=false。"},
            {"sev": "green", "title": "排除项：132002 不是主因", "detail": "目标窗口内 XDataNativeProxy 周期性收到 132002，放弃为 0。"},
            {"sev": "green", "title": "排除项：tile 链路不是主因", "detail": "updateLoadTileCenter 每秒更新，dftileinfo 有 add/delete。"},
        ],
        "chain": [
            {"sev": "green", "title": "132002 传输正常", "evidence": "ReceiveMsg bizCode:132002 收到:197 放弃:0", "downstream": "Unity 协议层有数据。"},
            {"sev": "green", "title": "MapDataHandler 正常", "evidence": "valid:1 mapst:3 navist:2", "downstream": "Baidu normal 数据进入处理链。"},
            {"sev": "green", "title": "tile 请求正常", "evidence": "updateLoadTileCenter + dftileinfo add/del", "downstream": "tile 链路没有断。"},
            {"sev": "red", "title": "LDConf 失败", "evidence": "nearNew=128", "downstream": "最终持续无图。"},
        ],
        "timeline": [
            ("07:51:04", "132002", "收到:197 放弃:0", tag("正常", "green")),
            ("07:51:08", "MapData", "valid:1 mapst:3 navist:2", tag("正常", "green")),
            ("07:51:12", "Tile", "dftileinfo add/del", tag("正常", "green")),
            ("07:51:14", "LDConf", "False nearNew 128", tag("异常", "red")),
        ],
        "matrix": [
            ("Android/Unity 132002", "正常", "收到且放弃 0", "非卡点"),
            ("Baidu normal", "正常", "MapDataHandler valid=1", "非卡点"),
            ("LDDataModel", "异常", "LDConf false nearNew=128", "主卡点"),
        ],
        "evidence": "07:51:04.624 I XDataNativeProxy ReceiveMsg bizCode:132002;收到:197:放弃:0\n07:51:08.231 I XPD_MapDataHandler valid:1 mapst:3 navist:2\n07:51:12.246 I XPD_MapDataHandler dftileinfo:add_size...del_size...\n07:51:14.743 I LDDataModel LDConf: False nearNew 128 scene 0-4-0-0",
    },
    {
        "slug": "addr2line-resolve",
        "title": "Native Crash 地址反解报告",
        "skill": "addr2line-resolve",
        "status": "yellow",
        "verdict": "结论：tombstone 地址可反解到 RenderExtend 渲染扩展函数，但该 skill 只解决符号定位；根因仍需结合崩溃前日志和调用时序判断。",
        "cards": [
            ("目标库", "libRenderExtend.so", "green", "符号表命中"),
            ("崩溃信号", "SIGSEGV", "red", "fault addr 0x0"),
            ("地址", "0x15ae0", "green", "已反解"),
            ("源码位置", "SceneRenderer.cpp:418", "yellow", "示例"),
            ("ROM", "6.0.2", "green", "符号版本匹配"),
            ("报告边界", "辅助定位", "yellow", "不单独定根因"),
        ],
        "issues": [
            {"sev": "yellow", "title": "关键发现：地址命中渲染扩展函数", "detail": "崩溃地址反解到 SceneRenderer::updateTile 附近。"},
            {"sev": "red", "title": "风险项：空指针访问", "detail": "fault addr 0x0，需结合调用前 tile/cache 状态继续确认。"},
            {"sev": "yellow", "title": "边界：仅反解地址", "detail": "addr2line 只能说明崩溃位置，不能替代完整日志根因分析。"},
        ],
        "chain": [
            {"sev": "green", "title": "版本定位", "evidence": "ROM 6.0.2 -> 匹配符号表", "downstream": "反解结果可信。"},
            {"sev": "yellow", "title": "地址反解", "evidence": "0x15ae0 -> SceneRenderer.cpp:418", "downstream": "定位到源码函数。"},
            {"sev": "red", "title": "崩溃类型", "evidence": "SIGSEGV fault addr 0x0", "downstream": "需要追空对象来源。"},
        ],
        "timeline": [
            ("10:31:04", "Tombstone", "SIGSEGV fault addr 0x0", tag("异常", "red")),
            ("10:31:05", "Symbol", "libRenderExtend.sym matched", tag("正常", "green")),
            ("10:31:05", "addr2line", "SceneRenderer.cpp:418", tag("命中", "yellow")),
            ("10:31:06", "Next", "等待日志时序确认", tag("待确认", "yellow")),
        ],
        "matrix": [
            ("符号版本", "正常", "ROM 与符号匹配", "可信"),
            ("地址定位", "命中", "SceneRenderer.cpp:418", "定位结果"),
            ("根因判断", "未完成", "缺崩溃前日志", "需联动其他 skill"),
        ],
        "evidence": "#00 pc 0000000000015ae0 /system/lib64/libRenderExtend.so\nlibRenderExtend.so 0x15ae0 -> SceneRenderer::updateTile\nSceneRenderer.cpp:418",
    },
]


def render_report(module: Any, spec: dict[str, Any]) -> str:
    body = [
        '<div class="container">',
        f"<h1>{module.H(spec['title'])}</h1>",
        (
            f'<div class="sub">样例报告，用于预览 <code>{module.H(spec["skill"])}</code> '
            "的 HTML 信息组织、状态色、链路结构和证据折叠区。内容为仿写样例，不代表真实 Bug 结论。</div>"
        ),
        f'<div class="verdict v-{module.H(spec["status"])}">{module.H(spec["verdict"])}</div>',
        f'<div class="cards">{module.render_cards(spec["cards"])}</div>',
        '<div class="section"><h2>结论摘要</h2>',
        module.render_issue_list(spec["issues"]),
        "</div>",
        module.render_chain(
            spec["chain"],
            title="链路与卡点",
            description="按上游证据、处理中间层和用户可见结果逐层展开；每个节点保留可回溯证据与下游影响。",
        ),
        '<div class="section"><h2>关键时间线</h2>',
        module.render_table(spec["timeline"], ["时间", "节点", "事件", "状态"]),
        "</div>",
        '<div class="section"><h2>排查矩阵</h2>',
        module.render_table(spec["matrix"], ["链路段", "状态", "代表证据", "判断"]),
        "</div>",
        '<div class="section"><h2>可折叠证据区</h2>',
        "<details open><summary>代表日志与脚本输出</summary>",
        f"<pre>{module.H(spec['evidence'])}</pre>",
        "</details></div>",
        '<div class="section"><h2>下一步动作</h2>',
        module.render_table(
            [
                ("自动化", "将本页结构接入对应 skill 的真实脚本输出。"),
                ("证据", "保留每个结论的时间、PID、tag、文件行号或源码路径。"),
                ("降噪", "重复日志聚合成统计和代表样本，完整明细放 JSON。"),
            ],
            ["类型", "动作"],
        ),
        "</div>",
        "</div>",
    ]
    return module.render_document(spec["title"], "".join(body))


def render_index(module: Any) -> str:
    cards = []
    for spec in REPORTS:
        cards.append(
            (
                f'<a class="sample-card {module.H(spec["status"])}" href="{module.H(spec["slug"])}.html">'
                f'<div class="sample-title">{module.H(spec["title"])}</div>'
                f'<div class="sample-skill">{module.H(spec["skill"])}</div>'
                f'<div class="sample-desc">{module.H(spec["verdict"])}</div>'
                "</a>"
            )
        )
    css = (
        module.BASE_REPORT_CSS
        + """
.sample-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }
.sample-card {
  display: block; text-decoration: none; color: inherit; background: var(--c-surface);
  border: 1px solid var(--c-border); border-left: 4px solid var(--c-text-3);
  border-radius: var(--radius); padding: 16px 18px; box-shadow: var(--shadow);
}
.sample-card:hover { box-shadow: 0 2px 12px rgba(0,0,0,.1); }
.sample-card.red { border-left-color: var(--c-red); }
.sample-card.yellow { border-left-color: var(--c-yellow); }
.sample-card.green { border-left-color: var(--c-green); }
.sample-title { font-weight: 700; font-size: 15px; margin-bottom: 4px; }
.sample-skill { color: var(--c-accent); font-size: 12px; font-family: "JetBrains Mono", monospace; margin-bottom: 8px; }
.sample-desc { color: var(--c-text-2); font-size: 13px; line-height: 1.6; }
"""
    )
    body = (
        '<div class="container">'
        "<h1>Bug 分析 Skill HTML 报告样例</h1>"
        '<div class="sub">每个页面都是仿写内容，主要用于查看样式。'
        "本集合只覆盖诊断/分析类 skill；飞书拉取、日志解码、ROM 查询、git-ai 检查等工具类不纳入主样例。</div>"
        '<div class="section"><h2>样例列表</h2><div class="sample-grid">'
        + "".join(cards)
        + "</div></div>"
        "</div>"
    )
    return module.render_document("Bug 分析 Skill HTML 报告样例", body, css=css)


def main() -> None:
    module = load_report_html()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for spec in REPORTS:
        (OUT_DIR / f"{spec['slug']}.html").write_text(render_report(module, spec), encoding="utf-8")
    (OUT_DIR / "index.html").write_text(render_index(module), encoding="utf-8")


if __name__ == "__main__":
    main()
