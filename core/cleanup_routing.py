"""Post-acquisition subtitle-cleanup routing (Milestone 9.4).

Every newly accepted library clip is classified from its stored
``subtitle_analysis_v1`` result:

    none / watermark_only            -> not_needed
    complex / unknown                -> ineligible
    bottom_simple / top_simple /
    single_region                    -> eligible_for_cleanup

Classification is metadata-only.  Destructive cleanup is opt-in per acquisition
run and hard-bounded by ``post_acquisition_max_cleanup``.  A successful cleanup
always stays ``review_status=pending``; this module never auto-approves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from core.config import AppSettings
from core.models import ClipRecord, SubtitleType
from core.subtitle_cleanup import SubtitleCleanupService
from core.subtitle_cleanup_models import CleanupStatus
from storage.library import MaterialLibrary

LOGGER = logging.getLogger(__name__)

ROUTING_NOT_NEEDED = "not_needed"
ROUTING_INELIGIBLE = "ineligible"
ROUTING_ELIGIBLE = "eligible_for_cleanup"
ROUTING_UNMEASURED = "unmeasured"


@dataclass
class CleanupRoutingDecision:
    """One new clip's routing result (read-only unless cleanup was enabled)."""

    clip_id: int
    classification: str = ""
    routing: str = ROUTING_UNMEASURED
    reason: str = ""
    cleanup_status: str = ""
    review_status: str = ""
    cleanup_attempted: bool = False
    cleanup_result_status: str = ""
    cleanup_output_path: str = ""
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        return self.routing == ROUTING_ELIGIBLE


class CleanupRouter:
    """Classify newly acquired clips and optionally run bounded cleanup."""

    def __init__(
        self,
        library: MaterialLibrary,
        settings: AppSettings,
        *,
        service: SubtitleCleanupService | None = None,
    ) -> None:
        self.library = library
        self.settings = settings
        self._service = service

    def service(self) -> SubtitleCleanupService:
        if self._service is None:
            self._service = SubtitleCleanupService(self.library, self.settings)
        return self._service

    @staticmethod
    def classify_analysis(analysis: dict[str, Any] | None, *, eligible_classes: set[str]) -> tuple[str, str, str]:
        """Return ``(classification, routing, reason)`` for one stored result."""

        if not analysis or not analysis.get("classification"):
            return "", ROUTING_UNMEASURED, "subtitle_analysis_v1_missing"
        classification = str(analysis.get("classification"))
        if classification in (str(SubtitleType.NONE), str(SubtitleType.WATERMARK_ONLY)):
            return classification, ROUTING_NOT_NEEDED, f"classification={classification}"
        if classification in eligible_classes:
            return classification, ROUTING_ELIGIBLE, f"eligible_simple_class:{classification}"
        return classification, ROUTING_INELIGIBLE, f"classification_not_eligible:{classification}"

    def classify_clip(self, clip: ClipRecord) -> CleanupRoutingDecision:
        analysis = clip.subtitle_analysis if isinstance(clip.subtitle_analysis, dict) else None
        classification, routing, reason = self.classify_analysis(
            analysis, eligible_classes={str(value) for value in self.settings.subtitle_cleanup.eligible_classes}
        )
        record = (
            self.library.subtitle_cleanup(int(clip.id))
            if clip.id is not None
            else None
        )
        return CleanupRoutingDecision(
            clip_id=int(clip.id or 0),
            classification=classification,
            routing=routing,
            reason=reason,
            cleanup_status=str((record or {}).get("status") or ""),
            review_status=str((record or {}).get("review_status") or ""),
            cleanup_output_path=str((record or {}).get("output_path") or ""),
            details={
                "material": clip.material,
                "process_stage": str(clip.process_stage),
                "library_category": clip.library_category,
            },
        )

    async def route_clips(
        self,
        clip_ids: Sequence[int],
        *,
        attempt_cleanup: bool = False,
        max_attempts: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> list[CleanupRoutingDecision]:
        """Classify clips and optionally attempt cleanup for eligible ones.

        ``attempt_cleanup`` is intentionally a per-run opt-in: the default
        acquisition path classifies every new clip but never edits media.
        """

        config = self.settings.subtitle_cleanup
        if not config.post_acquisition_routing:
            return []
        bound = (
            max(0, int(max_attempts))
            if max_attempts is not None
            else max(0, int(config.post_acquisition_max_cleanup))
        )
        decisions: list[CleanupRoutingDecision] = []
        attempts = 0
        for clip_id in clip_ids:
            clip = self.library.get_clip(int(clip_id))
            if clip is None:
                LOGGER.warning("cleanup routing: clip %s not found", clip_id)
                continue
            decision = self.classify_clip(clip)
            if decision.eligible and attempt_cleanup and attempts < bound:
                attempts += 1
                decision.cleanup_attempted = True
                try:
                    outcome = await self.service().cleanup_clip(int(clip_id))
                    decision.cleanup_result_status = str(outcome.status)
                    decision.cleanup_output_path = (
                        str(outcome.output_path) if outcome.output_path else ""
                    )
                    refreshed = self.library.subtitle_cleanup(int(clip_id))
                    if refreshed is not None:
                        decision.cleanup_status = str(refreshed.get("status") or "")
                        decision.review_status = str(
                            refreshed.get("review_status") or "pending"
                        )
                    if outcome.error:
                        decision.error = outcome.error
                except Exception as exc:  # pragma: no cover - router safety net
                    LOGGER.exception("post-acquisition cleanup failed for clip %s", clip_id)
                    decision.error = str(exc)
                    decision.cleanup_result_status = str(CleanupStatus.FAILED_PROCESSING)
            self.library.log_maintenance(
                "cleanup_routing",
                target_type="clip",
                target_id=decision.clip_id,
                details={
                    "clip_id": decision.clip_id,
                    "cleanup_version": config.version,
                    "classification": decision.classification,
                    "routing": decision.routing,
                    "reason": decision.reason,
                    "cleanup_attempted": decision.cleanup_attempted,
                    "cleanup_result_status": decision.cleanup_result_status,
                    **(context or {}),
                },
            )
            decisions.append(decision)
        return decisions
