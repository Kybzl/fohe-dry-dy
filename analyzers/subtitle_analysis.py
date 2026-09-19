"""Measured subtitle analysis (Milestone 6).

    sampled frames
      -> local text detection (analyzers/text_detection.py)
      -> per-frame geometry metrics
      -> temporal persistence metrics
      -> deterministic classification + cleanliness score
      -> optional Qwen semantic adjudication (hybrid)

Nothing here modifies pixels: it is detection and measurement only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from analyzers.text_detection import TextDetector, build_detector, detector_status
from core.models import ComplexityLevel, PreviewFrame, SubtitleType
from core.subtitle_models import (
    ANALYSIS_VERSION,
    SOURCE_HYBRID,
    SOURCE_LOCAL,
    SOURCE_QWEN,
    SOURCE_UNAVAILABLE,
    ZONE_BOTTOM,
    ZONE_CENTER,
    ZONE_TOP,
    FrameTextMetrics,
    SubtitleAnalysisResult,
    SubtitleCleanlinessWeights,
    SubtitleRuleSettings,
    SubtitleZoneSettings,
    frame_metrics,
    temporal_metrics,
)

LOGGER = logging.getLogger(__name__)

#: classification buckets used by the reports / UI filters (sections 31/46)
CLEAN_CLASSES: frozenset[SubtitleType] = frozenset(
    {SubtitleType.NONE, SubtitleType.WATERMARK_ONLY, SubtitleType.BOTTOM_SIMPLE}
)
SIMPLE_CLASSES: frozenset[SubtitleType] = frozenset(
    {SubtitleType.NONE, SubtitleType.WATERMARK_ONLY, SubtitleType.BOTTOM_SIMPLE, SubtitleType.TOP_SIMPLE}
)
COMPLEX_CLASSES: frozenset[SubtitleType] = frozenset(
    {
        SubtitleType.MULTI_REGION,
        SubtitleType.COLORED_BLOCK,
        SubtitleType.COMPLEX,
        SubtitleType.LARGE_CENTER_TEXT,
        SubtitleType.PROMOTIONAL_OVERLAY,
        SubtitleType.DENSE_TEXT,
    }
)


class SubtitleCache(Protocol):
    """Minimal cache contract (implemented by the SQLite library)."""

    def subtitle_cache_get(self, key: str) -> dict[str, Any] | None: ...

    def subtitle_cache_put(self, key: str, payload: Mapping[str, Any]) -> None: ...


@dataclass
class SubtitleAnalysisSettings:
    """Effective analyzer configuration (``subtitle_analysis:`` in config.yaml)."""

    enabled: bool = True
    engine: str = "auto"
    #: frames measured for a final clip (recognition on: promotion keywords)
    max_frames: int = 4
    #: frames measured for a source preview (detection only: cheap, geometry)
    preview_max_frames: int = 3
    #: mock/placeholder frames carry no real content: skip unless forced
    skip_with_mock_media: bool = True
    force_with_mock: bool = False
    hybrid_qwen: bool = True
    use_cache: bool = True
    debug_dir: Path | None = None
    zones: SubtitleZoneSettings = None  # type: ignore[assignment]
    rules: SubtitleRuleSettings = None  # type: ignore[assignment]
    weights: SubtitleCleanlinessWeights = None  # type: ignore[assignment]
    detector: TextDetector | None = None

    def __post_init__(self) -> None:
        self.zones = self.zones or SubtitleZoneSettings()
        self.rules = self.rules or SubtitleRuleSettings()
        self.weights = self.weights or SubtitleCleanlinessWeights()


class SubtitleAnalyzer:
    """Turns sampled frames into measured subtitle evidence."""

    def __init__(
        self,
        settings: SubtitleAnalysisSettings,
        *,
        cache: SubtitleCache | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache if (cache is not None and settings.use_cache) else None
        self.detector: TextDetector = settings.detector or build_detector(settings.engine)
        self._status: tuple[bool, str] | None = None
        self._signature: str | None = None

    # -- cache identity ----------------------------------------------------
    def settings_signature(self) -> str:
        """Short hash of every threshold that can change a verdict.

        A cached measurement is only reused while the analysis version *and*
        the rules are unchanged (section 34).
        """

        if self._signature is None:
            payload = {
                "version": ANALYSIS_VERSION,
                "engine": self.detector.name,
                "zones": self.settings.zones.model_dump(),
                "rules": self.settings.rules.model_dump(),
                "weights": self.settings.weights.model_dump(),
            }
            blob = json.dumps(payload, sort_keys=True, default=str)
            self._signature = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]
        return self._signature

    def cache_key(self, base: str) -> str:
        return f"{base}|{ANALYSIS_VERSION}|{self.settings_signature()}" if base else ""

    # -- status ------------------------------------------------------------
    @property
    def available(self) -> bool:
        if self._status is None:
            self._status = self.detector.available()
        return self._status[0]

    @property
    def status_note(self) -> str:
        if self._status is None:
            self._status = self.detector.available()
        return self._status[1]

    def status(self) -> dict[str, Any]:
        return detector_status(self.detector)

    # -- per frame ---------------------------------------------------------
    def measure_frame(self, image_path: Path | str, *, timestamp: float) -> FrameTextMetrics:
        regions = self.detector.detect(image_path)
        return frame_metrics(
            regions, zones=self.settings.zones, rules=self.settings.rules, timestamp=timestamp
        )

    # -- several frames ----------------------------------------------------
    def analyze_paths(
        self,
        frames: Sequence[tuple[float, Path]],
        *,
        cache_key: str = "",
        recognize: bool = True,
    ) -> SubtitleAnalysisResult:
        """Measure a frame set.  Pure local work (CPU); never raises."""

        started = time.perf_counter()
        if not self.settings.enabled:
            return SubtitleAnalysisResult(
                classification=SubtitleType.UNKNOWN,
                decision_source=SOURCE_UNAVAILABLE,
                engine=self.detector.name,
                unavailable_reason="subtitle_analysis.enabled = false",
            )
        cache_key = self.cache_key(cache_key)
        if cache_key and self.cache is not None:
            cached = self.cache.subtitle_cache_get(cache_key)
            if cached:
                cached["decision_source"] = cached.get("decision_source", SOURCE_LOCAL)
                result = SubtitleAnalysisResult.model_validate(cached)
                result.latency_ms = int((time.perf_counter() - started) * 1000)
                LOGGER.debug("subtitle analysis cache hit for %s", cache_key)
                return result
        if not recognize and not self.detector.recognizes_text:
            LOGGER.debug("detector %s cannot recognize text; geometry only", self.detector.name)
        usable, note = self.available, self.status_note
        if not usable:
            return SubtitleAnalysisResult(
                classification=SubtitleType.UNKNOWN,
                decision_source=SOURCE_UNAVAILABLE,
                engine=self.detector.name,
                unavailable_reason=note,
                frame_count=len(frames),
            )

        metrics: list[FrameTextMetrics] = []
        errors = 0
        for timestamp, path in frames:
            try:
                regions = self.detector.detect(path, recognize=recognize)
                metrics.append(
                    frame_metrics(
                        regions,
                        zones=self.settings.zones,
                        rules=self.settings.rules,
                        timestamp=timestamp,
                    )
                )
            except Exception as exc:
                errors += 1
                LOGGER.debug("text detection failed for %s: %s", path, exc)
        result = self.build_result(metrics, errors=errors, frame_budget=len(frames))
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        if cache_key and self.cache is not None and not result.is_unavailable:
            self.cache.subtitle_cache_put(cache_key, result.model_dump(mode="json"))
        return result

    async def analyze_frames(
        self,
        frames: Sequence[PreviewFrame],
        *,
        cache_key: str = "",
        recognize: bool = True,
        max_frames: int | None = None,
    ) -> SubtitleAnalysisResult:
        """Async wrapper: detection is CPU bound, so run it in a thread."""

        prepared: list[tuple[float, Path]] = [
            (float(frame.timestamp or 0.0), Path(frame.image_path))
            for frame in frames
            if getattr(frame, "image_path", None) is not None
        ]
        if not prepared:
            return SubtitleAnalysisResult(
                classification=SubtitleType.UNKNOWN,
                decision_source=SOURCE_UNAVAILABLE,
                engine=self.detector.name,
                unavailable_reason="no frames available for subtitle analysis",
            )
        limit = max(1, int(max_frames or self.settings.max_frames))
        prepared = prepared[:limit]
        return await asyncio.to_thread(
            self.analyze_paths, prepared, cache_key=cache_key, recognize=recognize
        )

    # -- classification ----------------------------------------------------
    def build_result(
        self,
        metrics: list[FrameTextMetrics],
        *,
        errors: int = 0,
        frame_budget: int | None = None,
    ) -> SubtitleAnalysisResult:
        """Classification + cleanliness from measured per-frame metrics."""

        if not metrics:
            # No frame could be measured: this is *not* a "worst case" verdict,
            # it means the measurement is unavailable and the pipeline must fall
            # back to the VLM subtitle judgement (section 39).
            return SubtitleAnalysisResult(
                classification=SubtitleType.UNKNOWN,
                decision_source=SOURCE_UNAVAILABLE,
                engine=self.detector.name,
                frame_count=0,
                unavailable_reason=(
                    f"no frame could be analysed ({errors} detection error(s))"
                    if errors
                    else "no frame could be analysed"
                ),
            )
        zones = self.settings.zones
        rules = self.settings.rules
        temporal = temporal_metrics(metrics, zones=zones, rules=rules)
        frame_count = len(metrics)
        avg_regions = round(sum(frame.region_count for frame in metrics) / frame_count, 3)
        max_regions = max(frame.region_count for frame in metrics)
        max_significant_regions = max(
            (frame.significant_region_count for frame in metrics), default=0
        )
        avg_area = round(
            sum(frame.total_text_area_ratio for frame in metrics) / frame_count, 5
        )
        largest = max(frame.largest_text_area_ratio for frame in metrics)
        zone_avgs = {
            ZONE_TOP: round(
                sum(frame.top_text_area_ratio for frame in metrics) / frame_count, 5
            ),
            ZONE_CENTER: round(
                sum(frame.center_text_area_ratio for frame in metrics) / frame_count, 5
            ),
            ZONE_BOTTOM: round(
                sum(frame.bottom_text_area_ratio for frame in metrics) / frame_count, 5
            ),
        }
        watermark_only = bool(
            all(
                frame.region_count == 0
                or frame.watermark_area_ratio >= frame.total_text_area_ratio - 1e-9
                for frame in metrics
            )
            and any(frame.region_count > 0 for frame in metrics)
        )
        promotion = sum(1 for frame in metrics if frame.promotion_text_detected) >= max(
            1, frame_count // 2
        )
        classification, evidence = self._classify(
            metrics,
            temporal=temporal,
            avg_area=avg_area,
            largest=largest,
            zone_avgs=zone_avgs,
            max_regions=max_regions,
            max_significant_regions=max_significant_regions,
            watermark_only=watermark_only,
            promotion=promotion,
        )
        cleanliness = self._cleanliness(
            classification=classification,
            temporal=temporal,
            avg_area=avg_area,
            zone_avgs=zone_avgs,
            watermark_only=watermark_only,
            promotion=promotion,
        )
        evidence = {
            **evidence,
            "detection_errors": errors,
            "frame_budget": frame_budget if frame_budget is not None else frame_count,
            "zones": {
                "center_start": zones.center_start,
                "bottom_start": zones.bottom_start,
                "band_min_width": zones.band_min_width,
            },
        }
        return SubtitleAnalysisResult(
            classification=classification,
            cleanliness_score=cleanliness,
            engine=self.detector.name,
            decision_source=SOURCE_LOCAL,
            frame_count=frame_count,
            text_presence_ratio=temporal["text_presence_ratio"],
            avg_text_regions=avg_regions,
            max_text_regions=max_regions,
            total_text_area_ratio_avg=avg_area,
            largest_text_area_ratio=largest,
            top_text_area_ratio_avg=zone_avgs[ZONE_TOP],
            center_text_area_ratio_avg=zone_avgs[ZONE_CENTER],
            bottom_text_area_ratio_avg=zone_avgs[ZONE_BOTTOM],
            bottom_persistence=temporal["bottom_persistence"],
            center_persistence=temporal["center_persistence"],
            large_text_persistence=temporal["large_text_persistence"],
            multi_region_persistence=temporal["multi_region_persistence"],
            band_persistence=temporal["band_persistence"],
            promotion_text_detected=promotion,
            watermark_only=watermark_only,
            evidence=evidence,
            frames=metrics,
        )

    def _classify(
        self,
        metrics: list[FrameTextMetrics],
        *,
        temporal: dict[str, float],
        avg_area: float,
        largest: float,
        zone_avgs: dict[str, float],
        max_regions: int,
        max_significant_regions: int,
        watermark_only: bool,
        promotion: bool,
    ) -> tuple[SubtitleType, dict[str, Any]]:
        """Deterministic classification (sections 12-18); first match wins."""

        rules = self.settings.rules
        evidence: dict[str, Any] = {
            "checks": [],
            "temporal": temporal,
            "avg_text_area_ratio": avg_area,
            "largest_text_area_ratio": largest,
            "zone_area_avgs": zone_avgs,
            "max_text_regions": max_regions,
        }
        if not any(frame.region_count > 0 for frame in metrics):
            evidence["checks"].append("no text region detected in any sampled frame")
            return SubtitleType.NONE, evidence
        if watermark_only:
            evidence["checks"].append("all detected text is a small corner watermark")
            return SubtitleType.WATERMARK_ONLY, evidence
        if promotion:
            evidence["checks"].append("promotional keywords detected in OCR text")
            return SubtitleType.PROMOTIONAL_OVERLAY, evidence
        if avg_area >= rules.dense_text_area:
            evidence["checks"].append(
                f"average text area {avg_area:.3f} >= dense threshold {rules.dense_text_area:.3f}"
            )
            return SubtitleType.DENSE_TEXT, evidence
        if (
            zone_avgs[ZONE_CENTER] >= rules.large_center_min_area
            and temporal["center_persistence"] >= rules.center_text_reject_persistence
        ):
            evidence["checks"].append(
                f"center zone covers {zone_avgs[ZONE_CENTER]:.3f} of the frame in "
                f"{temporal['center_persistence']:.0%} of frames"
            )
            return SubtitleType.LARGE_CENTER_TEXT, evidence
        if (
            temporal["band_persistence"] >= rules.band_min_persistence
            and temporal["multi_region_persistence"] >= rules.multi_region_min_persistence
        ):
            evidence["checks"].append(
                "persistent full-width text band together with other text zones"
            )
            return SubtitleType.COLORED_BLOCK, evidence
        if temporal["multi_region_persistence"] >= rules.multi_region_min_persistence:
            evidence["checks"].append(
                f"text in 2+ zones in {temporal['multi_region_persistence']:.0%} of frames"
            )
            return SubtitleType.MULTI_REGION, evidence
        if temporal["band_persistence"] >= rules.band_min_persistence:
            # a *thick* persistent full-width band is a caption bar, not a
            # subtitle line (thin wide lines never reach this branch)
            evidence["checks"].append(
                f"persistent thick full-width band in "
                f"{temporal['band_persistence']:.0%} of frames"
            )
            return SubtitleType.COLORED_BLOCK, evidence
        # zones that carry *significant* text (logos/watermarks excluded)
        zones_with_text = {zone for frame in metrics for zone in frame.zones}
        if not zones_with_text:
            zones_with_text = {zone for zone, area in zone_avgs.items() if area > 0}
        if zones_with_text == {ZONE_BOTTOM}:
            if (
                avg_area <= rules.simple_bottom_max_area
                and max_significant_regions <= rules.simple_bottom_max_regions
            ):
                evidence["checks"].append(
                    f"only bottom text, average area {avg_area:.3f} <= "
                    f"{rules.simple_bottom_max_area}, "
                    f"{max_significant_regions} line(s) at most"
                )
                return SubtitleType.BOTTOM_SIMPLE, evidence
            evidence["checks"].append(
                f"bottom text too large ({avg_area:.3f}) or too many lines "
                f"({max_significant_regions} > {rules.simple_bottom_max_regions})"
            )
            return SubtitleType.SINGLE_REGION, evidence
        if zones_with_text == {ZONE_TOP}:
            evidence["checks"].append("only top text")
            return SubtitleType.TOP_SIMPLE, evidence
        if max_regions <= 1:
            evidence["checks"].append("a single text region outside the simple zones")
            return SubtitleType.SINGLE_REGION, evidence
        evidence["checks"].append("text layout does not match a simple pattern")
        return SubtitleType.COMPLEX, evidence

    def _cleanliness(
        self,
        *,
        classification: SubtitleType,
        temporal: dict[str, float],
        avg_area: float,
        zone_avgs: dict[str, float],
        watermark_only: bool,
        promotion: bool,
    ) -> float:
        """Deterministic cleanliness score (section 20).

        ``1.0 - penalties`` where every penalty is a measured quantity times a
        documented weight; the LLM never contributes a number here.
        """

        weights = self.settings.weights
        rules = self.settings.rules
        if classification is SubtitleType.NONE:
            return 1.0
        if classification is SubtitleType.UNKNOWN:
            return 0.0
        penalty = 0.0
        if weights.center_text:
            center_factor = min(
                1.0, zone_avgs[ZONE_CENTER] / max(1e-6, rules.large_center_min_area)
            )
            penalty += weights.center_text * temporal["center_persistence"] * center_factor
        penalty += weights.multi_region * temporal["multi_region_persistence"]
        # the generic area penalty applies to text *outside* the mild bottom
        # band, otherwise a simple bottom subtitle would be penalised twice
        non_bottom_area = max(0.0, avg_area - zone_avgs[ZONE_BOTTOM])
        penalty += weights.text_area * min(
            1.0, non_bottom_area / max(1e-6, rules.dense_text_area)
        )
        penalty += weights.band * temporal["band_persistence"]
        penalty += weights.bottom_simple * temporal["bottom_persistence"]
        if not watermark_only:
            penalty += weights.top_simple * sum(
                1 for zone, area in zone_avgs.items() if zone == ZONE_TOP and area > 0
            )
        if classification is SubtitleType.DENSE_TEXT:
            penalty += weights.dense_text
        if watermark_only:
            penalty += weights.watermark
        if promotion:
            penalty += weights.promotion
        return round(max(0.0, min(1.0, 1.0 - penalty)), 3)

    # -- hybrid decision ---------------------------------------------------
    def local_confidence(self, result: SubtitleAnalysisResult) -> float:
        """How far the measurement sits from the decision boundaries.

        ``1.0`` = clearly one class, ``~0.5`` = right at a threshold (the hybrid
        policy asks the VLM in that case).
        """

        if result.is_unavailable or not result.frame_count:
            return 0.0
        rules = self.settings.rules
        if result.classification is SubtitleType.NONE:
            return 1.0 if result.max_text_regions == 0 else 0.5
        if result.classification is SubtitleType.BOTTOM_SIMPLE:
            margin = max(0.0, rules.simple_bottom_max_area - result.total_text_area_ratio_avg)
            return round(min(1.0, 0.5 + margin / max(1e-6, rules.simple_bottom_max_area)), 3)
        if result.classification is SubtitleType.LARGE_CENTER_TEXT:
            margin = result.center_text_area_ratio_avg - rules.large_center_min_area
            return round(min(1.0, 0.5 + margin / max(1e-6, rules.large_center_min_area)), 3)
        if result.classification in (SubtitleType.MULTI_REGION, SubtitleType.COLORED_BLOCK):
            margin = result.multi_region_persistence - rules.multi_region_min_persistence
            return round(min(1.0, 0.5 + margin / max(1e-6, 1 - rules.multi_region_min_persistence)), 3)
        if result.classification is SubtitleType.WATERMARK_ONLY:
            return 0.9
        return 0.5

    def needs_qwen(self, result: SubtitleAnalysisResult, *, margin: float = 0.15) -> bool:
        """Whether the local measurement alone is ambiguous (section 21)."""

        if not self.settings.hybrid_qwen:
            return False
        if result.is_unavailable:
            return True
        confidence = self.local_confidence(result)
        if result.classification in (
            SubtitleType.UNKNOWN,
            SubtitleType.COMPLEX,
            SubtitleType.SINGLE_REGION,
        ):
            return True
        return confidence <= 0.5 + margin


def combine_with_qwen(
    local: SubtitleAnalysisResult | None,
    *,
    qwen_type: SubtitleType | None,
    qwen_complexity: ComplexityLevel | None = None,
    accept: bool | None = None,
) -> SubtitleAnalysisResult:
    """Combine the measured result with the VLM's semantic opinion (sections 21-23).

    Rules, in order:

    1. no local measurement -> the VLM verdict is used (``qwen_fallback``)
    2. the local measurement is confident -> it wins (this is what removes the
       false "complex subtitle" rejections), and the VLM verdict is recorded as
       supporting/disagreeing evidence
    3. the local measurement is ambiguous -> the VLM verdict wins (``hybrid``)
    """

    if local is None or local.is_unavailable:
        if qwen_type is None:
            return local or SubtitleAnalysisResult(
                classification=SubtitleType.UNKNOWN,
                decision_source=SOURCE_UNAVAILABLE,
                unavailable_reason="no local measurement and no VLM verdict",
            )
        fallback = local.model_copy() if local is not None else SubtitleAnalysisResult()
        fallback.classification = qwen_type
        fallback.decision_source = SOURCE_QWEN
        fallback.evidence = {
            **dict(fallback.evidence),
            "qwen_type": str(qwen_type),
            "qwen_complexity": str(qwen_complexity) if qwen_complexity else "",
            "reason": "local subtitle analyzer unavailable",
        }
        if fallback.cleanliness_score <= 0.0:
            fallback.cleanliness_score = _cleanliness_from_complexity(qwen_complexity)
        return fallback

    combined = local.model_copy(deep=True)
    evidence = dict(combined.evidence)
    evidence["qwen_type"] = str(qwen_type) if qwen_type else ""
    evidence["qwen_complexity"] = str(qwen_complexity) if qwen_complexity else ""
    if qwen_type is not None:
        agrees = (
            (qwen_type in COMPLEX_CLASSES) == (local.classification in COMPLEX_CLASSES)
        )
        evidence["qwen_agrees"] = agrees
    if qwen_type is not None and local.classification in (
        SubtitleType.UNKNOWN,
        SubtitleType.COMPLEX,
        SubtitleType.SINGLE_REGION,
    ):
        combined.classification = qwen_type
        combined.decision_source = SOURCE_HYBRID
        if qwen_complexity is not None:
            measured = combined.cleanliness_score
            qwen_score = _cleanliness_from_complexity(qwen_complexity)
            combined.cleanliness_score = round((measured + qwen_score) / 2, 3)
    else:
        combined.decision_source = SOURCE_HYBRID if qwen_type is not None else SOURCE_LOCAL
    combined.evidence = evidence
    if accept is False and qwen_type is None:
        evidence["note"] = "VLM rejected the video; subtitle measurement is informational"
    return combined


def _cleanliness_from_complexity(complexity: ComplexityLevel | None) -> float:
    """Map the legacy VLM complexity label onto the 0..1 cleanliness scale."""

    return {
        ComplexityLevel.LOW: 0.85,
        ComplexityLevel.MEDIUM: 0.5,
        ComplexityLevel.HIGH: 0.15,
        ComplexityLevel.UNKNOWN: 0.5,
    }.get(complexity, 0.5)


def classification_bucket(classification: SubtitleType) -> str:
    """``clean`` / ``simple`` / ``complex`` / ``unknown`` for the reports."""

    if classification is SubtitleType.NONE or classification is SubtitleType.WATERMARK_ONLY:
        return "clean"
    if classification in (SubtitleType.BOTTOM_SIMPLE, SubtitleType.TOP_SIMPLE):
        return "simple"
    if classification in COMPLEX_CLASSES:
        return "complex"
    return "unknown"


#: measured classes that are hard rejects when the policy limit is exceeded
HARD_REJECT_CLASSES: frozenset[SubtitleType] = frozenset(
    {
        SubtitleType.LARGE_CENTER_TEXT,
        SubtitleType.MULTI_REGION,
        SubtitleType.COLORED_BLOCK,
        SubtitleType.PROMOTIONAL_OVERLAY,
        SubtitleType.DENSE_TEXT,
        SubtitleType.COMPLEX,
    }
)


def reject_reason_for(classification: SubtitleType):
    """Map a measured class onto the existing ``RejectReason`` vocabulary."""

    from core.models import RejectReason

    return {
        SubtitleType.LARGE_CENTER_TEXT: RejectReason.LARGE_CENTER_TEXT,
        SubtitleType.MULTI_REGION: RejectReason.MULTI_REGION_SUBTITLE,
        SubtitleType.COLORED_BLOCK: RejectReason.COLORED_TEXT_BLOCK,
        SubtitleType.PROMOTIONAL_OVERLAY: RejectReason.COLORED_TEXT_BLOCK,
        SubtitleType.DENSE_TEXT: RejectReason.MULTI_REGION_SUBTITLE,
        SubtitleType.COMPLEX: RejectReason.SUBTITLE_TOO_COMPLEX,
        SubtitleType.SINGLE_REGION: RejectReason.SUBTITLE_TOO_COMPLEX,
    }.get(classification)


def analyzer_from_settings(config: Any | None) -> SubtitleAnalyzer:
    """Build an analyzer from ``AppSettings.subtitle_analysis``."""

    from analyzers.subtitle_settings import build_subtitle_settings

    return SubtitleAnalyzer(build_subtitle_settings(config))
