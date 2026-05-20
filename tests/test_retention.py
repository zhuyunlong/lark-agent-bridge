from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lark_agent_bridge.app import BridgeApp
from lark_agent_bridge.models import LarkEvent, TaskResult
from lark_agent_bridge.models import BridgeConfig, JobRetentionOptions


class RetentionTests(unittest.TestCase):
    def test_cleanup_expired_jobs_preserves_historical_job_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                job_retention=JobRetentionOptions(max_age_hours=6),
            )
            app = BridgeApp(config)
            jobs_dir = config.data_dir / "jobs"
            expired = jobs_dir / "expired"
            fresh = jobs_dir / "fresh"
            (expired / "output").mkdir(parents=True)
            (fresh / "output").mkdir(parents=True)
            expired_file = expired / "output" / "old.md"
            fresh_file = fresh / "output" / "new.md"
            expired_file.write_text("old", encoding="utf-8")
            fresh_file.write_text("new", encoding="utf-8")

            now = datetime.now(timezone.utc)
            expired_ts = (now - timedelta(hours=7)).timestamp()
            fresh_ts = (now - timedelta(hours=1)).timestamp()
            os.utime(expired_file, (expired_ts, expired_ts))
            os.utime(fresh_file, (fresh_ts, fresh_ts))

            removed = app.cleanup_expired_jobs(now=now)
            self.assertEqual(removed, 0)
            self.assertTrue(expired.exists())
            self.assertTrue(fresh.exists())

    def test_cleanup_expired_jobs_preserves_reports_activity_and_conversations(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                job_retention=JobRetentionOptions(max_age_hours=6),
            )
            app = BridgeApp(config)
            now = datetime.now(timezone.utc)
            event = LarkEvent(
                event_id="evt_1",
                message_id="om_1",
                chat_id="oc_1",
                chat_type="group",
                sender_id="ou_1",
                message_type="text",
                content="@bot 分析 bug",
            )
            app.activity_store.record_event(event)
            app.activity_store.record_result(
                event,
                TaskResult(
                    success=True,
                    message="分析完成",
                    job_id="job_1",
                    details={"mode": "bug_analysis", "published_report_url": "http://127.0.0.1:8765/reports/job_1/"},
                ),
            )
            app.conversation_store.remember(
                root_message_id="om_1",
                chat_id="oc_1",
                mode="bug_analysis",
                request_text="分析 bug",
                summary_text="上一轮结论",
                report_url="http://127.0.0.1:8765/reports/job_1/",
                report_excerpt="报告摘录",
            )
            report_dir = config.data_dir / "published_reports" / "job_1"
            report_dir.mkdir(parents=True)
            report_file = report_dir / "index.html"
            report_file.write_text("report", encoding="utf-8")
            old_ts = (now - timedelta(hours=7)).timestamp()
            os.utime(report_file, (old_ts, old_ts))

            removed = app.cleanup_expired_jobs(now=now)

            self.assertEqual(removed, 0)
            self.assertTrue(report_file.exists())
            self.assertIsNotNone(app.activity_store.get_session("om_1"))
            self.assertIsNotNone(app.conversation_store.lookup("om_1"))

    def test_purge_all_jobs_removes_entire_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp))
            app = BridgeApp(config)
            jobs_dir = config.data_dir / "jobs"
            (jobs_dir / "job_a" / "output").mkdir(parents=True)
            (jobs_dir / "job_b" / "output").mkdir(parents=True)

            removed = app.purge_all_jobs()
            self.assertEqual(removed, 2)
            self.assertEqual(list(jobs_dir.glob("*")), [])

    def test_purge_all_jobs_preserves_bug_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp))
            app = BridgeApp(config)
            jobs_dir = config.data_dir / "jobs"
            cache_dir = config.data_dir / "bug_cache" / "xpfailuremgmt_6986570719"
            (jobs_dir / "job_a" / "output").mkdir(parents=True)
            (cache_dir / "logs").mkdir(parents=True)
            (cache_dir / "logs" / "main.log").write_text("cached", encoding="utf-8")

            removed = app.purge_all_jobs()

            self.assertEqual(removed, 1)
            self.assertEqual(list(jobs_dir.glob("*")), [])
            self.assertTrue(cache_dir.exists())

    def test_purge_all_jobs_preserves_followup_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp))
            app = BridgeApp(config)
            jobs_dir = config.data_dir / "jobs"
            (jobs_dir / "job_a" / "output").mkdir(parents=True)
            event = LarkEvent(
                event_id="evt_1",
                message_id="om_original_request",
                chat_id="oc_1",
                chat_type="group",
                sender_id="ou_1",
                message_type="text",
                content="@bot 分析 bug",
            )
            app.conversation_store.remember(
                root_message_id="om_original_request",
                chat_id="oc_1",
                mode="bug_analysis",
                request_text="分析 bug",
                summary_text="上一轮结论",
                report_url="http://127.0.0.1:8765/reports/job_1/",
                report_excerpt="报告摘录",
            )
            app.activity_store.record_event(event)
            app.activity_store.record_result(
                event,
                TaskResult(success=True, message="分析完成", details={"mode": "bug_analysis"}),
            )

            app.purge_all_jobs()

            self.assertIsNotNone(app.conversation_store.lookup("om_original_request"))
            self.assertIsNotNone(app.activity_store.get_session("om_original_request"))

    def test_purge_all_jobs_tolerates_permission_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp))
            app = BridgeApp(config)
            jobs_dir = config.data_dir / "jobs"
            (jobs_dir / "job_a" / "output").mkdir(parents=True)

            with mock.patch("shutil.rmtree", side_effect=PermissionError("denied")):
                removed = app.purge_all_jobs()

            self.assertEqual(removed, 0)

    def test_cleanup_expired_jobs_removes_only_old_bug_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                job_retention=JobRetentionOptions(max_age_hours=6, bug_cache_max_age_hours=24),
            )
            app = BridgeApp(config)
            cache_root = config.data_dir / "bug_cache"
            expired = cache_root / "xpfailuremgmt_6986570719"
            fresh = cache_root / "xpfailuremgmt_6987292722"
            (expired / "logs").mkdir(parents=True)
            (fresh / "logs").mkdir(parents=True)
            expired_file = expired / "logs" / "old.log"
            fresh_file = fresh / "logs" / "new.log"
            expired_file.write_text("old", encoding="utf-8")
            fresh_file.write_text("new", encoding="utf-8")

            now = datetime.now(timezone.utc)
            expired_ts = (now - timedelta(hours=25)).timestamp()
            fresh_ts = (now - timedelta(hours=2)).timestamp()
            os.utime(expired_file, (expired_ts, expired_ts))
            os.utime(fresh_file, (fresh_ts, fresh_ts))

            removed = app.cleanup_expired_jobs(now=now)

            self.assertEqual(removed, 1)
            self.assertFalse(expired.exists())
            self.assertTrue(fresh.exists())


if __name__ == "__main__":
    unittest.main()
