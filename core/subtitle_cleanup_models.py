"""Conservative local subtitle-cleanup models (Milestone 9.2).

This module is deliberately free of I/O and of the pipeline/storage layers so
the geometry rules can be unit tested with synthetic boxes.  The cleanup
service, the SQLite library and ``config.yaml`` all share these definitions.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

#: explicit algorithm version; a future cleanup algorithm must use a new value
CLEANUP_VERSION = "subtitle_cleanup_v1"


class CleanupStatus(StrEnum):
    """Settled outcomes of one cleanup attempt."""

    NOT_NEEDED = "not_needed"
    INELIGIBLE = "ineligible"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED_PROCESSING = "failed_processing"
    FAILED_QUALITY = "failed_quality"
    RESIDUAL_SUBTITLE = "residual_subtitle"
    DERIVATIVE_DELETED = "derivative_deleted"


class CleanupReviewStatus(StrEnum):
    """Human review state of a cleanup derivative (Milestone 9.3)."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


#: controlled vocabulary for a human rejection (never an automatic rule)
REVIEW_FAILURE_CLASSES: tuple[str, ...] = (
    "residual_subtitle",
    "visible_blur_patch",
    "content_removed",
    "flicker",
    "mask_too_large",
    "wrong_region",
    "timing_mismatch",
    "other",
)


#: conservative classes the operator allows to be attempted
DEFAULT_ELIGIBLE_CLASSES: tuple[str, ...] = (
    "bottom_simple",
    "top_simple",
    "single_region",
)


