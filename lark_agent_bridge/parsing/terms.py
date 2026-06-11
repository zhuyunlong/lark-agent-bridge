"""消息解析触发词与文案常量。"""

from __future__ import annotations



TRIGGER_TERMS = (
    "信号生命周期",
    "调查信号",
    "信号链",
    "信号链路",
    "有没有到 Unity",
    "有没有到Unity",
    "生命周期",
)


SIGNAL_CONTEXT_TERMS = (
    "信号",
    "signal",
    "Signal",
    "生命周期",
    "链路",
    "有没有到 Unity",
    "有没有到Unity",
)


ROM_LOOKUP_TERMS = (
    "rom-version",
    "rom version",
    "ROM版本",
    "rom版本",
    "导航版本",
    "APK版本",
    "apk版本",
    "查打包版本",
    "查导航版本",
    "Maven",
    "maven",
    "Jenkins",
    "jenkins",
    "Napa",
    "napa",
    "符号表",
)


ADDR2LINE_TERMS = (
    "addr2line",
    "地址反解",
    "反解地址",
    "反解符号表",
    "反解导航符号表",
    "分解符号表",
    "分解导航符号表",
    "符号表分解",
    "符号表反解",
    "堆栈解析",
    "解析堆栈",
    "反解堆栈",
    "反解crash",
    "反解 crash",
    "crash分析",
    "crash 分析",
    "tombstone",
)


TRAILING_URL_PUNCTUATION = "，。；;,.、)）]】}"


IDENTITY_TERMS = (
    "你是谁",
    "你是誰",
    "你是什么",
    "你是干嘛的",
    "who are you",
    "what are you",
)


HELP_TERMS = (
    "help",
    "/help",
    "帮助",
    "怎么用",
    "如何用",
    "你会什么",
    "你能做什么",
)


GREETING_TERMS = (
    "你好",
    "您好",
    "hi",
    "hello",
    "在吗",
    "在嘛",
)


CLAUDE_TRIGGER_TERMS = (
    "用claude code分析",
    "用 Claude Code 分析",
    "让claude code分析",
    "让 Claude Code 分析",
    "claude code 帮我分析",
    "Claude Code 帮我分析",
    "skill分析",
    "skill 分析",
    "技能分析",
)


PERCEPTION_TRIGGER_TERMS = (
    "当前感知数据",
    "感知数据链路",
    "感知链路",
    "感知数据总结",
    "perception summary",
    "perception-summary",
    "感知统计",
    "vhalhelper",
    "mapdatahandler",
    "x3dcb",
    "xdatanativeproxy",
)


OMLX_CHAT_TERMS = (
    "是什么",
    "为什么",
    "怎么",
    "如何",
    "能不能",
    "可以",
    "解释",
    "介绍",
    "翻译",
    "写一段",
    "帮我想",
    "聊聊",
    "what",
    "why",
    "how",
    "can you",
    "could you",
)


TASK_TERMS = (
    "日志",
    "排查",
    "分析",
    "报告",
    "仓库",
    "代码",
    "文件",
    "执行",
    "命令",
    "权限",
)


FOLLOWUP_RETRY_TERMS = (
    "重试",
    "重试一次",
    "再来一次",
    "重新跑",
    "重新执行",
    "再跑一次",
    "重跑",
)


FOLLOWUP_CONTINUE_TERMS = (
    "继续",
    "继续分析",
    "接着查",
    "继续查",
)


DIRECT_ANALYSIS_ACTION_TERMS = (
    "分析",
    "排查",
    "调查",
    "定位",
    "检查",
    "看下",
    "看一下",
    "看看",
    "查下",
    "查一下",
    "帮我看",
    "帮我分析",
)


DIRECT_ANALYSIS_DOMAIN_TERMS = (
    "日志",
    "附件",
    "文件",
    "压缩包",
    "目录",
    "启动",
    "卡顿",
    "黑屏",
    "闪退",
    "crash",
    "tombstone",
    "堆栈",
    "3d",
    "unity",
    "源码",
    "超速",
    "状态",
)


APP_SERVER_INVESTIGATION_AUTO_TERMS = ("auto模式", "auto")


APP_SERVER_INVESTIGATION_FREE_TERMS = ("全技能自主分析", "全技能分析", "自主分析")


SOURCE_ANALYSIS_TERMS = (
    "源码",
    "源代码",
    "基于源码",
    "根据源码",
    "从源码",
    "看源码",
    "读源码",
    "查源码",
    "源码分析",
    "源码调查",
    "source code",
)


SOURCE_ANALYSIS_ACTION_TERMS = (
    "分析",
    "调查",
    "定位",
    "解释",
    "梳理",
    "看下",
    "查下",
    "链路",
    "流程",
    "时序",
    "监听",
    "注册",
    "分发",
    "调用",
    "怎么",
    "如何",
)


SOURCE_ANALYSIS_CHAIN_TERMS = (
    "链路",
    "流程",
    "时序",
    "数据流",
    "泳道",
    "监听",
    "注册",
    "分发",
    "调用",
    "回调",
)


REPORT_FOLLOWUP_TERMS = (
    "HTML 报告",
    "html 报告",
    "HTML输出",
    "HTML 输出",
    "输出报告",
    "整理成 HTML",
    "整理成html",
    "泳道图",
    "时序图",
    "流程图",
    "数据流图",
    "链路图",
    "画图",
    "画出",
)


REQUIREMENT_ANALYSIS_TERMS = (
    "需求",
    "需求链接",
    "需求分析",
    "story",
)


REQUIREMENT_ANALYSIS_ACTION_TERMS = (
    "结合源码",
    "基于源码",
    "源码分析",
    "源代码",
    "源码对比",
    "可行",
    "可行性",
    "实现方案",
)


_SOURCE_TARGET_STOP_WORDS = {
    "source",
    "code",
    "html",
    "unity",
    "signal",
}


# ---------------------------------------------------------------------------
# 可选的触发词覆盖：config/parser_terms.toml 中同名 key（list[str]）将覆盖
# 本模块的内置触发词常量。文件不存在时行为与内置完全一致。
# 示例：
#   TRIGGER_TERMS = ["信号生命周期", "自定义触发词"]
# ---------------------------------------------------------------------------

def _apply_term_overrides(namespace: dict[str, object]) -> None:
    import tomllib

    from ..profile_registry import CONFIG_DIR

    path = CONFIG_DIR / "parser_terms.toml"
    if not path.exists():
        return
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return
    for key, value in data.items():
        current = namespace.get(key)
        if current is None or not isinstance(value, list):
            continue
        if not all(isinstance(item, str) for item in value):
            continue
        if isinstance(current, tuple):
            namespace[key] = tuple(value)
        elif isinstance(current, set):
            namespace[key] = set(value)


_apply_term_overrides(globals())
