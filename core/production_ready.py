"""Production-readiness report (Milestone 9.4, section 11).

One row per **semantic** library clip (never per derivative):

* original / cleaned preferred media decision
* cleanup status + human review status
* preferred media health

A clip is production-ready when its preferred media path is healthy.  Subtitle
cleanup is optional: an original-only clip is production-ready.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.config import AppSettings
from core.subtitle_cleanup_models import CleanupReviewStatus, CleanupStatus
from storage.library import MaterialLibrary


@dataclass
class ProductionReadyRow:
    clip_id: int
    category: str
    material: str
    process_stage: str
    quality: float
    subtitle_class: str
    cleanup_status: str = ""
    review_status: str = ""
    preferred_kind: str = "original"
    preferred_path: str = ""
    original_path: str = ""
    file_health: str = ""
    production_ready: bool = False
    cleanup_version: str = ""
    review_failure_class: str = ""
    clip_review_status: str = "unreviewed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "category": self.category,
            "material": self.material,
            "process_stage": self.process_stage,
            "quality": self.quality,
            "subtitle_class": self.subtitle_class,
            "cleanup_status": self.cleanup_status,
            "review_status": self.review_status,
            "review_failure_class": self.review_failure_class,
            "clip_review_status": self.clip_review_status,
            "preferred_kind": self.preferred_kind,
            "preferred_path": self.preferred_path,
            "original_path": self.original_path,
            "file_health": self.file_health,
            "production_ready": self.production_ready,
            "cleanup_version": self.cleanup_version,
        }


@dataclass
class ProductionReadyReport:
    rows: list[ProductionReadyRow] = field(default_factory=list)
    aggregates: dict[str, int] = field(default_factory=dict)
    cleanup_version: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": [row.as_dict() for row in self.rows],
            "aggregates": dict(self.aggregates),
            "cleanup_version": self.cleanup_version,
        }


class ProductionReadyService:
    """Read-only production readiness audit over the material library."""

    def __init__(self, library: MaterialLibrary, settings: AppSettings) -> None:
        self.library = library
        self.settings = settings

    def _health(self, preferred: Path | None, original: Path | None) -> tuple[str, bool]:
        if preferred is None:
            return "outside_library", False
        if not preferred.exists():
            return "missing_file", False
        try:
            if preferred.stat().st_size <= 0:
                return "empty_file", False
        except OSError:
            return "unreadable", False
        if original is not None:
            try:
                if preferred.resolve() == original.resolve():
                    return "ok", True
            except OSError:
                pass
        return "ok", True

    def rows(self) -> list[ProductionReadyRow]:
        clips = self.library.inventory_clips(limit=100000)
        config = self.settings.subtitle_cleanup
        rows: list[ProductionReadyRow] = []
        for clip in clips:
            if clip.id is None:
                continue
            analysis = clip.subtitle_analysis if isinstance(clip.subtitle_analysis, dict) else None
            subtitle_class = (
                str(analysis.get("classification"))
                if analysis and analysis.get("classification")
                else str(clip.subtitle_type)
            )
            cleanup = self.library.subtitle_cleanup(int(clip.id))
            original = self.library.safe_media_path(clip.file_path)
            preferred = self.library.safe_media_path(
                self.library.preferred_media_path(
                    clip, require_review=bool(config.require_review_before_preferred)
                )
            )
            health, media_healthy = self._health(preferred, original)
            clip_review = str(clip.review_status)
            ready = bool(media_healthy and clip_review != "rejected")
            kind = "original"
            if (
                preferred is not None
                and original is not None
                and preferred.resolve() != original.resolve()
            ):
                kind = "cleaned"
            rows.append(
                ProductionReadyRow(
                    clip_id=int(clip.id),
                    category=clip.library_category or "",
                    material=clip.material or "",
                    process_stage=str(clip.process_stage),
                    quality=round(float(clip.overall_score or 0.0), 3),
                    subtitle_class=subtitle_class,
                    cleanup_status=str((cleanup or {}).get("status") or ""),
                    review_status=str((cleanup or {}).get("review_status") or ""),
                    review_failure_class=str(
                        (cleanup or {}).get("review_failure_class") or ""
                    ),
                    clip_review_status=clip_review,
                    preferred_kind=kind,
                    preferred_path=str(preferred) if preferred else "",
                    original_path=str(original) if original else str(clip.file_path),
                    file_health=health,
                    production_ready=ready,
                    cleanup_version=str((cleanup or {}).get("version") or ""),
                )
            )
        rows.sort(key=lambda row: row.clip_id)
        return rows

    def report(self) -> ProductionReadyReport:
        rows = self.rows()
        aggregates = {
            "total_semantic_clips": len(rows),
            "original_only": sum(1 for row in rows if row.preferred_kind == "original"),
            "cleaned_preferred": sum(1 for row in rows if row.preferred_kind == "cleaned"),
            "cleanup_pending": sum(
                1
                for row in rows
                if row.cleanup_status == str(CleanupStatus.SUCCEEDED)
                and row.review_status in ("", str(CleanupReviewStatus.PENDING))
            ),
            "cleanup_approved": sum(
                1 for row in rows if row.review_status == str(CleanupReviewStatus.APPROVED)
            ),
            "cleanup_rejected": sum(
                1 for row in rows if row.review_status == str(CleanupReviewStatus.REJECTED)
            ),
            "cleanup_failed": sum(
                1
                for row in rows
                if row.cleanup_status
                in (
                    str(CleanupStatus.FAILED_PROCESSING),
                    str(CleanupStatus.FAILED_QUALITY),
                    str(CleanupStatus.RESIDUAL_SUBTITLE),
                )
            ),
            "production_ready_clips": sum(1 for row in rows if row.production_ready),
            "missing_preferred_media": sum(
                1 for row in rows if row.file_health != "ok"
            ),
            "rejected_clips": sum(
                1 for row in rows if row.clip_review_status == "rejected"
            ),
            "rejected_clips_not_usable": sum(
                1
                for row in rows
                if row.clip_review_status == "rejected"
            ),
            "missing_approved_derivatives": sum(
                1
                for row in rows
                if row.cleanup_status == str(CleanupStatus.SUCCEEDED)
                and row.review_status == str(CleanupReviewStatus.APPROVED)
                and row.preferred_kind != "cleaned"
            ),
        }
        return ProductionReadyReport(
            rows=rows,
            aggregates=aggregates,
            cleanup_version=self.settings.subtitle_cleanup.version,
        )
