SRC = "lark_agent_bridge/app/context_from.py"
CLASS_NAME = "_ContextFromMixin"
GROUPS = {
    "followup_clarify.py": ("_FollowupClarifyMixin", "追问澄清：Bug 时间/堆栈澄清与 Skill 确认、直传分析恢复", [
        "_bug_request_text_for_followup_context", "_fresh_bug_request_from_followup_context",
        "_maybe_handle_bug_time_clarification_followup",
        "_maybe_handle_bug_stack_clarification_followup",
        "_normalize_bug_time_clarification_text", "_looks_like_bug_time_fragment",
        "_bug_time_followup_has_clock", "_bug_time_followup_has_date",
        "_bug_time_followup_has_full_datetime", "_bug_time_text_has_full_datetime",
        "_followup_context_has_analysis_artifacts", "_session_with_followup_bug_metadata",
        "_recovered_direct_analysis_request_from_followup_context",
        "_execute_recovered_direct_analysis", "_maybe_handle_direct_analysis_followup",
        "_maybe_handle_bug_skill_confirmation_followup", "_send_followup_ack",
    ]),
    "existing_answer.py": ("_ExistingAnswerMixin", "基于既有分析产物的快速追问回答与置信评分", [
        "_answer_bug_followup_from_existing", "_bug_followup_requires_fresh_analysis",
        "_rank_existing_bug_evidence", "_existing_bug_answer_confidence",
        "_fast_answer_domain_terms", "_is_question_echo", "_looks_like_existing_evidence",
        "_normalize_fast_answer_text", "_fast_answer_tokens", "_clean_existing_answer_line",
        "_score_existing_answer_line", "_existing_bug_context_can_answer",
        "_followup_answerability_terms", "_append_answerability_term",
    ]),
    "context_lookup.py": ("_ContextLookupMixin", "会话上下文多级查找（活动会话/引用消息/产物消息）", [
        "_direct_reply_to", "_lookup_bot_alias_context", "_resolve_followup_context",
        "_followup_context_candidate_ids", "_context_from_activity_session",
        "_context_from_fetched_message", "_context_from_artifact_message",
        "_message_artifact_name", "_artifact_name_from_value",
        "_fetch_followup_reference_ids", "_extract_message_reference_ids",
        "_extract_message_records", "_message_content_text",
    ]),
}
CORE_DOC = None
