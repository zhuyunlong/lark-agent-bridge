from __future__ import annotations

from collections import deque
import subprocess
import unittest
from unittest import mock

from lark_agent_bridge.agents.codex_app_server_runtime import (
    CodexAppServerResult,
    CodexAppServerRuntime,
    CompletionState,
    _build_app_server_command,
    app_server_event_preview,
    check_codex_app_server_available,
)


class _FakeClient:
    def __init__(
        self,
        *,
        notifications: list[dict] | None = None,
        server_requests: list[dict] | None = None,
        stderr_lines: list[str] | None = None,
        turn_start_error: Exception | None = None,
        alive: bool = True,
        server_request_delay_polls: int = 0,
    ) -> None:
        self.notifications = deque(notifications or [])
        self.server_requests = deque(server_requests or [])
        self.stderr_lines = list(stderr_lines or [])
        self.turn_start_error = turn_start_error
        self.alive = alive
        self.server_request_delay_polls = server_request_delay_polls
        self.closed = False
        self.responses: list[tuple[object, dict]] = []
        self.error_responses: list[tuple[object, int, str]] = []
        self.requests: list[tuple[str, dict, float]] = []

    def initialize(self, **_kwargs):
        return {}

    def request(self, method: str, params: dict | None = None, timeout: float = 0.0):
        params = params or {}
        self.requests.append((method, params, timeout))
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            if self.turn_start_error is not None:
                raise self.turn_start_error
            return {"turn": {"id": "turn-1"}}
        if method == "turn/interrupt":
            return {}
        raise AssertionError(f"unexpected request {method}")

    def take_notification(self, timeout: float = 0.0):
        del timeout
        if self.notifications:
            return self.notifications.popleft()
        return None

    def take_server_request(self, timeout: float = 0.0):
        del timeout
        if self.server_request_delay_polls > 0:
            self.server_request_delay_polls -= 1
            return None
        if self.server_requests:
            return self.server_requests.popleft()
        return None

    def respond(self, request_id: object, result: dict) -> None:
        self.responses.append((request_id, result))

    def respond_error(self, request_id: object, code: int, message: str, data=None) -> None:
        del data
        self.error_responses.append((request_id, code, message))

    def stderr_tail(self, n: int = 20) -> list[str]:
        return self.stderr_lines[-n:]

    def stderr_line_count(self) -> int:
        return len(self.stderr_lines)

    def is_alive(self) -> bool:
        return self.alive

    def close(self) -> None:
        self.closed = True


def _runtime(client, **overrides):
    kwargs = dict(
        command="codex",
        cwd="/tmp/project",
        startup_timeout_seconds=1.0,
        turn_timeout_seconds=1.0,
        post_tool_quiet_timeout_seconds=1.0,
        notification_poll_seconds=0.0,
        max_event_audit=20,
        sandbox_mode="read-only",
        client_factory=lambda **_kwargs: client,
    )
    kwargs.update(overrides)
    return CodexAppServerRuntime(**kwargs)


