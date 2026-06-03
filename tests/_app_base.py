from pathlib import Path
from datetime import datetime, timedelta, timezone
import json
import os
import tempfile
import time
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

from lark_agent_bridge.app import BridgeApp
from lark_agent_bridge.agents import Addr2LineRunner, BugAnalysisPlan, BugAnalysisSelection, BugFollowupSelection, UnifiedBugDecision
from lark_agent_bridge.lark_client import CommandResult
from lark_agent_bridge.models import (
    Addr2LineRequest,
    ApprovalOptions,
    BridgeConfig as RealBridgeConfig,
    ClaudeAgentOptions,
    DualAgentOptions,
    DownloadResource,
    EventConsumerOptions,
    IntentDecision,
    KnowledgeOptions,
    KnowledgeSourceOptions,
    LarkEvent,
    LarkOptions,
    LocalResourceOptions,
    NotificationOptions,
    ReportServerOptions,
    SignalRequest,
    TaskResult,
    WorkflowArchiveOptions,
)
from lark_agent_bridge.knowledge.models import SearchHit


def BridgeConfig(*args, **kwargs):
    kwargs.setdefault("approval", ApprovalOptions(enabled=False))
    kwargs.setdefault("lark", LarkOptions(bot_name="bot"))
    return RealBridgeConfig(*args, **kwargs)


def _all_reply_message_ids(fake_lark):
    """Collect message IDs from both text replies and card replies."""
    ids = [r["message_id"] for r in fake_lark.replies]
    ids += [r["message_id"] for r in fake_lark.card_replies]
    return ids


def _last_reply_message_id(fake_lark):
    """Get the last replied message ID (text or card)."""
    all_ids = _all_reply_message_ids(fake_lark)
    return all_ids[-1] if all_ids else None


class FakeLarkClient:
    def __init__(self):
        self.sent = []
        self.replies = []
        self.files = []
        self.cards = []
        self.card_replies = []
        self.updated_cards = []
        self.fetched_messages = {}

    def send_response(self, event, text, *, markdown=False):
        self.sent.append({"event": event, "text": text, "markdown": markdown})
        return CommandResult(command=["send"], returncode=0)

    def reply(self, message_id, text, *, markdown=False):
        self.replies.append({"message_id": message_id, "text": text, "markdown": markdown})
        reply_message_id = f"om_reply_{len(self.replies)}"
        return CommandResult(
            command=["reply"],
            returncode=0,
            stdout=f'{{"data":{{"message_id":"{reply_message_id}"}}}}',
        )

    def reply_card(self, message_id, card_json):
        card_message_id = f"om_card_{len(self.card_replies) + 1}"
        self.card_replies.append({"message_id": message_id, "card_message_id": card_message_id, "card_json": card_json})
        return CommandResult(
            command=["reply-card"],
            returncode=0,
            stdout=f'{{"data":{{"message_id":"{card_message_id}"}}}}',
        )

    def send_card_response(self, event, card_json):
        self.cards.append({"event": event, "card_json": card_json})
        return CommandResult(command=["send-card"], returncode=0)

    def update_card(self, message_id, card_json):
        self.updated_cards.append({"message_id": message_id, "card_json": card_json})
        return CommandResult(command=["update-card"], returncode=0)

    def send_file_response(self, event, path):
        file_message_id = f"om_file_{len(self.files) + 1}"
        self.files.append({"event": event, "path": path, "message_id": file_message_id})
        return CommandResult(
            command=["send-file"],
            returncode=0,
            stdout=f'{{"data":{{"message_id":"{file_message_id}"}}}}',
        )

    def fetch_message(self, message_id):
        payload = self.fetched_messages.get(message_id)
        if payload is None:
            return CommandResult(command=["fetch"], returncode=1, stderr="not found")
        return CommandResult(command=["fetch"], returncode=0, stdout=payload)

    def check_environment(self):
        return {}

    def download_resource(self, **kwargs):
        raise AssertionError("download_resource should not be called in these tests")


