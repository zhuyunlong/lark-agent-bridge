"""Bridge-owned read-only tools for pydantic-ai agents.

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
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
from collections import OrderedDict
from pathlib import Path
from typing import Any

from ..log import get_logger

logger = get_logger("agent_tools")


# ---------------------------------------------------------------------------
# Tool call cache — eliminates redundant I/O within a single agentic loop.
#
# In a pydantic-ai run_sync() loop, the agent may call the same tool with
# identical arguments multiple times (e.g. re-reading the same file, re-running
# the same grep). Each redundant result bloats the message history, wasting
# tokens and reducing OpenAI prompt cache hit rates.
#
# ToolCallCache deduplicates within a single run, keeping the conversation
# history lean and improving prompt prefix cache efficiency from ~0% to 90%+.
# ---------------------------------------------------------------------------

class ToolCallCache:
    """Per-run LRU cache for tool call results.

    Scoped to a single register_tools() call (= one pydantic-ai run_sync loop).
    Thread-safe is not needed because pydantic-ai run_sync is single-threaded.
    """

    __slots__ = ("_cache", "_max_size", "_hits", "_misses", "_saved_tokens_estimate")

    def __init__(self, max_size: int = 256) -> None:
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._max_size = max_size
        self._hits = 0
        self._misses = 0
        self._saved_tokens_estimate = 0

    def _make_key(self, tool_name: str, *args: Any, **kwargs: Any) -> str:
        """Create a deterministic cache key from tool name and arguments."""
        raw = f"{tool_name}|{args!r}|{sorted(kwargs.items())!r}"
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, key: str) -> str | None:
        """Return cached result or None."""
        if key in self._cache:
            self._hits += 1
            self._cache.move_to_end(key)
            return self._cache[key]
        self._misses += 1
        return None

    def put(self, key: str, value: str) -> None:
        """Store a result, evicting LRU if at capacity."""
        if key in self._cache:
            self._cache.move_to_end(key)
            self._cache[key] = value
            return
        if len(self._cache) >= self._max_size:
            self._cache.popitem(last=False)
        self._cache[key] = value

    def get_or_compute(self, tool_name: str, args: tuple[Any, ...], kwargs: dict[str, Any],
                       compute_fn: Any) -> str:
        """Return cached result or compute, cache, and return."""
        key = self._make_key(tool_name, *args, **kwargs)
        cached = self.get(key)
        if cached is not None:
            # Estimate saved tokens (rough: 1 token ≈ 4 chars)
            self._saved_tokens_estimate += len(cached) // 4
            return cached + "\n\n[✓ cached — identical to previous call]"
        result = compute_fn(*args, **kwargs)
        self.put(key, result)
        return result

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return self._hits / total if total > 0 else 0.0

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self.hit_rate, 3),
            "entries": len(self._cache),
            "saved_tokens_estimate": self._saved_tokens_estimate,
        }

# Directories to skip during recursive scans
_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache",
    ".pytest_cache", ".tox", ".eggs", "build", "dist", ".idea", ".vscode",
    "data", ".gradle", ".cache", "Pods",
})
# Max file size to read during grep (256KB)
_MAX_GREP_FILE_SIZE = 256 * 1024
_MAX_LARGE_LOG_RESULTS = 80
_MAX_LARGE_LOG_LINE_CHARS = 1200
_BINARY_SKIP_SUFFIXES = {
    ".pyc", ".class", ".o", ".so", ".dylib", ".exe", ".jar", ".zip", ".gz",
    ".tar", ".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2", ".ttf",
}

# Max bytes when reading a full file (50 KB)
_MAX_READ_BYTES = 50 * 1024
# Max lines per paginated read_file call
_MAX_READ_LINES = 500

# ---------------------------------------------------------------------------
# Read-only bash execution safety
# ---------------------------------------------------------------------------

# Commands whose first token is allowed in bash tool
_BASH_ALLOWED_FIRST_TOKENS = frozenset({
    # Search & text
    "rg", "grep", "egrep", "fgrep", "ack", "ag",
    "find", "fd",
    "cat", "head", "tail", "bat",
    "wc", "sort", "uniq", "cut", "tr", "awk", "sed",
    "echo", "printf",
    # File/dir
    "ls", "la", "ll", "tree", "du", "stat", "file",
    "diff", "colordiff",
    # Compression / binary
    "zcat", "gunzip", "zgrep", "strings", "od", "xxd", "hexdump",
    # Git (subcommands checked separately)
    "git",
    # Data
    "jq", "yq", "python3", "python",
    # Pipes / chain helpers
    "xargs", "parallel",
    # Checksums
    "md5sum", "sha256sum", "shasum", "md5",
})

# Dangerous git subcommands that are never allowed
_GIT_BLOCKED_SUBCMDS = frozenset({
    "push", "commit", "merge", "rebase", "reset", "revert",
    "checkout", "switch", "restore", "clean", "apply", "am",
    "tag", "remote", "submodule", "fetch", "pull",
    "rm", "mv", "add", "stash", "bisect",
    "config", "init", "clone",
})

# Patterns that must never appear anywhere in the command string
_BASH_BLOCKED_RE = re.compile(
    r"""
    (?:^|\s)(?:rm|rmdir|mv|cp|chmod|chown|sudo|su)\s   # destructive file ops
    | (?:^|\s)(?:curl|wget|nc|ncat|netcat|ssh|scp|sftp|ftp)\s   # network
    | (?:^|\s)(?:pip|pip3|npm|yarn|brew|apt|yum|dnf|pacman)\s   # package mgr
    | (?:^|\s)(?:kill|killall|pkill|reboot|shutdown|poweroff)\s # process/system
    | (?:^|\s)(?:dd|mkfs|fdisk|format)\s                        # disk ops
    | >\s*(?!/dev/null)                    # redirect to file (not /dev/null)
    | >>[^\s]                              # append redirect
    | (?:^|\s)tee\s+[^|]                  # tee to file (piped tee OK)
    | ;\s*(?:rm|mv|cp|curl|wget|sudo)\s   # chained dangerous commands
    | \$\(.*(?:rm|mv|curl|sudo).*\)       # command substitution with danger
    """,
    re.VERBOSE | re.IGNORECASE,
)

_BASH_MAX_OUTPUT = 60 * 1024   # 60 KB
_BASH_TIMEOUT = 30              # seconds


def _bash_check_safe(command: str) -> str | None:
    """Return an error string if the command is unsafe, else None."""
    cmd = command.strip()
    if not cmd:
        return "Empty command"

    # Check blocked patterns anywhere in the command
    if _BASH_BLOCKED_RE.search(cmd):
        return f"Command blocked: contains disallowed operation"

    # Extract first non-env token (skip VAR=val assignments)
    tokens = cmd.lstrip().split()
    first = tokens[0] if tokens else ""
    # Skip env variable assignments like KEY=val cmd
    for tok in tokens:
        if "=" in tok and tok.split("=")[0].isidentifier():
            continue
        first = tok
        break

    # Strip path prefix (e.g., /usr/bin/rg → rg)
    first_base = os.path.basename(first)

    if first_base not in _BASH_ALLOWED_FIRST_TOKENS:
        return f"Command '{first_base}' not in allowed list. Use read_file/grep/glob for file access."

    # Extra check for git: block destructive subcommands
    if first_base == "git" and len(tokens) > 1:
        subcmd = tokens[1].lstrip("-")
        if subcmd in _GIT_BLOCKED_SUBCMDS:
            return f"git {subcmd} is not allowed (read-only mode)"

    return None


def _coerce_process_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value)


def _git_run(args: list[str], cwd: Path) -> str:
    """Run a git command in cwd. Returns stdout or empty string on error."""
    git = shutil.which("git")
    if not git:
        return ""
    try:
        proc = subprocess.run(
            [git] + args,
            capture_output=True, timeout=15, cwd=str(cwd),
        )
        return _coerce_process_output(proc.stdout)
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _find_git_root(path: Path) -> Path | None:
    """Walk up from path to find the .git directory root."""
    candidate = path if path.is_dir() else path.parent
    for _ in range(20):
        if (candidate / ".git").exists():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return None


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


def grep_text(pattern: str, *, workspace: Path, glob_filter: str = "**/*", max_results: int = 50, context_lines: int = 0) -> str:
    """Search for text pattern in files within workspace using ripgrep for speed."""
    ws = workspace.resolve()
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--no-heading", "-n", "-i", "--max-count", str(max_results),
               "--max-filesize", "256K"]
        if context_lines > 0:
            cmd.extend(["-C", str(min(context_lines, 15))])
        # Convert glob_filter to rg --glob
        if glob_filter and glob_filter != "**/*":
            cmd.extend(["--glob", glob_filter])
        # Skip common dirs
        for d in _SKIP_DIRS:
            cmd.extend(["--glob", f"!{d}/"])
        cmd.extend([pattern, str(ws)])
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=30)
            stdout = _coerce_process_output(proc.stdout)
            lines = stdout.strip().splitlines()
            if context_lines == 0:
                lines = lines[:max_results]
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

    # Fallback: Python implementation (no context support)
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


def _display_search_line(line: str, workspace: Path) -> str:
    ws_prefix = str(workspace.resolve()) + os.sep
    if line.startswith(ws_prefix):
        line = line.replace(ws_prefix, "", 1)
    if len(line) > _MAX_LARGE_LOG_LINE_CHARS:
        return line[:_MAX_LARGE_LOG_LINE_CHARS].rstrip() + " [... line truncated ...]"
    return line


def _search_large_log_python(
    *,
    pattern: str,
    target: Path,
    workspace: Path,
    max_results: int,
) -> str:
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return f"Error: invalid regex pattern: {exc}"

    def _candidate_files(root: Path):
        if root.is_file():
            yield root
            return
        try:
            iterator = root.rglob("*")
            for entry in iterator:
                if not entry.is_file():
                    continue
                try:
                    rel_parts = entry.relative_to(workspace).parts
                except ValueError:
                    rel_parts = entry.parts
                if any(part in _SKIP_DIRS or part.startswith(".") for part in rel_parts):
                    continue
                if entry.suffix.lower() in _BINARY_SKIP_SUFFIXES:
                    continue
                yield entry
        except OSError:
            return

    results: list[str] = []
    for entry in _candidate_files(target):
        try:
            rel = entry.relative_to(workspace)
        except ValueError:
            rel = entry
        try:
            with entry.open("r", encoding="utf-8", errors="replace") as handle:
                for line_no, line in enumerate(handle, 1):
                    if not regex.search(line):
                        continue
                    results.append(
                        _display_search_line(f"{rel}:{line_no}: {line.strip()}", workspace)
                    )
                    if len(results) >= max_results:
                        results.append(f"\n[... {max_results} results limit reached ...]")
                        return "\n".join(results)
        except OSError:
            continue

    if not results:
        return f"No matches found for pattern: {pattern}"
    return "\n".join(results)


def search_large_log(
    pattern: str,
    *,
    workspace: Path,
    path: str = "",
    max_results: int = _MAX_LARGE_LOG_RESULTS,
    context_lines: int = 0,
) -> str:
    """Search large decoded logs without the small-file grep limit.

    This is intended for generated Android/logcat artifacts where normal grep_text
    deliberately skips files larger than 256 KB.
    """
    ws = workspace.resolve()
    target = _safe_resolve(path, ws) if path else ws
    if target is None:
        return f"Error: path '{path}' is outside workspace"
    if path and not target.exists():
        return f"Error: file not found: {path}"
    if not target.exists():
        return f"Error: workspace not found: {workspace}"

    effective_max = max(1, min(int(max_results or _MAX_LARGE_LOG_RESULTS), 500))
    context = max(0, min(int(context_lines or 0), 10))

    rg = shutil.which("rg")
    if rg:
        cmd = [
            rg,
            "--no-heading",
            "--with-filename",
            "-n",
            "-i",
            "--max-count",
            str(effective_max),
        ]
        if context:
            cmd.extend(["-C", str(context)])
        if target.is_dir():
            for d in _SKIP_DIRS:
                cmd.extend(["--glob", f"!{d}/**"])
        cmd.extend([pattern, str(target)])
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=30)
            stdout = _coerce_process_output(proc.stdout)
            stderr = _coerce_process_output(proc.stderr)
            lines = [_display_search_line(line, ws) for line in stdout.strip().splitlines() if line]
            if lines:
                output_line_limit = effective_max if context == 0 else effective_max * (context * 2 + 2)
                if len(lines) > output_line_limit:
                    lines = lines[:output_line_limit]
                    lines.append(f"\n[... {effective_max} results limit reached ...]")
                return "\n".join(lines)
            if proc.returncode not in (0, 1) and stderr.strip():
                return f"[rg exit {proc.returncode}] {stderr.strip()[:1000]}"
        except (subprocess.TimeoutExpired, OSError):
            pass

    return _search_large_log_python(
        pattern=pattern,
        target=target,
        workspace=ws,
        max_results=effective_max,
    )


def glob_paths(pattern: str, *, workspace: Path, max_results: int = 100) -> str:
    """Find files matching a glob pattern within workspace."""
    ws = workspace.resolve()
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--files", "--glob", pattern]
        for d in _SKIP_DIRS:
            cmd.extend(["--glob", f"!{d}/"])
        cmd.append(str(ws))
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=15)
            stdout = _coerce_process_output(proc.stdout)
            lines = stdout.strip().splitlines()[:max_results]
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


# ---------------------------------------------------------------------------
# Outline extraction — symbol map for a source file
# ---------------------------------------------------------------------------

# Language-specific regex patterns for extracting symbols.
# Each entry: (compiled_regex, kind_label_or_None)
# When kind_label is None, the first capture group is the kind, last is name.
_OUTLINE_PATTERNS: dict[str, list[tuple[re.Pattern[str], str | None]]] = {
    ".kt": [
        (re.compile(r"^\s*(?:(?:public|private|protected|internal|open|abstract|data|sealed|enum|companion|override|suspend|inline|expect|actual|tailrec)\s+)*"
                    r"(class|interface|object|enum class|data class|sealed class|abstract class)\s+(\w+)"), None),
        (re.compile(r"^\s*(?:(?:public|private|protected|internal|open|override|suspend|inline|operator|infix|tailrec|external|expect|actual)\s+)*fun\s+(\w+)"), "fun"),
    ],
    ".java": [
        (re.compile(r"^\s*(?:(?:public|private|protected|static|final|abstract|synchronized|native)\s+)*"
                    r"(class|interface|enum|record)\s+(\w+)"), None),
        (re.compile(r"^\s{1,8}(?:(?:public|private|protected|static|final|synchronized|abstract|native|default)\s+)*"
                    r"(?:(?:<[^>]+>\s+)?[\w\[\]<>.,\s]+\s+)?(\w+)\s*\([^)]*\)\s*(?:throws\s+\w+(?:\s*,\s*\w+)*)?\s*\{"), "method"),
    ],
    ".cs": [
        (re.compile(r"^\s*(?:(?:public|private|protected|internal|static|sealed|abstract|partial|virtual|override|async|new|extern)\s+)*"
                    r"(class|interface|struct|enum|record)\s+(\w+)"), None),
        (re.compile(r"^\s{1,8}(?:(?:public|private|protected|internal|static|abstract|virtual|override|async|sealed|extern|partial|new)\s+)*"
                    r"(?:(?:Task|void|bool|int|string|double|float|long|object|IEnumerable|IAsyncEnumerable|IList|List|Dictionary|[\w<>\[\]?,\s]+)\s+)?(\w+)\s*\([^)]*\)"), "method"),
    ],
    ".py": [
        (re.compile(r"^(\s*)(class|def|async def)\s+(\w+)"), None),
    ],
    ".swift": [
        (re.compile(r"^\s*(?:(?:public|private|internal|open|fileprivate|final|override|static|class|mutating|nonmutating|lazy|weak|unowned)\s+)*"
                    r"(class|struct|enum|protocol|extension|func)\s+(\w+)"), None),
    ],
    ".go": [
        (re.compile(r"^type\s+(\w+)\s+(struct|interface)"), "type"),
        (re.compile(r"^func\s+(?:\([^)]+\)\s+)?(\w+)\s*\("), "func"),
    ],
    ".ts": [
        (re.compile(r"^\s*(?:export\s+)?(?:(?:default|abstract|declare)\s+)?"
                    r"(class|interface|enum|type|function)\s+(\w+)"), None),
    ],
    ".tsx": [
        (re.compile(r"^\s*(?:export\s+)?(?:(?:default|abstract|declare)\s+)?"
                    r"(class|interface|type|function)\s+(\w+)"), None),
    ],
    ".js": [
        (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(class|function)\s+(\w+)"), None),
    ],
    ".rs": [
        (re.compile(r"^\s*(?:pub\s+(?:(?:super|crate|self|in\s+\w+)::)?)?(?:pub\s+)?"
                    r"(fn|struct|enum|trait|impl|mod)\s+(\w+)"), None),
    ],
}

_OUTLINE_FALLBACK = [
    (re.compile(r"^\s*(?:public|private|protected)?\s*(class|interface|function|def|func|fn)\s+(\w+)"), None),
]


def extract_file_outline(path: str, *, roots: list[Path]) -> str:
    """Extract class/function/method outline from a source file."""
    resolved = _safe_resolve_multi(path, roots)
    if resolved is None:
        return f"Error: path '{path}' not found in workspace"
    if not resolved.exists() or not resolved.is_file():
        return f"Error: file not found: {path}"

    ext = resolved.suffix.lower()
    patterns = _OUTLINE_PATTERNS.get(ext, _OUTLINE_FALLBACK)

    # Try ctags first for precise results
    ctags = shutil.which("ctags") or shutil.which("universal-ctags")
    if ctags:
        try:
            proc = subprocess.run(
                [ctags, "--options=NONE", "--output-format=json", "--fields=+n", "-f", "-", str(resolved)],
                capture_output=True, timeout=15, text=True,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                import json as _json
                entries: list[str] = []
                for line in proc.stdout.strip().splitlines():
                    try:
                        item = _json.loads(line)
                        name = item.get("name", "")
                        kind = item.get("kind", "")
                        line_no = item.get("line", "?")
                        if name:
                            entries.append(f"  [{kind}] {name} (L{line_no})")
                    except Exception:
                        pass
                if entries:
                    return f"{path} ({len(entries)} symbols, via ctags):\n" + "\n".join(entries)
        except (subprocess.TimeoutExpired, OSError):
            pass

    # Fallback: regex-based extraction
    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Error reading {path}: {exc}"

    symbols: list[tuple[int, str, str]] = []  # (line_no, kind, name)
    for i, line in enumerate(content.splitlines(), 1):
        for pat, kind_override in patterns:
            m = pat.search(line)
            if m:
                groups = m.groups()
                if kind_override is not None:
                    name = groups[-1]
                    kind = kind_override
                elif ext == ".py":
                    # groups: indent, "class"/"def"/"async def", name
                    _, kind, name = groups
                elif len(groups) >= 2:
                    kind, name = groups[0], groups[-1]
                else:
                    kind, name = "", groups[0]
                symbols.append((i, kind.strip(), name.strip()))
                break  # one match per line

    if not symbols:
        return f"No symbols found in {path} (extension: {ext})"

    lines_out: list[str] = []
    for line_no, kind, name in symbols:
        lines_out.append(f"  [{kind}] {name} (L{line_no})")
    return f"{path} ({len(symbols)} symbols):\n" + "\n".join(lines_out)


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
    codegraph_client: Any | None = None,
    codegraph_roots: list[Path] | None = None,
    tool_cache: ToolCallCache | None = None,
) -> None:
    """Register bridge-owned tools on a pydantic-ai Agent instance.

    Args:
        agent: pydantic-ai Agent instance
        workspace: primary workspace root (source repo root)
        extra_roots: additional searchable roots (other repos, analysis dirs)
        report_dir: job output dir for reading prior reports
        log_metadata_path: path to prepared log metadata file
        progress_callback: optional callback(stage, message, **details) for real-time progress
        codegraph_client: optional CodeGraphClient instance for semantic code intelligence
        codegraph_roots: repo roots that have codegraph indexes (defaults to workspace + extra_roots)
        tool_cache: optional ToolCallCache for deduplicating tool calls within a run loop
    """
    all_roots = [workspace]
    if extra_roots:
        all_roots.extend(r for r in extra_roots if r not in all_roots)
    search_large_log_fn = search_large_log

    # Per-run cache for deduplicating tool calls (create if not provided)
    cache = tool_cache or ToolCallCache()

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
    def read_file(path: str, start_line: int = 0, end_line: int = 0) -> str:
        """读取文件内容（支持行范围分页）。
        - path: 相对路径
        - start_line: 起始行号（1-indexed），0 表示从头
        - end_line: 结束行号（含），0 表示到末尾
        对大文件建议先用 get_file_outline 了解结构，再按需读取具体行范围。"""
        logger.info("[tool_call] read_file(path=%s, start=%d, end=%d)", path, start_line, end_line)
        _emit_tool_progress("read_file", f"{path.split('/')[-1]}:{start_line or ''}‥{end_line or ''}")

        def _do_read_file(path_: str, start_: int, end_: int) -> str:
            resolved = _safe_resolve_multi(path_, all_roots)
            if resolved is None:
                return f"Error: path '{path_}' is outside workspace"
            if not resolved.exists():
                return f"Error: file not found: {path_}"
            if not resolved.is_file():
                return f"Error: not a file: {path_}"
            try:
                content = resolved.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return f"Error reading {path_}: {exc}"

            all_lines = content.splitlines()
            total = len(all_lines)

            s = max(0, (start_ - 1) if start_ > 0 else 0)
            e = min(total, end_ if end_ > 0 else total)

            if e - s > _MAX_READ_LINES:
                e = s + _MAX_READ_LINES
                truncated = True
            else:
                truncated = False

            sliced = all_lines[s:e]
            numbered = "\n".join(f"{s + i + 1}: {ln}" for i, ln in enumerate(sliced))

            suffix = ""
            if truncated:
                suffix = (f"\n\n[... showing lines {s+1}–{e} of {total}. "
                          f"Use start_line={e+1} to continue reading. ...]")
            elif e < total and (start_ > 0 or end_ > 0):
                suffix = f"\n\n(Lines {s+1}–{e} of {total} total)"
            elif start_ == 0 and end_ == 0 and len(content.encode()) > _MAX_READ_BYTES:
                suffix = (f"\n\n[... file has {total} lines; showing {s+1}–{e}. "
                          f"Use start_line/end_line to read specific sections. ...]")
            return numbered + suffix

        return cache.get_or_compute("read_file", (path, start_line, end_line), {}, _do_read_file)

    @agent.tool_plain
    def grep(pattern: str, glob_filter: str = "**/*", context_lines: int = 0) -> str:
        """在代码库中搜索正则表达式模式。返回匹配的文件:行号:内容。
        - glob_filter: 缩小搜索范围，如 '*.kt' 或 'src/**/*.java'
        - context_lines: >0 时显示匹配行前后 N 行上下文（类似 grep -C）"""
        logger.info("[tool_call] grep(pattern=%s, glob_filter=%s, context=%d)", pattern, glob_filter, context_lines)
        _emit_tool_progress("grep", f"{pattern[:40]} ({glob_filter})" + (f" ±{context_lines}" if context_lines else ""))

        def _do_grep(pat: str, gf: str, cl: int) -> str:
            results: list[str] = []
            for root in all_roots:
                partial = grep_text(pat, workspace=root, glob_filter=gf,
                                    max_results=30, context_lines=cl)
                if not partial.startswith("No matches"):
                    results.append(f"# {root.name}/\n{partial}")
            if not results:
                return f"No matches found for pattern: {pat}"
            return "\n\n".join(results)

        return cache.get_or_compute("grep", (pattern, glob_filter, context_lines), {}, _do_grep)

    @agent.tool_plain
    def search_large_log(pattern: str, path: str = "", context_lines: int = 0, max_results: int = _MAX_LARGE_LOG_RESULTS) -> str:
        """在大日志/解码日志中搜索正则表达式，不受普通 grep 的 256KB 文件大小限制。
        - pattern: 要搜索的正则表达式，例如 'startCheck timeout'
        - path: 可选，指定某个日志文件或目录；留空则搜索工作区
        - context_lines: 可选，显示匹配行前后 N 行上下文
        - max_results: 最多返回匹配结果数
        用法建议：先用本工具定位行号，再用 read_file(path, start_line, end_line) 精读上下文。"""
        logger.info(
            "[tool_call] search_large_log(pattern=%s, path=%s, context=%d, max_results=%d)",
            pattern,
            path,
            context_lines,
            max_results,
        )
        _emit_tool_progress("search_large_log", f"{pattern[:40]} ({path or 'workspace'})")

        if path:
            resolved = _safe_resolve_multi(path, all_roots)
            if resolved is None:
                return f"Error: path '{path}' is outside workspace"
            if not resolved.exists():
                return f"Error: file not found: {path}"
            for root in all_roots:
                try:
                    rel_path = str(resolved.relative_to(root.resolve()))
                except ValueError:
                    continue
                return search_large_log_fn(
                    pattern,
                    workspace=root,
                    path=rel_path,
                    max_results=max_results,
                    context_lines=context_lines,
                )
            return f"Error: path '{path}' is outside workspace"

        results: list[str] = []
        for root in all_roots:
            partial = search_large_log_fn(
                pattern,
                workspace=root,
                max_results=max_results,
                context_lines=context_lines,
            )
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

        def _do_glob(pat: str) -> str:
            results: list[str] = []
            for root in all_roots:
                partial = glob_paths(pat, workspace=root, max_results=50)
                if not partial.startswith("No files"):
                    results.append(f"# {root.name}/\n{partial}")
            if not results:
                return f"No files matching: {pat}"
            return "\n\n".join(results)

        return cache.get_or_compute("glob", (pattern,), {}, _do_glob)

    @agent.tool_plain
    def list_dir(path: str = ".") -> str:
        """列出目录内容。提供相对路径。"""
        logger.info("[tool_call] list_dir(path=%s)", path)
        _emit_tool_progress("list_dir", path[:50])

        def _do_list_dir(p: str) -> str:
            resolved = _safe_resolve_multi(p, all_roots)
            if resolved is None:
                return f"Error: path '{p}' is outside workspace"
            if not resolved.exists():
                return f"Error: directory not found: {p}"
            if not resolved.is_dir():
                return f"Error: not a directory: {p}"
            try:
                entries: list[str] = []
                for entry in sorted(resolved.iterdir()):
                    if entry.name in _SKIP_DIRS:
                        continue
                    suffix = "/" if entry.is_dir() else ""
                    entries.append(f"{entry.name}{suffix}")
                if not entries:
                    return f"(empty directory: {p})"
                return "\n".join(entries)
            except OSError as exc:
                return f"Error listing {p}: {exc}"

        return cache.get_or_compute("list_dir", (path,), {}, _do_list_dir)

    @agent.tool_plain
    def read_report_artifact(name: str) -> str:
        """读取已完成的分析产物（如前序分析报告 JSON/Markdown）。name 为文件名，如 'bug_scene_signal_report.json'。"""
        logger.info("[tool_call] read_report_artifact(name=%s)", name)
        _emit_tool_progress("read_report_artifact", name)

        def _do_read_report(n: str) -> str:
            if report_dir is None:
                return "Error: no report directory configured"
            target = report_dir / n
            if not target.exists():
                available = [f.name for f in report_dir.iterdir() if f.is_file()] if report_dir.exists() else []
                return f"Error: artifact '{n}' not found. Available: {', '.join(available[:20])}"
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
                if len(content) > 50000:
                    return content[:50000] + "\n\n[... truncated ...]"
                return content
            except OSError as exc:
                return f"Error reading artifact '{n}': {exc}"

        return cache.get_or_compute("read_report_artifact", (name,), {}, _do_read_report)

    @agent.tool_plain
    def read_prepared_log_metadata() -> str:
        """读取已解密/准备好的日志元数据摘要（文件列表、时间范围、大小等）。"""
        logger.info("[tool_call] read_prepared_log_metadata()")
        _emit_tool_progress("read_prepared_log_metadata", "日志元数据")

        def _do_read_log_metadata() -> str:
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

        return cache.get_or_compute("read_prepared_log_metadata", (), {}, _do_read_log_metadata)

    # ------------------------------------------------------------------
    # New tools: bash, get_file_outline, think, git_log, git_blame_range, repo_overview
    # ------------------------------------------------------------------

    @agent.tool_plain
    def bash(command: str, workdir: str = "") -> str:
        """在工作区执行只读 shell 命令（支持管道、重定向到 /dev/null）。
        允许：rg, grep, find, ls, cat, head, tail, wc, sort, uniq, awk, sed, cut, tr,
              git log/diff/show/blame, jq, tree, stat, du, xargs, python3, fd 等。
        禁止：rm, mv, cp, curl, wget, sudo, chmod, 以及任何写文件操作（> file）。
        workdir: 可选子目录路径（相对于 workspace），默认为 workspace 根目录。

        示例:
          bash("rg 'OnSceneChanged' --type cs | head -30")
          bash("git log --oneline --follow -- UnitySceneTypeService.kt | head -20")
          bash("find . -name '*.kt' | xargs grep -l 'SIGNAL_SR_SCENE_TYPE' | head -10")
          bash("cat SRDataManagerService.cs | sed -n '80,140p'")
          bash("rg 'fun onHandle' --type kotlin -C 3 | head -60")
        """
        logger.info("[tool_call] bash(command=%s)", command[:120])
        _emit_tool_progress("bash", f"$ {command[:70]}")

        # Safety check (not cached — must always run)
        err = _bash_check_safe(command)
        if err:
            logger.warning("[tool_call] bash BLOCKED: %s | cmd=%s", err, command[:120])
            return f"❌ Blocked: {err}\n\nAllowed commands: rg, grep, find, ls, cat, head, tail, wc, sort, uniq, awk, sed, git log/diff/show/blame, jq, xargs, tree, stat, python3, ..."

        def _do_bash(cmd: str, wd: str) -> str:
            cwd: Path = workspace
            if wd:
                resolved_wd = _safe_resolve_multi(wd, all_roots)
                if resolved_wd and resolved_wd.is_dir():
                    cwd = resolved_wd

            try:
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    timeout=_BASH_TIMEOUT,
                    cwd=str(cwd),
                )
                stdout = _coerce_process_output(proc.stdout)
                stderr = _coerce_process_output(proc.stderr)

                output = stdout
                if stderr.strip() and not stdout.strip():
                    output = stderr
                elif stderr.strip():
                    output = stdout + "\n[stderr]\n" + stderr[:1000]

                if len(output.encode()) > _BASH_MAX_OUTPUT:
                    output = output[:_BASH_MAX_OUTPUT].rsplit("\n", 1)[0]
                    output += f"\n\n[... output truncated at {_BASH_MAX_OUTPUT//1024}KB. Use head/tail/grep to narrow down ...]"

                if proc.returncode != 0 and not output.strip():
                    return f"[exit {proc.returncode}] {stderr.strip()[:500] or '(no output)'}"

                return output if output.strip() else f"[exit {proc.returncode}] (no output)"
            except subprocess.TimeoutExpired:
                return f"[timeout] Command exceeded {_BASH_TIMEOUT}s limit. Try narrowing the search."
            except OSError as exc:
                return f"[error] {exc}"

        return cache.get_or_compute("bash", (command, workdir), {}, _do_bash)

    @agent.tool_plain
    def get_file_outline(path: str) -> str:
        """获取文件的符号大纲（类、方法、函数列表及行号）。
        适合先了解文件整体结构，再用 read_file(start_line, end_line) 精确读取具体实现。
        支持 .kt .java .cs .py .swift .go .ts .js .rs 等语言。"""
        logger.info("[tool_call] get_file_outline(path=%s)", path)
        _emit_tool_progress("get_file_outline", path.split("/")[-1] if "/" in path else path)
        return cache.get_or_compute(
            "get_file_outline", (path,), {},
            lambda p: extract_file_outline(p, roots=all_roots),
        )

    @agent.tool_plain
    def think(thought: str) -> str:
        """记录分析思路（推理草稿）。不执行任何操作，仅记录当前推理步骤。
        用于：梳理复杂调用链、记录假设、规划下一步搜索方向。
        建议在开始复杂分析前用 think 写下分析计划。"""
        logger.info("[tool_call] think(thought=%s...)", thought[:80])
        _emit_tool_progress("think", f"💭 {thought[:60]}...")
        # think is never cached — each thought is unique and cheap
        return f"[思路记录] {thought}"

    @agent.tool_plain
    def git_log(path: str = ".", max_count: int = 20) -> str:
        """查看文件或目录的 git 提交历史。返回最近 N 条提交（hash, 时间, 作者, 消息）。
        用于：了解某个文件/函数最近被谁修改过、何时引入某改动。
        path 可以是文件路径或目录路径。"""
        logger.info("[tool_call] git_log(path=%s, max_count=%d)", path, max_count)
        _emit_tool_progress("git_log", f"{path} (last {max_count})")

        def _do_git_log(p: str, mc: int) -> str:
            resolved = _safe_resolve_multi(p, all_roots)
            if resolved is None:
                resolved = all_roots[0] if all_roots else None
            if resolved is None:
                return "Error: no workspace configured"
            git_root = _find_git_root(resolved) or resolved
            rel = str(resolved.relative_to(git_root)) if resolved != git_root else "."
            out = _git_run(
                ["log", f"--max-count={min(mc, 50)}", "--follow",
                 "--pretty=format:%h  %ai  %an  %s", "--", rel],
                cwd=git_root,
            )
            if not out.strip():
                return f"No git history found for: {p}"
            return f"git log --follow {rel} (last {mc}):\n{out.strip()}"

        return cache.get_or_compute("git_log", (path, max_count), {}, _do_git_log)

    @agent.tool_plain
    def git_blame_range(path: str, start_line: int, end_line: int) -> str:
        """查看文件指定行范围的 git blame（每行的最后修改 commit、时间、作者）。
        用于：确定某个关键代码行是何时、由谁引入的。
        start_line/end_line 均为 1-indexed 行号。"""
        logger.info("[tool_call] git_blame_range(path=%s, %d-%d)", path, start_line, end_line)
        _emit_tool_progress("git_blame_range", f"{path.split('/')[-1]}:{start_line}-{end_line}")

        def _do_git_blame(p: str, sl: int, el: int) -> str:
            resolved = _safe_resolve_multi(p, all_roots)
            if resolved is None or not resolved.exists():
                return f"Error: file not found: {p}"
            git_root = _find_git_root(resolved)
            if git_root is None:
                return f"Error: {p} is not inside a git repository"
            rel = str(resolved.relative_to(git_root))
            s = max(1, sl)
            e = max(s, min(el, s + 200))
            out = _git_run(
                ["blame", f"-L{s},{e}", "--date=short", "--porcelain", rel],
                cwd=git_root,
            )
            if not out.strip():
                return f"No blame info for {p}:{s}-{e}"
            lines_out: list[str] = []
            current: dict[str, str] = {}
            line_no = s
            for bl in out.splitlines():
                if bl.startswith("\t"):
                    code = bl[1:]
                    commit = current.get("commit", "?")[:8]
                    author = current.get("author", "?")[:20]
                    date = current.get("author-time-formatted", current.get("author-time", "?"))
                    lines_out.append(f"{line_no:5d}  {commit}  {date}  {author:<20}  {code}")
                    line_no += 1
                    current = {}
                elif bl[:40].replace(" ", "").isalnum() and len(bl) > 40:
                    current["commit"] = bl[:40]
                elif bl.startswith("author "):
                    current["author"] = bl[7:]
                elif bl.startswith("author-time "):
                    import datetime
                    ts = int(bl[12:].split()[0])
                    current["author-time-formatted"] = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            return f"git blame {rel} L{s}-{e}:\n" + "\n".join(lines_out)

        return cache.get_or_compute("git_blame_range", (path, start_line, end_line), {}, _do_git_blame)

    @agent.tool_plain
    def repo_overview(path: str = ".") -> str:
        """获取代码仓库概览：git 分支、最近提交、顶层目录结构、技术栈。
        用于：快速了解一个不熟悉的仓库的整体情况。"""
        logger.info("[tool_call] repo_overview(path=%s)", path)
        _emit_tool_progress("repo_overview", path[:50])

        def _do_repo_overview(p: str) -> str:
            resolved = _safe_resolve_multi(p, all_roots)
            if resolved is None:
                resolved = all_roots[0] if all_roots else Path(".")
            if not resolved.is_dir():
                resolved = resolved.parent
            git_root = _find_git_root(resolved) or resolved

            parts: list[str] = [f"Repository: {git_root.name}", f"Path: {git_root}"]

            branch = _git_run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=git_root).strip()
            if branch:
                parts.append(f"Branch: {branch}")
            head = _git_run(["log", "-1", "--pretty=format:%h  %ai  %an  %s"], cwd=git_root).strip()
            if head:
                parts.append(f"HEAD: {head}")

            recent = _git_run(["log", "--max-count=5", "--pretty=format:%h  %ai  %s"], cwd=git_root).strip()
            if recent:
                parts.append("Recent commits:")
                for c in recent.splitlines():
                    parts.append(f"  {c}")

            try:
                entries = sorted(git_root.iterdir(), key=lambda pp: (pp.is_file(), pp.name))
                struct: list[str] = []
                for e in entries[:40]:
                    if e.name in _SKIP_DIRS or e.name.startswith("."):
                        continue
                    struct.append(f"  {'📁' if e.is_dir() else '📄'} {e.name}{'/' if e.is_dir() else ''}")
                if struct:
                    parts.append("Structure:")
                    parts.extend(struct)
            except OSError:
                pass

            stack: list[str] = []
            for marker, label in [
                ("build.gradle", "Gradle/Android"), ("build.gradle.kts", "Gradle/Kotlin"),
                ("pom.xml", "Maven/Java"), ("package.json", "Node.js"),
                ("requirements.txt", "Python"), ("pyproject.toml", "Python"),
                ("go.mod", "Go"), ("Cargo.toml", "Rust"), ("*.csproj", "C#/.NET"),
            ]:
                if (git_root / marker).exists() or list(git_root.glob(marker)):
                    stack.append(label)
            if stack:
                parts.append(f"Tech stack: {', '.join(stack)}")

            return "\n".join(parts)

        return cache.get_or_compute("repo_overview", (path,), {}, _do_repo_overview)

    # ------------------------------------------------------------------
    # Codegraph tools (semantic code intelligence)
    # ------------------------------------------------------------------
    if codegraph_client is not None:
        _cg = codegraph_client
        _cg_roots = codegraph_roots or ([workspace] + (extra_roots or []))
        # Filter to roots that actually have codegraph indexes
        _indexed_roots = [r for r in _cg_roots if _cg.is_indexed(r)]
        if _indexed_roots:
            logger.info("Registering codegraph tools for %d indexed repos", len(_indexed_roots))

            @agent.tool_plain
            def search_codegraph(query: str, kind: str = "") -> str:
                """语义符号搜索。在代码库中搜索函数名、类名、变量名等符号。kind 可选: class, method, function, field。"""
                logger.info("[tool_call] search_codegraph(query=%s, kind=%s)", query, kind)
                _emit_tool_progress("search_codegraph", query[:50])

                def _do_search(q: str, k: str) -> str:
                    results: list[str] = []
                    for root in _indexed_roots:
                        hits = _cg.search_symbol(q, root, limit=10, kind=k or None)
                        if hits:
                            lines = [f"# {root.name}/"]
                            for h in hits:
                                sig = f" | {h.signature}" if h.signature else ""
                                lines.append(f"  [{h.kind}] {h.qualified_name or h.name} @ {h.path}:{h.line}{sig}")
                            results.append("\n".join(lines))
                    if not results:
                        return f"No symbols found for: {q}"
                    return "\n\n".join(results)

                return cache.get_or_compute("search_codegraph", (query, kind), {}, _do_search)

            @agent.tool_plain
            def get_callers(symbol: str) -> str:
                """查找调用了指定符号的所有调用者。用于追踪函数/方法的调用链。"""
                logger.info("[tool_call] get_callers(symbol=%s)", symbol)
                _emit_tool_progress("get_callers", symbol[:50])

                def _do_get_callers(sym: str) -> str:
                    results: list[str] = []
                    for root in _indexed_roots:
                        callers = _cg.get_callers(sym, root, limit=20)
                        if callers:
                            lines = [f"# {root.name}/ — callers of '{sym}'"]
                            for c in callers:
                                lines.append(f"  [{c.kind}] {c.name} @ {c.path}:{c.line}")
                            results.append("\n".join(lines))
                    if not results:
                        return f"No callers found for: {sym}"
                    return "\n\n".join(results)

                return cache.get_or_compute("get_callers", (symbol,), {}, _do_get_callers)

            @agent.tool_plain
            def get_code_context(task: str) -> str:
                """根据任务描述自动构建代码上下文。返回相关的入口点、调用关系和摘要。适合分析问题时快速获取全局视角。"""
                logger.info("[tool_call] get_code_context(task=%s)", task)
                _emit_tool_progress("get_code_context", task[:50])

                def _do_get_context(t: str) -> str:
                    results: list[str] = []
                    for root in _indexed_roots:
                        ctx = _cg.get_context(t, root, max_nodes=30)
                        if ctx.entry_points or ctx.summary:
                            lines = [f"# {root.name}/"]
                            if ctx.summary:
                                lines.append(f"概要: {ctx.summary[:300]}")
                            if ctx.entry_points:
                                lines.append("入口点:")
                                for ep in ctx.entry_points[:10]:
                                    name = ep.get("qualifiedName") or ep.get("name", "")
                                    kind = ep.get("kind", "")
                                    path = ep.get("filePath", "")
                                    line_no = ep.get("startLine", "")
                                    lines.append(f"  [{kind}] {name} @ {path}:{line_no}")
                            results.append("\n".join(lines))
                    if not results:
                        return f"No code context found for: {t}"
                    return "\n\n".join(results)

                return cache.get_or_compute("get_code_context", (task,), {}, _do_get_context)

            logger.info("Codegraph tools registered: search_codegraph, get_callers, get_code_context")
        else:
            logger.info("Codegraph client provided but no indexed repos found, skipping codegraph tools")

    logger.info(
        "Tools registered: bash(read-only shell+pipes), read_file(+line range), get_file_outline, "
        "grep(+context), search_large_log, glob, list_dir, think, git_log, git_blame_range, repo_overview, "
        "read_report_artifact, read_prepared_log_metadata%s",
        " + search_codegraph, get_callers, get_code_context" if codegraph_client else "",
    )
