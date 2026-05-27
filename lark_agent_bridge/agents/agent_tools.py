"""Bridge-owned read-only tools for pydantic-ai agents.

These tools provide safe, sandboxed access to the bridge workspace.
All tools are read-only — no file writes or command execution.
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


def read_file(path: str, *, workspace: Path, max_chars: int = 50000) -> str:
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
    """Search for text pattern in files within workspace."""
    import re

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return f"Error: invalid regex pattern: {exc}"

    results: list[str] = []
    count = 0
    ws = workspace.resolve()

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
    ws = workspace.resolve()
    matches: list[str] = []
    for filepath in ws.glob(pattern):
        # Skip excluded directories
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


def register_tools(agent: Any, workspace: Path) -> None:
    """Register bridge-owned tools on a pydantic-ai Agent instance.

    The agent must be a pydantic-ai Agent. Tools are registered via
    agent.tool_plain() decorator-style.
    """
    from pydantic_ai import RunContext

    @agent.tool_plain
    def tool_read_file(path: str) -> str:
        """Read a file within the workspace. Provide a relative path."""
        return read_file(path, workspace=workspace)

    @agent.tool_plain
    def tool_grep(pattern: str, glob_filter: str = "**/*") -> str:
        """Search for a regex pattern in files. Optional glob_filter to narrow scope."""
        return grep_text(pattern, workspace=workspace, glob_filter=glob_filter)

    @agent.tool_plain
    def tool_glob(pattern: str) -> str:
        """Find files matching a glob pattern."""
        return glob_paths(pattern, workspace=workspace)

    @agent.tool_plain
    def tool_list_dir(path: str = ".") -> str:
        """List directory contents."""
        return list_dir(path, workspace=workspace)
