"""Scenario tests: simulate Feishu Q&A interactions for codegraph + code_index pipeline.

Tests 3 categories as user requested:
  1. Bug link analysis (3+ scenarios)
  2. Source investigation follow-up conversations (3+ scenarios)
  3. Knowledge-base source-level Q&As (3+ scenarios)
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from lark_agent_bridge.knowledge.source_investigation import (
    SourceInvestigationResult,
    SourceInvestigationRunner,
    _codegraph_confidence,
    _codegraph_to_code_index_context,
    _result_from_codegraph,
)
from lark_agent_bridge.knowledge.models import SearchHit
from lark_agent_bridge.models import (
    BridgeConfig,
    SourceInvestigationOptions,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _make_config(tmp: str, *, codegraph: bool = True, code_index: bool = True) -> BridgeConfig:
    root = Path(tmp)
    return BridgeConfig(
        data_dir=root,
        guideengine_repo=root / "guideengine",
        source_investigation=SourceInvestigationOptions(
            enabled=True,
            codegraph_enabled=codegraph,
            code_index_enabled=code_index,
            codegraph_min_confidence=0.5,
            code_index_min_confidence=0.5,
            repo_roots=[root / "guideengine"],
        ),
    )


def _hit(question: str, signal: str = "", repo: str = "guideengine") -> SearchHit:
    meta: dict[str, Any] = {"repo": repo}
    if signal:
        meta["signal"] = signal
    return SearchHit(
        chunk_id="test-chunk",
        source_id="test-source",
        title=question,
        content=question,
        source_ref="",
        kind="template",
        score=0.9,
        metadata=meta,
    )


def _mock_codegraph_search(hits_data: list[dict]) -> str:
    return json.dumps([
        {
            "node": {
                "name": h.get("name", "Symbol"),
                "kind": h.get("kind", "function"),
                "qualifiedName": h.get("qname", h.get("name", "Symbol")),
                "filePath": h.get("path", "src/Foo.kt"),
                "startLine": h.get("line", 10),
                "language": h.get("lang", "Kotlin"),
                "signature": "",
            },
            "score": h.get("score", 100.0),
        }
        for h in hits_data
    ])


def _mock_codegraph_callers(symbol: str, callers: list[dict]) -> str:
    return json.dumps({
        "symbol": symbol,
        "callers": [
            {"name": c.get("name", "caller"), "kind": "method",
             "filePath": c.get("path", "Receiver.kt"), "startLine": c.get("line", 1)}
            for c in callers
        ],
    })


# ===========================================================================
# Category 1: Bug link analysis (3+ scenarios)
# ===========================================================================

class TestBugLinkAnalysis:
    """Simulate receiving Feishu bug links and analyzing source code."""

    def test_bug_analysis_signal_datacenter_mock_broadcast(self) -> None:
        """Bug: DataCenter mock信号不触发.
        Should find DataCenter class + mockSignal callers via codegraph.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            runner = SourceInvestigationRunner(config)

            question = "DataCenter mock信号不触发"
            hits = [_hit(question, signal="DataCenter.mockSignal")]

            with mock.patch.object(runner, "_get_codegraph") as mock_cg:
                from lark_agent_bridge.knowledge.codegraph_client import (
                    CgCallerHit,
                    CgSymbolHit,
                    CodeGraphClient,
                )
                client = mock.MagicMock(spec=CodeGraphClient)
                client.is_indexed.return_value = True
                client.search_symbol.return_value = [
                    CgSymbolHit(name="DataCenter", kind="class",
                                qualified_name="DataCenter", path="src/DataCenter.kt",
                                line=15, language="Kotlin", score=120.0),
                    CgSymbolHit(name="mockSignal", kind="method",
                                qualified_name="DataCenter.mockSignal", path="src/DataCenter.kt",
                                line=45, language="Kotlin", score=90.0),
                ]
                client.get_callers.return_value = [
                    CgCallerHit(name="handleBroadcast", kind="method",
                                path="src/Receiver.kt", line=67),
                    CgCallerHit(name="onSignalChanged", kind="method",
                                path="src/SignalManager.kt", line=120),
                ]
                mock_cg.return_value = client

                result = runner._try_codegraph(
                    question,
                    hits=[hits[0]],
                    repo_roots=[Path(tmp) / "guideengine"],
                )
                assert result is not None
                assert result.success is True
                assert "DataCenter" in result.answer
                assert result.confidence >= 0.5

    def test_bug_analysis_signal_missing_theme_download(self) -> None:
        """Bug: 主题下载失败.
        Source investigation for ThemeDownloader with multiple callers.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            runner = SourceInvestigationRunner(config)

            question = "主题下载失败是什么原因"
            hits = [_hit(question, signal="ThemeDownloader.download")]

            with mock.patch.object(runner, "_get_codegraph") as mock_cg:
                from lark_agent_bridge.knowledge.codegraph_client import (
                    CgCallerHit,
                    CgSymbolHit,
                    CodeGraphClient,
                )
                client = mock.MagicMock(spec=CodeGraphClient)
                client.is_indexed.return_value = True
                client.search_symbol.return_value = [
                    CgSymbolHit(name="ThemeDownloader", kind="class",
                                qualified_name="ThemeDownloader", path="src/ThemeDownloader.kt",
                                line=20, language="Kotlin", score=110.0),
                    CgSymbolHit(name="download", kind="method",
                                qualified_name="ThemeDownloader.download", path="src/ThemeDownloader.kt",
                                line=55, language="Kotlin", score=95.0),
                    CgSymbolHit(name="retryDownload", kind="method",
                                qualified_name="ThemeDownloader.retryDownload", path="src/ThemeDownloader.kt",
                                line=80, language="Kotlin", score=85.0),
                ]
                client.get_callers.return_value = [
                    CgCallerHit(name="ThemeManager.applyTheme", kind="method",
                                path="src/ThemeManager.kt", line=100),
                    CgCallerHit(name="SettingsActivity.onThemeSelected", kind="method",
                                path="ui/SettingsActivity.kt", line=230),
                    CgCallerHit(name="ThemePreviewPresenter.loadPreview", kind="method",
                                path="presenter/ThemePreviewPresenter.kt", line=45),
                    CgCallerHit(name="BootReceiver.initTheme", kind="method",
                                path="receiver/BootReceiver.kt", line=18),
                    CgCallerHit(name="ThemeTestHelper.setupMock", kind="method",
                                path="test/ThemeTestHelper.kt", line=55),
                ]
                mock_cg.return_value = client

                result = runner._try_codegraph(
                    question,
                    hits=[hits[0]],
                    repo_roots=[Path(tmp) / "guideengine"],
                )
                assert result is not None
                assert result.success is True
                assert result.confidence >= 0.7  # many callers boost confidence
                assert len(result.source_evidence) > 0

    def test_bug_analysis_codegraph_unavailable_falls_to_code_index(self) -> None:
        """When codegraph is unavailable, fallback to ctags+rg for bug analysis."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp, codegraph=False)
            runner = SourceInvestigationRunner(config)

            question = "mock信号发送异常"
            hits = [_hit(question, signal="SignalSender.sendMock")]

            # codegraph is disabled, code_index returns None (no ctags)
            with mock.patch.object(runner, "_get_code_index", return_value=None):
                result = runner._try_code_index(
                    question, hits=hits, repo_roots=[Path(tmp) / "guideengine"],
                )
                assert result is None  # graceful fallback

    def test_source_investigation_does_not_init_codegraph_in_request_path(self) -> None:
        """Unindexed repos are skipped in the request path instead of running init --index."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            runner = SourceInvestigationRunner(config)
            question = "DataCenter mock信号不触发"
            hits = [_hit(question, signal="DataCenter.mockSignal")]

            with mock.patch.object(runner, "_get_codegraph") as mock_cg:
                from lark_agent_bridge.knowledge.codegraph_client import CodeGraphClient

                client = mock.MagicMock(spec=CodeGraphClient)
                client.is_indexed.return_value = False
                mock_cg.return_value = client

                result = runner._try_codegraph(
                    question,
                    hits=hits,
                    repo_roots=[Path(tmp) / "guideengine"],
                )

            assert result is None
            client.ensure_index.assert_not_called()
            client.search_symbol.assert_not_called()

    def test_bug_analysis_low_confidence_enriches_prompt(self) -> None:
        """Low confidence codegraph result caches context for prompt enrichment."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            config.source_investigation.codegraph_min_confidence = 0.99  # force low confidence
            runner = SourceInvestigationRunner(config)

            question = "WallpaperService setWallpaper 参数含义"
            hits = [_hit(question, signal="WallpaperService.setWallpaper")]

            with mock.patch.object(runner, "_get_codegraph") as mock_cg:
                from lark_agent_bridge.knowledge.codegraph_client import (
                    CgSymbolHit,
                    CodeGraphClient,
                )
                client = mock.MagicMock(spec=CodeGraphClient)
                client.is_indexed.return_value = True
                client.search_symbol.return_value = [
                    CgSymbolHit(name="setWallpaper", kind="method",
                                qualified_name="WallpaperService.setWallpaper",
                                path="src/WallpaperService.kt",
                                line=42, language="Kotlin", score=100.0),
                ]
                client.get_callers.return_value = []
                mock_cg.return_value = client

                result = runner._try_codegraph(
                    question,
                    hits=[hits[0]],
                    repo_roots=[Path(tmp) / "guideengine"],
                )
                # Low confidence → returns None but caches context
                assert result is None
                assert runner._last_code_index_context is not None
                assert len(runner._last_code_index_context) > 0


