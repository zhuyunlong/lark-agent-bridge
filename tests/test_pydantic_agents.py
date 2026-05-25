"""Tests for pydantic-based AI agent models, agents, and routing FSM."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from lark_agent_bridge.agents.pydantic_models import (
    PYDANTIC_AVAILABLE,
    IntentOutput,
    _extract_json,
)
from lark_agent_bridge.agents.pydantic_agents import (
    AgentResult,
    IntentAgent,
    _check_pydantic_ai,
)
from lark_agent_bridge.agents.routing_fsm import (
    FSMContext,
    RoutingFSM,
    State,
    create_routing_fsm,
)
from lark_agent_bridge.models import AIProviderOptions


class TestIntentOutput(unittest.TestCase):
    """Test IntentOutput Pydantic model."""

    def test_valid_creation(self):
        out = IntentOutput(route="bug", confidence="high", reason="Bug link found")
        self.assertEqual(out.route, "bug")
        self.assertEqual(out.confidence, "high")
        self.assertEqual(out.reason, "Bug link found")
        self.assertEqual(out.followup_action, "none")
        self.assertEqual(out.context_source, "none")

    def test_all_routes(self):
        for route in ("signal", "bug", "direct_analysis", "perception_summary", "analysis_followup", "chat", "unsupported"):
            out = IntentOutput(route=route, confidence="low", reason="test")
            self.assertEqual(out.route, route)

    def test_invalid_route_rejected(self):
        if not PYDANTIC_AVAILABLE:
            self.skipTest("pydantic not installed")
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            IntentOutput(route="invalid_route", confidence="high", reason="test")

    def test_invalid_confidence_rejected(self):
        if not PYDANTIC_AVAILABLE:
            self.skipTest("pydantic not installed")
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            IntentOutput(route="bug", confidence="very_high", reason="test")

    def test_from_llm_response_clean_json(self):
        raw = '{"route": "signal", "confidence": "medium", "reason": "signal code"}'
        out = IntentOutput.from_llm_response(raw)
        self.assertEqual(out.route, "signal")
        self.assertEqual(out.confidence, "medium")

    def test_from_llm_response_markdown_wrapped(self):
        raw = '```json\n{"route": "chat", "confidence": "low", "reason": "普通问题"}\n```'
        out = IntentOutput.from_llm_response(raw)
        self.assertEqual(out.route, "chat")

    def test_from_llm_response_embedded_in_text(self):
        raw = 'I think this is:\n{"route": "bug", "confidence": "high", "reason": "has bug URL"}\nDone.'
        out = IntentOutput.from_llm_response(raw)
        self.assertEqual(out.route, "bug")

    def test_from_llm_response_no_json_raises(self):
        with self.assertRaises(ValueError):
            IntentOutput.from_llm_response("This is plain text without JSON")

    def test_to_dict(self):
        out = IntentOutput(route="bug", confidence="high", reason="test")
        d = out.to_dict()
        self.assertIsInstance(d, dict)
        self.assertEqual(d["route"], "bug")

    def test_reason_truncated(self):
        if not PYDANTIC_AVAILABLE:
            self.skipTest("pydantic not installed")
        long_reason = "x" * 300
        out = IntentOutput(route="bug", confidence="low", reason=long_reason)
        self.assertLessEqual(len(out.reason), 200)

    def test_defaults(self):
        out = IntentOutput(route="chat")
        self.assertEqual(out.confidence, "low")
        self.assertEqual(out.followup_action, "none")
        self.assertEqual(out.context_source, "none")
        self.assertEqual(out.reason, "")


class TestExtractJson(unittest.TestCase):
    """Test _extract_json utility."""

    def test_clean_json(self):
        self.assertEqual(_extract_json('{"a": 1}'), '{"a": 1}')

    def test_with_whitespace(self):
        self.assertEqual(_extract_json('  {"a": 1}  '), '{"a": 1}')

    def test_markdown_code_block(self):
        raw = '```json\n{"a": 1}\n```'
        self.assertEqual(_extract_json(raw), '{"a": 1}')

    def test_embedded_json(self):
        raw = 'Here is the result: {"route": "bug"} end'
        self.assertEqual(_extract_json(raw), '{"route": "bug"}')

    def test_no_json_raises(self):
        with self.assertRaises(ValueError):
            _extract_json("no json here")

    def test_nested_braces(self):
        raw = '{"a": {"b": 1}}'
        self.assertEqual(_extract_json(raw), '{"a": {"b": 1}}')


class TestIntentAgent(unittest.TestCase):
    """Test IntentAgent wrapper."""

    def test_not_available_without_config(self):
        opts = AIProviderOptions()  # empty/disabled
        agent = IntentAgent(opts)
        self.assertFalse(agent.is_available())

    def test_not_available_without_pydantic_ai(self):
        opts = AIProviderOptions(
            enabled=True,
            base_url="http://test:1234/v1",
            primary_model="test-model",
            fast_model="test-fast",
            api_key="test-key",
        )
        with patch("lark_agent_bridge.agents.pydantic_agents._PYDANTIC_AI_AVAILABLE", False):
            from lark_agent_bridge.agents import pydantic_agents

            # Reset the cached value
            old_val = pydantic_agents._PYDANTIC_AI_AVAILABLE
            pydantic_agents._PYDANTIC_AI_AVAILABLE = False
            try:
                agent = IntentAgent(opts)
                self.assertFalse(agent.is_available())
            finally:
                pydantic_agents._PYDANTIC_AI_AVAILABLE = old_val

    def test_classify_raises_when_not_available(self):
        opts = AIProviderOptions()
        agent = IntentAgent(opts)
        with self.assertRaises(RuntimeError):
            agent.classify(system_prompt="test", user_prompt="test")


class TestRoutingFSM(unittest.TestCase):
    """Test the routing state machine."""

    def test_basic_flow_high_confidence(self):
        """High confidence keyword match should go directly to DONE."""

        def keyword_handler(ctx: FSMContext) -> State:
            ctx.route = "bug"
            ctx.confidence = "high"
            return State.ROUTE

        fsm = create_routing_fsm(keyword_handler=keyword_handler)
        ctx = FSMContext(message_text="分析bug")
        result = fsm.run(ctx)
        self.assertEqual(result.current_state, State.DONE)
        self.assertEqual(result.route, "bug")

    def test_basic_flow_low_confidence(self):
        """Low confidence should trigger LLM classification."""

        def keyword_handler(ctx: FSMContext) -> State:
            ctx.confidence = "low"
            return State.LLM_CLASSIFY

        def llm_handler(ctx: FSMContext) -> State:
            ctx.route = "chat"
            ctx.confidence = "medium"
            return State.ROUTE

        fsm = create_routing_fsm(
            keyword_handler=keyword_handler,
            llm_classify_handler=llm_handler,
        )
        ctx = FSMContext(message_text="你好")
        result = fsm.run(ctx)
        self.assertEqual(result.current_state, State.DONE)
        self.assertEqual(result.route, "chat")

    def test_max_steps_protection(self):
        """FSM should not loop infinitely."""
        call_count = [0]

        def looping_handler(ctx: FSMContext) -> State:
            call_count[0] += 1
            return State.LLM_CLASSIFY  # ping-pong between states

        def other_handler(ctx: FSMContext) -> State:
            return State.KEYWORD_MATCH

        fsm = RoutingFSM()
        fsm.add_state(State.START, lambda ctx: State.KEYWORD_MATCH)
        fsm.add_state(State.KEYWORD_MATCH, looping_handler)
        fsm.add_state(State.LLM_CLASSIFY, other_handler)

        ctx = FSMContext(message_text="test")
        ctx.current_state = State.START
        result = fsm.run(ctx, max_steps=5)
        self.assertEqual(result.current_state, State.ERROR)
        self.assertIn("max steps", result.error)

    def test_missing_handler_error(self):
        """Missing handler should produce ERROR state."""
        fsm = RoutingFSM()
        ctx = FSMContext(message_text="test")
        ctx.current_state = State.EXECUTE  # no handler registered
        result = fsm.run(ctx)
        self.assertEqual(result.current_state, State.ERROR)

    def test_history_tracking(self):
        """State transitions should be recorded."""

        def keyword_handler(ctx: FSMContext) -> State:
            ctx.route = "signal"
            ctx.confidence = "high"
            return State.ROUTE

        fsm = create_routing_fsm(keyword_handler=keyword_handler)
        ctx = FSMContext(message_text="SIGNAL_X3D")
        result = fsm.run(ctx)
        self.assertTrue(len(result.history) > 0)
        self.assertIn("start", result.history[0].lower())

    def test_custom_route_handler(self):
        """Custom route handler should be called."""

        def keyword_handler(ctx: FSMContext) -> State:
            ctx.route = "bug"
            ctx.confidence = "high"
            return State.ROUTE

        def route_handler(ctx: FSMContext) -> State:
            ctx.metadata["routed"] = True
            return State.DONE

        fsm = create_routing_fsm(
            keyword_handler=keyword_handler,
            route_handler=route_handler,
        )
        ctx = FSMContext(message_text="bug analysis")
        result = fsm.run(ctx)
        self.assertTrue(result.metadata.get("routed"))

    def test_transition_conditions(self):
        """Transition conditions should be evaluated correctly."""
        fsm = RoutingFSM()
        fsm.add_state(State.START, lambda ctx: State.START)  # handler doesn't transition
        fsm.add_state(State.DONE, lambda ctx: State.DONE)

        fsm.add_transition(
            State.START,
            State.DONE,
            condition=lambda ctx: ctx.route == "done",
            label="route is done",
        )
        fsm.add_transition(
            State.START,
            State.ERROR,
            condition=lambda ctx: ctx.route != "done",
            label="route is not done",
        )

        ctx = FSMContext(message_text="test")
        ctx.route = "done"
        result = fsm.run(ctx)
        self.assertEqual(result.current_state, State.DONE)


class TestAgentResult(unittest.TestCase):
    """Test AgentResult dataclass."""

    def test_creation(self):
        result = AgentResult(
            output="test",
            model="gpt-4",
            duration_seconds=1.5,
            usage={"prompt_tokens": 10, "completion_tokens": 20},
        )
        self.assertEqual(result.model, "gpt-4")
        self.assertEqual(result.duration_seconds, 1.5)

    def test_defaults(self):
        result = AgentResult(output="test")
        self.assertEqual(result.model, "")
        self.assertEqual(result.duration_seconds, 0.0)
        self.assertEqual(result.usage, {})


class TestIntentRunnerPydanticIntegration(unittest.TestCase):
    """Test that IntentRunner correctly integrates the pydantic agent path."""

    def test_runner_imports_cleanly(self):
        """IntentRunner should import without errors regardless of pydantic."""
        from lark_agent_bridge.agents.intent_runner import IntentAnalysisRunner

        self.assertTrue(hasattr(IntentAnalysisRunner, "classify"))

    def test_runner_without_ai_provider(self):
        """Runner without ai_provider should not have pydantic agent."""
        from lark_agent_bridge.agents.intent_runner import IntentAnalysisRunner
        from lark_agent_bridge.config import load_config

        config = load_config(None)  # default dry-run config
        runner = IntentAnalysisRunner(config)
        self.assertIsNone(runner._intent_agent)

    def test_runner_with_ai_provider_creates_agent(self):
        """Runner with ai_provider should try to create pydantic agent."""
        from lark_agent_bridge.agents.intent_runner import IntentAnalysisRunner
        from lark_agent_bridge.config import load_config

        config = load_config(None)
        # Enable ai_provider
        config = config.__class__(
            **{
                **{f.name: getattr(config, f.name) for f in config.__dataclass_fields__.values()},
                "ai_provider": AIProviderOptions(
                    enabled=True,
                    base_url="http://127.0.0.1:15721/v1",
                    primary_model="test-model",
                    fast_model="test-fast",
                    api_key="test",
                    api_format="openai",
                ),
            }
        )
        runner = IntentAnalysisRunner(config)
        # LLM client should be available
        self.assertIsNotNone(runner._llm_client)
        # Pydantic agent availability depends on pydantic-ai being installed
        # (which may or may not be the case in test env)


if __name__ == "__main__":
    unittest.main()