class SubtitleCleanupConfig(BaseModel):
    """Effective cleanup settings (``subtitle_cleanup:`` in config.yaml).

    Every business threshold lives here; SQL and the UI never hardcode one.
    The defaults are intentionally conservative: the design priority is
    "preserve usable footage" over "remove subtitles".
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    version: str = CLEANUP_VERSION
    #: only these measured subtitle classes are candidates
    eligible_classes: list[str] = Field(default_factory=lambda: list(DEFAULT_ELIGIBLE_CLASSES))

    # -- higher-frequency local OCR geometry pass -------------------------
    sample_fps: float = 2.0
    max_samples: int = 240
    min_samples: int = 2
    min_confidence: float = 0.30

    # -- temporal track association ---------------------------------------
    min_iou: float = 0.20
    max_center_distance: float = 0.10
    #: a track must cover at least this share of the sampled frames
    min_persistence: float = 0.55
    min_track_seconds: float = 0.30
    #: maximum vertical wobble of the track centre (normalized)
    max_vertical_jitter: float = 0.05
    #: envelope may not grow more than this factor beyond the median box
    max_union_growth: float = 0.75
    max_union_area_ratio: float = 0.15
    max_region_area_ratio: float = 0.10

    # -- mask geometry safety ---------------------------------------------
    margin_ratio: float = 0.02
    min_margin_pixels: int = 4
    max_width_ratio: float = 0.90
    max_height_ratio: float = 0.30
    max_total_area_ratio: float = 0.20
    max_regions: int = 3

    # -- paid-cloud preflight ---------------------------------------------
    #: Refuse cloud submission when a persistent, near-solid rectangular
    #: backing plate surrounds the detected text. Such pixels already hide
    #: the scene, so subtitle erasure cannot reconstruct original detail.
    backing_block_dominant_ratio_min: float = 0.50
    backing_block_ring_ratio_max: float = 0.30
    backing_block_min_persistence: float = 0.50

    # -- temporal behavior -------------------------------------------------
    time_scoped: bool = True
    #: extend each active interval by this much before/after the observation
    padding_seconds: float = 0.20

    # -- deterministic local engine ---------------------------------------
    engine: str = "ffmpeg_delogo"
    #: OpenCV Telea inpainting radius used by subtitle_cleanup_v2.
    inpaint_radius: float = 3.0

    # -- Milestone 9.3 production workflow --------------------------------
    #: an automatically generated derivative never replaces the original in
    #: downstream editing/export until a human approves it
    require_review_before_preferred: bool = True
    #: best-effort static HTML/JPEG review pack after each successful cleanup
    review_pack: bool = True
    reports_dir: Path = Path("reports")
    review_pack_ratios: list[float] = Field(default_factory=lambda: [0.2, 0.5, 0.8])

    # -- Milestone 9.4 post-acquisition routing ---------------------------
    #: classify every newly accepted clip against subtitle_cleanup_v1; this is
    #: metadata-only and never runs OCR/FFmpeg by itself
    post_acquisition_routing: bool = True
    #: destructive cleanup on newly acquired clips is opt-in per run
    post_acquisition_auto_cleanup: bool = False
    #: hard ceiling for cleanup attempts on new clips in one acquisition run
    post_acquisition_max_cleanup: int = 2

    # -- post-cleanup validation / quality guard --------------------------
    min_evidence_reduction: float = 0.50
    duration_tolerance_seconds: float = 0.75
    resolution_tolerance_pixels: int = 2
    outside_mean_diff_max: float = 12.0
    blur_ratio_min: float = 0.05
    #: a cleaned rectangle must keep at least this share of the local texture
    #: variance; losing more than that is treated as a gross blur block
    region_std_ratio_min: float = 0.35
    #: inpainting must preserve local detail in both image directions.  A
    #: collapsed direction usually means pixels were stretched into a visible
    #: horizontal/vertical smear even when the crop still has high variance.
    directional_gradient_ratio_min: float = 0.20
    #: Reject only when directional collapse is persistent across the sampled
    #: mask regions. A single low-texture frame can legitimately lose the
    #: subtitle glyph edges without damaging its already-blurred background.
    directional_smear_fraction_max: float = 0.20
    flat_region_edge_std_min: float = 1.0
    dark_region_mean_max: float = 20.0
    dark_region_std_max: float = 6.0

    def eligible(self, classification: str) -> bool:
        return str(classification) in {str(item) for item in self.eligible_classes}


class TrackRecord(BaseModel):
    """One text region followed across the sampled frames.

    Text content is deliberately not part of the association key: subtitles
    change wording while keeping the same screen geometry.
    """

    model_config = ConfigDict(extra="ignore")

    first_seen: float = 0.0
    last_seen: float = 0.0
    sample_count: int = 0
    total_samples: int = 0
    persistence_ratio: float = 0.0

    median_x1: float = 0.0
    median_y1: float = 0.0
    median_x2: float = 0.0
    median_y2: float = 0.0

    union_x1: float = 0.0
    union_y1: float = 0.0
    union_x2: float = 0.0
    union_y2: float = 0.0

    vertical_jitter: float = 0.0
    center_jitter: float = 0.0
    union_growth: float = 0.0
    median_area_ratio: float = 0.0
    union_area_ratio: float = 0.0

    active_intervals: list[list[float]] = Field(default_factory=list)
    texts: list[str] = Field(default_factory=list)

    @property
    def median_box(self) -> tuple[float, float, float, float]:
        return (self.median_x1, self.median_y1, self.median_x2, self.median_y2)

    @property
    def union_box(self) -> tuple[float, float, float, float]:
        return (self.union_x1, self.union_y1, self.union_x2, self.union_y2)

class CleanupMask(BaseModel):
    """Smallest safe rectangle to clean, in normalized coordinates."""

    model_config = ConfigDict(extra="ignore")

    x1: float = Field(ge=0.0, le=1.0)
    y1: float = Field(ge=0.0, le=1.0)
    x2: float = Field(ge=0.0, le=1.0)
    y2: float = Field(ge=0.0, le=1.0)
    persistence_ratio: float = 0.0
    sample_count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    active_intervals: list[list[float]] = Field(default_factory=list)
    reason: str = ""

    @property
    def width_ratio(self) -> float:
        return round(max(0.0, self.x2 - self.x1), 5)

    @property
    def height_ratio(self) -> float:
        return round(max(0.0, self.y2 - self.y1), 5)

    @property
    def area_ratio(self) -> float:
        return round(self.width_ratio * self.height_ratio, 5)

    def pixel_box(
        self,
        width: int,
        height: int,
        *,
        min_size: int = 2,
    ) -> tuple[int, int, int, int]:
        """Integer ``(x, y, w, h)`` clamped to the real frame dimensions."""

        x = int(round(self.x1 * width))
        y = int(round(self.y1 * height))
        right = int(round(self.x2 * width))
        bottom = int(round(self.y2 * height))
        x = max(0, min(width - 1, x))
        y = max(0, min(height - 1, y))
        right = max(x + min_size, min(width, right))
        bottom = max(y + min_size, min(height, bottom))
        return x, y, right - x, bottom - y
