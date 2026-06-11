"""路径解析、仓库相对显示、文件摘录与 native 问题判定。"""

from __future__ import annotations

from pathlib import Path
from ..models import SearchHit


_NATIVE_HINT_TERMS = ("native", "jni", "c++", "cpp", ".so", "tombstone", "addr2line", "崩溃", "闪退")


def _resolve_module_path(module: str, repo_roots: list[Path]) -> Path | None:
    if not module:
        return None
    candidate = Path(module)
    if candidate.is_absolute():
        return candidate
    for root in repo_roots:
        full = root / module
        if full.exists():
            return full
    return None


def _display_repo_relative(path_text: str, repo_roots: list[Path]) -> str:
    path = (path_text or "").strip()
    if not path:
        return ""
    for root in repo_roots:
        root_text = str(root).rstrip("/") + "/"
        if path.startswith(root_text):
            return path[len(root_text) :]
    return path


def _collect_file_excerpt_lines(full_path: Path, module: str, question: str, hits: list[SearchHit]) -> list[str]:
    try:
        lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    tokens = _excerpt_tokens_for_module(module, question, hits)
    excerpts: list[str] = []
    for lineno, line in enumerate(lines, start=1):
        if any(token and token in line for token in tokens):
            excerpts.append(f"- {module}:{lineno} {line.strip()}")
        if len(excerpts) >= 2:
            break
    return excerpts


def _looks_like_native_question(question: str) -> bool:
    lowered = (question or "").casefold()
    return any(term in lowered for term in _NATIVE_HINT_TERMS)


def _excerpt_tokens_for_module(module: str, question: str, hits: list[SearchHit]) -> list[str]:
    tokens: list[str] = []
    for hit in hits[:5]:
        signal = str(hit.metadata.get("signal") or "").strip()
        code = str(hit.metadata.get("code") or "").strip()
        if signal:
            tokens.append(signal)
        if code:
            tokens.append(code)
    module_lower = module.casefold()
    if "signalmapping" in module_lower:
        tokens.extend(["put(", "SignalCode."])
    if "datacenterbroadcastreceiver" in module_lower:
        tokens.extend(["ACTION_MOCK", "mockSignal"])
    if module_lower.endswith("/datacenter.kt"):
        tokens.extend(["mockSignal(", "notifyObservers", "signalFlow"])
    if "carctlxpilothelper" in module_lower:
        tokens.extend(["onNextData(", "前车起步", "StartRemind", "SignalFormat."])
    if "xdatatransport" in module_lower:
        tokens.extend(["Signal.SignalCode.", "START_REMIND"])
    if "tipsbizservice" in module_lower or "androidunityproxy" in module_lower:
        tokens.extend(["START_REMIND", "SignalCode."])
    if "tipsservicerepository" in module_lower:
        tokens.extend(["handleStartStateTips", "matchStartTipsTxt", "startSignal"])
    if "tipsmsghelper" in module_lower:
        tokens.extend(["matchStartTipsTxt", "Key_Tips_HU_START_REMIND"])
    if module_lower.endswith("signal.proto"):
        tokens.extend(["前车起步", "SIGNAL_"])
    if "前车起步" in question:
        tokens.append("前车起步")
    seen: set[str] = set()
    deduped: list[str] = []
    for token in tokens:
        cleaned = token.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            deduped.append(cleaned)
    return deduped


def _exact_excerpt_tokens(question: str, hits: list[SearchHit]) -> list[str]:
    tokens: list[str] = []
    for hit in hits[:5]:
        signal = str(hit.metadata.get("signal") or "").strip()
        code = str(hit.metadata.get("code") or "").strip()
        if signal:
            tokens.append(signal)
        if code:
            tokens.append(code)
    if "前车起步" in question:
        tokens.append("前车起步")
    seen: set[str] = set()
    deduped: list[str] = []
    for token in tokens:
        if token and token not in seen:
            seen.add(token)
            deduped.append(token)
    return deduped
