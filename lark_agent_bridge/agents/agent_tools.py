"""Bridge-owned read-only tools for pydantic-ai agents.

These tools provide safe, sandboxed access to source repo workspaces.
All tools are read-only — no file writes or command execution.
Each tool call is logged for observability.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any

from ..log import get_logger

logger = get_logger("agent_tools")

# Directories to skip during recursive scans
_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache",
    ".pytest_cache", ".tox", ".eggs", "build", "dist", ".idea", ".vscode",
    "data", ".gradle", ".cache", "Pods",
})
# Max file size to read during grep (256KB)
_MAX_GREP_FILE_SIZE = 256 * 1024


def _safe_resolve(path_str: str, workspace: Path) -> Path | None:
    """Resolve a path and verify it's within workspace. Returns None if unsafe."""
    try:
        resolved = (workspace / path_str).resolve()
        if not str(resolved).startswith(str(workspace.resolve())):
            return None
        return resolved
    except (ValueError, OSError):
        return None


def _safe_resolve_multi(path_str: str, roots: list[Path]) -> Path | None:
    """Resolve a path against multiple workspace roots. Returns first match."""
    for root in roots:
        result = _safe_resolve(path_str, root)
        if result is not None and result.exists():
            return result
    # Try first root for non-existent paths (for error messages)
    if roots:
        return _safe_resolve(path_str, roots[0])
    return None


def read_file(path: str, *, workspace: Path, max_chars: int = 20000) -> str:
    """Read a file within the workspace. Returns content or error message."""
    resolved = _safe_resolve(path, workspace)
    if resolved is None:
        return f"Error: path '{path}' is outside workspace"
    if not resolved.exists():
        return f"Error: file not found: {path}"
    if not resolved.is_file():
        return f"Error: not a file: {path}"
    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
        if len(content) > max_chars:
            return content[:max_chars] + f"\n\n[... truncated at {max_chars} chars ...]"
        return content
    except OSError as exc:
        return f"Error reading {path}: {exc}"


def grep_text(pattern: str, *, workspace: Path, glob_filter: str = "**/*", max_results: int = 50) -> str:
    """Search for text pattern in files within workspace using ripgrep for speed."""
    import shutil
    import subprocess

    ws = workspace.resolve()
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--no-heading", "-n", "-i", "--max-count", str(max_results),
               "--max-filesize", "256K"]
        # Convert glob_filter to rg --glob
        if glob_filter and glob_filter != "**/*":
            cmd.extend(["--glob", glob_filter])
        # Skip common dirs
        for d in _SKIP_DIRS:
            cmd.extend(["--glob", f"!{d}/"])
        cmd.extend([pattern, str(ws)])
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            lines = proc.stdout.strip().splitlines()[:max_results]
            if not lines:
                return f"No matches found for pattern: {pattern}"
            # Make paths relative
            out: list[str] = []
            ws_prefix = str(ws) + "/"
            for line in lines:
                out.append(line.replace(ws_prefix, ""))
            return "\n".join(out)
        except (subprocess.TimeoutExpired, OSError):
            pass  # fallback to Python

    # Fallback: Python implementation
    import re
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return f"Error: invalid regex pattern: {exc}"

    results: list[str] = []
    count = 0

    def _walk(root: Path) -> None:
        nonlocal count
        if count >= max_results:
            return
        try:
            entries = sorted(root.iterdir())
        except OSError:
            return
        for entry in entries:
            if count >= max_results:
                return
            if entry.is_dir():
                if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                    continue
                _walk(entry)
            elif entry.is_file():
                if entry.suffix in {".pyc", ".class", ".o", ".so", ".dylib", ".exe", ".jar", ".zip", ".gz", ".tar", ".png", ".jpg", ".ico", ".woff", ".woff2", ".ttf"}:
                    continue
                try:
                    if entry.stat().st_size > _MAX_GREP_FILE_SIZE:
                        continue
                except OSError:
                    continue
                rel = entry.relative_to(ws)
                rel_str = str(rel)
                if glob_filter != "**/*" and not fnmatch.fnmatch(rel_str, glob_filter):
                    continue
                try:
                    content = entry.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for i, line in enumerate(content.splitlines(), 1):
                    if regex.search(line):
                        results.append(f"{rel}:{i}: {line.strip()}")
                        count += 1
                        if count >= max_results:
                            results.append(f"\n[... {max_results} results limit reached ...]")
                            return

    _walk(ws)
    if not results:
        return f"No matches found for pattern: {pattern}"
    return "\n".join(results)


