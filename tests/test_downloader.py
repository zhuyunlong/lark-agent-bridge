from pathlib import Path
import tempfile
import unittest

from lark_agent_bridge.downloader import DownloadError, LogDownloader, safe_filename_from_url
from lark_agent_bridge.lark_client import LarkClient
from lark_agent_bridge.models import BridgeConfig, DownloadResource, create_job_context


class FakeLarkClient:
    def __init__(self):
        self.calls = []

    def download_resource(self, **kwargs):
        self.calls.append(kwargs)
        output = Path(kwargs["output"])
        output.write_text("ok", encoding="utf-8")
        return __import__("lark_agent_bridge.lark_client", fromlist=["CommandResult"]).CommandResult(
            command=["download"],
            returncode=0,
        )


class DownloaderTests(unittest.TestCase):
    def test_safe_filename_from_url(self):
        self.assertEqual(safe_filename_from_url("https://example.com/a/b/log file.zip?x=1"), "log_file.zip")

    def test_url_dry_run_returns_planned_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp))
            context = create_job_context(config.data_dir, job_id="job1")
            downloader = LogDownloader(config, LarkClient(config))

            result = downloader.download(
                DownloadResource(kind="url", value="https://example.com/log.zip"),
                context=context,
                message_id="om_1",
            )

        self.assertTrue(result.dry_run)
        self.assertEqual(result.path.name, "log.zip")

    def test_rejects_unsupported_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp))
            context = create_job_context(config.data_dir, job_id="job1")
            downloader = LogDownloader(config, LarkClient(config))

            with self.assertRaises(DownloadError):
                downloader.download(DownloadResource(kind="ftp", value="ftp://example.com/a"), context=context, message_id="")

    def test_file_resource_uses_source_message_id_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp))
            context = create_job_context(config.data_dir, job_id="job1")
            fake_lark = FakeLarkClient()
            downloader = LogDownloader(config, fake_lark)

            result = downloader.download(
                DownloadResource(kind="file", value="file_abc123", source_message_id="om_file_msg"),
                context=context,
                message_id="om_followup_msg",
            )

        self.assertEqual(fake_lark.calls[0]["message_id"], "om_file_msg")
        self.assertEqual(result.path.name, "file_abc123")


if __name__ == "__main__":
    unittest.main()