class FakeClaudeRunner:
    def __init__(self, artifact_path: Path):
        self.artifact_path = artifact_path
        self.requests = []

    def run_skill_analysis(self, request, *, event=None):
        self.requests.append(request)
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="Claude Code skill 分析完成\n摘录:\n结论",
            details={"mode": "claude_skill", "files_to_send": [self.artifact_path]},
        )


class FakeOmlxChatClient:
    def __init__(self):
        self.prompts = []
        self.context_calls = []

    def reply(self, prompt):
        self.prompts.append(prompt)
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="omlx 模型回复",
            details={"mode": "omlx_chat"},
        )

    def reply_with_context(self, question, **kwargs):
        self.context_calls.append({"question": question, **kwargs})
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="基于上下文的回复",
            details={"mode": "analysis_followup"},
        )


class FakeKnowledgeService:
    def __init__(self, search_hits=None):
        self.questions = []
        self.search_hits = list(search_hits or [])
        self.search_questions = []

    def should_handle(self, text):
        return "/kb" in text or "知识库" in text or "OTA信号" in text

    def search(self, query, *, limit=None):
        self.search_questions.append(query)
        return self.search_hits[: limit or len(self.search_hits)]

    def answer(self, question):
        self.questions.append(question)
        return TaskResult(
            success=True,
            message="SIGNAL_OTA_ST 四种组合指令\nadb shell am broadcast -a com.xiaopeng.guide.action.mock.datacenter --ei code 105003 --ei format 7 --es value \"[1, 2]\"",
            details={
                "mode": "knowledge_qa",
                "knowledge_hits": [
                    {
                        "source_id": "guideengine-signals",
                        "title": "SIGNAL_OTA_ST 定义",
                        "source_ref": "/repo/signal.proto",
                        "score": 9.0,
                    }
                ],
            },
        )


class FakeIntentRunner:
    def __init__(self, decisions=None, *, enabled=True):
        self.decisions = decisions or {}
        self.enabled = enabled
        self.calls = []

    def is_enabled(self):
        return self.enabled

    def classify(self, **kwargs):
        self.calls.append(kwargs)
        route_content = kwargs["route_content"]
        decision = self.decisions.get(route_content)
        if callable(decision):
            decision = decision(**kwargs)
        if decision is None:
            decision = IntentDecision(route="chat", reason="default test route", confidence="high")
        return decision


