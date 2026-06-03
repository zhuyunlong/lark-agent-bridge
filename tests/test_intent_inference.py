from lark_agent_bridge.agents.bug._shared import infer_intent_from_text, resolve_effective_intent


def test_symptom_text_is_diagnose():
    assert infer_intent_from_text("为什么收不到 SIGNAL_VCU_ELECTRICIT_PERCENT，场景里看不到") == "diagnose"


def test_consult_text_is_consult():
    assert infer_intent_from_text("了解下这个信号怎么接入、涉及哪些模块、链路怎么走") == "consult"


def test_neutral_text_is_empty():
    assert infer_intent_from_text("SIGNAL_VCU_ELECTRICIT_PERCENT") == ""


def test_effective_intent_keeps_explicit():
    assert resolve_effective_intent("consult", has_logs=True) == "consult"
    assert resolve_effective_intent("diagnose", has_logs=False) == "diagnose"


def test_effective_intent_falls_back_on_logs():
    assert resolve_effective_intent("", has_logs=True) == "diagnose"
    assert resolve_effective_intent("", has_logs=False) == "consult"
