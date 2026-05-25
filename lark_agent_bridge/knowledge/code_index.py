"""Local code index for fast symbol lookup and reference search.

Uses Universal Ctags for symbol indexing and ripgrep for reference search.
Results are cached in a per-repo SQLite database to avoid repeated scans.

Both tools are optional — ``is_available()`` checks ``shutil.which()`` and
all public methods degrade gracefully when tools are absent.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_INDEX_DIR = ".code_index"
_DB_NAME = "symbols.sqlite"
_STALE_SECONDS = 3600  # re-index if older than 1 hour


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SymbolHit:
    name: str
    kind: str           # "class", "method", "function", "field", …
    path: str           # relative path within repo
    line: int
    language: str       # "Kotlin", "Java"
    scope: str = ""     # enclosing class/object
    signature: str = ""


@dataclass(slots=True)
class ReferenceHit:
    path: str
    line: int
    text: str


@dataclass(slots=True)
class CodeIndexContext:
    definitions: list[SymbolHit] = field(default_factory=list)
    references: list[ReferenceHit] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class CodeIndexClient:
    """Thin wrapper around ``ctags`` + ``rg`` for local code intelligence."""

    def __init__(
        self,
        repo_roots: list[Path],
        *,
        ctags_command: str = "ctags",
        rg_command: str = "rg",
        timeout: float = 10.0,
        cache_dir: Path | None = None,
    ) -> None:
        self._repo_roots = repo_roots
        self._ctags = ctags_command
        self._rg = rg_command
        self._timeout = timeout
        self._cache_dir = cache_dir  # override per-repo default

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self, repo: Path | None = None) -> bool:
        """Return True if ctags is installed (rg is optional)."""
        return shutil.which(self._ctags) is not None

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def ensure_index(self, repo: Path) -> bool:
        """Build or refresh the ctags index for *repo*.

        Returns True if the index is ready, False on failure.
        """
        db_path = self._db_path(repo)
        if db_path.exists():
            age = time.time() - db_path.stat().st_mtime
            if age < _STALE_SECONDS:
                return True

        if not shutil.which(self._ctags):
            return False

        db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(
                [
                    self._ctags,
                    "-R",
                    "--languages=Java,Kotlin",
                    "--output-format=json",
                    "--fields=+nSs",
                    "-f", "-",
                    str(repo),
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout * 6,  # index can be slow
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("ctags index failed for %s: %s", repo, exc)
            return False

        if proc.returncode != 0:
            logger.warning("ctags exited %d for %s: %s", proc.returncode, repo, proc.stderr[:200])
            return False

        return self._load_ctags_json(proc.stdout, db_path, repo)

    # ------------------------------------------------------------------
    # Symbol search
    # ------------------------------------------------------------------

    def search_symbol(self, query: str, repo: Path) -> list[SymbolHit]:
        """Search cached ctags index for symbols matching *query*."""
        db_path = self._db_path(repo)
        if not db_path.exists():
            return []

        hits: list[SymbolHit] = []
        try:
            con = sqlite3.connect(str(db_path), timeout=2)
            con.row_factory = sqlite3.Row
            # exact match first, then prefix, then substring
            for sql, params in [
                ("SELECT * FROM symbols WHERE name = ? LIMIT 20", (query,)),
                ("SELECT * FROM symbols WHERE name LIKE ? LIMIT 20", (f"{query}%",)),
                ("SELECT * FROM symbols WHERE name LIKE ? LIMIT 20", (f"%{query}%",)),
            ]:
                rows = con.execute(sql, params).fetchall()
                for row in rows:
                    hit = SymbolHit(
                        name=row["name"],
                        kind=row["kind"],
                        path=row["path"],
                        line=row["line"],
                        language=row["language"],
                        scope=row["scope"],
                        signature=row["signature"],
                    )
                    if hit not in hits:
                        hits.append(hit)
                if hits:
                    break
            con.close()
        except sqlite3.Error as exc:
            logger.debug("symbol search failed: %s", exc)
        return hits[:20]

    # ------------------------------------------------------------------
    # Reference search (ripgrep)
    # ------------------------------------------------------------------

    def find_references(
        self,
        symbol: str,
        repo: Path,
        *,
        max_results: int = 20,
    ) -> list[ReferenceHit]:
        """Find references to *symbol* in *repo* using ripgrep."""
        if not shutil.which(self._rg):
            return []

        try:
            proc = subprocess.run(
                [
                    self._rg, "--json", "-w",
                    "--glob", "*.kt",
                    "--glob", "*.java",
                    "--glob", "*.proto",
                    "--max-count", str(max_results),
                    symbol,
                    str(repo),
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("rg search failed: %s", exc)
            return []

        refs: list[ReferenceHit] = []
        for line in proc.stdout.splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "match":
                continue
            data = obj.get("data", {})
            path_obj = data.get("path", {})
            path_text = path_obj.get("text", "") if isinstance(path_obj, dict) else str(path_obj)
            line_number = data.get("line_number", 0)
            lines_obj = data.get("lines", {})
            line_text = lines_obj.get("text", "").strip() if isinstance(lines_obj, dict) else str(lines_obj).strip()
            try:
                rel = str(Path(path_text).relative_to(repo))
            except ValueError:
                rel = path_text
            refs.append(ReferenceHit(path=rel, line=line_number, text=line_text[:200]))
            if len(refs) >= max_results:
                break
        return refs

    # ------------------------------------------------------------------
    # Combined context
    # ------------------------------------------------------------------

    def get_context(self, query: str, repo: Path) -> CodeIndexContext:
        """Get definitions + references for *query* in *repo*."""
        defs = self.search_symbol(query, repo)
        refs = self.find_references(query, repo) if defs else []
        return CodeIndexContext(definitions=defs, references=refs)

    def enrich_prompt(self, query: str, repo_roots: list[Path]) -> list[str]:
        """Return prompt lines summarising indexed symbol info for *query*."""
        lines: list[str] = []
        for repo in repo_roots:
            if not self.is_available(repo):
                continue
            self.ensure_index(repo)
            ctx = self.get_context(query, repo)
            if not ctx.definitions:
                continue
            for sym in ctx.definitions[:5]:
                scope_prefix = f"{sym.scope}." if sym.scope else ""
                lines.append(f"- {sym.kind} {scope_prefix}{sym.name} @ {sym.path}:{sym.line}")
            for ref in ctx.references[:5]:
                lines.append(f"  引用: {ref.path}:{ref.line}: {ref.text}")
        return lines

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _db_path(self, repo: Path) -> Path:
        if self._cache_dir:
            safe_name = str(repo).replace("/", "_").strip("_")
            return self._cache_dir / f"{safe_name}.sqlite"
        return repo / _INDEX_DIR / _DB_NAME

    def _load_ctags_json(self, stdout: str, db_path: Path, repo: Path) -> bool:
        """Parse ctags JSON-lines output and write to SQLite."""
        try:
            con = sqlite3.connect(str(db_path), timeout=5)
            con.execute("DROP TABLE IF EXISTS symbols")
            con.execute(
                "CREATE TABLE symbols ("
                "  name TEXT NOT NULL,"
                "  kind TEXT NOT NULL,"
                "  path TEXT NOT NULL,"
                "  line INTEGER NOT NULL,"
                "  language TEXT NOT NULL DEFAULT '',"
                "  scope TEXT NOT NULL DEFAULT '',"
                "  signature TEXT NOT NULL DEFAULT ''"
                ")"
            )
            con.execute("CREATE INDEX idx_symbols_name ON symbols(name)")

            batch: list[tuple[str, str, str, int, str, str, str]] = []
            for raw_line in stdout.splitlines():
                raw_line = raw_line.strip()
                if not raw_line or not raw_line.startswith("{"):
                    continue
                try:
                    tag = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                name = tag.get("name", "")
                if not name or name.startswith("__"):
                    continue
                path = tag.get("path", "")
                try:
                    path = str(Path(path).relative_to(repo))
                except ValueError:
                    pass
                batch.append((
                    name,
                    tag.get("kind", "unknown"),
                    path,
                    int(tag.get("line", 0)),
                    tag.get("language", ""),
                    tag.get("scope", ""),
                    tag.get("signature", ""),
                ))
                if len(batch) >= 5000:
                    con.executemany("INSERT INTO symbols VALUES (?,?,?,?,?,?,?)", batch)
                    batch.clear()

            if batch:
                con.executemany("INSERT INTO symbols VALUES (?,?,?,?,?,?,?)", batch)
            con.commit()
            con.close()
            logger.info("indexed %s symbols for %s", db_path.stat().st_size, repo)
            return True
        except (sqlite3.Error, OSError) as exc:
            logger.warning("failed to write index %s: %s", db_path, exc)
            return False
