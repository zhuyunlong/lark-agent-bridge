"""Download URL and Feishu message resources into a job input directory."""

from __future__ import annotations

import ipaddress
from pathlib import Path
import re
import shutil
import socket
import time
from urllib.parse import unquote, urlparse
import urllib.request
import zipfile

from .lark_client import LarkClient
from .log import get_logger
from .models import BridgeConfig, DownloadResource, DownloadedResource, JobContext

logger = get_logger("download")


class DownloadError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# SSRF protection: reject redirects and connections to private/reserved IPs
# ---------------------------------------------------------------------------

_SSRF_SAFE_SCHEMES = {"http", "https"}
_DOWNLOAD_OUTPUT_WAIT_SECONDS = 10.0
_DOWNLOAD_OUTPUT_POLL_SECONDS = 0.05
_DOWNLOAD_OUTPUT_STABLE_SECONDS = 0.5


def _is_private_ip(host: str) -> bool:
    """Return True if *host* resolves to a private, loopback, or reserved IP."""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for _family, _type, _proto, _canon, sockaddr in infos:
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_link_local:
            return True
    return False


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Block automatic redirects and validate redirect targets."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme not in _SSRF_SAFE_SCHEMES:
            raise DownloadError(f"Redirect to disallowed scheme: {parsed.scheme}")
        if _is_private_ip(parsed.hostname or ""):
            raise DownloadError(f"Redirect to private/reserved IP blocked: {parsed.hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


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
        if resource.kind == "folder":
            return self._download_drive_folder(resource, context)
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
        if parsed.scheme not in _SSRF_SAFE_SCHEMES:
            raise DownloadError(f"Unsupported URL scheme: {parsed.scheme}")
        if not self.config.download.allow_private_urls and _is_private_ip(parsed.hostname or ""):
            raise DownloadError(f"Download from private/reserved IP blocked: {parsed.hostname}")
        target = context.input_dir / safe_filename_from_url(resource.value)
        staging_target = self._staging_path(target)
        if self.config.dry_run:
            return DownloadedResource(resource=resource, path=target, dry_run=True)

        request = urllib.request.Request(resource.value, headers={"User-Agent": "lark-agent-bridge/0.1"})
        opener = urllib.request.build_opener(_NoRedirectHandler)
        self._remove_path(staging_target)
        try:
            with opener.open(request, timeout=self.config.download.timeout_seconds) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > self.config.download.max_bytes:
                    raise DownloadError("Download exceeds configured max_bytes")
                total = 0
                with staging_target.open("wb") as fh:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > self.config.download.max_bytes:
                            raise DownloadError("Download exceeds configured max_bytes")
                        fh.write(chunk)
            self._publish_download_output(staging_target, target)
        except DownloadError:
            self._remove_path(staging_target)
            raise
        except OSError as exc:
            self._remove_path(staging_target)
            raise DownloadError(str(exc)) from exc
        return DownloadedResource(resource=resource, path=target)

    def _download_lark_resource(
        self,
        resource: DownloadResource,
        context: JobContext,
        message_id: str,
    ) -> DownloadedResource:
        effective_message_id = resource.source_message_id.strip() or message_id
        if not effective_message_id:
            raise DownloadError("message_id is required for Feishu resource downloads")
        target = context.input_dir / safe_filename(resource.value)
        staging_target = self._staging_path(target)
        self._remove_path(staging_target)
        result = self.lark_client.download_resource(
            message_id=effective_message_id,
            file_key=resource.value,
            resource_type=resource.resource_type,
            output=staging_target,
        )
        if result.returncode != 0:
            raise DownloadError(result.stderr or "lark-cli resource download failed")
        try:
            self._wait_for_download_output(staging_target, validate_zip=target.suffix.casefold() == ".zip")
            self._publish_download_output(staging_target, target)
        except Exception:
            self._remove_path(staging_target)
            raise
        return DownloadedResource(resource=resource, path=target, dry_run=result.dry_run, command=result.command)

    def _download_drive_folder(self, resource: DownloadResource, context: JobContext) -> DownloadedResource:
        folder_token = resource.value.strip()
        if not folder_token:
            raise DownloadError("folder_token is required for Feishu Drive folder downloads")
        target = context.input_dir / safe_filename(folder_token)
        staging_target = self._staging_path(target)
        self._remove_path(staging_target)
        result = self.lark_client.download_drive_folder(folder_token=folder_token, output_dir=staging_target)
        if result.returncode != 0:
            raise DownloadError(result.stderr or "lark-cli Drive folder pull failed")
        try:
            self._wait_for_download_output(staging_target, expect_dir=True)
            self._publish_download_output(staging_target, target)
        except Exception:
            self._remove_path(staging_target)
            raise
        return DownloadedResource(resource=resource, path=target, dry_run=result.dry_run, command=result.command)

    def _wait_for_download_output(self, target: Path, *, expect_dir: bool = False, validate_zip: bool = False) -> None:
        deadline = time.monotonic() + _DOWNLOAD_OUTPUT_WAIT_SECONDS
        last_size: int | None = None
        stable_since: float | None = None
        while True:
            if expect_dir:
                if target.is_dir():
                    return
            elif target.is_file():
                try:
                    size = target.stat().st_size
                except OSError:
                    size = None
                if size:
                    if last_size == size:
                        stable_since = stable_since or time.monotonic()
                        if time.monotonic() - stable_since >= _DOWNLOAD_OUTPUT_STABLE_SECONDS:
                            if not validate_zip or zipfile.is_zipfile(target):
                                return
                    else:
                        stable_since = None
                        last_size = size
            if time.monotonic() >= deadline:
                break
            time.sleep(_DOWNLOAD_OUTPUT_POLL_SECONDS)
        raise DownloadError(f"Download output was not ready: {target}")

    def _staging_path(self, target: Path) -> Path:
        return target.with_name(f"{target.name}.part")

    def _publish_download_output(self, staging_target: Path, target: Path) -> None:
        self._remove_path(target)
        staging_target.rename(target)

    def _remove_path(self, path: Path) -> None:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            return
        if path.exists() or path.is_symlink():
            path.unlink(missing_ok=True)


def safe_filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    name = unquote(Path(parsed.path).name) or "download"
    return safe_filename(name)


def safe_filename(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return safe or "download"