class FakeBugRunner:
    def __init__(self, metadata_path: Path, html_path: Path):
        self.metadata_path = metadata_path
        self.html_path = html_path
        self.requests = []
        self.analysis_calls = []
        self.reanalysis_calls = []
        self.agent_followup_calls = []
        self.progress_callbacks = []

    def run_bug_analysis(
        self,
        request,
        *,
        event=None,
        progress_callback=None,
        plans_override=None,
        classification_skill="",
        classification_source="",
        classification_reason="",
        classification_provider="",
    ):
        self.requests.append(request)
        self.analysis_calls.append(
            {
                "request": request,
                "plans_override": plans_override or [],
                "classification_skill": classification_skill,
                "classification_source": classification_source,
                "classification_reason": classification_reason,
                "classification_provider": classification_provider,
            }
        )
        self.progress_callbacks.append(progress_callback)
        if progress_callback is not None:
            progress_callback({"stage": "bug_fetch_data", "message": "拉取 bug 详情", "details": {"bug_url": request.bug_url}})
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="bug 分析完成",
            details={"mode": "bug_analysis", "files_to_send": [self.metadata_path, self.html_path]},
        )

    def selection_for_skill_name(self, skill_name, *, source, reason="", provider=""):
        mapping = {
            "xtheme-analyzer": ("xtheme", "XTheme时光主题分析"),
            "unity-startup-lifecycle-check": ("startup", "3D启动时序分析"),
            "3d-stuck-investigate": ("stuck", "3D卡顿分析"),
            "perception-data-summary": ("perception", "当前感知数据总结"),
        }
        if skill_name not in mapping:
            return None
        kind, label = mapping[skill_name]
        return BugAnalysisSelection(
            plans=[BugAnalysisPlan(kind=kind)],
            skill_name=skill_name,
            skill_label=label,
            source=source,
            reason=reason,
            provider=provider,
        )

    def supported_primary_bug_skills(self):
        return [
            {
                "name": "xtheme-analyzer",
                "kind": "xtheme",
                "label": "XTheme时光主题分析",
                "requires_logs": True,
                "role": "primary",
                "description": "分析主题切换、UI mode、日出日落等问题。",
            },
            {
                "name": "unity-startup-lifecycle-check",
                "kind": "startup",
                "label": "3D启动时序分析",
                "requires_logs": True,
                "role": "primary",
                "description": "分析 3D 启动、Surface、UnityReady、首帧生命周期。",
            },
            {
                "name": "3d-stuck-investigate",
                "kind": "stuck",
                "label": "3D卡顿分析",
                "requires_logs": True,
                "role": "primary",
                "description": "分析画面卡顿、黑屏、掉帧。",
            },
            {
                "name": "perception-data-summary",
                "kind": "perception",
                "label": "当前感知数据总结",
                "requires_logs": True,
                "role": "primary",
                "description": "分析感知数据、LD/SD 地图状态等问题。",
            },
        ]

    def classify_requests(self, *, prompt_text: str, title: str, description: str):
        if "感知数据" in prompt_text:
            return [BugAnalysisPlan(kind="perception")]
        if "信号链" in prompt_text or "VCU_ELECTRICIT_PERCENT" in prompt_text:
            return [BugAnalysisPlan(kind="signal")]
        if "启动和卡顿" in prompt_text or ("启动" in prompt_text and "卡顿" in prompt_text):
            return [BugAnalysisPlan(kind="startup")]
        if "场景信号" in prompt_text:
            return [BugAnalysisPlan(kind="scene_signal")]
        return [BugAnalysisPlan(kind="general")]

    def _skill_name_for_kind(self, kind: str) -> str:
        mapping = {
            "startup": "unity-startup-lifecycle-check",
            "stuck": "3d-stuck-investigate",
            "perception": "perception-data-summary",
            "signal": "signal-chain-analyzer",
            "xtheme": "xtheme-analyzer",
            "general": "general",
        }
        return mapping.get(kind, kind or "general")

    def _decide_source_analysis_request(self, *, request_text, prompt_text, title, description, plans, skill_name):
        merged = f"{request_text}\n{prompt_text}"
        requested = any(term in merged for term in ("源码", "源代码", "根据源码", "基于源码")) or "debug" in merged.lower()
        domain_kind = next((plan.kind for plan in plans if plan.kind != "general"), "general")
        source_mode = "off"
        if requested:
            source_mode = "standalone" if domain_kind == "general" else "append"
        stage_kinds = []
        if source_mode in {"off", "append"}:
            stage_kinds.append("domain")
        if source_mode in {"append", "standalone"}:
            stage_kinds.append("source")
        stage_kinds.append("summary")
        return SimpleNamespace(
            requested=requested,
            reason="测试源码分析诉求" if requested else "测试未命中源码诉求",
            source="test",
            domain_kind=domain_kind,
            source_mode=source_mode,
            context_profile=self._skill_name_for_kind(domain_kind) if domain_kind != "general" else "",
            stage_kinds=stage_kinds,
        )

    def classify_and_decide(self, *, request_text, prompt_text, title="", description="", plans=None):
        if plans is None:
            plans = self.classify_requests(prompt_text=prompt_text, title=title, description=description)
        first_plan = plans[0] if plans else BugAnalysisPlan(kind="general")
        skill_name = self._skill_name_for_kind(first_plan.kind) if first_plan.kind != "general" else ""
        source_decision = self._decide_source_analysis_request(
            request_text=request_text, prompt_text=prompt_text,
            title=title, description=description,
            plans=plans, skill_name=skill_name,
        )
        # Augment plans with source_stage if requested
        if source_decision.requested:
            if all(p.kind == "general" for p in plans):
                plans = [BugAnalysisPlan(kind="source_stage")]
            elif not any(p.kind == "source_stage" for p in plans):
                plans = [*plans, BugAnalysisPlan(kind="source_stage")]
        selection = BugAnalysisSelection(
            plans=plans,
            skill_name=skill_name or ("source_analysis" if source_decision.requested and all(p.kind == "source_stage" for p in plans) else "general"),
            skill_label="",
            source="preflight_rules",
            reason=source_decision.reason,
        )
        return UnifiedBugDecision(selection=selection, source_decision=source_decision)

    def run_direct_analysis(
        self,
        request,
        *,
        event=None,
        progress_callback=None,
        plans_override=None,
        classification_skill="",
        classification_source="",
        classification_reason="",
    ):
        self.requests.append(request)
        self.progress_callbacks.append(progress_callback)
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="直传文件分析完成",
            details={
                "mode": "direct_analysis",
                "files_to_send": [self.metadata_path, self.html_path],
                "analysis_skill": classification_skill,
                "classification_source": classification_source,
                "classification_reason": classification_reason,
                "plans_override": [plan.kind for plan in plans_override or []],
            },
        )

    def run_bug_reanalysis(self, **kwargs):
        self.reanalysis_calls.append(kwargs)
        progress_callback = kwargs.get("progress_callback")
        if progress_callback is not None:
            progress_callback({"stage": "bug_reanalysis_reuse_context", "message": "复用上下文"})
            progress_callback(
                {
                    "stage": "bug_agent_summary",
                    "message": "继续调用本地 Agent",
                    "details": {"session_id": "sess_123", "provider": "codex"},
                }
            )
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="bug 续聊重分析完成",
            job_id="job_reused",
            job_dir=self.html_path.parent.parent,
            details={"mode": "bug_reanalysis", "files_to_send": [self.html_path]},
        )

    def run_bug_agent_followup(self, **kwargs):
        self.agent_followup_calls.append(kwargs)
        progress_callback = kwargs.get("progress_callback")
        if progress_callback is not None:
            progress_callback(
                {
                    "stage": "bug_agent_followup_prepare",
                    "message": "继续调用本地 Agent",
                    "details": {"session_id": "sess_123", "provider": "codex"},
                }
            )
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="bug 智能体续聊完成",
            job_id="job_reused",
            job_dir=self.html_path.parent.parent,
            details={
                "mode": "bug_agent_followup",
                "agent_summary_session_id": "sess_123",
                "agent_summary_provider": "codex",
                "agent_summary_resumed": True,
            },
        )

    def decide_bug_followup(self, **kwargs):
        followup_text = str(kwargs.get("followup_text") or "")
        lowered = followup_text.casefold()
        if any(term in lowered for term in ("重新分析", "重跑", "再来一次", "重试", "时间点修正", "修正问题时间", "修复问题时间")):
            return BugFollowupSelection(
                should_reanalyze=True,
                force_rerun=True,
                plans=[],
                skill_name="",
                skill_label="",
                source="",
                reason="fake runner reanalysis",
                provider="",
            )
        if ("基于源码" in lowered or "根据源码" in lowered) and (
            "信号定义" in lowered or "vcu_electricit_percent" in lowered or "signal_" in lowered
        ):
            return BugFollowupSelection(
                should_reanalyze=True,
                force_rerun=True,
                plans=[],
                skill_name="",
                skill_label="",
                source="",
                reason="fake runner source-driven reanalysis",
                provider="",
            )
        return BugFollowupSelection(
            should_reanalyze=False,
            force_rerun=False,
            plans=[],
            skill_name="",
            skill_label="",
            source="",
            reason="fake runner agent followup",
            provider="",
        )


