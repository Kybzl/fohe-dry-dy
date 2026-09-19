"""Local, zero-cost candidate filtering.

No search result may reach the AI before passing these rules: duplicates,
already processed videos, impossible durations and obviously unrelated titles
are rejected locally (section 9 of the specification).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.models import RejectReason, VideoCandidate
from storage.dedup import DeduplicationService

LOGGER = logging.getLogger(__name__)

#: Titles containing these tokens are almost never drying material footage.
DEFAULT_IRRELEVANT_TOKENS: tuple[str, ...] = (
    "开箱",
    "手机支架",
    "汽车内饰",
    "游戏",
    "明星",
    "综艺",
    "招聘",
)


@dataclass(frozen=True)
class CandidateDecision:
    """Outcome of the local filter for one candidate."""

    accepted: bool
    reason: RejectReason | None = None
    detail: str = ""
    #: Milestone 9.1: accepted *pending* a media metadata probe because the
    #: discovery payload carried no trustworthy duration
    duration_unknown: bool = False

    @classmethod
    def accept(cls) -> CandidateDecision:
        return cls(True)

    @classmethod
    def reject(cls, reason: RejectReason, detail: str = "") -> CandidateDecision:
        return cls(False, reason, detail)


class CandidateFilter:
    """Rule based filter applied before any paid AI call."""

    def __init__(
        self,
        dedup: DeduplicationService,
        *,
        min_duration: float = 5.0,
        max_duration: float = 300.0,
        irrelevant_tokens: tuple[str, ...] = DEFAULT_IRRELEVANT_TOKENS,
        retry_rejected_after_days: float = 30.0,
        retry_failed_after_hours: float = 24.0,
        duration_resolution_retry_hours: float = 6.0,
    ) -> None:
        self.dedup = dedup
        self.min_duration = min_duration
        self.max_duration = max_duration
        self.irrelevant_tokens = irrelevant_tokens
        self.retry_rejected_after_days = retry_rejected_after_days
        self.retry_failed_after_hours = retry_failed_after_hours
        self.duration_resolution_retry_hours = duration_resolution_retry_hours

    def evaluate(
        self,
        candidate: VideoCandidate,
        *,
        material: str,
        seen_video_ids: set[str] | None = None,
        seen_urls: set[str] | None = None,
    ) -> CandidateDecision:
        seen_video_ids = seen_video_ids if seen_video_ids is not None else set()
        seen_urls = seen_urls if seen_urls is not None else set()

        if not candidate.platform_video_id:
            return CandidateDecision.reject(RejectReason.UNREACHABLE, "missing video id")
        if not candidate.source_url:
            return CandidateDecision.reject(RejectReason.UNREACHABLE, "missing source url")

        # 0. unreadable / corrupt media reported by the source adapter
        if candidate.metadata.get("unreadable"):
            return CandidateDecision.reject(
                RejectReason.CORRUPT_MEDIA,
                str(candidate.metadata.get("probe_error") or "media could not be read"),
            )
        # image albums / notes cannot produce video clips
        if candidate.metadata.get("no_video"):
            return CandidateDecision.reject(
                RejectReason.UNUSABLE_MEDIA,
                f"content kind '{candidate.metadata.get('kind')}' has no video stream",
            )

        # 1. duplicates inside the current run
        if candidate.dedup_key in seen_video_ids:
            return CandidateDecision.reject(RejectReason.DUPLICATE_VIDEO, "seen in this task")
        if candidate.source_url in seen_urls:
            return CandidateDecision.reject(RejectReason.DUPLICATE_URL, "url seen in this task")

        # 2. already handled by an earlier task (with a configurable retry policy)
        acquisition = self.dedup.acquisition_decision(
            candidate.platform,
            candidate.platform_video_id,
            retry_rejected_after_days=self.retry_rejected_after_days,
            retry_failed_after_hours=self.retry_failed_after_hours,
            duration_resolution_retry_hours=self.duration_resolution_retry_hours,
        )
        if acquisition.skip:
            return CandidateDecision.reject(
                RejectReason.ALREADY_PROCESSED,
                f"{acquisition.reason} {acquisition.detail}".strip(),
            )
        if acquisition.reason == "new" and self.dedup.url_seen(candidate.source_url):
            return CandidateDecision.reject(RejectReason.DUPLICATE_URL, "url already in library")

        # 3. duration sanity (Milestone 9.1)
        if candidate.duration is None:
            # ``duration_unknown`` is NOT "out of range": the upstream metadata
            # simply did not carry a duration.  The orchestrator runs a
            # low-cost media metadata probe before deciding anything.
            return CandidateDecision(True, None, "unknown duration", duration_unknown=True)
        if candidate.duration < self.min_duration or candidate.duration > self.max_duration:
            return CandidateDecision.reject(
                RejectReason.DURATION_OUT_OF_RANGE,
                f"duration {candidate.duration:.1f}s outside "
                f"[{self.min_duration:.0f}, {self.max_duration:.0f}]s",
            )

        # 4. obviously unrelated titles
        if self._title_is_irrelevant(candidate.title, material):
            return CandidateDecision.reject(
                RejectReason.TITLE_IRRELEVANT, f"title looks unrelated: {candidate.title!r}"
            )

        return CandidateDecision.accept()

    def _title_is_irrelevant(self, title: str, material: str) -> bool:
        if not title:
            return False
        if material and material in title:
            return False
        # Compare on characters so "苹果干" matches a title mentioning "苹果".
        if material and any(char in title for char in material):
            return False
        return any(token in title for token in self.irrelevant_tokens)
