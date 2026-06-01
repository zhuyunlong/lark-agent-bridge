import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from lark_agent_bridge.models import (
    BridgeConfig,
    RequirementAnalysisRequest,
    RequirementFact,
    RequirementSourceComparison,
    RequirementWorkItemRef,
    RequirementWorkItemSnapshot,
    TaskResult,
)
from lark_agent_bridge.requirement_analysis import (
    MeegleWorkItemClient,
    RequirementAnalysisRunner,
    build_requirement_source_prompt,
    extract_requirement_facts,
    parse_requirement_source_comparison,
)
from tests._app_base import event


class RequirementModelTests(unittest.TestCase):
    def test_request_and_comparison_models_are_generic(self):
        ref = RequirementWorkItemRef(
            url="https://project.feishu.cn/demo/story/detail/12345",
            project_key="demo",
            work_item_type="story",
            work_item_id="12345",
        )
        fact = RequirementFact(
            fact_id="REQ-1",
            text="支持某功能的进入条件",
            source_field="description",
        )
        request = RequirementAnalysisRequest(
            prompt="结合源码分析是否可行",
            workitem=ref,
            raw_text="@bot https://project.feishu.cn/demo/story/detail/12345 结合源码分析是否可行",
            triggered=True,
        )
        comparison = RequirementSourceComparison(
            verdict="insufficient_evidence",
            summary="需求描述不足，当前源码证据不能证明。",
            matched_items=[],
            gap_items=[],
            unknown_items=[fact],
            architecture_impact="unknown",
            architecture_impact_reason="源码证据不足。",
            source_evidence=[],
            diagram_notes=[],
            parse_warnings=[],
        )

        self.assertEqual(request.source_mode, "requirement_source")
        self.assertEqual(request.workitem.work_item_type, "story")
        self.assertEqual(comparison.verdict, "insufficient_evidence")
        self.assertEqual(comparison.unknown_items[0].fact_id, "REQ-1")


class FakeCommandRunner:
    def __init__(self, stdout_by_prefix):
        self.stdout_by_prefix = stdout_by_prefix
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        key = tuple(command[:3])
        stdout = self.stdout_by_prefix[key]
        return subprocess.CompletedProcess(command, 0, stdout, "")


class RequirementFetchTests(unittest.TestCase):
    def test_fetch_snapshot_and_extracts_generic_facts(self):
        ref = RequirementWorkItemRef(
            url="https://project.feishu.cn/demo/story/detail/12345",
            project_key="demo",
            work_item_type="story",
            work_item_id="12345",
        )
        runner = FakeCommandRunner(
            {
                ("meegle", "workitem", "get"): json.dumps(
                    {
                        "work_item_name": "通用功能需求",
                        "work_item_status": {"name": "设计中"},
                        "work_item_type": {"name": "需求管理"},
                        "description": {"text": "进入条件：满足 A。\n退出条件：满足 B。\n风险：依赖下游服务。"},
                        "wiki": "https://xiaopeng.feishu.cn/wiki/example",
                    },
                    ensure_ascii=False,
                ),
                ("meegle", "comment", "list"): json.dumps({"items": [], "total": 0}, ensure_ascii=False),
            }
        )
        client = MeegleWorkItemClient(command_runner=runner, fetch_comments=True)

        snapshot = client.fetch(ref, job_dir=Path(tempfile.mkdtemp()))

        self.assertEqual(snapshot.title, "通用功能需求")
        self.assertEqual(snapshot.status, "设计中")
        self.assertEqual(snapshot.comments_count, 0)
        self.assertGreaterEqual(len(snapshot.facts), 3)
        self.assertEqual(snapshot.facts[0].fact_id, "REQ-1")
        self.assertIn("进入条件", snapshot.facts[0].text)


class RequirementFactExtractionTests(unittest.TestCase):
    def test_extract_facts_is_not_business_keyword_specific(self):
        facts = extract_requirement_facts(
            "第一条：支持新入口。\n第二条：异常时保持旧链路。\n第三条：输出 HTML 报告。",
            max_fact_count=10,
        )

        self.assertEqual([fact.fact_id for fact in facts], ["REQ-1", "REQ-2", "REQ-3"])
        self.assertIn("支持新入口", facts[0].text)
        self.assertIn("HTML 报告", facts[2].text)


