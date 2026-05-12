"""Run the guideengine signal-chain analyzer."""

from __future__ import annotations

from pathlib import Path
import subprocess
import time

from .models import BridgeConfig, TaskResult


SCRIPT_PATH = ".github/skills/signal-chain-analyzer/scripts/analyze_signal_chain.py"


class SignalChainRunner:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config

    def build_command(
        self,
        *,
        signal: str,
        log_path: str | Path,
        html_output: str | Path,
        json_output: str | Path,
        since: str | None = None,
    ) -> list[str]:
        command = [
            "python3",
            SCRIPT_PATH,
            "--signal-code",
            str(signal),
            "--log-path",
            str(log_path),
            "--output",
            str(html_output),
            "--json-output",
            str(json_output),
        ]
        if since:
            command.extend(["--since", since])
        return command

    def run(
        self,
        *,
        signal: str,
        log_path: str | Path,
        output_dir: str | Path,
        since: str | None = None,
    ) -> TaskResult:
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        html_output = output / "signal_chain.html"
        json_output = output / "signal_chain.json"
        command = self.build_command(
            signal=signal,
            log_path=log_path,
            html_output=html_output,
            json_output=json_output,
            since=since,
        )
        if self.config.dry_run:
            return TaskResult(
                success=True,
                message="dry-run: runner command planned",
                html_report=html_output,
                json_report=json_output,
                command=command,
            )

        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=self.config.guideengine_repo,
                capture_output=True,
                text=True,
                timeout=self.config.runner_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return TaskResult(
                success=False,
                message="signal-chain analyzer timed out",
                html_report=html_output,
                json_report=json_output,
                command=command,
                duration_seconds=time.monotonic() - started,
                error_code="runner_timeout",
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
            )
        except OSError as exc:
            return TaskResult(
                success=False,
                message=str(exc),
                html_report=html_output,
                json_report=json_output,
                command=command,
                duration_seconds=time.monotonic() - started,
                error_code="runner_failed_to_start",
                stderr=str(exc),
            )

        if completed.returncode != 0:
            return TaskResult(
                success=False,
                message="signal-chain analyzer failed",
                html_report=html_output,
                json_report=json_output,
                command=command,
                duration_seconds=time.monotonic() - started,
                error_code="runner_failed",
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        if not html_output.exists() or not json_output.exists():
            return TaskResult(
                success=False,
                message="signal-chain analyzer did not produce expected reports",
                html_report=html_output,
                json_report=json_output,
                command=command,
                duration_seconds=time.monotonic() - started,
                error_code="runner_missing_output",
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        return TaskResult(
            success=True,
            message="signal-chain analyzer completed",
            html_report=html_output,
            json_report=json_output,
            command=command,
            duration_seconds=time.monotonic() - started,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

