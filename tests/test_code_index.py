"""Tests for knowledge/code_index.py."""

from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path
from unittest import mock

import pytest

from lark_agent_bridge.knowledge.code_index import (
    CodeIndexClient,
    CodeIndexContext,
    ReferenceHit,
    SymbolHit,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """Create a minimal fake repo directory."""
    src = tmp_path / "src" / "main" / "java"
    src.mkdir(parents=True)
    (src / "Foo.kt").write_text("class Foo { fun bar() {} }")
    return tmp_path


@pytest.fixture()
def client(tmp_repo: Path) -> CodeIndexClient:
    return CodeIndexClient([tmp_repo], timeout=5.0)


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------

class TestIsAvailable:
    def test_available_when_ctags_found(self, client: CodeIndexClient) -> None:
        with mock.patch("shutil.which", return_value="/usr/bin/ctags"):
            assert client.is_available() is True

    def test_not_available_when_ctags_missing(self, client: CodeIndexClient) -> None:
        with mock.patch("shutil.which", return_value=None):
            assert client.is_available() is False


# ---------------------------------------------------------------------------
# ensure_index
# ---------------------------------------------------------------------------

class TestEnsureIndex:
    def test_returns_false_when_ctags_missing(self, client: CodeIndexClient, tmp_repo: Path) -> None:
        with mock.patch("shutil.which", return_value=None):
            assert client.ensure_index(tmp_repo) is False

    def test_builds_index_from_ctags_output(self, client: CodeIndexClient, tmp_repo: Path) -> None:
        ctags_output = "\n".join([
            json.dumps({
                "name": "DataCenter",
                "kind": "class",
                "path": str(tmp_repo / "src/main/java/DataCenter.kt"),
                "line": 10,
                "language": "Kotlin",
                "scope": "",
                "signature": "",
            }),
            json.dumps({
                "name": "mockSignal",
                "kind": "method",
                "path": str(tmp_repo / "src/main/java/DataCenter.kt"),
                "line": 42,
                "language": "Kotlin",
                "scope": "DataCenter",
                "signature": "(signalId: Int)",
            }),
        ])
        with mock.patch("shutil.which", return_value="/usr/bin/ctags"), \
             mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout=ctags_output,
                stderr="",
            )
            assert client.ensure_index(tmp_repo) is True

        # Verify SQLite index was created
        db_path = tmp_repo / ".code_index" / "symbols.sqlite"
        assert db_path.exists()
        con = sqlite3.connect(str(db_path))
        rows = con.execute("SELECT name, kind, line FROM symbols ORDER BY line").fetchall()
        con.close()
        assert len(rows) == 2
        assert rows[0] == ("DataCenter", "class", 10)
        assert rows[1] == ("mockSignal", "method", 42)

    def test_skips_reindex_when_fresh(self, client: CodeIndexClient, tmp_repo: Path) -> None:
        db_dir = tmp_repo / ".code_index"
        db_dir.mkdir()
        db_path = db_dir / "symbols.sqlite"
        con = sqlite3.connect(str(db_path))
        con.execute("CREATE TABLE symbols (name TEXT, kind TEXT, path TEXT, line INTEGER, language TEXT, scope TEXT, signature TEXT)")
        con.commit()
        con.close()

        # Should return True without running ctags
        with mock.patch("subprocess.run") as mock_run:
            assert client.ensure_index(tmp_repo) is True
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# search_symbol
# ---------------------------------------------------------------------------

