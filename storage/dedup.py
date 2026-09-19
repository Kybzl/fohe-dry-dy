"""Deduplication and similarity grouping.

Hard duplicate checks (a clip is *not* stored twice):

1. ``SHA256`` of the cut clip file
2. representative-frame ``pHash`` within ``phash_max_distance`` bits

``source_video_id`` only prevents re-processing the same source video; a single
source video is explicitly allowed to produce **many** valid clips.

``content_key`` is *similarity metadata*: it groups clips that look/read the
same so a future auto-editor can rank them.  It is never a hard duplicate
unless ``use_content_key_as_duplicate`` is switched on in the configuration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from core.models import RejectReason, SourceVideoStatus, utc_now
from storage.library import MaterialLibrary

LOGGER = logging.getLogger(__name__)

#: source video states that mean "we already spent time on this video"
TERMINAL_SOURCE_STATUSES = frozenset(
    {
        SourceVideoStatus.PROCESSED,
        SourceVideoStatus.ANALYZED,
        SourceVideoStatus.NO_USABLE_SEGMENT,
        SourceVideoStatus.SKIPPED_DUPLICATE,
    }
)

#: states that mean "already finished, do not pay again"
COMPLETED_SOURCE_STATUSES = frozenset(
    {
        SourceVideoStatus.PROCESSED,
        SourceVideoStatus.SKIPPED_DUPLICATE,
    }
)

#: states that are worth retrying after a cool-down
REJECTED_SOURCE_STATUSES = frozenset(
    {
        SourceVideoStatus.REJECTED,
        SourceVideoStatus.REJECTED_PREVIEW,
    }
)

FAILED_SOURCE_STATUSES = frozenset(
    {
        SourceVideoStatus.FAILED,
        SourceVideoStatus.FAILED_SEARCH,
        SourceVideoStatus.FAILED_PREVIEW,
        SourceVideoStatus.FAILED_DOWNLOAD,
        SourceVideoStatus.FAILED_AI,
        SourceVideoStatus.FAILED_MEDIA,
    }
)

#: states that mean "someone is working on it right now"
IN_PROGRESS_SOURCE_STATUSES = frozenset(
    {
        SourceVideoStatus.PREVIEWING,
        SourceVideoStatus.DOWNLOADING,
        SourceVideoStatus.ANALYZING,
        SourceVideoStatus.QUALIFIED,
    }
)


@dataclass(frozen=True)
class AcquisitionDecision:
    """Whether a candidate should be processed again, and why."""

    skip: bool
    reason: str = ""
    detail: str = ""


def hamming_distance(left: str, right: str) -> int:
    """Bit distance between two hex perceptual hashes."""

    if not left or not right:
        return 64
    try:
        import imagehash

        if len(left) == 16 and len(right) == 16:
            return int(imagehash.hex_to_hash(left) - imagehash.hex_to_hash(right))
    except Exception:  # imagehash / numpy not installed: fall back to plain XOR
        pass
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return 0 if left == right else 64


def _as_utc(value: object) -> datetime | None:
    """Parse a stored ISO timestamp into an aware UTC datetime."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class DuplicateHit:
    """A previously stored clip that matches the candidate."""

    clip_id: int
    reason: RejectReason
    detector: str
    distance: int | None = None


@dataclass(frozen=True)
class SimilarClip:
    """A clip that is *similar* (same content key) but not a duplicate."""

    clip_id: int
    content_key: str


