import tempfile
import unittest
from pathlib import Path

from lark_agent_bridge.knowledge.source_investigation import SourceInvestigationResult
from lark_agent_bridge.models import CodexAppServerOptions, SourceAnalysisRequest
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

    def test_app_server_source_analysis_rewrites_bug_style_html_to_generic_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            analysis_dir = Path(tmp) / "analysis"
            analysis_dir.mkdir(parents=True, exist_ok=True)
            analysis_path = analysis_dir / "source_stage_analysis.md"
            analysis_path.write_text(
                "\n".join(
                    [
                        "## 结论摘要",
                        "",
                        "- 当前车道级信号分成 guideengine 与 Napa5 两条主链。",
                        "",
                        "| 泳道 | 时序动作 | 源码锚点 |",
                        "|---|---|---|",
                        "| Unity / LD | 上报 LD 中心点、LD 场景 | `SetLdTileCenterMsg.sendMsgData` |",
                        "| XData Transport | 分发 Unity / Native 信号 | `XDataTransport.onSignalData` |",
                        "",
                        "## 关键证据",
                        "",
                        "- **Unity 入口**：`sendMsgToAndroid(...)` 负责把 LD 场景发到 Android。 来源：`/tmp/SetLdTileCenterMsg.java:17`",
                    ]
                ),
                encoding="utf-8",
            )

            class FakeBugRunner:
                def _run_custom_skill_agent_analysis(self, **kwargs):
                    html_path = Path(kwargs["html_path"])
                    html_path.parent.mkdir(parents=True, exist_ok=True)
                    html_path.write_text("<html><body><h1>Bug 标题：旧报告</h1></body></html>", encoding="utf-8")
                    Path(kwargs["json_path"]).write_text("{}", encoding="utf-8")
                    return {
                        "ok": True,
                        "executor": "codex_app_server",
                        "provider": "codex",
                        "analysis_markdown_path": analysis_path,
                        "html_path": html_path,
                        "json_path": Path(kwargs["json_path"]),
                        "command": ["codex", "app-server"],
                        "stdout": "",
                        "stderr": "",
                        "thread_id": "thread-src",
                        "turn_id": "turn-src",
                        "app_server_version": "0.135.0",
                    }

            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                codex_app_server=CodexAppServerOptions(enabled=True, use_for_file_agent=True),
            )
            runner = RepositorySourceAnalysisRunner(
                config,
                source_runner=FakeSourceInvestigationRunner(None),
                bug_runner=FakeBugRunner(),
            )
            request = SourceAnalysisRequest(
                prompt="基于源码分析车道级相关信号",
                target="车道级相关信号",
                raw_text="@bot 基于源码分析车道级相关信号",
                triggered=True,
                diagram_kinds=["swimlane"],
            )

            result = runner.run(request, event(event_id="evt_source_app_server"))

            self.assertTrue(result.success)
            html_path = Path(result.html_report)
            html = html_path.read_text(encoding="utf-8")
            self.assertIn('class="swimlane-svg-wrap"', html)
            self.assertIn("车道级相关信号", html)
            self.assertNotIn("Bug 标题", html)
            self.assertNotIn("执行器", html)
            self.assertEqual(result.details["mode"], "source_analysis")
            self.assertEqual(result.details["source_execution_backend"], "codex_app_server")


if __name__ == "__main__":
    unittest.main()
