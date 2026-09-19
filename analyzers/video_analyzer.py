"""Deep analysis: locate the time ranges that actually show the material."""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Sequence

from ai.base import AuditContext, SegmentDetectionRequest
from ai.gateway import AIGateway
from core.models import DetectedSegment
from media.frame_sampler import FrameSampler

LOGGER = logging.getLogger(__name__)


def _nearest_boundary(
    boundaries: Sequence[float], target: float, tolerance: float
) -> float | None:
    candidates = [b for b in boundaries if abs(b - target) <= tolerance]
    return min(candidates, key=lambda value: abs(value - target)) if candidates else None


def split_long_segment(
    segment: DetectedSegment,
    *,
    min_duration: float,
    max_duration: float,
    boundaries: Sequence[float] = (),
    boundary_tolerance: float = 1.5,
) -> list[DetectedSegment]:
    """Split an over-long range into chunks that respect shot boundaries.

    The cut points prefer a nearby detected scene boundary over a blind
    ``start + max_duration`` division, so a chunk never straddles a shot change
    when a boundary is available close to the ideal position.
    """

    start, end = segment.start, segment.end
    if end - start <= max_duration + 0.01:
        return [segment]

    chunks: list[list[float]] = []
    cursor = start
    while end - cursor > max_duration + 0.01:
        ideal = cursor + max_duration
        cut = _nearest_boundary(boundaries, ideal, boundary_tolerance) or ideal
        if cut - cursor < min_duration or cut >= end:
            cut = min(cursor + max_duration, end)
        if end - cut < min_duration:
            break
        chunks.append([cursor, cut])
        cursor = cut

    if end - cursor >= min_duration:
        chunks.append([cursor, end])
    elif chunks:
        # The tail is too short to stand alone: merge it into the last chunk.
        chunks[-1][1] = end

    return [
        segment.model_copy(update={"start": round(chunk[0], 3), "end": round(chunk[1], 3)})
        for chunk in chunks
        if chunk[1] - chunk[0] >= min_duration
    ]


def normalize_segments(
    segments: Sequence[DetectedSegment],
    duration: float,
    *,
    min_duration: float,
    max_duration: float,
    max_segments: int = 12,
    boundaries: Sequence[float] = (),
    boundary_tolerance: float = 1.5,
    split_too_long: bool = True,
    overlap_tolerance: float = 0.5,
) -> list[DetectedSegment]:
    """Turn raw AI ranges into ranges that respect the duration contract.

    * invalid / NaN-ish values are dropped or clamped into ``[0, duration]``
      (``end <= start``, ``end > duration``, ``start < 0``)
    * unusable ranges are dropped
    * too short ranges are dropped
    * too long ranges are split (preferring scene boundaries)
    * overlapping ranges are removed (the earlier one wins)
    """

    if duration <= 0 or not segments:
        return []

    cleaned: list[DetectedSegment] = []
    for segment in segments:
        if not segment.usable:
            continue
        start = float(segment.start)
        end = float(segment.end)
        if math.isnan(start) or math.isnan(end):
            LOGGER.debug("dropping range with NaN timestamps")
            continue
        start = max(0.0, min(start, duration))
        end = max(0.0, min(end, duration))
        if end - start < min_duration:
            LOGGER.debug("dropping too short range %.2f-%.2f", start, end)
            continue
        candidate = segment.model_copy(update={"start": start, "end": end})
        if end - start > max_duration + 0.01 and split_too_long:
            cleaned.extend(
                split_long_segment(
                    candidate,
                    min_duration=min_duration,
                    max_duration=max_duration,
                    boundaries=boundaries,
                    boundary_tolerance=boundary_tolerance,
                )
            )
        else:
            cleaned.append(candidate)

    cleaned.sort(key=lambda item: item.start)
    selected: list[DetectedSegment] = []
    for segment in cleaned:
        if selected and segment.start < selected[-1].end - overlap_tolerance:
            LOGGER.debug("dropping overlapping range %.2f-%.2f", segment.start, segment.end)
            continue
        selected.append(segment)
        if len(selected) >= max_segments:
            break
    return selected


class VideoAnalyzer:
    """Samples frames and asks the vision provider for usable time ranges."""

    def __init__(
        self,
        gateway: AIGateway,
        sampler: FrameSampler,
        *,
        frame_count: int = 12,
        max_width: int | None = None,
        min_segment_duration: float = 3.0,
        max_segment_duration: float = 15.0,
        max_segments: int = 6,
        boundary_tolerance: float = 1.5,
    ) -> None:
        self.gateway = gateway
        self.sampler = sampler
        self.frame_count = frame_count
        self.max_width = max_width
        self.min_segment_duration = min_segment_duration
        self.max_segment_duration = max_segment_duration
        self.max_segments = max_segments
        self.boundary_tolerance = boundary_tolerance

    async def analyze(
        self,
        *,
        video_path: Path,
        duration: float,
        material: str,
        query: str,
        platform: str,
        platform_video_id: str,
        title: str = "",
        frames_dir: Path | None = None,
        context: dict | None = None,
        audit: AuditContext | None = None,
        boundaries: Sequence[float] = (),
        target_process_stage: str = "",
    ) -> list[DetectedSegment]:
        """Return the usable ranges of one video (empty list on failure)."""

        frames = []
        if frames_dir is not None:
            frames = await self.sampler.sample(
                video_path,
                duration,
                self.frame_count,
                f"{platform_video_id}_analysis",
                frames_dir,
                max_width=self.max_width,
            )
        if not frames:
            LOGGER.warning("no frames sampled for %s; skipping segment detection", platform_video_id)
            return []

        request = SegmentDetectionRequest(
            material=material,
            query=query,
            platform=platform,
            platform_video_id=platform_video_id,
            title=title,
            duration=duration,
            video_path=str(video_path),
            frames=frames,
            context=dict(context or {}),
            audit=audit or AuditContext(),
            min_segment_duration=self.min_segment_duration,
            max_segment_duration=self.max_segment_duration,
            target_process_stage=target_process_stage,
        )
        result = await self.gateway.detect_segments(request)
        if result is None:
            LOGGER.warning("segment detection failed for %s", platform_video_id)
            return []

        normalized = normalize_segments(
            result.segments,
            duration,
            min_duration=self.min_segment_duration,
            max_duration=self.max_segment_duration,
            max_segments=self.max_segments,
            boundaries=boundaries,
            boundary_tolerance=self.boundary_tolerance,
        )
        LOGGER.debug(
            "%s: %s raw ranges -> %s usable ranges",
            platform_video_id,
            len(result.segments),
            len(normalized),
        )
        return normalized
