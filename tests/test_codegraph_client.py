"""Tests for knowledge/codegraph_client.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from lark_agent_bridge.knowledge.codegraph_client import (
    CgCallerHit,
    CgContext,
    CgSymbolHit,
    CodeGraphClient,
)


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "Foo.kt").write_text("class Foo { fun bar() {} }")
    return tmp_path


@pytest.fixture()
def client() -> CodeGraphClient:
    return CodeGraphClient(timeout=5.0)


# ---------------------------------------------------------------------------
# is_available / is_indexed
# ---------------------------------------------------------------------------

class TestAvailability:
    def test_available_when_on_path(self, client: CodeGraphClient) -> None:
        with mock.patch("shutil.which", return_value="/usr/local/bin/codegraph"):
            assert client.is_available() is True

    def test_not_available_when_missing(self, client: CodeGraphClient) -> None:
        with mock.patch("shutil.which", return_value=None):
            assert client.is_available() is False

    def test_indexed_when_codegraph_dir_exists(self, client: CodeGraphClient, tmp_repo: Path) -> None:
        (tmp_repo / ".codegraph").mkdir()
        assert client.is_indexed(tmp_repo) is True

    def test_indexed_when_codegraph_status_accepts_worktree_without_local_dir(
        self, client: CodeGraphClient, tmp_repo: Path
    ) -> None:
        worktree = tmp_repo / ".worktrees" / "os6_robotaxi"
        worktree.mkdir(parents=True)
        with (
            mock.patch("shutil.which", return_value="/usr/local/bin/codegraph"),
            mock.patch.object(client, "_run", return_value="✓ Index is up to date") as run,
        ):
            assert client.is_indexed(worktree) is True
        run.assert_called_once()

    def test_not_indexed_when_no_dir(self, client: CodeGraphClient, tmp_repo: Path) -> None:
        with (
            mock.patch("shutil.which", return_value="/usr/local/bin/codegraph"),
            mock.patch.object(client, "_run", return_value=None),
        ):
            assert client.is_indexed(tmp_repo) is False


# ---------------------------------------------------------------------------
# search_symbol
# ---------------------------------------------------------------------------

class TestSearchSymbol:
    def test_parses_json_array(self, client: CodeGraphClient, tmp_repo: Path) -> None:
        json_output = json.dumps([
            {
                "node": {
                    "name": "DataCenter",
                    "kind": "class",
                    "qualifiedName": "DataCenter",
                    "filePath": "src/DataCenter.kt",
                    "startLine": 10,
                    "language": "Kotlin",
                    "signature": "",
                },
                "score": 125.0,
            }
        ])
        with mock.patch.object(client, "_run", return_value=json_output):
            hits = client.search_symbol("DataCenter", tmp_repo)
            assert len(hits) == 1
            assert hits[0].name == "DataCenter"
            assert hits[0].kind == "class"
            assert hits[0].line == 10
            assert hits[0].score == 125.0

    def test_returns_empty_on_failure(self, client: CodeGraphClient, tmp_repo: Path) -> None:
        with mock.patch.object(client, "_run", return_value=None):
            hits = client.search_symbol("Missing", tmp_repo)
            assert hits == []


# ---------------------------------------------------------------------------
# get_callers / get_callees
# ---------------------------------------------------------------------------

class TestCallGraph:
    def test_parses_callers_json(self, client: CodeGraphClient, tmp_repo: Path) -> None:
        json_output = json.dumps({
            "symbol": "mockSignal",
            "callers": [
                {"name": "handleBroadcast", "kind": "method",
                 "filePath": "src/Receiver.java", "startLine": 95},
            ],
        })
        with mock.patch.object(client, "_run", return_value=json_output):
            callers = client.get_callers("mockSignal", tmp_repo)
            assert len(callers) == 1
            assert callers[0].name == "handleBroadcast"
            assert callers[0].line == 95

    def test_parses_callees_json(self, client: CodeGraphClient, tmp_repo: Path) -> None:
        json_output = json.dumps({
            "symbol": "run",
            "callees": [
                {"name": "_try_local_signal_probe", "kind": "function",
                 "filePath": "source_investigation.py", "startLine": 107},
            ],
        })
        with mock.patch.object(client, "_run", return_value=json_output):
            callees = client.get_callees("run", tmp_repo)
            assert len(callees) == 1
            assert callees[0].name == "_try_local_signal_probe"

    def test_empty_on_failure(self, client: CodeGraphClient, tmp_repo: Path) -> None:
        with mock.patch.object(client, "_run", return_value=None):
            assert client.get_callers("x", tmp_repo) == []
            assert client.get_callees("x", tmp_repo) == []


# ---------------------------------------------------------------------------
# get_context
# ---------------------------------------------------------------------------

class TestGetContext:
    def test_parses_context_json(self, client: CodeGraphClient, tmp_repo: Path) -> None:
        json_output = json.dumps({
            "query": "signal mock",
            "summary": "Found 3 relevant symbols",
            "entryPoints": [
                {"name": "DataCenter", "kind": "class",
                 "filePath": "DataCenter.kt", "startLine": 10},
            ],
            "nodes": [],
            "edges": [],
        })
        with mock.patch.object(client, "_run", return_value=json_output):
            ctx = client.get_context("signal mock", tmp_repo)
            assert ctx.summary == "Found 3 relevant symbols"
            assert len(ctx.entry_points) == 1
            assert ctx.entry_points[0]["name"] == "DataCenter"

    def test_empty_on_failure(self, client: CodeGraphClient, tmp_repo: Path) -> None:
        with mock.patch.object(client, "_run", return_value=None):
            ctx = client.get_context("missing", tmp_repo)
            assert ctx.summary == ""
            assert ctx.entry_points == []


# ---------------------------------------------------------------------------
# enrich_prompt
# ---------------------------------------------------------------------------

class TestEnrichPrompt:
    def test_produces_lines(self, client: CodeGraphClient, tmp_repo: Path) -> None:
        (tmp_repo / ".codegraph").mkdir()
        with mock.patch.object(client, "get_context") as mock_ctx:
            mock_ctx.return_value = CgContext(
                summary="Found 2 symbols",
                entry_points=[
                    {"name": "DataCenter", "kind": "class",
                     "qualifiedName": "DataCenter", "filePath": "DC.kt",
                     "startLine": 10},
                ],
            )
            lines = client.enrich_prompt("DataCenter", [tmp_repo])
            assert len(lines) >= 1
            assert "DataCenter" in lines[0]
            assert "概要" in lines[1]


# ---------------------------------------------------------------------------
# Integration with source_investigation
# ---------------------------------------------------------------------------

class TestSourceInvestigationIntegration:
    """Verify the codegraph path in SourceInvestigationRunner."""

    def test_codegraph_disabled_skips_path(self) -> None:
        """When codegraph_enabled=False, the codegraph path is skipped."""
        from lark_agent_bridge.knowledge.source_investigation import SourceInvestigationRunner
        from lark_agent_bridge.models import BridgeConfig, SourceInvestigationOptions
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=root / "guideengine",
                source_investigation=SourceInvestigationOptions(
                    codegraph_enabled=False,
                    code_index_enabled=False,
                ),
            )
            runner = SourceInvestigationRunner(config)
            # _try_codegraph should not be called when disabled
            assert runner._codegraph is None

    def test_codegraph_not_available_falls_through(self) -> None:
        """When codegraph CLI is not on PATH, falls through gracefully."""
        from lark_agent_bridge.knowledge.source_investigation import SourceInvestigationRunner
        from lark_agent_bridge.models import BridgeConfig, SourceInvestigationOptions
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=root / "guideengine",
                source_investigation=SourceInvestigationOptions(
                    codegraph_enabled=True,
                    code_index_enabled=False,
                ),
            )
            runner = SourceInvestigationRunner(config)
            with mock.patch("shutil.which", return_value=None):
                result = runner._get_codegraph()
                assert result is None
