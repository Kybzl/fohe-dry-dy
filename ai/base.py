"""The provider agnostic ``VisionProvider`` contract.

Three capabilities are required by the pipeline:

1. ``preview_filter``   - is this video usable material at all?  (cheap)
2. ``detect_segments``  - which time ranges contain the target material?
3. ``tag_clip``         - structured metadata for one produced clip

Qwen is the primary provider and Volcano Engine the fallback, but both are
swapped in through ``config.yaml`` so core code never imports them directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.models import (
    ClipTagging,
    PreviewFilterResult,
    PreviewFrame,
    SegmentDetectionResult,
)


class ProviderError(RuntimeError):
    """Any recoverable provider failure (network, timeout, bad JSON...)."""


class ProviderTransientError(ProviderError):
    """Timeout, connection reset, HTTP 429 or 5xx -- worth retrying."""


class ProviderPermanentError(ProviderError):
    """Bad credentials, bad request, model not found -- retrying is useless."""


class ProviderAccountBlockedError(ProviderPermanentError):
    """Authoritative account/provider blocker (quota, auth, model access).

    ``subtype`` is a stable machine-readable value such as
    ``quota_exhausted`` / ``authentication_failed`` / ``model_access_denied``.
    """

    def __init__(self, message: str, *, subtype: str = "account_blocked") -> None:
        super().__init__(message)
        self.subtype = subtype


class ProviderSchemaError(ProviderError):
    """The provider answered, but the payload failed schema validation."""


class ProviderNotConfiguredError(ProviderError):
    """The provider has no credentials or is not implemented yet."""


@dataclass
class UsageInfo:
    """Token accounting reported by an OpenAI compatible endpoint."""

    model: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost": self.estimated_cost,
        }


class AuditContext(BaseModel):
    """Identifiers attached to every AI request for the ``ai_runs`` table."""

    model_config = ConfigDict(extra="ignore")

    task_id: int | None = None
    source_video_id: int | None = None
    clip_id: int | None = None
    #: why the call happened: ``pipeline`` (collection), ``retag`` (explicit
    #: re-tagging) or ``evaluation`` (A/B prompt comparison).  Keeps cost
    #: reports about *production* spend honest (Milestone 3.7, section 14).
    origin: str = "pipeline"


class _RequestBase(BaseModel):
    """Common context every vision request carries."""

    #: task intent (what the operator searched for) - never the visual verdict
    material: str
    #: explicit separation of intent vs observation (Milestone 3.7, section 2)
    requested_material: str = ""
    query: str = ""
    platform: str = "douyin"
    platform_video_id: str = ""
    title: str = ""
    duration: float | None = Field(default=None, ge=0)
    frames: list[PreviewFrame] = Field(default_factory=list)
    # Adapter specific hints (e.g. mock scenarios, platform stats).
    context: dict[str, Any] = Field(default_factory=dict)
    # Where this call belongs in the library (audit trail).
    audit: AuditContext = Field(default_factory=AuditContext)


class PreviewFilterRequest(_RequestBase):
    """Input of ``preview_filter`` -- preview stills taken before download."""


class SegmentDetectionRequest(_RequestBase):
    """Input of ``detect_segments`` -- sparse frames of the downloaded video."""

    duration: float = Field(gt=0)
    video_path: str | None = None
    min_segment_duration: float = 3.0
    max_segment_duration: float = 15.0
    target_process_stage: str = ""


class ClipTaggingRequest(_RequestBase):
    """Input of ``tag_clip`` -- one confirmed segment to be tagged."""

    start: float = Field(ge=0)
    end: float = Field(gt=0)
    segment_description: str = ""
    segment_relevance: float = 0.0
    #: pin a prompt version ('' = provider default, milestones 3.7 section 26/28)
    prompt_version: str = ""


class CoverageRequest(_RequestBase):
    """Reserved for a later milestone (long video coverage scanning)."""

    duration: float = Field(gt=0)


class VisionProvider(ABC):
    """Async vision capability provider."""

    #: registry key, e.g. ``"qwen"``
    name: str = "unknown"
    #: operations this provider implements, in audit terms
    operations: tuple[str, ...] = ("preview_filter", "segment_detection", "clip_tagging")
    #: prompt version persisted in ``ai_runs`` for every operation
    prompt_versions: dict[str, str] = {
        "preview_filter": "preview_filter_v1",
        "segment_detection": "segment_detection_v2",
        # Milestone 3.7: observe-the-final-clip-first prompt
        "clip_tagging": "clip_tagging_v2",
    }

    def __init__(self, *, timeout: float = 60.0, max_retries: int = 2, **options: Any) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.options = options
        self._last_usage: UsageInfo | None = None
        self._last_model: str = ""

    # -- audit helpers -----------------------------------------------------
    def model_for(self, operation: str) -> str:
        """Model name used for one operation (configuration driven)."""

        return self._last_model or str(self.options.get("model", "")) or "unknown"

    def prompt_version_for(self, operation: str, request: Any | None = None) -> str:
        """Prompt version actually used for this call.

        A request may pin a version (``clip_tagging_v1`` for an A/B comparison or
        an explicit retag); otherwise the provider default applies.
        """

        pinned = getattr(request, "prompt_version", "") if request is not None else ""
        if pinned:
            return str(pinned)
        return self.prompt_versions.get(operation, f"{operation}_v1")

    def consume_usage(self) -> UsageInfo | None:
        """Return and clear the usage of the most recent call."""

        usage, self._last_usage = self._last_usage, None
        return usage

    @abstractmethod
    async def preview_filter(self, request: PreviewFilterRequest) -> PreviewFilterResult:
        """Decide whether a candidate video is usable clip material."""

    @abstractmethod
    async def detect_segments(self, request: SegmentDetectionRequest) -> SegmentDetectionResult:
        """Locate the time ranges that actually show the target material."""

    @abstractmethod
    async def tag_clip(self, request: ClipTaggingRequest) -> ClipTagging:
        """Produce the structured tag set for one clip."""

    async def aclose(self) -> None:
        """Release transport resources.  Optional."""

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<{type(self).__name__} name={self.name}>"
