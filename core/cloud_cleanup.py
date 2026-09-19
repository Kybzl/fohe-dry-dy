"""Volcano Engine VOD refined subtitle-erase orchestration (Milestone 9.8).

This is a separate, explicit cleanup version:

* ``subtitle_cleanup_v1`` remains local RapidOCR + FFmpeg delogo
* ``subtitle_cleanup_v2_volcengine`` uploads to VOD and runs the official
  refined subtitle erase task

Cloud cleanup is opt-in, paid and never used to remove watermarks.  A clip with
a disqualifying moving/floating text watermark is rejected locally and is never
uploaded.
"""

from __future__ import annotations

import logging
import shutil
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from core.config import AppSettings
from core.models import ClipRecord, ReviewStatus, SubtitleType
from core.subtitle_cleanup import (
    SubtitleCleanupService,
    build_tracks,
    evaluate_evidence_reduction,
    evaluate_quality_guard,
    plan_masks,
    sample_timestamps,
    stable_tracks,
)
from core.subtitle_cleanup_models import (
    CleanupMask,
    CleanupReviewStatus,
    CleanupStatus,
    SubtitleCleanupConfig,
)
from core.subtitle_models import ANALYSIS_VERSION, SubtitleAnalysisResult
from media.volcengine_vod import (
    SdkVolcengineVodClient,
    VolcengineApiError,
    VolcengineCleanupError,
    VolcengineExecution,
    VolcengineNotConfiguredError,
)
from storage.library import MaterialLibrary

LOGGER = logging.getLogger(__name__)

CLOUD_CLEANUP_VERSION = "subtitle_cleanup_v2_volcengine"
CLOUD_CLEANUP_ENGINE = "volcengine_refined_subtitle_erase"
CLOUD_PROVIDER = "volcengine"

#: classes that must never be sent to cloud subtitle erase
CLOUD_INELIGIBLE_CLASSES = frozenset(
    {
        str(SubtitleType.MULTI_REGION),
        str(SubtitleType.COLORED_BLOCK),
        str(SubtitleType.LARGE_CENTER_TEXT),
        str(SubtitleType.PROMOTIONAL_OVERLAY),
        str(SubtitleType.DENSE_TEXT),
        str(SubtitleType.COMPLEX),
        str(SubtitleType.UNKNOWN),
    }
)


