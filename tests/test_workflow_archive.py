"""Tests for Lark workflow archival planning."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from lark_agent_bridge.lark_client import LarkClient
from lark_agent_bridge.models import BridgeConfig, LarkEvent, TaskResult, WorkflowArchiveOptions
from lark_agent_bridge.workflow_archive import WorkflowArchiver


def event(**overrides):
    values = {
        "event_id": "evt_archive",
        "message_id": "om_archive",
        "chat_id": "oc_archive",
        "chat_type": "group",
        "sender_id": "ou_archive",
        "message_type": "text",
        "content": "@bot 分析 bug",
    }
    values.update(overrides)
    return LarkEvent(**values)


class WorkflowArchiveTests(unittest.TestCase):
    def test_disabled_archive_is_skipped(self):
        config = BridgeConfig(workflow_archive=WorkflowArchiveOptions(enabled=False))
        archiver = WorkflowArchiver(config, LarkClient(config))
        result = TaskResult(success=True, message="done", job_id="j1", details={"mode": "bug_analysis"})

        archive = archiver.archive(result, event=event(), request_text="分析 bug")

        self.assertTrue(archive["skipped"])
        self.assertEqual(archive["reason"], "disabled")

    def test_dry_run_plans_doc_drive_and_base_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "report.html"
            metadata = Path(tmp) / "metadata.json"
            html.write_text("<html>report</html>", encoding="utf-8")
            metadata.write_text("{}", encoding="utf-8")
            config = BridgeConfig(
                dry_run=True,
                data_dir=Path(tmp) / "data",
                workflow_archive=WorkflowArchiveOptions(
                    enabled=True,
                    base_token="base_token",
                    table_id="tbl_case",
                    drive_folder_token="fld_reports",
                    doc_parent_token="fld_docs",
                ),
            )
            archiver = WorkflowArchiver(config, LarkClient(config))
            result = TaskResult(
                success=True,
                message="根因：主线程等待日志下载完成",
                job_id="job_archive",
                html_report=html,
                json_report=metadata,
                duration_seconds=12.5,
                details={
                    "mode": "bug_analysis",
                    "provider": "codex",
                    "published_report_url": "http://bridge/reports/job_archive/",
                    "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/1",
                    "report_version": 2,
                },
            )

            archive = archiver.archive(result, event=event(), request_text="分析 bug")

        commands = [item["command"] for item in archive["commands"]]
        self.assertEqual(commands[0][:5], ["lark-cli", "docs", "+create", "--api-version", "v2"])
        self.assertIn("--parent-token", commands[0])
        self.assertEqual(commands[1][:4], ["lark-cli", "drive", "+upload", "--as"])
        self.assertIn(str(html.resolve()), commands[1])
        self.assertEqual(commands[-1][:4], ["lark-cli", "base", "+record-upsert", "--as"])
        self.assertIn("--base-token", commands[-1])
        self.assertEqual(archive["doc"]["planned"], True)
        self.assertEqual(len(archive["drive"]["uploads"]), 2)
        self.assertEqual(archive["base"]["planned"], True)


if __name__ == "__main__":
    unittest.main()
