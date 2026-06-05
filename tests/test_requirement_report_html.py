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

        self.assertIn('class="verdict-title"', html)
        self.assertIn('class="report-meta"', html)
        self.assertNotIn('class="cards"', html)
        self.assertIn("通用功能需求", html)
        self.assertIn("需求事实矩阵", html)
        self.assertIn("问题与未确认项", html)
        self.assertIn("泳道图", html)
        self.assertIn("router.py", html)
        self.assertIn("REQ-2", html)
        self.assertNotIn("闸机", html)

    def test_report_promotes_agent_markdown_sections_before_requirement_details(self):
        ref = RequirementWorkItemRef("https://project.feishu.cn/demo/story/detail/12345", "demo", "story", "12345")
        snapshot = RequirementWorkItemSnapshot(
            ref=ref,
            title="通用功能需求",
            status="设计中",
            description="很长的需求描述",
            facts=[RequirementFact(f"REQ-{index}", f"事实 {index}", "description") for index in range(1, 31)],
        )
        comparison = RequirementSourceComparison(
            verdict="partially_implemented",
            summary="当前仓库只能证明上游链路部分具备，完整功能未闭环。",
            architecture_impact="medium",
            architecture_impact_reason="两条链路并行未打通。",
            source_evidence=[
                {
                    "file": "/workspace/app/decision.py",
                    "line": 111,
                    "symbol": "Decision.update",
                    "text": "现有触发链路不含新条件。",
                }
            ],
            unknown_items=[RequirementFact("待确认-1", "framework 下游是否会触发目标能力。", "agent_markdown")],
        )
        raw_answer = """
## 结论摘要

当前仓库只能证明上游链路部分具备，完整功能未闭环，属于 partially_implemented。

## 关键证据

1. **现有触发链路不含新条件**
   - 来源：`/workspace/app/decision.py:111`
   - 关键内容：`update_state()` 只读取旧条件。

## 最可能原因

1. **两条链路并行未打通**
   - 说明：上游信号没有进入下游状态机。

## 待确认项

- framework 下游是否会触发目标能力。

## 建议动作

1. 明确入口归属，再补齐状态机。
"""

        html = render_requirement_analysis_report(
            snapshot=snapshot,
            request_text="结合源码分析是否可行",
            raw_source_answer=raw_answer,
            comparison=comparison,
            backend="codex_app_server",
            warnings=["需求存在 wiki 链接；当前未读取 wiki 正文。"],
        )

        self.assertIn("当前仓库只能证明上游链路部分具备", html)
        self.assertIn("两条链路并行未打通", html)
        self.assertIn("现有触发链路不含新条件", html)
        self.assertIn("framework 下游是否会触发目标能力", html)
        self.assertIn("展开查看需求字段与事实矩阵", html)
        self.assertLess(html.index("结论摘要"), html.index("需求字段与事实矩阵"))
        self.assertLess(html.index("最可能原因"), html.index("需求字段与事实矩阵"))


if __name__ == "__main__":
    unittest.main()