def glob_paths(pattern: str, *, workspace: Path, max_results: int = 100) -> str:
    """Find files matching a glob pattern within workspace."""
    import shutil
    import subprocess

    ws = workspace.resolve()
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--files", "--glob", pattern]
        for d in _SKIP_DIRS:
            cmd.extend(["--glob", f"!{d}/"])
        cmd.append(str(ws))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            lines = proc.stdout.strip().splitlines()[:max_results]
            if not lines:
                return f"No files matching: {pattern}"
            ws_prefix = str(ws) + "/"
            out = [line.replace(ws_prefix, "") for line in lines]
            if len(lines) >= max_results:
                out.append(f"[... {max_results} results limit reached ...]")
            return "\n".join(out)
        except (subprocess.TimeoutExpired, OSError):
            pass  # fallback

    # Fallback: Python glob
    matches: list[str] = []
    for filepath in ws.glob(pattern):
        try:
            rel_parts = filepath.relative_to(ws).parts
        except ValueError:
            continue
        if any(p in _SKIP_DIRS or (p.startswith(".") and p != ".") for p in rel_parts):
            continue
        matches.append(str(filepath.relative_to(ws)))
        if len(matches) >= max_results:
            matches.append(f"[... {max_results} results limit reached ...]")
            break
    if not matches:
        return f"No files matching: {pattern}"
    return "\n".join(sorted(matches))


def list_dir(path: str = ".", *, workspace: Path) -> str:
    """List directory contents within workspace."""
    resolved = _safe_resolve(path, workspace)
    if resolved is None:
        return f"Error: path '{path}' is outside workspace"
    if not resolved.exists():
        return f"Error: directory not found: {path}"
    if not resolved.is_dir():
        return f"Error: not a directory: {path}"
    try:
        entries: list[str] = []
        for entry in sorted(resolved.iterdir()):
            if entry.name in _SKIP_DIRS:
                continue
            rel = entry.relative_to(workspace.resolve())
            suffix = "/" if entry.is_dir() else ""
            entries.append(f"{rel}{suffix}")
        if not entries:
            return f"(empty directory: {path})"
        return "\n".join(entries)
    except OSError as exc:
        return f"Error listing {path}: {exc}"


def read_bug_context(
    *,
    title: str,
    description: str,
    fault_time: str,
    request_text: str,
) -> str:
    """Format bug context as readable text for the agent."""
    parts = [
        "# Bug 上下文",
        f"## 标题\n{title}" if title else "",
        f"## 故障时间\n{fault_time}" if fault_time else "",
        f"## 用户请求\n{request_text}" if request_text else "",
        f"## 缺陷描述\n{description}" if description else "",
    ]
    return "\n\n".join(p for p in parts if p)


