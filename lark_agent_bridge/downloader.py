"""Download URL and Feishu message resources into a job input directory."""

from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import unquote, urlparse
import urllib.request

from .lark_client import LarkClient
from .models import BridgeConfig, DownloadResource, DownloadedResource, JobContext


class DownloadError(RuntimeError):
    pass


class LogDownloader:
    def __init__(self, config: BridgeConfig, lark_client: LarkClient) -> None:
        self.config = config
        self.lark_client = lark_client

    def download_all(
        self,
        resources: list[DownloadResource],
        *,
        context: JobContext,
        message_id: str,
    ) -> list[DownloadedResource]:
        return [self.download(resource, context=context, message_id=message_id) for resource in resources]

    def download(self, resource: DownloadResource, *, context: JobContext, message_id: str) -> DownloadedResource:
        if resource.kind == "url":
            return self._download_url(resource, context)
        if resource.kind in {"file", "image"}:
            return self._download_lark_resource(resource, context, message_id)
        if resource.kind == "local":
            path = Path(resource.value).expanduser()
            if self.config.dry_run:
                return DownloadedResource(resource=resource, path=path, dry_run=True)
            if not path.exists():
                raise DownloadError(f"Local log path does not exist: {path}")
            return DownloadedResource(resource=resource, path=path)
        raise DownloadError(f"Unsupported resource kind: {resource.kind}")

    def _download_url(self, resource: DownloadResource, context: JobContext) -> DownloadedResource:
        parsed = urlparse(resource.value)
        if parsed.scheme not in {"http", "https"}:
            raise DownloadError(f"Unsupported URL scheme: {parsed.scheme}")
        target = context.input_dir / safe_filename_from_url(resource.value)
        if self.config.dry_run:
            return DownloadedResource(resource=resource, path=target, dry_run=True)

        request = urllib.request.Request(resource.value, headers={"User-Agent": "lark-agent-bridge/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=self.config.download.timeout_seconds) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > self.config.download.max_bytes:
                    raise DownloadError("Download exceeds configured max_bytes")
                total = 0
                with target.open("wb") as fh:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > self.config.download.max_bytes:
                            raise DownloadError("Download exceeds configured max_bytes")
                        fh.write(chunk)
        except DownloadError:
            if target.exists():
                target.unlink()
            raise
        except OSError as exc:
            raise DownloadError(str(exc)) from exc
        return DownloadedResource(resource=resource, path=target)

    def _download_lark_resource(
        self,
        resource: DownloadResource,
        context: JobContext,
        message_id: str,
    ) -> DownloadedResource:
        if not message_id:
            raise DownloadError("message_id is required for Feishu resource downloads")
        target = context.input_dir / safe_filename(resource.value)
        result = self.lark_client.download_resource(
            message_id=message_id,
            file_key=resource.value,
            resource_type=resource.resource_type,
            output=target,
        )
        if result.returncode != 0:
            raise DownloadError(result.stderr or "lark-cli resource download failed")
        return DownloadedResource(resource=resource, path=target, dry_run=result.dry_run, command=result.command)


def safe_filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    name = unquote(Path(parsed.path).name) or "download"
    return safe_filename(name)


def safe_filename(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return safe or "download"

