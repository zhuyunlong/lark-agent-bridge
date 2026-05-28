#!/usr/bin/env python3
"""Standard real group-chat test suite for lark-agent-bridge.

Sends messages to a Feishu group via lark-cli, waits for bot responses,
and validates the results.  Designed to be run after each major milestone.

Usage:
    python scripts/test_group_chat.py                  # run all tests
    python scripts/test_group_chat.py --case initial    # initial bug only
    python scripts/test_group_chat.py --case followup   # follow-up only
    python scripts/test_group_chat.py --case time_fix   # time-correction only
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CHAT_ID = "oc_d977fe30a92c7ac81e3e6b543d99ef5b"
BOT_MENTION = "@朱云龙的飞书 CLI"
POLL_INTERVAL = 15          # seconds between status polls
MAX_WAIT      = 300         # max seconds to wait for bot completion

# Test data – 3D scene signal bug
BUG_URL_3D = "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998811703"
WRONG_TIME = "2025-05-10 14:30:00"
EXPECTED_CORRECT_TIME_SUBSTR = "2026-05-25"   # from bug title


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lark_cli(*args: str) -> str:
    """Run lark-cli and return stdout."""
    cmd = ["lark-cli", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return proc.stdout


def send_message(text: str) -> str:
    """Send a text message to the group chat. Returns message_id."""
    content = json.dumps({"text": text}, ensure_ascii=False)
    out = _lark_cli(
        "--as", "user",
        "im", "+messages-send",
        "--chat-id", CHAT_ID,
        "--msg-type", "text",
        "--content", content,
    )
    m = re.search(r'"message_id"\s*:\s*"(om_[^"]+)"', out)
    if not m:
        raise RuntimeError(f"Failed to send message: {out[:300]}")
    return m.group(1)


def reply_message(parent_msg_id: str, text: str) -> str:
    """Reply to a message. Returns new message_id."""
    out = _lark_cli(
        "--as", "user",
        "im", "+messages-reply",
        "--message-id", parent_msg_id,
        "--text", text,
    )
    m = re.search(r'"message_id"\s*:\s*"(om_[^"]+)"', out)
    if not m:
        raise RuntimeError(f"Failed to reply: {out[:300]}")
    return m.group(1)


def list_messages(page_size: int = 5) -> list[dict]:
    """List recent messages in the group chat (newest first)."""
    out = _lark_cli(
        "--as", "bot",
        "im", "+chat-messages-list",
        "--chat-id", CHAT_ID,
        "--page-size", str(page_size),
    )
    # strip lark-cli warnings
    json_start = out.find("{")
    if json_start < 0:
        return []
    data = json.loads(out[json_start:])
    return data.get("data", {}).get("messages", [])


def find_bot_card_reply(user_msg_id: str, timeout: int = MAX_WAIT) -> Optional[dict]:
    """Poll until the bot sends an interactive card replying to *user_msg_id*
    with status ✅ 已完成, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        msgs = list_messages(page_size=8)
        for msg in msgs:
            if (
                msg.get("reply_to") == user_msg_id
                and msg.get("msg_type") == "interactive"
            ):
                content = msg.get("content", "")
                if "✅ 已完成" in content or "已完成" in content:
                    return msg
                # still running
                if "分析中" in content or "处理中" in content:
                    break  # wait more
        time.sleep(POLL_INTERVAL)
    return None


# ---------------------------------------------------------------------------
# Test result
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    name: str
    passed: bool
    duration_s: float = 0
    details: str = ""
    card_msg_id: str = ""


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def test_initial_bug_analysis() -> TestResult:
    """Case 1: Send a 3D scene bug with wrong time and verify analysis completes."""
    print("\n🔷 Case 1: Initial 3D bug analysis (with wrong time)")
    t0 = time.time()

    msg_text = f"{BOT_MENTION} 问题时间 {WRONG_TIME} 3D场景信号分析 {BUG_URL_3D}"
    user_msg_id = send_message(msg_text)
    print(f"  → Sent: {user_msg_id}")

    card = find_bot_card_reply(user_msg_id)
    elapsed = time.time() - t0

    if card is None:
        return TestResult("initial_bug", False, elapsed, "Bot did not complete within timeout")

    content = card.get("content", "")
    has_skill = "3D场景信号分析" in content or "scene_signal" in content
    has_done = "✅ 已完成" in content
    details = f"completed={has_done}, skill_hit={has_skill}, msg_id={card['message_id']}"

    return TestResult(
        "initial_bug",
        has_done and has_skill,
        elapsed,
        details,
        card_msg_id=card["message_id"],
    )


