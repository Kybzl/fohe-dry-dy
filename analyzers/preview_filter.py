"""AI pre-filter stage (section 10 of the specification).

The AI verdict is only one input: the configured subtitle policy is applied on
top of it, so an accepted video with heavy subtitles is still dropped when the
user asks for strict filtering.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ai.base import AuditContext, PreviewFilterRequest
from ai.gateway import AIGateway
from analyzers.subtitle_analysis import (
    HARD_REJECT_CLASSES,
    combine_with_qwen,
    reject_reason_for,
)
from core.models import (
    ComplexityLevel,
    PreviewFilterResult,
    PreviewSource,
    RejectReason,
    SubtitlePolicy,
    SubtitleType,
    VideoCandidate,
)
from core.subtitle_models import SubtitleAnalysisResult

LOGGER = logging.getLogger(__name__)

#: subtitle-related rejection reasons, counted as "字幕淘汰" in the UI
SUBTITLE_REASONS = frozenset(
    {
        RejectReason.MULTI_REGION_SUBTITLE,
        RejectReason.COLORED_TEXT_BLOCK,
        RejectReason.LARGE_CENTER_TEXT,
        RejectReason.SUBTITLE_TOO_COMPLEX,
        RejectReason.TOO_MANY_STICKERS,
    }
)

ALLOWED_SUBTITLE_COMPLEXITY: dict[SubtitlePolicy, frozenset[ComplexityLevel]] = {
    SubtitlePolicy.STRICT: frozenset({ComplexityLevel.LOW}),
    SubtitlePolicy.BALANCED: frozenset({ComplexityLevel.LOW, ComplexityLevel.MEDIUM}),
    SubtitlePolicy.LOOSE: frozenset({ComplexityLevel.LOW, ComplexityLevel.MEDIUM}),
    SubtitlePolicy.OFF: frozenset(
        {ComplexityLevel.LOW, ComplexityLevel.MEDIUM, ComplexityLevel.HIGH, ComplexityLevel.UNKNOWN}
    ),
}


@dataclass(frozen=True)
class PreviewDecision:
    """Outcome of the AI pre-filter for one candidate video."""

    accepted: bool
    reason: RejectReason | None = None
    detail: str = ""
    subtitle_rejected: bool = False
    result: PreviewFilterResult | None = None
    ai_failed: bool = False

    @property
    def material_score(self) -> float | None:
        return self.result.material_relevance if self.result else None

    @property
    def subtitle_score(self) -> float | None:
        if self.result is None:
            return None
        return {
            ComplexityLevel.LOW: 0.1,
            ComplexityLevel.MEDIUM: 0.5,
            ComplexityLevel.HIGH: 0.9,
        }.get(self.result.subtitle_complexity, 0.5)

    @property
    def quality_score(self) -> float | None:
        return self.result.quality_score if self.result else None


class PreviewFilter:
    """Runs the pre-filter through the gateway and applies local policy."""

    def __init__(
        self,
        gateway: AIGateway,
        *,
        policy: SubtitlePolicy = SubtitlePolicy.STRICT,
        min_material_relevance: float = 0.35,
        min_quality_score: float = 0.45,
        max_frames: int = 16,
        #: measured subtitle allowance per policy (1 - cleanliness)
        subtitle_score_limit: dict[str, float] | None = None,
    ) -> None:
        self.gateway = gateway
        self.policy = policy
        self.min_material_relevance = min_material_relevance
        self.min_quality_score = min_quality_score
        self.max_frames = max(1, max_frames)
        limits = {
            SubtitlePolicy.STRICT: 0.25,
            SubtitlePolicy.BALANCED: 0.45,
            SubtitlePolicy.LOOSE: 0.65,
            SubtitlePolicy.OFF: 1.0,
        }
        for key, value in (subtitle_score_limit or {}).items():
            try:
                limits[SubtitlePolicy(key)] = float(value)
            except ValueError:  # pragma: no cover - bad config value
                continue
        self.subtitle_score_limit = limits

    def with_policy(self, policy: SubtitlePolicy) -> PreviewFilter:
        return PreviewFilter(
            self.gateway,
            policy=policy,
            min_material_relevance=self.min_material_relevance,
            min_quality_score=self.min_quality_score,
            max_frames=self.max_frames,
            subtitle_score_limit={str(key): value for key, value in self.subtitle_score_limit.items()},
        )

    async def evaluate(
        self,
        *,
        candidate: VideoCandidate,
        material: str,
        query: str,
        preview: PreviewSource,
        audit: AuditContext | None = None,
        subtitle: SubtitleAnalysisResult | None = None,
    ) -> PreviewDecision:
        """Decide whether the video is worth downloading and analysing.

        The measured subtitle analysis (when available) is authoritative for the
        *subtitle* part of the decision; the VLM still owns material/quality.
        """

        frames = list(preview.frames)[: self.max_frames]
        if not frames:
            return PreviewDecision(
                accepted=False,
                reason=RejectReason.UNUSABLE_VISUAL,
                detail="no preview frames could be extracted",
            )
        request = PreviewFilterRequest(
            material=material,
            query=query,
            platform=candidate.platform,
            platform_video_id=candidate.platform_video_id,
            title=candidate.title,
            duration=candidate.duration,
            frames=frames,
            context=dict(candidate.metadata),
            audit=audit or AuditContext(),
        )
        result = await self.gateway.preview_filter(request)
        if result is None:
            LOGGER.warning("preview filter unavailable for %s", candidate.platform_video_id)
            return PreviewDecision(
                accepted=False,
                reason=RejectReason.OTHER,
                detail="ai pre-filter failed on all providers",
                ai_failed=True,
            )

        if not result.accept:
            reason = result.reject_reason or RejectReason.OTHER
            return PreviewDecision(
                accepted=False,
                reason=reason,
                detail=f"ai rejected ({reason})",
                subtitle_rejected=reason in SUBTITLE_REASONS,
                result=result,
            )

        if not result.material_visible or result.material_relevance < self.min_material_relevance:
            return PreviewDecision(
                accepted=False,
                reason=RejectReason.NO_MATERIAL,
                detail=f"material relevance {result.material_relevance:.2f} too low",
                result=result,
            )

        if result.quality_score < self.min_quality_score:
            return PreviewDecision(
                accepted=False,
                reason=RejectReason.LOW_QUALITY,
                detail=f"quality score {result.quality_score:.2f} too low",
                result=result,
            )

        allowed = ALLOWED_SUBTITLE_COMPLEXITY.get(self.policy, ALLOWED_SUBTITLE_COMPLEXITY[SubtitlePolicy.STRICT])
        measured = combine_with_qwen(
            subtitle,
            qwen_type=_subtitle_type_from_result(result),
            qwen_complexity=result.subtitle_complexity,
            accept=result.accept,
        )
        limit = self.subtitle_score_limit.get(self.policy, 0.25)
        # Only a *real* local measurement may override the VLM verdict; when the
        # analyzer is unavailable the legacy complexity policy applies verbatim.
        has_measurement = subtitle is not None and not subtitle.is_unavailable
        if has_measurement:
            # measured cleanliness is authoritative (section 29)
            measured_score = round(1.0 - measured.cleanliness_score, 3)
            if measured.classification in HARD_REJECT_CLASSES:
                reason = reject_reason_for(measured.classification) or RejectReason.SUBTITLE_TOO_COMPLEX
                return PreviewDecision(
                    accepted=False,
                    reason=reason,
                    detail=(
                        f"measured subtitle class {measured.classification} "
                        f"(cleanliness {measured.cleanliness_score:.2f}, "
                        f"{_evidence_hint(measured)})"
                    ),
                    subtitle_rejected=True,
                    result=result,
                )
            if measured_score > limit:
                reason = reject_reason_for(measured.classification) or RejectReason.SUBTITLE_TOO_COMPLEX
                return PreviewDecision(
                    accepted=False,
                    reason=reason,
                    detail=(
                        f"measured subtitle score {measured_score:.2f} > {limit:.2f} "
                        f"({self.policy}); class={measured.classification}"
                    ),
                    subtitle_rejected=True,
                    result=result,
                )
            # the measurement says the footage is usable even if the VLM was
            # pessimistic: keep it and record the disagreement
            return PreviewDecision(
                accepted=True,
                result=result,
                detail=(
                    f"accepted by measured subtitle analysis "
                    f"({measured.classification}, cleanliness {measured.cleanliness_score:.2f}, "
                    f"source={measured.decision_source})"
                ),
            )
        if result.subtitle_complexity not in allowed:
            return PreviewDecision(
                accepted=False,
                reason=RejectReason.SUBTITLE_TOO_COMPLEX,
                detail=(
                    f"subtitle complexity {result.subtitle_complexity} rejected by "
                    f"{self.policy} policy (VLM fallback: {measured.unavailable_reason})"
                ),
                subtitle_rejected=True,
                result=result,
            )

        return PreviewDecision(accepted=True, result=result, detail="accepted")


#: legacy policy limits reused for the measured score (1 - cleanliness)
def _subtitle_type_from_result(result: PreviewFilterResult) -> SubtitleType:
    """Map the VLM complexity label onto the shared subtitle vocabulary."""

    return {
        ComplexityLevel.LOW: SubtitleType.BOTTOM_SIMPLE,
        ComplexityLevel.MEDIUM: SubtitleType.SINGLE_REGION,
        ComplexityLevel.HIGH: SubtitleType.COMPLEX,
        ComplexityLevel.UNKNOWN: SubtitleType.UNKNOWN,
    }.get(result.subtitle_complexity, SubtitleType.UNKNOWN)


def _evidence_hint(measured: SubtitleAnalysisResult) -> str:
    """Short, non-sensitive summary of why a class was chosen."""

    return (
        f"areas avg={measured.total_text_area_ratio_avg:.3f} "
        f"center_p={measured.center_persistence:.2f} "
        f"multi_p={measured.multi_region_persistence:.2f} "
        f"band_p={measured.band_persistence:.2f}"
    )
