"""Structured output models for pydantic-ai agent responses.

These models define the schemas that pydantic-ai agents must produce.
When pydantic is not installed, stub classes are provided so the module
can still be imported for type checking.
"""

from __future__ import annotations

from typing import Any

try:
    from pydantic import BaseModel, Field

    PYDANTIC_AVAILABLE = True
except ImportError:
    PYDANTIC_AVAILABLE = False

    class BaseModel:  # type: ignore[no-redef]
        """Stub BaseModel when pydantic is not installed."""

        def __init_subclass__(cls, **kwargs: Any) -> None:
            pass

        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

        def model_dump(self, **kwargs: Any) -> dict[str, Any]:
            return vars(self)

    def Field(**kwargs: Any) -> Any:  # type: ignore[no-redef]
        default = kwargs.get("default")
        if default is None and "default_factory" in kwargs:
            return kwargs["default_factory"]()
        return default


class SourceAnalysisOutput(BaseModel):
    """Structured output for source code analysis."""

    conclusion: str = Field(default="", description="结论摘要：一句话概述问题根因")
    root_cause: str = Field(default="", description="最可能原因：详细分析")
    evidence: list[dict[str, str]] = Field(
        default_factory=list,
        description="关键证据列表，每项含 file, line, content, relevance",
    )
    pending_items: list[str] = Field(
        default_factory=list,
        description="待确认项",
    )
    suggested_actions: list[str] = Field(
        default_factory=list,
        description="建议动作",
    )
    confidence: str = Field(default="medium", description="分析置信度: high/medium/low")
    tool_trace: list[dict[str, str]] = Field(
        default_factory=list,
        description="工具调用记录",
    )
    node_status: dict[str, str] = Field(
        default_factory=dict,
        description="源码文件名→ok/suspect/broken/unknown：标注链路节点是否打通",
    )
    findings: list[dict] = Field(
        default_factory=list,
        description="每项含 file/severity/title/kind",
    )

    def to_markdown(self) -> str:
        """Render as Markdown report."""
        parts = []
        if self.conclusion:
            parts.append(f"## 结论摘要\n\n{self.conclusion}")
        if self.root_cause:
            parts.append(f"## 最可能原因\n\n{self.root_cause}")
        if self.evidence:
            items = []
            for ev in self.evidence:
                file_ref = ev.get("file", "")
                line_ref = ev.get("line", "")
                content = ev.get("content", "")
                relevance = ev.get("relevance", "")
                loc = f"{file_ref}:{line_ref}" if line_ref else file_ref
                entry = f"- **{loc}**: {content}"
                if relevance:
                    entry += f" _{relevance}_"
                items.append(entry)
            parts.append("## 关键证据\n\n" + "\n".join(items))
        if self.pending_items:
            parts.append("## 待确认项\n\n" + "\n".join(f"- {item}" for item in self.pending_items))
        if self.suggested_actions:
            parts.append("## 建议动作\n\n" + "\n".join(f"- {action}" for action in self.suggested_actions))
        return "\n\n".join(parts) + "\n"


