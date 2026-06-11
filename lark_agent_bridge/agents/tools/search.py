"""Workspace search: grep_text (ripgrep + Python fallback), search_large_log, glob_paths."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

from .file_access import _SKIP_DIRS, _coerce_process_output, _safe_resolve

# Max file size to read during grep (256KB)
_MAX_GREP_FILE_SIZE = 256 * 1024
_MAX_LARGE_LOG_RESULTS = 80
_MAX_LARGE_LOG_LINE_CHARS = 1200
_BINARY_SKIP_SUFFIXES = {
    ".pyc", ".class", ".o", ".so", ".dylib", ".exe", ".jar", ".zip", ".gz",
    ".tar", ".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2", ".ttf",
}


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


_search_large_log_impl = search_large_log


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
