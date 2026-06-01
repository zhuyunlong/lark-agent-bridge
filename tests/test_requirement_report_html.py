import unittest

from lark_agent_bridge.models import (
    RequirementFact,
    RequirementSourceComparison,
    RequirementWorkItemRef,
    RequirementWorkItemSnapshot,
)
from lark_agent_bridge.reporting.requirement_report_html import render_requirement_analysis_report


class RequirementReportHtmlTests(unittest.TestCase):
    def test_report_renders_generic_statuses_and_unknowns(self):
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
        comparison = RequirementSourceComparison(
            verdict="partially_implemented",
            summary="部分需求已有源码证据。",
            matched_items=[snapshot.facts[0]],
            gap_items=[],
            unknown_items=[snapshot.facts[1]],
            architecture_impact="medium",
            architecture_impact_reason="涉及路由层。",
            source_evidence=[{"file": "router.py", "line": 10, "symbol": "route", "text": "route()"}],
            diagram_notes=["用户 -> 路由 -> 执行器"],
        )

        html = render_requirement_analysis_report(
            snapshot=snapshot,
            request_text="结合源码分析是否可行",
            raw_source_answer="原始源码分析输出",
            comparison=comparison,
            backend="source_investigation",
            warnings=["wiki 未读取"],
        )

        self.assertIn("一句话结论", html)
        self.assertIn("通用功能需求", html)
        self.assertIn("需求事实矩阵", html)
        self.assertIn("问题与未确认项", html)
        self.assertIn("泳道图", html)
        self.assertIn("router.py", html)
        self.assertIn("REQ-2", html)
        self.assertNotIn("闸机", html)


if __name__ == "__main__":
    unittest.main()
