"""Verify Anthropic prompt-cache hit rate across a multi-step agentic loop.

Runs a real source-analysis-style prompt through AgentRuntime against a repo
workspace, forcing several tool calls, then reports the per-run cache_read /
cache_write token metrics so we can confirm multi-step caching works.

Usage:
    .venv/bin/python scripts/verify_prompt_cache.py
"""
from __future__ import annotations

import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from lark_agent_bridge.config import load_config
from lark_agent_bridge.agents.agent_runtime import AgentRuntime

REPO = Path("../../xp/guideengine/.worktrees/os6_xpdev").resolve()

SYSTEM_PROMPT = (
    "You are a senior Android source-code analyst. Investigate the codebase using "
    "the provided read-only tools (read_file, grep, glob, list_dir). You MUST make "
    "several tool calls to gather evidence before answering. Work step by step: "
    "first list the top-level directory, then grep for relevant symbols, then read "
    "the most relevant files. Be thorough — at least 6 tool calls."
)

USER_PROMPT = (
    "源码分析：3D场景信号分析。缺陷：拾光主题切换成天玑主题后，进入场景模式没有展示3D场景。"
    "请定位主题切换(theme switch)与场景模式(scene mode)相关的代码，找出3D场景未展示的可能原因。"
    "请逐步调用工具收集证据：先 list_dir 查看目录结构，再 grep 搜索 theme / scene / 3D 等关键字，"
    "然后 read_file 阅读最相关的文件，最后给出分析结论。"
)


def main() -> int:
    cfg = load_config("config.toml")
    ai = cfg.ai_provider
    print(f"== provider: format={ai.api_format} model={ai.primary_model} base_url={ai.base_url}")
    if not REPO.exists():
        print(f"!! repo workspace not found: {REPO}")
        return 1

    runtime = AgentRuntime(ai, workspace=REPO, max_retries=1)
    print(f"== runtime path: {runtime._preferred_path}")

    result = runtime.run(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=USER_PROMPT,
        tools_enabled=True,
        strict_tools=True,
    )

    print("\n==================== RESULT ====================")
    print(f"ok={result.ok} runtime_path={result.runtime_path} duration={result.duration_seconds:.1f}s")
    print(f"tool_calls={result.tool_calls} error={result.error_code} {result.error[:200]}")
    u = result.usage or {}
    read = u.get("cache_read_tokens", 0)
    write = u.get("cache_write_tokens", 0)
    inp = u.get("request_tokens", 0)
    cacheable = read + write
    rate = (read / cacheable * 100) if cacheable else 0.0
    print("\n---------------- PROMPT CACHE ------------------")
    print(f"input_tokens       = {inp}")
    print(f"cache_read_tokens  = {read}")
    print(f"cache_write_tokens = {write}")
    print(f"cache hit rate     = {rate:.1f}%  (read / (read+write))")
    print(f"tool trace ({len(result.tool_trace)}):")
    for t in result.tool_trace[:20]:
        print(f"   - {t.get('tool')}({t.get('args','')[:60]})")
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
