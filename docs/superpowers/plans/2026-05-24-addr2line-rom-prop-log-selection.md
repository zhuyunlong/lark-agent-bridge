# Addr2Line ROM/Prop/Log Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make addr2line requests honor explicit ROM versions, otherwise infer ROM from downloaded `prop.txt`, and choose the correct `logN/logd` crash context by user-selected folder or fault time.

**Architecture:** Extend `Addr2LineRequest` with request-side log/time hints, teach the parser to extract those hints, and move missing-version preflight into `Addr2LineRunner` so it can download resources, select the best crash/log segment, infer ROM from the matching `prop.txt`, and then continue through the existing addr2line flow. Keep the existing explicit-ROM behavior intact.

**Tech Stack:** Python 3, unittest/pytest, existing `BridgeApp` / `Addr2LineRunner` / parser helpers

---

### Task 1: Add failing parser tests for log/time hints

**Files:**
- Modify: `tests/test_parser.py`
- Modify: `lark_agent_bridge/parser.py`
- Modify: `lark_agent_bridge/models.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_addr2line_request_extracts_log_folder_hint(self):
    request = parse_addr2line_request("log2 反解符号表", allow_missing_address=True)
    assert request.log_folder == "log2"


def test_parse_addr2line_request_extracts_fault_time_hint(self):
    request = parse_addr2line_request("5月22日 7:46 反解符号表", allow_missing_address=True)
    assert request.fault_time == "05-22 07:46"
```

- [ ] **Step 2: Run the parser tests to verify they fail**

Run: `PYTHONPATH=. pytest -q tests/test_parser.py -k "log_folder_hint or fault_time_hint"`
Expected: FAIL because `Addr2LineRequest` does not expose these fields yet.

- [ ] **Step 3: Add the request fields and parser extraction**

```python
@dataclass(slots=True)
class Addr2LineRequest:
    ...
    log_folder: str = ""
    fault_time: str = ""
```

```python
return Addr2LineRequest(
    ...
    log_folder=_extract_addr2line_log_folder(cleaned),
    fault_time=_extract_addr2line_fault_time(cleaned),
    ...
)
```

- [ ] **Step 4: Re-run the parser tests**

Run: `PYTHONPATH=. pytest -q tests/test_parser.py -k "log_folder_hint or fault_time_hint"`
Expected: PASS

### Task 2: Add failing runner tests for prop-based ROM inference

**Files:**
- Modify: `tests/test_app.py`
- Modify: `lark_agent_bridge/agents/addr2line_runner.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_addr2line_runner_infers_rom_from_lowest_log_prop_when_request_has_no_symbol_version(self):
    ...


def test_addr2line_runner_prefers_requested_log_folder_for_prop_and_crash_selection(self):
    ...
```

- [ ] **Step 2: Run the focused runner tests to verify they fail**

Run: `PYTHONPATH=. pytest -q tests/test_app.py -k "infers_rom_from_lowest_log_prop or prefers_requested_log_folder"`
Expected: FAIL because the runner currently returns `missing_symbol_version`.

- [ ] **Step 3: Implement resource preflight and prop selection**

```python
downloaded = self._download_resources(...)
selected_stack = self._select_navigation_stack(..., log_folder=request.log_folder, fault_time=request.fault_time)
inferred_rom = self._infer_rom_from_prop(..., preferred_log_folder=selected_stack.log_folder or request.log_folder)
```

- [ ] **Step 4: Re-run the focused runner tests**

Run: `PYTHONPATH=. pytest -q tests/test_app.py -k "infers_rom_from_lowest_log_prop or prefers_requested_log_folder"`
Expected: PASS

### Task 3: Add failing runner test for time-based log matching

**Files:**
- Modify: `tests/test_app.py`
- Modify: `lark_agent_bridge/agents/addr2line_runner.py`

- [ ] **Step 1: Write the failing test**

```python
def test_addr2line_runner_uses_fault_time_to_pick_matching_log_folder(self):
    ...
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `PYTHONPATH=. pytest -q tests/test_app.py -k "fault_time_to_pick_matching_log_folder"`
Expected: FAIL because the runner still chooses the old last-stack heuristic.

- [ ] **Step 3: Implement time-aware stack ranking**

```python
if parsed_fault_time is not None:
    ranked = sorted(candidates, key=lambda item: self._fault_time_distance(item.timestamp, parsed_fault_time))
    return ranked[0]
```

- [ ] **Step 4: Re-run the focused test**

Run: `PYTHONPATH=. pytest -q tests/test_app.py -k "fault_time_to_pick_matching_log_folder"`
Expected: PASS

### Task 4: Run the full focused regression slice

**Files:**
- Modify: `tests/test_parser.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Run the combined focused suite**

Run: `PYTHONPATH=. pytest -q tests/test_parser.py tests/test_app.py -k "addr2line or rom_lookup or log_folder_hint or fault_time_hint"`
Expected: PASS

- [ ] **Step 2: Review touched code for copy-constructor regressions**

```python
Addr2LineRequest(
    ...,
    log_folder=request.log_folder,
    fault_time=request.fault_time,
)
```

- [ ] **Step 3: Re-run the narrowest regression if any copy path was fixed**

Run: `PYTHONPATH=. pytest -q tests/test_app.py -k "addr2line"`
Expected: PASS
