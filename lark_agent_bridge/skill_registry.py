"""Shared registry helpers for bridge-visible analysis skills."""

from __future__ import annotations


PRIMARY_BUG_SKILL_MAP: dict[str, tuple[str, str, bool]] = {
    "unity-startup-lifecycle-check": ("startup", "3D启动时序分析", True),
    "3d-stuck-investigate": ("stuck", "3D卡顿分析", True),
    "scene-signal-diagnosis": ("scene_signal", "3D场景信号分析", True),
    "signal-chain-analyzer": ("signal", "信号链路分析", False),
    "perception-data-summary": ("perception", "当前感知数据总结", True),
    "xtheme-analyzer": ("xtheme", "XTheme时光主题分析", True),
    "ld-lane-level-log-analysis-portable": ("ld_lane_level", "LD车道级日志分析", True),
    "general": ("general", "通用问题分析", False),
}

AUX_BUG_SKILLS = {
    "feishu-bug-fetcher",
    "log-decoder",
    "addr2line-resolve",
    "rom-version-lookup",
}


def extract_skill_frontmatter(text: str) -> tuple[str, str]:
    name = ""
    description = ""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if line.startswith("name:"):
                name = line.partition(":")[2].strip().strip('"').strip("'")
            elif line.startswith("description:"):
                description = line.partition(":")[2].strip().strip('"').strip("'")
    return name, description