class RequirementSourcePromptTests(unittest.TestCase):
    def test_prompt_contains_constraints_and_no_special_case_terms(self):
        ref = RequirementWorkItemRef("https://project.feishu.cn/demo/story/detail/12345", "demo", "story", "12345")
        snapshot = RequirementWorkItemSnapshot(
            ref=ref,
            title="通用功能需求",
            status="设计中",
            description="支持新入口。\n异常时保持旧链路。",
            facts=[
                RequirementFact("REQ-1", "支持新入口", "description"),
                RequirementFact("REQ-2", "异常时保持旧链路", "description"),
            ],
        )

        prompt = build_requirement_source_prompt("结合源码分析是否可行", snapshot)

        self.assertIn("不要猜测", prompt)
        self.assertIn("verdict", prompt)
        self.assertIn("matched_items", prompt)
        self.assertIn("unknown_items", prompt)
        self.assertIn("REQ-1", prompt)
        self.assertNotIn("闸机", prompt)

    def test_parse_structured_json_block(self):
        answer = """
结论摘要。
```json
{
  "verdict": "partially_implemented",
  "summary": "部分需求已有源码证据。",
  "matched_items": [{"fact_id": "REQ-1", "text": "支持新入口", "source_field": "description"}],
  "gap_items": [],
  "unknown_items": [{"fact_id": "REQ-2", "text": "异常时保持旧链路", "source_field": "description"}],
  "architecture_impact": "medium",
  "architecture_impact_reason": "涉及路由层。",
  "source_evidence": [{"file": "router.py", "line": 10, "symbol": "route", "text": "route()"}],
  "diagram_notes": ["用户 -> 路由 -> 执行器"]
}
```
"""

        comparison = parse_requirement_source_comparison(answer)

        self.assertEqual(comparison.verdict, "partially_implemented")
        self.assertEqual(comparison.matched_items[0].fact_id, "REQ-1")
        self.assertEqual(comparison.unknown_items[0].fact_id, "REQ-2")
        self.assertEqual(comparison.source_evidence[0]["file"], "router.py")


class FakeRequirementClient:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def fetch(self, ref, *, job_dir, max_fact_count=30):
        return self.snapshot


class FakeSourceRunner:
    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def run(self, request, event=None, *, progress_callback=None):
        self.requests.append(request)
        return TaskResult(
            success=True,
            message="源码分析完成",
            details={
                "mode": "source_analysis",
                "source_execution_backend": "source_investigation",
                "source_answer": self.answer,
            },
        )


class RequirementAnalysisRunnerTests(unittest.TestCase):
    def test_runner_outputs_partial_result_without_special_case_assumptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref = RequirementWorkItemRef("https://project.feishu.cn/demo/story/detail/12345", "demo", "story", "12345")
            snapshot = RequirementWorkItemSnapshot(
                ref=ref,
                title="通用功能需求",
                status="设计中",
                facts=[
                    RequirementFact("REQ-1", "支持新入口", "description"),
                    RequirementFact("REQ-2", "异常时保持旧链路", "description"),
                ],
            )
            answer = """
部分需求已有证据。
```json
{
  "verdict": "partially_implemented",
  "summary": "部分需求已有源码证据。",
  "matched_items": [{"fact_id": "REQ-1", "text": "支持新入口", "source_field": "description"}],
  "gap_items": [],
  "unknown_items": [{"fact_id": "REQ-2", "text": "异常时保持旧链路", "source_field": "description"}],
  "architecture_impact": "medium",
  "architecture_impact_reason": "涉及路由层。",
  "source_evidence": [{"file": "router.py", "line": 10, "symbol": "route", "text": "route()"}],
  "diagram_notes": ["用户 -> 路由 -> 执行器"]
}
```
"""
            source_runner = FakeSourceRunner(answer)
            runner = RequirementAnalysisRunner(
                BridgeConfig(dry_run=False, data_dir=Path(tmp)),
                workitem_client=FakeRequirementClient(snapshot),
                source_analysis_runner=source_runner,
            )
            request = RequirementAnalysisRequest(prompt="结合源码分析是否可行", workitem=ref, triggered=True)

            result = runner.run(request, event(event_id="evt_req_partial"))

            self.assertTrue(result.success)
            self.assertEqual(result.details["mode"], "requirement_analysis")
            self.assertEqual(result.details["requirement_verdict"], "partially_implemented")
            self.assertTrue(Path(result.html_report).is_file())
            self.assertIn("REQ-2", Path(result.html_report).read_text(encoding="utf-8"))
            self.assertIn("verdict", source_runner.requests[0].prompt)

    def test_runner_marks_missing_json_as_insufficient_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref = RequirementWorkItemRef("https://project.feishu.cn/demo/story/detail/12345", "demo", "story", "12345")
            snapshot = RequirementWorkItemSnapshot(ref=ref, title="模糊需求", facts=[])
            runner = RequirementAnalysisRunner(
                BridgeConfig(dry_run=False, data_dir=Path(tmp)),
                workitem_client=FakeRequirementClient(snapshot),
                source_analysis_runner=FakeSourceRunner("我没有找到足够证据。"),
            )
            request = RequirementAnalysisRequest(prompt="结合源码分析是否可行", workitem=ref, triggered=True)

            result = runner.run(request, event(event_id="evt_req_insufficient"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["requirement_verdict"], "insufficient_evidence")
        self.assertIn("source_comparison_json_missing", result.details["requirement_parse_warnings"])


if __name__ == "__main__":
    unittest.main()
