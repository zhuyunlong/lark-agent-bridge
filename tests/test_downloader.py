from pathlib import Path
import tempfile
import threading
import time
import unittest
import unittest.mock
import zipfile

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

    def download_drive_folder(self, **kwargs):
        self.calls.append(kwargs)
        output = Path(kwargs["output_dir"])
        output.mkdir(parents=True, exist_ok=True)
        (output / "log.txt").write_text("ok", encoding="utf-8")
        return __import__("lark_agent_bridge.lark_client", fromlist=["CommandResult"]).CommandResult(
            command=["pull"],
            returncode=0,
        )


class DelayedFileLarkClient:
    def __init__(self, delay_seconds: float = 0.2):
        self.delay_seconds = delay_seconds
        self.thread = None
        self.calls = []
        self.final_existed_while_writing = False

    def download_resource(self, **kwargs):
        self.calls.append(kwargs)
        output = Path(kwargs["output"])
        final_output = output.with_name(output.name.removesuffix(".part"))

        def write_later():
            time.sleep(self.delay_seconds)
            self.final_existed_while_writing = final_output.exists()
            output.write_text("ok", encoding="utf-8")

        self.thread = threading.Thread(target=write_later)
        self.thread.start()
        return __import__("lark_agent_bridge.lark_client", fromlist=["CommandResult"]).CommandResult(
            command=["download"],
            returncode=0,
        )


class SlowlyCompletedZipLarkClient:
    def __init__(self, pause_seconds: float = 0.2):
        self.pause_seconds = pause_seconds
        self.thread = None
        self.calls = []
        self.final_existed_while_writing = False

    def download_resource(self, **kwargs):
        self.calls.append(kwargs)
        output = Path(kwargs["output"])
        final_output = output.with_name(output.name.removesuffix(".part"))

        def write_later():
            self.final_existed_while_writing = final_output.exists()
            output.write_bytes(b"PK\x03\x04partial")
            time.sleep(self.pause_seconds)
            with zipfile.ZipFile(output, "w") as zf:
                zf.writestr("crash.txt", "#00 pc 0000000000f385e4 /system/app/xp_envirodrive/lib/arm64/libunity.so\n")

        self.thread = threading.Thread(target=write_later)
        self.thread.start()
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

    def test_file_resource_waits_until_download_output_exists(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        config = BridgeConfig(dry_run=False, data_dir=Path(tmpdir.name))
        context = create_job_context(config.data_dir, job_id="job1")
        fake_lark = DelayedFileLarkClient()
        self.addCleanup(lambda: fake_lark.thread and fake_lark.thread.join(timeout=1))
        downloader = LogDownloader(config, fake_lark)

        result = downloader.download(
            DownloadResource(kind="file", value="file_abc123", source_message_id="om_file_msg"),
            context=context,
            message_id="om_followup_msg",
        )

        self.assertTrue(result.path.exists())
        self.assertEqual(result.path.read_text(encoding="utf-8"), "ok")
        self.assertFalse(fake_lark.final_existed_while_writing)
        self.assertTrue(str(fake_lark.calls[0]["output"]).endswith(".part"))

    def test_file_resource_waits_until_zip_output_is_complete(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        config = BridgeConfig(dry_run=False, data_dir=Path(tmpdir.name))
        context = create_job_context(config.data_dir, job_id="job1")
        fake_lark = SlowlyCompletedZipLarkClient()
        self.addCleanup(lambda: fake_lark.thread and fake_lark.thread.join(timeout=1))
        downloader = LogDownloader(config, fake_lark)

        result = downloader.download(
            DownloadResource(kind="file", value="crash.zip", source_message_id="om_file_msg"),
            context=context,
            message_id="om_followup_msg",
        )

        self.assertTrue(zipfile.is_zipfile(result.path))
        self.assertFalse(fake_lark.final_existed_while_writing)
        self.assertTrue(str(fake_lark.calls[0]["output"]).endswith(".part"))

    def test_folder_resource_pulls_drive_folder_to_input_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp))
            context = create_job_context(config.data_dir, job_id="job1")
            fake_lark = FakeLarkClient()
            downloader = LogDownloader(config, fake_lark)

            result = downloader.download(
                DownloadResource(kind="folder", value="fldcnlog123", source_message_id="om_folder_msg"),
                context=context,
                message_id="om_followup_msg",
            )

            self.assertEqual(fake_lark.calls[0]["folder_token"], "fldcnlog123")
            self.assertEqual(result.path.name, "fldcnlog123")
            self.assertTrue((result.path / "log.txt").exists())


if __name__ == "__main__":
    unittest.main()


class DownloaderPrivateUrlTests(unittest.TestCase):
    """Test allow_private_urls configuration for SSRF protection."""

    def test_private_ip_allowed_by_default(self):
        """Default config allows private IPs (core use case: internal file servers)."""
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp))
            self.assertTrue(config.download.allow_private_urls)

    def test_private_ip_blocked_when_disabled(self):
        from lark_agent_bridge.downloader import _is_private_ip
        from lark_agent_bridge.models import DownloadConfig
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                download=DownloadConfig(allow_private_urls=False),
            )
            context = create_job_context(config.data_dir, job_id="job-priv")
            downloader = LogDownloader(config, LarkClient(config))
            # Mock _is_private_ip to return True
            with unittest.mock.patch("lark_agent_bridge.downloader._is_private_ip", return_value=True):
                with self.assertRaises(DownloadError) as ctx:
                    downloader.download(
                        DownloadResource(kind="url", value="http://192.168.1.100/logs/app.log"),
                        context=context,
                        message_id="om_test",
                    )
                self.assertIn("private", str(ctx.exception).lower())

    def test_private_ip_not_blocked_when_allowed(self):
        """When allow_private_urls=True (default), private IPs should not be blocked."""
        from lark_agent_bridge.models import DownloadConfig
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                dry_run=True,
                data_dir=Path(tmp),
                download=DownloadConfig(allow_private_urls=True),
            )
            context = create_job_context(config.data_dir, job_id="job-allow")
            downloader = LogDownloader(config, LarkClient(config))
            # Even though IP is private, allow_private_urls=True means no block
            with unittest.mock.patch("lark_agent_bridge.downloader._is_private_ip", return_value=True):
                result = downloader.download(
                    DownloadResource(kind="url", value="http://192.168.1.100/logs/app.log"),
                    context=context,
                    message_id="om_test",
                )
            self.assertTrue(result.dry_run)