class FakePerceptionRunner:
    def __init__(self, html_path: Path):
        self.html_path = html_path
        self.requests = []

    def run_summary(self, request, *, event=None):
        self.requests.append(request)
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="感知数据总结完成",
            details={"mode": "perception_summary", "files_to_send": [self.html_path]},
        )


class FakeRomVersionRunner:
    def __init__(self):
        self.requests = []

    def run_lookup(self, request, *, event=None):
        self.requests.append(request)
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="ROM 版本查询完成\n导航版本: 6.2.2-test",
            details={
                "mode": "rom_version_lookup",
                "rom_version": request.rom_version,
                "required_outputs": {
                    "navigation_version": "V6.1.0_20260327175820_Release",
                    "symbol_table_url": (
                        "http://maven.xiaopeng.local/service/rest/repository/browse/"
                        "xp_android_release/com/xiaopeng/lib/envirodrive_so/V6.1.0_20260327175820_Release/"
                    ),
                    "napa5_download_url": "http://10.99.26.55/rom/napa/lib_napa5/6.1.0-test",
                },
            },
        )


class FailingRomVersionRunner:
    def __init__(self, *, message="ROM 版本查询失败", error_code="rom_version_lookup_failed"):
        self.requests = []
        self.message = message
        self.error_code = error_code

    def run_lookup(self, request, *, event=None):
        self.requests.append(request)
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=False,
            message=self.message,
            error_code=self.error_code,
            details={"mode": "rom_version_lookup", "rom_version": request.rom_version},
        )


