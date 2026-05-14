"""Shared data models for the Lark agent bridge."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any


@dataclass(slots=True)
class DownloadConfig:
    max_bytes: int = 5 * 1024 * 1024 * 1024
    timeout_seconds: int = 60


@dataclass(slots=True)
class JobRetentionOptions:
    enabled: bool = True
    max_age_hours: int = 6
    purge_all_on_listen_start: bool = True
    cleanup_interval_seconds: int = 60


@dataclass(slots=True)
class LarkOptions:
    reply_in_thread: bool = False
    mention_sender_in_group: bool = True
    bot_open_id: str = ""
    bot_name: str = ""


@dataclass(slots=True)
class ClaudeAgentOptions:
    enabled: bool = True
    command: str = "claude"
    trigger_prefixes: list[str] = field(default_factory=lambda: ["/skill", "/claude"])
    working_dir: Path | None = None
    add_dirs: list[Path] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=lambda: ["Read", "Grep", "Glob", "LS"])
    model: str | None = None
    agent: str | None = None
    permission_mode: str = "dontAsk"
    timeout_seconds: int = 1800
    max_prompt_chars: int = 12000
    upload_result_file: bool = True
    system_prompt: str = (
        "你是一个通过飞书触发的本地 Claude Code 分析 agent。"
        "只做只读分析，不修改文件，不执行写入动作。"
        "优先根据用户原文选择合适的 Claude Code skill 或分析路径。"
        "输出中文，结论先行，包含关键证据、风险和后续建议。"
    )


@dataclass(slots=True)
class BugAnalysisOptions:
    enabled: bool = True
    provider: str = "claude"
    command: str = "claude"
    working_dir: Path | None = None
    timeout_seconds: int = 5400
    max_prompt_chars: int = 16000
    upload_result_files: bool = True
    default_prompt: str = "调查3D启动时序"


@dataclass(slots=True)
class IntentAnalysisOptions:
    enabled: bool = False
    provider: str = ""
    command: str = ""
    working_dir: Path | None = None
    timeout_seconds: int = 180
    max_prompt_chars: int = 12000
    system_prompt: str = (
        "你是 Lark Agent Bridge 的意图路由器。"
        "你只能根据输入消息和给定上下文判断路由，不要调用工具，不要假装读取文件。"
        "你必须只输出一个 JSON 对象，不要输出 Markdown、解释或代码块。"
    )


@dataclass(slots=True)
class OmlxChatOptions:
    enabled: bool = True
    base_url: str = "http://127.0.0.1:8000/v1"
    model: str = "gemma-4-26b-a4b-it-4bit"
    api_key: str = ""
    timeout_seconds: int = 120
    max_prompt_chars: int = 2000
    max_tokens: int = 1024
    temperature: float = 0.3
    system_prompt: str = (
        "你是一个本地普通聊天助手。"
        "你没有文件系统、命令行、飞书管理、网络检索或代码修改权限。"
        "只回答普通聊天问题；如果用户要求日志分析、仓库分析、执行命令或访问文件，"
        "请提醒用户改用 /skill 或 /signal。"
    )
    followup_max_context_chars: int = 12000
    followup_max_history_turns: int = 6
    followup_system_prompt: str = (
        "你是一个飞书里的分析结果讲解助手。"
        "你只能基于当前提供的分析摘要、报告摘录和历史追问继续回答，"
        "不要假装读取新文件、重新跑脚本或访问外部链接。"
        "如果上下文里没有足够信息，就明确说明缺少证据。"
        "输出中文，结论先行。"
    )


@dataclass(slots=True)
class ReportServerOptions:
    enabled: bool = True
    bind_host: str = "127.0.0.1"
    port: int = 8765
    public_base_url: str = "http://127.0.0.1:8765/reports"


@dataclass(slots=True)
class BridgeConfig:
    dry_run: bool = True
    workspace_root: Path = field(default_factory=lambda: Path.cwd())
    guideengine_repo: Path = field(default_factory=lambda: Path.cwd())
    data_dir: Path = field(default_factory=lambda: Path("data"))
    allowed_chats: list[str] = field(default_factory=list)
    allowed_users: list[str] = field(default_factory=list)
    command_prefixes: list[str] = field(default_factory=lambda: ["/signal"])
    signal_aliases: dict[str, str] = field(default_factory=dict)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    job_retention: JobRetentionOptions = field(default_factory=JobRetentionOptions)
    lark: LarkOptions = field(default_factory=LarkOptions)
    claude_agent: ClaudeAgentOptions = field(default_factory=ClaudeAgentOptions)
    bug_analysis: BugAnalysisOptions = field(default_factory=BugAnalysisOptions)
    intent_analysis: IntentAnalysisOptions = field(default_factory=IntentAnalysisOptions)
    omlx_chat: OmlxChatOptions = field(default_factory=OmlxChatOptions)
    report_server: ReportServerOptions = field(default_factory=ReportServerOptions)
    runner_timeout_seconds: int = 900


@dataclass(slots=True)
class IntentDecision:
    route: str
    reason: str = ""
    confidence: str = ""
    followup_action: str = ""
    context_source: str = ""
    raw_response: str = ""


@dataclass(slots=True)
class LarkEvent:
    event_id: str
    message_id: str
    chat_id: str
    chat_type: str
    sender_id: str
    message_type: str
    content: str
    create_time: str = ""
    timestamp: str = ""
    reply_to: str = ""
    parent_id: str = ""
    root_id: str = ""
    thread_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LarkEvent":
        header = payload.get("header") or {}
        event_body = payload.get("event") or payload
        message = event_body.get("message") or event_body
        sender = event_body.get("sender") or {}
        sender_id = event_body.get("sender_id") or payload.get("sender_id") or ""
        if isinstance(sender_id, dict):
            sender_id = sender_id.get("open_id") or sender_id.get("user_id") or ""
        if not sender_id and isinstance(sender, dict):
            nested_sender_id = sender.get("sender_id") or {}
            if isinstance(nested_sender_id, dict):
                sender_id = (
                    nested_sender_id.get("open_id")
                    or nested_sender_id.get("user_id")
                    or nested_sender_id.get("union_id")
                    or ""
                )

        return cls(
            event_id=str(payload.get("event_id") or header.get("event_id") or ""),
            message_id=str(payload.get("message_id") or message.get("message_id") or ""),
            chat_id=str(payload.get("chat_id") or message.get("chat_id") or ""),
            chat_type=str(payload.get("chat_type") or message.get("chat_type") or ""),
            sender_id=str(sender_id),
            message_type=str(payload.get("message_type") or message.get("message_type") or message.get("msg_type") or ""),
            content=_coerce_message_content(payload.get("content") or message.get("content") or ""),
            create_time=str(payload.get("create_time") or message.get("create_time") or ""),
            timestamp=str(payload.get("timestamp") or header.get("create_time") or ""),
            reply_to=str(payload.get("reply_to") or message.get("reply_to") or ""),
            parent_id=str(payload.get("parent_id") or message.get("parent_id") or ""),
            root_id=str(payload.get("root_id") or message.get("root_id") or ""),
            thread_id=str(payload.get("thread_id") or message.get("thread_id") or ""),
            raw=payload,
        )


def _coerce_message_content(content: Any) -> str:
    if isinstance(content, dict):
        value = content.get("text") or content.get("content")
        return str(value if value is not None else content)
    if not isinstance(content, str):
        return str(content)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content
    if isinstance(parsed, dict):
        value = parsed.get("text") or parsed.get("content")
        if value is not None:
            return str(value)
    return content


@dataclass(slots=True)
class DownloadResource:
    kind: str
    value: str
    source_message_id: str = ""

    @property
    def resource_type(self) -> str:
        if self.kind == "image":
            return "image"
        if self.kind == "file":
            return "file"
        return self.kind


@dataclass(slots=True)
class SignalRequest:
    signal: str | None
    resources: list[DownloadResource] = field(default_factory=list)
    since: str | None = None
    raw_text: str = ""
    triggered: bool = False
    error: str | None = None


@dataclass(slots=True)
class ClaudeSkillRequest:
    prompt: str
    raw_text: str = ""
    triggered: bool = False
    error: str | None = None


@dataclass(slots=True)
class PerceptionSummaryRequest:
    prompt: str
    resources: list[DownloadResource] = field(default_factory=list)
    raw_text: str = ""
    triggered: bool = False
    error: str | None = None


@dataclass(slots=True)
class DirectAnalysisRequest:
    prompt: str
    resources: list[DownloadResource] = field(default_factory=list)
    raw_text: str = ""
    triggered: bool = False
    error: str | None = None


@dataclass(slots=True)
class BugRequest:
    bug_url: str
    prompt: str = ""
    raw_text: str = ""
    triggered: bool = False
    error: str | None = None


@dataclass(slots=True)
class JobContext:
    job_id: str
    job_dir: Path
    input_dir: Path
    output_dir: Path
    logs_dir: Path


@dataclass(slots=True)
class DownloadedResource:
    resource: DownloadResource
    path: Path
    dry_run: bool = False
    command: list[str] | None = None


@dataclass(slots=True)
class TaskResult:
    success: bool
    message: str
    skipped: bool = False
    job_id: str | None = None
    job_dir: Path | None = None
    html_report: Path | None = None
    json_report: Path | None = None
    command: list[str] | None = None
    duration_seconds: float | None = None
    error_code: str | None = None
    stdout: str = ""
    stderr: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(
            {
                "success": self.success,
                "message": self.message,
                "skipped": self.skipped,
                "job_id": self.job_id,
                "job_dir": self.job_dir,
                "html_report": self.html_report,
                "json_report": self.json_report,
                "command": self.command,
                "duration_seconds": self.duration_seconds,
                "error_code": self.error_code,
                "stdout": self.stdout,
                "stderr": self.stderr,
                "details": self.details,
            }
        )


def create_job_context(data_dir: Path, event: LarkEvent | None = None, job_id: str | None = None) -> JobContext:
    base_dir = Path(data_dir).expanduser().resolve()
    if job_id is None:
        if event and event.event_id:
            job_id = event.event_id
        else:
            job_id = "manual_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    safe_job_id = _safe_identifier(job_id)
    job_dir = base_dir / "jobs" / safe_job_id
    input_dir = job_dir / "input"
    output_dir = job_dir / "output"
    logs_dir = job_dir / "logs"
    for directory in (input_dir, output_dir, logs_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return JobContext(
        job_id=safe_job_id,
        job_dir=job_dir,
        input_dir=input_dir,
        output_dir=output_dir,
        logs_dir=logs_dir,
    )


def _safe_identifier(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    return safe or "job"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value
