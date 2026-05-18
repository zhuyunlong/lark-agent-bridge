# Dynamic Progress Cards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Feishu dynamic analysis progress cards that are created at request start, updated during backend progress, and finalized with result metadata, report link, token usage, and duration.

**Architecture:** Keep the feature inside the existing bridge delivery path. `BridgeApp` owns card lifecycle and progress aggregation; `LarkClient` exposes the send/reply/update primitives; `cards.py` builds reusable status/result card payloads. The implementation is best-effort: if card creation or update fails, analysis continues and final text/card fallback remains intact.

**Tech Stack:** Python 3.11 standard library, `unittest`, existing `lark-cli im` send/reply wrappers, generic `lark-cli api` update wrapper, existing `AgentActivityStore` progress records.

---

## File Structure

- Modify `lark_agent_bridge/lark_client.py`
  - Add `update_card(message_id, card_json)` wrapper around the generic `lark-cli api PATCH /open-apis/im/v1/messages/{message_id}` command, because the local `lark-cli im` shortcuts do not include a message-update helper.
  - Keep dry-run support through `_run_or_plan`.

- Modify `lark_agent_bridge/cards.py`
  - Extend `build_status_card()` to show progress nodes, elapsed time, token usage, and report links.
  - Keep current simple status-card tests passing.

- Modify `lark_agent_bridge/app.py`
  - Add a small internal progress-card state keyed by root/session message ID.
  - Create an initial progress card before long-running analysis begins.
  - Update the same card from `_notify_progress()`.
  - Finalize the progress card after result delivery.
  - Preserve existing final result card behavior and text fallback.

- Modify `tests/test_lark_client.py`
  - Add a command-level test for card update.

- Modify `tests/test_cards.py`
  - Add a rendering test for progress-card fields.

- Modify `tests/test_app.py`
  - Add app-level tests that progress cards are created, updated, and finalized for group and p2p analysis paths.

- Modify `README.md` and `docs/operations.md`
  - Document that dynamic card updates are best-effort and use `lark-cli api PATCH /open-apis/im/v1/messages/{message_id}`.

---

### Task 1: LarkClient Card Update Primitive

**Files:**
- Modify: `lark_agent_bridge/lark_client.py`
- Test: `tests/test_lark_client.py`

- [x] **Step 1: Write the failing test**

Add this test near existing reply/card command tests:

