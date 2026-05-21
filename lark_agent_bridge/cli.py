"""Command line interface for lark-agent-bridge."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading

from .app import BridgeApp
from .config import load_config, with_cli_overrides
from .knowledge import KnowledgeService
from .log import get_logger, setup_logging

logger = get_logger("cli")


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

    knowledge = subparsers.add_parser("knowledge", help="Manage and query the local knowledge base.")
    knowledge_subparsers = knowledge.add_subparsers(dest="knowledge_command", required=True)
    knowledge_sync = knowledge_subparsers.add_parser("sync", help="Sync configured knowledge sources.")
    _add_common_options(knowledge_sync)
    knowledge_sources = knowledge_subparsers.add_parser("sources", help="List indexed knowledge sources.")
    _add_common_options(knowledge_sources)
    knowledge_search = knowledge_subparsers.add_parser("search", help="Search the knowledge index.")
    _add_common_options(knowledge_search)
    knowledge_search.add_argument("query", nargs="+", help="Search query.")
    knowledge_answer = knowledge_subparsers.add_parser("answer", help="Answer a question from the knowledge index.")
    _add_common_options(knowledge_answer)
    knowledge_answer.add_argument("question", nargs="+", help="Question text.")
    knowledge_add = knowledge_subparsers.add_parser("add", help="Add one manual knowledge item.")
    _add_common_options(knowledge_add)
    knowledge_add.add_argument("--source-id", default="manual", help="Manual source id.")
    knowledge_add.add_argument("--title", required=True, help="Knowledge title.")
    knowledge_add.add_argument("--content", required=True, help="Knowledge content.")
    knowledge_add.add_argument("--source-ref", default="", help="Optional source reference.")
    knowledge_add_source = knowledge_subparsers.add_parser("add-source", help="Register an empty manual source.")
    _add_common_options(knowledge_add_source)
    knowledge_add_source.add_argument("--source-id", required=True, help="Source id.")
    knowledge_add_source.add_argument("--type", default="manual", help="Source type.")
    knowledge_add_source.add_argument("--title", default="", help="Source title.")
    knowledge_add_source.add_argument("--source-ref", default="", help="Source reference.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging()
    try:
        config = with_cli_overrides(load_config(args.config), dry_run=args.dry_run)
        progress_callback = _print_progress if args.command == "listen" and not config.dry_run else None
        app = BridgeApp(config, progress_callback=progress_callback)
        if args.command == "check":
            _print_json(app.check())
            return 0
        if args.command == "handle-event":
            payload = json.loads(Path(args.event).read_text(encoding="utf-8"))
            _print_json(app.handle_payload(payload).to_dict())
            return 0
        if args.command == "run-signal":
            _print_json(app.run_signal(signal=args.signal, log_path=args.log_path, since=args.since).to_dict())
            return 0
        if args.command == "knowledge":
            service = KnowledgeService(config)
            if args.knowledge_command == "sync":
                _print_json(service.sync_all())
                return 0
            if args.knowledge_command == "sources":
                _print_json({"sources": service.list_sources()})
                return 0
            if args.knowledge_command == "search":
                query = " ".join(args.query)
                _print_json({"hits": [hit.to_dict() for hit in service.search(query)]})
                return 0
            if args.knowledge_command == "answer":
                question = " ".join(args.question)
                _print_json(service.answer(question).to_dict())
                return 0
            if args.knowledge_command == "add":
                _print_json(
                    {
                        "item": service.add_text(
                            source_id=args.source_id,
                            title=args.title,
                            content=args.content,
                            source_ref=args.source_ref,
                        )
                    }
                )
                return 0
            if args.knowledge_command == "add-source":
                _print_json(
                    {
                        "source": service.register_source(
                            source_id=args.source_id,
                            source_type=args.type,
                            title=args.title,
                            source_ref=args.source_ref,
                        )
                    }
                )
                return 0
        if args.command == "listen":
            if config.dry_run:
                logger.info("dry-run: listen would consume im.message.receive_v1 events with lark-cli")
                return 0
            if config.job_retention.purge_all_on_listen_start:
                app.purge_all_jobs()
            app.cleanup_expired_jobs()
            app.start_report_server()
            stop_cleanup = _start_cleanup_loop(app)
            try:
                for payload in app.lark_client.consume_payloads(status_callback=app.record_daemon_status):
                    _print_json(app.handle_payload(payload).to_dict())
            finally:
                app.stop_report_server()
                if stop_cleanup is not None:
                    stop_cleanup.set()
            return 0
    except Exception as exc:
        logger.error("fatal: %s", exc, exc_info=True)
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
            app.run_health_maintenance()

    threading.Thread(target=_worker, name="job-retention-cleanup", daemon=True).start()
    return stop_event