# ===========================================================================
# Category 2: Source investigation follow-up conversations (3+ scenarios)
# ===========================================================================

class TestSourceInvestigationFollowUp:
    """Simulate follow-up questions after initial source investigation."""

    def test_followup_deeper_symbol_query(self) -> None:
        """User asks a follow-up diving deeper into a specific method."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            runner = SourceInvestigationRunner(config)

            question = "DataCenter.mockSignal 的具体实现逻辑是什么"
            hits = [_hit(question, signal="DataCenter.mockSignal")]

            with mock.patch.object(runner, "_get_codegraph") as mock_cg:
                from lark_agent_bridge.knowledge.codegraph_client import (
                    CgCallerHit,
                    CgSymbolHit,
                    CodeGraphClient,
                )
                client = mock.MagicMock(spec=CodeGraphClient)
                client.is_indexed.return_value = True
                client.search_symbol.return_value = [
                    CgSymbolHit(name="mockSignal", kind="method",
                                qualified_name="DataCenter.mockSignal",
                                path="src/DataCenter.kt", line=45,
                                language="Kotlin", score=110.0),
                ]
                client.get_callers.return_value = [
                    CgCallerHit(name="handleBroadcast", kind="method",
                                path="src/Receiver.kt", line=67),
                ]
                mock_cg.return_value = client

                result = runner._try_codegraph(
                    question, hits=[hits[0]],
                    repo_roots=[Path(tmp) / "guideengine"],
                )
                assert result is not None
                assert result.success is True
                assert "mockSignal" in result.answer

    def test_followup_caller_chain_question(self) -> None:
        """User asks 'who calls this method?' after seeing initial analysis."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            runner = SourceInvestigationRunner(config)

            question = "谁调用了 SignalManager.onSignalChanged"
            hits = [_hit(question, signal="SignalManager.onSignalChanged")]

            with mock.patch.object(runner, "_get_codegraph") as mock_cg:
                from lark_agent_bridge.knowledge.codegraph_client import (
                    CgCallerHit,
                    CgSymbolHit,
                    CodeGraphClient,
                )
                client = mock.MagicMock(spec=CodeGraphClient)
                client.is_indexed.return_value = True
                client.search_symbol.return_value = [
                    CgSymbolHit(name="onSignalChanged", kind="method",
                                qualified_name="SignalManager.onSignalChanged",
                                path="src/SignalManager.kt", line=120,
                                language="Kotlin", score=100.0),
                ]
                client.get_callers.return_value = [
                    CgCallerHit(name="DataCenter.updateSignal", kind="method",
                                path="src/DataCenter.kt", line=90),
                    CgCallerHit(name="MockSignalReceiver.onReceive", kind="method",
                                path="src/MockSignalReceiver.kt", line=30),
                    CgCallerHit(name="SignalTest.testUpdate", kind="method",
                                path="test/SignalTest.kt", line=15),
                ]
                mock_cg.return_value = client

                result = runner._try_codegraph(
                    question, hits=[hits[0]],
                    repo_roots=[Path(tmp) / "guideengine"],
                )
                assert result is not None
                assert result.success is True
                assert result.confidence >= 0.5
                # Evidence should contain caller references
                caller_evidence = [e for e in result.source_evidence if "caller" in e.get("text", "")]
                assert len(caller_evidence) >= 1

    def test_followup_cross_module_question(self) -> None:
        """User asks about interaction between two modules."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            runner = SourceInvestigationRunner(config)

            question = "ThemeManager 和 DataCenter 之间的关系"
            hits = [_hit(question)]

            with mock.patch.object(runner, "_get_codegraph") as mock_cg:
                from lark_agent_bridge.knowledge.codegraph_client import (
                    CgCallerHit,
                    CgSymbolHit,
                    CodeGraphClient,
                )
                client = mock.MagicMock(spec=CodeGraphClient)
                client.is_indexed.return_value = True
                # Multiple symbols found
                client.search_symbol.side_effect = [
                    [CgSymbolHit(name="ThemeManager", kind="class",
                                 qualified_name="ThemeManager",
                                 path="src/ThemeManager.kt", line=10,
                                 language="Kotlin", score=100.0)],
                    [CgSymbolHit(name="DataCenter", kind="class",
                                 qualified_name="DataCenter",
                                 path="src/DataCenter.kt", line=5,
                                 language="Kotlin", score=100.0)],
                ]
                client.get_callers.side_effect = [
                    [CgCallerHit(name="DataCenter.getThemeManager", kind="method",
                                 path="src/DataCenter.kt", line=150)],
                    [],
                ]
                mock_cg.return_value = client

                result = runner._try_codegraph(
                    question, hits=[hits[0]],
                    repo_roots=[Path(tmp) / "guideengine"],
                )
                assert result is not None
                assert result.success is True

    def test_followup_disabled_both_returns_none(self) -> None:
        """Both codegraph and code_index disabled → graceful None."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp, codegraph=False, code_index=False)
            runner = SourceInvestigationRunner(config)

            question = "mockSignal 的调用链"
            hits = [_hit(question, signal="DataCenter.mockSignal")]

            # Should not crash, just return None from both paths
            cg_result = runner._try_codegraph(
                question, hits=hits, repo_roots=[Path(tmp) / "guideengine"],
            )
            assert cg_result is None

            ci_result = runner._try_code_index(
                question, hits=hits, repo_roots=[Path(tmp) / "guideengine"],
            )
            assert ci_result is None


