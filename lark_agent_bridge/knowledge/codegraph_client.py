"""CodeGraph CLI wrapper for semantic code intelligence.

Uses the ``codegraph`` CLI (``@colbymchenry/codegraph``) for symbol search,
call graph tracing, and context building.  Falls back to the lighter
:mod:`code_index` module (ctags + ripgrep) when codegraph is not available.

All public methods degrade gracefully — ``is_available()`` returns False
when the CLI is absent, and every query method returns an empty result.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class CgSymbolHit:
    """A symbol returned by ``codegraph query``."""
    name: str
    kind: str            # "class", "method", "function", "field", …
    qualified_name: str
    path: str            # relative file path
    line: int
    language: str
    score: float = 0.0
    signature: str = ""


@dataclass(slots=True)
class CgCallerHit:
    """A caller/callee returned by ``codegraph callers/callees``."""
    name: str
    kind: str
    path: str
    line: int


@dataclass(slots=True)
class CgContext:
    """Context assembled by ``codegraph context``."""
    summary: str = ""
    entry_points: list[dict[str, Any]] = field(default_factory=list)
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class CodeGraphClient:
    """Thin subprocess wrapper around the ``codegraph`` CLI."""

    def __init__(
        self,
        *,
        command: str = "codegraph",
        timeout: float = 10.0,
    ) -> None:
        self._command = command
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """True when the codegraph CLI is on PATH."""
        return shutil.which(self._command) is not None

    def is_indexed(self, repo: Path) -> bool:
        """True when *repo* has a ``.codegraph/`` index directory."""
        return (repo / ".codegraph").is_dir()

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def ensure_index(self, repo: Path) -> bool:
        """Initialise + index *repo* if not already indexed.

        Returns True on success, False on failure.
        """
        if self.is_indexed(repo):
            return True
        if not self.is_available():
            return False
        try:
            subprocess.run(
                [self._command, "init", "--index", str(repo)],
                capture_output=True,
                text=True,
                timeout=self._timeout * 12,  # indexing can be slow
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("codegraph init failed for %s: %s", repo, exc)
            return False
        return self.is_indexed(repo)

    # ------------------------------------------------------------------
    # Symbol search
    # ------------------------------------------------------------------

    def search_symbol(
        self,
        query: str,
        repo: Path,
        *,
        limit: int = 10,
        kind: str | None = None,
    ) -> list[CgSymbolHit]:
        """Search for symbols matching *query* in *repo*."""
        cmd = [self._command, "query", query, "--path", str(repo), "--json",
               "--limit", str(limit)]
        if kind:
            cmd.extend(["--kind", kind])
        raw = self._run(cmd)
        if raw is None:
            return []
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            return []
        hits: list[CgSymbolHit] = []
        for item in (items if isinstance(items, list) else []):
            node = item.get("node", item)
            hits.append(CgSymbolHit(
                name=node.get("name", ""),
                kind=node.get("kind", ""),
                qualified_name=node.get("qualifiedName", ""),
                path=node.get("filePath", ""),
                line=int(node.get("startLine", 0)),
                language=node.get("language", ""),
                score=float(item.get("score", 0)),
                signature=node.get("signature", ""),
            ))
        return hits

    # ------------------------------------------------------------------
    # Call graph
    # ------------------------------------------------------------------

    def get_callers(
        self, symbol: str, repo: Path, *, limit: int = 20,
    ) -> list[CgCallerHit]:
        """Return direct callers of *symbol* in *repo*."""
        return self._call_graph("callers", symbol, repo, limit=limit)

    def get_callees(
        self, symbol: str, repo: Path, *, limit: int = 20,
    ) -> list[CgCallerHit]:
        """Return direct callees of *symbol* in *repo*."""
        return self._call_graph("callees", symbol, repo, limit=limit)

    def _call_graph(
        self, direction: str, symbol: str, repo: Path, *, limit: int,
    ) -> list[CgCallerHit]:
        cmd = [self._command, direction, symbol, "--path", str(repo),
               "--json", "--limit", str(limit)]
        raw = self._run(cmd)
        if raw is None:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        items = data.get(direction, []) if isinstance(data, dict) else []
        return [
            CgCallerHit(
                name=c.get("name", ""),
                kind=c.get("kind", ""),
                path=c.get("filePath", ""),
                line=int(c.get("startLine", 0)),
            )
            for c in items
        ]

    # ------------------------------------------------------------------
    # Context building
    # ------------------------------------------------------------------

    def get_context(
        self, task: str, repo: Path, *, max_nodes: int = 30,
    ) -> CgContext:
        """Build a structured context for *task* in *repo*."""
        cmd = [self._command, "context", task, "--path", str(repo),
               "--format", "json", "--no-code", "--max-nodes", str(max_nodes)]
        raw = self._run(cmd)
        if raw is None:
            return CgContext()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return CgContext()
        if not isinstance(data, dict):
            return CgContext()
        return CgContext(
            summary=data.get("summary", ""),
            entry_points=data.get("entryPoints", []),
            nodes=data.get("nodes", []),
            edges=data.get("edges", []),
        )

    # ------------------------------------------------------------------
    # Prompt enrichment
    # ------------------------------------------------------------------

    def enrich_prompt(
        self, query: str, repo_roots: list[Path],
    ) -> list[str]:
        """Return prompt lines summarising codegraph context for *query*."""
        lines: list[str] = []
        for repo in repo_roots:
            if not self.is_indexed(repo):
                continue
            ctx = self.get_context(query, repo)
            if not ctx.entry_points:
                continue
            for ep in ctx.entry_points[:5]:
                name = ep.get("qualifiedName") or ep.get("name", "")
                kind = ep.get("kind", "")
                path = ep.get("filePath", "")
                line = ep.get("startLine", "")
                lines.append(f"- {kind} {name} @ {path}:{line}")
            if ctx.summary:
                lines.append(f"  概要: {ctx.summary[:200]}")
        return lines

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run(self, cmd: list[str]) -> str | None:
        """Run a codegraph CLI command, return stdout or None."""
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("codegraph command failed: %s", exc)
            return None
        if proc.returncode != 0:
            logger.debug("codegraph exited %d: %s", proc.returncode, proc.stderr[:200])
            return None
        return proc.stdout
