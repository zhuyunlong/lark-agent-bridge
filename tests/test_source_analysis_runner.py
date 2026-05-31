import tempfile
import unittest
from pathlib import Path

from lark_agent_bridge.knowledge.source_investigation import SourceInvestigationResult
from lark_agent_bridge.models import SourceAnalysisRequest
from lark_agent_bridge.source_analysis import RepositorySourceAnalysisRunner

from tests._app_base import BridgeConfig, event


class FakeSourceInvestigationRunner:
    def __init__(self, result):
        self.result = result
        self.questions = []

    def run(self, question, *, hits=None):
        self.questions.append({"question": question, "hits": hits})
        return self.result


class SourceAnalysisRunnerTests(unittest.TestCase):
    def test_successful_source_result_creates_html_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_result = SourceInvestigationResult(
                success=True,
                answer="UnityReady 通过 Flow 分发。",
                canonical_key="UnityReady",
                confidence=0.9,
                source_evidence=[
                    {"file": "UnityHmiService.kt", "line": 42, "text": "getSignalFlow(SIGNAL_X3D_UNITY_READY)"}
                ],
                coverage_boundary="只读源码仓。",
                command=["source-runner"],
            )
            fake_source = FakeSourceInvestigationRunner(source_result)
            runner = RepositorySourceAnalysisRunner(
                BridgeConfig(dry_run=False, data_dir=Path(tmp)),
                source_runner=fake_source,
            )
            request = SourceAnalysisRequest(
                prompt="基于源码分析 UnityReady 信号链路如何监听",
                target="UnityReady",
                raw_text="@bot 基于源码分析 UnityReady 信号链路如何监听",
                triggered=True,
                diagram_kinds=["swimlane"],
            )

            result = runner.run(request, event(event_id="evt_source_runner"))

            self.assertTrue(result.success)
            self.assertEqual(result.details["mode"], "source_analysis")
            self.assertEqual(result.details["source_mode"], "repository_only")
            self.assertEqual(result.details["source_execution_backend"], "source_investigation")
            self.assertEqual(len(result.details["files_to_send"]), 1)
            html_path = Path(result.details["files_to_send"][0])
            self.assertTrue(html_path.is_file())
            self.assertIn("UnityReady", html_path.read_text(encoding="utf-8"))
            self.assertEqual(fake_source.questions[0]["question"], request.prompt)

    def test_failed_source_backend_returns_failure_without_fake_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_result = SourceInvestigationResult(
                success=False,
                error="source investigation disabled",
                command=["source-runner"],
            )
            runner = RepositorySourceAnalysisRunner(
                BridgeConfig(dry_run=False, data_dir=Path(tmp)),
                source_runner=FakeSourceInvestigationRunner(source_result),
            )
            request = SourceAnalysisRequest(
                prompt="基于源码分析 Foo",
                target="Foo",
                raw_text="@bot 基于源码分析 Foo",
                triggered=True,
            )

            result = runner.run(request, event(event_id="evt_source_runner_failed"))

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "source_analysis_failed")
        self.assertNotIn("files_to_send", result.details)


if __name__ == "__main__":
    unittest.main()