class FakeAddr2LineRunner:
    def __init__(self):
        self.requests = []

    def run_resolve(self, request: Addr2LineRequest, *, event=None):
        self.requests.append(request)
        if request.error == "missing_symbol_version" or not (
            request.rom_version or request.napa_version or request.apk_version
        ):
            return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
                success=False,
                message="缺少符号表版本",
                error_code="missing_symbol_version",
                details={"mode": "addr2line_resolve"},
            )
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="addr2line 反解完成\n- libunity.so 0xf385e4 -> UnityFunc",
            details={"mode": "addr2line_resolve", "rom_version": request.rom_version},
        )


class FakeSignalHandler:
    def __init__(self):
        self.requests = []

    def handle(self, request: SignalRequest, *, event=None):
        self.requests.append(request)
        if not request.signal:
            return TaskResult(
                success=False,
                message="缺少 signal",
                error_code="missing_signal",
                details={
                    "mode": "signal_lifecycle",
                    "resources": [
                        {
                            "kind": item.kind,
                            "value": item.value,
                            "source_message_id": item.source_message_id,
                        }
                        for item in request.resources
                    ],
                },
            )
        if not request.resources:
            return TaskResult(
                success=False,
                message="缺少日志",
                error_code="missing_log",
                details={"mode": "signal_lifecycle"},
            )
        return TaskResult(
            success=True,
            message="信号分析完成",
            details={"mode": "signal_lifecycle"},
        )


def event(**overrides):
    values = {
        "event_id": "evt_1",
        "message_id": "om_1",
        "chat_id": "oc_denied",
        "chat_type": "group",
        "sender_id": "ou_1",
        "message_type": "text",
        "content": "调查 132002 信号链路 https://example.com/log.zip",
    }
    values.update(overrides)
    return LarkEvent(**values)


class _AppTestBase(unittest.TestCase):
    pass


__all__ = [
    'Path',
    'datetime',
    'timedelta',
    'timezone',
    'json',
    'os',
    'tempfile',
    'time',
    'unittest',
    'zipfile',
    'SimpleNamespace',
    'mock',
    'BridgeApp',
    'Addr2LineRunner',
    'BugAnalysisPlan',
    'BugAnalysisSelection',
    'BugFollowupSelection',
    'UnifiedBugDecision',
    'CommandResult',
    'Addr2LineRequest',
    'ApprovalOptions',
    'RealBridgeConfig',
    'ClaudeAgentOptions',
    'DualAgentOptions',
    'DownloadResource',
    'EventConsumerOptions',
    'IntentDecision',
    'KnowledgeOptions',
    'KnowledgeSourceOptions',
    'LarkEvent',
    'LarkOptions',
    'LocalResourceOptions',
    'NotificationOptions',
    'ReportServerOptions',
    'SignalRequest',
    'TaskResult',
    'WorkflowArchiveOptions',
    'SearchHit',
    'BridgeConfig',
    '_all_reply_message_ids',
    '_last_reply_message_id',
    'FakeLarkClient',
    'FakeClaudeRunner',
    'FakeOmlxChatClient',
    'FakeKnowledgeService',
    'FakeIntentRunner',
    'FakeBugRunner',
    'FakePerceptionRunner',
    'FakeRomVersionRunner',
    'FailingRomVersionRunner',
    'FakeAddr2LineRunner',
    'FakeSignalHandler',
    'event',
    '_AppTestBase',
]



if __name__ == "__main__":
    unittest.main()
