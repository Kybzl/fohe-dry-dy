"""Video download abstraction.

The pipeline only ever talks to ``VideoDownloader``.  ``HttpDownloader`` is
the real, retrying httpx implementation; ``MockDownloader`` writes placeholder
files (plus a ``.meta.json`` sidecar) so mock mode never touches the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from media.placeholder import write_placeholder_mp4
from media.ffmpeg import DEFAULT_REMOTE_USER_AGENT, validate_remote_media_url

LOGGER = logging.getLogger(__name__)


class DownloadError(RuntimeError):
    """Raised when a source video cannot be fetched."""


class MediaUrlExpiredError(DownloadError):
    """The media URL was rejected by the CDN (signed URLs expire).

    The caller may refresh the URL once and retry; it must not loop.
    """


def local_path_from_url(url: str) -> Path:
    """Convert a local path or ``file://`` URL into a ``Path``.

    ``file:///D:/clips/apple.mp4`` and ``D:/clips/apple.mp4`` both work; the
    conversion uses ``urllib`` so no separator is ever written by hand.
    """

    text = str(url).strip()
    if not text:
        raise DownloadError("empty local file reference")
    if text.lower().startswith("file:"):
        parsed = urlparse(text)
        return Path(url2pathname(unquote(parsed.path)))

    parsed = urlparse(text)
    # A single letter "scheme" is a Windows drive letter (``E:\videos\x.mp4``),
    # not a URL scheme; anything longer (http, https, ...) is a real URL.
    if parsed.scheme and len(parsed.scheme) > 1:
        raise DownloadError(f"not a local file reference: {url}")
    return Path(text)


class VideoDownloader(ABC):
    """Download a source video into the local cache."""

    @abstractmethod
    async def download(
        self,
        url: str,
        dest: Path,
        *,
        metadata: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Path:
        """Store ``url`` at ``dest`` and return the written path.

        ``metadata`` carries hints already known from the platform (duration,
        resolution, fps).  Implementations may ignore it.
        """

    async def aclose(self) -> None:
        """Release transport resources.  Optional."""


class HttpDownloader(VideoDownloader):
    """Streaming httpx downloader with bounded retries and a hard timeout."""

    def __init__(
        self,
        *,
        timeout: float = 120.0,
        max_retries: int = 3,
        headers: dict[str, str] | None = None,
        user_agent: str = DEFAULT_REMOTE_USER_AGENT,
        part_suffix: str = ".part",
    ) -> None:
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.headers = {"User-Agent": user_agent, **(headers or {})}
        self.part_suffix = part_suffix
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self.timeout, headers=self.headers)
        return self._client

    async def download(
        self,
        url: str,
        dest: Path,
        *,
        metadata: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Path:
        remote = validate_remote_media_url(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + self.part_suffix)
        part.unlink(missing_ok=True)
        client = self._get_client()
        attempt = 0
        while True:
            attempt += 1
            try:
                async with client.stream(
                    "GET", remote, headers={**self.headers, **(headers or {})}
                ) as response:
                    if response.status_code in (401, 403, 410):
                        raise MediaUrlExpiredError(
                            f"media url rejected with HTTP {response.status_code}"
                        )
                    response.raise_for_status()
                    with part.open("wb") as handle:
                        async for chunk in response.aiter_bytes(chunk_size=1 << 16):
                            handle.write(chunk)
                if not part.exists() or part.stat().st_size == 0:
                    raise DownloadError("downloaded media is empty")
                os.replace(part, dest)
                LOGGER.debug("downloaded %s -> %s (%s bytes)", remote, dest, dest.stat().st_size)
                return dest
            except asyncio.CancelledError:
                part.unlink(missing_ok=True)
                raise
            except MediaUrlExpiredError:
                # Permanently unusable without a fresh URL: no retry here.
                part.unlink(missing_ok=True)
                raise
            except DownloadError:
                part.unlink(missing_ok=True)
                raise
            except Exception as exc:
                if attempt >= self.max_retries:
                    part.unlink(missing_ok=True)
                    raise DownloadError(
                        f"failed to download media (attempt {attempt}): {exc}"
                    ) from exc
                delay = float(attempt)
                LOGGER.warning(
                    "download retry %s/%s in %.1fs: %s", attempt, self.max_retries, delay, exc
                )
                await asyncio.sleep(delay)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class MockDownloader(VideoDownloader):
    """Writes a placeholder file and a metadata sidecar; no network access."""

    def __init__(self, *, fail_on: tuple[str, ...] = ()) -> None:
        #: substrings of the URL that should raise ``DownloadError`` (failure tests)
        self.fail_on = fail_on
        self.downloaded: list[Path] = []

    async def download(
        self,
        url: str,
        dest: Path,
        *,
        metadata: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Path:
        if any(token in url for token in self.fail_on):
            raise DownloadError(f"mock downloader refuses {url}")

        payload = dict(metadata or {})
        write_placeholder_mp4(dest, signature=url.encode("utf-8"))
        sidecar = dest.with_suffix(dest.suffix + ".meta.json")
        sidecar.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.downloaded.append(dest)
        LOGGER.debug("mock download %s -> %s", url, dest)
        return dest


class LocalFileDownloader(VideoDownloader):
    """Copies a local test video into the cache directory.

    The user's original file is never opened for writing, moved or deleted: the
    orchestrator only ever sees (and cleans up) the copy inside ``cache/``.
    """

    def __init__(self, *, copy_file: bool = True) -> None:
        self.copy_file = copy_file
        self.downloaded: list[Path] = []
        self.sources: dict[str, Path] = {}

    async def download(
        self,
        url: str,
        dest: Path,
        *,
        metadata: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Path:
        source = local_path_from_url(url)
        if not source.exists():
            raise DownloadError(f"local source video not found: {source}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.copy_file:
            try:
                await self._copy_cancellable(source, dest)
            except asyncio.CancelledError:
                # a half copied file must never be left behind (Ctrl+C safe)
                dest.unlink(missing_ok=True)
                raise
        elif source.resolve() != dest.resolve():
            raise DownloadError("copy_file=False requires the source to already be in cache")
        sidecar = dest.with_suffix(dest.suffix + ".meta.json")
        sidecar.write_text(json.dumps(metadata or {}, ensure_ascii=False), encoding="utf-8")
        self.downloaded.append(dest)
        self.sources[str(dest)] = source
        LOGGER.debug("local file staged %s -> %s", source, dest)
        return dest

    @staticmethod
    async def _copy_cancellable(source: Path, dest: Path, chunk_size: int = 1 << 20) -> None:
        """Copy in chunks so Ctrl+C is honoured promptly on large files."""

        with source.open("rb") as reader, dest.open("wb") as writer:
            while True:
                chunk = await asyncio.to_thread(reader.read, chunk_size)
                if not chunk:
                    break
                writer.write(chunk)
                await asyncio.sleep(0)  # let cancellation be delivered
