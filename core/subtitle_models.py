"""Measurable subtitle/text models (Milestone 6).

The point of this milestone is to replace "the VLM says subtitle_too_complex"
with numbers:

* where the text is (normalized boxes + screen zones)
* how much of the frame it covers
* how many independent regions exist
* how long those regions persist across sampled frames

Everything here is plain data - no I/O, no OCR dependency - so the geometry,
classification and cleanliness rules can be unit tested with synthetic boxes.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, computed_field

from core.models import SubtitleType

ANALYSIS_VERSION = "subtitle_analysis_v1"

#: long digit runs (phone numbers, account ids) are redacted before OCR text is
#: persisted or logged (section 37); the promotion keywords are unaffected
_PRIVATE_DIGITS = re.compile(r"\d{7,}")


def redact_text(value: str) -> str:
    """Mask phone-number-like digit runs in recognized text."""

    return _PRIVATE_DIGITS.sub("<num>", value or "")

#: where a region sits on the screen
ZONE_TOP = "top"
ZONE_CENTER = "center"
ZONE_BOTTOM = "bottom"

#: how the final classification was obtained (section 22)
SOURCE_LOCAL = "local"
SOURCE_HYBRID = "hybrid"
SOURCE_QWEN = "qwen_fallback"
SOURCE_UNAVAILABLE = "unavailable"


class TextRegion(BaseModel):
    """One detected text box in **normalized** coordinates (section 7)."""

    model_config = ConfigDict(extra="ignore")

    x1: float = Field(ge=0.0, le=1.0)
    y1: float = Field(ge=0.0, le=1.0)
    x2: float = Field(ge=0.0, le=1.0)
    y2: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    text: str = ""
    ocr_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    engine: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def width_ratio(self) -> float:
        return round(max(0.0, self.x2 - self.x1), 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def height_ratio(self) -> float:
        return round(max(0.0, self.y2 - self.y1), 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def area_ratio(self) -> float:
        return round(self.width_ratio * self.height_ratio, 5)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def center_x(self) -> float:
        return round((self.x1 + self.x2) / 2, 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def center_y(self) -> float:
        return round((self.y1 + self.y2) / 2, 4)

    def zone(self, *, center_start: float = 0.25, bottom_start: float = 0.72) -> str:
        """Screen zone of the region centre (section 8)."""

        if self.center_y < center_start:
            return ZONE_TOP
        if self.center_y < bottom_start:
            return ZONE_CENTER
        return ZONE_BOTTOM

    def is_corner_watermark(self, *, max_area: float = 0.02, margin: float = 0.18) -> bool:
        """Small box hugging a corner: a watermark rather than a subtitle."""

        if self.area_ratio > max_area:
            return False
        corner_x = self.x2 <= margin or self.x1 >= 1 - margin
        corner_y = self.y2 <= margin or self.y1 >= 1 - margin
        return bool(corner_x and corner_y)

    def iou(self, other: "TextRegion") -> float:
        left = max(self.x1, other.x1)
        top = max(self.y1, other.y1)
        right = min(self.x2, other.x2)
        bottom = min(self.y2, other.y2)
        if right <= left or bottom <= top:
            return 0.0
        intersection = (right - left) * (bottom - top)
        union = self.area_ratio + other.area_ratio - intersection
        return round(intersection / union, 4) if union > 0 else 0.0

    def center_distance(self, other: "TextRegion") -> float:
        return round(
            ((self.center_x - other.center_x) ** 2 + (self.center_y - other.center_y) ** 2)
            ** 0.5,
            4,
        )


class FrameTextMetrics(BaseModel):
    """Per-frame measurements (section 9)."""

    model_config = ConfigDict(extra="ignore")

    timestamp: float = 0.0
    region_count: int = 0
    #: regions big enough to be text (excludes logos/watermarks)
    significant_region_count: int = 0
    total_text_area_ratio: float = 0.0
    largest_text_area_ratio: float = 0.0
    top_text_area_ratio: float = 0.0
    center_text_area_ratio: float = 0.0
    bottom_text_area_ratio: float = 0.0
    full_width_band_count: int = 0
    recognized_character_count: int = 0
    promotion_text_detected: bool = False
    watermark_area_ratio: float = 0.0
    regions: list[TextRegion] = Field(default_factory=list)
    #: zones of *significant* regions (tiny logos/watermarks excluded)
    zones: list[str] = Field(default_factory=list)

    @property
    def zones_with_text(self) -> set[str]:
        if self.zones:
            return set(self.zones)
        return {region.zone() for region in self.regions}


class SubtitleAnalysisResult(BaseModel):
    """Structured, explainable subtitle verdict (section 19)."""

    model_config = ConfigDict(extra="ignore")

    classification: SubtitleType = SubtitleType.UNKNOWN
    cleanliness_score: float = Field(default=0.0, ge=0.0, le=1.0)
    analysis_version: str = ANALYSIS_VERSION
    decision_source: str = SOURCE_LOCAL
    engine: str = ""

    frame_count: int = 0
    text_presence_ratio: float = 0.0
    avg_text_regions: float = 0.0
    max_text_regions: int = 0
    total_text_area_ratio_avg: float = 0.0
    largest_text_area_ratio: float = 0.0
    top_text_area_ratio_avg: float = 0.0
    center_text_area_ratio_avg: float = 0.0
    bottom_text_area_ratio_avg: float = 0.0

    bottom_persistence: float = 0.0
    center_persistence: float = 0.0
    large_text_persistence: float = 0.0
    multi_region_persistence: float = 0.0
    band_persistence: float = 0.0

    promotion_text_detected: bool = False
    watermark_only: bool = False

    latency_ms: int | None = None
    unavailable_reason: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    frames: list[FrameTextMetrics] = Field(default_factory=list)

    @property
    def is_unavailable(self) -> bool:
        return self.decision_source == SOURCE_UNAVAILABLE

    def summary(self) -> str:
        return (
            f"{self.classification} (cleanliness={self.cleanliness_score:.2f}, "
            f"source={self.decision_source}, frames={self.frame_count}, "
            f"regions_avg={self.avg_text_regions:.1f}, area_avg="
            f"{self.total_text_area_ratio_avg:.3f})"
        )


class SubtitleZoneSettings(BaseModel):
    """Zone boundaries (section 8) - configurable, not hardcoded."""

    model_config = ConfigDict(extra="ignore")

    center_start: float = 0.25
    bottom_start: float = 0.72
    #: a region spanning at least this much width counts as a "band"
    band_min_width: float = 0.70
    #: ...and at least this much height: a thin wide line is a normal subtitle
    #: line, not a coloured caption bar
    band_min_height: float = 0.08


class SubtitleRuleSettings(BaseModel):
    """Measurable thresholds for the classification rules (sections 12-18/24)."""

    model_config = ConfigDict(extra="ignore")

    #: small bottom subtitle may cover at most this much of the frame
    simple_bottom_max_area: float = 0.12
    #: multi-line bottom subtitles are still "simple" up to this many regions
    simple_bottom_max_regions: int = 3
    #: a center region at least this large can be "large center text"
    large_center_min_area: float = 0.10
    #: text covering this much of the frame is "dense"
    dense_text_area: float = 0.25
    #: persistence needed before a zone counts as "persistent"
    multi_region_min_persistence: float = 0.40
    center_text_reject_persistence: float = 0.50
    large_text_min_persistence: float = 0.50
    band_min_persistence: float = 0.40
    #: corner watermark limits
    watermark_max_area: float = 0.02
    watermark_margin: float = 0.18
    #: a big square-ish box is probably a QR code / logo block
    qr_min_area: float = 0.03
    #: regions smaller than this (logos, watermarks) never create a text zone
    ignore_small_area: float = 0.02


class SubtitleCleanlinessWeights(BaseModel):
    """Documented, configurable penalties (section 20)."""

    model_config = ConfigDict(extra="ignore")

    center_text: float = 0.45
    multi_region: float = 0.35
    text_area: float = 0.30
    band: float = 0.25
    dense_text: float = 0.20
    bottom_simple: float = 0.08
    top_simple: float = 0.06
    watermark: float = 0.02
    promotion: float = 0.25

    def total(self) -> float:
        return sum(abs(value) for value in self.model_dump().values())


#: promotional words used as *supporting* evidence (never the only signal)
PROMOTION_KEYWORDS: tuple[str, ...] = (
    "价格",
    "优惠",
    "特价",
    "私信",
    "咨询",
    "联系电话",
    "电话",
    "微信",
    "下单",
    "购买",
    "厂家",
    "扫码",
    "批发",
    "包邮",
    "限时",
)


def region_zone_totals(
    regions: Iterable[TextRegion], *, zones: SubtitleZoneSettings
) -> dict[str, float]:
    """Summed text area per screen zone."""

    totals = {ZONE_TOP: 0.0, ZONE_CENTER: 0.0, ZONE_BOTTOM: 0.0}
    for region in regions:
        totals[
            region.zone(
                center_start=zones.center_start, bottom_start=zones.bottom_start
            )
        ] += region.area_ratio
    return {key: round(value, 5) for key, value in totals.items()}


def frame_metrics(
    regions: Iterable[TextRegion],
    *,
    zones: SubtitleZoneSettings,
    rules: SubtitleRuleSettings,
    timestamp: float = 0.0,
    keywords: tuple[str, ...] = PROMOTION_KEYWORDS,
) -> FrameTextMetrics:
    """Compute the per-frame metrics of section 9 from detected regions."""

    region_list = list(regions)
    zone_totals = region_zone_totals(region_list, zones=zones)
    watermark_area = sum(
        region.area_ratio
        for region in region_list
        if region.is_corner_watermark(
            max_area=rules.watermark_max_area, margin=rules.watermark_margin
        )
    )
    bands = [
        region
        for region in region_list
        if region.width_ratio >= zones.band_min_width
        and region.height_ratio >= zones.band_min_height
    ]
    significant_zones: list[str] = []
    significant_regions = 0
    for region in region_list:
        if region.area_ratio <= rules.ignore_small_area:
            continue  # logo / watermark: never counts as a text zone
        significant_regions += 1
        zone = region.zone(
            center_start=zones.center_start, bottom_start=zones.bottom_start
        )
        if zone not in significant_zones:
            significant_zones.append(zone)
    recognized = "".join(region.text for region in region_list if region.text)
    promotion = any(keyword in recognized for keyword in keywords)
    # recognized text is stored for evidence/debugging: mask private digit runs
    for region in region_list:
        if region.text:
            region.text = redact_text(region.text)
    return FrameTextMetrics(
        timestamp=round(timestamp, 3),
        region_count=len(region_list),
        significant_region_count=significant_regions,
        total_text_area_ratio=round(sum(region.area_ratio for region in region_list), 5),
        largest_text_area_ratio=round(
            max((region.area_ratio for region in region_list), default=0.0), 5
        ),
        top_text_area_ratio=zone_totals[ZONE_TOP],
        center_text_area_ratio=zone_totals[ZONE_CENTER],
        bottom_text_area_ratio=zone_totals[ZONE_BOTTOM],
        full_width_band_count=len(bands),
        recognized_character_count=len(recognized),
        promotion_text_detected=promotion,
        watermark_area_ratio=round(watermark_area, 5),
        regions=region_list,
        zones=significant_zones,
    )


def temporal_metrics(
    frames: list[FrameTextMetrics],
    *,
    zones: SubtitleZoneSettings,
    rules: SubtitleRuleSettings,
) -> dict[str, float]:
    """Persistence ratios across the sampled frames (sections 10/11)."""

    total = len(frames)
    if not total:
        return {
            "text_presence_ratio": 0.0,
            "bottom_persistence": 0.0,
            "center_persistence": 0.0,
            "large_text_persistence": 0.0,
            "multi_region_persistence": 0.0,
            "band_persistence": 0.0,
        }

    def ratio(count: int) -> float:
        return round(count / total, 4)

    presence = sum(1 for frame in frames if frame.region_count > 0)
    bottom = sum(1 for frame in frames if frame.bottom_text_area_ratio > 0)
    center = sum(
        1
        for frame in frames
        if frame.center_text_area_ratio >= rules.large_center_min_area
    )
    large = sum(
        1
        for frame in frames
        if frame.largest_text_area_ratio >= rules.large_center_min_area
    )
    multi = sum(1 for frame in frames if len(frame.zones_with_text) >= 2)
    band = sum(1 for frame in frames if frame.full_width_band_count > 0)
    return {
        "text_presence_ratio": ratio(presence),
        "bottom_persistence": ratio(bottom),
        "center_persistence": ratio(center),
        "large_text_persistence": ratio(large),
        "multi_region_persistence": ratio(multi),
        "band_persistence": ratio(band),
    }


def match_region_to_previous(
    region: TextRegion,
    previous: Iterable[TextRegion],
    *,
    min_iou: float = 0.3,
    max_center_distance: float = 0.08,
) -> TextRegion | None:
    """Cheap cross-frame matching (section 11): IoU or centre distance.

    Approximate on purpose - no optical flow, no tracker library.
    """

    best: TextRegion | None = None
    best_iou = 0.0
    for candidate in previous:
        iou = region.iou(candidate)
        if iou >= min_iou and iou > best_iou:
            best, best_iou = candidate, iou
            continue
        if iou < min_iou and region.center_distance(candidate) <= max_center_distance:
            if best is None:
                best = candidate
    return best


def result_from_mapping(payload: Mapping[str, Any] | None) -> SubtitleAnalysisResult | None:
    """Tolerant parse of a stored ``subtitle_analysis_json`` value."""

    if not payload:
        return None
    try:
        return SubtitleAnalysisResult.model_validate(dict(payload))
    except Exception:  # pragma: no cover - defensive
        return None
