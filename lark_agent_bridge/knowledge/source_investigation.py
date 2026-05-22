"""Read-only Codex CLI source investigation for knowledge QA."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import subprocess
import time
from typing import Any

from ..models import BridgeConfig


@dataclass(slots=True)
class SourceInvestigationResult:
    success: bool
    answer: str = ""
    canonical_key: str = ""
    confidence: float = 0.0
    commands: list[str] = field(default_factory=list)
    source_evidence: list[dict[str, Any]] = field(default_factory=list)
    coverage_boundary: str = ""
    writeback_allowed: bool = False
    error: str = ""
    command: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""


class SourceInvestigationRunner:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config

    def run(self, question: str) -> SourceInvestigationResult:
        options = self.config.source_investigation
        if not options.enabled:
            return SourceInvestigationResult(success=False, error="source investigation disabled")
        if options.provider.strip().casefold() != "codex":
            return SourceInvestigationResult(success=False, error=f"unsupported provider: {options.provider}")
        repo_roots = [path.expanduser() for path in (options.repo_roots or [self.config.guideengine_repo])]
        primary_root = repo_roots[0] if repo_roots else self.config.guideengine_repo
        output_path = self._output_path()
        command = self._build_command(question=question, output_path=output_path, primary_root=primary_root)
        try:
            completed = subprocess.run(
                command,
                cwd=str(primary_root),
                capture_output=True,
                text=True,
                timeout=max(1, int(options.timeout_seconds)),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return SourceInvestigationResult(
                success=False,
                error=f"source investigation timed out after {exc.timeout}s",
                command=command,
                stdout=str(exc.output or ""),
                stderr=str(exc.stderr or ""),
            )
        except OSError as exc:
            return SourceInvestigationResult(success=False, error=str(exc), command=command)
        if completed.returncode != 0:
            return SourceInvestigationResult(
                success=False,
                error=(completed.stderr or completed.stdout or f"codex exited {completed.returncode}")[:1000],
                command=command,
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
            )
        raw = ""
        if output_path.is_file():
            raw = output_path.read_text(encoding="utf-8", errors="replace")
        if not raw.strip():
            raw = _last_agent_message(completed.stdout or "") or _last_json_object(completed.stdout or "")
        parsed = _parse_json_object(raw)
        if parsed is None:
            return SourceInvestigationResult(
                success=False,
                error="source investigation returned non-json output",
                command=command,
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
            )
        schema_error = _schema_error(parsed)
        if schema_error:
            return SourceInvestigationResult(
                success=False,
                error=f"source investigation returned invalid schema: {schema_error}",
                command=command,
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
            )
        return _result_from_payload(parsed, command=command, stdout=completed.stdout or "", stderr=completed.stderr or "")

    def _build_command(self, *, question: str, output_path: Path, primary_root: Path) -> list[str]:
        options = self.config.source_investigation
        command = [
            options.command.strip() or "codex",
            "exec",
            "--skip-git-repo-check",
            "-s",
            "read-only",
            "-C",
            str(primary_root),
        ]
        for path in options.add_dirs:
            command.extend(["--add-dir", str(path.expanduser())])
        if options.model.strip():
            command.extend(["-m", options.model.strip()])
        command.extend(
            [
                "--json",
                "--output-last-message",
                str(output_path),
                self._prompt(question),
            ]
        )
        return command

    def _prompt(self, question: str) -> str:
        options = self.config.source_investigation
        repo_roots = options.repo_roots or [self.config.guideengine_repo]
        add_dirs = options.add_dirs
        return (
            "你是 lark-agent-bridge 的只读源码调查 agent。\n"
            "目标：回答用户的知识库/ADB/源码可验证问题，并判断是否可沉淀为知识库模板。\n"
            "限制：只读；优先使用 rg 搜索锚点；只读取关键片段；不要读取全仓大文件；不要修改文件。\n"
            f"用户问题：{question}\n"
            "源码根：\n"
            + "\n".join(f"- {path}" for path in repo_roots)
            + ("\n附加只读目录：\n" + "\n".join(f"- {path}" for path in add_dirs) if add_dirs else "")
            + "\n输出必须是一个 JSON 对象，不要 Markdown，不要代码块。字段："
            "answer(string), canonical_key(string), confidence(number 0-1), commands(array string), "
            "source_evidence(array object with file,line,text), coverage_boundary(string), writeback_allowed(boolean)。"
            f"source_evidence 最多 {max(1, int(options.max_evidence))} 条。"
            "只有源码证据能支持结论时 writeback_allowed 才能为 true。"
        )

    def _output_path(self) -> Path:
        base = (self.config.data_dir / "source_investigations").expanduser()
        base.mkdir(parents=True, exist_ok=True)
        return (base / f"source_investigation_{int(time.time() * 1000)}.json").resolve()


def _result_from_payload(
    payload: dict[str, Any],
    *,
    command: list[str],
    stdout: str,
    stderr: str,
) -> SourceInvestigationResult:
    evidence = payload.get("source_evidence")
    commands = payload.get("commands")
    result = SourceInvestigationResult(
        success=True,
        answer=str(payload.get("answer") or "").strip(),
        canonical_key=str(payload.get("canonical_key") or "").strip(),
        confidence=_float(payload.get("confidence")),
        commands=[str(item) for item in commands if str(item).strip()] if isinstance(commands, list) else [],
        source_evidence=[item for item in evidence if isinstance(item, dict)] if isinstance(evidence, list) else [],
        coverage_boundary=str(payload.get("coverage_boundary") or "").strip(),
        writeback_allowed=bool(payload.get("writeback_allowed")),
        command=command,
        stdout=stdout,
        stderr=stderr,
    )
    if not result.answer:
        result.success = False
        result.error = "source investigation returned empty answer"
    return result


def _schema_error(payload: dict[str, Any]) -> str:
    required = {
        "answer": str,
        "canonical_key": str,
        "confidence": (int, float),
        "commands": list,
        "source_evidence": list,
        "coverage_boundary": str,
        "writeback_allowed": bool,
    }
    for key, expected_type in required.items():
        if key not in payload:
            return f"missing {key}"
        if not isinstance(payload[key], expected_type):
            return f"{key} must be {expected_type}"
    return ""


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _last_json_object(raw: str) -> str:
    for line in reversed((raw or "").splitlines()):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return stripped
    return raw


def _last_agent_message(raw: str) -> str:
    message = ""
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            message = text.strip()
    return message


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
