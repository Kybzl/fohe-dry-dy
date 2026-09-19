"""Configurable representative frame sampling.

Every frame keeps its timestamp so the AI can map pictures back to time
ranges.  The number of frames sent to a vision model is always bounded
(``analysis.preview_max_frames`` / ``analysis.analysis_max_frames``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

from core.models import PreviewFrame, PreviewSource
from media.ffmpeg import MediaToolkit

LOGGER = logging.getLogger(__name__)

#: supported sampling strategies
SAMPLING_STRATEGIES = ("uniform", "uniform_edges", "dense_start")


class FrameSampler:
    """Uniform (or weighted) frame extraction on top of a ``MediaToolkit``."""

    def __init__(
        self,
        toolkit: MediaToolkit,
        *,
        max_frames: int = 12,
        max_width: int | None = None,
        strategy: str = "uniform",
    ) -> None:
        self.toolkit = toolkit
        self.max_frames = max(1, max_frames)
        self.max_width = max_width
        self.strategy = strategy if strategy in SAMPLING_STRATEGIES else "uniform"
        if strategy not in SAMPLING_STRATEGIES:
            LOGGER.warning("unknown sampling strategy %r; using uniform", strategy)

    # -- timestamp planning (pure, unit tested) ----------------------------
    @staticmethod
    def timestamps(
        duration: float,
        count: int,
        *,
        start: float = 0.0,
        end: float | None = None,
        strategy: str = "uniform",
    ) -> list[float]:
        """Evenly spaced timestamps inside ``[start, end]``.

        Uses bin centres, so no frame is taken exactly at 0 or at the very end
        (which is often a black frame or a title card).

        * ``uniform``       - evenly spaced bin centres (default)
        * ``uniform_edges`` - uniform plus explicit near-start / near-end frames
        * ``dense_start``   - half of the frames in the first 40% (hooks)
        """

        if duration <= 0 or count <= 0:
            return []
        window_start = max(0.0, start)
        window_end = min(duration, end if end is not None else duration)
        if window_end - window_start <= 0.05:
            return [round(window_start, 3)]
        span = window_end - window_start

        def centre(index: int, total: int) -> float:
            return window_start + span * (index + 0.5) / total

        if strategy == "uniform_edges" and count >= 4:
            head_count = max(1, count // 3)
            tail_count = max(1, count // 3)
            middle_count = max(1, count - head_count - tail_count)
            stamps = [
                window_start + span * (index + 0.5) / head_count * 0.15
                for index in range(head_count)
            ]
            stamps += [
                window_start + span * (0.35 + 0.3 * (index + 0.5) / middle_count)
                for index in range(middle_count)
            ]
            stamps += [
                window_end - span * 0.15 + span * 0.15 * (index + 0.5) / tail_count
                for index in range(tail_count)
            ]
        elif strategy == "dense_start" and count >= 3:
            head_count = max(1, count // 2)
            stamps = [window_start + span * 0.4 * (index + 0.5) / head_count for index in range(head_count)]
            stamps += [
                window_start + span * (0.4 + 0.6 * (index + 0.5) / (count - head_count))
                for index in range(count - head_count)
            ]
        else:
            stamps = [centre(index, count) for index in range(count)]

        ordered = sorted({round(min(max(stamp, window_start), window_end), 3) for stamp in stamps})
        return ordered

    # -- extraction --------------------------------------------------------
    async def sample(
        self,
        video: Path,
        duration: float,
        count: int,
        prefix: str,
        out_dir: Path,
        *,
        start: float = 0.0,
        end: float | None = None,
        max_width: int | None = None,
        strategy: str | None = None,
    ) -> list[PreviewFrame]:
        """Extract up to ``count`` frames and return them with timestamps."""

        effective_count = min(count, self.max_frames)
        stamps = self.timestamps(
            duration,
            effective_count,
            start=start,
            end=end,
            strategy=strategy or self.strategy,
        )
        if not stamps:
            return []
        try:
            paths = await self.toolkit.extract_frames(
                video,
                stamps,
                out_dir,
                prefix,
                size=max_width or self.max_width,
            )
        except Exception as exc:
            LOGGER.warning("frame extraction failed for %s: %s", video, exc)
            return []
        return [
            PreviewFrame(timestamp=stamp, image_path=path, source="sampled")
            for stamp, path in zip(stamps, paths)
        ]

    async def sample_remote(
        self,
        url: str,
        duration: float,
        count: int,
        prefix: str,
        out_dir: Path,
        *,
        max_width: int | None = None,
        strategy: str | None = None,
    ) -> list[PreviewFrame]:
        """Sample frames straight from a remote media URL.

        Only the bytes FFmpeg needs are read, so a rejected candidate never
        becomes a full local file.
        """

        effective_count = min(count, self.max_frames)
        stamps = self.timestamps(
            duration, effective_count, strategy=strategy or self.strategy
        )
        if not stamps:
            # No trustworthy duration: sample an early window the platform
            # usually starts with, instead of skipping the preview entirely.
            stamps = [round(0.5 + index * 1.5, 3) for index in range(min(4, effective_count))]
        try:
            paths = await self.toolkit.sample_remote_frames(
                url,
                stamps,
                out_dir,
                prefix,
                size=max_width or self.max_width,
            )
        except Exception as exc:
            LOGGER.warning("remote frame sampling failed for %s: %s", url[:80], exc)
            return []
        return [
            PreviewFrame(timestamp=stamp, image_path=path, source="remote")
            for stamp, path in zip(stamps, paths)
        ]

    @staticmethod
    def from_preview(preview: PreviewSource) -> list[PreviewFrame]:
        """Preview stills already provided by the platform adapter."""

        return list(preview.frames)

    @staticmethod
    def coverage_of(frames: Sequence[PreviewFrame]) -> tuple[float | None, float | None]:
        """First and last timestamp covered by a frame list."""

        if not frames:
            return None, None
        stamps = [frame.timestamp for frame in frames]
        return min(stamps), max(stamps)
