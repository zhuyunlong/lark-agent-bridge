"""Application orchestration for the bridge."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Callable

from .agents import (
    BugAnalysisRunner,
    ClaudeSkillRunner,
    IntentAnalysisFailure,
    IntentAnalysisRunner,
    OmlxChatClient,
    PerceptionSummaryRunner,
)
from .downloader import LogDownloader
from .lark_client import LarkClient
from .models import BridgeConfig, DownloadResource, IntentDecision, LarkEvent, SignalRequest, TaskResult, create_job_context
from .parser import (
    build_basic_chat_reply,
    extract_first_keyword_payload,
    find_resources,
    parse_claude_skill_request,
    parse_bug_request,
    parse_direct_analysis_request,
    parse_perception_summary_request,
    parse_signal_request,
    should_use_omlx_chat,
)
from .policy import PolicyDecision, build_policy_rejection_message, evaluate_event_policy
from .report_server import HtmlReportPublisher, ReportHttpServer, resolve_bind_host
from .runner import SignalChainRunner
from .state import AgentActivityStore, ConversationContextStore, EventStateStore
from .handlers.signal_lifecycle import SignalLifecycleHandler


CHAT_COMMAND_PREFIXES = ("/chat",)


class BridgeApp:
    def __init__(
        self,
        config: BridgeConfig,
        *,
        lark_client: LarkClient | None = None,
        state_store: EventStateStore | None = None,
        handler: SignalLifecycleHandler | None = None,
        claude_runner: ClaudeSkillRunner | None = None,
        bug_runner: BugAnalysisRunner | None = None,
        perception_runner: PerceptionSummaryRunner | None = None,
        chat_client: OmlxChatClient | None = None,
        intent_runner: IntentAnalysisRunner | None = None,
        report_publisher: HtmlReportPublisher | None = None,
        report_http_server: ReportHttpServer | None = None,
        conversation_store: ConversationContextStore | None = None,
        activity_store: AgentActivityStore | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.config = config
        self.lark_client = lark_client or LarkClient(config)
        self.state_store = state_store or EventStateStore(config.data_dir / "state" / "seen_events.jsonl")
        self.conversation_store = conversation_store or ConversationContextStore(
            config.data_dir / "state" / "conversation_contexts.json",
            max_history_turns=config.omlx_chat.followup_max_history_turns,
        )
        self.activity_store = activity_store or AgentActivityStore(config.data_dir / "state" / "agent_activity.json")
        self.progress_callback = progress_callback
        self.report_publisher = report_publisher or HtmlReportPublisher(config)
        self.report_http_server = report_http_server or ReportHttpServer(config, activity_store=self.activity_store)
        runner = SignalChainRunner(config)
        downloader = LogDownloader(config, self.lark_client)
        self.handler = handler or SignalLifecycleHandler(config, downloader, runner)
        self.claude_runner = claude_runner or ClaudeSkillRunner(config)
        self.bug_runner = bug_runner or BugAnalysisRunner(config)
        setattr(self.bug_runner, "_lark_client", self.lark_client)
        self.perception_runner = perception_runner or PerceptionSummaryRunner(config, self.lark_client)
        self.chat_client = chat_client or OmlxChatClient(config)
        self.intent_runner = intent_runner or IntentAnalysisRunner(config)

    def check(self) -> dict[str, object]:
        return {
            "dry_run": self.config.dry_run,
            "data_dir": str(self.config.data_dir),
            "guideengine_repo": str(self.config.guideengine_repo),
            "lark": self.lark_client.check_environment(),
            "report_server": {
                "enabled": self.config.report_server.enabled,
                "bind_host": resolve_bind_host(self.config.report_server.bind_host),
                "port": self.config.report_server.port,
                "public_base_url": self.report_publisher.public_base_url,
            },
            "intent_analysis": {
                "enabled": self.intent_runner.is_enabled(),
                "provider": self.config.intent_analysis.provider or self.config.bug_analysis.provider,
                "command": self.config.intent_analysis.command or self.config.bug_analysis.command,
            },
        }

    def handle_event_payload(self, payload: dict[str, object]) -> TaskResult:
        return self.handle_event(LarkEvent.from_dict(payload))

    def handle_event(self, event: LarkEvent) -> TaskResult:
        self.activity_store.record_event(event)
        try:
            result = self._handle_event(event)
        except Exception as exc:
            self.activity_store.record_error(event, exc)
            raise
        self.activity_store.record_result(event, result)
        return result

    def _handle_event(self, event: LarkEvent) -> TaskResult:
        self.cleanup_expired_jobs()
        route_content = event.content
        followup_context = None
        if event.chat_type == "group":
            addressed_content = self._strip_group_chat_mention(event.content)
            if addressed_content is None:
                followup_context = self._resolve_followup_context(event)
                addressed_content = self._strip_bot_mention_anywhere(event.content) if followup_context is not None else None
            if addressed_content is None:
                if not self.state_store.mark_seen(event):
                    return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
                return TaskResult(
                    success=True,
                    message="group message not addressed to this bot",
                    skipped=True,
                    details={"mode": "not_addressed"},
                )
            route_content = addressed_content

        decision = evaluate_event_policy(self.config, event)
        if (
            not decision.allowed
            and decision.reason == "chat_not_allowed"
            and followup_context is None
        ):
            followup_context = self._resolve_followup_context(event)
        referenced_resources = self._fetch_referenced_message_resources(event, route_content=route_content)
        signal_request = self._build_signal_request(route_content, referenced_resources)
        bug_request = parse_bug_request(route_content)
        direct_analysis_request = self._build_direct_analysis_request(route_content, referenced_resources)
        perception_request = self._build_perception_summary_request(route_content, referenced_resources)
        if (
            not decision.allowed
            and decision.reason == "chat_not_allowed"
            and self._allow_log_analysis_in_external_group(
                event,
                followup_context=followup_context,
                signal_request=signal_request,
                bug_request=bug_request,
                direct_analysis_request=direct_analysis_request,
                perception_request=perception_request,
            )
        ):
            decision = PolicyDecision(True, "group_log_analysis_allowed")
        if not decision.allowed:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
            result = TaskResult(
                success=False,
                message=build_policy_rejection_message(decision),
                error_code=decision.reason,
            )
            if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
                self._send_result(event, result)
            return result

        if followup_context is None:
            followup_context = self._resolve_followup_context(event)
        latest_chat_context = None
        if self.intent_runner.is_enabled():
            intent_result = self._handle_intent_routed_event(
                event,
                route_content,
                explicit_followup_context=followup_context,
                latest_chat_context=latest_chat_context,
            )
            if intent_result is not None:
                return intent_result
        if followup_context is not None:
            return self._handle_followup(event, route_content, followup_context)

        request = signal_request
        if request.triggered:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)

            result = self.handler.handle(request, event=event)
            return self._deliver_result(event, result, request_text=request.raw_text or route_content)

        skill_request = parse_claude_skill_request(
            route_content,
            trigger_prefixes=self.config.claude_agent.trigger_prefixes,
        )
        if skill_request.triggered:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)

            result = self.claude_runner.run_skill_analysis(skill_request, event=event)
            return self._deliver_result(event, result, request_text=skill_request.raw_text or route_content)

        if bug_request.triggered:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)

            self._notify_progress(
                "bug_request_received",
                "收到 bug 分析请求",
                event=event,
                bug_url=bug_request.bug_url,
                prompt=bug_request.prompt,
                raw_text=bug_request.raw_text,
            )
            result = self.bug_runner.run_bug_analysis(
                bug_request,
                event=event,
                progress_callback=self._event_progress_callback(event),
            )
            return self._deliver_result(event, result, request_text=bug_request.raw_text or route_content)

        if direct_analysis_request.triggered:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)

            self._notify_progress(
                "direct_analysis_request_received",
                "收到直传文件分析请求",
                event=event,
                prompt=direct_analysis_request.prompt,
                raw_text=direct_analysis_request.raw_text,
                resources=[item.value for item in direct_analysis_request.resources],
            )
            result = self.bug_runner.run_direct_analysis(
                direct_analysis_request,
                event=event,
                progress_callback=self._event_progress_callback(event),
            )
            return self._deliver_result(event, result, request_text=direct_analysis_request.raw_text or route_content)

        if perception_request.triggered:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)

            result = self.perception_runner.run_summary(perception_request, event=event)
            return self._deliver_result(event, result, request_text=perception_request.raw_text or route_content)

        if self._is_followup_intent(route_content):
            if followup_context is not None:
                return self._handle_followup(event, route_content, followup_context)
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
            result = self._missing_followup_reply_result(chat_type=event.chat_type)
            if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
                self._send_result(event, result)
            return result

        chat_reply = build_basic_chat_reply(route_content, command_prefixes=self.config.command_prefixes)
        chat_prompt = self._omlx_prompt(event, route_content)
        if chat_reply is None and chat_prompt is not None:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)

            result = self.chat_client.reply(chat_prompt)
            return self._deliver_result(event, result, request_text=route_content)

        if chat_reply is not None:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)

            result = TaskResult(
                success=True,
                message=chat_reply,
                details={"mode": "basic_chat"},
            )
            return self._deliver_result(event, result, request_text=route_content)

        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        result = TaskResult(
            success=True,
            message="not a handled request",
            skipped=True,
            details={"mode": "unsupported"},
        )
        self._send_result(event, result)
        return result

    def run_signal(self, *, signal: str, log_path: str | Path, since: str | None = None) -> TaskResult:
        self.cleanup_expired_jobs()
        context = create_job_context(self.config.data_dir, job_id=f"manual_{signal}")
        request = SignalRequest(
            signal=signal,
            resources=[DownloadResource(kind="local", value=str(log_path))],
            since=since,
            triggered=True,
        )
        runner = SignalChainRunner(self.config)
        result = runner.run(signal=signal, log_path=log_path, output_dir=context.output_dir, since=since)
        result.job_id = context.job_id
        result.job_dir = context.job_dir
        if result.success:
            result.message = (
                f"{'dry-run 计划' if self.config.dry_run else '处理完成'}: signal {request.signal}\n"
                f"日志路径: {log_path}\n"
                f"HTML 报告: {result.html_report}\n"
                f"JSON 报告: {result.json_report}"
            )
        return result

    def purge_all_jobs(self) -> int:
        jobs_root = self._jobs_root()
        removed = 0
        if jobs_root.exists():
            for job_dir in jobs_root.iterdir():
                if not job_dir.is_dir():
                    continue
                if self._remove_job_dir(job_dir):
                    removed += 1
        removed += self.report_publisher.purge_all_reports()
        self.conversation_store.clear()
        self.activity_store.clear()
        return removed

    def cleanup_expired_jobs(self, *, now: datetime | None = None) -> int:
        retention = self.config.job_retention
        if not retention.enabled:
            return 0
        jobs_root = self._jobs_root()
        reference_time = now or datetime.now(timezone.utc)
        cutoff_seconds = retention.max_age_hours * 3600
        removed = 0
        if jobs_root.exists():
            for job_dir in jobs_root.iterdir():
                if not job_dir.is_dir():
                    continue
                age_seconds = reference_time.timestamp() - self._latest_job_mtime(job_dir)
                if age_seconds <= cutoff_seconds:
                    continue
                if self._remove_job_dir(job_dir):
                    removed += 1
        removed += self.report_publisher.cleanup_expired_reports(max_age_hours=retention.max_age_hours)
        removed += self.conversation_store.prune_expired(
            max_age_hours=retention.max_age_hours,
            now=reference_time,
        )
        removed += self.activity_store.prune_expired(
            max_age_hours=retention.max_age_hours,
            now=reference_time,
        )
        return removed

    def start_report_server(self) -> None:
        self.report_http_server.start()

    def stop_report_server(self) -> None:
        self.report_http_server.stop()

    def _deliver_result(
        self,
        event: LarkEvent,
        result: TaskResult,
        *,
        request_text: str,
        root_message_id: str | None = None,
    ) -> TaskResult:
        finalized = self._prepare_delivery_result(event, result, request_text=request_text, root_message_id=root_message_id)
        self._send_result(event, finalized)
        return finalized

    def _send_result(self, event: LarkEvent, result: TaskResult) -> None:
        if self.config.dry_run:
            return
        if event.chat_type not in {"group", "p2p"}:
            return
        delivery = str(result.details.get("delivery", "")).strip() or "send"
        session_id = str(result.details.get("conversation_root_message_id") or "").strip() or None
        self._notify_progress(
            "reply_sending",
            "发送文字回复",
            event=event,
            session_id=session_id,
            success=result.success,
            mode=result.details.get("mode", ""),
            delivery=delivery,
        )
        if delivery == "reply" and event.message_id:
            self.lark_client.reply(event.message_id, self._reply_payload(event, result.message))
        else:
            self.lark_client.send_response(event, result.message)
        if not result.success:
            return
        for path in result.details.get("files_to_send", []):
            self._notify_progress(
                "file_uploading",
                f"上传结果文件 {Path(path).name}",
                event=event,
                session_id=session_id,
                path=str(path),
            )
            send_result = self.lark_client.send_file_response(event, Path(path))
            if send_result is None:
                continue
            if send_result.returncode != 0:
                self._notify_progress(
                    "file_upload_failed",
                    f"上传结果文件失败 {Path(path).name}",
                    event=event,
                    session_id=session_id,
                    path=str(path),
                    stderr=(send_result.stderr or send_result.stdout or "unknown error")[:500],
                )
                self.lark_client.send_response(
                    event,
                    f"附件发送失败：{Path(path).name}\n原因：{(send_result.stderr or send_result.stdout or 'unknown error')[:500]}",
                )
            else:
                self._notify_progress(
                    "file_uploaded",
                    f"上传结果文件完成 {Path(path).name}",
                    event=event,
                    session_id=session_id,
                    path=str(path),
                )

    def _omlx_prompt(self, event: LarkEvent, content: str | None = None) -> str | None:
        if not self.config.omlx_chat.enabled:
            return None
        prompt_source = event.content if content is None else content
        chat_command_prompt = self._chat_command_prompt(prompt_source)
        if chat_command_prompt is not None:
            return chat_command_prompt
        if should_use_omlx_chat(prompt_source, max_chars=self.config.omlx_chat.max_prompt_chars):
            return prompt_source
        stripped = prompt_source.strip()
        if event.chat_type != "p2p" or not stripped:
            return None
        if stripped.startswith("/"):
            return None
        return stripped

    def _chat_command_prompt(self, text: str) -> str | None:
        content = self._strip_group_chat_mention(text)
        if content is None:
            content = text.strip()
        prompt = extract_first_keyword_payload(content, CHAT_COMMAND_PREFIXES)
        if prompt is None or not prompt:
            return None
        return prompt

    def _strip_group_chat_mention(self, text: str) -> str | None:
        content = text.strip()
        at_tag = re.match(r'^<at\s+[^>]*user_id="([^"]+)"[^>]*></at>\s*(.*)$', content)
        if at_tag:
            configured_bot = self.config.lark.bot_open_id.strip()
            if configured_bot and at_tag.group(1) != configured_bot:
                return None
            return at_tag.group(2).strip()
        if self.config.lark.bot_open_id.strip():
            return None
        configured_name = self.config.lark.bot_name.strip()
        if configured_name:
            name_prefix = f"@{configured_name}"
            if content.startswith(name_prefix + " "):
                return content[len(name_prefix) :].strip()
            return None
        spaced_name_at = re.match(r"^@.+\s+(/chat(?:\s+.*)?)$", content)
        if spaced_name_at:
            return spaced_name_at.group(1).strip()
        plain_at = re.match(r"^@\S+\s+(.*)$", content)
        if plain_at:
            return plain_at.group(1).strip()
        return None

    def _strip_group_followup_mention(self, event: LarkEvent) -> str | None:
        if not (event.reply_to or event.parent_id or event.root_id or event.thread_id):
            return None
        return self._strip_bot_mention_anywhere(event.content)

    def _strip_bot_mention_anywhere(self, text: str) -> str | None:
        content = text.strip()
        configured_bot = self.config.lark.bot_open_id.strip()
        at_matches = re.findall(r'<at\s+[^>]*user_id="([^"]+)"[^>]*></at>', content)
        if at_matches and (not configured_bot or configured_bot in at_matches):
            cleaned = re.sub(r'<at\s+[^>]*></at>\s*', " ", content)
            return self._normalize_mention_text(cleaned)
        configured_name = self.config.lark.bot_name.strip()
        if configured_name:
            mention_text = f"@{configured_name}"
            if mention_text in content:
                return self._normalize_mention_text(content.replace(mention_text, " "))
            return None
        generic_plain = re.search(r"@\S+", content)
        if generic_plain:
            return self._normalize_mention_text(re.sub(r"@\S+", " ", content, count=1))
        return None

    def _normalize_mention_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    def _jobs_root(self) -> Path:
        return self.config.data_dir / "jobs"

    def _event_progress_callback(self, event: LarkEvent, *, session_id: str | None = None) -> Callable[[dict[str, object]], None]:
        def _callback(progress: dict[str, object]) -> None:
            stage = str(progress.get("stage", "progress"))
            message = str(progress.get("message", ""))
            details = progress.get("details", {})
            if not isinstance(details, dict):
                details = {"value": details}
            details = dict(details)
            nested_session_id = details.pop("session_id", None)
            if nested_session_id and "provider_session_id" not in details:
                details["provider_session_id"] = nested_session_id
            for key in ("stage", "message", "event"):
                if key in details:
                    details[f"progress_{key}"] = details.pop(key)
            self._notify_progress(stage, message, event=event, session_id=session_id, **details)

        return _callback

    def _notify_progress(
        self,
        stage: str,
        message: str,
        *,
        event: LarkEvent | None = None,
        session_id: str | None = None,
        **details: object,
    ) -> None:
        payload: dict[str, object] = {
            "type": "progress",
            "stage": stage,
            "message": message,
        }
        if session_id:
            payload["session_id"] = session_id
        if event is not None:
            payload.update(
                {
                    "event_id": event.event_id,
                    "message_id": event.message_id,
                    "chat_id": event.chat_id,
                    "chat_type": event.chat_type,
                }
            )
        if details:
            payload["details"] = details
        self.activity_store.record_progress(payload)
        if self.progress_callback is None:
            return
        self.progress_callback(payload)

    def _prepare_delivery_result(
        self,
        event: LarkEvent,
        result: TaskResult,
        *,
        request_text: str,
        root_message_id: str | None = None,
    ) -> TaskResult:
        if not result.success:
            return result
        published = self.report_publisher.publish_result(result)
        if published is None:
            return result
        summary_text = self._link_delivery_summary(result.message)
        result.message = f"{summary_text}\n\n报告链接：{published.url}"
        details = dict(result.details)
        details["delivery"] = "reply"
        details["published_report_url"] = published.url
        details["published_report_index"] = str(published.index_path)
        context_root_message_id = root_message_id or event.root_id or event.message_id
        details["conversation_root_message_id"] = context_root_message_id
        if event.chat_type == "group":
            details["files_to_send"] = [Path(path) for path in published.source_report_paths]
        else:
            details.pop("files_to_send", None)
        result.details = details
        self.conversation_store.remember(
            root_message_id=context_root_message_id,
            chat_id=event.chat_id,
            mode=str(details.get("mode", "")),
            request_text=request_text.strip() or self._fallback_request_text(result),
            summary_text=summary_text,
            report_url=published.url,
            report_excerpt=published.context_excerpt,
        )
        return result

    def _handle_intent_routed_event(
        self,
        event: LarkEvent,
        route_content: str,
        *,
        explicit_followup_context,
        latest_chat_context,
    ) -> TaskResult | None:
        self._notify_progress(
            "intent_analysis_started",
            "调用本地 Agent 判断消息意图",
            event=event,
            has_explicit_followup_context=explicit_followup_context is not None,
            has_latest_chat_context=latest_chat_context is not None,
        )
        try:
            decision = self.intent_runner.classify(
                event=event,
                route_content=route_content,
                explicit_followup_context=explicit_followup_context,
                latest_chat_context=latest_chat_context,
            )
        except IntentAnalysisFailure as exc:
            self._notify_progress(
                "intent_analysis_failed",
                str(exc),
                event=event,
                error_code=exc.error_code,
                stderr=(exc.stderr or exc.stdout or "")[:500],
            )
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
            result = TaskResult(
                success=False,
                message=str(exc),
                command=exc.command,
                error_code=exc.error_code,
                stdout=exc.stdout,
                stderr=exc.stderr,
                details={"mode": "intent_analysis"},
            )
            if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
                self._send_result(event, result)
            return result
        self._notify_progress(
            "intent_analysis_completed",
            "本地 Agent 已完成消息意图判断",
            event=event,
            route=decision.route,
            followup_action=decision.followup_action,
            context_source=decision.context_source,
            confidence=decision.confidence,
            reason=decision.reason,
        )
        return self._dispatch_intent_decision(
            event,
            route_content,
            decision,
            explicit_followup_context=explicit_followup_context,
            latest_chat_context=latest_chat_context,
        )

    def _dispatch_intent_decision(
        self,
        event: LarkEvent,
        route_content: str,
        decision: IntentDecision,
        *,
        explicit_followup_context,
        latest_chat_context,
    ) -> TaskResult | None:
        route = decision.route
        if route == "analysis_followup":
            followup_context = self._choose_followup_context(
                decision,
                explicit_followup_context=explicit_followup_context,
                latest_chat_context=latest_chat_context,
            )
            if followup_context is None:
                if not self.state_store.mark_seen(event):
                    return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
                result = self._missing_followup_reply_result(mode="intent_analysis", chat_type=event.chat_type)
                if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
                    self._send_result(event, result)
                return result
            return self._handle_followup(
                event,
                route_content,
                followup_context,
                followup_action=decision.followup_action,
            )
        if route == "signal":
            return self._handle_signal_intent(event, route_content)
        if route == "claude_skill":
            return self._handle_skill_intent(event, route_content)
        if route == "bug":
            return self._handle_bug_intent(event, route_content)
        if route == "direct_analysis":
            return self._handle_direct_analysis_intent(event, route_content)
        if route == "perception_summary":
            return self._handle_perception_intent(event, route_content)
        if route == "chat":
            return self._handle_chat_intent(event, route_content)
        if route == "unsupported":
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
            result = TaskResult(
                success=True,
                message="not a handled request",
                skipped=True,
                details={"mode": "unsupported"},
            )
            self._send_result(event, result)
            return result
        return None

    def _choose_followup_context(self, decision: IntentDecision, *, explicit_followup_context, latest_chat_context):
        _ = decision
        _ = latest_chat_context
        return explicit_followup_context

    def _analysis_context_modes(self) -> set[str]:
        return {
            "bug_analysis",
            "bug_reanalysis",
            "bug_agent_followup",
            "direct_analysis",
            "perception_summary",
            "signal_lifecycle",
        }

    def _latest_analysis_context(self, chat_id: str, *, explicit_followup_context=None):
        latest = self.conversation_store.latest_for_chat(chat_id, modes=self._analysis_context_modes())
        if latest is None:
            return None
        if explicit_followup_context is not None and latest.root_message_id == explicit_followup_context.root_message_id:
            return None
        return latest

    def _missing_followup_reply_result(self, *, mode: str = "followup_guard", chat_type: str = "") -> TaskResult:
        if chat_type == "p2p":
            message = "若要延续上一次分析，请直接回复对应那条分析消息。"
        else:
            message = "若要延续上一次分析，请回复对应那条分析消息；群聊里还需要 @机器人。"
        return TaskResult(
            success=False,
            message=message,
            error_code="missing_followup_reply",
            details={"mode": mode},
        )

    def _allow_log_analysis_in_external_group(
        self,
        event: LarkEvent,
        *,
        followup_context,
        signal_request: SignalRequest,
        bug_request,
        direct_analysis_request,
        perception_request,
    ) -> bool:
        if event.chat_type != "group":
            return False
        if not self.config.allowed_chats:
            return False
        if event.chat_id in self.config.allowed_chats:
            return False
        if followup_context is not None:
            return True
        return bool(
            signal_request.triggered
            or bug_request.triggered
            or direct_analysis_request.triggered
            or perception_request.triggered
        )

    def _build_signal_request(self, route_content: str, referenced_resources: list[DownloadResource]) -> SignalRequest:
        request = parse_signal_request(
            route_content,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
        )
        if not referenced_resources:
            return request
        return SignalRequest(
            signal=request.signal,
            resources=self._merge_resources(request.resources, referenced_resources),
            since=request.since,
            raw_text=request.raw_text,
            triggered=request.triggered,
            error=request.error,
        )

    def _build_perception_summary_request(self, route_content: str, referenced_resources: list[DownloadResource]):
        request = parse_perception_summary_request(route_content)
        merged_resources = self._merge_resources(request.resources, referenced_resources)
        if request.triggered:
            return request.__class__(
                prompt=request.prompt,
                resources=merged_resources,
                raw_text=request.raw_text,
                triggered=True,
                error=request.error,
            )
        if not referenced_resources:
            return request
        hinted = f"{route_content.strip()} {' '.join(item.value for item in referenced_resources)}".strip()
        hinted_request = parse_perception_summary_request(hinted)
        if not hinted_request.triggered:
            return request
        return request.__class__(
            prompt=route_content.strip(),
            resources=merged_resources,
            raw_text=route_content,
            triggered=True,
            error=None if route_content.strip() else "missing_prompt",
        )

    def _build_direct_analysis_request(self, route_content: str, referenced_resources: list[DownloadResource]):
        request = parse_direct_analysis_request(route_content)
        merged_resources = self._merge_resources(request.resources, referenced_resources)
        if request.triggered:
            return request.__class__(
                prompt=request.prompt,
                resources=merged_resources,
                raw_text=request.raw_text,
                triggered=True,
                error=request.error,
            )
        if not referenced_resources:
            return request
        hinted = f"{route_content.strip()} {' '.join(item.value for item in referenced_resources)}".strip()
        hinted_request = parse_direct_analysis_request(hinted)
        if not hinted_request.triggered:
            return request
        return request.__class__(
            prompt=route_content.strip(),
            resources=merged_resources,
            raw_text=route_content,
            triggered=True,
            error=None if route_content.strip() else "missing_prompt",
        )

    def _merge_resources(
        self,
        primary: list[DownloadResource],
        extra: list[DownloadResource],
    ) -> list[DownloadResource]:
        merged: list[DownloadResource] = []
        seen: set[tuple[str, str, str]] = set()
        for item in [*primary, *extra]:
            key = (item.kind, item.value, item.source_message_id.strip())
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    def _fetch_referenced_message_resources(self, event: LarkEvent, *, route_content: str) -> list[DownloadResource]:
        resources: list[DownloadResource] = []
        for message_id in self._candidate_reference_message_ids(event, route_content=route_content):
            fetched = self.lark_client.fetch_message(message_id)
            if fetched.returncode != 0:
                continue
            resources = self._merge_resources(
                resources,
                self._extract_resources_from_message_payload(fetched.stdout, fallback_message_id=message_id),
            )
        return resources

    def _candidate_reference_message_ids(self, event: LarkEvent, *, route_content: str) -> list[str]:
        candidates = [value for value in [event.reply_to, event.parent_id, event.root_id] if value]
        if not candidates and self._should_lookup_current_message_for_resources(event, route_content) and event.message_id:
            fetched_current = self.lark_client.fetch_message(event.message_id)
            if fetched_current.returncode == 0:
                candidates.extend(
                    candidate
                    for candidate in self._extract_message_reference_ids(fetched_current.stdout)
                    if candidate and candidate != event.message_id
                )
        unique: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            value = candidate.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            unique.append(value)
        return unique[:3]

    def _should_lookup_current_message_for_resources(self, event: LarkEvent, route_content: str) -> bool:
        if event.reply_to or event.parent_id or event.root_id:
            return True
        inline_direct = parse_direct_analysis_request(route_content)
        if inline_direct.triggered:
            return not inline_direct.resources
        if self._looks_like_direct_analysis_prompt(route_content):
            return True
        inline_perception = parse_perception_summary_request(route_content)
        if inline_perception.triggered:
            return not inline_perception.resources
        inline_signal = parse_signal_request(
            route_content,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
        )
        if inline_signal.triggered:
            return not inline_signal.resources
        return False

    def _looks_like_direct_analysis_prompt(self, route_content: str) -> bool:
        hinted = f"{route_content.strip()} file_probe"
        return parse_direct_analysis_request(hinted).triggered

    def _extract_resources_from_message_payload(self, payload_text: str, *, fallback_message_id: str = "") -> list[DownloadResource]:
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        data = payload.get("data", {})
        if not isinstance(data, dict):
            return []
        messages = data.get("messages")
        if isinstance(messages, dict):
            messages = [messages]
        if not isinstance(messages, list):
            return []
        resources: list[DownloadResource] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_id = str(message.get("message_id") or fallback_message_id).strip()
            extracted = self._extract_resources_from_message_value(message, source_message_id=message_id)
            resources = self._merge_resources(resources, extracted)
        return resources

    def _extract_resources_from_message_value(self, value: object, *, source_message_id: str) -> list[DownloadResource]:
        resources: list[DownloadResource] = []
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = None
            if parsed is not None and parsed is not value:
                return self._extract_resources_from_message_value(parsed, source_message_id=source_message_id)
            return [
                item
                for item in find_resources(value, source_message_id=source_message_id)
                if item.kind in {"file", "image"}
            ]
        if isinstance(value, dict):
            for key, nested in value.items():
                if key == "file_key" and isinstance(nested, str) and nested.strip():
                    resources = self._merge_resources(
                        resources,
                        [DownloadResource(kind="file", value=nested.strip(), source_message_id=source_message_id)],
                    )
                    continue
                if key == "image_key" and isinstance(nested, str) and nested.strip():
                    resources = self._merge_resources(
                        resources,
                        [DownloadResource(kind="image", value=nested.strip(), source_message_id=source_message_id)],
                    )
                    continue
                resources = self._merge_resources(
                    resources,
                    self._extract_resources_from_message_value(nested, source_message_id=source_message_id),
                )
            return resources
        if isinstance(value, list):
            for item in value:
                resources = self._merge_resources(
                    resources,
                    self._extract_resources_from_message_value(item, source_message_id=source_message_id),
                )
        return resources

    def _handle_signal_intent(self, event: LarkEvent, route_content: str) -> TaskResult:
        request = parse_signal_request(
            route_content,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
        )
        if not request.triggered:
            request = SignalRequest(signal=None, raw_text=route_content, triggered=True, error="missing_signal")
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        result = self.handler.handle(request, event=event)
        return self._deliver_result(event, result, request_text=request.raw_text or route_content)

    def _handle_skill_intent(self, event: LarkEvent, route_content: str) -> TaskResult:
        skill_request = parse_claude_skill_request(
            route_content,
            trigger_prefixes=self.config.claude_agent.trigger_prefixes,
        )
        if not skill_request.triggered:
            skill_request = skill_request.__class__(prompt=route_content.strip(), raw_text=route_content, triggered=True)
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        result = self.claude_runner.run_skill_analysis(skill_request, event=event)
        return self._deliver_result(event, result, request_text=skill_request.raw_text or route_content)

    def _handle_bug_intent(self, event: LarkEvent, route_content: str) -> TaskResult:
        bug_request = parse_bug_request(route_content)
        if not bug_request.triggered:
            bug_request = bug_request.__class__(bug_url="", prompt=route_content.strip(), raw_text=route_content, triggered=True, error="missing_bug_url")
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        self._notify_progress(
            "bug_request_received",
            "收到 bug 分析请求",
            event=event,
            bug_url=bug_request.bug_url,
            prompt=bug_request.prompt,
            raw_text=bug_request.raw_text,
        )
        result = self.bug_runner.run_bug_analysis(
            bug_request,
            event=event,
            progress_callback=self._event_progress_callback(event),
        )
        return self._deliver_result(event, result, request_text=bug_request.raw_text or route_content)

    def _handle_direct_analysis_intent(self, event: LarkEvent, route_content: str) -> TaskResult:
        direct_analysis_request = parse_direct_analysis_request(route_content)
        if not direct_analysis_request.triggered:
            direct_analysis_request = direct_analysis_request.__class__(
                prompt=route_content.strip(),
                resources=[],
                raw_text=route_content,
                triggered=True,
                error="missing_log",
            )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        self._notify_progress(
            "direct_analysis_request_received",
            "收到直传文件分析请求",
            event=event,
            prompt=direct_analysis_request.prompt,
            raw_text=direct_analysis_request.raw_text,
            resources=[item.value for item in direct_analysis_request.resources],
        )
        result = self.bug_runner.run_direct_analysis(
            direct_analysis_request,
            event=event,
            progress_callback=self._event_progress_callback(event),
        )
        return self._deliver_result(event, result, request_text=direct_analysis_request.raw_text or route_content)

    def _handle_perception_intent(self, event: LarkEvent, route_content: str) -> TaskResult:
        perception_request = parse_perception_summary_request(route_content)
        if not perception_request.triggered:
            perception_request = perception_request.__class__(prompt=route_content.strip(), raw_text=route_content, triggered=True)
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        result = self.perception_runner.run_summary(perception_request, event=event)
        return self._deliver_result(event, result, request_text=perception_request.raw_text or route_content)

    def _handle_chat_intent(self, event: LarkEvent, route_content: str) -> TaskResult:
        chat_reply = build_basic_chat_reply(route_content, command_prefixes=self.config.command_prefixes)
        chat_prompt = self._omlx_prompt(event, route_content)
        if chat_reply is None and chat_prompt is not None:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
            result = self.chat_client.reply(chat_prompt)
            return self._deliver_result(event, result, request_text=route_content)
        if chat_reply is not None:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
            result = TaskResult(
                success=True,
                message=chat_reply,
                details={"mode": "basic_chat"},
            )
            return self._deliver_result(event, result, request_text=route_content)
        return None

    def _handle_followup(
        self,
        event: LarkEvent,
        route_content: str,
        followup_context,
        *,
        followup_action: str | None = None,
    ) -> TaskResult:
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        action = (followup_action or "").strip()
        should_reanalyze = action == "reanalysis" or (
            not action and self._is_bug_reanalysis_followup(route_content, followup_context)
        )
        if should_reanalyze:
            previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
            result = self.bug_runner.run_bug_reanalysis(
                followup_text=route_content,
                previous_context=followup_context,
                previous_session=previous_session,
                event=event,
                progress_callback=self._event_progress_callback(event, session_id=followup_context.root_message_id),
            )
            finalized = self._deliver_result(
                event,
                result,
                request_text=f"{followup_context.request_text}\n\n追问/修正：{route_content}",
                root_message_id=followup_context.root_message_id,
            )
            if finalized.success:
                self.conversation_store.append_exchange(
                    followup_context.root_message_id,
                    user_text=route_content,
                    assistant_text=finalized.message,
                )
            return finalized
        if "bug" in str(followup_context.mode).casefold():
            previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
            result = self.bug_runner.run_bug_agent_followup(
                followup_text=route_content,
                previous_context=followup_context,
                previous_session=previous_session,
                event=event,
                progress_callback=self._event_progress_callback(event, session_id=followup_context.root_message_id),
            )
            return self._finalize_followup_reply(event, result, followup_context, route_content)
        result = self.chat_client.reply_with_context(
            route_content,
            request_text=followup_context.request_text,
            summary_text=followup_context.summary_text,
            report_excerpt=followup_context.report_excerpt,
            history=followup_context.history,
            report_url=followup_context.report_url,
        )
        return self._finalize_followup_reply(event, result, followup_context, route_content)

    def _finalize_followup_reply(self, event: LarkEvent, result: TaskResult, followup_context, route_content: str) -> TaskResult:
        result.details["delivery"] = "reply"
        result.details["conversation_root_message_id"] = followup_context.root_message_id
        if result.success and followup_context.report_url and followup_context.report_url not in result.message:
            if result.message.strip():
                result.message = f"{result.message}\n\n报告链接：{followup_context.report_url}"
            else:
                result.message = f"报告链接：{followup_context.report_url}"
        if result.success:
            self.conversation_store.append_exchange(
                followup_context.root_message_id,
                user_text=route_content,
                assistant_text=result.message,
            )
        self._send_result(event, result)
        return result

    def _link_delivery_summary(self, message: str) -> str:
        lines = []
        for raw_line in (message or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            lowered = line.casefold()
            if lowered.startswith(("html", "json", "reports:", "metadata:", "job:", "日志路径:", "耗时:", "结果文件:")):
                continue
            if "/jobs/" in line or "\\jobs\\" in line:
                continue
            lines.append(line)
        summary = "\n".join(lines[:8]).strip()
        if len(summary) > 1200:
            summary = summary[:1199].rstrip() + "…"
        return summary or "分析完成"

    def _fallback_request_text(self, result: TaskResult) -> str:
        for key in ("user_request_text", "prompt"):
            value = result.details.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return result.message.splitlines()[0].strip() if result.message else ""

    def _reply_payload(self, event: LarkEvent, text: str) -> str:
        if event.chat_type == "group" and self.config.lark.mention_sender_in_group and event.sender_id:
            return f'<at user_id="{event.sender_id}"></at> {text}'
        return text

    def _resolve_followup_context(self, event: LarkEvent):
        context = self.conversation_store.find(event)
        if context is not None:
            return context
        for key in self._fetch_followup_reference_ids(event):
            context = self.conversation_store.lookup(key)
            if context is not None:
                return context
        return None

    def _fetch_followup_reference_ids(self, event: LarkEvent) -> list[str]:
        pending = [value for value in [event.reply_to, event.parent_id, event.root_id] if value]
        if not pending and event.message_id:
            fetched_current = self.lark_client.fetch_message(event.message_id)
            if fetched_current.returncode == 0:
                pending.extend(
                    candidate
                    for candidate in self._extract_message_reference_ids(fetched_current.stdout)
                    if candidate and candidate != event.message_id
                )
        visited: set[str] = set()
        discovered: list[str] = []
        while pending and len(visited) < 6:
            current = pending.pop(0)
            if not current or current in visited:
                continue
            visited.add(current)
            fetched = self.lark_client.fetch_message(current)
            if fetched.returncode != 0:
                continue
            for candidate in self._extract_message_reference_ids(fetched.stdout):
                if not candidate or candidate in visited:
                    continue
                if candidate not in discovered:
                    discovered.append(candidate)
                pending.append(candidate)
        return discovered

    def _extract_message_reference_ids(self, payload_text: str) -> list[str]:
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        data = payload.get("data", {})
        if not isinstance(data, dict):
            return []
        messages = data.get("messages")
        if isinstance(messages, dict):
            messages = [messages]
        if not isinstance(messages, list):
            return []
        ids: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            for key in ("message_id", "reply_to", "root_id", "parent_id", "thread_id"):
                value = message.get(key)
                if isinstance(value, str) and value.strip():
                    ids.append(value.strip())
        return ids

    def _is_contextual_followup(self, event: LarkEvent, route_content: str) -> bool:
        if not route_content.strip():
            return False
        return bool(event.reply_to or event.parent_id or event.root_id or event.thread_id)

    def _is_followup_intent(self, route_content: str) -> bool:
        lowered = route_content.casefold()
        return any(
            term in lowered
            for term in (
                "修正",
                "修复问题时间",
                "更正",
                "改成",
                "修改",
                "重新分析",
                "重新跑",
                "重跑",
                "再分析",
                "上次",
                "上一条",
                "这个报告",
                "这份报告",
                "问题时间",
                "故障时间",
                "时间点",
                "用你之前下载",
                "之前下载",
                "下载下来",
                "之前的日志",
                "日志搜索",
                "logd",
                "关键字",
                "卡顿skill",
                "卡顿 skill",
                "系统卡顿报告",
            )
        )

    def _is_bug_reanalysis_followup(self, route_content: str, followup_context) -> bool:
        if "bug" not in str(followup_context.mode).casefold():
            return False
        lowered = route_content.casefold()
        if any(term in lowered for term in ("重新分析", "重新跑", "重跑", "再分析")):
            return True
        has_correction = any(term in lowered for term in ("修正", "修复问题时间", "更正", "修改", "改成"))
        has_time = re.search(r"(?<!\d)\d{1,2}[:：]\d{2}(?:\s*分)?(?!\d)", route_content) is not None
        return has_correction and has_time

    def _latest_job_mtime(self, job_dir: Path) -> float:
        latest = 0.0
        try:
            for child in job_dir.rglob("*"):
                if not child.is_file():
                    continue
                try:
                    child_mtime = child.stat().st_mtime
                except OSError:
                    continue
                if child_mtime > latest:
                    latest = child_mtime
        except OSError:
            latest = 0.0
        if latest == 0.0:
            try:
                latest = job_dir.stat().st_mtime
            except OSError:
                latest = 0.0
        return latest

    def _remove_job_dir(self, job_dir: Path) -> bool:
        try:
            shutil.rmtree(job_dir, onerror=self._handle_rmtree_error)
            return True
        except OSError:
            return False

    def _handle_rmtree_error(self, func, path, exc_info) -> None:
        try:
            os.chmod(path, stat.S_IRWXU)
        except OSError:
            pass
        func(path)