def register_tools(
    agent: Any,
    workspace: Path,
    *,
    extra_roots: list[Path] | None = None,
    report_dir: Path | None = None,
    log_metadata_path: Path | None = None,
    progress_callback: Any | None = None,
) -> None:
    """Register bridge-owned tools on a pydantic-ai Agent instance.

    Args:
        agent: pydantic-ai Agent instance
        workspace: primary workspace root (source repo root)
        extra_roots: additional searchable roots (other repos, analysis dirs)
        report_dir: job output dir for reading prior reports
        log_metadata_path: path to prepared log metadata file
        progress_callback: optional callback(stage, message, **details) for real-time progress
    """
    all_roots = [workspace]
    if extra_roots:
        all_roots.extend(r for r in extra_roots if r not in all_roots)

    # Counter for real-time progress
    _call_count = [0]

    def _emit_tool_progress(tool_name: str, summary: str) -> None:
        """Emit real-time progress for each tool call."""
        _call_count[0] += 1
        if progress_callback is not None:
            try:
                progress_callback(
                    stage="source_analysis_tool_call",
                    message=f"🔍 [{_call_count[0]}] {tool_name}: {summary}",
                    tool=tool_name,
                    call_index=_call_count[0],
                )
            except Exception:
                pass  # never let progress emission break tool execution

    @agent.tool_plain
    def read_file(path: str) -> str:
        """读取文件内容。提供相对路径。可读取源码文件、配置文件、日志摘要等。"""
        logger.info("[tool_call] read_file(path=%s)", path)
        _emit_tool_progress("read_file", path.split("/")[-1] if "/" in path else path)
        resolved = _safe_resolve_multi(path, all_roots)
        if resolved is None:
            return f"Error: path '{path}' is outside workspace"
        if not resolved.exists():
            return f"Error: file not found: {path}"
        if not resolved.is_file():
            return f"Error: not a file: {path}"
        try:
            content = resolved.read_text(encoding="utf-8", errors="replace")
            if len(content) > 50000:
                return content[:50000] + f"\n\n[... truncated at 50000 chars ...]"
            return content
        except OSError as exc:
            return f"Error reading {path}: {exc}"

    @agent.tool_plain
    def grep(pattern: str, glob_filter: str = "**/*") -> str:
        """在代码库中搜索正则表达式模式。返回匹配的文件:行号:内容。可用 glob_filter 缩小范围，如 '*.kt' 或 'src/**/*.java'。"""
        logger.info("[tool_call] grep(pattern=%s, glob_filter=%s)", pattern, glob_filter)
        _emit_tool_progress("grep", f"{pattern[:40]} ({glob_filter})")
        results: list[str] = []
        for root in all_roots:
            partial = grep_text(pattern, workspace=root, glob_filter=glob_filter, max_results=30)
            if not partial.startswith("No matches"):
                results.append(f"# {root.name}/\n{partial}")
        if not results:
            return f"No matches found for pattern: {pattern}"
        return "\n\n".join(results)

    @agent.tool_plain
    def glob(pattern: str) -> str:
        """查找匹配 glob 模式的文件路径。例如 '**/*.kt'、'src/**/Signal*.java'。"""
        logger.info("[tool_call] glob(pattern=%s)", pattern)
        _emit_tool_progress("glob", pattern[:50])
        results: list[str] = []
        for root in all_roots:
            partial = glob_paths(pattern, workspace=root, max_results=50)
            if not partial.startswith("No files"):
                results.append(f"# {root.name}/\n{partial}")
        if not results:
            return f"No files matching: {pattern}"
        return "\n\n".join(results)

    @agent.tool_plain
    def list_dir(path: str = ".") -> str:
        """列出目录内容。提供相对路径。"""
        logger.info("[tool_call] list_dir(path=%s)", path)
        _emit_tool_progress("list_dir", path[:50])
        resolved = _safe_resolve_multi(path, all_roots)
        if resolved is None:
            return f"Error: path '{path}' is outside workspace"
        if not resolved.exists():
            return f"Error: directory not found: {path}"
        if not resolved.is_dir():
            return f"Error: not a directory: {path}"
        try:
            entries: list[str] = []
            for entry in sorted(resolved.iterdir()):
                if entry.name in _SKIP_DIRS:
                    continue
                suffix = "/" if entry.is_dir() else ""
                entries.append(f"{entry.name}{suffix}")
            if not entries:
                return f"(empty directory: {path})"
            return "\n".join(entries)
        except OSError as exc:
            return f"Error listing {path}: {exc}"

    @agent.tool_plain
    def read_report_artifact(name: str) -> str:
        """读取已完成的分析产物（如前序分析报告 JSON/Markdown）。name 为文件名，如 'bug_scene_signal_report.json'。"""
        logger.info("[tool_call] read_report_artifact(name=%s)", name)
        _emit_tool_progress("read_report_artifact", name)
        if report_dir is None:
            return "Error: no report directory configured"
        target = report_dir / name
        if not target.exists():
            available = [f.name for f in report_dir.iterdir() if f.is_file()] if report_dir.exists() else []
            return f"Error: artifact '{name}' not found. Available: {', '.join(available[:20])}"
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
            if len(content) > 50000:
                return content[:50000] + "\n\n[... truncated ...]"
            return content
        except OSError as exc:
            return f"Error reading artifact '{name}': {exc}"

    @agent.tool_plain
    def read_prepared_log_metadata() -> str:
        """读取已解密/准备好的日志元数据摘要（文件列表、时间范围、大小等）。"""
        logger.info("[tool_call] read_prepared_log_metadata()")
        _emit_tool_progress("read_prepared_log_metadata", "日志元数据")
        if log_metadata_path is None:
            return "Error: no log metadata path configured"
        if not log_metadata_path.exists():
            return f"Error: log metadata not found at {log_metadata_path}"
        try:
            content = log_metadata_path.read_text(encoding="utf-8", errors="replace")
            if len(content) > 30000:
                return content[:30000] + "\n\n[... truncated ...]"
            return content
        except OSError as exc:
            return f"Error reading log metadata: {exc}"
