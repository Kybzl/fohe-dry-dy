"""Frame-count policies (Milestone 3.7, sections 7/8/11/13).

Two different needs, two different policies:

* **preview filtering** runs on every candidate before download, so it must be
  the cheapest stage - the frame count scales with the video duration instead
  of always sending the maximum.
* **final clip tagging** only happens for accepted clips and must observe the
  clip itself, so it uses a small, fixed set of positions inside the clip.

Both are pure functions so they can be unit tested and reused by the local and
the Douyin source without duplicating the rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class PreviewFrameBand:
    """``duration <= max_seconds`` -> ``frames`` (``max_seconds <= 0`` = catch-all)."""

    max_seconds: float
    frames: int


#: Default ladder: short Douyin clips do not need 16 frames to decide whether
#: they are usable.  Evidence from Milestone 3.6: the preview verdict was stable
#: with 7-8 frames while the prompt+image payload dominated the cost.
DEFAULT_PREVIEW_BANDS: tuple[PreviewFrameBand, ...] = (
    PreviewFrameBand(15.0, 6),
    PreviewFrameBand(45.0, 8),
    PreviewFrameBand(90.0, 10),
    PreviewFrameBand(0.0, 12),
)

#: Positions inside a final clip used for tagging (20/40/60/80%).
DEFAULT_CLIP_RATIOS: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8)


def preview_frame_count(
    duration: float | None,
    *,
    bands: Sequence[PreviewFrameBand] | None = DEFAULT_PREVIEW_BANDS,
    ceiling: int = 12,
    floor: int = 3,
) -> int:
    """Adaptive preview frame count for a video of ``duration`` seconds.

    An unknown duration falls back to the first band (the cheapest one), and the
    result is always clamped into ``[floor, ceiling]``.
    """

    ladder = list(bands) if bands else list(DEFAULT_PREVIEW_BANDS)
    max_frames = max(1, int(ceiling))
    min_frames = max(1, min(int(floor), max_frames))
    seconds = None if duration is None or duration <= 0 else float(duration)
    chosen = None
    catch_all = None
    cheapest = ladder[0].frames if ladder else None
    for band in ladder:
        if band.max_seconds <= 0:
            catch_all = band.frames
            continue
        if seconds is not None and seconds <= band.max_seconds:
            chosen = band.frames
            break
    if chosen is None and seconds is None:
        # unknown duration: stay cheap, the preview verdict does not need the
        # longest ladder step
        chosen = cheapest
    if chosen is None:
        chosen = catch_all
    if chosen is None:
        chosen = min_frames
    return max(min_frames, min(max_frames, int(chosen)))


def clip_frame_ratios(count: int, *, ratios: Sequence[float] = DEFAULT_CLIP_RATIOS) -> list[float]:
    """``count`` positions inside a clip, evenly spread over the ratio band.

    With the default band ``(0.2, 0.4, 0.6, 0.8)`` asking for 4 frames gives
    exactly those positions; other counts are interpolated inside the same
    ``[0.2, 0.8]`` range so the clip is never sampled at its very edges.
    """

    wanted = max(1, int(count))
    if wanted == 1:
        return [ratios[len(ratios) // 2] if ratios else 0.5]
    low, high = (ratios[0], ratios[-1]) if ratios else (0.2, 0.8)
    step = (high - low) / (wanted - 1)
    return [round(low + step * index, 4) for index in range(wanted)]


def clip_frame_timestamps(
    start: float,
    end: float,
    count: int,
    *,
    ratios: Sequence[float] = DEFAULT_CLIP_RATIOS,
    margin: float = 0.05,
) -> list[float]:
    """Absolute timestamps inside ``[start, end)`` for clip tagging."""

    duration = max(0.0, float(end) - float(start))
    if duration <= 0:
        return []
    positions = clip_frame_ratios(count, ratios=ratios)
    earliest = float(start) + min(margin, duration / 4)
    latest = float(end) - min(margin, duration / 4)
    stamps: list[float] = []
    for ratio in positions:
        stamp = float(start) + duration * float(ratio)
        stamps.append(round(min(max(stamp, earliest), latest), 3))
    # keep the order, drop exact duplicates
    ordered: list[float] = []
    for stamp in stamps:
        if stamp not in ordered:
            ordered.append(stamp)
    return ordered
