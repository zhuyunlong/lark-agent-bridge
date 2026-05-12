from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lark_agent_bridge.app import BridgeApp
from lark_agent_bridge.models import BridgeConfig, JobRetentionOptions


class RetentionTests(unittest.TestCase):
    def test_cleanup_expired_jobs_removes_only_old_directories(self):
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
            self.assertEqual(removed, 1)
            self.assertFalse(expired.exists())
            self.assertTrue(fresh.exists())

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

    def test_purge_all_jobs_tolerates_permission_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp))
            app = BridgeApp(config)
            jobs_dir = config.data_dir / "jobs"
            (jobs_dir / "job_a" / "output").mkdir(parents=True)

            with mock.patch("shutil.rmtree", side_effect=PermissionError("denied")):
                removed = app.purge_all_jobs()

            self.assertEqual(removed, 0)


if __name__ == "__main__":
    unittest.main()