class CodexAppServerRuntimeTests(unittest.TestCase):
    def test_event_preview_reports_command_and_token_usage(self):
        command_preview = app_server_event_preview(
            {
                "method": "item/started",
                "params": {
                    "item": {
                        "type": "commandExecution",
                        "command": "/bin/zsh -lc \"rg --line-number scene mode\"",
                    }
                },
            }
        )
        usage_preview = app_server_event_preview(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "tokenUsage": {
                        "total": {
                            "totalTokens": 321,
                            "inputTokens": 280,
                            "cachedInputTokens": 200,
                            "outputTokens": 41,
                        }
                    }
                },
            }
        )

        self.assertIn("rg --line-number scene mode", command_preview)
        self.assertIn("token≈321", usage_preview)
        self.assertIn("cache=200", usage_preview)

    def test_event_preview_reports_error_and_warning(self):
        error_preview = app_server_event_preview(
            {
                "method": "error",
                "params": {
                    "error": {
                        "message": "Reconnecting... 5/5",
                        "additionalDetails": "request timed out",
                    }
                },
            }
        )
        warning_preview = app_server_event_preview(
            {
                "method": "warning",
                "params": {"message": "Falling back from WebSockets to HTTPS transport."},
            }
        )

        self.assertIn("Reconnecting... 5/5", error_preview)
        self.assertIn("Falling back", warning_preview)

    def test_runtime_returns_final_answer_and_usage(self):
        client = _FakeClient(
            notifications=[
                {
                    "method": "item/started",
                    "params": {
                        "item": {
                            "type": "commandExecution",
                            "command": "/bin/zsh -lc \"rg --line-number 3D场景\"",
                        }
                    },
                },
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "tokenUsage": {
                            "total": {
                                "totalTokens": 4321,
                                "inputTokens": 4000,
                                "outputTokens": 321,
                            }
                        }
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "agentMessage",
                            "phase": "commentary",
                            "text": "正在读取上下文",
                        }
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "## 结论摘要\n- 已定位。\n\n## 关键证据\n- L1: evidence\n",
                        }
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {"turn": {"status": "completed"}},
                },
            ]
        )
        runtime = CodexAppServerRuntime(
            command="codex",
            cwd="/tmp/project",
            startup_timeout_seconds=1.0,
            turn_timeout_seconds=1.0,
            post_tool_quiet_timeout_seconds=1.0,
            notification_poll_seconds=0.0,
            max_event_audit=20,
            sandbox_mode="read-only",
            client_factory=lambda **_kwargs: client,
        )

        result = runtime.run_turn("分析 3D 场景主题切换")

        self.assertTrue(result.ok)
        self.assertEqual(result.thread_id, "thread-1")
        self.assertEqual(result.turn_id, "turn-1")
        self.assertIn("## 结论摘要", result.final_text)
        self.assertEqual(result.usage["totalTokens"], 4321)
        self.assertGreaterEqual(len(result.events), 4)
        self.assertEqual(client.requests[0][0], "thread/start")
        self.assertEqual(client.requests[1][0], "turn/start")
        self.assertTrue(client.closed)

    def test_runtime_declines_server_requests(self):
        client = _FakeClient(
            notifications=[
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "## 结论摘要\n- done\n\n## 关键证据\n- L1\n",
                        }
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {"turn": {"status": "completed"}},
                },
            ],
            server_requests=[
                {
                    "id": 99,
                    "method": "item/commandExecution/requestApproval",
                    "params": {"command": "rg scene", "cwd": "/tmp/project"},
                }
            ],
        )
        runtime = CodexAppServerRuntime(
            command="codex",
            cwd="/tmp/project",
            startup_timeout_seconds=1.0,
            turn_timeout_seconds=1.0,
            post_tool_quiet_timeout_seconds=1.0,
            notification_poll_seconds=0.0,
            max_event_audit=20,
            sandbox_mode="read-only",
            client_factory=lambda **_kwargs: client,
        )

        result = runtime.run_turn("分析")

        self.assertTrue(result.ok)
        self.assertEqual(client.responses, [(99, {"decision": "decline"})])

    def test_runtime_approves_bridge_codegraph_mcp_permission_request(self):
        client = _FakeClient(
            notifications=[
                {
                    "method": "item/started",
                    "params": {
                        "item": {
                            "type": "mcpToolCall",
                            "id": "call-codegraph",
                            "server": "bridge_codegraph",
                            "tool": "codegraph_status",
                        }
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "## 结论摘要\n- done\n\n## 关键证据\n- L1\n",
                        }
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {"turn": {"status": "completed"}},
                },
            ],
            server_requests=[
                {
                    "id": 100,
                    "method": "item/permissions/requestApproval",
                    "params": {},
                }
            ],
            server_request_delay_polls=1,
        )
        runtime = CodexAppServerRuntime(
            command="codex",
            cwd="/tmp/project",
            startup_timeout_seconds=1.0,
            turn_timeout_seconds=1.0,
            post_tool_quiet_timeout_seconds=1.0,
            notification_poll_seconds=0.0,
            max_event_audit=20,
            sandbox_mode="read-only",
            client_factory=lambda **_kwargs: client,
        )

        result = runtime.run_turn("分析")

        self.assertTrue(result.ok)
        self.assertEqual(client.responses, [(100, {"decision": "approve"})])

    def test_runtime_without_completion_event_is_partial(self):
        client = _FakeClient(
            notifications=[
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "commandExecution",
                            "command": "/bin/zsh -lc \"rg scene\"",
                        }
                    },
                },
                {
                    "method": "item/started",
                    "params": {
                        "item": {
                            "type": "agentMessage",
                            "id": "msg-1",
                            "phase": "final_answer",
                            "text": "",
                        }
                    },
                },
                {
                    "method": "item/agentMessage/delta",
                    "params": {"itemId": "msg-1", "delta": "## 结论摘要\n- ok\n\n## 关键证据\n- L1\n"},
                },
            ]
        )
        runtime = _runtime(
            client, turn_timeout_seconds=0.01, post_tool_quiet_timeout_seconds=0.01,
        )

        result = runtime.run_turn("分析")

        self.assertEqual(result.completion_state, CompletionState.PARTIAL)
        self.assertFalse(result.ok)
        self.assertIn("## 结论摘要", result.final_text)  # partial text preserved for audit

    def test_runtime_times_out_after_repeated_stream_disconnect_error(self):
        client = _FakeClient(
            notifications=[
                {
                    "method": "error",
                    "params": {
                        "error": {
                            "message": "Reconnecting... 5/5",
                            "additionalDetails": "request timed out",
                        }
                    },
                }
            ]
        )
        runtime = CodexAppServerRuntime(
            command="codex",
            cwd="/tmp/project",
            startup_timeout_seconds=1.0,
            turn_timeout_seconds=0.01,
            post_tool_quiet_timeout_seconds=30.0,
            notification_poll_seconds=0.0,
            max_event_audit=20,
            sandbox_mode="read-only",
            client_factory=lambda **_kwargs: client,
        )

        result = runtime.run_turn("分析")

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "codex_app_server_turn_timeout")
        self.assertTrue(result.should_retire)

    def test_turn_completed_event_yields_complete_state(self):
        client = _FakeClient(
            notifications=[
                {
                    "method": "item/completed",
                    "params": {"item": {"type": "agentMessage", "phase": "final_answer",
                        "text": "## 结论摘要\n- ok\n\n## 关键证据\n- L1 evidence\n"}},
                },
                {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
            ]
        )
        result = _runtime(client).run_turn("分析")
        self.assertEqual(result.completion_state, CompletionState.COMPLETE)
        self.assertTrue(result.ok)

    def test_ongoing_progress_prevents_post_tool_stall(self):
        # tool completes, then reasoning + token usage arrive before the final
        # answer: a short no_event timeout must NOT kill the turn, because those
        # events count as progress.
        client = _FakeClient(
            notifications=[
                {"method": "item/completed", "params": {"item": {"type": "commandExecution", "command": "rg x"}}},
                {"method": "item/started", "params": {"item": {"type": "reasoning"}}},
                {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"total": {"totalTokens": 10}}}},
                {"method": "item/completed", "params": {"item": {"type": "agentMessage", "phase": "final_answer",
                    "text": "## 结论摘要\n- ok\n\n## 关键证据\n- L1\n\n## 建议动作\n- go\n"}}},
                {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
            ]
        )
        result = _runtime(client, no_event_timeout_seconds=5.0).run_turn("分析")
        self.assertEqual(result.completion_state, CompletionState.COMPLETE)

    def test_no_event_timeout_marks_partial_and_retires(self):
        client = _FakeClient(notifications=[])  # genuinely silent: no events at all
        result = _runtime(
            client, turn_timeout_seconds=5.0, no_event_timeout_seconds=0.01,
        ).run_turn("分析")
        self.assertEqual(result.completion_state, CompletionState.PARTIAL)
        self.assertEqual(result.error_code, "codex_app_server_no_event_timeout")
        self.assertTrue(result.should_retire)

    def test_check_codex_app_server_available_rejects_old_version(self):
        with mock.patch(
            "lark_agent_bridge.agents.codex_app_server_runtime.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["codex", "--version"],
                returncode=0,
                stdout="codex-cli 0.124.0\n",
                stderr="",
            ),
        ):
            ok, message = check_codex_app_server_available("codex", "0.125.0")

        self.assertFalse(ok)
        self.assertIn("0.124.0", message)

    def test_check_codex_app_server_available_accepts_current_version(self):
        with mock.patch(
            "lark_agent_bridge.agents.codex_app_server_runtime.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["codex", "--version"],
                returncode=0,
                stdout="codex-cli 0.134.0\n",
                stderr="",
            ),
        ):
            ok, message = check_codex_app_server_available("codex", "0.125.0")

        self.assertTrue(ok)
        self.assertEqual(message, "0.134.0")

    def test_build_app_server_command_applies_lightweight_overrides(self):
        command = _build_app_server_command(
            command="codex",
            sandbox_mode="read-only",
            disable_node_repl=True,
            disable_analytics=True,
            disable_memories=True,
            disable_apps_feature=True,
            disable_plugins_feature=True,
            disable_computer_use_feature=True,
            reasoning_effort="medium",
        )

        self.assertEqual(command[:4], ["codex", "app-server", "-c", 'sandbox_mode="read-only"'])
        self.assertIn("analytics.enabled=false", command)
        self.assertIn("apps", command)
        self.assertIn("plugins", command)
        self.assertIn("computer_use", command)
        self.assertIn("mcp_servers.node_repl.enabled=false", command)
        self.assertIn("--disable", command)
        self.assertIn("memories", command)
        self.assertIn('model_reasoning_effort="medium"', command)


if __name__ == "__main__":
    unittest.main()
