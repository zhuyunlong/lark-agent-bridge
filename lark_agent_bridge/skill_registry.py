"""Shared registry helpers for bridge-visible analysis skills."""

from __future__ import annotations

from typing import Any


PRIMARY_BUG_SKILL_MAP: dict[str, tuple[str, str, bool]] = {
    "unity-startup-lifecycle-check": ("startup", "3D启动时序分析", True),
    "3d-stuck-investigate": ("stuck", "3D卡顿分析", True),
    "scene-signal-diagnosis": ("scene_signal", "3D场景信号分析", True),
    "signal-chain-analyzer": ("signal", "信号链路分析", False),
    "perception-data-summary": ("perception", "当前感知数据总结", True),
    "xtheme-analyzer": ("xtheme", "XTheme时光主题分析", True),
    "ld-lane-level-log-analysis-portable": ("ld_lane_level", "LD车道级日志分析", True),
    "pullover-chain-analyzer": ("pullover_chain", "靠边停车链路分析", False),
    "general": ("general", "通用问题分析", False),
}

AUX_BUG_SKILLS = {
    "feishu-bug-fetcher",
    "log-decoder",
    "addr2line-resolve",
    "rom-version-lookup",
}


def extract_skill_frontmatter_fields(text: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if ":" not in line or line.startswith((" ", "\t", "#")):
                continue
            key, _sep, raw_value = line.partition(":")
            key = key.strip()
            if not key:
                continue
            fields[key] = _parse_frontmatter_value(raw_value.strip())
    return fields


def extract_skill_frontmatter(text: str) -> tuple[str, str]:
    fields = extract_skill_frontmatter_fields(text)
    name = str(fields.get("name") or "").strip()
    description = str(fields.get("description") or "").strip()
    return name, description


def _parse_frontmatter_value(raw_value: str) -> Any:
    value = raw_value.strip().strip('"').strip("'")
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip('"').strip("'") for item in inner.split(",") if item.strip()]
    return value