# ===========================================================================
# Category 3: Knowledge-base source-level Q&As (3+ scenarios)
# ===========================================================================

class TestKnowledgeBaseSourceQA:
    """Simulate knowledge-base source-level questions."""

    def test_kb_what_does_class_do(self) -> None:
        """User asks '某个类是做什么的' from knowledge-base context."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            config.source_investigation.codegraph_min_confidence = 0.3  # class-only query
            runner = SourceInvestigationRunner(config)

            question = "LauncherThemeController 是做什么的"
            hits = [_hit(question)]

            with mock.patch.object(runner, "_get_codegraph") as mock_cg:
                from lark_agent_bridge.knowledge.codegraph_client import (
                    CgSymbolHit,
                    CodeGraphClient,
                )
                client = mock.MagicMock(spec=CodeGraphClient)
                client.is_indexed.return_value = True
                client.search_symbol.return_value = [
                    CgSymbolHit(name="LauncherThemeController", kind="class",
                                qualified_name="LauncherThemeController",
                                path="controller/LauncherThemeController.kt",
                                line=25, language="Kotlin", score=130.0),
                ]
                client.get_callers.return_value = []
                mock_cg.return_value = client

                result = runner._try_codegraph(
                    question, hits=[hits[0]],
                    repo_roots=[Path(tmp) / "guideengine"],
                )
                # Even with no callers, class definition gives base confidence
                assert result is not None
                assert result.success is True
                assert "LauncherThemeController" in result.answer

    def test_kb_how_is_feature_implemented(self) -> None:
        """User asks '这个功能是怎么实现的' — a broad implementation question."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            runner = SourceInvestigationRunner(config)

            question = "壁纸预览功能是怎么实现的"
            hits = [_hit(question, signal="WallpaperPreview")]

            with mock.patch.object(runner, "_get_codegraph") as mock_cg:
                from lark_agent_bridge.knowledge.codegraph_client import (
                    CgCallerHit,
                    CgSymbolHit,
                    CodeGraphClient,
                )
                client = mock.MagicMock(spec=CodeGraphClient)
                client.is_indexed.return_value = True
                client.search_symbol.return_value = [
                    CgSymbolHit(name="WallpaperPreview", kind="class",
                                qualified_name="WallpaperPreview",
                                path="ui/WallpaperPreview.kt",
                                line=10, language="Kotlin", score=100.0),
                    CgSymbolHit(name="showPreview", kind="method",
                                qualified_name="WallpaperPreview.showPreview",
                                path="ui/WallpaperPreview.kt",
                                line=45, language="Kotlin", score=90.0),
                ]
                client.get_callers.return_value = [
                    CgCallerHit(name="WallpaperPicker.onItemClick", kind="method",
                                path="ui/WallpaperPicker.kt", line=78),
                    CgCallerHit(name="PreviewTransition.start", kind="method",
                                path="anim/PreviewTransition.kt", line=22),
                ]
                mock_cg.return_value = client

                result = runner._try_codegraph(
                    question, hits=[hits[0]],
                    repo_roots=[Path(tmp) / "guideengine"],
                )
                assert result is not None
                assert result.success is True
                assert result.confidence >= 0.5

    def test_kb_where_is_config_defined(self) -> None:
        """User asks '配置在哪里定义的' — file location question."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            config.source_investigation.codegraph_min_confidence = 0.3  # single variable hit
            runner = SourceInvestigationRunner(config)

            question = "SourceInvestigationOptions 配置在哪里定义的"
            hits = [_hit(question)]

            with mock.patch.object(runner, "_get_codegraph") as mock_cg:
                from lark_agent_bridge.knowledge.codegraph_client import (
                    CgSymbolHit,
                    CodeGraphClient,
                )
                client = mock.MagicMock(spec=CodeGraphClient)
                client.is_indexed.return_value = True
                client.search_symbol.return_value = [
                    CgSymbolHit(name="SourceInvestigationOptions", kind="class",
                                qualified_name="SourceInvestigationOptions",
                                path="lark_agent_bridge/models.py",
                                line=251, language="Python", score=95.0),
                ]
                client.get_callers.return_value = []
                mock_cg.return_value = client

                result = runner._try_codegraph(
                    question, hits=[hits[0]],
                    repo_roots=[Path(tmp) / "guideengine"],
                )
                assert result is not None
                assert "SourceInvestigationOptions" in result.answer
                assert "models.py" in result.answer

    def test_kb_no_symbol_extracted_returns_none(self) -> None:
        """When no symbols can be extracted, gracefully returns None."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            runner = SourceInvestigationRunner(config)

            question = "系统设置页面怎么打开"
            hits = [_hit(question)]

            result = runner._try_codegraph(
                question, hits=[hits[0]],
                repo_roots=[Path(tmp) / "guideengine"],
            )
            # No recognizable code symbols → None
            assert result is None


