"""Read-only bash execution safety and git subprocess helpers."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .file_access import _coerce_process_output

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
