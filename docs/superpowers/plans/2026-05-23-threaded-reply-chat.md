# Threaded Reply Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make knowledge answers and ordinary chat replies stay threaded under the user's message, allow group follow-ups in an established reply chain without repeated `@bot`, and prevent mid-thread follow-ups from inheriting later branch context.

**Architecture:** Keep the existing root-plus-alias conversation model, but treat alias message IDs as branch snapshots instead of always collapsing them back to the root. Persist ordinary chat contexts the same way bug follow-ups are persisted, then route group follow-ups by reply-chain context before requiring a fresh mention. Keep bug/report flows unchanged.

**Tech Stack:** Python 3.11, unittest, existing `BridgeApp` / `ConversationContextStore` / card builders

---

### Task 1: Make Alias Contexts Behave Like Branch Snapshots

**Files:**
- Modify: `lark_agent_bridge/state.py`
- Test: `tests/test_state.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Add a failing alias-lookup test**

Add a unit test in `tests/test_state.py` proving that an alias lookup returns the alias snapshot history instead of resolving back to the root context history.

Expected shape:

```python
def test_lookup_alias_keeps_snapshot_history(self):
    store = ConversationContextStore(state_file)
    store.remember(
        root_message_id="om_root",
        chat_id="oc_1",
        mode="omlx_chat",
        request_text="你好",
        summary_text="第一轮回答",
        report_url="",
        report_excerpt="",
    )
    store.append_exchange("om_root", user_text="第一问", assistant_text="第一答")
    store.remember_alias(alias_message_id="om_bot_reply_1", root_message_id="om_root")
    store.append_exchange("om_root", user_text="第二问", assistant_text="第二答")

    alias_context = store.lookup("om_bot_reply_1")

    self.assertEqual(
        alias_context.history,
        [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
        ],
    )
```

- [ ] **Step 2: Run the alias-lookup test and confirm it fails**

Run:

```bash
python3.11 -m unittest tests.test_state -q
```

Expected: the new test fails because `lookup("om_bot_reply_1")` currently resolves back to the root and includes later history.

- [ ] **Step 3: Implement alias-first lookup semantics**

Update `ConversationContextStore.lookup()` in `lark_agent_bridge/state.py` so that:

- a direct alias hit returns the stored alias context as-is
- only direct root lookups return the root context
- no automatic alias-to-root collapse happens on lookup

Minimal target behavior:

```python
def lookup(self, key: str) -> ConversationContext | None:
    normalized = key.strip() if key else ""
    if not normalized:
        return None
    return self._contexts.get(normalized)
```

Preserve `remember_alias()` snapshot copying and existing persistence format.

- [ ] **Step 4: Run the state tests and confirm they pass**

Run:

```bash
python3.11 -m unittest tests.test_state -q
```

Expected: PASS.

- [ ] **Step 5: Add a branch-isolation app test**

In `tests/test_app.py`, add a test that:

- starts an `omlx_chat` or `basic_chat` session
- simulates two bot replies under the same root
- replies from the earlier bot message
- verifies the prompt/history given to the later follow-up excludes branch content that happened after that earlier reply

Use the fake chat client to capture the prompt/context payload rather than asserting on internal store details only.

- [ ] **Step 6: Run the new app test and confirm it fails**

Run the specific test:

```bash
python3.11 -m unittest tests.test_app.AppTests.test_chat_followup_from_middle_reply_ignores_later_branch_history -q
```

Expected: FAIL because current follow-up resolution still behaves as if the entire root history is the active branch.

- [ ] **Step 7: Commit Task 1**

```bash
git add lark_agent_bridge/state.py tests/test_state.py tests/test_app.py
git commit -m "refactor: keep conversation alias snapshots isolated"
```

### Task 2: Persist Chat Contexts and Allow Group Follow-Ups Without Repeated Mention

**Files:**
- Modify: `lark_agent_bridge/app.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Add failing tests for reply delivery and no-mention follow-up**

Add tests covering:

1. `basic_chat` uses `reply` instead of `send_response`
2. `omlx_chat` uses `reply`
3. group follow-up inside an established knowledge/chat reply chain works without `@bot`
4. a non-reply group message without `@bot` is still ignored

Representative expectations:

```python
self.assertIn("om_user_msg", _all_reply_message_ids(fake_lark))
self.assertEqual(result.details["conversation_root_message_id"], "om_user_msg")
```

and:

```python
self.assertTrue(followup.success)
self.assertEqual(followup.details["mode"], "knowledge_qa")
```

- [ ] **Step 2: Run the targeted app tests and confirm they fail**

Run:

```bash
python3.11 -m unittest \
  tests.test_app.AppTests.test_basic_chat_replies_to_user_message \
  tests.test_app.AppTests.test_omlx_chat_replies_to_user_message \
  tests.test_app.AppTests.test_group_knowledge_followup_in_reply_chain_without_mention \
  tests.test_app.AppTests.test_group_non_reply_without_mention_still_skipped \
  -q
```

