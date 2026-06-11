"""Workspace-sandboxed file access: path safety, read_file, list_dir, outline."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

# Directories to skip during recursive scans
_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache",
    ".pytest_cache", ".tox", ".eggs", "build", "dist", ".idea", ".vscode",
    "data", ".gradle", ".cache", "Pods",
})

# Max bytes when reading a full file (50 KB)
_MAX_READ_BYTES = 50 * 1024
# Max lines per paginated read_file call
_MAX_READ_LINES = 500


def _coerce_process_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value)


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
