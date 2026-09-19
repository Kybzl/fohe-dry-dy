"""Final quality gate: the last chance to reject a weak clip."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.models import ClipTagging, RejectReason, SubtitlePolicy, SubtitleType
from core.normalization import material_base

LOGGER = logging.getLogger(__name__)

DEFAULT_SUBTITLE_LIMITS: dict[SubtitlePolicy, float] = {
    SubtitlePolicy.STRICT: 0.25,
    SubtitlePolicy.BALANCED: 0.45,
    SubtitlePolicy.LOOSE: 0.65,
    SubtitlePolicy.OFF: 1.0,
}

#: subtitle layouts that are never acceptable, whatever the policy
HARD_REJECT_SUBTITLES = frozenset(
    {SubtitleType.MULTI_REGION, SubtitleType.COLORED_BLOCK, SubtitleType.COMPLEX}
)


@dataclass(frozen=True)
class GateDecision:
    accepted: bool
    reason: RejectReason | None = None
    detail: str = ""
    subtitle_rejected: bool = False


class QualityGate:
    """Score and subtitle based acceptance rules for final clips."""

    def __init__(
        self,
        *,
        policy: SubtitlePolicy = SubtitlePolicy.STRICT,
        min_overall_score: float = 0.55,
        min_material_score: float = 0.6,
        min_visual_quality_score: float = 0.5,
        subtitle_score_limit: dict[str, float] | None = None,
    ) -> None:
        self.policy = policy
        self.min_overall_score = min_overall_score
        self.min_material_score = min_material_score
        self.min_visual_quality_score = min_visual_quality_score
        limits = dict(DEFAULT_SUBTITLE_LIMITS)
        for key, value in (subtitle_score_limit or {}).items():
            try:
                limits[SubtitlePolicy(key)] = float(value)
            except ValueError:  # pragma: no cover - bad config value
                continue
        self.subtitle_score_limit = limits

    def with_policy(self, policy: SubtitlePolicy) -> QualityGate:
        return QualityGate(
            policy=policy,
            min_overall_score=self.min_overall_score,
            min_material_score=self.min_material_score,
            min_visual_quality_score=self.min_visual_quality_score,
            subtitle_score_limit={str(key): value for key, value in self.subtitle_score_limit.items()},
        )

    def evaluate(
        self,
        *,
        tagging: ClipTagging,
        duration: float,
        min_duration: float = 3.0,
        max_duration: float = 15.0,
        required_material: str = "",
    ) -> GateDecision:
        if duration < min_duration or duration > max_duration:
            return GateDecision(
                False,
                RejectReason.SEGMENT_TOO_SHORT if duration < min_duration else RejectReason.QUALITY_GATE,
                f"duration {duration:.2f}s outside [{min_duration}, {max_duration}]",
            )

        if tagging.subtitle_type in HARD_REJECT_SUBTITLES:
            return GateDecision(
                False,
                RejectReason.SUBTITLE_TOO_COMPLEX,
                f"subtitle type {tagging.subtitle_type}",
                True,
            )

        limit = self.subtitle_score_limit.get(self.policy, 0.25)
        if tagging.subtitle_score > limit:
            return GateDecision(
                False,
                RejectReason.SUBTITLE_TOO_COMPLEX,
                f"subtitle score {tagging.subtitle_score:.2f} > {limit:.2f} ({self.policy})",
                True,
            )

        expected = material_base(required_material)
        observed = material_base(tagging.material)
        if expected and observed != expected:
            return GateDecision(
                False,
                RejectReason.NO_MATERIAL,
                f"observed material {observed or '(empty)'} != required {expected}",
            )

        scores = tagging.scores
        if scores.material_relevance < self.min_material_score:
            return GateDecision(
                False,
                RejectReason.NO_MATERIAL,
                f"material score {scores.material_relevance:.2f} < {self.min_material_score:.2f}",
            )
        if scores.visual_quality < self.min_visual_quality_score:
            return GateDecision(
                False,
                RejectReason.LOW_QUALITY,
                f"visual quality {scores.visual_quality:.2f} < {self.min_visual_quality_score:.2f}",
            )
        if scores.overall < self.min_overall_score:
            return GateDecision(
                False,
                RejectReason.QUALITY_GATE,
                f"overall {scores.overall:.2f} < {self.min_overall_score:.2f}",
            )

        return GateDecision(True, detail="passed")
