"""Conservative local subtitle cleanup (Milestone 9.2).

The pipeline is deliberately boring and measurable:

    eligibility gate
      -> bounded 2 fps local OCR geometry pass
      -> temporal region tracking (IoU / vertical geometry, text-agnostic)
      -> smallest safe mask rectangles + area guards
      -> deterministic local FFmpeg delogo (time scoped)
      -> ffprobe + visual quality guard
      -> post-cleanup OCR comparison
      -> atomic derivative + SQLite record

The original library clip is immutable.  A failed or uncertain attempt keeps
the original authoritative media and records why cleanup did not happen.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import shutil
import statistics
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from analyzers.subtitle_analysis import SubtitleAnalyzer
from core.config import AppSettings
from core.models import ClipRecord, SubtitleType
from core.subtitle_cleanup_models import (
    CLEANUP_VERSION,
    REVIEW_FAILURE_CLASSES,
    CleanupMask,
    CleanupReviewStatus,
    CleanupStatus,
    SubtitleCleanupConfig,
    TrackRecord,
)
from core.subtitle_models import (
    ANALYSIS_VERSION,
    FrameTextMetrics,
    SubtitleAnalysisResult,
    TextRegion,
    frame_metrics,
)
from media.ffmpeg import EncodeSettings, MediaInfo, MediaToolkit
from media.subtitle_cleanup import (
    CleanupEngine,
    FFmpegDelogoEngine,
    OpenCVInpaintEngine,
)
from storage.library import MaterialLibrary

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class OcrSample:
    """One detected text box at one timestamp."""

    timestamp: float
    region: TextRegion


@dataclass
class CleanupOutcome:
    """Operator-facing result of one cleanup attempt."""

    clip_id: int
    status: CleanupStatus
    reason: str = ""
    output_path: Path | None = None
    engine: str = ""
    version: str = CLEANUP_VERSION
    record_id: int | None = None
    before: SubtitleAnalysisResult | None = None
    after: SubtitleAnalysisResult | None = None
    masks: list[CleanupMask] = field(default_factory=list)
    tracks: list[TrackRecord] = field(default_factory=list)
    quality: dict[str, Any] = field(default_factory=dict)
    reduction: dict[str, Any] = field(default_factory=dict)
    review_status: str = str(CleanupReviewStatus.PENDING)
    review_note: str = ""
    review_failure_class: str = ""
    processing_ms: int = 0
    elapsed_ms: int = 0
    error: str = ""
    reused: bool = False

    @property
    def ok(self) -> bool:
        return self.status is CleanupStatus.SUCCEEDED

    def lines(self) -> list[str]:
        label = {
            CleanupStatus.NOT_NEEDED: "无需清理",
            CleanupStatus.INELIGIBLE: "不满足安全条件",
            CleanupStatus.PENDING: "等待处理",
            CleanupStatus.SUCCEEDED: "清理成功",
            CleanupStatus.FAILED_PROCESSING: "处理失败",
            CleanupStatus.FAILED_QUALITY: "质量门未通过",
            CleanupStatus.RESIDUAL_SUBTITLE: "仍有残留字幕",
        }.get(self.status, str(self.status))
        lines = [
            f"[{self.status.value}] 片段 #{self.clip_id} 字幕清理: {label}"
        ]
        if self.reason:
            lines.append(f"  原因/状态: {self.reason}")
        if self.before is not None:
            lines.append(
                f"  清理前: 分类={self.before.classification} "
                f"洁净度={self.before.cleanliness_score:.2f} "
                f"区域/帧最多={self.before.max_text_regions} "
                f"覆盖率={self.before.total_text_area_ratio_avg:.3f}"
            )
        if self.after is not None:
            lines.append(
                f"  清理后: 分类={self.after.classification} "
                f"洁净度={self.after.cleanliness_score:.2f} "
                f"区域/帧最多={self.after.max_text_regions} "
                f"覆盖率={self.after.total_text_area_ratio_avg:.3f}"
            )
        if self.masks:
            ratio = sum(mask.area_ratio for mask in self.masks)
            lines.append(
                f"  清理区域: {len(self.masks)} 个，归一化遮罩面积 {ratio:.3f}"
            )
            for mask in self.masks[:5]:
                lines.append(
                    f"    x1={mask.x1:.3f} y1={mask.y1:.3f} "
                    f"x2={mask.x2:.3f} y2={mask.y2:.3f} "
                    f"持续={mask.persistence_ratio:.0%}"
                )
        if self.quality:
            lines.append(
                f"  质量门: {'通过' if self.quality.get('passed') else '未通过'}"
                f" (outside_diff={self.quality.get('outside_mean_diff_max')})"
            )
        if self.reduction:
            lines.append(
                f"  字幕证据: before={self.reduction.get('before_count')} "
                f"after={self.reduction.get('after_count')} "
                f"residual={self.reduction.get('residual_count')} "
                f"减少={self.reduction.get('reduced')}"
            )
        if self.output_path is not None:
            lines.append(f"  清理文件: {self.output_path}")
        lines.append(
            f"  人工复核: {self.review_status}"
            + (f" | 失败分类: {self.review_failure_class}" if self.review_failure_class else "")
        )
        if self.review_note:
            lines.append(f"  复核备注: {self.review_note}")
        if self.error:
            lines.append(f"  错误: {self.error}")
        lines.append(f"  清理版本: {self.version} | 引擎: {self.engine or '-'} | 耗时: {self.elapsed_ms}ms")
        return lines


@dataclass
class CleanupCandidate:
    """One read-only row of the production candidate scan (Milestone 9.3)."""

    clip_id: int
    category: str
    material: str
    subtitle_class: str
    cleanliness: float
    duration: float
    eligibility: str
    eligible: bool
    cleanup_status: str = ""
    review_status: str = ""
    reason: str = ""
    measured: bool = False
    existing_output_path: str = ""

    @property
    def processable(self) -> bool:
        """Whether a bounded batch should attempt this candidate."""

        if not self.eligible:
            return False
        if self.cleanup_status == str(CleanupStatus.SUCCEEDED):
            return False
        if self.cleanup_status in (
            str(CleanupStatus.NOT_NEEDED),
            str(CleanupStatus.INELIGIBLE),
        ):
            return False
        return True

    def row(self) -> list[Any]:
        return [
            self.clip_id,
            self.category or "未记录",
            self.material,
            self.subtitle_class,
            round(self.cleanliness, 2),
            round(self.duration, 2),
            self.eligibility,
            self.cleanup_status or "-",
            self.review_status or "-",
            self.reason,
        ]


# ---------------------------------------------------------------------------
# Pure geometry helpers (unit tested with synthetic boxes)
# ---------------------------------------------------------------------------
def _region_overlap_ratio(region: TextRegion, mask: CleanupMask) -> float:
    """Share of ``region`` that lies inside one cleanup mask."""

    if region.area_ratio <= 0:
        return 0.0
    left = max(region.x1, mask.x1)
    top = max(region.y1, mask.y1)
    right = min(region.x2, mask.x2)
    bottom = min(region.y2, mask.y2)
    if right <= left or bottom <= top:
        return 0.0
    return round(((right - left) * (bottom - top)) / region.area_ratio, 4)


def _merge_intervals(
    timestamps: Sequence[float],
    *,
    gap_seconds: float,
    padding_seconds: float,
) -> list[list[float]]:
    """Merge observation timestamps into time-bounded intervals."""

    if not timestamps:
        return []
    ordered = sorted(float(value) for value in timestamps)
    intervals: list[list[float]] = []
    start = end = ordered[0]
    for value in ordered[1:]:
        if value - end <= gap_seconds:
            end = value
            continue
        intervals.append([start, end])
        start = end = value
    intervals.append([start, end])
    padded = [
        [max(0.0, first - padding_seconds), last + padding_seconds]
        for first, last in intervals
    ]
    merged: list[list[float]] = []
    for first, last in padded:
        if merged and first <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], last)
        else:
            merged.append([first, last])
    return merged


def _match_track_index(
    tracks: Sequence[list[tuple[float, TextRegion]]],
    region: TextRegion,
    *,
    timestamp: float,
    config: SubtitleCleanupConfig,
) -> int | None:
    """Closest existing track for one region (geometry only, no text)."""

    best_index: int | None = None
    best_score = -1.0
    for index, points in enumerate(tracks):
        if not points:
            continue
        if round(points[-1][0], 3) == round(float(timestamp), 3):
            # one track may hold at most one box per sampled timestamp; a
            # second box in the same frame starts its own track.
            continue
        last = points[-1][1]
        iou = region.iou(last)
        vertical_distance = abs(region.center_y - last.center_y)
        distance = region.center_distance(last)
        if iou >= config.min_iou:
            score = 1.0 + iou
        elif (
            vertical_distance <= config.max_center_distance
            and distance <= config.max_center_distance * 1.6
        ):
            score = 0.5 + max(0.0, 1.0 - distance)
        else:
            continue
        if score > best_score:
            best_score = score
            best_index = index
    return best_index


def build_tracks(
    samples: Sequence[OcrSample],
    *,
    config: SubtitleCleanupConfig,
    total_samples: int | None = None,
) -> list[TrackRecord]:
    """Associate text boxes across timestamps by IoU / vertical geometry.

    The recognized *text* is intentionally ignored: a subtitle line changes
    wording while remaining the same on-screen region.
    """

    ordered = sorted(samples, key=lambda sample: (sample.timestamp, sample.region.x1))
    tracks: list[list[tuple[float, TextRegion]]] = []
    for sample in ordered:
        if sample.region.confidence < config.min_confidence:
            continue
        index = _match_track_index(
            tracks, sample.region, timestamp=sample.timestamp, config=config
        )
        if index is None:
            tracks.append([(sample.timestamp, sample.region)])
        else:
            tracks[index].append((sample.timestamp, sample.region))

    observation_count = max(
        1,
        int(total_samples)
        if total_samples is not None
        else len({round(sample.timestamp, 3) for sample in ordered}) or 1,
    )
    gap = max(0.4, 2.5 / max(0.1, float(config.sample_fps)))
    records: list[TrackRecord] = []
    for points in tracks:
        distinct_timestamps = sorted(
            {round(timestamp, 3) for timestamp, _region in points}
        )
        timestamps = distinct_timestamps
        regions = [region for _timestamp, region in points]
        median_x1 = statistics.median(region.x1 for region in regions)
        median_y1 = statistics.median(region.y1 for region in regions)
        median_x2 = statistics.median(region.x2 for region in regions)
        median_y2 = statistics.median(region.y2 for region in regions)
        median_area = max(1e-9, (median_x2 - median_x1) * (median_y2 - median_y1))
        union_x1 = min(region.x1 for region in regions)
        union_y1 = min(region.y1 for region in regions)
        union_x2 = max(region.x2 for region in regions)
        union_y2 = max(region.y2 for region in regions)
        union_area = max(0.0, (union_x2 - union_x1) * (union_y2 - union_y1))
        median_cx = (median_x1 + median_x2) / 2
        median_cy = (median_y1 + median_y2) / 2
        vertical_jitter = max(abs(region.center_y - median_cy) for region in regions)
        center_jitter = max(
            ((region.center_x - median_cx) ** 2 + (region.center_y - median_cy) ** 2)
            ** 0.5
            for region in regions
        )
        texts: list[str] = []
        for region in regions:
            text = (region.text or "").strip()
            if text and text not in texts:
                texts.append(text)
        records.append(
            TrackRecord(
                first_seen=round(min(timestamps), 3),
                last_seen=round(max(timestamps), 3),
                sample_count=len(distinct_timestamps),
                total_samples=observation_count,
                persistence_ratio=round(
                    min(1.0, len(distinct_timestamps) / observation_count), 4
                ),
                median_x1=round(median_x1, 5),
                median_y1=round(median_y1, 5),
                median_x2=round(median_x2, 5),
                median_y2=round(median_y2, 5),
                union_x1=round(union_x1, 5),
                union_y1=round(union_y1, 5),
                union_x2=round(union_x2, 5),
                union_y2=round(union_y2, 5),
                vertical_jitter=round(vertical_jitter, 5),
                center_jitter=round(center_jitter, 5),
                union_growth=round(max(0.0, union_area / median_area - 1.0), 4),
                median_area_ratio=round(median_area, 5),
                union_area_ratio=round(union_area, 5),
                active_intervals=_merge_intervals(
                    timestamps,
                    gap_seconds=gap,
                    padding_seconds=max(0.0, float(config.padding_seconds)),
                ),
                texts=texts[:8],
            )
        )
    records.sort(key=lambda item: (item.persistence_ratio, item.union_area_ratio), reverse=True)
    return records


def stable_tracks(
    tracks: Sequence[TrackRecord],
    *,
    config: SubtitleCleanupConfig,
) -> list[TrackRecord]:
    """Tracks that pass every configurable stability threshold."""

    stable: list[TrackRecord] = []
    for track in tracks:
        if track.sample_count < max(2, int(config.min_samples)):
            continue
        if track.persistence_ratio < config.min_persistence:
            continue
        if track.persistence_ratio < 0.9 and (track.last_seen - track.first_seen) < config.min_track_seconds:
            continue
        if track.vertical_jitter > config.max_vertical_jitter:
            continue
        if track.union_growth > config.max_union_growth:
            continue
        if track.median_area_ratio > config.max_region_area_ratio:
            continue
        if track.union_area_ratio > config.max_union_area_ratio:
            continue
        stable.append(track)
    return stable


def plan_masks(
    tracks: Sequence[TrackRecord],
    *,
    config: SubtitleCleanupConfig,
    width: int,
    height: int,
) -> tuple[list[CleanupMask], str]:
    """Smallest safe mask rectangles, or ``([], reason)`` when unsafe."""

    if not tracks:
        return [], "no_stable_subtitle_track"
    masks: list[CleanupMask] = []
    rejected = 0
    margin_x = max(config.margin_ratio, config.min_margin_pixels / max(1, width))
    margin_y = max(config.margin_ratio, config.min_margin_pixels / max(1, height))
    for track in tracks:
        x1 = max(0.0, min(track.union_x1 - margin_x, 1.0))
        y1 = max(0.0, min(track.union_y1 - margin_y, 1.0))
        x2 = max(0.0, min(track.union_x2 + margin_x, 1.0))
        y2 = max(0.0, min(track.union_y2 + margin_y, 1.0))
        if x2 - x1 <= 0 or y2 - y1 <= 0:
            rejected += 1
            continue
        mask = CleanupMask(
            x1=round(x1, 5),
            y1=round(y1, 5),
            x2=round(x2, 5),
            y2=round(y2, 5),
            persistence_ratio=track.persistence_ratio,
            sample_count=track.sample_count,
            first_seen=track.first_seen,
            last_seen=track.last_seen,
            active_intervals=[list(item) for item in track.active_intervals],
            reason="temporal_track",
        )
        if mask.width_ratio > config.max_width_ratio:
            rejected += 1
            continue
        if mask.height_ratio > config.max_height_ratio:
            rejected += 1
            continue
        if mask.area_ratio > config.max_region_area_ratio:
            rejected += 1
            continue
        masks.append(mask)

    masks.sort(key=lambda item: item.area_ratio, reverse=True)
    masks = masks[: max(1, int(config.max_regions))]
    total_area = sum(mask.area_ratio for mask in masks)
    if not masks:
        return [], "mask_geometry_guard"
    if total_area > config.max_total_area_ratio:
        return [], "mask_total_area_guard"
    if rejected and not masks:
        return [], "mask_geometry_guard"
    return masks, ""


def evaluate_evidence_reduction(
    before: SubtitleAnalysisResult,
    after: SubtitleAnalysisResult,
    masks: Sequence[CleanupMask],
    *,
    config: SubtitleCleanupConfig,
) -> dict[str, Any]:
    """Did the post-cleanup OCR measurably reduce subtitle evidence?"""

    residual = 0
    residual_regions: list[TextRegion] = []
    for frame in after.frames:
        for region in frame.regions:
            if region.confidence < config.min_confidence:
                continue
            if any(_region_overlap_ratio(region, mask) >= 0.10 for mask in masks):
                residual += 1
                residual_regions.append(region)
    before_count = int(before.max_text_regions)
    after_count = int(after.max_text_regions)
    before_area = float(before.total_text_area_ratio_avg)
    after_area = float(after.total_text_area_ratio_avg)
    count_reduced = after_count < before_count
    area_reduced = before_area > 0 and after_area <= before_area * (
        1.0 - max(0.0, min(1.0, config.min_evidence_reduction))
    )
    reduced = bool(count_reduced or area_reduced)
    return {
        "before_count": before_count,
        "after_count": after_count,
        "before_area": round(before_area, 5),
        "after_area": round(after_area, 5),
        "before_cleanliness": before.cleanliness_score,
        "after_cleanliness": after.cleanliness_score,
        "residual_count": residual,
        "residual_regions": [region.model_dump(mode="json") for region in residual_regions[:10]],
        "count_reduced": count_reduced,
        "area_reduced": area_reduced,
        "reduced": reduced,
        "success": reduced and residual == 0,
    }


# ---------------------------------------------------------------------------
# Local visual quality guard
# ---------------------------------------------------------------------------
def _scaled_box(
    mask: CleanupMask,
    *,
    width: int,
    height: int,
    scaled_width: int,
    scaled_height: int,
) -> tuple[int, int, int, int]:
    x, y, box_w, box_h = mask.pixel_box(width, height)
    x1 = int(round(x * scaled_width / max(1, width)))
    y1 = int(round(y * scaled_height / max(1, height)))
    x2 = int(round((x + box_w) * scaled_width / max(1, width)))
    y2 = int(round((y + box_h) * scaled_height / max(1, height)))
    x1 = max(0, min(scaled_width - 1, x1))
    y1 = max(0, min(scaled_height - 1, y1))
    x2 = max(x1 + 1, min(scaled_width, x2))
    y2 = max(y1 + 1, min(scaled_height, y2))
    return x1, y1, x2 - x1, y2 - y1


def evaluate_quality_guard(
    before_paths: Sequence[Path],
    after_paths: Sequence[Path],
    masks: Sequence[CleanupMask],
    *,
    config: SubtitleCleanupConfig,
    before_info: MediaInfo,
    after_info: MediaInfo,
) -> tuple[bool, dict[str, Any]]:
    """Deterministic visual sanity check of a candidate derivative.

    The original is kept unless duration/resolution match, the content outside
    the masks stays effectively unchanged, and no mask turns into a black or
    grossly blurred rectangle.
    """

    metrics: dict[str, Any] = {
        "passed": False,
        "checks": [],
        "duration_before": before_info.duration,
        "duration_after": after_info.duration,
        "resolution_before": [before_info.width, before_info.height],
        "resolution_after": [after_info.width, after_info.height],
    }
    if not before_paths or not after_paths:
        metrics["checks"].append("comparison frames missing")
        return False, metrics
    if abs(float(after_info.duration) - float(before_info.duration)) > config.duration_tolerance_seconds:
        metrics["checks"].append("duration drift")
        return False, metrics
    if (
        before_info.width is not None
        and after_info.width is not None
        and abs(int(after_info.width) - int(before_info.width)) > config.resolution_tolerance_pixels
    ) or (
        before_info.height is not None
        and after_info.height is not None
        and abs(int(after_info.height) - int(before_info.height)) > config.resolution_tolerance_pixels
    ):
        metrics["checks"].append("resolution drift")
        return False, metrics

    try:
        from PIL import Image, ImageChops, ImageFilter, ImageStat
    except Exception as exc:  # pragma: no cover - Pillow is an OCR dependency
        metrics["checks"].append(f"quality guard unavailable: {exc}")
        return False, metrics

    outside_diffs: list[float] = []
    inside_diffs: list[float] = []
    solid_regions = 0
    blur_regions = 0
    smear_regions = 0
    evaluated_regions = 0
    directional_gradient_ratios: list[float] = []
    for before_path, after_path in zip(before_paths, after_paths):
        try:
            with Image.open(before_path) as before_image, Image.open(after_path) as after_image:
                before_gray = before_image.convert("L")
                after_gray = after_image.convert("L")
                if before_gray.size != after_gray.size:
                    metrics["checks"].append("comparison frame size mismatch")
                    return False, metrics
                width, height = before_gray.size
                scale = min(1.0, 360.0 / max(1, width))
                scaled_width = max(1, int(round(width * scale)))
                scaled_height = max(1, int(round(height * scale)))
                before_small = before_gray.resize((scaled_width, scaled_height))
                after_small = after_gray.resize((scaled_width, scaled_height))
                diff = ImageChops.difference(before_small, after_small)
                full_mean = float(ImageStat.Stat(diff).mean[0])
                full_pixels = float(scaled_width * scaled_height)
                inside_pixels = 0
                inside_sum = 0.0
                for mask in masks:
                    evaluated_regions += 1
                    x, y, box_w, box_h = _scaled_box(
                        mask,
                        width=width,
                        height=height,
                        scaled_width=scaled_width,
                        scaled_height=scaled_height,
                    )
                    box = (x, y, x + box_w, y + box_h)
                    pixels = float(box_w * box_h)
                    inside_pixels += pixels
                    crop = diff.crop(box)
                    inside_mean = float(ImageStat.Stat(crop).mean[0])
                    inside_sum += inside_mean * pixels
                    inside_diffs.append(inside_mean)
                    after_crop = after_small.crop(box)
                    before_crop = before_small.crop(box)
                    after_stats = ImageStat.Stat(after_crop)
                    before_stats = ImageStat.Stat(before_crop)
                    after_mean = float(after_stats.mean[0])
                    after_std = float(after_stats.stddev[0])
                    before_mean = float(before_stats.mean[0])
                    before_std = float(before_stats.stddev[0])
                    after_edges = float(
                        ImageStat.Stat(after_crop.filter(ImageFilter.FIND_EDGES)).stddev[0]
                    )
                    before_edges = float(
                        ImageStat.Stat(before_crop.filter(ImageFilter.FIND_EDGES)).stddev[0]
                    )
                    # A failed inpaint can retain variance while stretching
                    # nearby pixels into conspicuous stripes.  Measure local
                    # change in both directions so that collapse in either
                    # direction is rejected instead of being hidden by the
                    # other direction's surviving edges.
                    def directional_gradients(image: Image.Image) -> tuple[float, float]:
                        crop_width, crop_height = image.size
                        horizontal = 0.0
                        vertical = 0.0
                        if crop_width > 1:
                            horizontal = float(
                                ImageStat.Stat(
                                    ImageChops.difference(
                                        image.crop((1, 0, crop_width, crop_height)),
                                        image.crop((0, 0, crop_width - 1, crop_height)),
                                    )
                                ).mean[0]
                            )
                        if crop_height > 1:
                            vertical = float(
                                ImageStat.Stat(
                                    ImageChops.difference(
                                        image.crop((0, 1, crop_width, crop_height)),
                                        image.crop((0, 0, crop_width, crop_height - 1)),
                                    )
                                ).mean[0]
                            )
                        return horizontal, vertical

                    before_gradients = directional_gradients(before_crop)
                    after_gradients = directional_gradients(after_crop)
                    gradient_ratios = [
                        after_value / before_value
                        for before_value, after_value in zip(before_gradients, after_gradients)
                        if before_value > config.flat_region_edge_std_min
                    ]
                    directional_ratio = min(gradient_ratios, default=1.0)
                    directional_gradient_ratios.append(directional_ratio)
                    if directional_ratio < config.directional_gradient_ratio_min:
                        smear_regions += 1
                    if (
                        after_mean <= config.dark_region_mean_max
                        and after_std <= config.dark_region_std_max
                        and before_mean > config.dark_region_mean_max
                    ):
                        solid_regions += 1
                    if (
                        before_std > 2.0 and after_std <= 1.0
                    ) or (
                        before_std > 20.0
                        and after_std < before_std * config.region_std_ratio_min
                    ) or (
                        before_edges > config.flat_region_edge_std_min
                        and after_edges < before_edges * config.blur_ratio_min
                    ) or after_edges <= 0.05:
                        blur_regions += 1
                inside_pixels = min(inside_pixels, full_pixels)
                outside_pixels = max(1.0, full_pixels - inside_pixels)
                outside_mean = max(
                    0.0,
                    (full_mean * full_pixels - inside_sum) / outside_pixels,
                )
                outside_diffs.append(outside_mean)
        except Exception as exc:
            metrics["checks"].append(f"comparison frame unreadable: {exc}")
            return False, metrics

    metrics.update(
        {
            "outside_mean_diff_max": round(max(outside_diffs, default=0.0), 3),
            "inside_mean_diff_max": round(max(inside_diffs, default=0.0), 3),
            "solid_regions": solid_regions,
            "blur_regions": blur_regions,
            "smear_regions": smear_regions,
            "evaluated_regions": evaluated_regions,
            "smear_region_fraction": round(
                smear_regions / max(1, evaluated_regions), 4
            ),
            "directional_gradient_ratio_min": round(
                min(directional_gradient_ratios, default=1.0), 3
            ),
        }
    )
    if solid_regions:
        metrics["checks"].append("solid/black cleanup rectangle")
        return False, metrics
    if blur_regions:
        metrics["checks"].append("grossly blurred cleanup rectangle")
        return False, metrics
    if metrics["smear_region_fraction"] > config.directional_smear_fraction_max:
        metrics["checks"].append("directional inpaint smear")
        return False, metrics
    if metrics["outside_mean_diff_max"] > config.outside_mean_diff_max:
        metrics["checks"].append("outside-mask content changed too much")
        return False, metrics
    metrics["passed"] = True
    metrics["checks"].append("duration/resolution preserved; outside-mask diff within tolerance")
    return True, metrics


def sample_timestamps(duration: float, *, config: SubtitleCleanupConfig) -> list[float]:
    """Bounded 2 fps (configurable) timestamps for the cleanup OCR pass."""

    fps = max(0.2, float(config.sample_fps))
    if duration <= 0:
        return []
    step = 1.0 / fps
    count = min(
        max(1, int(float(config.max_samples))),
        max(1, int(duration * fps)),
    )
    stamps = [round(index * step, 3) for index in range(count)]
    return [stamp for stamp in stamps if stamp < max(0.05, duration)]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class SubtitleCleanupService:
    """Conservative cleanup orchestration for stored library clips."""

    def __init__(
        self,
        library: MaterialLibrary,
        settings: AppSettings,
        *,
        config: SubtitleCleanupConfig | None = None,
        analyzer: SubtitleAnalyzer | None = None,
        toolkit: MediaToolkit | None = None,
        engine: CleanupEngine | None = None,
    ) -> None:
        self.library = library
        self.settings = settings
        self.config = config or settings.subtitle_cleanup
        self.analyzer = analyzer
        self.toolkit = toolkit
        self.engine = engine
        self._temp_root = settings.paths.cache_dir / "subtitle_cleanup"
        self._preserved_output: Path | None = None

    # -- dependency helpers ------------------------------------------------
    def _analyzer(self) -> SubtitleAnalyzer:
        if self.analyzer is None:
            from core.dependencies import build_subtitle_analyzer

            self.analyzer = build_subtitle_analyzer(
                self.settings, cache=self.library, media_backend="ffmpeg"
            )
        if self.analyzer is None:  # pragma: no cover - disabled by config
            raise RuntimeError("subtitle analysis is disabled in config")
        return self.analyzer

    def _toolkit(self) -> MediaToolkit:
        if self.toolkit is None:
            from core.dependencies import build_toolkit

            self.toolkit = build_toolkit(
                self.settings, backend=self.settings.media.backend
            )
        return self.toolkit

    def _engine(self) -> CleanupEngine:
        if self.engine is None:
            common = {
                "ffmpeg_bin": self.settings.media.ffmpeg_path or "ffmpeg",
                "timeout": self.settings.media.command_timeout_seconds,
                "time_scoped": self.config.time_scoped,
            }
            if self.config.engine == "opencv_inpaint":
                self.engine = OpenCVInpaintEngine(
                    **common,
                    radius=self.config.inpaint_radius,
                )
            else:
                self.engine = FFmpegDelogoEngine(**common)
        return self.engine

    def _encode_settings(self) -> EncodeSettings:
        return EncodeSettings(
            video_codec=self.settings.media.video_codec,
            crf=self.settings.media.crf,
            preset=self.settings.media.preset,
            audio_codec=self.settings.media.audio_codec,
            audio_bitrate=self.settings.media.audio_bitrate,
        )

    # -- output paths ------------------------------------------------------
    @staticmethod
    def derivative_path(clip: ClipRecord, version: str = CLEANUP_VERSION) -> Path:
        """Deterministic shallow derivative path for one clip/version."""

        original = Path(clip.file_path)
        parent = original.parent
        category_dir = parent.parent if parent.name.lower() == "clips" else parent
        return category_dir / "clean" / f"{original.stem}__{version}.mp4"

    # -- OCR sampling ------------------------------------------------------
    def _ocr_frames(
        self,
        frames: Sequence[tuple[float, Path]],
        *,
        analyzer: SubtitleAnalyzer,
        config: SubtitleCleanupConfig,
    ) -> tuple[list[OcrSample], list[FrameTextMetrics], int]:
        samples: list[OcrSample] = []
        metrics: list[FrameTextMetrics] = []
        errors = 0
        detector = analyzer.detector
        for timestamp, path in frames:
            try:
                regions = detector.detect(path, recognize=True)
            except Exception as exc:
                errors += 1
                LOGGER.debug("cleanup OCR failed for %s: %s", path, exc)
                continue
            for region in regions:
                if region.confidence >= config.min_confidence:
                    samples.append(OcrSample(timestamp=float(timestamp), region=region))
            metrics.append(
                frame_metrics(
                    regions,
                    zones=analyzer.settings.zones,
                    rules=analyzer.settings.rules,
                    timestamp=float(timestamp),
                )
            )
        return samples, metrics, errors

    async def _extract_frames(
        self,
        video: Path,
        timestamps: Sequence[float],
        directory: Path,
        prefix: str,
    ) -> list[Path]:
        if not timestamps:
            return []
        return await self._toolkit().extract_frames(
            video, timestamps, directory, prefix, size=None
        )

    # -- cleanup -----------------------------------------------------------
    def _outcome_from_record(
        self,
        clip_id: int,
        record: dict[str, Any],
        *,
        reused: bool = True,
    ) -> CleanupOutcome:
        try:
            status = CleanupStatus(str(record.get("status")))
        except ValueError:  # pragma: no cover - forward-compatible database row
            status = CleanupStatus.PENDING
        before = None
        after = None
        if record.get("before_metrics"):
            try:
                before = SubtitleAnalysisResult.model_validate(record["before_metrics"])
            except Exception:  # pragma: no cover - defensive
                before = None
        if record.get("after_metrics"):
            try:
                after = SubtitleAnalysisResult.model_validate(record["after_metrics"])
            except Exception:  # pragma: no cover - defensive
                after = None
        masks: list[CleanupMask] = []
        for item in record.get("regions") or []:
            try:
                masks.append(CleanupMask.model_validate(item))
            except Exception:  # pragma: no cover - defensive
                continue
        return CleanupOutcome(
            clip_id=clip_id,
            status=status,
            reason=str(record.get("skip_reason") or ""),
            output_path=Path(record["output_path"]) if record.get("output_path") else None,
            engine=str(record.get("engine") or ""),
            version=str(record.get("version") or CLEANUP_VERSION),
            record_id=int(record.get("id") or 0) or None,
            before=before,
            after=after,
            masks=masks,
            review_status=str(record.get("review_status") or CleanupReviewStatus.PENDING),
            review_note=str(record.get("review_note") or ""),
            review_failure_class=str(record.get("review_failure_class") or ""),
            processing_ms=int(record.get("processing_ms") or 0),
            error=str(record.get("error") or ""),
            reused=reused,
        )

    def _persist(
        self,
        clip_id: int,
        *,
        status: CleanupStatus,
        reason: str = "",
        engine: str = "",
        source_analysis_version: str = "",
        output_path: Path | None = None,
        eligible: bool = False,
        masks: Sequence[CleanupMask] = (),
        before: SubtitleAnalysisResult | None = None,
        after: SubtitleAnalysisResult | None = None,
        quality: dict[str, Any] | None = None,
        reduction: dict[str, Any] | None = None,
        processing_ms: int = 0,
        review_status: str | None = None,
        review_note: str | None = None,
        review_failure_class: str | None = None,
        error: str = "",
    ) -> int:
        existing = self.library.subtitle_cleanup(clip_id, self.config.version)
        effective_review = (
            str(review_status)
            if review_status is not None
            else str((existing or {}).get("review_status") or CleanupReviewStatus.PENDING)
        )
        if status is CleanupStatus.SUCCEEDED and review_status is None:
            # fresh pixels always require a new human review
            effective_review = str(CleanupReviewStatus.PENDING)
        effective_note = (
            str(review_note)
            if review_note is not None
            else str((existing or {}).get("review_note") or "")
        )
        effective_failure = (
            str(review_failure_class)
            if review_failure_class
            else str((existing or {}).get("review_failure_class") or "") or None
        )
        return self.library.save_subtitle_cleanup(
            clip_id=clip_id,
            version=self.config.version,
            status=status,
            engine=engine,
            source_analysis_version=source_analysis_version,
            # a forced retry that ends not-needed/ineligible keeps any previous
            # healthy derivative referenced, so it never becomes an orphan file
            output_path=output_path or self._preserved_output,
            eligible=eligible,
            skip_reason=reason,
            regions=[mask.model_dump(mode="json") for mask in masks],
            before_metrics=before.model_dump(mode="json") if before else None,
            after_metrics=after.model_dump(mode="json") if after else None,
            settings=self.config.model_dump(mode="json"),
            quality=quality,
            reduction=reduction,
            processing_ms=int(processing_ms or 0),
            review_status=effective_review,
            review_note=effective_note,
            review_failure_class=effective_failure,
            reviewed_at=(existing or {}).get("reviewed_at") if review_status is None else None,
            error=error,
        )

    @staticmethod
    def _atomic_publish(temp_path: Path, dest: Path) -> None:
        """Move a validated temp file to its final path without a partial final."""

        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.parent / f".{dest.name}.{uuid.uuid4().hex[:8]}.part"
        try:
            shutil.move(str(temp_path), str(part))
            os.replace(part, dest)
        finally:
            if part.exists():
                part.unlink(missing_ok=True)

    @staticmethod
    def _cleanup_dir(directory: Path) -> None:
        if not directory.exists():
            return
        try:
            shutil.rmtree(directory, ignore_errors=True)
        except OSError as exc:  # pragma: no cover - defensive
            LOGGER.debug("could not remove cleanup temp dir %s: %s", directory, exc)

    async def cleanup_clip(self, clip_id: int, *, force: bool = False) -> CleanupOutcome:
        """Run (or report) one conservative cleanup attempt for one clip."""

        started = time.perf_counter()
        clip = self.library.get_clip(int(clip_id))
        if clip is None:
            return CleanupOutcome(
                clip_id=int(clip_id),
                status=CleanupStatus.FAILED_PROCESSING,
                reason="clip_not_found",
                error=f"clip #{clip_id} does not exist",
            )
        if clip.id is None:
            return CleanupOutcome(
                clip_id=int(clip_id),
                status=CleanupStatus.FAILED_PROCESSING,
                reason="clip_without_id",
            )
        if not self.config.enabled:
            record_id = self._persist(
                int(clip.id),
                status=CleanupStatus.INELIGIBLE,
                reason="subtitle_cleanup_disabled",
                eligible=False,
            )
            return CleanupOutcome(
                clip_id=int(clip.id),
                status=CleanupStatus.INELIGIBLE,
                reason="subtitle_cleanup_disabled",
                record_id=record_id,
                version=self.config.version,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )

        existing = self.library.subtitle_cleanup(int(clip.id), self.config.version)
        if existing is not None and not force:
            outcome = self._outcome_from_record(int(clip.id), existing, reused=True)
            if outcome.status is CleanupStatus.SUCCEEDED:
                output = self.library.safe_media_path(outcome.output_path)
                if output is None or not output.exists():
                    outcome.reason = "existing_derivative_missing_use_force"
                    outcome.output_path = None
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return outcome

        previous_output: Path | None = None
        if existing is not None and existing.get("output_path"):
            candidate = self.library.safe_media_path(existing.get("output_path"))
            if candidate is not None and candidate.exists():
                previous_output = candidate
        self._preserved_output = previous_output
        temp_dir = self._temp_root / f"clip_{int(clip.id)}_{uuid.uuid4().hex[:8]}"
        temp_output = temp_dir / "candidate.mp4"
        output_path = self.derivative_path(clip, self.config.version)
        try:
            source = self.library.safe_media_path(clip.file_path)
            if source is None or not source.exists():
                return self._record_failure(
                    clip,
                    CleanupStatus.FAILED_PROCESSING,
                    "source_media_missing_or_outside_library",
                    started,
                )
            toolkit = self._toolkit()
            analyzer = self._analyzer()
            try:
                before_info = await toolkit.probe(source)
            except Exception as exc:
                return self._record_failure(
                    clip,
                    CleanupStatus.FAILED_PROCESSING,
                    f"ffprobe_before_failed: {exc}",
                    started,
                )
            if not before_info.has_video or not before_info.width or not before_info.height:
                return self._record_failure(
                    clip,
                    CleanupStatus.FAILED_PROCESSING,
                    "source_has_no_video_stream",
                    started,
                )
            duration = float(clip.duration or before_info.duration or 0.0)
            if duration <= 0:
                return self._record_failure(
                    clip,
                    CleanupStatus.FAILED_PROCESSING,
                    "source_has_no_usable_duration",
                    started,
                )

            timestamps = sample_timestamps(duration, config=self.config)
            before_frames = await self._extract_frames(
                source, timestamps, temp_dir / "before", "before"
            )
            if not before_frames:
                return self._record_failure(
                    clip,
                    CleanupStatus.FAILED_PROCESSING,
                    "no_before_frames_extracted",
                    started,
                )
            paired = list(zip(timestamps[: len(before_frames)], before_frames))
            samples, metrics, errors = self._ocr_frames(
                paired, analyzer=analyzer, config=self.config
            )
            before_result = analyzer.build_result(
                metrics, errors=errors, frame_budget=len(timestamps)
            )
            if before_result.is_unavailable:
                return self._record_failure(
                    clip,
                    CleanupStatus.FAILED_PROCESSING,
                    f"subtitle_ocr_unavailable: {before_result.unavailable_reason}",
                    started,
                    before=before_result,
                )

            classification = str(before_result.classification)
            if before_result.classification in (
                SubtitleType.NONE,
                SubtitleType.WATERMARK_ONLY,
            ):
                record_id = self._persist(
                    int(clip.id),
                    status=CleanupStatus.NOT_NEEDED,
                    reason=f"classification={classification}",
                    eligible=False,
                    before=before_result,
                    source_analysis_version=ANALYSIS_VERSION,
                )
                return CleanupOutcome(
                    clip_id=int(clip.id),
                    status=CleanupStatus.NOT_NEEDED,
                    reason=f"classification={classification}",
                    before=before_result,
                    record_id=record_id,
                    version=self.config.version,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
            if not self.config.eligible(classification):
                record_id = self._persist(
                    int(clip.id),
                    status=CleanupStatus.INELIGIBLE,
                    reason=f"classification_not_eligible:{classification}",
                    eligible=False,
                    before=before_result,
                    source_analysis_version=ANALYSIS_VERSION,
                )
                return CleanupOutcome(
                    clip_id=int(clip.id),
                    status=CleanupStatus.INELIGIBLE,
                    reason=f"classification_not_eligible:{classification}",
                    before=before_result,
                    record_id=record_id,
                    version=self.config.version,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )

            tracks = build_tracks(
                samples, config=self.config, total_samples=len(metrics)
            )
            stable = stable_tracks(tracks, config=self.config)
            masks, guard_reason = plan_masks(
                stable,
                config=self.config,
                width=int(before_info.width),
                height=int(before_info.height),
            )
            if not masks:
                reason = guard_reason or "no_stable_subtitle_track"
                record_id = self._persist(
                    int(clip.id),
                    status=CleanupStatus.INELIGIBLE,
                    reason=reason,
                    eligible=False,
                    before=before_result,
                    source_analysis_version=ANALYSIS_VERSION,
                )
                return CleanupOutcome(
                    clip_id=int(clip.id),
                    status=CleanupStatus.INELIGIBLE,
                    reason=reason,
                    before=before_result,
                    tracks=tracks,
                    record_id=record_id,
                    version=self.config.version,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )

            engine = self._engine()
            usable, note = engine.available()
            if not usable:
                return self._record_failure(
                    clip,
                    CleanupStatus.FAILED_PROCESSING,
                    f"cleanup_engine_unavailable: {note}",
                    started,
                    before=before_result,
                    tracks=tracks,
                    masks=masks,
                )
            try:
                await engine.apply(
                    source,
                    temp_output,
                    masks,
                    width=int(before_info.width),
                    height=int(before_info.height),
                    encode_settings=self._encode_settings(),
                )
            except Exception as exc:
                LOGGER.warning("subtitle cleanup engine failed for clip %s: %s", clip.id, exc)
                return self._record_failure(
                    clip,
                    CleanupStatus.FAILED_PROCESSING,
                    f"cleanup_engine_failed: {exc}",
                    started,
                    before=before_result,
                    engine=getattr(engine, "name", ""),
                    tracks=tracks,
                    masks=masks,
                )

            try:
                after_info = await toolkit.probe(temp_output)
            except Exception as exc:
                return self._record_failure(
                    clip,
                    CleanupStatus.FAILED_PROCESSING,
                    f"ffprobe_after_failed: {exc}",
                    started,
                    before=before_result,
                    engine=getattr(engine, "name", ""),
                    tracks=tracks,
                    masks=masks,
                )

            after_frames = await self._extract_frames(
                temp_output, timestamps[: len(before_frames)], temp_dir / "after", "after"
            )
            quality_ok, quality = evaluate_quality_guard(
                before_frames,
                after_frames,
                masks,
                config=self.config,
                before_info=before_info,
                after_info=after_info,
            )
            if not quality_ok:
                return self._record_failure(
                    clip,
                    CleanupStatus.FAILED_QUALITY,
                    str(quality.get("checks") or "quality_guard_failed"),
                    started,
                    before=before_result,
                    engine=getattr(engine, "name", ""),
                    tracks=tracks,
                    masks=masks,
                    quality=quality,
                )

            after_samples, after_metrics, after_errors = self._ocr_frames(
                list(zip(timestamps[: len(after_frames)], after_frames)),
                analyzer=analyzer,
                config=self.config,
            )
            after_result = analyzer.build_result(
                after_metrics, errors=after_errors, frame_budget=len(timestamps)
            )
            if after_result.is_unavailable:
                return self._record_failure(
                    clip,
                    CleanupStatus.FAILED_PROCESSING,
                    f"post_cleanup_ocr_unavailable: {after_result.unavailable_reason}",
                    started,
                    before=before_result,
                    engine=getattr(engine, "name", ""),
                    tracks=tracks,
                    masks=masks,
                    quality=quality,
                )
            reduction = evaluate_evidence_reduction(
                before_result, after_result, masks, config=self.config
            )
            if not reduction["success"]:
                return self._record_failure(
                    clip,
                    CleanupStatus.RESIDUAL_SUBTITLE,
                    "subtitle_evidence_not_reduced_enough",
                    started,
                    before=before_result,
                    after=after_result,
                    engine=getattr(engine, "name", ""),
                    tracks=tracks,
                    masks=masks,
                    quality=quality,
                    reduction=reduction,
                )
            self._atomic_publish(temp_output, output_path)
            processing_ms = int((time.perf_counter() - started) * 1000)
            record_id = self._persist(
                int(clip.id),
                status=CleanupStatus.SUCCEEDED,
                reason="",
                engine=getattr(engine, "name", ""),
                source_analysis_version=ANALYSIS_VERSION,
                output_path=output_path,
                eligible=True,
                before=before_result,
                after=after_result,
                masks=masks,
                quality=quality,
                reduction=reduction,
                processing_ms=processing_ms,
                review_status=str(CleanupReviewStatus.PENDING),
                review_note="",
            )
            outcome = CleanupOutcome(
                clip_id=int(clip.id),
                status=CleanupStatus.SUCCEEDED,
                output_path=output_path,
                engine=getattr(engine, "name", ""),
                version=self.config.version,
                record_id=record_id,
                before=before_result,
                after=after_result,
                masks=masks,
                tracks=tracks,
                quality=quality,
                reduction=reduction,
                review_status=str(CleanupReviewStatus.PENDING),
                processing_ms=processing_ms,
                elapsed_ms=processing_ms,
            )
            self.library.log_maintenance(
                "cleanup_regenerated" if force else "cleanup_generated",
                target_type="clip",
                target_id=int(clip.id),
                details={
                    "clip_id": int(clip.id),
                    "cleanup_version": self.config.version,
                    "engine": outcome.engine,
                    "output_path": str(output_path),
                    "processing_ms": processing_ms,
                },
            )
            if self.config.review_pack:
                try:
                    await self._build_review_pack_for_clip(int(clip.id))
                except Exception as exc:  # pragma: no cover - report is best effort
                    LOGGER.warning(
                        "review pack generation failed for clip %s: %s", clip.id, exc
                    )
            return outcome
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.exception("subtitle cleanup failed for clip %s", clip.id)
            return self._record_failure(
                clip,
                CleanupStatus.FAILED_PROCESSING,
                f"unexpected_error: {exc}",
                started,
            )
        finally:
            self._cleanup_dir(temp_dir)
            self._preserved_output = None

    def _record_failure(
        self,
        clip: ClipRecord,
        status: CleanupStatus,
        reason: str,
        started: float,
        *,
        before: SubtitleAnalysisResult | None = None,
        after: SubtitleAnalysisResult | None = None,
        engine: str = "",
        tracks: Sequence[TrackRecord] = (),
        masks: Sequence[CleanupMask] = (),
        quality: dict[str, Any] | None = None,
        reduction: dict[str, Any] | None = None,
    ) -> CleanupOutcome:
        record_id = self._persist(
            int(clip.id or 0),
            status=status,
            reason=reason,
            engine=engine,
            source_analysis_version=ANALYSIS_VERSION if before else "",
            output_path=self._preserved_output,
            eligible=bool(masks),
            masks=masks,
            before=before,
            after=after,
            quality=quality,
            reduction=reduction,
            processing_ms=int((time.perf_counter() - started) * 1000),
            error=reason,
        )
        return CleanupOutcome(
            clip_id=int(clip.id or 0),
            status=status,
            reason=reason,
            engine=engine,
            version=self.config.version,
            record_id=record_id,
            before=before,
            after=after,
            masks=list(masks),
            tracks=list(tracks),
            quality=quality or {},
            reduction=reduction or {},
            review_status=str(CleanupReviewStatus.PENDING),
            processing_ms=int((time.perf_counter() - started) * 1000),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            error=reason,
        )

    # -- production workflow: candidates / batch / review / reporting ------
    def _derivative_healthy(self, record: dict[str, Any] | None) -> bool:
        if not record or str(record.get("status")) != str(CleanupStatus.SUCCEEDED):
            return False
        derivative = self.library.safe_media_path(record.get("output_path"))
        return bool(derivative is not None and derivative.exists())

    def candidate_scan(
        self,
        *,
        category: str | None = None,
        limit: int | None = None,
    ) -> list[CleanupCandidate]:
        """Read-only eligibility scan over the stored library metadata.

        This never runs OCR, FFmpeg or a model: it uses the stored measured
        subtitle analysis (falling back to the historical subtitle type) and the
        existing cleanup record.
        """

        clips = self.library.inventory_clips(limit=100000)
        if category:
            clips = [clip for clip in clips if clip.library_category == category]
        candidates: list[CleanupCandidate] = []
        for clip in clips:
            if clip.id is None:
                continue
            measured = clip.subtitle_analysis if isinstance(clip.subtitle_analysis, dict) else None
            if measured and measured.get("classification"):
                subtitle_class = str(measured.get("classification"))
                cleanliness = float(measured.get("cleanliness_score") or 0.0)
                measured_flag = True
            else:
                subtitle_class = str(clip.subtitle_type)
                cleanliness = float(clip.subtitle_cleanliness_score or 0.0)
                measured_flag = False
            record = self.library.subtitle_cleanup(int(clip.id))
            cleanup_status = str((record or {}).get("status") or "")
            review_status = str((record or {}).get("review_status") or "")
            eligible = True
            eligibility = "eligible_unprocessed"
            reason = ""
            if cleanup_status == str(CleanupStatus.SUCCEEDED):
                eligibility = "eligible_processed"
                reason = f"existing_succeeded:{review_status or 'pending'}"
                if not self._derivative_healthy(record):
                    reason = "missing_derivative"
            elif cleanup_status in (
                str(CleanupStatus.NOT_NEEDED),
                str(CleanupStatus.INELIGIBLE),
            ):
                eligibility = cleanup_status
                eligible = False
                reason = str((record or {}).get("skip_reason") or cleanup_status)
            elif subtitle_class in (
                str(SubtitleType.NONE),
                str(SubtitleType.WATERMARK_ONLY),
            ):
                eligibility = "not_needed"
                eligible = False
                reason = f"classification={subtitle_class}"
            elif not self.config.eligible(subtitle_class):
                eligibility = "ineligible"
                eligible = False
                reason = f"classification_not_eligible:{subtitle_class}"
            elif cleanup_status in (
                str(CleanupStatus.FAILED_PROCESSING),
                str(CleanupStatus.FAILED_QUALITY),
                str(CleanupStatus.RESIDUAL_SUBTITLE),
            ):
                eligibility = "eligible_failed"
                reason = f"existing_{cleanup_status}"
            elif cleanup_status == str(CleanupStatus.DERIVATIVE_DELETED):
                eligibility = "eligible_deleted"
                reason = "derivative_deleted"
            else:
                reason = f"existing_{cleanup_status}" if cleanup_status else "stored_classification"
            candidates.append(
                CleanupCandidate(
                    clip_id=int(clip.id),
                    category=clip.library_category or "",
                    material=clip.material or "",
                    subtitle_class=subtitle_class,
                    cleanliness=cleanliness,
                    duration=float(clip.duration or 0.0),
                    eligibility=eligibility,
                    eligible=eligible,
                    cleanup_status=cleanup_status,
                    review_status=review_status,
                    reason=reason,
                    measured=measured_flag,
                    existing_output_path=str((record or {}).get("output_path") or ""),
                )
            )
        candidates.sort(
            key=lambda item: (
                0 if item.eligibility == "eligible_unprocessed" else 1,
                item.clip_id,
            )
        )
        if limit is not None:
            return candidates[: max(1, int(limit))]
        return candidates

    def eligible_unprocessed(self, *, category: str | None = None) -> list[CleanupCandidate]:
        return [
            item
            for item in self.candidate_scan(category=category)
            if item.eligible and item.eligibility == "eligible_unprocessed"
        ]

    async def cleanup_batch(
        self,
        *,
        limit: int,
        force: bool = False,
        category: str | None = None,
    ) -> list[CleanupOutcome]:
        """Explicitly bounded batch; it never defaults to the whole library."""

        bound = max(1, int(limit))
        selected: list[CleanupCandidate] = []
        for candidate in self.candidate_scan(category=category):
            if not candidate.eligible:
                continue
            if candidate.cleanup_status == str(CleanupStatus.SUCCEEDED) and not force:
                continue  # existing healthy result: idempotent skip
            if candidate.cleanup_status in (
                str(CleanupStatus.FAILED_PROCESSING),
                str(CleanupStatus.FAILED_QUALITY),
                str(CleanupStatus.RESIDUAL_SUBTITLE),
                str(CleanupStatus.DERIVATIVE_DELETED),
            ) and not force:
                continue  # do not retry failed/deleted results without --force
            if candidate.cleanup_status in (
                str(CleanupStatus.NOT_NEEDED),
                str(CleanupStatus.INELIGIBLE),
            ):
                continue
            selected.append(candidate)
            if len(selected) >= bound:
                break
        outcomes: list[CleanupOutcome] = []
        for candidate in selected:
            outcomes.append(await self.cleanup_clip(candidate.clip_id, force=force))
        return outcomes

    # -- human review / derivative lifecycle ------------------------------
    async def verify_derivative(
        self, clip_id: int, *, version: str = CLEANUP_VERSION
    ) -> dict[str, Any]:
        """Probe and validate one cleanup derivative; never modifies media."""

        clip = self.library.get_clip(int(clip_id))
        record = self.library.subtitle_cleanup(int(clip_id), version)
        checks: dict[str, bool] = {}
        details: dict[str, Any] = {}
        if clip is None or record is None:
            return {"ok": False, "checks": {"record": False}, "details": details}
        checks["status_succeeded"] = str(record.get("status")) == str(CleanupStatus.SUCCEEDED)
        derivative = self.library.safe_media_path(record.get("output_path"))
        checks["inside_library"] = derivative is not None
        checks["file_exists"] = bool(derivative is not None and derivative.exists())
        if derivative is not None and derivative.exists():
            try:
                info = await self._toolkit().probe(derivative)
                checks["ffprobe_readable"] = True
                checks["video_stream"] = bool(info.has_video)
                checks["duration_match"] = (
                    abs(float(info.duration) - float(clip.duration or info.duration))
                    <= self.config.duration_tolerance_seconds
                )
                checks["resolution_match"] = (
                    bool(info.width)
                    and bool(info.height)
                    and bool(clip.width)
                    and bool(clip.height)
                    and abs(int(info.width) - int(clip.width)) <= self.config.resolution_tolerance_pixels
                    and abs(int(info.height) - int(clip.height)) <= self.config.resolution_tolerance_pixels
                )
                details.update(
                    {
                        "derivative": str(derivative),
                        "duration": info.duration,
                        "width": info.width,
                        "height": info.height,
                        "has_audio": info.has_audio,
                    }
                )
            except Exception as exc:
                checks["ffprobe_readable"] = False
                details["error"] = str(exc)
        ok = bool(checks) and all(checks.values())
        return {"ok": ok, "checks": checks, "details": details}

    def review_cleanup(
        self,
        clip_id: int,
        *,
        status: str,
        note: str = "",
        failure_class: str = "",
        version: str = CLEANUP_VERSION,
    ) -> tuple[bool, str]:
        """Approve / reject / reset one cleanup derivative review."""

        try:
            review = CleanupReviewStatus(str(status))
        except ValueError:
            return False, f"unknown review status: {status}"
        record = self.library.subtitle_cleanup(int(clip_id), version)
        if record is None:
            return False, f"clip #{clip_id} has no cleanup record"
        if review is CleanupReviewStatus.APPROVED:
            if str(record.get("status")) != str(CleanupStatus.SUCCEEDED):
                return False, "only a succeeded cleanup can be approved"
            if not self._derivative_healthy(record):
                return False, "derivative is missing or unhealthy"
        if review is CleanupReviewStatus.REJECTED:
            if not failure_class:
                return False, "reject requires --review-failure-class"
            if failure_class not in REVIEW_FAILURE_CLASSES:
                return False, (
                    f"unknown failure class: {failure_class}; expected one of "
                    + ", ".join(REVIEW_FAILURE_CLASSES)
                )
        self.library.set_subtitle_cleanup_review(
            int(clip_id),
            review_status=str(review),
            note=note,
            failure_class=failure_class if review is CleanupReviewStatus.REJECTED else None,
            version=version,
        )
        operation = {
            CleanupReviewStatus.APPROVED: "cleanup_approved",
            CleanupReviewStatus.REJECTED: "cleanup_rejected",
            CleanupReviewStatus.PENDING: "cleanup_review_reset",
        }[review]
        self.library.log_maintenance(
            operation,
            target_type="clip",
            target_id=int(clip_id),
            details={
                "clip_id": int(clip_id),
                "cleanup_version": version,
                "review_status": str(review),
                "review_note": note,
                "failure_class": failure_class,
            },
        )
        return True, f"clip #{clip_id} cleanup review = {review}"

    def delete_derivative(
        self, clip_id: int, *, note: str = "", version: str = CLEANUP_VERSION
    ) -> tuple[bool, str]:
        """Delete one rejected derivative; the original clip is never touched."""

        clip = self.library.get_clip(int(clip_id))
        record = self.library.subtitle_cleanup(int(clip_id), version)
        if clip is None or record is None:
            return False, f"clip #{clip_id} has no cleanup record"
        derivative = self.library.safe_media_path(record.get("output_path"))
        original = self.library.safe_media_path(clip.file_path)
        if derivative is None:
            self.library.mark_subtitle_cleanup_derivative_deleted(
                int(clip_id), note=note or "derivative already missing", version=version
            )
            return True, f"clip #{clip_id} derivative was already missing; record updated"
        if original is not None and derivative == original:
            return False, "refusing to delete the original clip"
        try:
            derivative.unlink(missing_ok=True)
        except OSError as exc:
            return False, f"could not delete derivative: {exc}"
        self.library.mark_subtitle_cleanup_derivative_deleted(
            int(clip_id), note=note or "derivative deleted by operator", version=version
        )
        self.library.log_maintenance(
            "cleanup_derivative_deleted",
            target_type="clip",
            target_id=int(clip_id),
            details={
                "clip_id": int(clip_id),
                "cleanup_version": version,
                "derivative": str(derivative),
                "original": str(original) if original else "",
                "note": note,
            },
        )
        return True, f"clip #{clip_id} derivative deleted; original preserved"

    # -- review pack -------------------------------------------------------
    @staticmethod
    def _relative(path: Path, base: Path) -> str:
        try:
            return os.path.relpath(path, base).replace("\\", "/")
        except ValueError:  # pragma: no cover - different drives on Windows
            return path.as_uri()

    def _annotate_overlay(
        self,
        frame: Path,
        dest: Path,
        masks: Sequence[CleanupMask],
    ) -> Path | None:
        try:
            from PIL import Image, ImageDraw

            with Image.open(frame) as image:
                canvas = image.convert("RGB")
                draw = ImageDraw.Draw(canvas)
                width, height = canvas.size
                for mask in masks:
                    x, y, w, h = mask.pixel_box(width, height)
                    draw.rectangle([x, y, x + w, y + h], outline=(255, 0, 0), width=3)
                dest.parent.mkdir(parents=True, exist_ok=True)
                canvas.save(dest, quality=90)
                return dest
        except Exception as exc:  # pragma: no cover - review artifact only
            LOGGER.debug("could not annotate cleanup overlay: %s", exc)
            return None

    async def _build_review_pack_for_clip(self, clip_id: int) -> Path | None:
        clip = self.library.get_clip(int(clip_id))
        record = self.library.subtitle_cleanup(int(clip_id), self.config.version)
        if clip is None or record is None or not self._derivative_healthy(record):
            return None
        derivative = self.library.safe_media_path(record.get("output_path"))
        original = self.library.safe_media_path(clip.file_path)
        if derivative is None or original is None:
            return None
        masks = [
            CleanupMask.model_validate(item)
            for item in (record.get("regions") or [])
            if isinstance(item, dict)
        ]
        ratios = [float(value) for value in (self.config.review_pack_ratios or [0.2, 0.5, 0.8])]
        duration = float(clip.duration or 0.0)
        stamps = [round(max(0.0, min(0.999, ratio)) * duration, 3) for ratio in ratios] if duration else [0.0]
        pack_dir = self.config.reports_dir / "subtitle_cleanup" / f"clip_{int(clip_id)}_{self.config.version}"
        frames_dir = pack_dir / "frames"
        original_frames = await self._toolkit().extract_frames(
            original, stamps, frames_dir, "original", size=self.settings.analysis.preview_max_width
        )
        cleaned_frames = await self._toolkit().extract_frames(
            derivative, stamps, frames_dir, "cleaned", size=self.settings.analysis.preview_max_width
        )
        overlays: list[Path | None] = []
        for index, frame in enumerate(original_frames):
            overlays.append(
                self._annotate_overlay(
                    frame, frames_dir / f"overlay_{index:03d}.jpg", masks
                )
            )
        before = record.get("before_metrics") or {}
        after = record.get("after_metrics") or {}
        quality = record.get("quality") or {}
        reduction = record.get("reduction") or {}
        masked_area = round(
            sum(
                max(0.0, float(item.get("x2", 0)) - float(item.get("x1", 0)))
                * max(0.0, float(item.get("y2", 0)) - float(item.get("y1", 0)))
                for item in (record.get("regions") or [])
            ),
            5,
        )
        cleanliness_before = float(before.get("cleanliness_score") or 0.0)
        cleanliness_after = float(after.get("cleanliness_score") or 0.0)
        lines = [
            "<!doctype html>",
            "<html lang='zh-CN'><head><meta charset='utf-8'>",
            f"<title>字幕清理复核 clip #{int(clip_id)}</title>",
            "<style>body{font-family:sans-serif;margin:24px;max-width:1200px}"
            "table{border-collapse:collapse;margin:12px 0}td,th{border:1px solid #ccc;padding:6px}"
            "img{max-width:360px;margin:4px;border:1px solid #ddd}"
            ".muted{color:#666}video{max-width:480px;margin:6px}</style></head><body>",
            f"<h1>字幕清理复核：clip #{int(clip_id)}</h1>",
            f"<p class='muted'>版本 {html.escape(str(record.get('version')))} | "
            f"引擎 {html.escape(str(record.get('engine')))} | "
            f"清理状态 {html.escape(str(record.get('status')))} | "
            f"复核状态 {html.escape(str(record.get('review_status')))}</p>",
            "<h2>原片 / 清理片</h2>",
            f"<video controls preload='none' src='{html.escape(self._relative(original, pack_dir))}'></video>",
            f"<video controls preload='none' src='{html.escape(self._relative(derivative, pack_dir))}'></video>",
            "<h2>匹配时间点 before / after / overlay</h2>",
        ]
        for index, stamp in enumerate(stamps):
            original_frame = original_frames[index] if index < len(original_frames) else None
            cleaned_frame = cleaned_frames[index] if index < len(cleaned_frames) else None
            overlay = overlays[index] if index < len(overlays) else None
            lines.append(f"<h3>{stamp:.3f}s</h3><div>")
            if original_frame is not None:
                lines.append(
                    f"<img src='{html.escape(self._relative(original_frame, pack_dir))}' alt='original frame'>"
                )
            if cleaned_frame is not None:
                lines.append(
                    f"<img src='{html.escape(self._relative(cleaned_frame, pack_dir))}' alt='cleaned frame'>"
                )
            if overlay is not None:
                lines.append(
                    f"<img src='{html.escape(self._relative(overlay, pack_dir))}' alt='cleanup region overlay'>"
                )
            lines.append("</div>")
        lines.extend(
            [
                "<h2>清理指标</h2>",
                "<table><tr><th>指标</th><th>值</th></tr>",
                f"<tr><td>before cleanliness</td><td>{cleanliness_before:.3f}</td></tr>",
                f"<tr><td>after cleanliness</td><td>{cleanliness_after:.3f}</td></tr>",
                f"<tr><td>cleanliness delta</td><td>{cleanliness_after - cleanliness_before:+.3f}</td></tr>",
                f"<tr><td>before region count</td><td>{html.escape(str(before.get('max_text_regions')))}</td></tr>",
                f"<tr><td>after region count</td><td>{html.escape(str(after.get('max_text_regions')))}</td></tr>",
                f"<tr><td>masked area ratio</td><td>{masked_area:.5f}</td></tr>",
                f"<tr><td>outside-mask mean diff</td><td>{html.escape(str(quality.get('outside_mean_diff_max')))}</td></tr>",
                f"<tr><td>duration delta</td><td>{html.escape(str((quality.get('duration_after') or 0) - (quality.get('duration_before') or 0)))}</td></tr>",
                f"<tr><td>processing seconds</td><td>{(int(record.get('processing_ms') or 0) / 1000.0):.2f}</td></tr>",
                f"<tr><td>quality guard</td><td>{html.escape(str(quality.get('passed')))}</td></tr>",
                f"<tr><td>OCR reduction</td><td>{html.escape(str(reduction.get('success')))}</td></tr>",
                "</table>",
                "<h2>人工复核</h2>",
                f"<p>状态：<b>{html.escape(str(record.get('review_status')))}</b></p>",
                f"<p>备注：{html.escape(str(record.get('review_note') or '-'))}</p>",
                f"<p>失败分类：{html.escape(str(record.get('review_failure_class') or '-'))}</p>",
                "</body></html>",
            ]
        )
        pack_dir.mkdir(parents=True, exist_ok=True)
        index_path = pack_dir / "index.html"
        index_path.write_text("\n".join(lines), encoding="utf-8")
        self.library.log_maintenance(
            "cleanup_review_pack",
            target_type="clip",
            target_id=int(clip_id),
            details={"clip_id": int(clip_id), "pack": str(index_path)},
        )
        return index_path

    async def build_review_pack(
        self,
        *,
        clip_ids: Sequence[int] | None = None,
    ) -> list[Path]:
        """Generate review packs for successful healthy derivatives."""

        if clip_ids:
            targets = [int(value) for value in clip_ids]
        else:
            targets = [
                int(record["clip_id"])
                for record in self.library.list_subtitle_cleanups(limit=100000)
                if str(record.get("status")) == str(CleanupStatus.SUCCEEDED)
            ]
        packs: list[Path] = []
        for clip_id in targets:
            try:
                pack = await self._build_review_pack_for_clip(int(clip_id))
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.warning("review pack failed for clip %s: %s", clip_id, exc)
                pack = None
            if pack is not None:
                packs.append(pack)
        return packs

    # -- report ------------------------------------------------------------
    def report(self) -> dict[str, Any]:
        """Production status distribution plus per-class success metrics."""

        records = self.library.list_subtitle_cleanups(limit=100000)
        candidates = self.candidate_scan()
        counts = {
            "total_library_clips": int(self.library.count_clips()),
            "not_needed": 0,
            "ineligible": 0,
            "eligible_unprocessed": sum(
                1 for item in candidates if item.eligibility == "eligible_unprocessed"
            ),
            "succeeded_pending_review": 0,
            "approved": 0,
            "rejected": 0,
            "failed_processing": 0,
            "failed_quality": 0,
            "residual_subtitle": 0,
            "missing_derivative": 0,
        }
        successes: list[dict[str, Any]] = []
        per_class: dict[str, list[float]] = {}
        for record in records:
            status = str(record.get("status") or "")
            review = str(record.get("review_status") or "pending")
            if status == str(CleanupStatus.NOT_NEEDED):
                counts["not_needed"] += 1
            elif status == str(CleanupStatus.INELIGIBLE):
                counts["ineligible"] += 1
            elif status == str(CleanupStatus.SUCCEEDED):
                if not self._derivative_healthy(record):
                    counts["missing_derivative"] += 1
                if review == str(CleanupReviewStatus.APPROVED):
                    counts["approved"] += 1
                elif review == str(CleanupReviewStatus.REJECTED):
                    counts["rejected"] += 1
                else:
                    counts["succeeded_pending_review"] += 1
                before = record.get("before_metrics") or {}
                after = record.get("after_metrics") or {}
                quality = record.get("quality") or {}
                regions = record.get("regions") or []
                cleanliness_before = float(before.get("cleanliness_score") or 0.0)
                cleanliness_after = float(after.get("cleanliness_score") or 0.0)
                masked_area = round(
                    sum(
                        max(0.0, float(item.get("x2", 0)) - float(item.get("x1", 0)))
                        * max(0.0, float(item.get("y2", 0)) - float(item.get("y1", 0)))
                        for item in regions
                    ),
                    5,
                )
                row = {
                    "clip_id": record.get("clip_id"),
                    "subtitle_class": str(before.get("classification") or "unknown"),
                    "before_cleanliness": cleanliness_before,
                    "after_cleanliness": cleanliness_after,
                    "cleanliness_delta": round(cleanliness_after - cleanliness_before, 3),
                    "before_regions": before.get("max_text_regions"),
                    "after_regions": after.get("max_text_regions"),
                    "masked_area_ratio": masked_area,
                    "outside_mask_mean_diff": quality.get("outside_mean_diff_max"),
                    "duration_delta": round(
                        float(quality.get("duration_after") or 0)
                        - float(quality.get("duration_before") or 0),
                        3,
                    ),
                    "processing_seconds": round(int(record.get("processing_ms") or 0) / 1000.0, 3),
                    "engine": record.get("engine"),
                    "review_status": review,
                    "output_path": record.get("output_path"),
                    "updated_at": record.get("updated_at"),
                }
                successes.append(row)
                per_class.setdefault(row["subtitle_class"], []).append(
                    row["cleanliness_delta"]
                )
            elif status == str(CleanupStatus.FAILED_PROCESSING):
                counts["failed_processing"] += 1
            elif status == str(CleanupStatus.FAILED_QUALITY):
                counts["failed_quality"] += 1
            elif status == str(CleanupStatus.RESIDUAL_SUBTITLE):
                counts["residual_subtitle"] += 1
            elif status == str(CleanupStatus.DERIVATIVE_DELETED):
                counts["rejected"] += 1
        class_metrics = [
            {
                "subtitle_class": klass,
                "count": len(values),
                "average_cleanliness_delta": round(sum(values) / len(values), 3),
            }
            for klass, values in sorted(per_class.items())
        ]
        return {
            "total_records": len(records),
            "counts": counts,
            "status_counts": {
                str(record.get("status")): sum(
                    1 for item in records if str(item.get("status")) == str(record.get("status"))
                )
                for record in records
            },
            "successes": successes,
            "class_metrics": class_metrics,
        }
