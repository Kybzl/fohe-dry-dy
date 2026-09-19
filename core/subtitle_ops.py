"""Operator-facing subtitle analysis (Milestone 6, sections 31/32/33/50).

* ``report()`` - measured class distribution, average cleanliness, subtitle
  rejection context (read-only, works with historical rows)
* ``analyze_clip()`` - measure one stored clip (dry-run by default)
* ``analyze_many()`` - bounded bulk analysis, dry-run unless confirmed

Frame extraction reuses the same 20/40/60/80% policy as clip tagging so the
analysis sees the same pictures the tagger saw.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from analyzers.subtitle_analysis import (
    SubtitleAnalyzer,
    classification_bucket,
)
from core.config import AppSettings
from core.frame_policy import clip_frame_timestamps
from core.models import ClipRecord, PreviewFrame, SubtitleType
from core.subtitle_models import SubtitleAnalysisResult
from media.ffmpeg import MediaToolkit
from storage.library import MaterialLibrary

LOGGER = logging.getLogger(__name__)


@dataclass
class ClipSubtitleResult:
    """One clip's measured subtitle outcome."""

    clip_id: int
    library_category: str = ""
    previous_type: SubtitleType = SubtitleType.UNKNOWN
    previous_score: float = 0.0
    result: SubtitleAnalysisResult | None = None
    applied: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.result is not None and not self.result.is_unavailable

    def row(self) -> list[Any]:
        measured = self.result
        return [
            self.clip_id,
            self.library_category or "未记录",
            str(self.previous_type),
            str(measured.classification) if measured else "-",
            round(measured.cleanliness_score, 2) if measured else 0.0,
            round(measured.total_text_area_ratio_avg, 3) if measured else 0.0,
            measured.avg_text_regions if measured else 0.0,
            measured.decision_source if measured else self.error[:20],
        ]


@dataclass
class SubtitleOpsReport:
    """Aggregated report used by ``--subtitle-report`` (section 31)."""

    total_clips: int = 0
    measured_clips: int = 0
    classification_counts: dict[str, int] = field(default_factory=dict)
    bucket_counts: dict[str, int] = field(default_factory=dict)
    average_cleanliness: float | None = None
    measured_average_cleanliness: float | None = None
    subtitle_rejections: int = 0
    preview_rejections: int = 0
    decision_sources: dict[str, int] = field(default_factory=dict)
    per_category: list[dict[str, Any]] = field(default_factory=list)