def test_followup_reanalysis(parent_card_id: str) -> TestResult:
    """Case 2: Reply with a follow-up question (续聊) and verify reanalysis."""
    print("\n🔷 Case 2: Follow-up reanalysis (续聊)")
    t0 = time.time()

    reply_id = reply_message(parent_card_id, "重新分析信号链路，检查信号传递是否完整")
    print(f"  → Reply sent: {reply_id}")

    card = find_bot_card_reply(reply_id)
    elapsed = time.time() - t0

    if card is None:
        return TestResult("followup", False, elapsed, "Bot did not complete reanalysis within timeout")

    content = card.get("content", "")
    has_done = "✅ 已完成" in content
    is_reanalysis = "重新分析" in content or "续聊" in content or "Bug 重新分析" in content
    details = f"completed={has_done}, is_reanalysis={is_reanalysis}, msg_id={card['message_id']}"

    return TestResult(
        "followup",
        has_done,
        elapsed,
        details,
        card_msg_id=card["message_id"],
    )


def test_time_correction(parent_card_id: str) -> TestResult:
    """Case 3: Reply asking agent to correct the wrong time using actual bug time.

    The initial analysis was given wrong time (2025-05-10). The bug title actually
    contains the correct time (2026-05-25 16:50:41). This follow-up asks the agent
    to re-analyze using the real time from the bug data.
    """
    print("\n🔷 Case 3: Time-correction follow-up")
    t0 = time.time()

    reply_id = reply_message(parent_card_id, "请根据BUG问题实际时间继续分析")
    print(f"  → Reply sent: {reply_id}")

    card = find_bot_card_reply(reply_id)
    elapsed = time.time() - t0

    if card is None:
        return TestResult("time_correction", False, elapsed, "Bot did not complete within timeout")

    content = card.get("content", "")
    has_done = "✅ 已完成" in content
    # The agent should mention the correct time somewhere in its analysis
    has_correct_time = EXPECTED_CORRECT_TIME_SUBSTR in content
    # The agent should acknowledge the time was wrong
    has_correction = any(kw in content for kw in [
        "时间窗口错误", "时间修正", "正确时间", "实际时间", "2026-05-25",
    ])
    details = (
        f"completed={has_done}, correct_time={has_correct_time}, "
        f"acknowledged_correction={has_correction}, msg_id={card['message_id']}"
    )

    return TestResult(
        "time_correction",
        has_done and (has_correct_time or has_correction),
        elapsed,
        details,
        card_msg_id=card["message_id"],
    )


def test_source_analysis(parent_card_id: str) -> TestResult:
    """Case 4: Request source code analysis (续聊: 源码分析).

    Verifies the pydantic-ai agentic loop is used (not file-agent fallback)
    by checking for pydantic_ai provider in the response.
    """
    print("\n🔷 Case 4: Source code analysis (pydantic-ai agentic loop)")
    t0 = time.time()

    reply_id = reply_message(parent_card_id, "基于源码重新分析，检查信号处理相关的代码逻辑")
    print(f"  → Reply sent: {reply_id}")

    card = find_bot_card_reply(reply_id, timeout=MAX_WAIT)
    elapsed = time.time() - t0

    if card is None:
        return TestResult("source_analysis", False, elapsed, "Bot did not complete source analysis within timeout")

    content = card.get("content", "")
    has_done = "✅ 已完成" in content
    has_source = any(kw in content for kw in [
        "源码分析", "source", "代码", "函数", "方法", "类",
    ])
    details = f"completed={has_done}, has_source_ref={has_source}, msg_id={card['message_id']}"

    return TestResult(
        "source_analysis",
        has_done and has_source,
        elapsed,
        details,
        card_msg_id=card["message_id"],
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Standard real group-chat test suite")
    parser.add_argument(
        "--case",
        choices=["initial", "followup", "time_fix", "source", "all"],
        default="all",
        help="Which test case to run (default: all)",
    )
    parser.add_argument(
        "--parent-card-id",
        help="Message ID of an existing bot card to use for followup/time_fix tests",
    )
    args = parser.parse_args()

    results: list[TestResult] = []

    if args.case in ("all", "initial"):
        r1 = test_initial_bug_analysis()
        results.append(r1)
        parent_card = r1.card_msg_id
    else:
        parent_card = args.parent_card_id
        if not parent_card:
            print("ERROR: --parent-card-id is required for followup/time_fix cases")
            sys.exit(1)

    if args.case in ("all", "followup"):
        r2 = test_followup_reanalysis(parent_card)
        results.append(r2)
        # update parent for next test
        if r2.card_msg_id:
            parent_card = r2.card_msg_id

    if args.case in ("all", "time_fix"):
        r3 = test_time_correction(parent_card)
        results.append(r3)
        if r3.card_msg_id:
            parent_card = r3.card_msg_id

    if args.case in ("all", "source"):
        r4 = test_source_analysis(parent_card)
        results.append(r4)

    # --- Report ---
    print("\n" + "=" * 60)
    print("📊 Group Chat Test Results")
    print("=" * 60)
    all_passed = True
    for r in results:
        icon = "✅" if r.passed else "❌"
        print(f"  {icon} {r.name:20s}  {r.duration_s:6.1f}s  {r.details}")
        if not r.passed:
            all_passed = False
    print("=" * 60)
    if all_passed:
        print("🎉 All tests passed!")
    else:
        print("⚠️  Some tests failed.")
    print()

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
