"""``LocalFileSource``: real local video files exposed through ``VideoSource``.

This adapter exists so the whole pipeline (probing, AI analysis, cutting,
tagging, persistence) can run on real footage before Douyin integration.
The orchestrator only ever talks to the ``VideoSource`` interface -- there is
no local-file special case anywhere in the core workflow.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Iterable, Sequence

from core.models import PreviewFrame, PreviewSource, VideoCandidate, VideoInfo
from media.ffmpeg import MediaInfo, MediaToolkit
from sources.base import SourceError, VideoSource

LOGGER = logging.getLogger(__name__)

DEFAULT_EXTENSIONS: tuple[str, ...] = (".mp4", ".mov", ".m4v")


class LocalFileSource(VideoSource):
    """Serves one or more local video files as a searchable source."""

    platform = "local"
    #: the candidate set is the file list itself: one search covers everything
    search_mode = "collection"

    def __init__(
        self,
        files: Iterable[Path | str] = (),
        *,
        extensions: Sequence[str] = DEFAULT_EXTENSIONS,
        recursive: bool = False,
        toolkit: MediaToolkit | None = None,
        preview_dir: Path | None = None,
        preview_frame_count: int = 8,
        preview_max_width: int | None = 640,
        sampling_strategy: str = "uniform",
        request_timeout: float = 20.0,
        max_retries: int = 3,
    ) -> None:
        super().__init__(request_timeout=request_timeout, max_retries=max_retries)
        self.extensions = tuple(
            (extension if extension.startswith(".") else f".{extension}").lower()
            for extension in extensions
        )
        self.recursive = recursive
        self.toolkit = toolkit
        self.preview_dir = Path(preview_dir) if preview_dir else None
        self.preview_frame_count = max(1, preview_frame_count)
        self.preview_max_width = preview_max_width
        self.sampling_strategy = sampling_strategy
        self._paths: dict[str, Path] = {}
        self._info: dict[str, MediaInfo] = {}
        self._probe_errors: dict[str, str] = {}
        for item in files:
            for path in self.expand(item):
                self.register(path)

    # -- file handling -----------------------------------------------------
    def expand(self, item: Path | str) -> list[Path]:
        """Expand a file or directory into the supported video files it holds."""

        path = Path(item).expanduser()
        if path.is_dir():
            pattern = "**/*" if self.recursive else "*"
            return sorted(
                candidate
                for candidate in path.glob(pattern)
                if candidate.is_file() and candidate.suffix.lower() in self.extensions
            )
        return [path]

    def register(self, path: Path) -> str:
        """Add one file and return its stable platform video id."""

        video_id = self._video_id(path)
        self._paths[video_id] = path
        return video_id

    @property
    def files(self) -> list[Path]:
        return list(self._paths.values())

    def _video_id(self, path: Path) -> str:
        digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]
        return f"local_{digest}"

    def path_for(self, video_id: str) -> Path:
        path = self._paths.get(video_id)
        if path is None:
            raise SourceError(f"unknown local video id: {video_id}")
        return path

    # -- VideoSource -------------------------------------------------------
    async def search(self, query: str, limit: int) -> list[VideoCandidate]:
        """Return one candidate per registered file.

        ``query`` only influences ranking/limiting (a local file has no
        platform search), which keeps the adapter contract intact.
        """

        candidates: list[VideoCandidate] = []
        for video_id, path in list(self._paths.items()):
            if len(candidates) >= max(0, limit):
                break
            info, error = await self._probe(video_id)
            metadata: dict = {
                "source_adapter": "local",
                "source": "local",
                "file_name": path.name,
                "file_path": str(path),
                "query": query,
            }
            if info is not None:
                metadata.update(
                    {
                        "width": info.width,
                        "height": info.height,
                        "fps": info.fps,
                        "has_audio": info.has_audio,
                        "codec": info.codec,
                    }
                )
            else:
                metadata.update({"unreadable": True, "probe_error": error})

            candidates.append(
                VideoCandidate(
                    platform=self.platform,
                    platform_video_id=video_id,
                    source_url=path.as_uri(),
                    title=path.stem,
                    author="local file",
                    duration=info.duration if info else None,
                    metadata=metadata,
                )
            )
        LOGGER.info("local source: %s candidate(s) for %r", len(candidates), query)
        return candidates

    async def get_video_info(self, video_id: str) -> VideoInfo:
        path = self.path_for(video_id)
        info, error = await self._probe(video_id)
        if info is None:
            raise SourceError(f"cannot read metadata of {path.name}: {error}")
        return VideoInfo(
            platform=self.platform,
            platform_video_id=video_id,
            source_url=path.as_uri(),
            title=path.stem,
            author="local file",
            description=str(path),
            duration=info.duration,
            width=info.width,
            height=info.height,
            fps=info.fps,
            metadata={
                "source": "local",
                "file_name": path.name,
                "codec": info.codec,
                "has_audio": info.has_audio,
            },
        )

    async def get_preview(self, video_id: str) -> PreviewSource:
        """Sample real frames from the local file for the AI pre-filter."""

        path = self.path_for(video_id)
        info, error = await self._probe(video_id)
        duration = info.duration if info else 0.0
        frames: list[PreviewFrame] = []
        if self.toolkit is None:
            LOGGER.warning("no media toolkit configured; cannot sample preview frames")
        elif duration > 0:
            from media.frame_sampler import FrameSampler

            sampler = FrameSampler(
                self.toolkit,
                max_frames=self.preview_frame_count,
                max_width=self.preview_max_width,
                strategy=self.sampling_strategy,
            )
            # ``preview_dir`` already is the preview folder; only fall back to
            # "<file dir>/previews" when none was configured.
            out_dir = self.preview_dir or (path.parent / "previews")
            frames = await sampler.sample(
                path,
                duration,
                self.preview_frame_count,
                f"{video_id}_preview",
                out_dir,
            )
        else:
            LOGGER.warning("cannot sample preview frames for %s (%s)", path.name, error)
        return PreviewSource(
            platform=self.platform,
            platform_video_id=video_id,
            duration=duration,
            frames=frames,
            video_path=path,
            metadata={"source": "local", "file_name": path.name},
        )

    async def get_download_url(self, video_id: str) -> str:
        """A ``file://`` URI; ``LocalFileDownloader`` stages it into the cache."""

        return self.path_for(video_id).as_uri()

    # -- helpers -----------------------------------------------------------
    async def _probe(self, video_id: str) -> tuple[MediaInfo | None, str | None]:
        if video_id in self._info:
            return self._info[video_id], None
        if video_id in self._probe_errors:
            return None, self._probe_errors[video_id]

        path = self.path_for(video_id)
        if not path.exists():
            self._probe_errors[video_id] = f"file not found: {path}"
            return None, self._probe_errors[video_id]
        if self.toolkit is None:
            # Without a toolkit we cannot know the duration; the orchestrator
            # will probe the staged copy instead.
            return None, "no media toolkit configured"
        try:
            info = await self.toolkit.probe(path)
        except Exception as exc:
            self._probe_errors[video_id] = f"{type(exc).__name__}: {exc}"
            LOGGER.warning("cannot probe local file %s: %s", path.name, exc)
            return None, self._probe_errors[video_id]
        if not info.has_video:
            self._probe_errors[video_id] = "no video stream found"
            return None, self._probe_errors[video_id]
        self._info[video_id] = info
        return info, None

    def describe(self) -> str:
        return f"local files={len(self._paths)}"
