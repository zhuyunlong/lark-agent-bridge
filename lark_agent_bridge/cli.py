"""Command line interface for lark-agent-bridge."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading

from .app import BridgeApp
from .config import load_config, with_cli_overrides


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m lark_agent_bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="Check local configuration and lark-cli availability.")
    _add_common_options(check)

    handle_event = subparsers.add_parser("handle-event", help="Handle one Feishu event JSON file.")
    _add_common_options(handle_event)
    handle_event.add_argument("--event", required=True, help="Path to an im.message.receive_v1 sample event JSON.")

    run_signal = subparsers.add_parser("run-signal", help="Run signal lifecycle analysis directly.")
    _add_common_options(run_signal)
    run_signal.add_argument("--signal", required=True, help="Signal code or SIGNAL_* enum.")
    run_signal.add_argument("--log-path", required=True, help="Local log file or directory.")
    run_signal.add_argument("--since", help="Optional time range passed to the analyzer.")

    listen = subparsers.add_parser("listen", help="Listen to Feishu Bot message events.")
    _add_common_options(listen)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = with_cli_overrides(load_config(args.config), dry_run=args.dry_run)
        progress_callback = _print_progress if args.command == "listen" and not config.dry_run else None
        app = BridgeApp(config, progress_callback=progress_callback)
        if args.command == "check":
            _print_json(app.check())
            return 0
        if args.command == "handle-event":
            payload = json.loads(Path(args.event).read_text(encoding="utf-8"))
            _print_json(app.handle_event_payload(payload).to_dict())
            return 0
        if args.command == "run-signal":
            _print_json(app.run_signal(signal=args.signal, log_path=args.log_path, since=args.since).to_dict())
            return 0
        if args.command == "listen":
            if config.dry_run:
                print("dry-run: listen would consume im.message.receive_v1 events with lark-cli")
                return 0
            if config.job_retention.purge_all_on_listen_start:
                app.purge_all_jobs()
            app.start_report_server()
            stop_cleanup = _start_cleanup_loop(app)
            try:
                for event in app.lark_client.consume_events():
                    _print_json(app.handle_event(event).to_dict())
            finally:
                app.stop_report_server()
                if stop_cleanup is not None:
                    stop_cleanup.set()
            return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unknown command: {args.command}")
    return 2


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="Path to TOML config. Defaults to safe built-in dry-run config.")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run mode.")


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _print_progress(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def _start_cleanup_loop(app: BridgeApp) -> threading.Event | None:
    retention = app.config.job_retention
    if not retention.enabled:
        return None
    try:
        interval_seconds = int(retention.cleanup_interval_seconds)
    except (TypeError, ValueError):
        return None
    if interval_seconds <= 0:
        return None
    stop_event = threading.Event()

    def _worker() -> None:
        while not stop_event.wait(interval_seconds):
            app.cleanup_expired_jobs()

    threading.Thread(target=_worker, name="job-retention-cleanup", daemon=True).start()
    return stop_event
