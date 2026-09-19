"""Pydantic domain models shared by every layer of the agent.

These models are the contract between the sources, the AI providers, the
media toolkit, the storage layer and the UI.  Every value that crosses a
module boundary is validated here so a malformed AI response or a broken
platform response can never silently poison the library.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    """Timezone aware ``now`` used for every persisted timestamp."""

    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enumerations (the controlled vocabulary of the clip library)
# ---------------------------------------------------------------------------
class MaterialForm(StrEnum):
    WHOLE = "whole"
    SLICE = "slice"
    PIECE = "piece"
    STRIP = "strip"
    POWDER = "powder"
    GRANULE = "granule"
    LEAF = "leaf"
    UNKNOWN = "unknown"


class MaterialState(StrEnum):
    FRESH = "fresh"
    PREPARED = "prepared"
    SEMI_DRIED = "semi_dried"
    DRYING = "drying"
    DRIED = "dried"
    FINISHED = "finished"
    UNKNOWN = "unknown"


class ProcessStage(StrEnum):
    RAW_MATERIAL = "raw_material"
    PREPARATION = "preparation"
    WASHING = "washing"
    CUTTING = "cutting"
    LOADING = "loading"
    TRAY_ARRANGEMENT = "tray_arrangement"
    BEFORE_DRYING = "before_drying"
    DRYING = "drying"
    INSIDE_DRYER = "inside_dryer"
    UNLOADING = "unloading"
    FINISHED_PRODUCT = "finished_product"
    PACKAGING = "packaging"
    EQUIPMENT = "equipment"
    FACTORY = "factory"
    OTHER = "other"


class ShotType(StrEnum):
    WIDE = "wide"
    MEDIUM = "medium"
    CLOSE_UP = "close_up"
    MACRO = "macro"
    DETAIL = "detail"
    UNKNOWN = "unknown"


class CameraMotion(StrEnum):
    STATIC = "static"
    PAN = "pan"
    TILT = "tilt"
    ZOOM_IN = "zoom_in"
    ZOOM_OUT = "zoom_out"
    TRACKING = "tracking"
    HANDHELD = "handheld"
    UNKNOWN = "unknown"


class PersonRole(StrEnum):
    NONE = "none"
    WORKER = "worker"
    HOST = "host"
    CUSTOMER = "customer"
    MULTIPLE_PEOPLE = "multiple_people"
    UNKNOWN = "unknown"


class SubtitleType(StrEnum):
    NONE = "none"
    BOTTOM_SIMPLE = "bottom_simple"
    TOP_SIMPLE = "top_simple"
    SINGLE_REGION = "single_region"
    MULTI_REGION = "multi_region"
    COLORED_BLOCK = "colored_block"
    COMPLEX = "complex"
    UNKNOWN = "unknown"
    # Milestone 6: measured classes produced by local text detection.  The
    # values above are unchanged so historical rows keep their meaning.
    WATERMARK_ONLY = "watermark_only"
    LARGE_CENTER_TEXT = "large_center_text"
    PROMOTIONAL_OVERLAY = "promotional_overlay"
    DENSE_TEXT = "dense_text"


class EditRole(StrEnum):
    HOOK = "hook"
    INTRO = "intro"
    RAW_MATERIAL = "raw_material"
    PROCESS = "process"
    DETAIL = "detail"
    EQUIPMENT = "equipment"
    TRANSITION = "transition"
    RESULT = "result"
    ENDING = "ending"


class ComplexityLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class SubtitlePolicy(StrEnum):
    """How aggressively clips carrying subtitles are rejected."""

    OFF = "off"
    LOOSE = "loose"
    BALANCED = "balanced"
    STRICT = "strict"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReviewStatus(StrEnum):
    """Human review state of one clip (Milestone 4, section 10).

    Deliberately separate from the AI tags/scores: reviewing a clip never
    rewrites what the model observed.
    """

    UNREVIEWED = "unreviewed"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"

    @property
    def label(self) -> str:
        return REVIEW_STATUS_LABELS[self]


REVIEW_STATUS_LABELS: dict[ReviewStatus, str] = {
    ReviewStatus.UNREVIEWED: "未审核",
    ReviewStatus.APPROVED: "已批准",
    ReviewStatus.REJECTED: "已拒绝",
    ReviewStatus.NEEDS_REVIEW: "需要复核",
}

#: label -> status, for Gradio dropdowns
REVIEW_STATUS_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (status.label, status.value) for status in ReviewStatus
)


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


class SourceVideoStatus(StrEnum):
    # acquisition lifecycle (Milestone 3)
    DISCOVERED = "discovered"
    METADATA_CHECKED = "metadata_checked"
    PREVIEWING = "previewing"
    REJECTED_PREVIEW = "rejected_preview"
    QUALIFIED = "qualified"
    DOWNLOADING = "downloading"
    ANALYZING = "analyzing"
    # terminal / failure states
    CANDIDATE = "candidate"
    REJECTED = "rejected"
    DOWNLOADED = "downloaded"
    ANALYZED = "analyzed"
    NO_USABLE_SEGMENT = "no_usable_segment"
    PROCESSED = "processed"
    FAILED = "failed"
    FAILED_SEARCH = "failed_search"
    FAILED_PREVIEW = "failed_preview"
    FAILED_DOWNLOAD = "failed_download"
    FAILED_AI = "failed_ai"
    FAILED_MEDIA = "failed_media"
    SKIPPED_DUPLICATE = "skipped_duplicate"


class RejectReason(StrEnum):
    """Stable reason codes; shared by the local filter and the AI pre-filter."""

    NO_MATERIAL = "no_material"
    UNUSABLE_MEDIA = "unusable_media"
    MULTI_REGION_SUBTITLE = "multi_region_subtitle"
    COLORED_TEXT_BLOCK = "colored_text_block"
    LARGE_CENTER_TEXT = "large_center_text"
    SUBTITLE_TOO_COMPLEX = "subtitle_too_complex"
    SPLIT_SCREEN = "split_screen"
    PICTURE_IN_PICTURE = "picture_in_picture"
    TOO_MANY_STICKERS = "too_many_stickers"
    LOW_QUALITY = "low_quality"
    HEAVY_PERSON_OCCLUSION = "heavy_person_occlusion"
    SEVERE_CAMERA_SHAKE = "severe_camera_shake"
    UNUSABLE_VISUAL = "unusable_visual"
    SEGMENT_TOO_SHORT = "segment_too_short"
    DURATION_OUT_OF_RANGE = "duration_out_of_range"
    #: Milestone 9.1: the upstream discovery metadata carried no duration.
    #: This is *not* a content rejection - it triggers a media metadata probe.
    DURATION_UNKNOWN = "duration_unknown"
    #: the probe could not establish a trustworthy duration either
    DURATION_UNKNOWN_UNRESOLVED = "duration_unknown_unresolved"
    #: the "media" URL actually returned a web page / non-video payload
    INVALID_MEDIA_SOURCE = "invalid_media_source"
    DUPLICATE_URL = "duplicate_url"
    DUPLICATE_VIDEO = "duplicate_video"
    DUPLICATE_CLIP = "duplicate_clip"
    ALREADY_PROCESSED = "already_processed"
    UNREACHABLE = "unreachable"
    CORRUPT_MEDIA = "corrupt_media"
    TITLE_IRRELEVANT = "title_irrelevant"
    NO_USABLE_SEGMENT = "no_usable_segment"
    QUALITY_GATE = "quality_gate"
    TARGET_STAGE_MISMATCH = "target_stage_mismatch"
    OTHER = "other"

    # -- reason precedence (Milestone 3.6) ---------------------------------
    #: reasons that describe a real, content/media outcome of processing this
    #: video.  They are the *truth* about the video and must never be
    #: overwritten by a later discovery bookkeeping event.
    @classmethod
    def terminal_reasons(cls) -> frozenset["RejectReason"]:
        return frozenset(
            {
                cls.NO_MATERIAL,
                cls.MULTI_REGION_SUBTITLE,
                cls.COLORED_TEXT_BLOCK,
                cls.LARGE_CENTER_TEXT,
                cls.SUBTITLE_TOO_COMPLEX,
                cls.SPLIT_SCREEN,
                cls.PICTURE_IN_PICTURE,
                cls.TOO_MANY_STICKERS,
                cls.LOW_QUALITY,
                cls.HEAVY_PERSON_OCCLUSION,
                cls.SEVERE_CAMERA_SHAKE,
                cls.UNUSABLE_VISUAL,
                cls.UNUSABLE_MEDIA,
                cls.CORRUPT_MEDIA,
                cls.NO_USABLE_SEGMENT,
                cls.FAILED_DOWNLOAD,
                cls.FAILED_AI,
                cls.FAILED_MEDIA,
                cls.SEGMENT_TOO_SHORT,
                cls.QUALITY_GATE,
                cls.TITLE_IRRELEVANT,
                cls.DURATION_OUT_OF_RANGE,
                cls.UNREACHABLE,
                cls.OTHER,
            }
        )

    #: reasons that only record *how* we met the video again (discovery
    #: bookkeeping).  They must never replace a terminal content reason.
    @classmethod
    def bookkeeping_reasons(cls) -> frozenset["RejectReason"]:
        return frozenset(
            {
                cls.DUPLICATE_VIDEO,
                cls.DUPLICATE_URL,
                cls.DUPLICATE_CLIP,
                cls.ALREADY_PROCESSED,
            }
        )


# ---------------------------------------------------------------------------
# Source layer models
# ---------------------------------------------------------------------------
class VideoCandidate(BaseModel):
    """A lightweight search hit, produced by ``VideoSource.search``."""

    model_config = ConfigDict(extra="forbid")

    platform: str
    platform_video_id: str
    source_url: str
    title: str = ""
    author: str = ""
    duration: float | None = Field(default=None, ge=0)
    cover_url: str | None = None
    published_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: every search query that surfaced this candidate (section 12)
    matched_queries: list[str] = Field(default_factory=list)
    author_id: str | None = None
    #: direct media stream URL when the source already knows it
    media_url: str | None = None
    statistics: dict[str, Any] = Field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        return f"{self.platform}:{self.platform_video_id}"

    @property
    def normalized_url(self) -> str:
        """URL used for global candidate deduplication."""

        url = (self.source_url or "").strip()
        if not url:
            return ""
        # drop query/fragment noise so identical posts collapse
        return url.split("?", 1)[0].split("#", 1)[0].rstrip("/")


class VideoInfo(BaseModel):
    """Metadata of a concrete video, produced by ``VideoSource.get_video_info``."""

    platform: str
    platform_video_id: str
    source_url: str
    title: str = ""
    author: str = ""
    description: str = ""
    duration: float | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    fps: float | None = Field(default=None, ge=0)
    cover_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PreviewFrame(BaseModel):
    """A still image sampled from a video, always carrying its timestamp."""

    timestamp: float = Field(ge=0)
    image_path: Path | None = None
    source: str = "sampled"


class PreviewSource(BaseModel):
    """Cheap preview material used by the AI pre-filter (before downloading)."""

    platform: str
    platform_video_id: str
    duration: float | None = Field(default=None, ge=0)
    frames: list[PreviewFrame] = Field(default_factory=list)
    video_path: Path | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# AI layer models
# ---------------------------------------------------------------------------
class PreviewFilterResult(BaseModel):
    """Structured verdict of the AI pre-filter (see prompt ``preview_filter``)."""

    model_config = ConfigDict(extra="forbid")

    accept: bool
    material_visible: bool
    material_relevance: float = Field(ge=0.0, le=1.0)
    subtitle_complexity: ComplexityLevel
    visual_complexity: ComplexityLevel
    quality_score: float = Field(ge=0.0, le=1.0)
    reject_reason: RejectReason | None = None

    @model_validator(mode="after")
    def _reject_reason_when_rejected(self) -> PreviewFilterResult:
        if not self.accept and self.reject_reason is None:
            # Tolerated, but normalised to a generic code so counters stay sane.
            self.reject_reason = RejectReason.OTHER
        return self


class DetectedSegment(BaseModel):
    """A candidate time range returned by ``detect_segments``."""

    model_config = ConfigDict(extra="forbid")

    start: float = Field(ge=0)
    end: float = Field(gt=0)
    description: str = ""
    material_relevance: float = Field(default=0.0, ge=0.0, le=1.0)
    usable: bool = True

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @model_validator(mode="after")
    def _end_after_start(self) -> DetectedSegment:
        if self.end <= self.start:
            raise ValueError(f"segment end ({self.end}) must be greater than start ({self.start})")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "description": self.description,
            "material_relevance": self.material_relevance,
            "usable": self.usable,
        }


class SegmentDetectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segments: list[DetectedSegment] = Field(default_factory=list)


class ClipScores(BaseModel):
    """Normalised 0..1 quality scores attached to every clip."""

    material_relevance: float = Field(default=0.0, ge=0.0, le=1.0)
    visual_quality: float = Field(default=0.0, ge=0.0, le=1.0)
    subtitle_cleanliness: float = Field(default=0.0, ge=0.0, le=1.0)
    stability: float = Field(default=0.0, ge=0.0, le=1.0)
    composition: float = Field(default=0.0, ge=0.0, le=1.0)
    overall: float = Field(default=0.0, ge=0.0, le=1.0)

    def recompute_overall(self) -> float:
        """Weighted overall score, used when a provider omits ``overall``."""

        weights = {
            "material_relevance": 0.35,
            "visual_quality": 0.2,
            "subtitle_cleanliness": 0.15,
            "stability": 0.15,
            "composition": 0.15,
        }
        total = sum(getattr(self, name) * weight for name, weight in weights.items())
        return round(total, 4)


class ClipTagging(BaseModel):
    """Full structured metadata for one produced clip."""

    model_config = ConfigDict(extra="forbid")

    material: str
    material_form: MaterialForm = MaterialForm.UNKNOWN
    material_state: MaterialState = MaterialState.UNKNOWN

    process_stage: ProcessStage = ProcessStage.OTHER

    equipment_type: str | None = None
    equipment_visible: bool = False

    scene: str = ""

    shot_type: ShotType = ShotType.UNKNOWN
    camera_motion: CameraMotion = CameraMotion.UNKNOWN

    people: bool = False
    people_count: int = Field(default=0, ge=0)
    person_role: PersonRole = PersonRole.NONE

    subtitle_type: SubtitleType = SubtitleType.UNKNOWN
    subtitle_score: float = Field(default=0.0, ge=0.0, le=1.0)

    edit_roles: list[EditRole] = Field(default_factory=list)

    description: str = ""

    scores: ClipScores = Field(default_factory=ClipScores)

    @field_validator("edit_roles", mode="before")
    @classmethod
    def _normalize_edit_role_aliases(cls, value: Any) -> Any:
        """Repair a narrow, common provider mix-up between stage and role.

        ``packaging`` and ``preparation`` are valid process stages, not edit
        roles. Vision providers occasionally copy them into both fields even
        when the schema lists the controlled role vocabulary. Both are process
        footage editorially, so preserve the otherwise valid tagging result
        instead of failing the complete response.
        """

        if not isinstance(value, list):
            return value
        stage_aliases = {"packaging", "preparation"}
        return ["process" if item in stage_aliases else item for item in value]

    @field_validator("edit_roles")
    @classmethod
    def _dedupe_edit_roles(cls, value: list[EditRole]) -> list[EditRole]:
        seen: list[EditRole] = []
        for role in value:
            if role not in seen:
                seen.append(role)
        return seen

    @model_validator(mode="after")
    def _sync_people_fields(self) -> ClipTagging:
        if self.people_count > 0:
            self.people = True
            if self.person_role is PersonRole.NONE:
                self.person_role = PersonRole.UNKNOWN
        elif not self.people:
            self.people_count = 0
            if self.person_role is PersonRole.UNKNOWN:
                self.person_role = PersonRole.NONE
        return self


class ClipScoresPatch(BaseModel):
    """Partial score payload tolerated from real providers."""

    material_relevance: float | None = None
    visual_quality: float | None = None
    subtitle_cleanliness: float | None = None
    stability: float | None = None
    composition: float | None = None
    overall: float | None = None


# ---------------------------------------------------------------------------
# Timing / artifact models
# ---------------------------------------------------------------------------
class SegmentTiming(BaseModel):
    """Final, scene-refined time range of a clip inside its source video."""

    start: float = Field(ge=0)
    end: float = Field(gt=0)
    ai_start: float | None = None
    ai_end: float | None = None
    scene_refined: bool = False

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @model_validator(mode="after")
    def _end_after_start(self) -> SegmentTiming:
        if self.end <= self.start:
            raise ValueError("segment end must be greater than start")
        return self


class ClipArtifact(BaseModel):
    """Result of writing one physical clip file plus its thumbnail."""

    file_path: Path
    thumbnail_path: Path | None = None
    duration: float = Field(ge=0)
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    fps: float | None = Field(default=None, ge=0)
    size_bytes: int = Field(default=0, ge=0)
    sha256: str | None = None
    phash: str | None = None
    #: frames sampled from the **final clip** for tagging (section 7/8); they
    #: are kept on disk only when the caller asked for a persistent directory
    tagging_frames: list[Path] = Field(default_factory=list)


class ClipRecord(BaseModel):
    """A fully persisted clip as stored in SQLite and returned to the UI."""

    id: int | None = None
    task_id: int | None = None
    source_video_id: int | None = None

    platform: str = ""
    platform_video_id: str = ""
    source_url: str = ""
    source_title: str = ""
    source_author: str = ""
    source_author_id: str | None = None
    source_publish_time: datetime | None = None
    source_start: float = 0.0
    source_end: float = 0.0

    #: physical/business category - the user's library folder (section 19).
    #: ``material`` below is the semantic observation and may differ.
    library_category: str = ""
    #: ``mock`` / ``local_test`` / ``douyin_real`` (section 23)
    provenance: str = ""
    #: prompt version that produced the semantic tags (audit trail)
    tag_prompt_version: str = ""
    #: human review state - never mixed with the AI verdict (Milestone 4)
    review_status: ReviewStatus = ReviewStatus.UNREVIEWED
    review_note: str = ""
    favorite: bool = False
    #: measured subtitle evidence (Milestone 6), None for historical clips
    subtitle_analysis: dict[str, Any] | None = None

    material: str
    material_form: MaterialForm = MaterialForm.UNKNOWN
    material_state: MaterialState = MaterialState.UNKNOWN
    process_stage: ProcessStage = ProcessStage.OTHER
    equipment_type: str | None = None
    equipment_visible: bool = False
    scene: str = ""
    shot_type: ShotType = ShotType.UNKNOWN
    camera_motion: CameraMotion = CameraMotion.UNKNOWN
    people: bool = False
    people_count: int = 0
    person_role: PersonRole = PersonRole.NONE
    subtitle_type: SubtitleType = SubtitleType.UNKNOWN
    subtitle_score: float = 0.0
    edit_roles: list[EditRole] = Field(default_factory=list)
    description: str = ""

    duration: float = 0.0
    width: int | None = None
    height: int | None = None
    fps: float | None = None

    material_score: float = 0.0
    visual_quality_score: float = 0.0
    subtitle_cleanliness_score: float = 0.0
    stability_score: float = 0.0
    composition_score: float = 0.0
    overall_score: float = 0.0

    file_path: Path
    thumbnail_path: Path | None = None
    phash: str | None = None
    sha256: str | None = None
    content_key: str | None = None

    tags: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


class SourceVideoRecord(BaseModel):
    id: int | None = None
    task_id: int | None = None
    platform: str
    platform_video_id: str
    source_url: str
    title: str = ""
    author: str = ""
    author_id: str | None = None
    cover_url: str | None = None
    publish_time: datetime | None = None
    duration: float | None = None
    status: SourceVideoStatus = SourceVideoStatus.CANDIDATE
    reject_reason: RejectReason | None = None
    preview_material_score: float | None = None
    preview_subtitle_score: float | None = None
    preview_quality_score: float | None = None
    media_url: str | None = None
    matched_queries: list[str] = Field(default_factory=list)
    statistics: dict[str, Any] = Field(default_factory=dict)
    attempt_count: int = 0
    last_attempt_at: datetime | None = None
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Task / pipeline models
# ---------------------------------------------------------------------------
class TaskRequest(BaseModel):
    """Everything one collection run needs; produced by the UI."""

    material: str
    #: raw search phrase supplied by the user (used verbatim as the first query)
    query_seed: str | None = None
    #: Milestone 9: an explicit, objective-specific query list.  When set, the
    #: orchestrator runs *exactly* these queries and never expands the material
    #: (so a plan item controls precisely what is executed).
    explicit_queries: list[str] = Field(default_factory=list)
    target_clip_count: int = Field(default=5, ge=1, le=200)
    #: Optional objective for gap-driven collection. Clips from adjacent stages
    #: remain rejected so the first acceptable segment cannot consume the goal.
    target_process_stage: ProcessStage | None = None
    min_clip_duration: float = Field(default=3.0, gt=0)
    max_clip_duration: float = Field(default=15.0, gt=0)
    subtitle_policy: SubtitlePolicy = SubtitlePolicy.STRICT
    library_root: Path | None = None
    #: explicit physical library category; defaults to ``material`` (section 18/19)
    library_category: str | None = None
    sources: list[str] = Field(default_factory=lambda: ["douyin"])
    platform: str = "douyin"
    # Per-run overrides (Milestone 2): they let the CLI/UI pick a source,
    # provider and media backend without editing config.yaml.
    source: str | None = None
    local_files: list[Path] = Field(default_factory=list)
    douyin_urls: list[str] = Field(default_factory=list)
    provider: str | None = None
    media_backend: str | None = None
    #: continue an earlier task instead of creating a new one
    resume_task_id: int | None = None
    #: Milestone 9.1: per-task discovery cap.  A plan item uses it to hand每个
    #: objective query a fair share of the item's candidate budget instead of
    #: letting the first query consume everything.
    max_candidates: int | None = None
    # -- Milestone 9.5: plan/query audit metadata ---------------------------
    plan_id: int | None = None
    plan_item_id: int | None = None
    query_family: str = ""
    planned_order: int = 0
    candidate_cap: int = 0
    was_reserve: bool = False
    reserve_activation_reason: str = ""

    @field_validator("material")
    @classmethod
    def _material_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("material must not be empty")
        return value

    @model_validator(mode="after")
    def _duration_bounds(self) -> TaskRequest:
        if self.max_clip_duration < self.min_clip_duration:
            raise ValueError("max_clip_duration must be >= min_clip_duration")
        return self


class TaskRecord(BaseModel):
    id: int | None = None
    material: str
    target_clip_count: int
    min_clip_duration: float
    max_clip_duration: float
    subtitle_policy: SubtitlePolicy
    status: TaskStatus = TaskStatus.PENDING
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ClipQuery(BaseModel):
    """Library query used by the future automatic editing system.

    Example: material=苹果, process_stage=drying, people=false,
    subtitle_type=[none, bottom_simple], min_overall_score=0.80.

    ``library_category`` filters the physical/business grouping (``苹果干``)
    while ``material`` filters the semantic observation (``苹果``), so clips
    showing fresh, drying and dried apple can be found either way (section 31).

    Milestone 4 adds human-review state, free-text search, an allowlisted sort
    option and LIMIT/OFFSET pagination.  The query object stays a pure data
    contract: the storage layer validates every column name it interpolates and
    the UI layer only maps widgets onto these fields.
    """

    library_category: str | None = None
    provenance: str | None = None
    tag_prompt_version: str | None = None
    source_platform: str | None = None
    material: str | None = None
    material_form: MaterialForm | None = None
    material_state: MaterialState | None = None
    process_stage: ProcessStage | None = None
    equipment_type: str | None = None
    equipment_visible: bool | None = None
    scene: str | None = None
    people: bool | None = None
    person_role: PersonRole | None = None
    subtitle_type: list[SubtitleType] | None = None
    shot_type: ShotType | None = None
    camera_motion: CameraMotion | None = None
    edit_role: EditRole | None = None
    task_id: int | None = None
    #: human review (Milestone 4)
    review_status: list[ReviewStatus] | None = None
    favorite: bool | None = None
    #: free-text search over description / scene / source title / author
    free_text: str | None = None
    min_duration: float | None = None
    max_duration: float | None = None
    min_overall_score: float | None = None
    max_overall_score: float | None = None
    min_material_score: float | None = None
    created_after: str | None = None
    created_before: str | None = None
    #: allowlisted sort option code (see CLIP_SORT_OPTIONS)
    sort_by: str = "newest"
    #: overrides the option's own direction when set
    sort_direction: SortDirection | None = None
    offset: int = Field(default=0, ge=0)
    order_by: str = "overall_score"
    limit: int = Field(default=20, ge=1, le=500)

    def as_filters(self) -> list[tuple[str, Any]]:
        """Plain SQL ``column = value`` filters (joins handled by the library)."""

        filters: list[tuple[str, Any]] = []
        if self.library_category:
            filters.append(("library_category", self.library_category))
        if self.provenance:
            filters.append(("provenance", self.provenance))
        if self.tag_prompt_version:
            filters.append(("tag_prompt_version", self.tag_prompt_version))
        if self.source_platform:
            filters.append(("platform", self.source_platform))
        if self.material:
            filters.append(("material", self.material))
        if self.material_form:
            filters.append(("material_form", str(self.material_form)))
        if self.material_state:
            filters.append(("material_state", str(self.material_state)))
        if self.process_stage:
            filters.append(("process_stage", str(self.process_stage)))
        if self.equipment_type:
            filters.append(("equipment_type", self.equipment_type))
        if self.scene:
            filters.append(("scene", self.scene))
        if self.equipment_visible is not None:
            filters.append(("equipment_visible", int(self.equipment_visible)))
        if self.people is not None:
            filters.append(("people", int(self.people)))
        if self.person_role:
            filters.append(("person_role", str(self.person_role)))
        if self.shot_type:
            filters.append(("shot_type", str(self.shot_type)))
        if self.camera_motion:
            filters.append(("camera_motion", str(self.camera_motion)))
        if self.task_id is not None:
            filters.append(("task_id", self.task_id))
        if self.favorite is not None:
            filters.append(("favorite", int(self.favorite)))
        return filters

    def sort_option(self) -> "ClipSortOption":
        """The selected sort option (defaults to newest-first)."""

        return sort_option(self.sort_by)


@dataclass(frozen=True)
class ClipSortOption:
    """One allowlisted ORDER BY choice (section 5)."""

    code: str
    label: str
    column: str
    direction: SortDirection


#: the only sort options the UI may offer; ``column`` is validated again in
#: storage before it reaches SQL
CLIP_SORT_OPTIONS: tuple[ClipSortOption, ...] = (
    ClipSortOption("newest", "最新入库", "created_at", SortDirection.DESC),
    ClipSortOption("oldest", "最早入库", "created_at", SortDirection.ASC),
    ClipSortOption("overall_desc", "综合评分从高到低", "overall_score", SortDirection.DESC),
    ClipSortOption("overall_asc", "综合评分从低到高", "overall_score", SortDirection.ASC),
    ClipSortOption("duration_asc", "时长从短到长", "duration", SortDirection.ASC),
    ClipSortOption("duration_desc", "时长从长到短", "duration", SortDirection.DESC),
    ClipSortOption("material_desc", "物料相关度从高到低", "material_score", SortDirection.DESC),
    ClipSortOption(
        "subtitle_clean_desc", "字幕洁净度从高到低", "subtitle_cleanliness_score", SortDirection.DESC
    ),
)

#: hard allowlist of sortable columns (never interpolate anything else)
CLIP_SORT_COLUMNS: frozenset[str] = frozenset(
    option.column for option in CLIP_SORT_OPTIONS
) | {"id"}

DEFAULT_SORT_CODE = "newest"


def sort_option(code: str | None) -> ClipSortOption:
    """Resolve a sort code, falling back to the default (never raising)."""

    wanted = (code or "").strip()
    for option in CLIP_SORT_OPTIONS:
        if option.code == wanted:
            return option
    for option in CLIP_SORT_OPTIONS:
        if option.code == DEFAULT_SORT_CODE:
            return option
    return CLIP_SORT_OPTIONS[0]  # pragma: no cover - defensive


def clip_sort_choices() -> tuple[tuple[str, str], ...]:
    """``(label, code)`` pairs for the UI dropdown."""

    return tuple((option.label, option.code) for option in CLIP_SORT_OPTIONS)


class PipelineStats(BaseModel):
    """Counters shown live in the UI (section 30 of the specification)."""

    searched_candidates: int = 0
    unique_candidates: int = 0
    examined_candidates: int = 0
    prescreened: int = 0
    subtitle_rejected: int = 0
    other_rejected: int = 0
    quality_rejected: int = 0
    duplicates_rejected: int = 0
    analyzed: int = 0
    segments_found: int = 0
    clips_saved: int = 0
    downloads: int = 0
    errors: int = 0
    queries_generated: int = 0
    # -- Milestone 9.1 production counters ---------------------------------
    #: dedup/retry-policy suppressed candidates (previously processed)
    dedup_suppressed: int = 0
    #: discovery returned no duration and a media probe was attempted
    duration_unknown: int = 0
    #: the probe established a trustworthy duration
    duration_unknown_resolved: int = 0
    #: the probe could not establish a duration
    duration_unknown_unresolved: int = 0
    # -- Milestone 9.6 duration-resolution metrics -------------------------
    duration_known_from_metadata: int = 0
    duration_remote_probe_resolved: int = 0
    duration_local_probe_resolved: int = 0
    duration_frame_derived: int = 0
    duration_bounded_decode_resolved: int = 0
    duration_unresolved: int = 0
    media_unreachable: int = 0
    probe_downloads: int = 0
    probe_bytes: int = 0
    probe_latency_ms: int = 0
    probe_cache_reused: int = 0
    provider_circuit_trips: int = 0
    provider_calls_skipped: int = 0
    #: the "media" payload was a web page / non-video file
    invalid_media_source: int = 0
    # -- Milestone 9.5 novelty counters ------------------------------------
    new_to_system: int = 0
    known_source: int = 0
    current_run_duplicate: int = 0
    already_processed: int = 0
    already_represented: int = 0

    def as_rows(self) -> list[tuple[str, int]]:
        labels = [
            ("搜索候选", self.searched_candidates),
            ("唯一候选", self.unique_candidates),
            ("已处理候选", self.examined_candidates),
            ("已预筛", self.prescreened),
            ("字幕淘汰", self.subtitle_rejected),
            ("其他淘汰", self.other_rejected),
            ("质量淘汰", self.quality_rejected),
            ("去重淘汰", self.duplicates_rejected),
            ("进入分析", self.analyzed),
            ("发现有效片段", self.segments_found),
            ("最终保存数量", self.clips_saved),
            ("下载次数", self.downloads),
            ("错误次数", self.errors),
        ]
        return labels


class ProgressEvent(BaseModel):
    """Emitted by the orchestrator so the UI can stream progress."""

    stage: str
    message: str
    stats: PipelineStats = Field(default_factory=PipelineStats)
    level: str = "info"


class SegmentReport(BaseModel):
    """One detected time range, as shown in the UI debug panel."""

    start: float
    end: float
    description: str = ""
    material_relevance: float = 0.0
    refined_start: float | None = None
    refined_end: float | None = None
    saved: bool = False
    reject_reason: RejectReason | None = None


class SourceVideoReport(BaseModel):
    platform: str
    platform_video_id: str
    title: str = ""
    source_author: str = ""
    #: which discovery backend found it (browser / dtk_keyword / archive ...)
    discovery_backend: str = ""
    source_url: str = ""
    source_path: str = ""
    duration: float | None = None
    status: SourceVideoStatus
    reject_reason: RejectReason | None = None
    preview_accept: bool | None = None
    preview_reason: RejectReason | None = None
    preview_detail: str = ""
    segments_found: int = 0
    clips_saved: int = 0
    ai_calls: int = 0
    matched_queries: list[str] = Field(default_factory=list)
    segments: list[SegmentReport] = Field(default_factory=list)
    #: measured subtitle evidence (Milestone 6); None = analyzer unavailable
    subtitle_analysis: dict[str, Any] | None = None

    @property
    def accepted(self) -> bool:
        return self.preview_accept is True


class PipelineResult(BaseModel):
    """Final outcome of one collection run."""

    task_id: int | None = None
    material: str
    status: TaskStatus
    queries: list[str] = Field(default_factory=list)
    stats: PipelineStats = Field(default_factory=PipelineStats)
    clips: list[ClipRecord] = Field(default_factory=list)
    source_videos: list[SourceVideoReport] = Field(default_factory=list)
    ai_usage: dict[str, Any] = Field(default_factory=dict)
    #: which discovery backend ran, and its final state (section 15/24)
    discovery_backend: str = ""
    discovery_status: str = ""
    discovery_detail: str = ""
    #: True when discovery could not execute at all (≠ "search ran, 0 results")
    discovery_blocked: bool = False
    #: per-backend discovery state, e.g. {"browser": "verification_required"}
    discovery_states: dict[str, str] = Field(default_factory=dict)
    #: Milestone 9.7 provider readiness / circuit state
    provider_unavailable: bool = False
    provider_failure_class: str = ""
    provider_failure_subtype: str = ""
    provider_model: str = ""
    provider_operation: str = ""
    provider_detail: str = ""
    messages: list[str] = Field(default_factory=list)
    #: Milestone 9.5 per-query novelty/yield audit (no secrets)
    query_audit: list[dict[str, Any]] = Field(default_factory=list)
    elapsed_seconds: float = 0.0
    error: str | None = None

    def summary_text(self) -> str:
        """Human readable report rendered in the Gradio status panel."""

        lines = [
            f"任务 #{self.task_id}  物料: {self.material}  状态: {self.status.value}",
            f"耗时: {self.elapsed_seconds:.2f}s",
            "",
        ]
        lines.extend(f"{label}: {value}" for label, value in self.stats.as_rows())
        if self.messages:
            lines.append("")
            lines.append("日志:")
            lines.extend(f"- {message}" for message in self.messages[-12:])
        return "\n".join(lines)