def detect_solid_caption_backing(
    frame_paths: Sequence[Path],
    masks: Sequence[CleanupMask],
    *,
    config: SubtitleCleanupConfig,
) -> tuple[bool, dict[str, Any]]:
    """Detect persistent solid caption plates before any paid submission."""

    metrics: dict[str, Any] = {
        "checked_frames": 0,
        "checked_regions": 0,
        "blocked_regions": 0,
        "persistence": 0.0,
        "dominant_ratio_max": 0.0,
        "ring_ratio_min": 1.0,
    }
    if not frame_paths or not masks:
        return False, metrics
    try:
        from PIL import Image
    except Exception:
        return False, metrics

    blocked_frames = 0
    for frame_path in frame_paths:
        frame_blocked = False
        try:
            with Image.open(frame_path) as opened:
                image = opened.convert("RGB")
                width, height = image.size
                for mask in masks:
                    x, y, box_width, box_height = mask.pixel_box(width, height)
                    if box_width < 4 or box_height < 4:
                        continue
                    metrics["checked_regions"] += 1
                    crop = image.crop((x, y, x + box_width, y + box_height))
                    scale = min(1.0, 240.0 / max(crop.size))
                    if scale < 1.0:
                        crop = crop.resize(
                            (
                                max(1, round(crop.width * scale)),
                                max(1, round(crop.height * scale)),
                            )
                        )

                    def colour_key(pixel: tuple[int, int, int]) -> tuple[int, int, int]:
                        return tuple(channel // 16 for channel in pixel)

                    counts = Counter(colour_key(pixel) for pixel in crop.getdata())
                    if not counts:
                        continue
                    dominant, count = counts.most_common(1)[0]
                    dominant_ratio = count / max(1, crop.width * crop.height)
                    pad_x = max(8, round(box_width * 0.08))
                    pad_y = max(8, round(box_height * 0.20))
                    outer = (
                        max(0, x - pad_x),
                        max(0, y - pad_y),
                        min(width, x + box_width + pad_x),
                        min(height, y + box_height + pad_y),
                    )
                    ring_image = image.crop(outer)
                    ring_pixels = list(ring_image.getdata())
                    inner_left = x - outer[0]
                    inner_top = y - outer[1]
                    inner_right = inner_left + box_width
                    inner_bottom = inner_top + box_height
                    ring_matches = 0
                    ring_total = 0
                    ring_width, _ring_height = ring_image.size
                    for index, pixel in enumerate(ring_pixels):
                        px = index % ring_width
                        py = index // ring_width
                        if inner_left <= px < inner_right and inner_top <= py < inner_bottom:
                            continue
                        ring_total += 1
                        if colour_key(pixel) == dominant:
                            ring_matches += 1
                    ring_ratio = ring_matches / max(1, ring_total)
                    metrics["dominant_ratio_max"] = max(
                        float(metrics["dominant_ratio_max"]), dominant_ratio
                    )
                    metrics["ring_ratio_min"] = min(
                        float(metrics["ring_ratio_min"]), ring_ratio
                    )
                    if (
                        dominant_ratio >= config.backing_block_dominant_ratio_min
                        and ring_ratio <= config.backing_block_ring_ratio_max
                    ):
                        metrics["blocked_regions"] += 1
                        frame_blocked = True
        except Exception:
            LOGGER.debug(
                "caption backing preflight could not read %s", frame_path, exc_info=True
            )
        metrics["checked_frames"] += 1
        if frame_blocked:
            blocked_frames += 1

    persistence = blocked_frames / max(1, int(metrics["checked_frames"]))
    metrics["persistence"] = round(persistence, 4)
    metrics["dominant_ratio_max"] = round(float(metrics["dominant_ratio_max"]), 4)
    metrics["ring_ratio_min"] = round(float(metrics["ring_ratio_min"]), 4)
    return persistence >= config.backing_block_min_persistence, metrics


@dataclass
class CloudCleanupOutcome:
    clip_id: int
    status: CleanupStatus
    reason: str = ""
    run_id: str = ""
    output_path: Path | None = None
    before: SubtitleAnalysisResult | None = None
    after: SubtitleAnalysisResult | None = None
    masks: list[CleanupMask] = field(default_factory=list)
    quality: dict[str, Any] = field(default_factory=dict)
    reduction: dict[str, Any] = field(default_factory=dict)
    cloud_status: str = ""
    error_class: str = ""
    elapsed_ms: int = 0
    reused: bool = False

    @property
    def ok(self) -> bool:
        return self.status is CleanupStatus.SUCCEEDED

    def lines(self) -> list[str]:
        lines = [
            f"[{self.status.value}] 片段 #{self.clip_id} Volcano 字幕擦除"
        ]
        if self.reason:
            lines.append(f"  原因: {self.reason}")
        if self.run_id:
            lines.append(f"  RunId: {self.run_id}")
        if self.before is not None:
            lines.append(
                f"  before: {self.before.classification} "
                f"clean={self.before.cleanliness_score:.3f} "
                f"regions={self.before.max_text_regions}"
            )
        if self.after is not None:
            lines.append(
                f"  after: {self.after.classification} "
                f"clean={self.after.cleanliness_score:.3f} "
                f"regions={self.after.max_text_regions}"
            )
        if self.cloud_status:
            lines.append(f"  cloud status: {self.cloud_status}")
        if self.output_path is not None:
            lines.append(f"  派生: {self.output_path}")
        if self.error_class:
            lines.append(f"  cloud error: {self.error_class}")
        lines.append(f"  版本: {CLOUD_CLEANUP_VERSION} | 耗时: {self.elapsed_ms}ms")
        return lines


class CloudCleanupService:
    """Explicit Volcano VOD subtitle-erase orchestration for one clip."""

    def __init__(
        self,
        library: MaterialLibrary,
        settings: AppSettings,
        *,
        client: Any | None = None,
        local_service: SubtitleCleanupService | None = None,
    ) -> None:
        self.library = library
        self.settings = settings
        self.client = client or SdkVolcengineVodClient(settings)
        self.local_service = local_service or SubtitleCleanupService(
            library, settings
        )
        self._temp_root = settings.paths.cache_dir / "cloud_cleanup"

    # -- paths / persistence ----------------------------------------------
    @staticmethod
    def derivative_path(clip: ClipRecord) -> Path:
        original = Path(clip.file_path)
        parent = original.parent
        category_dir = parent.parent if parent.name.lower() == "clips" else parent
        return (
            category_dir
            / "clean"
            / f"{original.stem}__{CLOUD_CLEANUP_VERSION}.mp4"
        )

    def _existing(self, clip_id: int) -> dict[str, Any] | None:
        return self.library.subtitle_cleanup(clip_id, CLOUD_CLEANUP_VERSION)

    def paid_tasks_today(self) -> int:
        """Count audited paid submissions in the current UTC day."""

        today = datetime.now(timezone.utc).date()
        return sum(
            1
            for item in self.library.list_maintenance_log(limit=10000)
            if item.get("operation") == "cloud_cleanup_submitted"
            and datetime.fromisoformat(str(item.get("created_at"))).date() == today
        )

    async def preflight_clip(self, clip_id: int) -> CloudCleanupOutcome:
        """Run every content-safety gate without upload or paid API calls."""

        started = time.perf_counter()
        clip = self.library.get_clip(int(clip_id))
        if clip is None or clip.id is None:
            return CloudCleanupOutcome(
                clip_id=int(clip_id),
                status=CleanupStatus.FAILED_PROCESSING,
                reason="clip_not_found",
            )
        if str(clip.review_status) == str(ReviewStatus.REJECTED):
            return CloudCleanupOutcome(
                clip_id=int(clip.id),
                status=CleanupStatus.INELIGIBLE,
                reason="clip_human_rejected",
            )
        source = self.library.safe_media_path(clip.file_path)
        if source is None or not source.exists():
            return CloudCleanupOutcome(
                clip_id=int(clip.id),
                status=CleanupStatus.FAILED_PROCESSING,
                reason="source_media_missing_or_outside_library",
            )
        config = self.settings.subtitle_cleanup
        temp_dir = self._temp_root / f"clip_{int(clip.id)}_preflight_{uuid.uuid4().hex[:8]}"
        try:
            toolkit = self.local_service._toolkit()
            analyzer = self.local_service._analyzer()
            info = await toolkit.probe(source)
            duration = float(clip.duration or info.duration or 0.0)
            timestamps = sample_timestamps(duration, config=config)
            frames = await self.local_service._extract_frames(
                source, timestamps, temp_dir / "before", "before"
            )
            samples, metrics, errors = self.local_service._ocr_frames(
                list(zip(timestamps[: len(frames)], frames)),
                analyzer=analyzer,
                config=config,
            )
            before = analyzer.build_result(metrics, errors=errors, frame_budget=len(timestamps))
            if before.is_unavailable:
                return CloudCleanupOutcome(
                    clip_id=int(clip.id),
                    status=CleanupStatus.FAILED_PROCESSING,
                    reason=f"local_subtitle_analysis_unavailable:{before.unavailable_reason}",
                    before=before,
                )
            classification = str(before.classification)
            if classification in (str(SubtitleType.NONE), str(SubtitleType.WATERMARK_ONLY)):
                return CloudCleanupOutcome(
                    clip_id=int(clip.id),
                    status=CleanupStatus.NOT_NEEDED,
                    reason=f"classification={classification}",
                    before=before,
                )
            if classification in CLOUD_INELIGIBLE_CLASSES:
                return CloudCleanupOutcome(
                    clip_id=int(clip.id),
                    status=CleanupStatus.INELIGIBLE,
                    reason=f"classification_not_eligible:{classification}",
                    before=before,
                )
            tracks = build_tracks(samples, config=config, total_samples=len(metrics))
            watermark_reason = self._watermark_reason(tracks)
            if watermark_reason:
                return CloudCleanupOutcome(
                    clip_id=int(clip.id),
                    status=CleanupStatus.INELIGIBLE,
                    reason=f"watermark_disqualified:{watermark_reason}",
                    before=before,
                )
            masks, guard_reason = plan_masks(
                stable_tracks(tracks, config=config),
                config=config,
                width=int(info.width),
                height=int(info.height),
            )
            if not masks:
                return CloudCleanupOutcome(
                    clip_id=int(clip.id),
                    status=CleanupStatus.INELIGIBLE,
                    reason=guard_reason or "no_stable_subtitle_track",
                    before=before,
                )
            has_backing, backing = detect_solid_caption_backing(
                frames, masks, config=config
            )
            if has_backing:
                reason = (
                    "solid_caption_backing_detected:"
                    f"persistence={backing['persistence']:.3f},"
                    f"dominant={backing['dominant_ratio_max']:.3f},"
                    f"ring={backing['ring_ratio_min']:.3f}"
                )
                return CloudCleanupOutcome(
                    clip_id=int(clip.id),
                    status=CleanupStatus.INELIGIBLE,
                    reason=reason,
                    before=before,
                    masks=list(masks),
                )
            return CloudCleanupOutcome(
                clip_id=int(clip.id),
                status=CleanupStatus.PENDING,
                reason="eligible_for_cloud_submission",
                before=before,
                masks=list(masks),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:
            LOGGER.exception("cloud cleanup preflight failed for clip %s", clip.id)
            return CloudCleanupOutcome(
                clip_id=int(clip.id),
                status=CleanupStatus.FAILED_PROCESSING,
                reason=f"preflight_error:{exc}",
                error_class=type(exc).__name__,
            )
        finally:
            self.local_service._cleanup_dir(temp_dir)

    def _outcome_from_record(self, clip_id: int, record: dict[str, Any]) -> CloudCleanupOutcome:
        try:
            status = CleanupStatus(str(record.get("status")))
        except ValueError:
            status = CleanupStatus.PENDING
        before = after = None
        if record.get("before_metrics"):
            try:
                before = SubtitleAnalysisResult.model_validate(record["before_metrics"])
            except Exception:
                before = None
        if record.get("after_metrics"):
            try:
                after = SubtitleAnalysisResult.model_validate(record["after_metrics"])
            except Exception:
                after = None
        return CloudCleanupOutcome(
            clip_id=clip_id,
            status=status,
            reason=str(record.get("skip_reason") or ""),
            run_id=str(record.get("run_id") or ""),
            output_path=Path(record["output_path"]) if record.get("output_path") else None,
            before=before,
            after=after,
            cloud_status=str(record.get("cloud_status") or ""),
            error_class=str(record.get("cloud_error_class") or ""),
            reused=True,
        )

    def _persist(
        self,
        clip_id: int,
        *,
        status: CleanupStatus,
        reason: str = "",
        run_id: str = "",
        output_path: Path | None = None,
        input_kind: str = "",
        input_vid: str = "",
        submitted_at: str | None = None,
        completed_at: str | None = None,
        cloud_status: str = "",
        cloud_error_class: str = "",
        cloud_output_vid: str = "",
        cloud_output_file_name: str = "",
        before: SubtitleAnalysisResult | None = None,
        after: SubtitleAnalysisResult | None = None,
        masks: Sequence[CleanupMask] = (),
        quality: dict[str, Any] | None = None,
        reduction: dict[str, Any] | None = None,
        processing_ms: int = 0,
        review_status: str = str(CleanupReviewStatus.PENDING),
        error: str = "",
    ) -> int:
        return self.library.save_subtitle_cleanup(
            clip_id=clip_id,
            version=CLOUD_CLEANUP_VERSION,
            status=status,
            engine=CLOUD_CLEANUP_ENGINE,
            source_analysis_version=ANALYSIS_VERSION,
            output_path=output_path,
            eligible=bool(masks),
            skip_reason=reason,
            regions=[mask.model_dump(mode="json") for mask in masks],
            before_metrics=before.model_dump(mode="json") if before else None,
            after_metrics=after.model_dump(mode="json") if after else None,
            settings=self.settings.cloud_cleanup.volcengine.model_dump(mode="json"),
            quality=quality,
            reduction=reduction,
            processing_ms=processing_ms,
            review_status=review_status,
            provider=CLOUD_PROVIDER,
            input_kind=input_kind,
            input_vid=input_vid,
            run_id=run_id,
            submitted_at=submitted_at,
            completed_at=completed_at,
            cloud_status=cloud_status,
            cloud_error_class=cloud_error_class,
            cloud_output_vid=cloud_output_vid,
            # never persist a signed playback URL
            cloud_output_file_name=(
                "" if str(cloud_output_file_name).startswith("http") else cloud_output_file_name
            ),
            error=error,
        )

    # -- local analysis ----------------------------------------------------
    def _watermark_reason(self, tracks: Sequence[Any]) -> str:
        config = self.settings.cloud_cleanup.volcengine
        for track in tracks:
            if track.persistence_ratio < config.watermark_min_persistence:
                continue
            if track.vertical_jitter > config.watermark_max_vertical_jitter:
                return f"moving_text_vertical_jitter:{track.vertical_jitter:.3f}"
            if (
                track.union_growth > config.watermark_max_union_growth
                and track.median_y1 < 0.5
            ):
                return f"floating_top_text_growth:{track.union_growth:.2f}"
        return ""

    @staticmethod
    def _ratio_locations(
        masks: Sequence[CleanupMask], *, margin: float
    ) -> list[dict[str, float]]:
        locations: list[dict[str, float]] = []
        for mask in masks:
            x1 = max(0.0, min(1.0, mask.x1 - margin))
            y1 = max(0.0, min(1.0, mask.y1 - margin))
            x2 = max(0.0, min(1.0, mask.x2 + margin))
            y2 = max(0.0, min(1.0, mask.y2 + margin))
            if x2 <= x1 or y2 <= y1:
                continue
            locations.append(
                {
                    "TopLeftX": round(x1, 5),
                    "TopLeftY": round(y1, 5),
                    "BottomRightX": round(x2, 5),
                    "BottomRightY": round(y2, 5),
                }
            )
        return locations

    async def cleanup_clip(self, clip_id: int, *, force: bool = False) -> CloudCleanupOutcome:
        started = time.perf_counter()
        clip = self.library.get_clip(int(clip_id))
        if clip is None or clip.id is None:
            return CloudCleanupOutcome(
                clip_id=int(clip_id),
                status=CleanupStatus.FAILED_PROCESSING,
                reason="clip_not_found",
            )
        existing = self._existing(int(clip.id))
        if existing is not None and not force:
            if self._is_recoverable(existing):
                return await self._resume_completed_output(clip, existing, started)
            outcome = self._outcome_from_record(int(clip.id), existing)
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return outcome
        if not self.settings.cloud_cleanup.enabled:
            self._persist(
                int(clip.id),
                status=CleanupStatus.INELIGIBLE,
                reason="cloud_cleanup_disabled",
            )
            return CloudCleanupOutcome(
                clip_id=int(clip.id),
                status=CleanupStatus.INELIGIBLE,
                reason="cloud_cleanup_disabled",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        # Clip-level human rejection is authoritative: never upload.
        if str(clip.review_status) == str(ReviewStatus.REJECTED):
            reason = "clip_human_rejected"
            self._persist(int(clip.id), status=CleanupStatus.INELIGIBLE, reason=reason)
            return CloudCleanupOutcome(
                clip_id=int(clip.id),
                status=CleanupStatus.INELIGIBLE,
                reason=reason,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        source = self.library.safe_media_path(clip.file_path)
        if source is None or not source.exists():
            return CloudCleanupOutcome(
                clip_id=int(clip.id),
                status=CleanupStatus.FAILED_PROCESSING,
                reason="source_media_missing_or_outside_library",
            )

        config: SubtitleCleanupConfig = self.settings.subtitle_cleanup
        temp_dir = self._temp_root / f"clip_{int(clip.id)}_{uuid.uuid4().hex[:8]}"
        previous_output = Path(existing["output_path"]) if existing and existing.get("output_path") else None
        try:
            toolkit = self.local_service._toolkit()
            analyzer = self.local_service._analyzer()
            info = await toolkit.probe(source)
            duration = float(clip.duration or info.duration or 0.0)
            timestamps = sample_timestamps(duration, config=config)
            before_frames = await self.local_service._extract_frames(
                source, timestamps, temp_dir / "before", "before"
            )
            samples, metrics, errors = self.local_service._ocr_frames(
                list(zip(timestamps[: len(before_frames)], before_frames)),
                analyzer=analyzer,
                config=config,
            )
            before = analyzer.build_result(metrics, errors=errors, frame_budget=len(timestamps))
            if before.is_unavailable:
                return self._record_failure(
                    clip,
                    CleanupStatus.FAILED_PROCESSING,
                    f"local_subtitle_analysis_unavailable: {before.unavailable_reason}",
                    started,
                )
            classification = str(before.classification)
            if classification in (str(SubtitleType.NONE), str(SubtitleType.WATERMARK_ONLY)):
                self._persist(
                    int(clip.id),
                    status=CleanupStatus.NOT_NEEDED,
                    reason=f"classification={classification}",
                    before=before,
                )
                return CloudCleanupOutcome(
                    clip_id=int(clip.id),
                    status=CleanupStatus.NOT_NEEDED,
                    reason=f"classification={classification}",
                    before=before,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
            if classification in CLOUD_INELIGIBLE_CLASSES:
                reason = f"classification_not_eligible:{classification}"
                self._persist(
                    int(clip.id),
                    status=CleanupStatus.INELIGIBLE,
                    reason=reason,
                    before=before,
                )
                return CloudCleanupOutcome(
                    clip_id=int(clip.id),
                    status=CleanupStatus.INELIGIBLE,
                    reason=reason,
                    before=before,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
            tracks = build_tracks(samples, config=config, total_samples=len(metrics))
            watermark_reason = self._watermark_reason(tracks)
            if watermark_reason:
                self._persist(
                    int(clip.id),
                    status=CleanupStatus.INELIGIBLE,
                    reason=f"watermark_disqualified:{watermark_reason}",
                    before=before,
                )
                return CloudCleanupOutcome(
                    clip_id=int(clip.id),
                    status=CleanupStatus.INELIGIBLE,
                    reason=f"watermark_disqualified:{watermark_reason}",
                    before=before,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
            stable = stable_tracks(tracks, config=config)
            masks, guard_reason = plan_masks(
                stable,
                config=config,
                width=int(info.width),
                height=int(info.height),
            )
            if not masks:
                reason = guard_reason or "no_stable_subtitle_track"
                self._persist(
                    int(clip.id),
                    status=CleanupStatus.INELIGIBLE,
                    reason=reason,
                    before=before,
                )
                return CloudCleanupOutcome(
                    clip_id=int(clip.id),
                    status=CleanupStatus.INELIGIBLE,
                    reason=reason,
                    before=before,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
            has_backing_block, backing_metrics = detect_solid_caption_backing(
                before_frames,
                masks,
                config=config,
            )
            if has_backing_block:
                reason = (
                    "solid_caption_backing_detected:"
                    f"persistence={backing_metrics['persistence']:.3f},"
                    f"dominant={backing_metrics['dominant_ratio_max']:.3f},"
                    f"ring={backing_metrics['ring_ratio_min']:.3f}"
                )
                self._persist(
                    int(clip.id),
                    status=CleanupStatus.INELIGIBLE,
                    reason=reason,
                    before=before,
                    masks=masks,
                )
                self.library.log_maintenance(
                    "cloud_cleanup_preflight_blocked",
                    target_type="clip",
                    target_id=int(clip.id),
                    details={"reason": reason, "metrics": backing_metrics},
                )
                return CloudCleanupOutcome(
                    clip_id=int(clip.id),
                    status=CleanupStatus.INELIGIBLE,
                    reason=reason,
                    before=before,
                    masks=list(masks),
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
            locations = self._ratio_locations(
                masks, margin=float(self.settings.cloud_cleanup.volcengine.locations_margin_ratio)
            )
            return await self._run_cloud(
                clip=clip,
                source=source,
                info=info,
                before=before,
                masks=masks,
                locations=locations,
                before_frames=before_frames,
                timestamps=timestamps,
                temp_dir=temp_dir,
                started=started,
                previous_output=previous_output,
            )
        except Exception as exc:
            LOGGER.exception("cloud cleanup failed for clip %s", clip.id)
            # A failure after submission (most commonly while downloading the
            # finished output) must not erase the cloud identifiers already
            # checkpointed for recovery.
            checkpoint = self._existing(int(clip.id)) or {}
            checkpoint_before = None
            checkpoint_masks: list[CleanupMask] = []
            try:
                if checkpoint.get("before_metrics"):
                    checkpoint_before = SubtitleAnalysisResult.model_validate(
                        checkpoint["before_metrics"]
                    )
                checkpoint_masks = [
                    CleanupMask.model_validate(item)
                    for item in (checkpoint.get("regions") or [])
                ]
            except Exception:
                LOGGER.warning(
                    "could not restore cleanup checkpoint context for clip %s",
                    clip.id,
                    exc_info=True,
                )
            return self._record_failure(
                clip,
                CleanupStatus.FAILED_PROCESSING,
                f"unexpected_error: {exc}",
                started,
                before=checkpoint_before,
                masks=checkpoint_masks,
                run_id=str(checkpoint.get("run_id") or ""),
                input_kind=str(checkpoint.get("input_kind") or ""),
                input_vid=str(checkpoint.get("input_vid") or ""),
                submitted_at=checkpoint.get("submitted_at"),
                completed_at=checkpoint.get("completed_at"),
                cloud_status=str(checkpoint.get("cloud_status") or ""),
                cloud_error_class=type(exc).__name__,
                cloud_output_vid=str(checkpoint.get("cloud_output_vid") or ""),
                cloud_output_file_name=str(
                    checkpoint.get("cloud_output_file_name") or ""
                ),
            )
        finally:
            self.local_service._cleanup_dir(temp_dir)

    @staticmethod
    def _is_recoverable(record: dict[str, Any]) -> bool:
        """Return whether a paid cloud result can be resumed locally."""

        return bool(
            str(record.get("cloud_status") or "").lower() == "success"
            and record.get("cloud_output_vid")
            and record.get("before_metrics")
            and record.get("regions")
            and not record.get("output_path")
        )

    async def _resume_completed_output(
        self,
        clip: ClipRecord,
        record: dict[str, Any],
        started: float,
    ) -> CloudCleanupOutcome:
        """Download and validate an already completed cloud task."""

        source = self.library.safe_media_path(clip.file_path)
        if source is None or not source.exists():
            return self._outcome_from_record(int(clip.id or 0), record)
        temp_dir = (
            self._temp_root
            / f"clip_{int(clip.id or 0)}_resume_{uuid.uuid4().hex[:8]}"
        )
        run_id = str(record.get("run_id") or "")
        input_kind = str(record.get("input_kind") or "")
        input_vid = str(record.get("input_vid") or "")
        output_vid = str(record.get("cloud_output_vid") or "")
        output_file_name = str(record.get("cloud_output_file_name") or "")
        submitted_at = record.get("submitted_at")
        completed_at = record.get("completed_at")
        try:
            before = SubtitleAnalysisResult.model_validate(record["before_metrics"])
            masks = [CleanupMask.model_validate(item) for item in record["regions"]]
            candidate = temp_dir / "cloud_result.mp4"
            await self.client.download_output(
                output_vid,
                candidate,
                output_file_name=output_file_name,
            )
            # Post-cloud policy: a result that passed the local paid preflight
            # is exported directly. Only verify that the downloaded media is
            # structurally readable; do not repeat OCR or visual scoring.
            await self.local_service._toolkit().probe(candidate)
            output_path = self.derivative_path(clip)
            SubtitleCleanupService._atomic_publish(candidate, output_path)
            processing_ms = int((time.perf_counter() - started) * 1000)
            self._persist(
                int(clip.id or 0),
                status=CleanupStatus.SUCCEEDED,
                output_path=output_path,
                before=before,
                masks=masks,
                processing_ms=processing_ms,
                review_status=str(CleanupReviewStatus.APPROVED),
                run_id=run_id,
                input_kind=input_kind,
                input_vid=input_vid,
                submitted_at=submitted_at,
                completed_at=completed_at,
                cloud_status="Success",
                cloud_output_vid=output_vid,
                cloud_output_file_name=output_file_name,
            )
            self.library.log_maintenance(
                "cloud_cleanup_resumed",
                target_type="clip",
                target_id=int(clip.id or 0),
                details={
                    "clip_id": int(clip.id or 0),
                    "cleanup_version": CLOUD_CLEANUP_VERSION,
                    "run_id": run_id,
                    "input_vid": input_vid,
                    "output_vid": output_vid,
                    "output_path": str(output_path),
                },
            )
            return CloudCleanupOutcome(
                clip_id=int(clip.id or 0),
                status=CleanupStatus.SUCCEEDED,
                reason="resumed_completed_cloud_output",
                run_id=run_id,
                output_path=output_path,
                before=before,
                masks=list(masks),
                cloud_status="Success",
                elapsed_ms=processing_ms,
            )
        except Exception as exc:
            LOGGER.exception("cloud cleanup resume failed for clip %s", clip.id)
            return self._record_failure(
                clip,
                CleanupStatus.FAILED_PROCESSING,
                f"resume_error: {exc}",
                started,
                run_id=run_id,
                input_kind=input_kind,
                input_vid=input_vid,
                submitted_at=submitted_at,
                completed_at=completed_at,
                cloud_status="Success",
                cloud_error_class=type(exc).__name__,
                cloud_output_vid=output_vid,
                cloud_output_file_name=output_file_name,
            )
        finally:
            self.local_service._cleanup_dir(temp_dir)

    async def _run_cloud(
        self,
        *,
        clip: ClipRecord,
        source: Path,
        info: Any,
        before: SubtitleAnalysisResult,
        masks: Sequence[CleanupMask],
        locations: Sequence[dict[str, float]],
        before_frames: Sequence[Path],
        timestamps: Sequence[float],
        temp_dir: Path,
        started: float,
        previous_output: Path | None,
    ) -> CloudCleanupOutcome:
        daily_limit = int(self.settings.cloud_cleanup.max_paid_tasks_per_day)
        used_today = self.paid_tasks_today()
        if used_today >= daily_limit:
            return self._record_failure(
                clip,
                CleanupStatus.INELIGIBLE,
                f"daily_paid_task_limit_reached:{used_today}/{daily_limit}",
                started,
                before=before,
                masks=masks,
            )
        readiness = await self.client.readiness()
        if not readiness.ready:
            return self._record_failure(
                clip,
                CleanupStatus.FAILED_PROCESSING,
                readiness.detail or "volcengine_not_ready",
                started,
                before=before,
                masks=masks,
            )
        uploaded = await self.client.upload_local(source)
        vid = str(uploaded.get("vid") or "")
        input_kind = str(uploaded.get("input_kind") or "upload")
        submitted_at = datetime.now(timezone.utc).isoformat()
        run_id = await self.client.start_subtitle_erase(vid, locations=locations)
        self.library.log_maintenance(
            "cloud_cleanup_submitted",
            target_type="clip",
            target_id=int(clip.id),
            details={"clip_id": int(clip.id), "run_id": run_id, "input_vid": vid},
        )
        self._persist(
            int(clip.id),
            status=CleanupStatus.PENDING,
            run_id=run_id,
            input_kind=input_kind,
            input_vid=vid,
            submitted_at=submitted_at,
            cloud_status="processing",
            before=before,
            masks=masks,
        )
        execution: VolcengineExecution = await self.client.poll_execution(run_id)
        completed_at = datetime.now(timezone.utc).isoformat()
        if execution.status != "Success":
            return self._record_failure(
                clip,
                CleanupStatus.FAILED_PROCESSING,
                f"volcengine_execution_{execution.status or 'failed'}",
                started,
                before=before,
                masks=masks,
                run_id=run_id,
                input_kind=input_kind,
                input_vid=vid,
                submitted_at=submitted_at,
                completed_at=completed_at,
                cloud_status=execution.status,
                cloud_error_class=execution.error_class or "execution_failed",
            )
        # Persist the successful cloud result before attempting the local
        # download.  This makes a completed paid task recoverable without a
        # second upload or paid execution if network/download validation fails.
        self._persist(
            int(clip.id),
            status=CleanupStatus.PENDING,
            run_id=run_id,
            input_kind=input_kind,
            input_vid=vid,
            submitted_at=submitted_at,
            completed_at=completed_at,
            cloud_status=execution.status,
            cloud_output_vid=execution.output_vid,
            cloud_output_file_name=execution.output_file_name,
            before=before,
            masks=masks,
        )
        candidate = temp_dir / "cloud_result.mp4"
        await self.client.download_output(
            execution.output_vid,
            candidate,
            output_file_name=execution.output_file_name,
        )
        # The full content gate runs before the paid request. After Volcano
        # succeeds, only ensure the downloaded media is structurally readable;
        # skip post-processing frames, OCR and visual quality scoring.
        await self.local_service._toolkit().probe(candidate)
        output_path = self.derivative_path(clip)
        SubtitleCleanupService._atomic_publish(candidate, output_path)
        processing_ms = int((time.perf_counter() - started) * 1000)
        self._persist(
            int(clip.id),
            status=CleanupStatus.SUCCEEDED,
            output_path=output_path,
            before=before,
            masks=masks,
            processing_ms=processing_ms,
            review_status=str(CleanupReviewStatus.APPROVED),
            run_id=run_id,
            input_kind=input_kind,
            input_vid=vid,
            submitted_at=submitted_at,
            completed_at=completed_at,
            cloud_status=execution.status,
            cloud_output_vid=execution.output_vid,
            cloud_output_file_name=execution.output_file_name,
        )
        self.library.log_maintenance(
            "cloud_cleanup_succeeded",
            target_type="clip",
            target_id=int(clip.id),
            details={
                "clip_id": int(clip.id),
                "cleanup_version": CLOUD_CLEANUP_VERSION,
                "engine": CLOUD_CLEANUP_ENGINE,
                "provider": CLOUD_PROVIDER,
                "run_id": run_id,
                "output_path": str(output_path),
                "input_vid": vid,
                "output_vid": execution.output_vid,
                "processing_ms": processing_ms,
            },
        )
        return CloudCleanupOutcome(
            clip_id=int(clip.id),
            status=CleanupStatus.SUCCEEDED,
            run_id=run_id,
            output_path=output_path,
            before=before,
            masks=list(masks),
            cloud_status=execution.status,
            elapsed_ms=processing_ms,
        )

    def _record_failure(
        self,
        clip: ClipRecord,
        status: CleanupStatus,
        reason: str,
        started: float,
        *,
        before: SubtitleAnalysisResult | None = None,
        after: SubtitleAnalysisResult | None = None,
        masks: Sequence[CleanupMask] = (),
        quality: dict[str, Any] | None = None,
        reduction: dict[str, Any] | None = None,
        run_id: str = "",
        input_kind: str = "",
        input_vid: str = "",
        submitted_at: str | None = None,
        completed_at: str | None = None,
        cloud_status: str = "",
        cloud_error_class: str = "",
        cloud_output_vid: str = "",
        cloud_output_file_name: str = "",
    ) -> CloudCleanupOutcome:
        self._persist(
            int(clip.id or 0),
            status=status,
            reason=reason,
            before=before,
            after=after,
            masks=masks,
            quality=quality,
            reduction=reduction,
            run_id=run_id,
            input_kind=input_kind,
            input_vid=input_vid,
            submitted_at=submitted_at,
            completed_at=completed_at,
            cloud_status=cloud_status,
            cloud_error_class=cloud_error_class,
            cloud_output_vid=cloud_output_vid,
            cloud_output_file_name=cloud_output_file_name,
            error=reason,
        )
        if cloud_error_class or status is CleanupStatus.FAILED_PROCESSING:
            self.library.log_maintenance(
                "cloud_cleanup_failed",
                target_type="clip",
                target_id=int(clip.id or 0),
                details={
                    "clip_id": int(clip.id or 0),
                    "cleanup_version": CLOUD_CLEANUP_VERSION,
                    "status": str(status),
                    "reason": reason[:200],
                    "run_id": run_id,
                    "error_class": cloud_error_class,
                },
            )
        return CloudCleanupOutcome(
            clip_id=int(clip.id or 0),
            status=status,
            reason=reason,
            run_id=run_id,
            before=before,
            after=after,
            masks=list(masks),
            quality=quality or {},
            reduction=reduction or {},
            cloud_status=cloud_status,
            error_class=cloud_error_class,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    def readiness(self) -> Any:
        """Synchronous readiness wrapper for CLI usage."""
        import asyncio

        return asyncio.run(self.client.readiness())

    async def adopt_downloaded_output(
        self,
        clip_id: int,
        candidate: Path,
        *,
        run_id: str = "",
        input_vid: str = "",
        output_vid: str = "",
        output_file_name: str = "",
    ) -> CloudCleanupOutcome:
        """Validate and register a manually recovered completed VOD output."""

        started = time.perf_counter()
        clip = self.library.get_clip(int(clip_id))
        if clip is None or clip.id is None:
            return CloudCleanupOutcome(clip_id=clip_id, status=CleanupStatus.FAILED_PROCESSING, reason="clip_not_found")
        existing = self._existing(int(clip_id)) or {}
        run_id = str(run_id or existing.get("run_id") or "")
        input_vid = str(input_vid or existing.get("input_vid") or "")
        output_vid = str(output_vid or existing.get("cloud_output_vid") or "")
        output_file_name = str(
            output_file_name or existing.get("cloud_output_file_name") or ""
        )
        source = self.library.safe_media_path(clip.file_path)
        candidate = Path(candidate).resolve()
        if source is None or not source.exists() or not candidate.exists():
            return self._record_failure(clip, CleanupStatus.FAILED_PROCESSING, "adopt_source_or_candidate_missing", started)
        temp_dir = self._temp_root / f"clip_{clip_id}_adopt_{uuid.uuid4().hex[:8]}"
        try:
            toolkit = self.local_service._toolkit()
            analyzer = self.local_service._analyzer()
            info = await toolkit.probe(source)
            after_info = await toolkit.probe(candidate)
            timestamps = sample_timestamps(float(clip.duration or info.duration or 0.0), config=self.local_service.config)
            before_frames = await self.local_service._extract_frames(source, timestamps, temp_dir / "before", "before")
            samples, metrics, errors = self.local_service._ocr_frames(
                list(zip(timestamps[: len(before_frames)], before_frames)), analyzer=analyzer, config=self.local_service.config
            )
            before = analyzer.build_result(metrics, errors=errors, frame_budget=len(timestamps))
            tracks = build_tracks(samples, config=self.local_service.config, total_samples=len(metrics))
            masks, reason = plan_masks(stable_tracks(tracks, config=self.local_service.config), config=self.local_service.config, width=int(info.width), height=int(info.height))
            if not masks:
                return self._record_failure(clip, CleanupStatus.INELIGIBLE, reason or "no_stable_subtitle_track", started, before=before)
            after_frames = await self.local_service._extract_frames(candidate, timestamps[: len(before_frames)], temp_dir / "after", "after")
            quality_ok, quality = evaluate_quality_guard(before_frames, after_frames, masks, config=self.local_service.config, before_info=info, after_info=after_info)
            _samples, after_metrics, after_errors = self.local_service._ocr_frames(
                list(zip(timestamps[: len(after_frames)], after_frames)), analyzer=analyzer, config=self.local_service.config
            )
            after = analyzer.build_result(after_metrics, errors=after_errors, frame_budget=len(timestamps))
            reduction = evaluate_evidence_reduction(before, after, masks, config=self.local_service.config)
            status = CleanupStatus.SUCCEEDED if quality_ok and reduction["success"] else (CleanupStatus.FAILED_QUALITY if not quality_ok else CleanupStatus.RESIDUAL_SUBTITLE)
            output_path = self.derivative_path(clip)
            if status is CleanupStatus.SUCCEEDED and candidate != output_path.resolve():
                SubtitleCleanupService._atomic_publish(candidate, output_path)
            self._persist(
                clip_id, status=status, reason="" if status is CleanupStatus.SUCCEEDED else "adopted_output_validation_failed",
                output_path=output_path if status is CleanupStatus.SUCCEEDED else None,
                before=before, after=after, masks=masks, quality=quality, reduction=reduction,
                processing_ms=int((time.perf_counter() - started) * 1000), review_status=str(CleanupReviewStatus.APPROVED),
                run_id=run_id, input_kind=str(existing.get("input_kind") or "upload"),
                input_vid=input_vid, submitted_at=existing.get("submitted_at"),
                completed_at=existing.get("completed_at") or datetime.now(timezone.utc).isoformat(),
                cloud_status="Success", cloud_output_vid=output_vid, cloud_output_file_name=output_file_name,
            )
            return CloudCleanupOutcome(clip_id=clip_id, status=status, reason="adopted_downloaded_cloud_output", run_id=run_id, output_path=output_path if status is CleanupStatus.SUCCEEDED else None, before=before, after=after, masks=list(masks), quality=quality, reduction=reduction, cloud_status="Success", elapsed_ms=int((time.perf_counter() - started) * 1000))
        except Exception as exc:
            LOGGER.exception("could not adopt downloaded output for clip %s", clip_id)
            return self._record_failure(clip, CleanupStatus.FAILED_PROCESSING, f"adopt_error: {exc}", started, run_id=run_id, input_kind=str(existing.get("input_kind") or "upload"), input_vid=input_vid, submitted_at=existing.get("submitted_at"), completed_at=existing.get("completed_at"), cloud_status="Success", cloud_error_class=type(exc).__name__, cloud_output_vid=output_vid, cloud_output_file_name=output_file_name)
        finally:
            self.local_service._cleanup_dir(temp_dir)
