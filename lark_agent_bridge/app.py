"""Application orchestration for the bridge."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import stat

from .agents import BugAnalysisRunner, ClaudeSkillRunner, OmlxChatClient, PerceptionSummaryRunner
from .downloader import LogDownloader
from .lark_client import LarkClient
from .models import BridgeConfig, DownloadResource, LarkEvent, SignalRequest, TaskResult, create_job_context
from .parser import (
    build_basic_chat_reply,
    extract_first_keyword_payload,
    parse_claude_skill_request,
    parse_bug_request,
    parse_direct_analysis_request,
    parse_perception_summary_request,
    parse_signal_request,
    should_use_omlx_chat,
)
from .policy import build_policy_rejection_message, evaluate_event_policy
from .runner import SignalChainRunner
from .state import EventStateStore
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
    ) -> None:
        self.config = config
        self.lark_client = lark_client or LarkClient(config)
        self.state_store = state_store or EventStateStore(config.data_dir / "state" / "seen_events.jsonl")
        runner = SignalChainRunner(config)
        downloader = LogDownloader(config, self.lark_client)
        self.handler = handler or SignalLifecycleHandler(config, downloader, runner)
        self.claude_runner = claude_runner or ClaudeSkillRunner(config)
        self.bug_runner = bug_runner or BugAnalysisRunner(config)
        setattr(self.bug_runner, "_lark_client", self.lark_client)
        self.perception_runner = perception_runner or PerceptionSummaryRunner(config, self.lark_client)
        self.chat_client = chat_client or OmlxChatClient(config)

    def check(self) -> dict[str, object]:
        return {
            "dry_run": self.config.dry_run,
            "data_dir": str(self.config.data_dir),
            "guideengine_repo": str(self.config.guideengine_repo),
            "lark": self.lark_client.check_environment(),
        }

    def handle_event_payload(self, payload: dict[str, object]) -> TaskResult:
        return self.handle_event(LarkEvent.from_dict(payload))

    def handle_event(self, event: LarkEvent) -> TaskResult:
        self.cleanup_expired_jobs()
        route_content = event.content
        if event.chat_type == "group":
            addressed_content = self._strip_group_chat_mention(event.content)
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

        request = parse_signal_request(
            route_content,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
        )
        if request.triggered:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)

            result = self.handler.handle(request, event=event)
            self._send_result(event, result)
            return result

        skill_request = parse_claude_skill_request(
            route_content,
            trigger_prefixes=self.config.claude_agent.trigger_prefixes,
        )
        if skill_request.triggered:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)

            result = self.claude_runner.run_skill_analysis(skill_request, event=event)
            self._send_result(event, result)
            return result

        bug_request = parse_bug_request(route_content)
        if bug_request.triggered:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)

            result = self.bug_runner.run_bug_analysis(bug_request, event=event)
            self._send_result(event, result)
            return result

        direct_analysis_request = parse_direct_analysis_request(route_content)
        if direct_analysis_request.triggered:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)

            result = self.bug_runner.run_direct_analysis(direct_analysis_request, event=event)
            self._send_result(event, result)
            return result

        perception_request = parse_perception_summary_request(route_content)
        if perception_request.triggered:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)

            result = self.perception_runner.run_summary(perception_request, event=event)
            self._send_result(event, result)
            return result

        chat_reply = build_basic_chat_reply(route_content, command_prefixes=self.config.command_prefixes)
        chat_prompt = self._omlx_prompt(event, route_content)
        if chat_reply is None and chat_prompt is not None:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)

            result = self.chat_client.reply(chat_prompt)
            self._send_result(event, result)
            return result

        if chat_reply is not None:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)

            result = TaskResult(
                success=True,
                message=chat_reply,
                details={"mode": "basic_chat"},
            )
            self._send_result(event, result)
            return result

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
        if not jobs_root.exists():
            return 0
        removed = 0
        for job_dir in jobs_root.iterdir():
            if not job_dir.is_dir():
                continue
            if self._remove_job_dir(job_dir):
                removed += 1
        return removed

    def cleanup_expired_jobs(self, *, now: datetime | None = None) -> int:
        retention = self.config.job_retention
        if not retention.enabled:
            return 0
        jobs_root = self._jobs_root()
        if not jobs_root.exists():
            return 0
        reference_time = now or datetime.now(timezone.utc)
        cutoff_seconds = retention.max_age_hours * 3600
        removed = 0
        for job_dir in jobs_root.iterdir():
            if not job_dir.is_dir():
                continue
            age_seconds = reference_time.timestamp() - self._latest_job_mtime(job_dir)
            if age_seconds <= cutoff_seconds:
                continue
            if self._remove_job_dir(job_dir):
                removed += 1
        return removed

    def _send_result(self, event: LarkEvent, result: TaskResult) -> None:
        if self.config.dry_run:
            return
        if event.chat_type not in {"group", "p2p"}:
            return
        self.lark_client.send_response(event, result.message)
        if not result.success:
            return
        for path in result.details.get("files_to_send", []):
            send_result = self.lark_client.send_file_response(event, Path(path))
            if send_result is None:
                continue
            if send_result.returncode != 0:
                self.lark_client.send_response(
                    event,
                    f"附件发送失败：{Path(path).name}\n原因：{(send_result.stderr or send_result.stdout or 'unknown error')[:500]}",
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

    def _jobs_root(self) -> Path:
        return self.config.data_dir / "jobs"

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