```python
def test_update_card_uses_generic_patch_api(self):
    with tempfile.TemporaryDirectory() as tmp:
        client = LarkClient(BridgeConfig(dry_run=True, data_dir=Path(tmp)))

        result = client.update_card("om_card_1", "{\"config\":{}}")

    self.assertEqual(
        result.command,
            [
                "lark-cli",
                "api",
                "PATCH",
                "/open-apis/im/v1/messages/om_card_1",
                "--as",
                "bot",
                "--data",
                '{"msg_type":"interactive","content":"{\\"config\\":{}}"}',
            ],
        )
```

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
python3.11 -m unittest tests.test_lark_client.LarkClientTests.test_update_card_uses_generic_patch_api
```

Expected: FAIL/ERROR because `LarkClient.update_card` does not exist.

- [x] **Step 3: Write minimal implementation**

Add:

```python
def update_card(self, message_id: str, card_json: str) -> CommandResult:
    data = json.dumps(
        {"msg_type": "interactive", "content": card_json},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    command = [
        "lark-cli",
        "api",
        "PATCH",
        f"/open-apis/im/v1/messages/{message_id}",
        "--as",
        "bot",
        "--data",
        data,
    ]
    return self._run_or_plan(command)
```

- [x] **Step 4: Run test to verify it passes**

Run:

```bash
python3.11 -m unittest tests.test_lark_client.LarkClientTests.test_update_card_uses_generic_patch_api
```

Expected: PASS.

---

### Task 2: Progress Card Rendering

**Files:**
- Modify: `lark_agent_bridge/cards.py`
- Test: `tests/test_cards.py`

- [x] **Step 1: Write the failing test**

Add:

```python
def test_status_card_with_progress_runtime_and_tokens(self):
    card = build_status_card(
        title="Bug 分析",
        status="analyzing",
        details={"任务ID": "job_1"},
        progress=[
            {"stage": "bug_fetch_data", "message": "拉取 bug 详情"},
            {"stage": "bug_agent_summary", "message": "调用本地 Agent", "details": {"provider": "codex"}},
        ],
        elapsed_seconds=12.4,
        token_usage={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
        report_url="http://127.0.0.1:8765/reports/job_1/",
    )

    rendered = str(card)
    assert "后台进度" in rendered
    assert "bug_fetch_data" in rendered
    assert "12.4 秒" in rendered
    assert "1200" in rendered
    assert "打开报告" in rendered
```

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
python3.11 -m unittest tests.test_cards.TestBuildStatusCard.test_status_card_with_progress_runtime_and_tokens
```

Expected: FAIL/ERROR because `build_status_card()` does not accept the new keyword arguments.

- [x] **Step 3: Write minimal implementation**

Extend `build_status_card()` with optional `progress`, `elapsed_seconds`, `token_usage`, and `report_url` keyword-only parameters. Render no more than the latest 6 progress nodes to keep card size bounded.

- [x] **Step 4: Run test to verify it passes**

Run:

```bash
python3.11 -m unittest tests.test_cards.TestBuildStatusCard.test_status_card_with_progress_runtime_and_tokens
```

Expected: PASS.

---

### Task 3: BridgeApp Dynamic Card Lifecycle

**Files:**
- Modify: `lark_agent_bridge/app.py`
- Test: `tests/test_app.py`

- [x] **Step 1: Write the failing test**

Add methods to `FakeLarkClient`:

```python
self.updated_cards = []

def update_card(self, message_id, card_json):
    self.updated_cards.append({"message_id": message_id, "card_json": card_json})
    return CommandResult(command=["update-card"], returncode=0)
```

Add test:

```python
def test_bug_request_creates_updates_and_finalizes_progress_card(self):
    with tempfile.TemporaryDirectory() as tmp:
        metadata = Path(tmp) / "bug_metadata.md"
        html = Path(tmp) / "bug_report.html"
        metadata.write_text("bug", encoding="utf-8")
        html.write_text("<html></html>", encoding="utf-8")
        fake_lark = FakeLarkClient()
        app = BridgeApp(
            BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
            lark_client=fake_lark,
            bug_runner=FakeBugRunner(metadata, html),
        )

        result = app.handle_event(
            event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动")
        )

    self.assertTrue(result.success)
    self.assertGreaterEqual(len(fake_lark.card_replies), 1)
    self.assertGreaterEqual(len(fake_lark.updated_cards), 1)
    self.assertTrue(any("bug_fetch_data" in item["card_json"] for item in fake_lark.updated_cards))
    self.assertTrue(any("已完成" in item["card_json"] for item in fake_lark.updated_cards))
```

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
python3.11 -m unittest tests.test_app.AppTests.test_bug_request_creates_updates_and_finalizes_progress_card
```

Expected: FAIL because no progress card lifecycle exists.

- [x] **Step 3: Write minimal implementation**

Add internal helpers:

```python
def _begin_progress_card(self, event, *, title, mode, root_message_id=None): ...
def _update_progress_card(self, event, *, status="analyzing", result=None, session_id=None): ...
def _card_message_id_from_result(self, command_result): ...
def _progress_card_title(self, mode): ...
```

Implementation rules:
- Only run when `dry_run` is false and `event.chat_type in {"group", "p2p"}`.
- Start card with `build_status_card(status="queued")`.
- Prefer replying to the triggering message via `reply_card(event.message_id, card_json)` so the card stays in context.
- Store returned card message ID when possible; in tests fall back to the triggering `event.message_id` if command output has no ID.
- In `_notify_progress()`, if a progress card exists for the session/event, call `update_card(card_message_id, card_json)`.
- On completion, call `_update_progress_card(..., status="completed", result=result)`.
- On exception paths already caught by callers, leave existing error behavior intact; add failure-card finalization only where `_send_result()` handles a failed `TaskResult`.

- [x] **Step 4: Run test to verify it passes**

Run:

```bash
python3.11 -m unittest tests.test_app.AppTests.test_bug_request_creates_updates_and_finalizes_progress_card
```

Expected: PASS.

---

### Task 4: P2P and Failure/Fallback Coverage

**Files:**
- Modify: `tests/test_app.py`
- Modify: `lark_agent_bridge/app.py`

- [x] **Step 1: Write the failing p2p test**

Add:

```python
def test_p2p_bug_request_updates_progress_card_without_group_mention(self):
    with tempfile.TemporaryDirectory() as tmp:
        metadata = Path(tmp) / "bug_metadata.md"
        html = Path(tmp) / "bug_report.html"
        metadata.write_text("bug", encoding="utf-8")
        html.write_text("<html></html>", encoding="utf-8")
        fake_lark = FakeLarkClient()
        app = BridgeApp(
            BridgeConfig(dry_run=False, data_dir=Path(tmp)),
            lark_client=fake_lark,
            bug_runner=FakeBugRunner(metadata, html),
        )

        result = app.handle_event(
            event(
                chat_id="ou_chat_1",
                chat_type="p2p",
                content="https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动",
            )
        )

    self.assertTrue(result.success)
    self.assertGreaterEqual(len(fake_lark.card_replies), 1)
    self.assertGreaterEqual(len(fake_lark.updated_cards), 1)
```

- [x] **Step 2: Run test to verify it fails or confirms missing behavior**

Run:

```bash
python3.11 -m unittest tests.test_app.AppTests.test_p2p_bug_request_updates_progress_card_without_group_mention
```

Expected before implementation: FAIL if lifecycle is not generic enough.

- [x] **Step 3: Adjust implementation**

Ensure progress-card start/update does not require group mention after routing has already accepted the event.

- [x] **Step 4: Run both app lifecycle tests**

Run:

```bash
python3.11 -m unittest \
  tests.test_app.AppTests.test_bug_request_creates_updates_and_finalizes_progress_card \
  tests.test_app.AppTests.test_p2p_bug_request_updates_progress_card_without_group_mention
```

Expected: PASS.

- [x] **Step 5: Cover update failure fallback**

Add and run `test_progress_card_update_failure_falls_back_to_text_reply` and `test_failed_progress_card_update_failure_falls_back_to_text_reply` to verify a failed `update_card()` call still leaves the original analysis running and sends the final text reply for both success and failure results.

---

### Task 5: Documentation and Final Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/operations.md`

- [x] **Step 1: Document behavior**

Update docs to say:

```markdown
Dynamic progress cards are best-effort. For HTML-producing analysis requests,
the bridge replies with an initial progress card, updates the same card with
backend progress nodes, elapsed time, token usage when available, and finally
adds the report link. If the deployed `lark-cli` cannot update cards, analysis
still completes and the bridge falls back to the existing final result reply.
```

- [x] **Step 2: Run targeted verification**

Run:

```bash
python3.11 -m unittest tests.test_lark_client tests.test_cards tests.test_app.AppTests.test_bug_request_replies_with_published_link tests.test_app.AppTests.test_bug_request_creates_updates_and_finalizes_progress_card tests.test_app.AppTests.test_p2p_bug_request_updates_progress_card_without_group_mention
```

Expected: PASS.

- [x] **Step 3: Run whitespace/diff check**

Run:

```bash
git diff --check
```

Expected: no output and exit 0.

---

## Self-Review

- Spec coverage: plan covers dynamic card start, progress updates, final status, token/duration rendering, group and p2p scenarios, fallback behavior, and docs.
- Placeholder scan: no TBD/TODO steps remain.
- Type consistency: uses existing `CommandResult`, `TaskResult`, `LarkEvent`, `build_status_card`, and `card_to_json` types.
