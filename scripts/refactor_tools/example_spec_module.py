SRC = "lark_agent_bridge/knowledge/service.py"
PKG_DIR = "lark_agent_bridge/knowledge/answers"
PKG_IMPORT = ".answers"
PKG_DOC = "知识库答题子包：模拟/信号/命令/低置信候选/源码调查 各答案分支。"
ADD_DOTS = 1
GROUPS = {
    "answer_simulation.py": ("信号模拟问题识别、模板匹配与模拟类答案。", [
        "_SIMULATION_INTENT_TERMS", "_MOCK_ACTION", "_SIMULATION_TEMPLATES", "_DERIVED_SOURCE_ID",
        "_TEMPLATE_MATCH_SEPARATOR_RE",
        "_looks_like_signal_simulation_question", "_build_simulation_result",
        "_candidate_payload", "_matching_simulation_templates", "_template_positive_match",
        "_template_negative_match", "_template_terms", "_template_term_matches",
        "_normalize_template_match_text", "_looks_like_complex_simulation_question",
        "_contains_signal_token", "_dedupe_templates", "_prefer_template_hits",
        "_primary_template_hits", "_template_fallback_hits", "_template_reference_rank",
        "_direct_simulation_answer", "_custom_required_signal_answer",
        "_complex_simulation_policy_answer", "_simulation_candidates_answer",
        "_is_negative_template_question",
    ]),
    "answer_signal.py": ("信号源候选与 proto 字段摘要类答案。", [
        "_build_signal_source_candidate_result", "_signal_source_candidates",
        "_signal_source_candidates_answer", "_extract_signal_proto_field",
        "_signal_proto_summary", "_generic_answer",
    ]),
    "answer_command.py": ("命令查询识别与命令命中类答案。", [
        "_COMMAND_MARKERS",
        "_is_command_lookup_question", "_command_hits_answer",
        "_extract_command_lines", "_flatten_string_values", "_extract_command_from_line",
    ]),
    "answer_lowconf.py": ("低置信候选筛选、主题词过滤与候选答案。", [
        "_SIGNAL_DOMAIN_TERMS", "_MIN_RETRIEVAL_SCORE", "_LOW_CONFIDENCE_MAX_CANDIDATES",
        "_LOW_CONFIDENCE_SEARCH_LIMIT", "_LOW_CONFIDENCE_GENERIC_TOPIC_TERMS",
        "_low_confidence_topic_terms", "_specific_low_confidence_topic_terms",
        "_filter_specific_low_confidence_topic_terms", "_is_low_confidence_topic_term",
        "_should_offer_low_confidence_candidates", "_should_prefer_executable_candidates",
        "_filter_command_hits_by_specific_terms", "_has_specific_non_command_hits",
        "_has_explicit_signal_domain", "_low_confidence_command_candidates",
        "_search_hit_matches_any_topic_term", "_search_text_matches_topic_term",
        "_low_confidence_candidates_answer", "_candidate_intro", "_build_command_hit_result",
    ]),
    "answer_source.py": ("源码调查触发判定、写回与派生模拟。", [
        "_MIN_WRITEBACK_CONFIDENCE", "_SOURCE_INVESTIGATION_TERMS",
        "_source_metadata", "_source_investigation_unavailable_answer",
        "_strip_trigger_prefix", "_should_record_source_derived_simulation",
        "_derived_adb_simulation_chunks", "_should_run_source_investigation",
        "_explicit_source_investigation_requested", "_can_write_back_source_result",
        "_chunk_from_source_investigation", "_preview",
    ]),
}