class SubtitleOps:
    """Analyze and report measured subtitle evidence for stored clips."""

    def __init__(
        self,
        library: MaterialLibrary,
        settings: AppSettings,
        *,
        analyzer: SubtitleAnalyzer | None = None,
        toolkit: MediaToolkit | None = None,
    ) -> None:
        self.library = library
        self.settings = settings
        self.analyzer = analyzer
        self.toolkit = toolkit
        self._frames_dir = settings.paths.cache_dir / "frames"

    # -- helpers -----------------------------------------------------------
    def _analyzer(self) -> SubtitleAnalyzer:
        if self.analyzer is None:
            from core.dependencies import build_subtitle_analyzer

            # stored clips are real files, so the mock-media skip does not apply
            self.analyzer = build_subtitle_analyzer(
                self.settings, cache=self.library, media_backend="ffmpeg"
            )
        if self.analyzer is None:  # pragma: no cover - disabled by config
            raise RuntimeError("subtitle analysis is disabled in config")
        return self.analyzer

    def _toolkit(self) -> MediaToolkit:
        if self.toolkit is None:
            from core.dependencies import build_toolkit

            self.toolkit = build_toolkit(self.settings, backend=self.settings.media.backend)
        return self.toolkit

    async def _frames_for_clip(self, clip: ClipRecord) -> tuple[list[PreviewFrame], Path | None]:
        video = self.library.safe_media_path(clip.file_path)
        if video is None or not video.exists():
            raise FileNotFoundError(f"clip #{clip.id} file is missing or outside the library")
        duration = float(clip.duration or (clip.source_end - clip.source_start) or 0.0)
        if duration <= 0:
            raise ValueError(f"clip #{clip.id} has no usable duration")
        ratios = tuple(self.settings.analysis.clip_frame_ratios) or (0.2, 0.4, 0.6, 0.8)
        stamps = clip_frame_timestamps(0.0, duration, len(ratios), ratios=ratios)
        out_dir = self._frames_dir / f"subtitle_{clip.id}"
        written = await self._toolkit().extract_frames(
            video, stamps, out_dir, f"sub{clip.id}", size=self.settings.analysis.preview_max_width
        )
        frames = [
            PreviewFrame(timestamp=timestamp, image_path=image, source="clip")
            for timestamp, image in zip(stamps, written)
        ]
        return frames, out_dir

    @staticmethod
    def _cleanup(directory: Path | None) -> None:
        if directory is None:
            return
        try:
            if directory.exists():
                for file in directory.rglob("*"):
                    if file.is_file():
                        file.unlink(missing_ok=True)
                directory.rmdir()
        except OSError as exc:  # pragma: no cover - defensive
            LOGGER.debug("could not clean subtitle frames %s: %s", directory, exc)

    # -- single clip -------------------------------------------------------
    async def analyze_clip(
        self,
        clip_id: int,
        *,
        apply: bool = False,
    ) -> ClipSubtitleResult:
        clip = self.library.get_clip(clip_id)
        if clip is None:
            return ClipSubtitleResult(clip_id=clip_id, error="clip not found")
        outcome = ClipSubtitleResult(
            clip_id=clip_id,
            library_category=clip.library_category,
            previous_type=clip.subtitle_type,
            previous_score=clip.subtitle_score,
        )
        frames_dir: Path | None = None
        try:
            frames, frames_dir = await self._frames_for_clip(clip)
            analyzer = self._analyzer()
            cache_key = (
                f"clip:{clip.sha256}" if clip.sha256 else f"clip:{Path(clip.file_path).stem}"
            )
            outcome.result = await analyzer.analyze_frames(
                frames, cache_key=cache_key, recognize=True
            )
        except Exception as exc:
            LOGGER.exception("subtitle analysis failed for clip %s", clip_id)
            outcome.error = str(exc)[:200]
            return outcome
        finally:
            self._cleanup(frames_dir)
        if apply and outcome.ok:
            self.library.save_clip_subtitle_analysis(clip_id, outcome.result)
            self.library.database.execute(
                "UPDATE clips SET subtitle_type = ?, subtitle_score = ?, "
                "subtitle_cleanliness_score = ? WHERE id = ?",
                (
                    str(outcome.result.classification),
                    round(1.0 - outcome.result.cleanliness_score, 3),
                    outcome.result.cleanliness_score,
                    clip_id,
                ),
            )
            outcome.applied = True
        return outcome

    # -- bulk --------------------------------------------------------------
    async def analyze_many(
        self,
        *,
        library_category: str | None = None,
        limit: int = 20,
        apply: bool = False,
    ) -> list[ClipSubtitleResult]:
        clips = self.library.inventory_clips(limit=1000)
        if library_category:
            clips = [clip for clip in clips if clip.library_category == library_category]
        selected = clips[: max(1, int(limit))]
        results: list[ClipSubtitleResult] = []
        for clip in selected:
            results.append(await self.analyze_clip(int(clip.id or 0), apply=apply))
        return results

    # -- report ------------------------------------------------------------
    def report(self, *, library_category: str | None = None) -> SubtitleOpsReport:
        """Aggregate measured + historical subtitle data (sections 31/46/47)."""

        report = SubtitleOpsReport()
        clips = self.library.inventory_clips(limit=100000)
        if library_category:
            clips = [clip for clip in clips if clip.library_category == library_category]
        report.total_clips = len(clips)
        cleanliness_values: list[float] = []
        measured_values: list[float] = []
        per_category: dict[str, dict[str, Any]] = {}
        for clip in clips:
            measured = clip.subtitle_analysis
            classification = clip.subtitle_type
            source = "historical"
            if isinstance(measured, dict) and measured.get("classification"):
                classification = SubtitleType(str(measured["classification"]))
                source = str(measured.get("decision_source") or "local")
                report.measured_clips += 1
                measured_values.append(float(measured.get("cleanliness_score") or 0.0))
            report.classification_counts[str(classification)] = (
                report.classification_counts.get(str(classification), 0) + 1
            )
            bucket = classification_bucket(classification)
            report.bucket_counts[bucket] = report.bucket_counts.get(bucket, 0) + 1
            report.decision_sources[source] = report.decision_sources.get(source, 0) + 1
            cleanliness_values.append(float(clip.subtitle_cleanliness_score or 0.0))
            stats = per_category.setdefault(
                clip.library_category or "未记录",
                {"clips": 0, "clean": 0, "simple": 0, "complex": 0, "unknown": 0},
            )
            stats["clips"] += 1
            stats[bucket] = stats.get(bucket, 0) + 1
        if cleanliness_values:
            report.average_cleanliness = round(
                sum(cleanliness_values) / len(cleanliness_values), 3
            )
        if measured_values:
            report.measured_average_cleanliness = round(
                sum(measured_values) / len(measured_values), 3
            )
        report.per_category = [
            {"library_category": name, **stats} for name, stats in sorted(per_category.items())
        ]
        row = self.library.database.query_one(
            "SELECT COUNT(*) AS n FROM source_videos "
            "WHERE reject_reason IN ('subtitle_too_complex', 'multi_region_subtitle', "
            "'colored_text_block', 'large_center_text', 'too_many_stickers')"
        )
        report.subtitle_rejections = int(row["n"]) if row else 0
        row = self.library.database.query_one(
            "SELECT COUNT(*) AS n FROM source_videos WHERE status = 'rejected_preview'"
        )
        report.preview_rejections = int(row["n"]) if row else 0
        return report

    def search_yield_subtitle_insight(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Subtitle rejection rate per search query where data allows (section 47)."""

        rows = self.library.database.query(
            """
            SELECT matched_queries,
                   status,
                   reject_reason
            FROM source_videos
            WHERE matched_queries IS NOT NULL AND matched_queries != ''
            """
        )
        import json as _json

        buckets: dict[str, dict[str, int]] = {}
        for row in rows:
            try:
                queries = _json.loads(row["matched_queries"]) or []
            except (TypeError, ValueError):  # pragma: no cover - defensive
                continue
            if not isinstance(queries, list):
                continue
            rejected = str(row["status"]) == "rejected_preview"
            subtitle = str(row["reject_reason"] or "") in {
                "subtitle_too_complex",
                "multi_region_subtitle",
                "colored_text_block",
                "large_center_text",
                "too_many_stickers",
            }
            for query in queries:
                bucket = buckets.setdefault(str(query), {"seen": 0, "rejected": 0, "subtitle": 0})
                bucket["seen"] += 1
                if rejected:
                    bucket["rejected"] += 1
                if subtitle:
                    bucket["subtitle"] += 1
        insight = [
            {
                "query": query,
                "candidates_seen": bucket["seen"],
                "preview_rejected": bucket["rejected"],
                "subtitle_rejected": bucket["subtitle"],
                "subtitle_rejection_rate": round(
                    bucket["subtitle"] / bucket["seen"], 3
                )
                if bucket["seen"]
                else None,
            }
            for query, bucket in buckets.items()
        ]
        insight.sort(key=lambda item: (item["subtitle_rejection_rate"] or 0), reverse=True)
        return insight[: max(1, int(limit))]