# ===========================================================================
# Unit tests for helper functions
# ===========================================================================

class TestHelperFunctions:
    def test_codegraph_confidence_empty(self) -> None:
        assert _codegraph_confidence([], [], "q", []) == 0.0

    def test_codegraph_confidence_base(self) -> None:
        from lark_agent_bridge.knowledge.codegraph_client import CgContext
        ctx = CgContext(summary="test", entry_points=[{"name": "A", "kind": "class"}])
        conf = _codegraph_confidence(
            [(Path("/tmp"), ctx)], [], "q", [],
        )
        assert conf >= 0.35

    def test_codegraph_confidence_with_callers(self) -> None:
        from lark_agent_bridge.knowledge.codegraph_client import CgCallerHit, CgContext
        ctx = CgContext(summary="test", entry_points=[
            {"name": "A", "kind": "class"},
            {"name": "B", "kind": "method"},
        ])
        callers = [
            CgCallerHit(name="c1", kind="m", path="f.kt", line=1),
            CgCallerHit(name="c2", kind="m", path="g.kt", line=2),
        ]
        conf = _codegraph_confidence(
            [(Path("/tmp"), ctx)],
            [(Path("/tmp"), "A", callers)],
            "q", [],
        )
        assert conf >= 0.7

    def test_result_from_codegraph_produces_evidence(self) -> None:
        from lark_agent_bridge.knowledge.codegraph_client import CgContext
        ctx = CgContext(summary="test", entry_points=[
            {"name": "Foo", "kind": "class", "qualifiedName": "Foo",
             "filePath": "Foo.kt", "startLine": 10},
        ])
        result = _result_from_codegraph(
            [(Path("/tmp"), ctx)], [], "q", [], confidence=0.8,
        )
        assert result.success is True
        assert result.confidence == 0.8
        assert len(result.source_evidence) >= 1

    def test_codegraph_to_code_index_context_converts(self) -> None:
        from lark_agent_bridge.knowledge.codegraph_client import CgContext, CodeGraphClient
        ctx = CgContext(summary="test", entry_points=[
            {"name": "Bar", "kind": "function", "qualifiedName": "ns::Bar",
             "filePath": "bar.py", "startLine": 5},
        ])
        cg = mock.MagicMock(spec=CodeGraphClient)
        result = _codegraph_to_code_index_context(ctx, cg, Path("/tmp"))
        assert hasattr(result, "definitions")
        assert len(result.definitions) == 1
        assert result.definitions[0].name == "Bar"