class LDLaneLevelOutput(BaseModel):
    """Structured output for LD lane-level analysis."""

    conclusion: str = Field(default="", description="结论摘要：一句话概述 LD 车道级问题根因")
    root_cause: str = Field(default="", description="最可能原因：详细分析")
    evidence: list[dict[str, str]] = Field(
        default_factory=list,
        description="关键证据列表，每项含 file, line, content, relevance",
    )
    montecarlo_findings: str = Field(default="", description="蒙特卡洛日志关键发现")
    tile_render_findings: str = Field(default="", description="瓦片渲染关键发现")
    pending_items: list[str] = Field(
        default_factory=list,
        description="待确认项",
    )
    suggested_actions: list[str] = Field(
        default_factory=list,
        description="建议动作",
    )
    confidence: str = Field(default="medium", description="分析置信度: high/medium/low")

    def to_markdown(self) -> str:
        """Render as Markdown report."""
        parts = []
        if self.conclusion:
            parts.append(f"## 结论摘要\n\n{self.conclusion}")
        if self.root_cause:
            parts.append(f"## 最可能原因\n\n{self.root_cause}")
        if self.montecarlo_findings:
            parts.append(f"## 蒙特卡洛日志发现\n\n{self.montecarlo_findings}")
        if self.tile_render_findings:
            parts.append(f"## 瓦片渲染发现\n\n{self.tile_render_findings}")
        if self.evidence:
            items = []
            for ev in self.evidence:
                file_ref = ev.get("file", "")
                line_ref = ev.get("line", "")
                content = ev.get("content", "")
                relevance = ev.get("relevance", "")
                loc = f"{file_ref}:{line_ref}" if line_ref else file_ref
                entry = f"- **{loc}**: {content}"
                if relevance:
                    entry += f" _{relevance}_"
                items.append(entry)
            parts.append("## 关键证据\n\n" + "\n".join(items))
        if self.pending_items:
            parts.append("## 待确认项\n\n" + "\n".join(f"- {item}" for item in self.pending_items))
        if self.suggested_actions:
            parts.append("## 建议动作\n\n" + "\n".join(f"- {action}" for action in self.suggested_actions))
        return "\n\n".join(parts) + "\n"


class BugAnalysisOutput(BaseModel):
    """Structured output for general bug analysis."""

    summary: str = Field(default="", description="一句话总结")
    analysis: str = Field(default="", description="详细分析")
    evidence: list[dict[str, str]] = Field(
        default_factory=list,
        description="关键证据",
    )
    severity: str = Field(default="medium", description="严重程度: critical/high/medium/low")
    category: str = Field(default="", description="问题分类")
    suggested_fix: str = Field(default="", description="修复建议")

    def to_markdown(self) -> str:
        """Render as Markdown report."""
        parts = []
        if self.summary:
            parts.append(f"## 结论摘要\n\n{self.summary}")
        if self.analysis:
            parts.append(f"## 详细分析\n\n{self.analysis}")
        if self.evidence:
            items = []
            for ev in self.evidence:
                file_ref = ev.get("file", "")
                content = ev.get("content", "")
                items.append(f"- **{file_ref}**: {content}")
            parts.append("## 关键证据\n\n" + "\n".join(items))
        if self.suggested_fix:
            parts.append(f"## 修复建议\n\n{self.suggested_fix}")
        return "\n\n".join(parts) + "\n"


class BugSummaryOutput(BaseModel):
    """Structured output for bug analysis summary / final report."""

    conclusion: str = Field(default="", description="核心结论（一段话）")
    root_cause: str = Field(default="", description="根因分析")
    impact: str = Field(default="", description="影响范围")
    evidence_summary: list[str] = Field(
        default_factory=list,
        description="关键证据摘要列表",
    )
    action_items: list[str] = Field(
        default_factory=list,
        description="后续行动项",
    )
    full_report: str = Field(default="", description="完整 Markdown 报告正文")

    def to_markdown(self) -> str:
        """Render as Markdown summary."""
        if self.full_report.strip():
            return self.full_report.strip() + "\n"
        parts = []
        if self.conclusion:
            parts.append(f"## 结论\n\n{self.conclusion}")
        if self.root_cause:
            parts.append(f"## 根因分析\n\n{self.root_cause}")
        if self.impact:
            parts.append(f"## 影响范围\n\n{self.impact}")
        if self.evidence_summary:
            items = "\n".join(f"- {e}" for e in self.evidence_summary)
            parts.append(f"## 关键证据\n\n{items}")
        if self.action_items:
            items = "\n".join(f"- {a}" for a in self.action_items)
            parts.append(f"## 后续行动\n\n{items}")
        return "\n\n".join(parts) + "\n"
