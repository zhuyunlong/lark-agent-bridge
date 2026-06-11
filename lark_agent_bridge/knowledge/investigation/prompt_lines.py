"""调查 prompt 的各类提示行与优先模块推导。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING
from ..models import SearchHit
from .path_utils import (
    _collect_file_excerpt_lines,
    _display_repo_relative,
    _looks_like_native_question,
    _resolve_module_path,
)


_LOW_VALUE_PATH_HINTS = (
    "src/test",
    "src/androidTest",
    "build/",
    "generated/",
    "third_party/",
    "*.pb.cc",
    "*.pb.h",
    "*.pb.c",
)


_DEFAULT_PRIORITY_MODULES = (
    "module_floorcenter/module_proto/src/main/proto/signal.proto",
    "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/define/mapping/code/SignalMapping.kt",
    "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/center/DataCenter.kt",
    "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/debug/broadcast/DataCenterBroadcastReceiver.java",
)


_SIGNAL_PRIORITY_MODULES = {
    "SIGNAL_CTL_": (
        "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/helper/carcontrol/CarCtlXpilotHelper.kt",
        "module_core/unity_service/src/main/java/com/xiaopeng/guideengine/unity_service/channel/to_unity/AndroidUnityProxy.kt",
    ),
    "SIGNAL_X3D_": (
        "module_core/module_xdata_service/src/main/java/com/xiaopeng/guideengine/xdatanative/transport/XDataTransport.kt",
        "module_core/subreality_biz/src/main/java/com/xiaopeng/ainavi/subreality_biz/tips/TipsBizService.kt",
        "module_display/launcher_subreality_service/src/main/java/com/xiaopeng/ainavi/tips/TipsServiceRepository.kt",
        "module_display/launcher_subreality_service/src/main/java/com/xiaopeng/ainavi/utils/TipsMsgHelper.kt",
    ),
    "SIGNAL_XUI_": (
        "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/helper/xuimanager/XuiContextInfoHelper.kt",
    ),
}


def _source_filter_prompt_lines(
    question: str,
    *,
    exclude_paths: tuple[str, ...] = _LOW_VALUE_PATH_HINTS,
) -> list[str]:
    native_clause = (
        "问题明确涉及 native/JNI/C++ 时，才允许扩展到手写 .cpp/.cc/.h；否则不要读取这些文件。"
        if _looks_like_native_question(question)
        else "除非问题明确涉及 native/JNI/C++，否则不要读取 .cpp/.cc/.h。"
    )
    return [
        "搜索/读码规则：",
        "1. 默认只看手写 .kt/.java/.proto；先定义/映射/分发，再看下游消费。",
        f"2. 忽略以下低价值路径或文件：{', '.join(exclude_paths)}。",
        f"3. {native_clause}",
        "4. 使用 rg 时优先带排除条件，避免扫测试、generated、第三方和 proto 生成产物。",
    ]


def _candidate_hint_prompt_lines(hits: list[SearchHit], repo_roots: list[Path]) -> list[str]:
    if not hits:
        return []
    lines = [
        "候选锚点（仅用于缩小搜索范围，不代表最终结论）：",
        "你必须先验证候选是否真的匹配用户问题；如果候选与源码不符，必须推翻候选并继续搜索。",
    ]
    for index, hit in enumerate(hits[:3], start=1):
        signal = str(hit.metadata.get("signal") or "").strip()
        code = str(hit.metadata.get("code") or "").strip()
        source = _display_repo_relative(hit.source_ref, repo_roots)
        detail = " | ".join(
            part
            for part in (
                hit.title.strip() or hit.source_id.strip(),
                f"signal={signal}" if signal else "",
                f"code={code}" if code else "",
                f"source={source}" if source else "",
            )
            if part
        )
        lines.append(f"{index}. {detail}")
    return lines


def _priority_module_prompt_lines(
    question: str,
    hits: list[SearchHit],
    repo_roots: list[Path],
    *,
    default_modules: tuple[str, ...] = _DEFAULT_PRIORITY_MODULES,
    signal_modules: dict[str, tuple[str, ...]] = _SIGNAL_PRIORITY_MODULES,
) -> list[str]:
    modules = _priority_modules(
        question, hits, repo_roots,
        default_modules=default_modules,
        signal_modules=signal_modules,
    )
    if not modules:
        return []
    roots = _priority_module_roots(modules)
    return [
        "优先阅读这些重点模块（先看定义/映射/分发，再看下游消费；仍需自行验证是否相关）：",
        *[f"- {module}" for module in modules],
        "执行边界：",
        "1. 最多执行 12 个命令。",
        "2. 第一阶段只允许读取上面的重点模块，不要先做全仓搜索。",
        f"3. 第二阶段若仍不足，只允许在重点模块所在目录内补充 rg：{', '.join(roots)}。",
        "4. 第三阶段如果仍不能确认，直接输出低置信边界，不要继续扩仓搜索。",
        "5. 只要已经确认信号定义、映射/生产链、注入能力、至少一条消费/transport 证据，就立即停止搜索并输出 JSON。",
    ]


def _priority_modules(
    question: str,
    hits: list[SearchHit],
    repo_roots: list[Path],
    *,
    default_modules: tuple[str, ...] = _DEFAULT_PRIORITY_MODULES,
    signal_modules: dict[str, tuple[str, ...]] = _SIGNAL_PRIORITY_MODULES,
) -> list[str]:
    modules: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        cleaned = path.strip()
        if not cleaned or cleaned in seen:
            return
        seen.add(cleaned)
        modules.append(cleaned)

    has_signal_hits = any(hit.kind == "signal_proto_entry" or str(hit.metadata.get("signal") or "").strip() for hit in hits)
    if "信号" in question or has_signal_hits:
        for path in default_modules:
            add(path)
    for hit in hits[:5]:
        source = _display_repo_relative(hit.source_ref, repo_roots)
        if source.endswith((".kt", ".java", ".proto")):
            add(source)
        signal = str(hit.metadata.get("signal") or "").strip()
        for prefix, paths in signal_modules.items():
            if signal.startswith(prefix):
                for path in paths:
                    add(path)
    return modules


def _priority_module_roots(modules: list[str]) -> list[str]:
    roots: list[str] = []
    seen: set[str] = set()
    for module in modules:
        parts = Path(module).parts
        if len(parts) >= 2:
            root = "/".join(parts[:2])
        else:
            root = module
        if root and root not in seen:
            seen.add(root)
            roots.append(root)
    return roots


def _prefetched_excerpt_prompt_lines(
    question: str,
    hits: list[SearchHit],
    repo_roots: list[Path],
    *,
    default_modules: tuple[str, ...] = _DEFAULT_PRIORITY_MODULES,
    signal_modules: dict[str, tuple[str, ...]] = _SIGNAL_PRIORITY_MODULES,
) -> list[str]:
    modules = _priority_modules(
        question, hits, repo_roots,
        default_modules=default_modules,
        signal_modules=signal_modules,
    )
    excerpts: list[str] = []
    for module in modules:
        full_path = _resolve_module_path(module, repo_roots)
        if not full_path or not full_path.is_file():
            continue
        lines = _collect_file_excerpt_lines(full_path, module, question, hits)
        excerpts.extend(lines)
        if len(excerpts) >= 8:
            break
    if not excerpts:
        return []
    return [
        "预采样源码摘录（这些是主流程提前抽取的关键片段；如果已足够回答，就不要再搜索）：",
        *excerpts[:8],
    ]


def _code_index_prompt_lines(contexts: list[tuple[Path, Any]]) -> list[str]:
    """Format code index context as prompt enrichment lines."""
    lines: list[str] = ["预索引符号信息（优先使用，减少搜索）："]
    for _, ctx in contexts:
        for defn in ctx.definitions[:5]:
            scope_prefix = f"{defn.scope}." if defn.scope else ""
            lines.append(f"- {defn.kind} {scope_prefix}{defn.name} @ {defn.path}:{defn.line}")
        for ref in ctx.references[:5]:
            lines.append(f"  引用: {ref.path}:{ref.line}: {ref.text}")
    return lines if len(lines) > 1 else []
