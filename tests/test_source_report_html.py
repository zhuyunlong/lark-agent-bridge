import unittest

from lark_agent_bridge.reporting.source_report_html import (
    render_context_diagram_report,
    render_source_analysis_report,
)


class SourceReportHtmlTests(unittest.TestCase):
    def test_source_analysis_report_contains_swimlane_evidence_and_verdict(self):
        html = render_source_analysis_report(
            title="UnityReady 源码分析",
            request_text="基于源码分析 UnityReady 信号链路如何监听",
            answer="UnityReady 通过 getSignalFlow 分发。",
            target="UnityReady",
            source_evidence=[
                {
                    "file": "UnityHmiService.kt",
                    "line": 42,
                    "text": "fun getSignalFlow(code: Int)",
                }
            ],
            coverage_boundary="只检查源码仓。",
            diagram_kinds=["swimlane", "sequence"],
            backend="source_investigation",
            success=True,
        )

        self.assertIn("结论摘要", html)
        self.assertIn('class="verdict-title"', html)
        self.assertIn('class="report-meta"', html)
        self.assertNotIn('class="cards"', html)
        self.assertIn("泳道图", html)
        self.assertIn("证据", html)
        self.assertIn("UnityHmiService.kt", html)

    def test_source_analysis_report_escapes_user_text(self):
        html = render_source_analysis_report(
            title="<script>alert(1)</script>",
            request_text="<b>bad</b>",
            answer="<img src=x onerror=alert(1)>",
            target="<UnityReady>",
            source_evidence=[],
            coverage_boundary="",
            diagram_kinds=["swimlane"],
            backend="test",
            success=True,
        )

        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<b>bad</b>", html)
        self.assertNotIn("<img src=x onerror=alert(1)>", html)
        self.assertIn("&lt;UnityReady&gt;", html)

    def test_source_analysis_report_handles_empty_evidence_as_bounded_report(self):
        html = render_source_analysis_report(
            title="源码分析",
            request_text="基于源码分析 Foo",
            answer="未拿到可验证源码证据。",
            target="Foo",
            source_evidence=[],
            coverage_boundary="源码检索未命中。",
            diagram_kinds=[],
            backend="source_investigation",
            success=True,
        )

        self.assertIn("证据不足", html)
        self.assertIn("源码检索未命中", html)

    def test_context_diagram_report_contains_context_summary(self):
        html = render_context_diagram_report(
            title="追问泳道图",
            request_text="原始请求",
            followup_text="画出泳道图",
            summary_text="原始分析摘要",
            report_excerpt="报告摘录",
            history=[{"user": "上一问", "assistant": "上一答"}],
            diagram_kinds=["swimlane"],
            source_mode="repository_only",
            context_profile="source_analysis",
        )

        self.assertIn("泳道图", html)
        self.assertIn('class="verdict-title"', html)
        self.assertIn('class="report-meta"', html)
        self.assertIn("原始分析摘要", html)
        self.assertIn("报告摘录", html)

    def test_source_analysis_report_renders_svg_swimlane_from_markdown_answer(self):
        html = render_source_analysis_report(
            title="车道级信号 源码分析",
            request_text="基于源码解释车道级相关信号",
            answer="""
## 结论摘要

- 当前车道级信号分成 guideengine 与 Napa5 两条主链。
- 本轮只有源码，没有日志，运行态触发情况待确认。

| 泳道 | 时序动作 | 源码锚点 |
|---|---|---|
| Unity / LD | 上报 LD 中心点、LD 场景 | `SetLdTileCenterMsg.sendMsgData`、`LDSceneMsg.sendMsgData` |
| XData Transport | 分发 Unity / Native 信号 | `XDataTransport.onSignalData` |
| Napa5 渲染 | 消费变道和红毯事件 | `SRMarks.OnLaneChanged`、`SRLayerAEB.OnFrontLaneWarning` |

## 关键证据

- **Unity 入口**：`sendMsgToAndroid(...)` 负责把 LD 场景发到 Android。 来源：`/tmp/SetLdTileCenterMsg.java:17`、`:23`
""",
            target="车道级相关信号",
            source_evidence=[],
            coverage_boundary="只检查源码仓。",
            diagram_kinds=["swimlane"],
            backend="codex_app_server",
            success=True,
        )

        self.assertIn('class="swimlane-svg-wrap"', html)
        self.assertIn("<svg", html)
        self.assertIn("guideengine 与 Napa5 两条主链", html)
        self.assertIn("SetLdTileCenterMsg.sendMsgData", html)
        self.assertIn("SetLdTileCenterMsg.java:17", html)
        self.assertNotIn("lane-grid", html)

    def test_source_analysis_report_renders_pending_as_dedicated_section(self):
        html = render_source_analysis_report(
            title="待确认项独立成节",
            request_text="基于源码分析 Foo",
            answer=(
                "## 结论摘要\n- 初步定位在 Foo 分发链。\n\n"
                "## 待确认项\n"
                "- 运行态是否真正触发该分支待确认。\n"
                "- 缺少 Bar 的调用栈证据。\n"
            ),
            target="Foo",
            source_evidence=[{"file": "Foo.kt", "line": "10", "text": "hit"}],
            coverage_boundary="只检查源码仓。",
            diagram_kinds=[],
            backend="source_investigation",
            success=True,
        )

        # 待确认项必须独立成节，且承载 markdown 中的待确认内容
        self.assertIn("待确认项", html)
        self.assertIn("运行态是否真正触发该分支待确认", html)
        # 边界与说明只保留 coverage_boundary，不再吞掉待确认项内容
        boundary_block = html.split("边界与说明", 1)[1].split("待确认项", 1)[0]
        self.assertIn("只检查源码仓", boundary_block)
        self.assertNotIn("运行态是否真正触发该分支待确认", boundary_block)


    def test_empty_evidence_not_green(self):
        html = render_source_analysis_report(
            title="t", request_text="r", answer="（无结构化结论）", target="X",
            source_evidence=[], coverage_boundary="", diagram_kinds=[], backend="x", success=True,
        )
        # verdict banner must not be green when there is no evidence
        body = html.split("</style>")[-1]
        self.assertNotIn("v-green", body)

    def test_with_evidence_can_be_green(self):
        html = render_source_analysis_report(
            title="t", request_text="r", answer="## 结论摘要\n- ok", target="X",
            source_evidence=[{"file": "A.kt", "line": "10", "text": "hit"}],
            coverage_boundary="", diagram_kinds=[], backend="x", success=True,
        )
        # verdict banner must be green when there is evidence and success=True
        body = html.split("</style>")[-1]
        self.assertIn("v-green", body)


if __name__ == "__main__":
    unittest.main()
