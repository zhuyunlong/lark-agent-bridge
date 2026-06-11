"""Bridge-owned read-only tools for pydantic-ai agents (facade over tools/).

These tools provide safe, sandboxed access to source repo workspaces.
All tools are read-only — no file writes or command execution.
Each tool call is logged for observability.

Tool set (aligned with OpenCode / Claude Code / Codex):
  Core I/O:    read_file (with line range), get_file_outline, list_dir
  Search:      grep (with context lines), search_large_log, glob
  Shell:       bash (read-only shell, pipes + rg/sed/awk/jq/git supported)
  Git:         git_log, git_blame_range, repo_overview
  Reasoning:   think (scratchpad)
  Report:      read_report_artifact, read_prepared_log_metadata
  Codegraph:   search_codegraph, get_callers, get_code_context  (when indexed)

实现已拆分到 ``lark_agent_bridge.agents.tools`` 子包：
  cache.py        工具调用 LRU 缓存（ToolCallCache）
  file_access.py  路径沙箱解析 / read_file / list_dir / 文件大纲提取
  search.py       grep_text / search_large_log / glob_paths
  shell.py        只读 bash 安全检查 / git 子进程辅助
  register.py     register_tools 注册入口 / read_bug_context

本模块保留全部原顶层符号的 re-export，外部 import 路径不变。
"""

from __future__ import annotations

# Defensive compat: keep the original top-level imports importable as
# lark_agent_bridge.agents.agent_tools.<name> (e.g. for test patching).
import fnmatch  # noqa: F401
import hashlib  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import shutil  # noqa: F401
import subprocess  # noqa: F401
from collections import OrderedDict  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any  # noqa: F401

from ..log import get_logger  # noqa: F401

from .tools.cache import ToolCallCache
from .tools.file_access import (
    _MAX_READ_BYTES,
    _MAX_READ_LINES,
    _OUTLINE_FALLBACK,
    _OUTLINE_PATTERNS,
    _SKIP_DIRS,
    _coerce_process_output,
    _safe_resolve,
    _safe_resolve_multi,
    extract_file_outline,
    list_dir,
    read_file,
)
from .tools.search import (
    _BINARY_SKIP_SUFFIXES,
    _MAX_GREP_FILE_SIZE,
    _MAX_LARGE_LOG_LINE_CHARS,
    _MAX_LARGE_LOG_RESULTS,
    _display_search_line,
    _search_large_log_impl,
    _search_large_log_python,
    glob_paths,
    grep_text,
    search_large_log,
)
from .tools.shell import (
    _BASH_ALLOWED_FIRST_TOKENS,
    _BASH_BLOCKED_RE,
    _BASH_MAX_OUTPUT,
    _BASH_TIMEOUT,
    _GIT_BLOCKED_SUBCMDS,
    _bash_check_safe,
    _find_git_root,
    _git_run,
)
from .tools.register import (
    logger,
    read_bug_context,
    register_tools,
)

__all__ = [
    "ToolCallCache",
    "extract_file_outline",
    "glob_paths",
    "grep_text",
    "list_dir",
    "logger",
    "read_bug_context",
    "read_file",
    "register_tools",
    "search_large_log",
]