class TestSearchSymbol:
    def test_exact_match(self, client: CodeIndexClient, tmp_repo: Path) -> None:
        # Seed index
        db_dir = tmp_repo / ".code_index"
        db_dir.mkdir()
        db_path = db_dir / "symbols.sqlite"
        con = sqlite3.connect(str(db_path))
        con.execute("CREATE TABLE symbols (name TEXT, kind TEXT, path TEXT, line INTEGER, language TEXT, scope TEXT, signature TEXT)")
        con.execute("CREATE INDEX idx_symbols_name ON symbols(name)")
        con.execute("INSERT INTO symbols VALUES ('DataCenter','class','src/DataCenter.kt',10,'Kotlin','','')")
        con.execute("INSERT INTO symbols VALUES ('mockSignal','method','src/DataCenter.kt',42,'Kotlin','DataCenter','(signalId: Int)')")
        con.commit()
        con.close()

        hits = client.search_symbol("DataCenter", tmp_repo)
        assert len(hits) == 1
        assert hits[0].name == "DataCenter"
        assert hits[0].kind == "class"
        assert hits[0].line == 10

    def test_no_results(self, client: CodeIndexClient, tmp_repo: Path) -> None:
        db_dir = tmp_repo / ".code_index"
        db_dir.mkdir()
        db_path = db_dir / "symbols.sqlite"
        con = sqlite3.connect(str(db_path))
        con.execute("CREATE TABLE symbols (name TEXT, kind TEXT, path TEXT, line INTEGER, language TEXT, scope TEXT, signature TEXT)")
        con.execute("CREATE INDEX idx_symbols_name ON symbols(name)")
        con.commit()
        con.close()

        hits = client.search_symbol("NonExistent", tmp_repo)
        assert hits == []


# ---------------------------------------------------------------------------
# find_references
# ---------------------------------------------------------------------------

class TestFindReferences:
    def test_parses_rg_json(self, client: CodeIndexClient, tmp_repo: Path) -> None:
        rg_output = "\n".join([
            json.dumps({
                "type": "match",
                "data": {
                    "path": {"text": str(tmp_repo / "src/Foo.kt")},
                    "line_number": 15,
                    "lines": {"text": "  val dc = DataCenter()"},
                },
            }),
            json.dumps({"type": "summary", "data": {}}),
        ])
        with mock.patch("shutil.which", return_value="/usr/bin/rg"), \
             mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout=rg_output,
                stderr="",
            )
            refs = client.find_references("DataCenter", tmp_repo)
            assert len(refs) == 1
            assert refs[0].line == 15
            assert "DataCenter" in refs[0].text

    def test_empty_when_rg_missing(self, client: CodeIndexClient, tmp_repo: Path) -> None:
        with mock.patch("shutil.which", return_value=None):
            refs = client.find_references("DataCenter", tmp_repo)
            assert refs == []


# ---------------------------------------------------------------------------
# get_context
# ---------------------------------------------------------------------------

class TestGetContext:
    def test_combines_defs_and_refs(self, client: CodeIndexClient, tmp_repo: Path) -> None:
        with mock.patch.object(client, "search_symbol") as mock_search, \
             mock.patch.object(client, "find_references") as mock_refs:
            mock_search.return_value = [
                SymbolHit(name="Foo", kind="class", path="Foo.kt", line=1, language="Kotlin")
            ]
            mock_refs.return_value = [
                ReferenceHit(path="Bar.kt", line=10, text="val f = Foo()")
            ]
            ctx = client.get_context("Foo", tmp_repo)
            assert len(ctx.definitions) == 1
            assert len(ctx.references) == 1

    def test_skips_refs_when_no_defs(self, client: CodeIndexClient, tmp_repo: Path) -> None:
        with mock.patch.object(client, "search_symbol", return_value=[]), \
             mock.patch.object(client, "find_references") as mock_refs:
            ctx = client.get_context("Missing", tmp_repo)
            assert ctx.definitions == []
            assert ctx.references == []
            mock_refs.assert_not_called()


# ---------------------------------------------------------------------------
# enrich_prompt
# ---------------------------------------------------------------------------

class TestEnrichPrompt:
    def test_produces_prompt_lines(self, tmp_repo: Path) -> None:
        client = CodeIndexClient([tmp_repo])
        with mock.patch.object(client, "is_available", return_value=True), \
             mock.patch.object(client, "ensure_index", return_value=True), \
             mock.patch.object(client, "get_context") as mock_ctx:
            mock_ctx.return_value = CodeIndexContext(
                definitions=[
                    SymbolHit(name="DataCenter", kind="class", path="DataCenter.kt", line=10, language="Kotlin", scope=""),
                ],
                references=[
                    ReferenceHit(path="Main.kt", line=5, text="DataCenter.init()"),
                ],
            )
            lines = client.enrich_prompt("DataCenter", [tmp_repo])
            assert len(lines) >= 2
            assert "DataCenter" in lines[0]
            assert "引用" in lines[1]
