"""Scene detection and shot boundary refinement.

The AI returns approximate timestamps; PySceneDetect gives the real shot
boundaries.  A boundary is only accepted when it is *close* to the AI
timestamp, so a clip never grows into unrelated content.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable, Sequence

from core.models import DetectedSegment, SegmentTiming

LOGGER = logging.getLogger(__name__)


class SceneBoundaryDetector(ABC):
    """Returns the shot boundary timestamps (seconds) of a video."""

    @abstractmethod
    def detect(self, video_path: Path) -> list[float]:
        """Detect boundaries; must return ``[]`` when detection is impossible."""


class StaticBoundaryDetector(SceneBoundaryDetector):
    """Fallback used when PySceneDetect is unavailable or disabled."""

    def detect(self, video_path: Path) -> list[float]:
        return []


class PySceneDetectBoundaryDetector(SceneBoundaryDetector):
    """Real detector; imports PySceneDetect lazily and degrades to no-op."""

    def __init__(self, *, threshold: float = 27.0) -> None:
        self.threshold = threshold

    def detect(self, video_path: Path) -> list[float]:
        try:
            from scenedetect import ContentDetector, detect
        except Exception as exc:  # pragma: no cover - optional dependency
            LOGGER.warning("PySceneDetect unavailable (%s); skipping refinement", exc)
            return []

        try:
            raw = detect(
                str(video_path), ContentDetector(threshold=self.threshold), show_progress=False
            )
            scene_list = self._normalise_detect_result(raw)
        except Exception as exc:
            LOGGER.warning("PySceneDetect failed for %s: %s", video_path, exc)
            return []

        boundaries: list[float] = []
        for item in scene_list:
            # PySceneDetect versions differ: 0.6 yields (start, end) pairs,
            # 0.7 yields a flat list of scene start timecodes.
            if isinstance(item, (tuple, list)):
                for timecode in item:
                    boundaries.append(float(timecode.get_seconds()))
            else:
                boundaries.append(float(item.get_seconds()))
        return sorted({round(value, 3) for value in boundaries})

    @staticmethod
    def _normalise_detect_result(raw: object) -> list:
        """Accept both PySceneDetect return shapes.

        * 0.6: ``(scene_list, frame_num, fps, duration)``
        * 0.7: the scene list itself (``[(start, end), ...]``)
        """

        if isinstance(raw, tuple) and len(raw) >= 1 and isinstance(raw[0], (list, tuple)):
            return list(raw[0])
        return list(raw) if isinstance(raw, (list, tuple)) else []


class SceneRefiner:
    """Snaps AI timestamps to nearby shot boundaries."""

    def __init__(
        self,
        detector: SceneBoundaryDetector | None = None,
        *,
        tolerance_seconds: float = 2.0,
        max_expansion_ratio: float = 0.35,
    ) -> None:
        self.detector = detector or StaticBoundaryDetector()
        self.tolerance_seconds = tolerance_seconds
        self.max_expansion_ratio = max_expansion_ratio

    async def boundaries_for(self, video_path: Path) -> list[float]:
        """Detect boundaries off the event loop (CPU bound)."""

        try:
            return await asyncio.to_thread(self.detector.detect, video_path)
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("scene detection failed for %s: %s", video_path, exc)
            return []

    def refine(
        self,
        segment: DetectedSegment,
        boundaries: Sequence[float],
        *,
        duration: float | None = None,
    ) -> SegmentTiming:
        """Return the refined timing of one segment (never expands too far)."""

        tolerance = min(self.tolerance_seconds, self.max_expansion_ratio * segment.duration)
        start = segment.start
        end = segment.end
        refined = False

        if boundaries:
            lower = self._closest_below(boundaries, start, tolerance)
            if lower is not None:
                start = lower
                refined = True
            upper = self._closest_above(boundaries, end, tolerance)
            if upper is not None:
                end = upper
                refined = True

        start = max(0.0, min(start, segment.start + tolerance))
        end = max(segment.end - tolerance, end)
        if duration is not None:
            end = min(end, duration)
            start = min(start, max(0.0, end - 0.5))

        return SegmentTiming(
            start=round(start, 3),
            end=round(end, 3),
            ai_start=segment.start,
            ai_end=segment.end,
            scene_refined=refined,
        )

    def refine_all(
        self,
        segments: Iterable[DetectedSegment],
        boundaries: Sequence[float],
        *,
        duration: float | None = None,
    ) -> list[SegmentTiming]:
        return [self.refine(segment, boundaries, duration=duration) for segment in segments]

    @staticmethod
    def _closest_below(boundaries: Sequence[float], value: float, tolerance: float) -> float | None:
        candidates = [b for b in boundaries if 0.0 <= value - b <= tolerance]
        return max(candidates) if candidates else None

    @staticmethod
    def _closest_above(boundaries: Sequence[float], value: float, tolerance: float) -> float | None:
        candidates = [b for b in boundaries if 0.0 <= b - value <= tolerance]
        return min(candidates) if candidates else None


def detector_from_settings(enabled: bool = True, *, threshold: float = 27.0) -> SceneBoundaryDetector:
    """Pick the real detector when enabled, otherwise the no-op fallback."""

    if enabled:
        return PySceneDetectBoundaryDetector(threshold=threshold)
    return StaticBoundaryDetector()
