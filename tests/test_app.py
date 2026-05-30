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
        self.reanalysis_calls = []
        self.agent_followup_calls = []
        self.progress_callbacks = []

    def run_bug_analysis(self, request, *, event=None, progress_callback=None):
        self.requests.append(request)
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
            "startup": "3d-stuck-investigate",
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


class AppTests(unittest.TestCase):
    def test_progress_token_usage_normalizes_prompt_completion_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(BridgeConfig(data_dir=Path(tmp)))
            result = TaskResult(
                success=True,
                message="ok",
                details={
                    "agent_summary_prompt_tokens": 321,
                    "agent_summary_cached_input_tokens": 280,
                    "agent_summary_completion_tokens": 54,
                    "agent_summary_total_tokens": 375,
                },
            )

            usage = app._progress_token_usage(result)

        self.assertEqual(
            usage,
            {
                "input_tokens": 321,
                "cached_input_tokens": 280,
                "output_tokens": 54,
                "total_tokens": 375,
            },
        )

    def test_record_daemon_status_updates_health_monitor_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(BridgeConfig(data_dir=Path(tmp)))

            app.record_daemon_status({"stage": "event_consumer_ready", "process_id": os.getpid()})

            health = app.check()["health"]
        self.assertEqual(health["components"]["event_consumer"]["pid"], os.getpid())
        self.assertTrue(health["components"]["event_consumer"]["alive"])

    def test_health_monitor_restores_daemon_pid_from_saved_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(data_dir=Path(tmp))
            app = BridgeApp(config)
            app.record_daemon_status({"stage": "event_consumer_ready", "process_id": os.getpid()})

            restored = BridgeApp(config)
            health = restored.check()["health"]

        self.assertEqual(health["components"]["event_consumer"]["pid"], os.getpid())
        self.assertTrue(health["components"]["event_consumer"]["alive"])

    def test_health_maintenance_records_stuck_process_cleanup(self):
        class FakeWatchdog:
            def terminate_stuck(self):
                return [{"pid": 123, "name": "agent", "terminated": True, "idle_seconds": 99}]

        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(BridgeConfig(data_dir=Path(tmp)))
            app.process_watchdog = FakeWatchdog()

            cleaned = app.run_health_maintenance()
            session = app.activity_store.get_session("daemon")

        self.assertEqual(cleaned[0]["pid"], 123)
        self.assertEqual(session["progress"][0]["stage"], "stuck_process_cleanup")

    def test_default_runners_share_process_watchdog(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(BridgeConfig(data_dir=Path(tmp)))

            self.assertIs(app.claude_runner.process_watchdog, app.process_watchdog)
            self.assertIs(app.bug_runner.process_watchdog, app.process_watchdog)
            self.assertIs(app.perception_runner.process_watchdog, app.process_watchdog)
            self.assertIs(app.intent_runner.process_watchdog, app.process_watchdog)
            self.assertIs(app.handler.runner.process_watchdog, app.process_watchdog)

    def test_reanalysis_keeps_same_bug_report_version_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_chat = FakeOmlxChatClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )
            bug_url = "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722"

            first = app.handle_event(event(content=f"@bot {bug_url} 调查3D启动和卡顿"))
            followup = app.handle_event(
                event(
                    event_id="evt_reanalysis_version",
                    message_id="om_reanalysis_version",
                    content="@bot 重新分析，故障时间改成 11:30",
                    reply_to=first.details["conversation_root_message_id"],
                )
            )

        self.assertEqual(first.details["report_group_key"], f"bug:{bug_url}")
        self.assertEqual(followup.details["report_group_key"], f"bug:{bug_url}")
        self.assertEqual(followup.details["report_version"], 2)
        self.assertNotEqual(first.details["published_report_url"], followup.details["published_report_url"])
        self.assertTrue(first.details["published_report_url"].endswith("/v1/"))
        self.assertTrue(followup.details["published_report_url"].endswith("/v2/"))

    def test_report_ready_notification_pushes_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    notifications=NotificationOptions(enabled=True),
                ),
                lark_client=fake_lark,
                bug_runner=FakeBugRunner(metadata, html),
            )

            result = app.handle_event(
                event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动")
            )

        self.assertTrue(result.success)
        self.assertTrue(any("分析报告已生成" in item["text"] for item in fake_lark.sent))

    def test_report_ready_notification_dedup_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp) / "data",
                allowed_chats=["oc_denied"],
                notifications=NotificationOptions(enabled=True),
            )

            first_lark = FakeLarkClient()
            first_app = BridgeApp(config, lark_client=first_lark, bug_runner=FakeBugRunner(metadata, html))
            first_app.handle_event(
                event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动")
            )

            second_lark = FakeLarkClient()
            second_app = BridgeApp(config, lark_client=second_lark, bug_runner=FakeBugRunner(metadata, html))
            second_app.handle_event(
                event(
                    event_id="evt_restart_notification",
                    message_id="om_restart_notification",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动",
                )
            )

        self.assertTrue(any("分析报告已生成" in item["text"] for item in first_lark.sent))
        self.assertFalse(any("分析报告已生成" in item["text"] for item in second_lark.sent))

    def test_dual_agent_arbitration_is_added_when_secondary_summary_exists(self):
        class DualFakeBugRunner(FakeBugRunner):
            def run_bug_analysis(self, request, *, event=None, progress_callback=None):
                result = super().run_bug_analysis(request, event=event, progress_callback=progress_callback)
                result.message = "根因：网络超时"
                result.details["agent_summary_provider"] = "codex"
                result.details["secondary_agent_summary"] = "根因：内存泄漏"
                result.details["secondary_agent_provider"] = "claude"
                return result

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    dual_agent=DualAgentOptions(enabled=True),
                ),
                lark_client=FakeLarkClient(),
                bug_runner=DualFakeBugRunner(metadata, html),
            )

            result = app.handle_event(
                event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动")
            )

        self.assertIn("arbitration", result.details)
        self.assertIn("双 Agent 裁决", result.message)

    def test_bug_request_waits_for_card_approval_then_runs_after_approve_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    approval=ApprovalOptions(enabled=True),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            pending = app.handle_event(
                event(
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动和卡顿"
                )
            )
            self.assertFalse(pending.success)
            self.assertEqual(pending.error_code, "approval_pending")
            self.assertEqual(app.activity_store.get_session("om_1")["status"], "pending")
            self.assertEqual(len(fake_bug.requests), 0)
            self.assertEqual(len(fake_lark.cards), 1)

            import json
            card = json.loads(fake_lark.cards[0]["card_json"])
            request_id = card["elements"][-1]["actions"][0]["value"]["request_id"]
            approved = app.handle_card_action_payload(
                {
                    "event": {
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {"value": {"action": "approve", "request_id": request_id}},
                    }
                }
            )

        self.assertTrue(approved.success)
        self.assertEqual(approved.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)

    def test_expired_approval_action_does_not_run_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    approval=ApprovalOptions(enabled=True),
                ),
                lark_client=FakeLarkClient(),
                bug_runner=fake_bug,
            )
            app.approval_store.default_ttl_seconds = 0.01
            pending = app.handle_event(
                event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动")
            )
            time.sleep(0.02)

            approved = app.handle_card_action_payload(
                {
                    "event": {
                        "action": {
                            "value": {
                                "action": "approve",
                                "request_id": pending.details["approval_request_id"],
                            }
                        }
                    }
                }
            )

        self.assertFalse(approved.success)
        self.assertEqual(approved.error_code, "approval_not_available")
        self.assertEqual(len(fake_bug.requests), 0)

    def test_reject_card_action_does_not_run_pending_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    approval=ApprovalOptions(enabled=True),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )
            pending = app.handle_event(
                event(
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )
            request_id = pending.details["approval_request_id"]

            rejected = app.handle_card_action_payload(
                {"event": {"action": {"value": {"action": "reject", "request_id": request_id}}}}
            )

        self.assertFalse(rejected.success)
        self.assertEqual(rejected.error_code, "approval_rejected")
        self.assertEqual(len(fake_bug.requests), 0)

    def test_direct_analysis_waits_for_approval_then_runs_after_approve_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    approval=ApprovalOptions(enabled=True),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            pending = app.handle_event(event(content="@bot 分析启动和卡顿 file_log_123"))
            self.assertFalse(pending.success)
            self.assertEqual(pending.error_code, "approval_pending")
            self.assertEqual(len(fake_bug.requests), 0)

            approved = app.handle_card_action_payload(
                {
                    "event": {
                        "action": {
                            "value": {
                                "action": "approve",
                                "request_id": pending.details["approval_request_id"],
                            }
                        }
                    }
                }
            )

        self.assertTrue(approved.success)
        self.assertEqual(approved.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)

    def test_followup_reanalysis_waits_for_approval_then_runs_after_approve_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    approval=ApprovalOptions(enabled=True),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )
            app.conversation_store.remember(
                root_message_id="om_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="原始 bug 分析",
                summary_text="原始结论",
                report_url="http://report",
                report_excerpt="报告摘录",
            )

            pending = app.handle_event(
                event(
                    event_id="evt_reanalysis_pending",
                    message_id="om_followup",
                    reply_to="om_root",
                    content="@bot 重新分析",
                )
            )
            self.assertFalse(pending.success)
            self.assertEqual(pending.error_code, "approval_pending")
            self.assertEqual(len(fake_bug.reanalysis_calls), 0)

            approved = app.handle_card_action_payload(
                {
                    "event": {
                        "action": {
                            "value": {
                                "action": "approve",
                                "request_id": pending.details["approval_request_id"],
                            }
                        }
                    }
                }
            )

        self.assertTrue(approved.success)
        self.assertEqual(approved.details["mode"], "bug_reanalysis")
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)

    def test_card_reanalysis_rejects_missing_context_before_chat_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(BridgeConfig(data_dir=Path(tmp)))

            result = app.handle_card_action_payload(
                {
                    "event": {
                        "context": {"open_message_id": "om_card"},
                        "action": {
                            "value": {
                                "action": "reanalyze",
                                "root_message_id": "om_root",
                            }
                        },
                    }
                }
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_reanalysis_context")

    def test_card_reanalysis_resolves_context_via_job_id(self):
        class JobAwareBugRunner(FakeBugRunner):
            def run_bug_analysis(self, request, *, event=None, progress_callback=None):
                result = super().run_bug_analysis(request, event=event, progress_callback=progress_callback)
                result.job_id = "job_bug_1"
                return result

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = JobAwareBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                )
            )
            result = app.handle_card_action_payload(
                {
                    "header": {"event_id": "evt_card_reanalyze"},
                    "event": {
                        "context": {
                            "open_message_id": "om_card",
                            "open_chat_id": "oc_denied",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "reanalyze",
                                "job_id": "job_bug_1",
                            },
                            "form_value": {
                                "followup_prompt": "基于已下载日志和源码重新检查启动时序",
                            },
                        },
                    },
                }
            )

        self.assertTrue(first.success)
        self.assertTrue(result.success)
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        self.assertEqual(
            fake_bug.reanalysis_calls[0]["followup_text"],
            "基于已下载日志和源码重新检查启动时序",
        )
        self.assertEqual(result.details["mode"], "bug_reanalysis")

    def test_workflow_archive_failure_does_not_block_delivery(self):
        class FailingArchiver:
            def archive(self, *args, **kwargs):
                raise RuntimeError("archive permission denied")

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    workflow_archive=WorkflowArchiveOptions(enabled=True),
                ),
                lark_client=fake_lark,
                bug_runner=FakeBugRunner(metadata, html),
            )
            app.workflow_archiver = FailingArchiver()

            result = app.handle_event(
                event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动")
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["workflow_archive"]["error_type"], "RuntimeError")
        self.assertTrue(fake_lark.replies or fake_lark.card_replies)

    def test_bug_request_emits_progress_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            progress_events = []
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                progress_callback=progress_events.append,
            )

            result = app.handle_event(
                event(
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动和卡顿"
                )
            )

        self.assertTrue(result.success)
        self.assertGreaterEqual(len(progress_events), 4)
        self.assertEqual(progress_events[0]["stage"], "bug_request_received")
        self.assertEqual(progress_events[1]["stage"], "bug_fetch_data")
        self.assertEqual(progress_events[-1]["stage"], "file_uploaded")
        self.assertTrue(all(event.get("details", {}).get("executor") for event in progress_events))
        self.assertEqual(progress_events[0]["details"]["executor"], "Bridge 编排器")

    def test_bug_request_creates_updates_and_finalizes_progress_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
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
        self.assertTrue(any("飞书/Meegle CLI" in item["card_json"] for item in fake_lark.updated_cards))
        self.assertTrue(any("已完成" in item["card_json"] for item in fake_lark.updated_cards))

    def test_progress_live_url_uses_lan_ip_when_report_server_binds_all_interfaces(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "lark_agent_bridge.app.resolve_public_base_url"
        ) as resolve_url:
            resolve_url.return_value = "http://10.2.3.4:8765/reports"
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    report_server=ReportServerOptions(
                        enabled=True,
                        bind_host="0.0.0.0",
                        port=8765,
                        public_base_url="",
                    ),
                ),
                lark_client=FakeLarkClient(),
            )

            live_url = app._progress_live_url("om_1")

        self.assertEqual(live_url, "http://10.2.3.4:8765/sessions?session=om_1")
        resolve_url.assert_called_with("", port=8765, bind_host="0.0.0.0")

    def test_completed_progress_card_keeps_result_followup_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    event_consumer=EventConsumerOptions(event_key="card.action.trigger"),
                ),
                lark_client=fake_lark,
                bug_runner=FakeBugRunner(metadata, html),
            )

            result = app.handle_event(
                event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动")
            )

        self.assertTrue(result.success)
        self.assertGreaterEqual(len(fake_lark.updated_cards), 1)
        final_card = fake_lark.updated_cards[-1]["card_json"]
        self.assertIn("followup_prompt", final_card)
        self.assertIn("answer_from_report", final_card)
        self.assertIn("reanalyze", final_card)
        self.assertIn("continue_agent", final_card)
        self.assertIn("feedback_helpful", final_card)
        self.assertIn("feedback_unhelpful", final_card)

    def test_completed_progress_card_offers_alternate_agent_reanalysis_choices(self):
        class CodexBugRunner(FakeBugRunner):
            def run_bug_analysis(self, request, *, event=None, progress_callback=None):
                result = super().run_bug_analysis(request, event=event, progress_callback=progress_callback)
                result.details["agent_summary_provider"] = "codex"
                return result

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    event_consumer=EventConsumerOptions(event_key="card.action.trigger"),
                ),
                lark_client=fake_lark,
                bug_runner=CodexBugRunner(metadata, html),
            )

            app.handle_event(event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动"))

        final_card = fake_lark.updated_cards[-1]["card_json"]
        self.assertIn("select_bug_agent", final_card)
        self.assertIn("换 Claude 重分析", final_card)
        self.assertIn("用 OMLX 本地模型", final_card)
        self.assertIn('"agent_provider":"claude"', final_card)
        self.assertIn('"agent_provider":"omlx"', final_card)
        self.assertNotIn('"agent_provider":"codex"', final_card)

    def test_select_bug_agent_sends_confirmation_card_and_confirm_runs_selected_agent(self):
        class JobAwareBugRunner(FakeBugRunner):
            def run_bug_analysis(self, request, *, event=None, progress_callback=None):
                result = super().run_bug_analysis(request, event=event, progress_callback=progress_callback)
                result.job_id = "job_bug_1"
                result.details["agent_summary_provider"] = "codex"
                return result

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = JobAwareBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    event_consumer=EventConsumerOptions(event_key="card.action.trigger"),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )
            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动",
                )
            )

            selected = app.handle_card_action_payload(
                {
                    "header": {"event_id": "evt_select_agent"},
                    "event": {
                        "context": {
                            "open_message_id": "om_result_card",
                            "open_chat_id": "oc_denied",
                            "chat_type": "group",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "select_bug_agent",
                                "job_id": "job_bug_1",
                                "root_message_id": first.details["conversation_root_message_id"],
                                "agent_provider": "claude",
                            },
                            "form_value": {"followup_prompt": "换 Claude 重点看源码证据"},
                        },
                    },
                }
            )
            confirmed = app.handle_card_action_payload(
                {
                    "header": {"event_id": "evt_confirm_agent"},
                    "event": {
                        "context": {
                            "open_message_id": "om_confirm_card",
                            "open_chat_id": "oc_denied",
                            "chat_type": "group",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "confirm_bug_agent_reanalysis",
                                "job_id": "job_bug_1",
                                "root_message_id": first.details["conversation_root_message_id"],
                                "agent_provider": "claude",
                                "followup_text": "换 Claude 重点看源码证据",
                            }
                        },
                    },
                }
            )

        self.assertTrue(selected.success)
        self.assertTrue(any("confirm_bug_agent_reanalysis" in item["card_json"] for item in fake_lark.card_replies))
        self.assertTrue(confirmed.success)
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        self.assertEqual(fake_bug.reanalysis_calls[0]["agent_provider_override"], "claude")
        self.assertEqual(fake_bug.reanalysis_calls[0]["followup_text"], "换 Claude 重点看源码证据")

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
        self.assertTrue(any("已完成" in item["card_json"] for item in fake_lark.updated_cards))

    def test_progress_card_update_failure_falls_back_to_text_reply(self):
        class FailingUpdateLarkClient(FakeLarkClient):
            def update_card(self, message_id, card_json):
                self.updated_cards.append({"message_id": message_id, "card_json": card_json})
                return CommandResult(command=["update-card"], returncode=1, stderr="update failed")

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FailingUpdateLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=FakeBugRunner(metadata, html),
            )

            result = app.handle_event(
                event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动")
            )

        self.assertTrue(result.success)
        self.assertGreaterEqual(len(fake_lark.card_replies), 1)
        self.assertGreaterEqual(len(fake_lark.updated_cards), 1)
        self.assertTrue(any(reply["message_id"] == "om_1" for reply in fake_lark.replies))
        self.assertTrue(any("bug 分析完成" in reply["text"] for reply in fake_lark.replies))

    def test_stream_progress_card_updates_are_throttled(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
            )
            app._progress_card_stream_update_interval_seconds = 60.0
            evt = event(message_id="om_stream_progress", content="@bot 继续分析")

            app.send_status_card(
                evt,
                title="Bug 重新分析",
                status="analyzing",
                session_id="om_stream_progress",
            )
            self.assertEqual(len(fake_lark.card_replies), 1)

            app._notify_progress(
                "source_stage_agent_analysis_stream",
                "Codex app-server: Codex delta A",
                event=evt,
                session_id="om_stream_progress",
                provider="codex",
            )
            app._notify_progress(
                "source_stage_agent_analysis_stream",
                "Codex app-server: Codex delta B",
                event=evt,
                session_id="om_stream_progress",
                provider="codex",
            )
            self.assertEqual(len(fake_lark.updated_cards), 1)

            app._notify_progress(
                "bug_reanalysis_run_analysis",
                "基于已准备日志重新执行源码分析阶段",
                event=evt,
                session_id="om_stream_progress",
            )
            self.assertEqual(len(fake_lark.updated_cards), 2)

    def test_progress_card_send_failure_falls_back_to_received_text(self):
        class FailingCardLarkClient(FakeLarkClient):
            def reply_card(self, message_id, card_json):
                self.card_replies.append({"message_id": message_id, "card_json": card_json})
                return CommandResult(command=["reply-card"], returncode=1, stderr="card failed")

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FailingCardLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=FakeBugRunner(metadata, html),
            )

            result = app.handle_event(
                event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动")
            )

        self.assertTrue(result.success)
        self.assertGreaterEqual(len(fake_lark.replies), 1)
        self.assertIn("已收到", fake_lark.replies[0]["text"])
        self.assertTrue(any("status_card_send_failed" == item["stage"] for item in app.activity_store.get_session("om_1")["progress"]))

    def test_failed_progress_card_update_failure_falls_back_to_text_reply(self):
        class FailingUpdateLarkClient(FakeLarkClient):
            def update_card(self, message_id, card_json):
                self.updated_cards.append({"message_id": message_id, "card_json": card_json})
                return CommandResult(command=["update-card"], returncode=1, stderr="update failed")

        class FailingBugRunner(FakeBugRunner):
            def run_bug_analysis(self, request, *, event=None, progress_callback=None):
                self.requests.append(request)
                self.progress_callbacks.append(progress_callback)
                return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
                    success=False,
                    message="bug 分析失败",
                    error_code="bug_analysis_failed",
                    details={"mode": "bug_analysis"},
                )

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FailingUpdateLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=FailingBugRunner(metadata, html),
            )

            result = app.handle_event(
                event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动")
            )

        self.assertFalse(result.success)
        self.assertGreaterEqual(len(fake_lark.card_replies), 1)
        self.assertGreaterEqual(len(fake_lark.updated_cards), 1)
        self.assertTrue(any(reply["message_id"] == "om_1" for reply in fake_lark.replies))
        self.assertTrue(any("bug 分析失败" in reply["text"] for reply in fake_lark.replies))

    def test_non_analysis_request_outside_allowed_chat_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_allowed"],
                ),
                lark_client=fake_lark,
            )

            result = app.handle_event(event(content="@bot /chat 讲个笑话"))

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "chat_not_allowed")
        self.assertEqual(len(fake_lark.sent), 1)
        self.assertIn("当前群未加入允许列表", fake_lark.sent[0]["text"])

    def test_group_chat_is_allowed_by_default_without_chat_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=[],
                ),
                lark_client=FakeLarkClient(),
                chat_client=fake_chat,
            )

            result = app.handle_event(event(chat_id="oc_any_group", content="@bot /chat 讲个笑话"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])

    def test_bug_request_in_non_allowlisted_group_is_allowed_when_addressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_allowed"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    chat_id="oc_denied",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(_last_reply_message_id(fake_lark), "om_1")

    def test_unsupported_request_still_sends_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="bot"),
                ),
                lark_client=fake_lark,
            )

            result = app.handle_event(event(content="@bot 这条消息当前没有实现对应能力"))

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.message, "not a handled request")
        self.assertEqual(len(fake_lark.sent), 1)
        self.assertEqual(fake_lark.sent[0]["text"], "not a handled request")

    def test_intent_unsupported_reanalysis_without_context_prompts_for_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_intent = FakeIntentRunner(
                {
                    "重新分析": IntentDecision(
                        route="unsupported",
                        reason="没有上下文",
                        confidence="high",
                        followup_action="none",
                        context_source="none",
                    )
                }
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=[],
                ),
                lark_client=fake_lark,
                intent_runner=fake_intent,
            )

            result = app.handle_event(event(content="@bot 重新分析"))

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_followup_reply")
        self.assertIn("回复对应那条分析消息", result.message)
        self.assertEqual(fake_intent.calls, [])
        self.assertEqual(fake_lark.card_replies, [])
        self.assertEqual(fake_lark.updated_cards, [])
        self.assertTrue(any("回复对应那条分析消息" in item["text"] for item in fake_lark.sent))

    def test_reply_to_failed_bug_link_session_uses_activity_context_before_intent(self):
        class FailingIntentRunner(FakeIntentRunner):
            def classify(self, **kwargs):
                raise AssertionError("reply follow-up with recovered bug context should bypass intent classification")

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_lark.fetched_messages["om_followup"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_followup",
                                "reply_to": "om_failed_original",
                            }
                        ]
                    }
                }
            )
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=[],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                intent_runner=FailingIntentRunner(enabled=True),
            )
            original_event = event(
                event_id="evt_failed_original",
                message_id="om_failed_original",
                content=(
                    "@bot [ [缺陷] 【F01】车机大屏页面卡住-SB174577]"
                    "(https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593) 分析3D生命周期"
                ),
            )
            app.activity_store.record_event(original_event)
            failed_result = __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
                success=False,
                message="Claude Code skill 分析失败",
                job_id="job_failed_original",
                job_dir=Path(tmp) / "jobs" / "job_failed_original",
                error_code="claude_failed",
                details={"mode": "claude_skill"},
            )
            app.activity_store.record_result(original_event, failed_result)

            result = app.handle_event(
                event(
                    event_id="evt_followup_reanalysis",
                    message_id="om_followup",
                    content="@bot 重新分析",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_reanalysis")
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        call = fake_bug.reanalysis_calls[0]
        self.assertEqual(call["previous_context"].root_message_id, "om_failed_original")
        self.assertIn("分析3D生命周期", call["previous_context"].request_text)
        self.assertEqual(
            call["previous_session"]["details"]["bug_url"],
            "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593",
        )

    def test_reply_to_fetched_bug_link_message_without_local_session_starts_fresh_bug_analysis(self):
        class FailingIntentRunner(FakeIntentRunner):
            def classify(self, **kwargs):
                raise AssertionError("reply follow-up with fetched bug context should bypass intent classification")

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            original_message = {
                "message_id": "om_original_from_lark",
                "chat_id": "oc_denied",
                "content": (
                    "@bot [ [缺陷] 【F01】车机大屏页面卡住-SB174577]"
                    "(https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593) 分析3D生命周期"
                ),
            }
            fake_lark.fetched_messages["om_followup"] = json.dumps(
                {"data": {"messages": [{"message_id": "om_followup", "reply_to": "om_original_from_lark"}]}}
            )
            fake_lark.fetched_messages["om_original_from_lark"] = json.dumps(
                {"data": {"messages": [original_message]}}
            )
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=[],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                intent_runner=FailingIntentRunner(enabled=True),
            )

            result = app.handle_event(
                event(
                    event_id="evt_followup_fetched_reanalysis",
                    message_id="om_followup",
                    content="@bot 重新分析",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(
            fake_bug.requests[0].bug_url,
            "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593",
        )
        self.assertIn("分析3D生命周期", fake_bug.requests[0].prompt)
        self.assertIn("重新分析", fake_bug.requests[0].prompt)

    def test_claude_skill_request_is_unsupported_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_claude = FakeClaudeRunner(Path(tmp) / "skill-result.md")
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                claude_runner=fake_claude,
            )

            result = app.handle_event(event(content="@bot /skill 分析下这个 skill 场景"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "unsupported")
        self.assertEqual(fake_claude.requests, [])

    def test_claude_skill_request_sends_text_and_result_file_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "skill-result.md"
            artifact.write_text("结论", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_claude = FakeClaudeRunner(artifact)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    claude_agent=ClaudeAgentOptions(trigger_prefixes=["/skill"]),
                ),
                lark_client=fake_lark,
                claude_runner=fake_claude,
            )

            result = app.handle_event(event(content="@bot /skill 分析下这个 skill 场景"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "claude_skill")
        self.assertEqual(fake_claude.requests[0].prompt, "分析下这个 skill 场景")
        self.assertEqual(len(fake_lark.sent), 1)
        self.assertEqual(len(fake_lark.files), 1)
        self.assertEqual(fake_lark.files[0]["path"], artifact)

    def test_claude_skill_request_accepts_bot_name_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "skill-result.md"
            artifact.write_text("结论", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_claude = FakeClaudeRunner(artifact)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="Test Bot"),
                    claude_agent=ClaudeAgentOptions(trigger_prefixes=["/skill"]),
                ),
                lark_client=fake_lark,
                claude_runner=fake_claude,
            )

            result = app.handle_event(event(content="@Test Bot /skill 分析下这个 skill 场景"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "claude_skill")
        self.assertEqual(fake_claude.requests[0].prompt, "分析下这个 skill 场景")
        self.assertEqual(len(fake_lark.sent), 1)
        self.assertEqual(len(fake_lark.files), 1)

    def test_claude_skill_request_accepts_prefix_without_slash(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "skill-result.md"
            artifact.write_text("结论", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_claude = FakeClaudeRunner(artifact)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    claude_agent=ClaudeAgentOptions(trigger_prefixes=["skill"]),
                ),
                lark_client=fake_lark,
                claude_runner=fake_claude,
            )

            result = app.handle_event(event(content="@bot skill 分析下这个 skill 场景"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "claude_skill")
        self.assertEqual(fake_claude.requests[0].prompt, "分析下这个 skill 场景")

    def test_bug_request_replies_with_published_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )
            session = app.activity_store.get_session("om_1")

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(fake_bug.requests[0].bug_url, "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722")
        self.assertEqual(fake_bug.requests[0].prompt, "调查3D启动时序")
        self.assertIn("published_report_url", result.details)
        # Card or text reply should have been sent
        total_replies = len(fake_lark.replies) + len(fake_lark.card_replies)
        self.assertGreaterEqual(total_replies, 1)
        self.assertEqual(len(fake_lark.files), 1)
        self.assertEqual(Path(fake_lark.files[0]["path"]).resolve(), html.resolve())
        # If text reply was sent, check content; if card was sent, check card data
        if fake_lark.replies:
            self.assertTrue(fake_lark.replies[0]["text"].startswith('<at user_id="ou_1"></at> '))
            self.assertIn("报告链接：", fake_lark.replies[0]["text"])
        else:
            import json
            card_data = json.loads(fake_lark.card_replies[0]["card_json"])
            self.assertIn("header", card_data)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session["mode"], "bug_analysis")
        self.assertEqual(session["status"], "succeeded")
        self.assertEqual(session["report_url"], result.details["published_report_url"])
        self.assertTrue(any(item["stage"] == "bug_fetch_data" for item in session["progress"]))

    def test_6998811703_3D场景模式_shell_cleans_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    content=(
                        "@bot @朱云龙的飞书 CLI "
                        "[ [缺陷] 2026-05-25 16:50:41 【d03】6.2.3】拾光主题切换成天玑主题，进入场景模式没有展示3D场景]"
                        "(https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998811703) "
                        "分析 3D场景模式"
                    )
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(fake_bug.requests[0].bug_url, "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998811703")
        self.assertEqual(fake_bug.requests[0].prompt, "分析 3D场景模式")

    def test_simple_question_uses_omlx_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="bot"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            result = app.handle_event(
                event(
                    chat_id="ou_chat_1",
                    chat_type="p2p",
                    content="帮我解释一下什么是 token？",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.message, "omlx 模型回复")
        self.assertEqual(fake_chat.prompts, ["帮我解释一下什么是 token？"])
        self.assertIn("om_1", _all_reply_message_ids(fake_lark))

    def test_knowledge_question_uses_knowledge_card_before_omlx(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_knowledge = FakeKnowledgeService()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    knowledge=KnowledgeOptions(enabled=True),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                intent_runner=FakeIntentRunner(enabled=False),
                knowledge_service=fake_knowledge,
            )

            result = app.handle_event(
                event(
                    event_id="evt_kb",
                    message_id="om_kb",
                    chat_id="oc_p2p",
                    chat_type="p2p",
                    content="知识库 OTA信号如何模拟",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "knowledge_qa")
        self.assertEqual(fake_knowledge.questions, ["知识库 OTA信号如何模拟"])
        self.assertEqual(fake_chat.prompts, [])
        self.assertEqual(len(fake_lark.card_replies), 1)
        self.assertEqual(fake_lark.card_replies[0]["message_id"], "om_kb")
        self.assertIn("知识库回答", fake_lark.card_replies[0]["card_json"])
        self.assertIn("SIGNAL_OTA_ST 定义", fake_lark.card_replies[0]["card_json"])

    def test_group_knowledge_followup_in_reply_chain_without_mention(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_knowledge = FakeKnowledgeService()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    knowledge=KnowledgeOptions(enabled=True),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                intent_runner=FakeIntentRunner(enabled=False),
                knowledge_service=fake_knowledge,
            )
            app.conversation_store.remember(
                root_message_id="om_root_request",
                chat_id="oc_denied",
                mode="knowledge_qa",
                request_text="知识库 OTA信号如何模拟",
                summary_text="第一轮知识回答",
                report_url="",
                report_excerpt="",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_bot_kb_reply",
                root_message_id="om_root_request",
            )

            result = app.handle_event(
                event(
                    event_id="evt_group_followup_no_mention",
                    message_id="om_group_followup_no_mention",
                    reply_to="om_bot_kb_reply",
                    content="知识库 OTA信号如何模拟",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "knowledge_qa")
        self.assertEqual(fake_knowledge.questions[-1], "知识库 OTA信号如何模拟")
        self.assertEqual(len(fake_lark.card_replies), 1)
        self.assertEqual(fake_lark.card_replies[0]["message_id"], "om_group_followup_no_mention")

    def test_group_bug_followup_in_reply_chain_without_mention(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                summary_text="bug 分析完成",
                report_url="http://report",
                report_excerpt="SceneType=Main",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_bug_reply",
                root_message_id="om_bug_root",
            )

            followup = app.handle_event(
                event(
                    event_id="evt_group_bug_followup_no_mention",
                    message_id="om_group_bug_followup_no_mention",
                    reply_to="om_bug_reply",
                    content="问题时间是2026-05-11 23:12分左右",
                )
            )

        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(len(fake_bug.agent_followup_calls), 1)
        self.assertEqual(fake_bug.agent_followup_calls[0]["followup_text"], "问题时间是2026-05-11 23:12分左右")

    def test_group_reply_in_thread_without_replying_to_bot_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(Path(tmp) / "m.md", Path(tmp) / "r.html")
            (Path(tmp) / "m.md").write_text("bug", encoding="utf-8")
            (Path(tmp) / "r.html").write_text("<html></html>", encoding="utf-8")
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    lark=LarkOptions(bot_open_id="ou_bot", bot_name="bot"),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_thread_root",
                chat_id="oc_open",
                mode="bug_analysis",
                request_text="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查",
                summary_text="bug 分析完成",
                report_url="http://r",
                report_excerpt="",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_bot_reply",
                root_message_id="om_thread_root",
            )

            result = app.handle_event(
                event(
                    event_id="evt_reply_to_peer_in_thread",
                    message_id="om_reply_to_peer",
                    chat_id="oc_open",
                    reply_to="om_peer_msg",
                    root_id="om_thread_root",
                    parent_id="om_peer_msg",
                    content="@王忠华 M5MAX",
                )
            )

        self.assertTrue(result.skipped)
        self.assertEqual(result.details.get("mode"), "not_addressed")
        self.assertEqual(fake_bug.agent_followup_calls, [])
        self.assertEqual(fake_bug.requests, [])

    def test_group_reply_to_bot_root_without_mention_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(Path(tmp) / "m.md", Path(tmp) / "r.html")
            (Path(tmp) / "m.md").write_text("bug", encoding="utf-8")
            (Path(tmp) / "r.html").write_text("<html></html>", encoding="utf-8")
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    lark=LarkOptions(bot_open_id="ou_bot", bot_name="bot"),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_user_triggering",
                chat_id="oc_open",
                mode="bug_analysis",
                request_text="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查",
                summary_text="bug 分析完成",
                report_url="http://r",
                report_excerpt="",
            )

            result = app.handle_event(
                event(
                    event_id="evt_reply_to_user_triggering_msg",
                    message_id="om_reply_to_root",
                    chat_id="oc_open",
                    reply_to="om_user_triggering",
                    content="继续追问",
                )
            )

        self.assertTrue(result.skipped)
        self.assertEqual(result.details.get("mode"), "not_addressed")

    def test_group_bug_followup_retry_once_routes_to_bug_reanalysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_retry_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                summary_text="bug 分析完成",
                report_url="http://report",
                report_excerpt="SceneType=Main",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_bug_retry_reply",
                root_message_id="om_bug_retry_root",
            )
            original = event(
                event_id="evt_bug_retry_original",
                message_id="om_bug_retry_root",
                content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
            )
            app.activity_store.record_event(original)
            app.activity_store.record_result(
                original,
                TaskResult(
                    success=True,
                    message="bug 分析完成",
                    details={
                        "mode": "bug_analysis",
                        "conversation_root_message_id": "om_bug_retry_root",
                    },
                ),
            )

            followup = app.handle_event(
                event(
                    event_id="evt_group_bug_retry_once",
                    message_id="om_group_bug_retry_once",
                    reply_to="om_bug_retry_reply",
                    content="重试一次",
                )
            )

        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_reanalysis")
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        self.assertTrue(fake_bug.reanalysis_calls[0]["force_rerun"])

    def test_internal_operation_question_with_knowledge_hit_uses_knowledge_probe_before_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_knowledge = FakeKnowledgeService(
                search_hits=[
                    SearchHit(
                        chunk_id="power:1",
                        source_id="guideengine-runbook",
                        title="上下电模拟 runbook",
                        content="上下电模拟需要使用车机测试广播或台架电源流程。",
                        source_ref="/kb/power.md",
                        score=12.0,
                    )
                ]
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    knowledge=KnowledgeOptions(enabled=True),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                knowledge_service=fake_knowledge,
            )

            result = app.handle_event(event(content="@bot 上下电如何模拟"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "knowledge_qa")
        self.assertEqual(fake_knowledge.search_questions, ["上下电如何模拟"])
        self.assertEqual(fake_knowledge.questions, ["上下电如何模拟"])
        self.assertEqual(fake_chat.prompts, [])
        self.assertEqual(len(fake_lark.card_replies), 1)

    def test_power_cycle_question_uses_real_knowledge_answer_and_records_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    knowledge=KnowledgeOptions(enabled=True, storage=Path(tmp) / "knowledge.sqlite"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                intent_runner=FakeIntentRunner(enabled=False),
            )

            result = app.handle_event(event(content="@bot 上下电如何模拟"))
            hits = app.knowledge_service.search("上下电如何模拟")

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "knowledge_qa")
        self.assertIn("SIGNAL_MCU_IG_ST 上下电模拟指令", result.message)
        self.assertIn("--ei code 36001 --ei format 3 --es value 1", result.message)
        self.assertEqual(hits[0].source_id, "derived-adb-simulations")
        self.assertEqual(fake_chat.prompts, [])
        self.assertEqual(len(fake_lark.card_replies), 1)

    def test_internal_operation_question_without_knowledge_hit_does_not_fall_back_to_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_knowledge = FakeKnowledgeService(search_hits=[])
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    knowledge=KnowledgeOptions(enabled=True),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                knowledge_service=fake_knowledge,
            )

            result = app.handle_event(event(content="@bot 上下电如何模拟"))

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "knowledge_probe_no_hits")
        self.assertEqual(result.details["mode"], "knowledge_probe")
        self.assertEqual(fake_knowledge.search_questions, ["上下电如何模拟"])
        self.assertEqual(fake_knowledge.questions, [])
        self.assertEqual(fake_chat.prompts, [])
        self.assertEqual(fake_lark.replies[0]["message_id"], "om_1")
        self.assertIn("知识库未命中", fake_lark.replies[0]["text"])

    def test_knowledge_probe_can_reply_with_low_confidence_command_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adb_path = root / "adb_data.json"
            adb_path.write_text(
                json.dumps(
                    {
                        "commands": [
                            {
                                "name": "直接发送文本给小P",
                                "command": (
                                    "adb shell am broadcast -a carspeechservice.ACTION_SEND_TEXT "
                                    "--es text \"打开车窗\" --ei soundArea 2"
                                ),
                                "group": "语音",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=root,
                    allowed_chats=["oc_denied"],
                    knowledge=KnowledgeOptions(
                        enabled=True,
                        storage=root / "knowledge.sqlite",
                        sources=[
                            KnowledgeSourceOptions(
                                id="guideengine-adb",
                                type="local_json",
                                path=str(adb_path),
                            )
                        ],
                    ),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                intent_runner=FakeIntentRunner(enabled=False),
            )

            result = app.handle_event(event(content="@bot 车窗如何模拟"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "knowledge_qa")
        self.assertEqual(result.details["answer_type"], "low_confidence_candidates")
        self.assertIn("carspeechservice.ACTION_SEND_TEXT", result.message)
        self.assertEqual(fake_chat.prompts, [])
        self.assertEqual(len(fake_lark.card_replies), 1)
        self.assertIn("低置信候选", fake_lark.card_replies[0]["card_json"])

    def test_unrelated_chat_does_not_probe_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_knowledge = FakeKnowledgeService(
                search_hits=[
                    SearchHit(
                        chunk_id="power:1",
                        source_id="guideengine-runbook",
                        title="上下电模拟 runbook",
                        content="上下电模拟流程。",
                        score=12.0,
                    )
                ]
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    knowledge=KnowledgeOptions(enabled=True),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                knowledge_service=fake_knowledge,
            )

            result = app.handle_event(event(content="@bot /chat 讲个笑话"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_knowledge.search_questions, [])
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])

    def test_broad_how_question_does_not_probe_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_knowledge = FakeKnowledgeService(
                search_hits=[
                    SearchHit(
                        chunk_id="doc:1",
                        source_id="guideengine-runbook",
                        title="日报模板",
                        content="日报模板示例。",
                        score=12.0,
                    )
                ]
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    knowledge=KnowledgeOptions(enabled=True),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                knowledge_service=fake_knowledge,
            )

            result = app.handle_event(event(content="@bot 怎么写日报"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_knowledge.search_questions, [])
        self.assertEqual(fake_chat.prompts, ["怎么写日报"])

    def test_internal_build_data_question_uses_knowledge_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_knowledge = FakeKnowledgeService(
                search_hits=[
                    SearchHit(
                        chunk_id="mock:1",
                        source_id="guideengine-runbook",
                        title="点火状态模拟",
                        content="点火状态可通过已验证模板模拟。",
                        score=12.0,
                    )
                ]
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    knowledge=KnowledgeOptions(enabled=True),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                knowledge_service=fake_knowledge,
            )

            result = app.handle_event(event(content="@bot 点火状态怎么造"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "knowledge_qa")
        self.assertEqual(fake_knowledge.search_questions, ["点火状态怎么造"])
        self.assertEqual(fake_chat.prompts, [])

    def test_help_request_replies_as_plain_text_without_intent_or_card(self):
        class FailingIntentRunner(FakeIntentRunner):
            def classify(self, **kwargs):
                raise AssertionError("help should be answered locally")

        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                intent_runner=FailingIntentRunner(enabled=True),
            )

            result = app.handle_event(event(content="@bot help"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "basic_chat")
        self.assertEqual(fake_lark.card_replies, [])
        self.assertEqual(fake_lark.updated_cards, [])
        self.assertEqual(len(fake_lark.replies), 1)
        self.assertEqual(fake_lark.replies[0]["message_id"], "om_1")
        self.assertIn("常用触发方式", fake_lark.replies[0]["text"])
        self.assertIn("| Bug 分析 |", fake_lark.replies[0]["text"])

    def test_stale_help_replayed_before_listener_ready_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    event_consumer=EventConsumerOptions(
                        drop_stale_light_interactions=True,
                        stale_light_interaction_grace_seconds=60,
                    ),
                ),
                lark_client=fake_lark,
                intent_runner=FakeIntentRunner(enabled=True),
            )
            app.record_daemon_status({"stage": "event_consumer_ready", "ready": True})
            old_create_time = int((datetime.now(timezone.utc) - timedelta(minutes=5)).timestamp() * 1000)

            result = app.handle_event(
                event(
                    event_id="evt_stale_help",
                    message_id="om_stale_help",
                    content="@bot help",
                    create_time=str(old_create_time),
                )
            )

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "stale_light_interaction")
        self.assertEqual(fake_lark.sent, [])
        self.assertEqual(fake_lark.replies, [])
        self.assertEqual(fake_lark.card_replies, [])

    def test_stale_bug_request_is_not_dropped_by_light_interaction_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            metadata = tmp_path / "metadata.md"
            html = tmp_path / "report.html"
            metadata.write_text("metadata", encoding="utf-8")
            html.write_text("<html>report</html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            bug_runner = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=tmp_path,
                    allowed_chats=["oc_denied"],
                    event_consumer=EventConsumerOptions(
                        drop_stale_light_interactions=True,
                        stale_light_interaction_grace_seconds=60,
                    ),
                ),
                lark_client=fake_lark,
                bug_runner=bug_runner,
            )
            app.record_daemon_status({"stage": "event_consumer_ready", "ready": True})
            old_create_time = int((datetime.now(timezone.utc) - timedelta(minutes=5)).timestamp() * 1000)

            result = app.handle_event(
                event(
                    event_id="evt_stale_bug",
                    message_id="om_stale_bug",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6991604970 分析主题变化",
                    create_time=str(old_create_time),
                )
            )

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(len(bug_runner.requests), 1)
        self.assertEqual(result.details["mode"], "bug_analysis")

    def test_identity_request_replies_as_plain_text_without_intent_or_card(self):
        class FailingIntentRunner(FakeIntentRunner):
            def classify(self, **kwargs):
                raise AssertionError("identity should be answered locally")

        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                intent_runner=FailingIntentRunner(enabled=True),
            )

            result = app.handle_event(event(content="@bot 你是谁"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "basic_chat")
        self.assertEqual(fake_lark.card_replies, [])
        self.assertEqual(fake_lark.updated_cards, [])
        self.assertEqual(len(fake_lark.replies), 1)
        self.assertEqual(fake_lark.replies[0]["message_id"], "om_1")
        self.assertIn("Lark Agent Bridge", fake_lark.replies[0]["text"])

    def test_agent_intent_routes_simple_question_to_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_intent = FakeIntentRunner(
                {
                    "帮我解释一下什么是 token？": IntentDecision(
                        route="chat",
                        reason="普通聊天提问",
                        confidence="high",
                        followup_action="none",
                        context_source="none",
                    )
                }
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                intent_runner=fake_intent,
            )

            result = app.handle_event(
                event(
                    chat_id="ou_chat_1",
                    chat_type="p2p",
                    content="帮我解释一下什么是 token？",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_chat.prompts, ["帮我解释一下什么是 token？"])
        self.assertEqual(fake_intent.calls, [])

    def test_super_user_in_non_allowlisted_group_uses_intent_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_intent = FakeIntentRunner(
                {
                    "帮我解释一下什么是 token？": IntentDecision(
                        route="chat",
                        reason="super user addressed chat",
                        confidence="high",
                        followup_action="none",
                        context_source="none",
                    )
                }
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_allowed"],
                    allowed_users=["ou_super"],
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                intent_runner=fake_intent,
            )

            result = app.handle_event(
                event(
                    chat_id="oc_denied",
                    sender_id="ou_super",
                    content="@bot 帮我解释一下什么是 token？",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_chat.prompts, ["帮我解释一下什么是 token？"])
        self.assertEqual(fake_intent.calls, [])

    def test_perception_summary_request_sends_html_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "perception-summary.html"
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_runner = FakePerceptionRunner(html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                perception_runner=fake_runner,
            )

            result = app.handle_event(event(content="@bot 总结当前感知数据"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "perception_summary")
        self.assertEqual(fake_runner.requests[0].prompt, "总结当前感知数据")
        total_replies = len(fake_lark.replies) + len(fake_lark.card_replies)
        self.assertEqual(total_replies, 1)
        self.assertEqual(len(fake_lark.files), 1)
        self.assertEqual(Path(fake_lark.files[0]["path"]).resolve(), html.resolve())
        self.assertIn("published_report_url", result.details)

    def test_intent_perception_reply_to_file_fetches_reply_resource_when_event_lacks_reply_to(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "perception-summary.html"
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_lark.fetched_messages["om_current"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_current",
                                "content": "@bot 你也查下这个现状",
                                "reply_to": "om_file_msg",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "msg_type": "file",
                                "content": '<file key="file_v3_0011s_6d5d723c-ec0b-44f3-9908-a02be496b54g" name="Log.zip"/>',
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_runner = FakePerceptionRunner(html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                perception_runner=fake_runner,
                intent_runner=FakeIntentRunner(
                    {
                        "你也查下这个现状": IntentDecision(
                            route="perception_summary",
                            reason="用户要求基于被回复日志查看现状",
                            confidence="high",
                        )
                    }
                ),
            )

            result = app.handle_event(event(message_id="om_current", content="@bot 你也查下这个现状"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "perception_summary")
        self.assertEqual(len(fake_runner.requests), 1)
        self.assertEqual(len(fake_runner.requests[0].resources), 1)
        self.assertEqual(fake_runner.requests[0].resources[0].kind, "file")
        self.assertEqual(fake_runner.requests[0].resources[0].source_message_id, "om_file_msg")
        self.assertEqual(
            fake_runner.requests[0].resources[0].value,
            "file_v3_0011s_6d5d723c-ec0b-44f3-9908-a02be496b54g",
        )

    def test_perception_followup_recovers_original_file_from_reply_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "perception-summary.html"
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_lark.fetched_messages["om_current"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_current",
                                "reply_to": "om_previous_text",
                            }
                        ]
                    }
                }
            )
            fake_lark.fetched_messages["om_previous_text"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_previous_text",
                                "reply_to": "om_file_msg",
                                "content": {"text": "上一轮感知数据总结"},
                            }
                        ]
                    }
                }
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": {"file_key": "file_perception_zip"},
                            }
                        ]
                    }
                }
            )
            fake_runner = FakePerceptionRunner(html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                perception_runner=fake_runner,
            )

            result = app.handle_event(
                event(
                    event_id="evt_perception_followup",
                    message_id="om_current",
                    content="@bot 总结当前感知数据",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "perception_summary")
        self.assertEqual(len(fake_runner.requests), 1)
        resources = fake_runner.requests[0].resources
        self.assertEqual(resources[0].kind, "file")
        self.assertEqual(resources[0].value, "file_perception_zip")
        self.assertEqual(resources[0].source_message_id, "om_file_msg")

    def test_direct_analysis_request_with_file_routes_to_bug_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(event(content="@bot 分析启动和卡顿 file_abc123 11:30"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertIn("published_report_url", result.details)
        total_replies = len(fake_lark.replies) + len(fake_lark.card_replies)
        self.assertEqual(total_replies, 1)
        self.assertEqual(len(fake_lark.files), 1)
        self.assertEqual(Path(fake_lark.files[0]["path"]).resolve(), html.resolve())

    def test_direct_analysis_with_explicit_source_clue_sends_preflight_card_then_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    event_id="evt_direct_source_preflight",
                    message_id="om_direct_source_preflight",
                    content="@bot 基于SRViolationHandler.kt源码分析 时间点2026-05-22 07:46 分析超速状态 file_abc123",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertGreaterEqual(len(fake_lark.card_replies), 1)
        initial_card = fake_lark.card_replies[0]["card_json"]
        self.assertIn("意图分析", initial_card)
        self.assertIn("高置信度", initial_card)
        self.assertIn("源码导向文件分析", initial_card)

    def test_generic_file_request_returns_clarification_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(Path(tmp) / "analysis.md", Path(tmp) / "analysis.html")
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    event_id="evt_direct_need_direction",
                    message_id="om_direct_need_direction",
                    content="@bot 时间点2026-05-22 07:46 调查3D生命周期 file_abc123",
                )
            )

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "bug_clarification")
        self.assertTrue(result.details["needs_user_direction"])
        self.assertEqual(fake_bug.requests, [])
        self.assertIn("直接源码分析", result.message)
        self.assertIn("1.", result.message)
        self.assertGreaterEqual(len(fake_lark.card_replies), 1)
        self.assertIn("意图分析", fake_lark.card_replies[0]["card_json"])

    def test_direct_analysis_followup_recovers_original_file_from_reply_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_current"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_current",
                                "reply_to": "om_previous_text",
                            }
                        ]
                    }
                }
            )
            fake_lark.fetched_messages["om_previous_text"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_previous_text",
                                "reply_to": "om_file_msg",
                                "content": {"text": "上一轮直传分析"},
                            }
                        ]
                    }
                }
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": {
                                    "file_key": "file_direct_zip",
                                    "file_name": "L1NSPGHB3SB010669log0.zip",
                                },
                            }
                        ]
                    }
                }
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    event_id="evt_direct_followup",
                    message_id="om_current",
                    content="@bot 分析启动和卡顿",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        resources = fake_bug.requests[0].resources
        self.assertEqual(resources[0].kind, "file")
        self.assertEqual(resources[0].value, "file_direct_zip")
        self.assertEqual(resources[0].source_message_id, "om_file_msg")
        self.assertEqual(resources[0].display_name, "L1NSPGHB3SB010669log0.zip")

    def test_bug_clarification_reply_direct_source_analysis_recovers_direct_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            fake_lark.fetched_messages["om_followup_choice"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_followup_choice",
                                "reply_to": "om_clarification_root",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_clarification_root"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_clarification_root",
                                "reply_to": "om_file_msg",
                                "content": "@bot 时间点2026-05-22 07:46 调查3D生命周期",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "msg_type": "file",
                                "content": '<file key="file_v3_0011v_33d1772b-86ac-4789-b6e1-35c77ec29a5g" name="L1NSPGHB3SB010669log0.zip"/>',
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
            )
            clarification_event = event(
                event_id="evt_clarification_root",
                message_id="om_clarification_root",
                content="@bot 时间点2026-05-22 07:46 调查3D生命周期",
            )
            app.activity_store.record_event(clarification_event)
            app.activity_store.record_result(
                clarification_event,
                TaskResult(
                    success=True,
                    message="当前未自动命中专用 Skill。\n1. 3D启动时序分析\n2. 当前感知数据总结\n3. 直接源码分析",
                    details={
                        "mode": "bug_clarification",
                        "conversation_root_message_id": "om_clarification_root",
                        "user_request_text": "时间点2026-05-22 07:46 调查3D生命周期",
                        "needs_user_direction": True,
                        "intent_options": [
                            {"index": 1, "type": "skill", "skill_name": "3d-stuck-investigate", "label": "3D启动时序分析"},
                            {"index": 2, "type": "skill", "skill_name": "perception-data-summary", "label": "当前感知数据总结"},
                            {"index": 3, "type": "source_analysis", "label": "直接源码分析"},
                        ],
                    },
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_followup_choice",
                    message_id="om_followup_choice",
                    content="@bot 直接源码分析",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_chat.context_calls, [])

    def test_bug_clarification_reply_numeric_choice_recovers_direct_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_followup_choice"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_followup_choice",
                                "reply_to": "om_clarification_root",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_clarification_root"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_clarification_root",
                                "reply_to": "om_file_msg",
                                "content": "@bot 时间点2026-05-22 07:46 调查3D生命周期",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "msg_type": "file",
                                "content": '<file key="file_v3_0011v_33d1772b-86ac-4789-b6e1-35c77ec29a5g" name="L1NSPGHB3SB010669log0.zip"/>',
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )
            clarification_event = event(
                event_id="evt_clarification_root",
                message_id="om_clarification_root",
                content="@bot 时间点2026-05-22 07:46 调查3D生命周期",
            )
            app.activity_store.record_event(clarification_event)
            app.activity_store.record_result(
                clarification_event,
                TaskResult(
                    success=True,
                    message="当前未自动命中专用 Skill。\n1. 3D启动时序分析\n2. 当前感知数据总结\n3. 直接源码分析",
                    details={
                        "mode": "bug_clarification",
                        "conversation_root_message_id": "om_clarification_root",
                        "user_request_text": "时间点2026-05-22 07:46 调查3D生命周期",
                        "needs_user_direction": True,
                        "intent_options": [
                            {"index": 1, "type": "skill", "skill_name": "3d-stuck-investigate", "label": "3D启动时序分析"},
                            {"index": 2, "type": "skill", "skill_name": "perception-data-summary", "label": "当前感知数据总结"},
                            {"index": 3, "type": "source_analysis", "label": "直接源码分析"},
                        ],
                    },
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_followup_choice",
                    message_id="om_followup_choice",
                    content="@bot 1",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)

    def test_direct_analysis_can_use_authorized_download_dir_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            downloads = Path(tmp) / "downloads"
            local_log = downloads / "Log.zip"
            downloads.mkdir()
            local_log.write_text("log", encoding="utf-8")
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            route_content = "日志我下载到服务器的下载目录了 Log.zip 基于这个日志分析"
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp) / "data",
                    allowed_chats=["oc_denied"],
                    allowed_users=["ou_me"],
                    local_resources=LocalResourceOptions(allowed_dirs=[downloads]),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                intent_runner=FakeIntentRunner(
                    {
                        route_content: IntentDecision(
                            route="direct_analysis",
                            reason="用户明确授权使用下载目录文件",
                            confidence="high",
                        )
                    }
                ),
            )

            result = app.handle_event(event(content=f"@bot {route_content}", sender_id="ou_me"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].resources[0].kind, "local")
        self.assertEqual(Path(fake_bug.requests[0].resources[0].value), local_log.resolve())

    def test_direct_analysis_rejects_download_dir_file_from_non_allowed_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "downloads"
            downloads.mkdir()
            (downloads / "Log.zip").write_text("log", encoding="utf-8")
            route_content = "日志我下载到服务器的下载目录了 Log.zip 基于这个日志分析"
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp) / "data",
                    allowed_chats=["oc_denied"],
                    allowed_users=["ou_me"],
                    local_resources=LocalResourceOptions(allowed_dirs=[downloads]),
                ),
                lark_client=FakeLarkClient(),
                intent_runner=FakeIntentRunner(
                    {
                        route_content: IntentDecision(
                            route="direct_analysis",
                            reason="用户明确授权使用下载目录文件",
                            confidence="high",
                        )
                    }
                ),
            )

            result = app.handle_event(event(content=f"@bot {route_content}", sender_id="ou_other"))

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_log")

    def test_signal_followup_walks_reply_chain_to_find_prepared_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            (log_dir / "main.log").write_text("05-20 12:00:00 signal 132002", encoding="utf-8")
            route_content = "那就看132002 信号吧"
            fake_lark = FakeLarkClient()
            fake_lark.fetched_messages["om_signal_b"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_signal_b",
                                "reply_to": "om_bug_a",
                            }
                        ]
                    }
                }
            )
            fake_handler = FakeSignalHandler()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp) / "data",
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                handler=fake_handler,
                intent_runner=FakeIntentRunner(
                    {
                        route_content: IntentDecision(
                            route="signal",
                            reason="继续查看具体信号",
                            confidence="high",
                        )
                    }
                ),
            )
            original_event = event(
                event_id="evt_bug_original",
                message_id="om_bug_a",
                content="@bot bug 分析完成",
            )
            app.activity_store.record_event(original_event)
            app.activity_store.record_result(
                original_event,
                TaskResult(
                    success=True,
                    message="上一轮分析完成",
                    details={
                        "mode": "bug_reanalysis",
                        "prepared_log_input": str(log_dir),
                        "selected_log_input": str(log_dir),
                    },
                ),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_a",
                chat_id="oc_denied",
                mode="bug_reanalysis",
                request_text="上一轮 bug 分析",
                summary_text="已有日志",
                report_url="",
                report_excerpt="",
            )

            result = app.handle_event(
                event(
                    event_id="evt_signal_followup",
                    message_id="om_signal_c",
                    reply_to="om_signal_b",
                    content=f"@bot {route_content}",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(len(fake_handler.requests), 1)
        resources = fake_handler.requests[0].resources
        self.assertEqual(resources[0].kind, "local")
        self.assertEqual(Path(resources[0].value), log_dir.resolve())

    def test_rom_version_lookup_preempts_signal_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            fake_handler = FakeSignalHandler()
            text = (
                "@bot XMARTM3EUD03E5_V6.2.2.6808_20260424002644.3_REV01_USERDEBUG "
                "调用rom-version skill 找下导航版本"
            )
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                handler=fake_handler,
                rom_version_runner=fake_rom,
            )

            result = app.handle_event(event(content=text))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "rom_version_lookup")
        self.assertEqual(len(fake_rom.requests), 1)
        self.assertEqual(fake_handler.requests, [])
        self.assertEqual(len(fake_lark.replies), 1)
        self.assertEqual(fake_lark.replies[0]["message_id"], "om_1")
        self.assertIn("导航版本", fake_lark.replies[0]["text"])

    def test_addr2line_request_routes_to_runner_with_rom(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            fake_addr2line = FakeAddr2LineRunner()
            text = (
                "@bot XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release 反解地址\n"
                "#05 pc 0000000000f385e4 /system/app/xp_envirodrive/lib/arm64/libunity.so"
            )
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"], lark=LarkOptions(bot_name="bot")),
                lark_client=fake_lark,
                rom_version_runner=fake_rom,
                addr2line_runner=fake_addr2line,
            )

            result = app.handle_event(event(content=text))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "addr2line_resolve")
        self.assertEqual(len(fake_addr2line.requests), 1)
        self.assertIn("libunity.so", fake_addr2line.requests[0].addr_text)
        self.assertEqual(
            fake_addr2line.requests[0].rom_version,
            "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
        )
        self.assertEqual(fake_addr2line.requests[0].apk_version, "V6.1.0_20260327175820_Release")

    def test_rom_plus_symbol_table_phrase_without_stack_routes_to_rom_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            fake_addr2line = FakeAddr2LineRunner()
            rom = "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release"
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"], lark=LarkOptions(bot_name="bot")),
                lark_client=fake_lark,
                rom_version_runner=fake_rom,
                addr2line_runner=fake_addr2line,
            )

            result = app.handle_event(
                event(
                    content=f"@bot ROM版本号{rom} 反解导航符号表"
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "rom_version_lookup")
        self.assertEqual(len(fake_rom.requests), 1)
        self.assertEqual(len(fake_addr2line.requests), 0)
        self.assertIn("导航版本", fake_lark.replies[-1]["text"])

    def test_addr2line_request_resolves_navigation_version_before_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            fake_addr2line = FakeAddr2LineRunner()
            rom = "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release"
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"], lark=LarkOptions(bot_name="bot")),
                lark_client=fake_lark,
                rom_version_runner=fake_rom,
                addr2line_runner=fake_addr2line,
            )

            result = app.handle_event(
                event(
                    content=(
                        f"@bot ROM版本号{rom} 反解导航符号表\n"
                        "#05 pc 0000000000f385e4 /system/app/xp_envirodrive/lib/arm64/libunity.so"
                    )
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "addr2line_resolve")
        self.assertEqual(len(fake_rom.requests), 1)
        self.assertEqual(len(fake_addr2line.requests), 1)
        self.assertEqual(fake_addr2line.requests[0].rom_version, rom)
        self.assertEqual(fake_addr2line.requests[0].apk_version, "V6.1.0_20260327175820_Release")

    def test_addr2line_request_fails_when_rom_lookup_cannot_resolve_navigation_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            failing_rom = FailingRomVersionRunner(message="SCM 查询失败")
            fake_addr2line = FakeAddr2LineRunner()
            rom = "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release"
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"], lark=LarkOptions(bot_name="bot")),
                lark_client=fake_lark,
                rom_version_runner=failing_rom,
                addr2line_runner=fake_addr2line,
            )

            result = app.handle_event(
                event(
                    content=(
                        f"@bot ROM版本号{rom} 反解导航符号表\n"
                        "#05 pc 0000000000f385e4 /system/app/xp_envirodrive/lib/arm64/libunity.so"
                    )
                )
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "addr2line_symbol_version_lookup_failed")
        self.assertEqual(len(failing_rom.requests), 1)
        self.assertEqual(len(fake_addr2line.requests), 0)
        self.assertIn("SCM 查询失败", result.message)

    def test_addr2line_request_prefers_recent_navigation_version_for_same_rom(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            fake_addr2line = FakeAddr2LineRunner()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"], lark=LarkOptions(bot_name="bot")),
                lark_client=fake_lark,
                rom_version_runner=fake_rom,
                addr2line_runner=fake_addr2line,
            )
            rom = "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release"
            app.handle_event(
                event(
                    event_id="evt_rom",
                    message_id="om_rom",
                    content=f"@bot ROM版本号{rom} 找下导航版本",
                )
            )

            result = app.handle_event(
                event(
                    event_id="evt_stack",
                    message_id="om_stack",
                    content=(
                        f"@bot ROM版本号{rom} 反解crash.txt堆栈\n"
                        "#05 pc 0000000000f385e4 /system/app/xp_envirodrive/lib/arm64/libunity.so"
                    ),
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "addr2line_resolve")
        self.assertEqual(len(fake_addr2line.requests), 1)
        self.assertEqual(fake_addr2line.requests[0].rom_version, rom)
        self.assertEqual(fake_addr2line.requests[0].apk_version, "V6.1.0_20260327175820_Release")
        self.assertIn("addr2line 反解完成", fake_lark.replies[-1]["text"])

    def test_addr2line_request_reuses_recent_same_chat_rom_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            fake_addr2line = FakeAddr2LineRunner()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"], lark=LarkOptions(bot_name="bot")),
                lark_client=fake_lark,
                rom_version_runner=fake_rom,
                addr2line_runner=fake_addr2line,
            )
            rom_text = (
                "@bot ROM版本号XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release "
                "找下导航版本"
            )
            app.handle_event(event(event_id="evt_rom", message_id="om_rom", content=rom_text))

            result = app.handle_event(
                event(
                    event_id="evt_stack",
                    message_id="om_stack",
                    content=(
                        "@bot 反解地址\n"
                        "#05 pc 0000000000f385e4 /system/app/xp_envirodrive/lib/arm64/libunity.so"
                    ),
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "addr2line_resolve")
        self.assertEqual(len(fake_addr2line.requests), 1)
        self.assertEqual(
            fake_addr2line.requests[0].rom_version,
            "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
        )

    def test_addr2line_file_reply_routes_with_referenced_file_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            fake_addr2line = FakeAddr2LineRunner()
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": {"file_key": "file_crash_txt"},
                            }
                        ]
                    }
                }
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="bot"),
                ),
                lark_client=fake_lark,
                rom_version_runner=fake_rom,
                addr2line_runner=fake_addr2line,
            )
            text = (
                "@bot XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release "
                "反解导航符号表"
            )

            result = app.handle_event(event(content=text, reply_to="om_file_msg"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "addr2line_resolve")
        self.assertEqual(len(fake_rom.requests), 1)
        self.assertEqual(len(fake_addr2line.requests), 1)
        request = fake_addr2line.requests[0]
        self.assertEqual(request.resources[0].kind, "file")
        self.assertEqual(request.resources[0].value, "file_crash_txt")
        self.assertEqual(request.resources[0].source_message_id, "om_file_msg")
        self.assertEqual(request.addr_text, "")
        self.assertEqual(
            request.rom_version,
            "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
        )
        self.assertEqual(request.apk_version, "V6.1.0_20260327175820_Release")

    def test_addr2line_file_reply_accepts_symbol_decomposition_wording(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            fake_addr2line = FakeAddr2LineRunner()
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": {"file_key": "file_crash_zip"},
                            }
                        ]
                    }
                }
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="bot"),
                ),
                lark_client=fake_lark,
                rom_version_runner=fake_rom,
                addr2line_runner=fake_addr2line,
            )
            rom = "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release"
            app.handle_event(event(event_id="evt_rom", message_id="om_rom", content=f"@bot ROM版本号{rom} 查导航版本"))

            result = app.handle_event(
                event(
                    event_id="evt_addr2line_symbol_decomposition",
                    message_id="om_addr2line_symbol_decomposition",
                    content="@bot 分解符号表",
                    reply_to="om_file_msg",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "addr2line_resolve")
        self.assertEqual(len(fake_addr2line.requests), 1)
        request = fake_addr2line.requests[0]
        self.assertEqual(request.resources[0].kind, "file")
        self.assertEqual(request.resources[0].value, "file_crash_zip")
        self.assertEqual(request.rom_version, rom)
        self.assertEqual(request.apk_version, "V6.1.0_20260327175820_Release")

    def test_addr2line_followup_recovers_original_file_from_reply_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            fake_addr2line = FakeAddr2LineRunner()
            fake_lark.fetched_messages["om_current"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_current",
                                "reply_to": "om_previous_text",
                            }
                        ]
                    }
                }
            )
            fake_lark.fetched_messages["om_previous_text"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_previous_text",
                                "reply_to": "om_file_msg",
                                "content": {"text": "ROM版本号XMART... 反解crash.txt堆栈"},
                            }
                        ]
                    }
                }
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": {"file_key": "file_crash_zip"},
                            }
                        ]
                    }
                }
            )
            rom = "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release"
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="bot"),
                ),
                lark_client=fake_lark,
                rom_version_runner=fake_rom,
                addr2line_runner=fake_addr2line,
            )

            result = app.handle_event(
                event(
                    event_id="evt_addr2line_followup",
                    message_id="om_current",
                    content=f"@bot ROM版本号{rom} 反解导航符号表",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "addr2line_resolve")
        self.assertEqual(len(fake_rom.requests), 1)
        self.assertEqual(len(fake_addr2line.requests), 1)
        request = fake_addr2line.requests[0]
        self.assertEqual(request.resources[0].kind, "file")
        self.assertEqual(request.resources[0].value, "file_crash_zip")
        self.assertEqual(request.resources[0].source_message_id, "om_file_msg")
        self.assertEqual(request.addr_text, "")
        self.assertEqual(request.rom_version, rom)
        self.assertEqual(request.apk_version, "V6.1.0_20260327175820_Release")

    def test_addr2line_runner_extracts_last_montecarlo_stack_from_logd_crash_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / ".ai/skills/addr2line-resolve/scripts/addr2line_resolve.py"
            script.parent.mkdir(parents=True)
            script.write_text("# fake script\n", encoding="utf-8")
            crash_file = root / "zip_src/data/Log/log0/logd/crash.txt.01"
            crash_file.parent.mkdir(parents=True)
            crash_file.write_text(
                "\n".join(
                    [
                        "--------- beginning of crash",
                        "05-19 15:27:55.000  1000  1000 F DEBUG   : Cmdline: /system/bin/other",
                        "05-19 15:27:55.000  1000  1000 F DEBUG   :       #00 pc 0000000000012340  /system/lib64/libother.so",
                        "05-19 15:28:40.041  9497  9497 F DEBUG   : Cmdline: /system/app/xp_envirodrive-mainland/xp_envirodrive-mainland",
                        "05-19 15:28:40.041  9497  9497 F DEBUG   : pid: 2531, tid: 9497, name: UnityMain  >>> com.xiaopeng.montecarlo <<<",
                        "05-19 15:28:40.041  9497  9497 F DEBUG   :       #00 pc 0000000000f385e4  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        "05-19 15:28:40.041  9497  9497 F DEBUG   :       #01 pc 00000000010fa7f0  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        "05-19 15:32:57.535 11311 11311 F DEBUG   : Cmdline: /system/app/xp_envirodrive-mainland/xp_envirodrive-mainland",
                        "05-19 15:32:57.535 11311 11311 F DEBUG   : pid: 11311, tid: 11311, name: UnityMain  >>> com.xiaopeng.montecarlo <<<",
                        "05-19 15:32:57.535 11311 11311 F DEBUG   :       #00 pc 00000000010f5948  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        "05-19 15:32:57.535 11311 11311 F DEBUG   :       #01 pc 0000000002020202  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                    ]
                ),
                encoding="utf-8",
            )
            archive = root / "crash_bundle.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.write(crash_file, "data/Log/log0/logd/crash.txt.01")
                zf.writestr(
                    "dfx.txt",
                    "\n".join(
                        [
                            '"processName" : "com.xiaopeng.montecarlo",',
                            '"stack" : "      #00 pc 00000000dfdfdfdf  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so\\n",',
                        ]
                    ),
                )
            runner = Addr2LineRunner(
                BridgeConfig(dry_run=True, data_dir=root / "data", workspace_root=root, guideengine_repo=root)
            )

            result = runner.run_resolve(
                Addr2LineRequest(
                    addr_text="",
                    rom_version="XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
                    resources=[DownloadResource(kind="local", value=str(archive))],
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        command_text = " ".join(result.command or [])
        self.assertIn("00000000010f5948", command_text)
        self.assertIn("0000000002020202", command_text)
        self.assertNotIn("0000000000f385e4", command_text)
        self.assertNotIn("dfdfdfdf", command_text)
        self.assertTrue(str(result.details["addr_source"]).endswith("data/Log/log0/logd/crash.txt.01"))

    def test_addr2line_runner_extracts_last_navigation_stack_from_crash_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / ".ai/skills/addr2line-resolve/scripts/addr2line_resolve.py"
            script.parent.mkdir(parents=True)
            script.write_text("# fake script\n", encoding="utf-8")
            crash_file = root / "crash.txt"
            crash_file.write_text(
                "\n".join(
                    [
                        "05-16 12:00:00.000  1000  1000 F DEBUG   : Cmdline: /system/bin/other",
                        "05-16 12:00:00.000  1000  1000 F DEBUG   :       #00 pc 0000000000012340  /system/lib64/libother.so",
                        "05-16 12:18:40.041  9497  9497 F DEBUG   : Cmdline: /system/app/xp_envirodrive/xp_envirodrive",
                        "05-16 12:18:40.041  9497  9497 F DEBUG   :       #05 pc 0000000000f385e4  /system/app/xp_envirodrive/lib/arm64/libunity.so",
                        "05-16 12:18:40.041  9497  9497 F DEBUG   :       #06 pc 00000000010fa7f0  /system/app/xp_envirodrive/lib/arm64/libunity.so",
                        "05-16 12:18:41.041  9497  9497 F DEBUG   : Cmdline: /system/app/xp_envirodrive/xp_envirodrive",
                        "05-16 12:18:41.041  9497  9497 F DEBUG   :       #05 pc 00000000010f5948  /system/app/xp_envirodrive/lib/arm64/libunity.so",
                    ]
                ),
                encoding="utf-8",
            )
            runner = Addr2LineRunner(
                BridgeConfig(dry_run=True, data_dir=root / "data", workspace_root=root, guideengine_repo=root)
            )

            result = runner.run_resolve(
                Addr2LineRequest(
                    addr_text="",
                    rom_version="XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
                    resources=[DownloadResource(kind="local", value=str(crash_file))],
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        self.assertIn("00000000010f5948", " ".join(result.command or []))
        self.assertNotIn("0000000000f385e4", " ".join(result.command or []))
        self.assertEqual(result.details["addr_source"], str(crash_file))

    def test_addr2line_runner_infers_rom_from_lowest_log_prop_when_request_has_no_symbol_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / ".ai/skills/addr2line-resolve/scripts/addr2line_resolve.py"
            script.parent.mkdir(parents=True)
            script.write_text("# fake script\n", encoding="utf-8")
            rom_log1 = "XMARTQGZHE29E5_V6.1.0.1111_20260327111111.1_REV01_USER_Release"
            rom_log2 = "XMARTQGZHE29E5_V6.1.0.2222_20260327222222.1_REV01_USER_Release"
            log1_crash = root / "zip_src/data/Log/log1/logd/crash.txt"
            log2_crash = root / "zip_src/data/Log/log2/logd/crash.txt"
            for path, text in (
                (
                    log1_crash,
                    "\n".join(
                        [
                            "05-19 15:28:40.041  9497  9497 F DEBUG   : Cmdline: /system/app/xp_envirodrive-mainland/xp_envirodrive-mainland",
                            "05-19 15:28:40.041  9497  9497 F DEBUG   : pid: 2531, tid: 9497, name: UnityMain  >>> com.xiaopeng.montecarlo <<<",
                            "05-19 15:28:40.041  9497  9497 F DEBUG   :       #00 pc 0000000000f385e4  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        ]
                    ),
                ),
                (
                    log2_crash,
                    "\n".join(
                        [
                            "05-19 15:32:57.535 11311 11311 F DEBUG   : Cmdline: /system/app/xp_envirodrive-mainland/xp_envirodrive-mainland",
                            "05-19 15:32:57.535 11311 11311 F DEBUG   : pid: 11311, tid: 11311, name: UnityMain  >>> com.xiaopeng.montecarlo <<<",
                            "05-19 15:32:57.535 11311 11311 F DEBUG   :       #00 pc 00000000010f5948  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        ]
                    ),
                ),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            (log1_crash.parent / "prop.txt").write_text(f"ro.build.version={rom_log1}\n", encoding="utf-8")
            (log2_crash.parent / "prop.txt").write_text(f"ro.build.version={rom_log2}\n", encoding="utf-8")
            archive = root / "crash_bundle.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.write(log1_crash, "data/Log/log1/logd/crash.txt")
                zf.write(log2_crash, "data/Log/log2/logd/crash.txt")
                zf.write(log1_crash.parent / "prop.txt", "data/Log/log1/logd/prop.txt")
                zf.write(log2_crash.parent / "prop.txt", "data/Log/log2/logd/prop.txt")
            fake_rom = FakeRomVersionRunner()
            runner = Addr2LineRunner(
                BridgeConfig(dry_run=True, data_dir=root / "data", workspace_root=root, guideengine_repo=root),
                rom_version_runner=fake_rom,
            )

            result = runner.run_resolve(
                Addr2LineRequest(
                    addr_text="",
                    resources=[DownloadResource(kind="local", value=str(archive))],
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(len(fake_rom.requests), 1)
        self.assertEqual(fake_rom.requests[0].rom_version, rom_log1)
        self.assertTrue(str(result.details["addr_source"]).endswith("data/Log/log1/logd/crash.txt"))

    def test_addr2line_runner_prefers_requested_log_folder_for_prop_and_crash_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / ".ai/skills/addr2line-resolve/scripts/addr2line_resolve.py"
            script.parent.mkdir(parents=True)
            script.write_text("# fake script\n", encoding="utf-8")
            rom_log1 = "XMARTQGZHE29E5_V6.1.0.3333_20260327333333.1_REV01_USER_Release"
            rom_log2 = "XMARTQGZHE29E5_V6.1.0.4444_20260327444444.1_REV01_USER_Release"
            log1_crash = root / "zip_src/data/Log/log1/logd/crash.txt"
            log2_crash = root / "zip_src/data/Log/log2/logd/crash.txt"
            for path, text in (
                (
                    log1_crash,
                    "\n".join(
                        [
                            "05-19 15:28:40.041  9497  9497 F DEBUG   : Cmdline: /system/app/xp_envirodrive-mainland/xp_envirodrive-mainland",
                            "05-19 15:28:40.041  9497  9497 F DEBUG   : pid: 2531, tid: 9497, name: UnityMain  >>> com.xiaopeng.montecarlo <<<",
                            "05-19 15:28:40.041  9497  9497 F DEBUG   :       #00 pc 0000000000f385e4  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        ]
                    ),
                ),
                (
                    log2_crash,
                    "\n".join(
                        [
                            "05-19 15:32:57.535 11311 11311 F DEBUG   : Cmdline: /system/app/xp_envirodrive-mainland/xp_envirodrive-mainland",
                            "05-19 15:32:57.535 11311 11311 F DEBUG   : pid: 11311, tid: 11311, name: UnityMain  >>> com.xiaopeng.montecarlo <<<",
                            "05-19 15:32:57.535 11311 11311 F DEBUG   :       #00 pc 00000000010f5948  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        ]
                    ),
                ),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            (log1_crash.parent / "prop.txt").write_text(f"ro.build.version={rom_log1}\n", encoding="utf-8")
            (log2_crash.parent / "prop.txt").write_text(f"ro.build.version={rom_log2}\n", encoding="utf-8")
            archive = root / "crash_bundle.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.write(log1_crash, "data/Log/log1/logd/crash.txt")
                zf.write(log2_crash, "data/Log/log2/logd/crash.txt")
                zf.write(log1_crash.parent / "prop.txt", "data/Log/log1/logd/prop.txt")
                zf.write(log2_crash.parent / "prop.txt", "data/Log/log2/logd/prop.txt")
            fake_rom = FakeRomVersionRunner()
            runner = Addr2LineRunner(
                BridgeConfig(dry_run=True, data_dir=root / "data", workspace_root=root, guideengine_repo=root),
                rom_version_runner=fake_rom,
            )

            result = runner.run_resolve(
                Addr2LineRequest(
                    addr_text="",
                    resources=[DownloadResource(kind="local", value=str(archive))],
                    log_folder="log1",
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(len(fake_rom.requests), 1)
        self.assertEqual(fake_rom.requests[0].rom_version, rom_log1)
        self.assertTrue(str(result.details["addr_source"]).endswith("data/Log/log1/logd/crash.txt"))

    def test_addr2line_runner_uses_fault_time_to_pick_matching_log_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / ".ai/skills/addr2line-resolve/scripts/addr2line_resolve.py"
            script.parent.mkdir(parents=True)
            script.write_text("# fake script\n", encoding="utf-8")
            rom_log1 = "XMARTQGZHE29E5_V6.1.0.5555_20260327555555.1_REV01_USER_Release"
            rom_log2 = "XMARTQGZHE29E5_V6.1.0.6666_20260327666666.1_REV01_USER_Release"
            log1_crash = root / "zip_src/data/Log/log1/logd/crash.txt"
            log2_crash = root / "zip_src/data/Log/log2/logd/crash.txt"
            for path, text in (
                (
                    log1_crash,
                    "\n".join(
                        [
                            "05-19 15:28:40.041  9497  9497 F DEBUG   : Cmdline: /system/app/xp_envirodrive-mainland/xp_envirodrive-mainland",
                            "05-19 15:28:40.041  9497  9497 F DEBUG   : pid: 2531, tid: 9497, name: UnityMain  >>> com.xiaopeng.montecarlo <<<",
                            "05-19 15:28:40.041  9497  9497 F DEBUG   :       #00 pc 0000000000f385e4  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        ]
                    ),
                ),
                (
                    log2_crash,
                    "\n".join(
                        [
                            "05-19 15:32:57.535 11311 11311 F DEBUG   : Cmdline: /system/app/xp_envirodrive-mainland/xp_envirodrive-mainland",
                            "05-19 15:32:57.535 11311 11311 F DEBUG   : pid: 11311, tid: 11311, name: UnityMain  >>> com.xiaopeng.montecarlo <<<",
                            "05-19 15:32:57.535 11311 11311 F DEBUG   :       #00 pc 00000000010f5948  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        ]
                    ),
                ),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            (log1_crash.parent / "prop.txt").write_text(f"ro.build.version={rom_log1}\n", encoding="utf-8")
            (log2_crash.parent / "prop.txt").write_text(f"ro.build.version={rom_log2}\n", encoding="utf-8")
            archive = root / "crash_bundle.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.write(log1_crash, "data/Log/log1/logd/crash.txt")
                zf.write(log2_crash, "data/Log/log2/logd/crash.txt")
                zf.write(log1_crash.parent / "prop.txt", "data/Log/log1/logd/prop.txt")
                zf.write(log2_crash.parent / "prop.txt", "data/Log/log2/logd/prop.txt")
            fake_rom = FakeRomVersionRunner()
            runner = Addr2LineRunner(
                BridgeConfig(dry_run=True, data_dir=root / "data", workspace_root=root, guideengine_repo=root),
                rom_version_runner=fake_rom,
            )

            result = runner.run_resolve(
                Addr2LineRequest(
                    addr_text="",
                    resources=[DownloadResource(kind="local", value=str(archive))],
                    fault_time="05-19 15:28",
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(len(fake_rom.requests), 1)
        self.assertEqual(fake_rom.requests[0].rom_version, rom_log1)
        self.assertTrue(str(result.details["addr_source"]).endswith("data/Log/log1/logd/crash.txt"))

    def test_addr2line_runner_parses_subrealitytrace_threads_when_symbol_version_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace_file = root / "subrealitytrace_2026-05-24-20-04-00"
            trace_file.write_text(
                "\n".join(
                    [
                        "----- pid 24454 at 2026-05-24 20:04:13.754987528+0800 -----",
                        '"peng.montecarlo" sysTid=24454',
                        "    #00 pc 0000000000085a9c  /apex/com.android.runtime/lib64/bionic/libc.so (syscall+28)",
                        "    #01 pc 00000000030fa9bc  /system/framework/arm64/boot-framework.oat (android.app.ActivityThread.main+732)",
                        '"UnityMain" sysTid=25293',
                        "    #00 pc 00000000012ae2a0  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        "    #01 pc 000000000072afb4  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                        '"XPD_LD" sysTid=24839',
                        "    #00 pc 00000000000264f0  /system/app/xp_envirodrive-mainland/lib/arm64/libxdata_native.so",
                        "    #01 pc 0000000000402480  /system/app/xp_envirodrive-mainland/lib/arm64/libxdata_sdk.so",
                        '"JniSurfaceTexLoop" sysTid=24859',
                        "    #00 pc 00000000000175a4  /system/app/xp_envirodrive-mainland/lib/arm64/libRenderExtend.so",
                        "    #01 pc 00000000000179dc  /system/app/xp_envirodrive-mainland/lib/arm64/libRenderExtend.so",
                        '"RenderThread" sysTid=24525',
                        "    #00 pc 00000000003c7138  /system/lib64/libhwui.so (android::uirenderer::renderthread::RenderThread::threadLoop()+76)",
                        '"GLThread 883" sysTid=24542',
                        "    #00 pc 000000000153d768  /system/framework/arm64/boot-framework.oat (android.opengl.GLSurfaceView$GLThread.guardedRun+1944)",
                    ]
                ),
                encoding="utf-8",
            )
            runner = Addr2LineRunner(
                BridgeConfig(dry_run=True, data_dir=root / "data", workspace_root=root, guideengine_repo=root)
            )

            result = runner.run_resolve(
                Addr2LineRequest(
                    addr_text="",
                    resources=[DownloadResource(kind="local", value=str(trace_file))],
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details.get("analysis_mode"), "single_trace_thread_parse")
        self.assertEqual(result.details.get("trace_source"), str(trace_file))
        counts = result.details.get("thread_category_counts") or {}
        self.assertGreaterEqual(counts.get("UnityMain", 0), 1)
        self.assertGreaterEqual(counts.get("XPD_*", 0), 1)
        self.assertGreaterEqual(counts.get("JniSurfaceTex*", 0), 1)
        self.assertGreaterEqual(counts.get("主线程", 0), 1)
        self.assertGreaterEqual(counts.get("渲染相关线程", 0), 1)
        self.assertIn("UnityMain", result.message)
        self.assertIn("场景/渲染相关 so:", result.message)
        self.assertIn("UnityClassic::Baselib_SystemFutex_Wait", result.message)
        self.assertIn("Semaphore::WaitForSignal", result.message)
        self.assertIn("JniSurfaceTexLoop", result.message)
        self.assertNotIn("#00 pc", result.message)

    def test_addr2line_runner_keeps_missing_symbol_version_for_non_trace_single_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / ".ai/skills/addr2line-resolve/scripts/addr2line_resolve.py"
            script.parent.mkdir(parents=True)
            script.write_text("# fake script\n", encoding="utf-8")
            crash_file = root / "crash.txt"
            crash_file.write_text(
                "\n".join(
                    [
                        "#00 pc 0000000000f385e4 /system/app/xp_envirodrive/lib/arm64/libunity.so",
                        "#01 pc 00000000010fa7f0 /system/app/xp_envirodrive/lib/arm64/libunity.so",
                    ]
                ),
                encoding="utf-8",
            )
            runner = Addr2LineRunner(
                BridgeConfig(dry_run=True, data_dir=root / "data", workspace_root=root, guideengine_repo=root)
            )

            result = runner.run_resolve(
                Addr2LineRequest(
                    addr_text="",
                    resources=[DownloadResource(kind="local", value=str(crash_file))],
                    triggered=True,
                )
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_symbol_version")

    def test_scene_signal_prompt_preempts_generic_signal_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            route_content = "分析3D场景信号"
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_handler = FakeSignalHandler()
            fake_lark.fetched_messages["om_file_msg"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_file_msg",
        "content": "{\\"file_key\\":\\"file_scene_log\\"}"
      }
    ]
  }
}
"""
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                handler=fake_handler,
                intent_runner=FakeIntentRunner(
                    {
                        route_content: IntentDecision(
                            route="signal",
                            reason="旧路由误判为信号生命周期",
                            confidence="high",
                        )
                    }
                ),
            )

            result = app.handle_event(event(reply_to="om_file_msg", content=f"@bot {route_content}"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_handler.requests), 0)
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, route_content)
        self.assertEqual(fake_bug.requests[0].resources[0].kind, "file")
        self.assertEqual(fake_bug.requests[0].resources[0].value, "file_scene_log")

    def test_scene_signal_new_request_does_not_require_latest_chat_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            route_content = "分析 3D场景信号"
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                intent_runner=FakeIntentRunner(
                    {
                        route_content: IntentDecision(
                            route="analysis_followup",
                            reason="同群最近一次场景信号主题",
                            confidence="high",
                            followup_action="context_chat",
                            context_source="latest_chat",
                        )
                    }
                ),
            )
            app.conversation_store.remember(
                root_message_id="om_previous_scene_report",
                chat_id="oc_denied",
                mode="direct_analysis",
                request_text="分析下 3D场景信号",
                summary_text="已有场景信号报告",
                report_url="http://report",
                report_excerpt="3D场景信号分析报告",
            )

            result = app.handle_event(
                event(
                    event_id="evt_scene_new_request",
                    message_id="om_scene_new_request",
                    content=f"@bot {route_content}",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertNotEqual(result.error_code, "missing_followup_reply")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, route_content)

    def test_reply_to_file_intent_followup_misroute_falls_back_to_direct_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            route_content = "问题时间 2026-05-29 17:16，分析启动和卡顿"
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": '{"file_key":"file_lane_level_log"}',
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                intent_runner=FakeIntentRunner(
                    {
                        route_content: IntentDecision(
                            route="analysis_followup",
                            reason="误把回复文件当成续聊",
                            confidence="high",
                            followup_action="context_chat",
                            context_source="none",
                        )
                    }
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_direct_analysis_reply_file",
                    message_id="om_direct_analysis_reply_file",
                    reply_to="om_file_msg",
                    content=f"@bot {route_content}",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertNotEqual(result.error_code, "missing_followup_reply")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, route_content)
        self.assertEqual(fake_bug.requests[0].resources[0].kind, "file")
        self.assertEqual(fake_bug.requests[0].resources[0].value, "file_lane_level_log")

    def test_followup_keywords_with_reply_file_still_reach_intent_router_direct_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            route_content = "问题时间 2026-05-29 17:16，分析车道级 @朱云龙的飞书 CLI"
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_current"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_current",
                                "content": route_content,
                                "reply_to": "om_file_msg",
                                "mentions": [
                                    {
                                        "id": "cli_a976baa2cdfadcc7",
                                        "key": "@_user_1",
                                        "name": "朱云龙的飞书 CLI",
                                    }
                                ],
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": '<file key="file_v3_00125_xxx" name="main_2026-05-29_17-00.alog"/>',
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                allowed_chats=["oc_denied"],
            )
            config.lark.bot_name = "朱云龙的飞书 CLI"
            app = BridgeApp(
                config,
                lark_client=fake_lark,
                bug_runner=fake_bug,
                intent_runner=FakeIntentRunner(
                    {
                        "问题时间 2026-05-29 17:16，分析车道级": IntentDecision(
                            route="analysis_followup",
                            reason="误把回复文件当成续聊",
                            confidence="high",
                            followup_action="context_chat",
                            context_source="none",
                        )
                    }
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_reply_file_followup_keywords",
                    message_id="om_current",
                    content=route_content,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertNotEqual(result.error_code, "missing_followup_reply")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].resources[0].value, "file_v3_00125_xxx")

    def test_intent_bug_misroute_with_reply_file_falls_back_to_direct_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            route_content = "@朱云龙的飞书 CLI 问题时间 2026-05-29 17:16，分析车道级"
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_current"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_current",
                                "content": route_content,
                                "reply_to": "om_file_msg",
                                "mentions": [
                                    {
                                        "id": "cli_a976baa2cdfadcc7",
                                        "key": "@_user_1",
                                        "name": "朱云龙的飞书 CLI",
                                    }
                                ],
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": '<file key="file_v3_00125_bug_misroute" name="main_2026-05-29_17-00.alog"/>',
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                allowed_chats=["oc_denied"],
            )
            config.lark.bot_name = "朱云龙的飞书 CLI"
            app = BridgeApp(
                config,
                lark_client=fake_lark,
                bug_runner=fake_bug,
                intent_runner=FakeIntentRunner(
                    {
                        "问题时间 2026-05-29 17:16，分析车道级": IntentDecision(
                            route="bug",
                            reason="误判成 bug 重分析",
                            confidence="high",
                            followup_action="reanalysis",
                            context_source="explicit",
                        )
                    }
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_reply_file_bug_misroute",
                    message_id="om_current",
                    content=route_content,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertNotEqual(result.error_code, "missing_bug_url")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].resources[0].value, "file_v3_00125_bug_misroute")

    def test_signal_followup_fetches_current_message_when_event_omits_reply_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            (log_dir / "main.log").write_text("05-20 12:00:00 signal 132002", encoding="utf-8")
            route_content = "132002 信号"
            fake_lark = FakeLarkClient()
            fake_lark.fetched_messages["om_signal_c"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_signal_c",
                                "reply_to": "om_signal_b",
                            }
                        ]
                    }
                }
            )
            fake_lark.fetched_messages["om_signal_b"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_signal_b",
                                "reply_to": "om_bug_a",
                            }
                        ]
                    }
                }
            )
            fake_handler = FakeSignalHandler()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp) / "data",
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                handler=fake_handler,
                intent_runner=FakeIntentRunner(
                    {
                        route_content: IntentDecision(
                            route="signal",
                            reason="查看具体信号",
                            confidence="high",
                        )
                    }
                ),
            )
            original_event = event(
                event_id="evt_bug_original",
                message_id="om_bug_a",
                content="@bot bug 分析完成",
            )
            app.activity_store.record_event(original_event)
            app.activity_store.record_result(
                original_event,
                TaskResult(
                    success=True,
                    message="上一轮分析完成",
                    details={
                        "mode": "bug_reanalysis",
                        "prepared_log_input": str(log_dir),
                        "selected_log_input": str(log_dir),
                    },
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_signal_followup_no_reply_field",
                    message_id="om_signal_c",
                    content=f"@bot {route_content}",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(len(fake_handler.requests), 1)
        resources = fake_handler.requests[0].resources
        self.assertEqual(resources[0].kind, "local")
        self.assertEqual(Path(resources[0].value), log_dir.resolve())

    def test_signal_request_without_reply_chain_does_not_reuse_same_chat_latest_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            (log_dir / "main.log").write_text("05-20 12:00:00 signal 132002", encoding="utf-8")
            route_content = "看132002 信号吧"
            fake_handler = FakeSignalHandler()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp) / "data",
                    allowed_chats=["oc_denied"],
                ),
                lark_client=FakeLarkClient(),
                handler=fake_handler,
                intent_runner=FakeIntentRunner(
                    {
                        route_content: IntentDecision(
                            route="signal",
                            reason="查看具体信号",
                            confidence="high",
                        )
                    }
                ),
            )
            app.conversation_store.remember(
                root_message_id="om_unrelated_bug",
                chat_id="oc_denied",
                mode="bug_reanalysis",
                request_text="同群另一轮 bug 分析",
                summary_text="已有日志",
                report_url="",
                report_excerpt="",
            )
            app.activity_store.record_result(
                event(message_id="om_unrelated_bug", content="@bot unrelated"),
                TaskResult(
                    success=True,
                    message="上一轮分析完成",
                    details={
                        "mode": "bug_reanalysis",
                        "prepared_log_input": str(log_dir),
                        "selected_log_input": str(log_dir),
                    },
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_signal_no_chain",
                    message_id="om_signal_no_chain",
                    content=f"@bot {route_content}",
                )
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_log")
        self.assertEqual(len(fake_handler.requests), 1)
        self.assertEqual(fake_handler.requests[0].resources, [])

    def test_signal_followup_recovers_resources_from_previous_missing_signal_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            route_content = "那就看132002 信号吧"
            fake_lark = FakeLarkClient()
            fake_lark.fetched_messages["om_missing_signal"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_missing_signal",
                                "reply_to": "om_file_msg",
                            }
                        ]
                    }
                }
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": "{\"file_key\":\"file_v3_log_abc\"}",
                            }
                        ]
                    }
                }
            )
            fake_handler = FakeSignalHandler()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp) / "data",
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                handler=fake_handler,
                intent_runner=FakeIntentRunner(
                    {
                        route_content: IntentDecision(
                            route="signal",
                            reason="补充具体信号继续分析",
                            confidence="high",
                        )
                    }
                ),
            )
            previous_event = event(
                event_id="evt_missing_signal",
                message_id="om_missing_signal",
                content="@bot 基于日志 分析上下电信号",
            )
            app.activity_store.record_event(previous_event)
            app.activity_store.record_result(
                previous_event,
                TaskResult(
                    success=False,
                    message="缺少 signal",
                    error_code="missing_signal",
                    details={},
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_signal_after_missing_signal",
                    message_id="om_signal_after_missing_signal",
                    reply_to="om_missing_signal",
                    content=f"@bot {route_content}",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(len(fake_handler.requests), 1)
        resources = fake_handler.requests[0].resources
        self.assertEqual(resources[0].kind, "file")
        self.assertEqual(resources[0].value, "file_v3_log_abc")
        self.assertEqual(resources[0].source_message_id, "om_file_msg")

    def test_direct_analysis_reply_to_file_in_non_allowlisted_group_routes_to_bug_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_file_msg"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_file_msg",
        "content": "{\\"file_key\\":\\"file_abc123\\"}"
      }
    ]
  }
}
"""
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_allowed"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    chat_id="oc_denied",
                    reply_to="om_file_msg",
                    content="@bot 分析启动和卡顿",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].resources[0].kind, "file")
        self.assertEqual(fake_bug.requests[0].resources[0].value, "file_abc123")
        self.assertEqual(fake_bug.requests[0].resources[0].source_message_id, "om_file_msg")

    def test_reply_to_file_routes_to_direct_analysis_even_when_prompt_has_no_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_current"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_current",
                                "content": "@bot 看下这个",
                                "reply_to": "om_file_msg",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": '<file key="file_v3_0011s_6d5d723c-ec0b-44f3-9908-a02be496b54g" name="Log.zip"/>',
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                intent_runner=FakeIntentRunner(enabled=False),
            )

            result = app.handle_event(event(message_id="om_current", content="@bot 看下这个"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, "看下这个")
        self.assertEqual(len(fake_bug.requests[0].resources), 1)
        self.assertEqual(
            fake_bug.requests[0].resources[0].value,
            "file_v3_0011s_6d5d723c-ec0b-44f3-9908-a02be496b54g",
        )

    def test_reply_to_folder_routes_to_direct_analysis_even_when_prompt_has_no_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_current"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_current",
                                "content": "@bot 看下这个",
                                "reply_to": "om_folder_msg",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_folder_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_folder_msg",
                                "content": '<folder token="fldcnlog123" name="LogFolder"/>',
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                intent_runner=FakeIntentRunner(enabled=False),
            )

            result = app.handle_event(event(message_id="om_current", content="@bot 看下这个"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].resources[0].kind, "folder")
        self.assertEqual(fake_bug.requests[0].resources[0].value, "fldcnlog123")
        self.assertEqual(fake_bug.requests[0].resources[0].source_message_id, "om_folder_msg")

    def test_reply_to_file_generic_question_prefers_chat_not_direct_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            fake_lark.fetched_messages["om_current"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_current",
                                "content": "@bot 这个是什么？",
                                "reply_to": "om_file_msg",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": '<file key="file_v3_0011s_6d5d723c-ec0b-44f3-9908-a02be496b54g" name="Log.zip"/>',
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
                intent_runner=FakeIntentRunner(enabled=False),
            )

            result = app.handle_event(event(message_id="om_current", content="@bot 这个是什么？"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(len(fake_bug.requests), 0)
        self.assertEqual(len(fake_chat.prompts), 1)

    def test_followup_reply_uses_saved_analysis_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
            )

            first = app.handle_event(
                event(
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_followup",
                    message_id="om_followup",
                    root_id="om_1",
                    parent_id="om_bot_reply",
                    content="@bot 这个结论的根因是什么",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(len(fake_bug.agent_followup_calls), 1)
        self.assertEqual(fake_bug.agent_followup_calls[0]["followup_text"], "这个结论的根因是什么")
        self.assertEqual(fake_chat.context_calls, [])
        self.assertIn("om_followup", _all_reply_message_ids(fake_lark))
        # Card replies don't include at-mention text, so only check text replies for that
        text_replies_for_followup = [r for r in fake_lark.replies if r["message_id"] == "om_followup"]
        if text_replies_for_followup:
            self.assertTrue(text_replies_for_followup[-1]["text"].startswith('<at user_id="ou_1"></at> '))

    def test_followup_reply_resolves_context_via_reply_to_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )
            fake_lark.fetched_messages["om_bot_analysis_reply"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_bot_analysis_reply",
        "reply_to": "om_original_request"
      }
    ]
  }
}
"""
            followup = app.handle_event(
                event(
                    event_id="evt_followup_chain",
                    message_id="om_followup_chain",
                    reply_to="om_bot_analysis_reply",
                    content="@bot 问题时间是2026-05-11 23:12分左右",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(len(fake_bug.agent_followup_calls), 1)
        self.assertEqual(fake_bug.agent_followup_calls[0]["followup_text"], "问题时间是2026-05-11 23:12分左右")
        self.assertIn("om_followup_chain", _all_reply_message_ids(fake_lark))

    def test_chat_followup_from_middle_reply_ignores_later_branch_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                intent_runner=FakeIntentRunner(enabled=False),
            )
            app.conversation_store.remember(
                root_message_id="om_root_chat",
                chat_id="ou_chat_1",
                mode="omlx_chat",
                request_text="第一问",
                summary_text="第一答",
                report_url="",
                report_excerpt="",
            )
            app.conversation_store.append_exchange("om_root_chat", user_text="第一问", assistant_text="第一答")
            app.conversation_store.remember_alias(alias_message_id="om_bot_reply_1", root_message_id="om_root_chat")
            app.conversation_store.append_exchange("om_root_chat", user_text="第二问", assistant_text="第二答")

            result = app.handle_event(
                event(
                    event_id="evt_mid_branch_followup",
                    message_id="om_mid_branch_followup",
                    chat_id="ou_chat_1",
                    chat_type="p2p",
                    reply_to="om_bot_reply_1",
                    content="第三问",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "analysis_followup")
        self.assertEqual(len(fake_chat.context_calls), 1)
        self.assertEqual(
            fake_chat.context_calls[0]["history"],
            [
                {"role": "user", "content": "第一问"},
                {"role": "assistant", "content": "第一答"},
            ],
        )

    def test_followup_reply_to_progress_card_resolves_original_bug_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>SceneType=Main</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                allowed_chats=["oc_denied"],
            )
            app = BridgeApp(
                config,
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                )
            )
            card_message_id = fake_lark.card_replies[0]["card_message_id"]
            followup = app.handle_event(
                event(
                    event_id="evt_followup_card_alias",
                    message_id="om_followup_card_alias",
                    reply_to=card_message_id,
                    content="@bot 最后的Unity 场景 SceneType是什么",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(followup.details["conversation_root_message_id"], "om_original_request")
        self.assertEqual(len(fake_bug.agent_followup_calls), 1)
        self.assertEqual(fake_bug.agent_followup_calls[0]["followup_text"], "最后的Unity 场景 SceneType是什么")

    def test_followup_reply_to_uploaded_html_resolves_context_when_event_lacks_reply_to(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>SceneType=Main</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                allowed_chats=["oc_denied"],
            )
            app = BridgeApp(
                config,
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                )
            )
            uploaded_html_message_id = fake_lark.files[-1]["message_id"]
            restarted_lark = FakeLarkClient()
            restarted_bug = FakeBugRunner(metadata, html)
            restarted_app = BridgeApp(
                config,
                lark_client=restarted_lark,
                bug_runner=restarted_bug,
                chat_client=FakeOmlxChatClient(),
            )
            restarted_lark.fetched_messages["om_followup_html"] = f"""
{{
  "ok": true,
  "data": {{
    "messages": [
      {{
        "message_id": "om_followup_html",
        "reply_to": "{uploaded_html_message_id}"
      }}
    ]
  }}
}}
"""
            followup = restarted_app.handle_event(
                event(
                    event_id="evt_followup_html",
                    message_id="om_followup_html",
                    content="@bot 最后的Unity 场景 SceneType是什么",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(followup.details["conversation_root_message_id"], "om_original_request")
        self.assertEqual(len(restarted_bug.agent_followup_calls), 1)
        self.assertEqual(restarted_bug.agent_followup_calls[0]["followup_text"], "最后的Unity 场景 SceneType是什么")

    def test_p2p_followup_reply_uses_saved_analysis_context_without_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    chat_id="ou_chat_1",
                    chat_type="p2p",
                    content="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                )
            )
            fake_lark.fetched_messages["om_bot_analysis_reply"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_bot_analysis_reply",
        "reply_to": "om_original_request"
      }
    ]
  }
}
"""
            followup = app.handle_event(
                event(
                    event_id="evt_followup_p2p",
                    message_id="om_followup_p2p",
                    chat_id="ou_chat_1",
                    chat_type="p2p",
                    reply_to="om_bot_analysis_reply",
                    content="这个结论的根因是什么",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(len(fake_bug.agent_followup_calls), 1)
        self.assertEqual(fake_bug.agent_followup_calls[0]["followup_text"], "这个结论的根因是什么")
        self.assertEqual(fake_chat.context_calls, [])
        self.assertIn("om_followup_p2p", _all_reply_message_ids(fake_lark))
        text_replies_for_p2p = [r for r in fake_lark.replies if r["message_id"] == "om_followup_p2p"]
        if text_replies_for_p2p:
            self.assertFalse(text_replies_for_p2p[-1]["text"].startswith("<at "))

    def test_group_addr2line_followup_in_reply_chain_without_mention(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_addr2line = FakeAddr2LineRunner()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                addr2line_runner=fake_addr2line,
            )
            app.conversation_store.remember(
                root_message_id="om_addr_root",
                chat_id="oc_denied",
                mode="addr2line_resolve",
                request_text="@bot ROM版本号XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release 反解堆栈",
                summary_text="addr2line 反解完成",
                report_url="",
                report_excerpt="",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_addr_reply",
                root_message_id="om_addr_root",
            )
            app.activity_store.record_result(
                event(message_id="om_addr_root", content="@bot ROM版本号XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release 反解堆栈"),
                TaskResult(
                    success=True,
                    message="addr2line 反解完成",
                    details={
                        "mode": "addr2line_resolve",
                        "rom_version": "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
                        "symbol_version": "V6.1.0_20260327175820_Release",
                        "symbol_version_kind": "apk",
                        "resources": [
                            {"kind": "local", "value": str(Path(tmp) / "crash_bundle.zip")},
                        ],
                    },
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_group_addr_followup_no_mention",
                    message_id="om_group_addr_followup_no_mention",
                    reply_to="om_addr_reply",
                    content="再反解一次",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "addr2line_resolve")
        self.assertEqual(len(fake_addr2line.requests), 1)
        self.assertEqual(
            fake_addr2line.requests[0].rom_version,
            "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
        )
        self.assertEqual(fake_addr2line.requests[0].resources[0].value, str(Path(tmp) / "crash_bundle.zip"))

    def test_group_addr2line_followup_retry_once_reuses_addr2line_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_addr2line = FakeAddr2LineRunner()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                addr2line_runner=fake_addr2line,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_addr_root_retry",
                chat_id="oc_denied",
                mode="addr2line_resolve",
                request_text="@bot ROM版本号XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release 反解堆栈",
                summary_text="addr2line 反解完成",
                report_url="",
                report_excerpt="",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_addr_reply_retry",
                root_message_id="om_addr_root_retry",
            )
            app.activity_store.record_result(
                event(message_id="om_addr_root_retry", content="@bot ROM版本号XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release 反解堆栈"),
                TaskResult(
                    success=True,
                    message="addr2line 反解完成",
                    details={
                        "mode": "addr2line_resolve",
                        "rom_version": "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
                        "symbol_version": "V6.1.0_20260327175820_Release",
                        "symbol_version_kind": "apk",
                        "resources": [
                            {"kind": "local", "value": str(Path(tmp) / "retry_bundle.zip")},
                        ],
                    },
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_group_addr_retry_once",
                    message_id="om_group_addr_retry_once",
                    reply_to="om_addr_reply_retry",
                    content="重试一次",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "addr2line_resolve")
        self.assertEqual(len(fake_addr2line.requests), 1)
        self.assertEqual(fake_addr2line.requests[0].resources[0].value, str(Path(tmp) / "retry_bundle.zip"))

    def test_group_rom_lookup_followup_in_reply_chain_without_mention(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                rom_version_runner=fake_rom,
            )
            app.conversation_store.remember(
                root_message_id="om_rom_root",
                chat_id="oc_denied",
                mode="rom_version_lookup",
                request_text="@bot ROM版本号XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release 查导航版本",
                summary_text="ROM 版本查询完成",
                report_url="",
                report_excerpt="",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_rom_reply",
                root_message_id="om_rom_root",
            )
            app.activity_store.record_result(
                event(message_id="om_rom_root", content="@bot ROM版本号XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release 查导航版本"),
                TaskResult(
                    success=True,
                    message="ROM 版本查询完成",
                    details={
                        "mode": "rom_version_lookup",
                        "rom_version": "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
                        "required_outputs": {
                            "navigation_version": "V6.1.0_20260327175820_Release",
                        },
                    },
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_group_rom_followup_no_mention",
                    message_id="om_group_rom_followup_no_mention",
                    reply_to="om_rom_reply",
                    content="再查一次",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "rom_version_lookup")
        self.assertEqual(len(fake_rom.requests), 1)
        self.assertEqual(
            fake_rom.requests[0].rom_version,
            "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
        )

    def test_followup_reanalysis_fetches_current_message_reply_to_when_event_lacks_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )
            fake_lark.fetched_messages["om_followup_missing_reply"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_followup_missing_reply",
        "reply_to": "om_bot_analysis_reply"
      }
    ]
  }
}
"""
            fake_lark.fetched_messages["om_bot_analysis_reply"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_bot_analysis_reply",
        "reply_to": "om_original_request"
      }
    ]
  }
}
"""
            followup = app.handle_event(
                event(
                    event_id="evt_followup_missing_reply",
                    message_id="om_followup_missing_reply",
                    content="@bot 修复问题时间 23:12分 重新分析下",
                )
            )
            context = app.conversation_store.lookup("om_original_request")

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_reanalysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        self.assertEqual(fake_bug.reanalysis_calls[0]["followup_text"], "修复问题时间 23:12分 重新分析下")
        self.assertEqual(followup.details["conversation_root_message_id"], "om_original_request")
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.root_message_id, "om_original_request")
        self.assertTrue(context.history)
        session = app.activity_store.get_session("om_original_request")
        self.assertIsNotNone(session)
        assert session is not None
        agent_progress = [item for item in session["progress"] if item["stage"] == "bug_agent_summary"]
        self.assertTrue(agent_progress)
        self.assertEqual(agent_progress[-1]["details"]["provider_session_id"], "sess_123")

    def test_bug_reanalysis_keeps_original_request_text_in_conversation_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=FakeLarkClient(),
                bug_runner=FakeBugRunner(metadata, html),
            )

            original = app.handle_event(
                event(
                    event_id="evt_bug_original",
                    message_id="om_bug_original",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动生命周期",
                )
            )
            self.assertTrue(original.success)
            initial_context = app.conversation_store.lookup("om_bug_original")
            self.assertIsNotNone(initial_context)
            assert initial_context is not None
            original_request_text = initial_context.request_text

            followup = app.handle_event(
                event(
                    event_id="evt_bug_reanalysis",
                    message_id="om_bug_reanalysis",
                    reply_to="om_bug_original",
                    root_id="om_bug_original",
                    content="@bot 重新分析",
                )
            )
            updated_context = app.conversation_store.lookup("om_bug_original")

        self.assertTrue(followup.success)
        self.assertIsNotNone(updated_context)
        assert updated_context is not None
        self.assertEqual(updated_context.request_text, original_request_text)
        self.assertNotIn("追问/修正：", updated_context.request_text)

    def test_followup_reanalysis_fetches_current_message_reply_to_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                allowed_chats=["oc_denied"],
                job_retention=RealBridgeConfig().job_retention.__class__(
                    enabled=True,
                    max_age_hours=6,
                    bug_cache_max_age_hours=24,
                    purge_all_on_listen_start=True,
                    cleanup_interval_seconds=60,
                ),
            )

            first_lark = FakeLarkClient()
            first_app = BridgeApp(config, lark_client=first_lark, bug_runner=FakeBugRunner(metadata, html))
            first = first_app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )

            second_lark = FakeLarkClient()
            second_lark.fetched_messages["om_followup_missing_reply"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_followup_missing_reply",
        "reply_to": "om_bot_analysis_reply"
      }
    ]
  }
}
"""
            second_lark.fetched_messages["om_bot_analysis_reply"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_bot_analysis_reply",
        "reply_to": "om_original_request"
      }
    ]
  }
}
"""
            second_bug = FakeBugRunner(metadata, html)
            second_app = BridgeApp(config, lark_client=second_lark, bug_runner=second_bug)
            followup = second_app.handle_event(
                event(
                    event_id="evt_followup_missing_reply_restart",
                    message_id="om_followup_missing_reply",
                    content="@bot 修复问题时间 23:12分 重新分析下",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_reanalysis")
        self.assertEqual(len(second_bug.reanalysis_calls), 1)
        self.assertEqual(followup.details["conversation_root_message_id"], "om_original_request")

    def test_group_followup_reply_chain_is_resolved_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                allowed_chats=["oc_denied"],
                job_retention=RealBridgeConfig().job_retention.__class__(
                    enabled=True,
                    max_age_hours=6,
                    bug_cache_max_age_hours=24,
                    purge_all_on_listen_start=True,
                    cleanup_interval_seconds=60,
                ),
            )

            first_lark = FakeLarkClient()
            first_app = BridgeApp(config, lark_client=first_lark, bug_runner=FakeBugRunner(metadata, html))
            first = first_app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )

            second_lark = FakeLarkClient()
            second_lark.fetched_messages["om_followup_group_restart"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_followup_group_restart",
        "reply_to": "om_bot_analysis_reply"
      }
    ]
  }
}
"""
            second_lark.fetched_messages["om_bot_analysis_reply"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_bot_analysis_reply",
        "reply_to": "om_original_request"
      }
    ]
  }
}
"""
            second_bug = FakeBugRunner(metadata, html)
            second_app = BridgeApp(config, lark_client=second_lark, bug_runner=second_bug)
            followup = second_app.handle_event(
                event(
                    event_id="evt_followup_group_restart",
                    message_id="om_followup_group_restart",
                    content="@bot 继续把刚才那批日志往下查",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(len(second_bug.agent_followup_calls), 1)
        self.assertEqual(followup.details["conversation_root_message_id"], "om_original_request")

    def test_followup_reanalysis_without_reply_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_followup_latest",
                    message_id="om_followup_latest",
                    content="@bot 修正问题时间为 23:12分 重新分析",
                )
            )

        self.assertTrue(first.success)
        self.assertFalse(followup.success)
        self.assertEqual(followup.error_code, "missing_followup_reply")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(len(fake_bug.reanalysis_calls), 0)

    def test_p2p_followup_without_reply_is_rejected_without_at_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    chat_id="ou_chat_1",
                    chat_type="p2p",
                    content="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_followup_p2p_latest",
                    message_id="om_followup_p2p_latest",
                    chat_id="ou_chat_1",
                    chat_type="p2p",
                    content="修正问题时间为 23:12分 重新分析",
                )
            )

        self.assertTrue(first.success)
        self.assertFalse(followup.success)
        self.assertEqual(followup.error_code, "missing_followup_reply")
        self.assertIn("直接回复对应那条分析消息", followup.message)
        self.assertNotIn("@机器人", followup.message)
        self.assertEqual(len(fake_bug.reanalysis_calls), 0)

    def test_followup_reply_with_bug_link_stays_in_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_followup_bug_link",
                    message_id="om_followup_bug_link",
                    root_id="om_original_request",
                    parent_id="om_bot_reply",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 时间点修正为23:12分",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_reanalysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        self.assertEqual(fake_bug.reanalysis_calls[0]["followup_text"], "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 时间点修正为23:12分")
        self.assertEqual(fake_chat.context_calls, [])

    def test_bug_followup_reply_routes_to_bug_agent_instead_of_context_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_followup_logd",
                    message_id="om_followup_logd",
                    root_id="om_original_request",
                    parent_id="om_bot_reply",
                    content="@bot 没有数据 你不会分析 logd里面的日志吗",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(len(fake_bug.agent_followup_calls), 1)
        self.assertEqual(fake_bug.agent_followup_calls[0]["followup_text"], "没有数据 你不会分析 logd里面的日志吗")
        self.assertEqual(fake_chat.context_calls, [])
        all_reply_ids = [r["message_id"] for r in fake_lark.replies] + [r["message_id"] for r in fake_lark.card_replies]
        self.assertIn("om_followup_logd", all_reply_ids)

    def test_bug_followup_reruns_when_existing_report_cannot_answer_new_source_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_followup_source_gap",
                    message_id="om_followup_source_gap",
                    root_id="om_original_request",
                    parent_id="om_bot_reply",
                    content="@bot 这个结果不提 VCU_ELECTRICIT_PERCENT，基于源码重新看信号定义",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_reanalysis")
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        self.assertTrue(fake_bug.reanalysis_calls[0]["force_rerun"])
        self.assertEqual(fake_bug.agent_followup_calls, [])
        self.assertEqual(fake_chat.context_calls, [])

    def test_generic_bug_reanalysis_keeps_previous_plan_despite_noisy_report_excerpt(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text(
                "<html><body>3D 生命周期报告 P. 上下电上下文 启动链路 SIGNAL_MCU_IG_ST</body></html>",
                encoding="utf-8",
            )
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            manual_calls = []

            def noisy_manual_selection(**kwargs):
                manual_calls.append(kwargs)
                return BugAnalysisSelection(
                    plans=[BugAnalysisPlan(kind="signal", signal_code="SIGNAL_MCU_IG_ST")],
                    skill_name="signal-chain-analyzer",
                    skill_label="信号链路分析",
                    source="manual_fallback",
                    reason="noisy report excerpt",
                )

            fake_bug._manual_bug_selection = noisy_manual_selection
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_3d_lifecycle",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 调查3D生命周期",
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_generic_reanalysis",
                    message_id="om_generic_reanalysis",
                    root_id="om_original_3d_lifecycle",
                    parent_id="om_bot_reply",
                    content="@bot 重新分析一遍",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_reanalysis")
        self.assertEqual(manual_calls, [])
        call = fake_bug.reanalysis_calls[0]
        self.assertTrue(call["force_rerun"])
        self.assertIsNone(call["plans_override"])
        self.assertEqual(call["classification_skill"], "")
        self.assertEqual(call["classification_source"], "")

    def test_agent_intent_routes_bug_followup_to_same_agent_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            initial_bug_text = "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
            fake_intent = FakeIntentRunner(
                {
                    initial_bug_text: IntentDecision(
                        route="bug",
                        reason="新的 bug 链接分析请求",
                        confidence="high",
                        followup_action="none",
                        context_source="none",
                    ),
                    "继续把刚才那批日志往下查": IntentDecision(
                        route="analysis_followup",
                        reason="同一 bug 续聊",
                        confidence="high",
                        followup_action="continue_agent",
                        context_source="explicit",
                    )
                }
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
                intent_runner=fake_intent,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content=f"@bot {initial_bug_text}"
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_followup_agent",
                    message_id="om_followup_agent",
                    root_id="om_original_request",
                    parent_id="om_bot_reply",
                    content="@bot 继续把刚才那批日志往下查",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(len(fake_bug.agent_followup_calls), 1)
        self.assertEqual(fake_bug.agent_followup_calls[0]["followup_text"], "继续把刚才那批日志往下查")
        self.assertEqual(fake_chat.context_calls, [])
        self.assertEqual(fake_intent.calls, [])

    def test_bug_followup_without_reply_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_followup_latest_bug_agent",
                    message_id="om_followup_latest_bug_agent",
                    content="@bot 用你之前下载下来日志搜索 关键字看 卡顿skill 将23:10到23:15之间的系统卡顿报告发出来",
                )
            )

        self.assertTrue(first.success)
        self.assertFalse(followup.success)
        self.assertEqual(followup.error_code, "missing_followup_reply")
        self.assertEqual(len(fake_bug.agent_followup_calls), 0)
        self.assertEqual(fake_chat.context_calls, [])

    def test_bug_followup_existing_answer_replies_with_choice_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    event_consumer=EventConsumerOptions(event_key="card.action.trigger"),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="- 问题时刻系统主题是黑夜，证据是 daynightMode=2、uiMode=35、Activity isNightMode=true。",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="- 关键证据：系统 UI 已切到 Night，但 XTheme themeMode 还是 Day。",
            )

            followup = app.handle_event(
                event(
                    event_id="evt_existing_followup_card",
                    message_id="om_existing_followup_card",
                    root_id="om_bug_root",
                    content="@bot 问题时刻的 系统主题是白天还是黑夜",
                )
            )

        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_followup_existing_answer")
        self.assertEqual(len(fake_lark.card_replies), 1)
        self.assertEqual(fake_lark.replies, [])
        card = json.loads(fake_lark.card_replies[0]["card_json"])
        rendered = str(card)
        self.assertIn("先输入追问", rendered)
        self.assertIn("followup_prompt", rendered)
        self.assertIn("按输入从报告回答", rendered)
        self.assertIn("按输入重跑日志", rendered)
        self.assertIn("按输入续 Agent", rendered)
        self.assertIn("有用(记录)", rendered)
        self.assertIn("不准(记录)", rendered)
        self.assertIn("http://127.0.0.1:8765/reports/om_bug_root/", rendered)

    def test_answer_from_report_card_action_requires_input_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="- 系统主题是黑夜。",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="- XTheme themeMode 还是 Day。",
            )

            result = app.handle_payload(
                {
                    "header": {"event_id": "evt_answer_no_prompt"},
                    "event": {
                        "context": {
                            "open_message_id": "om_card_answer",
                            "open_chat_id": "oc_denied",
                            "chat_type": "group",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "answer_from_report",
                                "root_message_id": "om_bug_root",
                            }
                        },
                    },
                }
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_card_followup_prompt")
        self.assertEqual(fake_bug.agent_followup_calls, [])
        self.assertEqual(fake_bug.reanalysis_calls, [])
        self.assertTrue(fake_lark.replies)
        self.assertIn("请先在卡片输入框填写", fake_lark.replies[-1]["text"])

    def test_reanalysis_card_action_uses_form_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=FakeLarkClient(),
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="- 系统主题是黑夜。",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="- XTheme themeMode 还是 Day。",
            )

            result = app.handle_payload(
                {
                    "header": {"event_id": "evt_reanalyze_form_prompt"},
                    "event": {
                        "context": {
                            "open_message_id": "om_card_reanalyze_form",
                            "open_chat_id": "oc_denied",
                            "chat_type": "group",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "reanalyze",
                                "root_message_id": "om_bug_root",
                            },
                            "form_value": {
                                "followup_prompt": "根据导航源码分析 SR 页面生命周期",
                            },
                        },
                    },
                }
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_reanalysis")
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        self.assertEqual(fake_bug.reanalysis_calls[0]["followup_text"], "根据导航源码分析 SR 页面生命周期")

    def test_select_bug_skill_card_action_routes_reanalysis_to_selected_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=FakeLarkClient(),
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            request_text = "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析这个 bug"
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_clarification",
                request_text=request_text,
                summary_text="未命中专用 skill，请选择分析方向。",
                report_url="",
                report_excerpt="",
            )
            original = event(
                event_id="evt_bug_clarify",
                message_id="om_bug_root",
                content=request_text,
            )
            app.activity_store.record_event(original)
            app.activity_store.record_result(
                original,
                TaskResult(
                    success=True,
                    message="未命中专用 skill",
                    job_id="job_clarify",
                    job_dir=Path(tmp) / "jobs" / "job_clarify",
                    details={
                        "mode": "bug_clarification",
                        "conversation_root_message_id": "om_bug_root",
                        "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322",
                        "user_request_text": request_text,
                    },
                ),
            )

            result = app.handle_payload(
                {
                    "header": {"event_id": "evt_select_xtheme"},
                    "event": {
                        "context": {
                            "open_message_id": "om_card_select",
                            "open_chat_id": "oc_denied",
                            "chat_type": "group",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "select_bug_skill",
                                "root_message_id": "om_bug_root",
                                "job_id": "job_clarify",
                                "skill_name": "xtheme-analyzer",
                            },
                            "form_value": {"followup_prompt": "重点看问题时间前的 ThemeHelper 变化"},
                        },
                    },
                }
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_reanalysis")
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        call = fake_bug.reanalysis_calls[0]
        self.assertTrue(call["force_rerun"])
        self.assertEqual(call["classification_skill"], "xtheme-analyzer")
        self.assertEqual(call["classification_source"], "user_selected_card")
        self.assertEqual(call["plans_override"][0].kind, "xtheme")
        self.assertIn("ThemeHelper", call["followup_text"])

    def test_bug_result_card_offers_skill_correction_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    event_consumer=EventConsumerOptions(event_key="card.action.trigger"),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            result = TaskResult(
                success=True,
                message="Bug 分析完成\n结论：当前更像是感知链路问题。",
                job_id="job_bug_result",
                details={
                    "mode": "bug_analysis",
                    "published_report_url": "http://127.0.0.1:8765/reports/job_bug_result/",
                    "analysis_skill": "perception-data-summary",
                    "analysis_skill_label": "当前感知数据总结",
                    "classification_source": "agent",
                },
            )

            sent = app._try_send_result_card(
                event(message_id="om_bug_result"),
                result,
                delivery="reply",
                session_id="om_bug_result",
            )

        self.assertTrue(sent)
        self.assertEqual(len(fake_lark.card_replies), 1)
        rendered = fake_lark.card_replies[0]["card_json"]
        self.assertIn("意图/Skill 校正", rendered)
        self.assertIn("当前命中：当前感知数据总结", rendered)
        self.assertIn("select_bug_skill", rendered)
        self.assertIn("xtheme-analyzer", rendered)
        self.assertIn("perception-data-summary", rendered)

    def test_bug_result_card_hides_card_action_buttons_without_card_action_consumer(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            result = TaskResult(
                success=True,
                message="Bug 分析完成\n结论：当前更像是启动时序问题。",
                job_id="job_bug_result",
                details={
                    "mode": "bug_analysis",
                    "published_report_url": "http://127.0.0.1:8765/reports/job_bug_result/",
                    "analysis_skill": "3d-stuck-investigate",
                    "analysis_skill_label": "3D启动时序分析",
                    "classification_source": "agent",
                    "agent_summary_provider": "direct_api",
                    "agent_summary_model": "mimo-v2.5-pro",
                },
            )

            sent = app._try_send_result_card(
                event(message_id="om_bug_result"),
                result,
                delivery="reply",
                session_id="om_bug_result",
            )

        self.assertTrue(sent)
        rendered = fake_lark.card_replies[0]["card_json"]
        self.assertIn("Agent 模型", rendered)
        self.assertIn("mimo-v2.5-pro", rendered)
        self.assertIn("命中 Skill", rendered)
        self.assertNotIn("意图/Skill 校正", rendered)
        self.assertNotIn("select_bug_skill", rendered)
        self.assertNotIn("feedback_helpful", rendered)
        self.assertNotIn("feedback_unhelpful", rendered)
        self.assertNotIn("下方按钮", rendered)

    def test_finished_progress_card_offers_skill_correction_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    event_consumer=EventConsumerOptions(event_key="card.action.trigger"),
                ),
                lark_client=FakeLarkClient(),
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app._progress_cards["om_bug_result"] = {
                "title": "Bug 分析",
                "status": "analyzing",
                "details": {},
                "started_at": datetime.now(timezone.utc),
                "message_id": "om_card_1",
            }
            result = TaskResult(
                success=True,
                message="Bug 分析完成\n结论：当前更像是感知链路问题。",
                job_id="job_bug_result",
                details={
                    "mode": "bug_analysis",
                    "published_report_url": "http://127.0.0.1:8765/reports/job_bug_result/",
                    "analysis_skill": "perception-data-summary",
                    "analysis_skill_label": "当前感知数据总结",
                    "classification_source": "agent",
                    "agent_summary_provider": "direct_api",
                    "agent_summary_model": "mimo-v2.5-pro",
                    "conversation_root_message_id": "om_bug_result",
                },
            )

            card = app._build_progress_card(
                event(message_id="om_bug_result"),
                key="om_bug_result",
                status="completed",
                result=result,
                note=result.message,
            )

        rendered = str(card)
        self.assertIn("Agent 模型", rendered)
        self.assertIn("mimo-v2.5-pro", rendered)
        self.assertIn("意图/Skill 校正", rendered)
        self.assertIn("当前命中：当前感知数据总结", rendered)
        self.assertIn("select_bug_skill", rendered)
        self.assertIn("命中 Skill", rendered)

    def test_continue_agent_card_action_explicitly_resumes_saved_agent_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=FakeLarkClient(),
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="- 系统主题是黑夜。",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="- XTheme themeMode 还是 Day。",
            )

            result = app.handle_payload(
                {
                    "header": {"event_id": "evt_continue_agent_card"},
                    "event": {
                        "context": {
                            "open_message_id": "om_card",
                            "open_chat_id": "oc_denied",
                            "chat_type": "group",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "continue_agent",
                                "root_message_id": "om_bug_root",
                                "followup_text": "继续原 Agent 会话看一下刚才的判断",
                            }
                        },
                    },
                }
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_agent_followup")
        self.assertEqual(len(fake_bug.agent_followup_calls), 1)
        self.assertTrue(fake_bug.agent_followup_calls[0]["resume_agent_session"])
        self.assertEqual(fake_bug.agent_followup_calls[0]["followup_text"], "继续原 Agent 会话看一下刚才的判断")

    def test_feedback_card_action_records_activity_progress_with_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html"),
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="- 系统主题是黑夜。",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="- XTheme themeMode 还是 Day。",
            )

            result = app.handle_payload(
                {
                    "header": {"event_id": "evt_feedback_card"},
                    "event": {
                        "context": {
                            "open_message_id": "om_card_feedback",
                            "open_chat_id": "oc_denied",
                            "chat_type": "group",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "feedback_unhelpful",
                                "root_message_id": "om_bug_root",
                                "job_id": "job_1",
                                "followup_text": "根据导航源码分析",
                            }
                        },
                    },
                }
            )

            session = app.activity_store.get_session("om_bug_root") or {}
            feedback_events = [
                item for item in session.get("progress", []) if item.get("stage") == "followup_feedback_recorded"
            ]

        self.assertTrue(result.success)
        self.assertEqual(result.details["feedback"], "unhelpful")
        self.assertEqual(len(feedback_events), 1)
        self.assertEqual(feedback_events[0]["details"]["feedback"], "unhelpful")
        self.assertEqual(feedback_events[0]["details"]["followup_text"], "根据导航源码分析")
        self.assertEqual(feedback_events[0]["details"]["job_id"], "job_1")
        self.assertTrue(fake_lark.replies)

    def test_feedback_card_action_uses_saved_chat_context_when_callback_omits_chat_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html"),
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="- 系统主题是黑夜。",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="- XTheme themeMode 还是 Day。",
            )

            result = app.handle_payload(
                {
                    "header": {"event_id": "evt_feedback_no_chat_id"},
                    "event": {
                        "context": {
                            "open_message_id": "om_card_feedback",
                            "chat_type": "group",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "feedback_helpful",
                                "root_message_id": "om_bug_root",
                                "job_id": "job_1",
                            }
                        },
                    },
                }
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["feedback"], "helpful")
        self.assertTrue(fake_lark.replies)
        self.assertEqual(fake_lark.replies[-1]["message_id"], "om_card_feedback")

    def test_reanalysis_card_action_passes_authorized_local_log_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "downloads"
            downloads.mkdir()
            local_log = downloads / "Log.zip"
            local_log.write_text("log", encoding="utf-8")
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp) / "data",
                    allowed_chats=["oc_denied"],
                    allowed_users=["ou_1"],
                    local_resources=LocalResourceOptions(allowed_dirs=[downloads]),
                ),
                lark_client=FakeLarkClient(),
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="- 上一轮未带日志。",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="- 缺少日志。",
            )
            app.activity_store.record_result(
                event(message_id="om_bug_root", content="@bot bug"),
                __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
                    success=True,
                    message="上一轮完成",
                    job_id="job_1",
                    job_dir=Path(tmp) / "data" / "jobs" / "job_1",
                    details={"mode": "bug_analysis", "prepared_log_input": "", "selected_log_input": ""},
                ),
            )

            result = app.handle_payload(
                {
                    "header": {"event_id": "evt_reanalyze_local_log"},
                    "event": {
                        "context": {
                            "open_message_id": "om_card_reanalyze",
                            "open_chat_id": "oc_denied",
                            "chat_type": "group",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "reanalyze",
                                "root_message_id": "om_bug_root",
                                "job_id": "job_1",
                            },
                            "form_value": {
                                "followup_prompt": "日志我下载到服务器的下载目录了 Log.zip 基于这个日志分析",
                            },
                        },
                    },
                }
            )

        self.assertTrue(result.success)
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        local_resources = fake_bug.reanalysis_calls[0]["local_log_resources"]
        self.assertEqual(len(local_resources), 1)
        self.assertEqual(local_resources[0].kind, "local")
        self.assertEqual(Path(local_resources[0].value), local_log.resolve())

    def test_bug_followup_answers_from_existing_context_without_resuming_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text=(
                    "## 结论摘要\n"
                    "- 问题时刻系统主题是黑夜，证据是 daynightMode=2、uiMode=35、Activity isNightMode=true。\n"
                    "- SR/XTheme 业务链路仍停在 Day。"
                ),
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="关键证据：系统 UI 已切到 Night，但 XTheme themeMode 还是 Day。",
            )

            followup = app.handle_event(
                event(
                    event_id="evt_existing_followup",
                    message_id="om_existing_followup",
                    root_id="om_bug_root",
                    content="@bot 问题时刻的 系统主题是白天还是黑夜",
                )
            )

        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_followup_existing_answer")
        self.assertGreaterEqual(followup.details["answer_confidence"], 0.8)
        self.assertEqual(fake_bug.agent_followup_calls, [])
        self.assertEqual(fake_bug.reanalysis_calls, [])
        self.assertIn("已有报告", followup.message)
        self.assertIn("黑夜", followup.message)
        self.assertIn("http://127.0.0.1:8765/reports/om_bug_root/", followup.message)

    def test_bug_followup_existing_answer_does_not_use_user_question_as_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="- 问题时刻 `2026-05-18 14:03:01.457` 的系统主题是黑夜。置信度：高。",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="- main.txt 在 14:02:53.597 记录 `onDayNightModeChanged,isNightMode=true`。",
            )
            app.conversation_store.append_exchange(
                "om_bug_root",
                user_text="问题时刻的 系统主题是白天还是黑夜",
                assistant_text="问题时刻系统主题是黑夜。",
            )

            followup = app.handle_event(
                event(
                    event_id="evt_existing_followup_no_user_evidence",
                    message_id="om_existing_followup_no_user_evidence",
                    root_id="om_bug_root",
                    content="@bot 问题时刻的 系统主题是白天还是黑夜",
                )
            )

        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_followup_existing_answer")
        self.assertEqual(fake_bug.agent_followup_calls, [])
        self.assertIn("系统主题是黑夜", followup.message)
        self.assertIn("isNightMode=true", followup.message)
        self.assertNotIn("- 问题时刻的 系统主题是白天还是黑夜", followup.message)

    def test_bug_followup_low_confidence_existing_answer_delegates_to_agent(self):
        class DecidingBugRunner(FakeBugRunner):
            def decide_bug_followup(self, **kwargs):
                return BugFollowupSelection(
                    should_reanalyze=False,
                    force_rerun=False,
                    plans=[],
                    skill_name="xtheme-analyzer",
                    skill_label="XTheme时光主题分析",
                    source="agent",
                    reason="问题引入了已有摘要没有覆盖的新概念，交给 Agent 续聊。",
                    provider="codex",
                )

        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_bug = DecidingBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="- 问题时刻系统主题是黑夜，证据是 daynightMode=2、uiMode=35、Activity isNightMode=true。",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="- 关键证据：系统 UI 已切到 Night，但 XTheme themeMode 还是 Day。",
            )

            followup = app.handle_event(
                event(
                    event_id="evt_existing_followup_application_theme",
                    message_id="om_existing_followup_application_theme",
                    root_id="om_bug_root",
                    content="@bot 问题时刻 application主题 是白天还是黑夜",
                )
            )

        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(len(fake_bug.agent_followup_calls), 1)
        self.assertEqual(fake_bug.reanalysis_calls, [])

    def test_bug_followup_sends_progress_card_before_expensive_reanalysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="已有摘要",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="已有报告",
            )

            followup = app.handle_event(
                event(
                    event_id="evt_reanalysis_followup",
                    message_id="om_reanalysis_followup",
                    root_id="om_bug_root",
                    content="@bot 结果不合理，基于源码重新分析",
                )
            )

        self.assertTrue(followup.success)
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        self.assertFalse(fake_lark.replies)
        self.assertTrue(fake_lark.card_replies)
        self.assertEqual(fake_lark.card_replies[0]["message_id"], "om_reanalysis_followup")
        self.assertIn("请求处理中", fake_lark.card_replies[0]["card_json"])
        self.assertTrue(any("bug_followup_decision_started" in item["card_json"] for item in fake_lark.updated_cards))
        self.assertTrue(any("已完成" in item["card_json"] for item in fake_lark.updated_cards))
        final_card = fake_lark.updated_cards[-1]["card_json"]
        self.assertIn("Bug 重新分析", final_card)
        self.assertNotIn("Bug 追问", final_card)
        self.assertNotIn("续聊判断", final_card)

    def test_agent_intent_followup_without_reply_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            initial_bug_text = "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
            followup_text = "以23:12为准，照旧日志再来一次"
            fake_intent = FakeIntentRunner(
                {
                    initial_bug_text: IntentDecision(
                        route="bug",
                        reason="新的 bug 链接分析请求",
                        confidence="high",
                        followup_action="none",
                        context_source="none",
                    ),
                    followup_text: IntentDecision(
                        route="analysis_followup",
                        reason="同群最近一次 bug 结果的时间修正续聊",
                        confidence="high",
                        followup_action="reanalysis",
                        context_source="latest_chat",
                    )
                }
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
                intent_runner=fake_intent,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content=f"@bot {initial_bug_text}"
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_followup_intent_latest",
                    message_id="om_followup_intent_latest",
                    content=f"@bot {followup_text}",
                )
            )

        self.assertTrue(first.success)
        self.assertFalse(followup.success)
        self.assertEqual(followup.error_code, "missing_followup_reply")
        self.assertEqual(len(fake_bug.reanalysis_calls), 0)
        self.assertEqual(fake_bug.agent_followup_calls, [])
        self.assertEqual(fake_chat.context_calls, [])

    def test_group_followup_intent_without_reply_chain_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
                intent_runner=FakeIntentRunner(enabled=False),
            )
            followup = app.handle_event(
                event(
                    event_id="evt_group_latest_followup_no_mention",
                    message_id="om_group_latest_followup_no_mention",
                    content="重新分析一遍",
                )
            )

        self.assertTrue(followup.success)
        self.assertTrue(followup.skipped)
        self.assertEqual(followup.details["mode"], "not_addressed")
        self.assertEqual(len(fake_bug.reanalysis_calls), 0)

    def test_followup_reanalysis_fetches_current_message_reply_chain_through_clarification_to_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_lark.fetched_messages["om_followup_retry"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_followup_retry",
                                "reply_to": "om_clarification",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_clarification"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_clarification",
                                "reply_to": "om_file_msg",
                                "content": "@bot 基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "msg_type": "file",
                                "content": '<file key="file_v3_0011v_33d1772b-86ac-4789-b6e1-35c77ec29a5g" name="L1NSPGHB3SB010669log0.zip"/>',
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            clarification_event = event(
                event_id="evt_clarification",
                message_id="om_clarification",
                content="@bot 基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态",
            )
            app.activity_store.record_event(clarification_event)
            app.activity_store.record_result(
                clarification_event,
                TaskResult(
                    success=True,
                    message="缺少明确问题时间",
                    details={
                        "mode": "bug_time_clarification",
                        "user_request_text": "基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态",
                    },
                ),
            )

            followup = app.handle_event(
                event(
                    event_id="evt_followup_retry",
                    message_id="om_followup_retry",
                    content="@bot 重新分析",
                )
            )

        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, "基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态")
        self.assertEqual(len(fake_bug.requests[0].resources), 1)
        self.assertEqual(fake_bug.requests[0].resources[0].kind, "file")
        self.assertEqual(fake_bug.requests[0].resources[0].value, "file_v3_0011v_33d1772b-86ac-4789-b6e1-35c77ec29a5g")

    def test_followup_reanalysis_prefers_direct_analysis_recovery_when_clarification_has_job_but_no_bug_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_lark.fetched_messages["om_followup_retry"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_followup_retry",
                                "reply_to": "om_clarification",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_clarification"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_clarification",
                                "reply_to": "om_file_msg",
                                "content": "@bot 基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "msg_type": "file",
                                "content": '<file key="file_v3_0011v_33d1772b-86ac-4789-b6e1-35c77ec29a5g" name="L1NSPGHB3SB010669log0.zip"/>',
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            clarification_event = event(
                event_id="evt_clarification_with_job",
                message_id="om_clarification",
                content="@bot 基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态",
            )
            app.activity_store.record_event(clarification_event)
            app.activity_store.record_result(
                clarification_event,
                TaskResult(
                    success=True,
                    message="缺少明确问题时间",
                    job_id="job_direct_pending",
                    job_dir=Path(tmp) / "jobs" / "job_direct_pending",
                    details={
                        "mode": "bug_time_clarification",
                        "user_request_text": "基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态",
                        "bug_url": "",
                    },
                ),
            )

            followup = app.handle_event(
                event(
                    event_id="evt_followup_retry_with_job",
                    message_id="om_followup_retry",
                    content="@bot 重新分析",
                )
            )

        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(len(fake_bug.reanalysis_calls), 0)
        self.assertEqual(fake_bug.requests[0].prompt, "基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态")

    def test_direct_analysis_failure_card_retry_reruns_direct_analysis_instead_of_omlx_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            fake_lark.fetched_messages["om_retry_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_retry_msg",
                                "reply_to": "om_failure_card",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_failure_card"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_failure_card",
                                "reply_to": "om_direct_root",
                                "content": "<card title=\"📋 文件分析\">下载失败</card>",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_direct_root"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_direct_root",
                                "reply_to": "om_file_msg",
                                "content": "@bot 基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "msg_type": "file",
                                "content": '<file key="file_v3_00120_d905735f-5edc-4122-91ef-ad8d306f401g" name="L1NSPGHB3SB010669log0.zip"/>',
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
            )
            root_event = event(
                event_id="evt_direct_root",
                message_id="om_direct_root",
                content="@bot 基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态",
            )
            app.activity_store.record_event(root_event)
            app.activity_store.record_result(
                root_event,
                TaskResult(
                    success=False,
                    message="下载失败",
                    job_id="job_direct_failed",
                    job_dir=Path(tmp) / "jobs" / "job_direct_failed",
                    details={
                        "mode": "direct_analysis",
                        "conversation_root_message_id": "om_direct_root",
                        "user_request_text": "基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态",
                    },
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_retry_msg",
                    message_id="om_retry_msg",
                    content="@bot 重新分析",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_chat.context_calls, [])

    def test_group_message_without_bot_mention_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="bot"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            result = app.handle_event(event(content="https://gic-ai-center.xiaopeng.com/skills/122"))

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "not_addressed")
        self.assertEqual(fake_chat.prompts, [])
        self.assertEqual(fake_lark.sent, [])

    def test_p2p_plain_chat_uses_omlx_instead_of_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_allowed"],
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            result = app.handle_event(
                event(
                    event_id="evt_p2p_chat",
                    chat_id="ou_chat_1",
                    chat_type="p2p",
                    content="讲个笑话",
                )
            )

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])
        self.assertEqual(fake_lark.replies[0]["message_id"], "om_1")
        self.assertEqual(fake_lark.replies[0]["text"], "omlx 模型回复")

    def test_group_chat_command_requires_mention(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="bot"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            result = app.handle_event(event(content="/chat 讲个笑话"))

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "not_addressed")
        self.assertEqual(fake_chat.prompts, [])

    def test_group_human_mention_is_not_treated_as_bot_when_identity_unconfigured(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_open_id="", bot_name=""),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            result = app.handle_event(event(content="@邸立猛 这个现在正常了吗"))

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "not_addressed")
        self.assertEqual(fake_chat.prompts, [])
        self.assertEqual(fake_lark.replies, [])
        self.assertEqual(fake_lark.sent, [])

    def test_group_mentioned_chat_command_uses_omlx(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="bot"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            result = app.handle_event(event(content="@bot /chat 讲个笑话"))

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])
        self.assertEqual(len(fake_lark.replies), 1)
        self.assertEqual(fake_lark.replies[0]["message_id"], "om_1")

    def test_group_mentioned_chat_command_accepts_prefix_without_slash(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="bot"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            result = app.handle_event(event(content="@bot chat 讲个笑话"))

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])

    def test_group_mentioned_chat_command_accepts_bot_name_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="Test Bot"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            result = app.handle_event(event(content="@Test Bot /chat 讲个笑话"))

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])

    def test_plain_question_bypasses_intent_card(self):
        class FailingIntentRunner(FakeIntentRunner):
            def classify(self, **kwargs):
                raise AssertionError("plain chat should not wait for intent classification")

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="Test Bot"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                bug_runner=FakeBugRunner(metadata, html),
                intent_runner=FailingIntentRunner(enabled=True),
            )

            result = app.handle_event(
                event(
                    event_id="evt_ack_before_intent",
                    content="@Test Bot 帮我解释一下 token",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_lark.card_replies, [])
        self.assertEqual(fake_lark.updated_cards, [])
        self.assertEqual(fake_lark.replies[0]["message_id"], "om_1")
        self.assertEqual(fake_lark.replies[0]["text"], '<at user_id="ou_1"></at> omlx 模型回复')

    def test_explicit_bug_link_bypasses_intent_classifier(self):
        class FailingIntentRunner(FakeIntentRunner):
            def classify(self, **kwargs):
                raise AssertionError("explicit bug links should not wait for intent classification")

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=FakeLarkClient(),
                bug_runner=fake_bug,
                intent_runner=FailingIntentRunner(enabled=True),
            )

            result = app.handle_event(
                event(
                    event_id="evt_explicit_bug_bypass_intent",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593 分析3D生命周期",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, "分析3D生命周期")

    def test_markdown_bug_link_bypasses_intent_classifier(self):
        class FailingIntentRunner(FakeIntentRunner):
            def classify(self, **kwargs):
                raise AssertionError("markdown bug links should not wait for intent classification")

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="朱云龙的飞书 CLI"),
                ),
                lark_client=FakeLarkClient(),
                bug_runner=fake_bug,
                intent_runner=FailingIntentRunner(enabled=True),
            )

            result = app.handle_event(
                event(
                    event_id="evt_markdown_bug_bypass_intent",
                    content="@朱云龙的飞书 CLI [ [缺陷] 【F01】车机大屏页面卡住-SB174577](https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593) 分析3D生命周期",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, "分析3D生命周期")

    def test_intent_chat_updates_ack_card_with_answer(self):
        class FailingIntentRunner(FakeIntentRunner):
            def classify(self, **kwargs):
                raise AssertionError("plain chat should not wait for intent classification")

        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                intent_runner=FailingIntentRunner(enabled=True),
            )

            result = app.handle_event(event(content="@bot 帮我解释一下 token"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_lark.card_replies, [])
        self.assertEqual(fake_lark.updated_cards, [])
        self.assertEqual(fake_lark.replies[0]["message_id"], "om_1")
        self.assertEqual(fake_lark.replies[0]["text"], '<at user_id="ou_1"></at> omlx 模型回复')

    def test_group_bug_request_accepts_configured_bot_name_at_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("metadata", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="Test Bot"),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    event_id="evt_bot_name_at_end",
                    content="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593 分析3D生命周期 @Test Bot",
                )
            )

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, "分析3D生命周期")

    def test_group_bug_request_source_stage_executor_not_ready_is_delivered_without_conclusion(self):
        class NotReadyBugRunner(FakeBugRunner):
            def run_bug_analysis(self, request, *, event=None, progress_callback=None):
                self.requests.append(request)
                self.progress_callbacks.append(progress_callback)
                if progress_callback is not None:
                    progress_callback(
                        {
                            "stage": "bug_run_analysis",
                            "message": "执行源码分析阶段",
                            "details": {"analysis_kind": "source_stage"},
                        }
                    )
                return TaskResult(
                    success=False,
                    message=(
                        "已命中专用 Skill `source-analysis-skill`，但当前没有可执行分析器，"
                        "尚未执行实际 Skill 分析。\n不会基于占位报告给出根因结论。"
                    ),
                    error_code="source_stage_executor_not_ready",
                    details={
                        "mode": "bug_analysis",
                        "analysis_kind": "source_stage",
                        "analysis_skill": "source-analysis-skill",
                        "source_stage_analysis_status": "executor_not_ready",
                    },
                )

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("metadata", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = NotReadyBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    event_id="evt_source_stage_not_ready_group_bug",
                    message_id="om_source_stage_not_ready_group_bug",
                    content=(
                        "@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767 "
                        "2026-05-22 19:46 退无图"
                    ),
                )
            )
            delivered_text = "\n".join(
                [item["text"] for item in fake_lark.replies]
                + [item["card_json"] for item in fake_lark.card_replies]
                + [item["card_json"] for item in fake_lark.updated_cards]
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "source_stage_executor_not_ready")
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].bug_url, "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767")
        self.assertEqual(fake_bug.requests[0].prompt, "2026-05-22 19:46 退无图")
        self.assertIn("当前没有可执行分析器", delivered_text)
        self.assertNotIn("最可能原因", delivered_text)
        self.assertNotIn("结论摘要", delivered_text)

    def test_group_bug_request_source_stage_file_agent_ready_delivers_after_execution(self):
        class ReadyBugRunner(FakeBugRunner):
            def run_bug_analysis(self, request, *, event=None, progress_callback=None):
                self.requests.append(request)
                self.progress_callbacks.append(progress_callback)
                if progress_callback is not None:
                    progress_callback(
                        {
                            "stage": "source_stage_agent_analysis",
                            "message": "执行源码分析文件 Agent 分析",
                            "details": {
                                "analysis_kind": "source_stage",
                                "analysis_skill": "agent-ready-source-skill",
                                "source_stage_analysis_status": "completed",
                            },
                        }
                    )
                    progress_callback(
                        {
                            "stage": "bug_agent_summary",
                            "message": "基于执行证据整理最终结论",
                            "details": {"provider": "codex", "session_id": "sess_ready"},
                        }
                    )
                return TaskResult(
                    success=True,
                    message="file agent summary：已基于 source_stage_analysis.md 的关键证据完成分析。",
                    job_id="job_custom_ready",
                    job_dir=self.html_path.parent,
                    details={
                        "mode": "bug_analysis",
                        "analysis_kind": "source_stage",
                        "analysis_skill": "agent-ready-source-skill",
                        "source_stage_executor": "file_agent",
                        "source_stage_analysis_status": "completed",
                        "source_stage_evidence_count": 2,
                        "files_to_send": [self.html_path],
                    },
                )

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "source_stage_report.html"
            metadata.write_text("metadata", encoding="utf-8")
            html.write_text("<html>custom skill report</html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = ReadyBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    event_id="evt_source_stage_ready_group_bug",
                    message_id="om_source_stage_ready_group_bug",
                    content=(
                        "@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767 "
                        "2026-05-22 19:46 退无图"
                    ),
                )
            )
            delivered_text = "\n".join(
                [item["text"] for item in fake_lark.replies]
                + [item["card_json"] for item in fake_lark.card_replies]
                + [item["card_json"] for item in fake_lark.updated_cards]
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(result.details["analysis_kind"], "source_stage")
        self.assertEqual(result.details["source_stage_analysis_status"], "completed")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].bug_url, "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767")
        self.assertEqual(fake_bug.requests[0].prompt, "2026-05-22 19:46 退无图")
        self.assertIn("file agent summary", delivered_text)
        self.assertNotIn("当前没有可执行分析器", delivered_text)
        self.assertEqual([Path(item["path"]).name for item in fake_lark.files], ["source_stage_report.html"])

    def test_group_bug_request_accepts_rich_text_wrapped_bot_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("metadata", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="朱云龙的飞书 CLI"),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    event_id="evt_rich_text_bot_name",
                    content="<p>@朱云龙的飞书 CLI https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593 分析3D生命周期</p>",
                )
            )

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, "分析3D生命周期")

    def test_group_bug_request_recovers_bot_mention_from_message_metadata_when_config_has_no_bot_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("metadata", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_lark.fetched_messages["om_mid_text_bot_mention"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_mid_text_bot_mention",
                                "chat_id": "oc_denied",
                                "content": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995823164 @朱云龙的飞书 CLI 调查3D启动生命周期",
                                "mentions": [
                                    {
                                        "id": "cli_a976baa2cdfadcc7",
                                        "key": "@_user_1",
                                        "name": "朱云龙的飞书 CLI",
                                    }
                                ],
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_open_id="", bot_name=""),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    event_id="evt_mid_text_bot_mention",
                    message_id="om_mid_text_bot_mention",
                    content="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995823164 @朱云龙的飞书 CLI 调查3D启动生命周期",
                )
            )

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, "调查3D启动生命周期")

    def test_group_chat_command_uses_configured_bot_mention_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_open_id="ou_bot", bot_name="bot"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            rejected = app.handle_event(
                event(
                    event_id="evt_wrong_bot",
                    content='<at user_id="ou_other"></at> /chat 讲个笑话',
                )
            )
            accepted = app.handle_event(
                event(
                    event_id="evt_right_bot",
                    content='<at user_id="ou_bot"></at> /chat 讲个笑话',
                )
            )

        self.assertTrue(rejected.skipped)
        self.assertEqual(rejected.details["mode"], "not_addressed")
        self.assertTrue(accepted.success)
        self.assertFalse(accepted.skipped)
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])

    def test_group_chat_command_uses_configured_bot_name_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="Test Bot"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            rejected = app.handle_event(
                event(
                    event_id="evt_wrong_bot_name",
                    content="@其他机器人 /chat 讲个笑话",
                )
            )
            accepted = app.handle_event(
                event(
                    event_id="evt_right_bot_name",
                    content="@Test Bot /chat 讲个笑话",
                )
            )

        self.assertTrue(rejected.skipped)
        self.assertEqual(rejected.details["mode"], "not_addressed")
        self.assertTrue(accepted.success)
        self.assertFalse(accepted.skipped)
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])

    def test_group_chat_command_accepts_configured_bot_name_without_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="朱云龙的飞书 CLI"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            rejected = app.handle_event(
                event(
                    event_id="evt_human_mention",
                    content="@朱云龙 /chat 讲个笑话",
                )
            )
            accepted = app.handle_event(
                event(
                    event_id="evt_bot_name_no_space",
                    content="@朱云龙的飞书CLI /chat 讲个笑话",
                )
            )

        self.assertTrue(rejected.skipped)
        self.assertEqual(rejected.details["mode"], "not_addressed")
        self.assertTrue(accepted.success)
        self.assertFalse(accepted.skipped)
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])

    def test_signal_followup_retry_reruns_signal_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_handler = FakeSignalHandler()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                handler=fake_handler,
            )
            app.conversation_store.remember(
                root_message_id="om_signal_root",
                chat_id="oc_denied",
                mode="signal_lifecycle",
                request_text="132002 https://example.com/log.zip",
                summary_text="信号分析完成",
                report_url="http://report",
                report_excerpt="",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_signal_reply",
                root_message_id="om_signal_root",
            )

            result = app.handle_event(
                event(
                    event_id="evt_signal_retry",
                    message_id="om_signal_retry",
                    reply_to="om_signal_reply",
                    content="重新分析",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "signal_lifecycle")
        self.assertEqual(len(fake_handler.requests), 1)
        self.assertEqual(fake_handler.requests[0].signal, "132002")

    def test_perception_followup_retry_reruns_perception_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "report.html"
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_runner = FakePerceptionRunner(html)
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                perception_runner=fake_runner,
            )
            app.conversation_store.remember(
                root_message_id="om_perception_root",
                chat_id="oc_denied",
                mode="perception_summary",
                request_text="感知数据总结",
                summary_text="感知数据总结完成",
                report_url="http://report",
                report_excerpt="",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_perception_reply",
                root_message_id="om_perception_root",
            )

            result = app.handle_event(
                event(
                    event_id="evt_perception_retry",
                    message_id="om_perception_retry",
                    reply_to="om_perception_reply",
                    content="重新分析",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "perception_summary")
        self.assertEqual(len(fake_runner.requests), 1)

    def test_signal_followup_non_retry_falls_through_to_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_handler = FakeSignalHandler()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                handler=fake_handler,
                chat_client=fake_chat,
            )
            app.conversation_store.remember(
                root_message_id="om_signal_chat_root",
                chat_id="oc_denied",
                mode="signal_lifecycle",
                request_text="132002 https://example.com/log.zip",
                summary_text="信号分析完成",
                report_url="http://report",
                report_excerpt="这个信号在16:06到达Unity",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_signal_chat_reply",
                root_message_id="om_signal_chat_root",
            )

            result = app.handle_event(
                event(
                    event_id="evt_signal_chat",
                    message_id="om_signal_chat",
                    reply_to="om_signal_chat_reply",
                    content="这个结论什么意思",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(len(fake_handler.requests), 0)
        self.assertEqual(len(fake_chat.context_calls), 1)


if __name__ == "__main__":
    unittest.main()