class DeduplicationService:
    """Answers "have we already got this exact clip / this video?"."""

    def __init__(
        self,
        library: MaterialLibrary,
        *,
        enabled: bool = True,
        phash_max_distance: int = 6,
        use_content_key_as_duplicate: bool = False,
    ) -> None:
        self.library = library
        self.enabled = enabled
        self.phash_max_distance = phash_max_distance
        self.use_content_key_as_duplicate = use_content_key_as_duplicate

    # -- source video level ------------------------------------------------
    def source_video_processed(self, platform: str, platform_video_id: str) -> bool:
        """True when this platform video was already fully processed.

        Note: this only avoids re-analysing the *same source video*.  Several
        clips from one source video are perfectly normal and allowed.
        """

        if not self.enabled:
            return False
        record = self.library.get_source_video(platform, platform_video_id)
        if record is None:
            return False
        return record.status in TERMINAL_SOURCE_STATUSES

    def source_video_status(self, platform: str, platform_video_id: str) -> SourceVideoStatus | None:
        record = self.library.get_source_video(platform, platform_video_id)
        return record.status if record else None

    def acquisition_decision(
        self,
        platform: str,
        platform_video_id: str,
        *,
        retry_rejected_after_days: float = 30.0,
        retry_failed_after_hours: float = 24.0,
        duration_resolution_retry_hours: float = 6.0,
        in_progress_stale_hours: float = 6.0,
    ) -> AcquisitionDecision:
        """Decide whether to spend time/AI on a source video again (section 14).

        * processed / skipped -> always skip
        * rejected            -> retry only after ``retry_rejected_after_days``
        * failed_*            -> retry only after ``retry_failed_after_hours``
        * in progress         -> skip unless the attempt looks abandoned
        """

        if not self.enabled:
            return AcquisitionDecision(False, "dedup-disabled")

        record = self.library.get_source_video(platform, platform_video_id)
        if record is None:
            return AcquisitionDecision(False, "new")

        status = record.status
        last = _as_utc(record.last_attempt_at) or _as_utc(record.created_at) or utc_now()
        age = utc_now() - last

        if str(record.reject_reason or "") == str(
            RejectReason.DURATION_UNKNOWN_UNRESOLVED
        ):
            # Milestone 9.6: duration/media resolution is an execution retry,
            # never a 30-day content rejection.
            if age < timedelta(hours=duration_resolution_retry_hours):
                return AcquisitionDecision(
                    True,
                    "duration_unresolved_recent",
                    f"age={age} < {duration_resolution_retry_hours}h",
                )
            return AcquisitionDecision(
                False,
                "retry_duration_unresolved",
                f"age={age} >= {duration_resolution_retry_hours}h",
            )

        if status in COMPLETED_SOURCE_STATUSES:
            return AcquisitionDecision(True, "already_processed", f"status={status}")
        if status in IN_PROGRESS_SOURCE_STATUSES:
            if age < timedelta(hours=in_progress_stale_hours):
                return AcquisitionDecision(True, "in_progress", f"status={status}")
            return AcquisitionDecision(False, "stale_in_progress", f"status={status}")
        if status in REJECTED_SOURCE_STATUSES:
            if age < timedelta(days=retry_rejected_after_days):
                return AcquisitionDecision(
                    True,
                    "recently_rejected",
                    f"status={status} age={age.days}d < {retry_rejected_after_days}d",
                )
            return AcquisitionDecision(False, "retry_rejected", f"status={status}")
        if status in FAILED_SOURCE_STATUSES:
            if age < timedelta(hours=retry_failed_after_hours):
                return AcquisitionDecision(
                    True,
                    "recent_failure",
                    f"status={status} age={age} < {retry_failed_after_hours}h",
                )
            return AcquisitionDecision(False, "retry_failed", f"status={status}")
        return AcquisitionDecision(False, "unknown_status", f"status={status}")

    def url_seen(self, source_url: str) -> bool:
        if not self.enabled:
            return False
        return self.library.url_seen(source_url)

    # -- clip level --------------------------------------------------------
    def find_duplicate(
        self,
        *,
        sha256: str | None = None,
        phash: str | None = None,
        content_key: str | None = None,
        material: str | None = None,
    ) -> DuplicateHit | None:
        """Return the first *hard* duplicate of this clip, or ``None``."""

        if not self.enabled:
            return None

        if sha256:
            clip_id = self.library.find_clip_by_sha256(sha256)
            if clip_id is not None:
                LOGGER.debug("duplicate clip by sha256: %s", clip_id)
                return DuplicateHit(clip_id, RejectReason.DUPLICATE_CLIP, "sha256", 0)

        if phash:
            for clip_id, stored in self.library.all_phashes(material):
                distance = hamming_distance(phash, stored)
                if distance <= self.phash_max_distance:
                    LOGGER.debug("duplicate clip by phash (distance %s): %s", distance, clip_id)
                    return DuplicateHit(
                        clip_id, RejectReason.DUPLICATE_CLIP, "phash", distance
                    )

        if self.use_content_key_as_duplicate and content_key:
            clip_id = self.library.find_clip_by_content_key(content_key)
            if clip_id is not None:
                LOGGER.debug("duplicate clip by content_key (opt in): %s", clip_id)
                return DuplicateHit(clip_id, RejectReason.DUPLICATE_CLIP, "content_key", 0)

        return None

    # -- similarity (never rejects) ----------------------------------------
    def find_similar(
        self,
        *,
        content_key: str | None,
        limit: int = 5,
    ) -> list[SimilarClip]:
        """Clips sharing the same semantic content key (ranking metadata)."""

        if not self.enabled or not content_key:
            return []
        rows = self.library.clips_with_content_key(content_key, limit=limit)
        return [SimilarClip(clip_id=clip_id, content_key=content_key) for clip_id in rows]

    def similarity_groups(self, *, material: str | None = None, limit: int = 20) -> list[tuple[str, int]]:
        """``(content_key, clip_count)`` for the library, largest group first."""

        return self.library.content_key_groups(material=material, limit=limit)