Expected: FAIL because ordinary chat does not consistently persist reply-thread context and group follow-up admission still depends on explicit mention stripping.

- [ ] **Step 3: Persist ordinary chat contexts**

In `lark_agent_bridge/app.py`, update the ordinary chat success path so that `basic_chat` and `omlx_chat` successful deliveries:

- set `details["delivery"] = "reply"`
- set `details["conversation_root_message_id"] = event.root_id or event.message_id`
- store a root conversation context through `conversation_store.remember(...)`
- append exchanges after successful follow-up replies just like bug follow-ups do

Implement this in `_handle_chat_intent()` and/or `_prepare_delivery_result()` rather than duplicating logic per mode when possible.

- [ ] **Step 4: Allow no-mention follow-up when context already exists**

In the group gate inside `_handle_event()`:

- keep explicit `@bot` required for new group requests
- when no mention is present, resolve `followup_context`
- if a follow-up context exists and its mode is in `{knowledge_qa, knowledge_probe, basic_chat, omlx_chat}`, accept the raw message as `route_content`
- otherwise continue to return `not_addressed`

Avoid broadening this behavior to bug/signal/direct-analysis flows.

- [ ] **Step 5: Preserve alias snapshots after successful reply/card delivery**

Ensure successful reply-based ordinary chat and knowledge-card deliveries record alias message IDs via `_remember_delivery_alias_from_result(...)`, and that these aliases point at the current branch snapshot.

If needed, update `_prepare_delivery_result()` or the success path so alias recording happens after the current branch history is in the store.

- [ ] **Step 6: Run the targeted app tests and confirm they pass**

Run:

```bash
python3.11 -m unittest \
  tests.test_app.AppTests.test_basic_chat_replies_to_user_message \
  tests.test_app.AppTests.test_omlx_chat_replies_to_user_message \
  tests.test_app.AppTests.test_group_knowledge_followup_in_reply_chain_without_mention \
  tests.test_app.AppTests.test_group_non_reply_without_mention_still_skipped \
  tests.test_app.AppTests.test_chat_followup_from_middle_reply_ignores_later_branch_history \
  -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 2**

```bash
git add lark_agent_bridge/app.py tests/test_app.py
git commit -m "feat: thread chat replies through reply-chain context"
```

### Task 3: Keep Knowledge Cards Readable and Verify No Regressions

**Files:**
- Modify: `lark_agent_bridge/cards.py`
- Test: `tests/test_cards.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Add a failing knowledge-card readability test**

In `tests/test_cards.py`, add a test asserting that knowledge cards:

- keep the answer body directly readable
- do not append the generic `完整内容请查看报告` truncation hint

Example expectation:

```python
card = build_knowledge_answer_card(title="知识库回答", answer=long_answer, hits=[...])
rendered = str(card)
self.assertIn("结论摘要", rendered)
self.assertNotIn("完整内容请查看报告", rendered)
```

- [ ] **Step 2: Run the targeted card test and confirm it fails**

Run:

```bash
python3.11 -m unittest tests.test_cards -q
```

Expected: FAIL because `build_knowledge_answer_card()` currently uses the generic truncation helper that appends the report hint.

- [ ] **Step 3: Implement a knowledge-specific readable truncation path**

In `lark_agent_bridge/cards.py`:

- stop using `_truncate_summary(..., max_chars=1600)` directly for the knowledge answer body
- add a knowledge-specific helper that trims to a safe size without the report-style hint
- preserve the sources section layout

Minimal direction:

```python
def _truncate_card_body(text: str, *, max_chars: int) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= max_chars:
        return cleaned
    truncated = cleaned[:max_chars]
    last_newline = truncated.rfind("\n")
    if last_newline >= max_chars // 2:
        truncated = truncated[:last_newline]
    return truncated.rstrip() + "\n\n(内容较长，已截断显示)"
```

Use this helper only for knowledge-card body text.

- [ ] **Step 4: Run cards and reply-chain regression tests**

Run:

```bash
python3.11 -m unittest \
  tests.test_cards \
  tests.test_app.AppTests.test_followup_reply_resolves_context_via_reply_to_chain \
  tests.test_app.AppTests.test_followup_reply_to_progress_card_resolves_original_bug_context \
  tests.test_app.AppTests.test_followup_reply_to_uploaded_html_resolves_context_when_event_lacks_reply_to \
  -q
```

Expected: PASS.

- [ ] **Step 5: Run full verification**

Run:

```bash
python3.11 -m unittest discover -s tests -q
```

Expected: PASS with the current skipped count only.

- [ ] **Step 6: Commit Task 3**

```bash
git add lark_agent_bridge/cards.py tests/test_cards.py tests/test_app.py
git commit -m "feat: make threaded knowledge chat replies readable"
```

- [ ] **Step 7: Final diff review**

Run:

```bash
git diff --stat HEAD~3..HEAD
git status --short
```

Expected:

- only the planned files changed for this feature
- unrelated `downloader` worktree changes remain untouched

