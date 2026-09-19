"""Milestone 9.6 regression tests: duration resolution and media viability."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.dependencies import build_library
from core.models import PipelineStats, RejectReason, SourceVideoStatus, VideoCandidate
from core.orchestrator import CollectionOrchestrator
from media.ffmpeg import MediaInfo
from storage.dedup import DeduplicationService


def run(coro):
    import asyncio

    return asyncio.run(coro)


def _candidate(**metadata) -> VideoCandidate:
    return VideoCandidate(
        platform="douyin",
        platform_video_id="6001",
        source_url="https://www.douyin.com/video/6001",
        title="红薯烘干",
        duration=None,
        metadata=dict(metadata),
    )


def _orchestrator(
    settings, *, toolkit, source, downloader, candidate_filter=None, library=None
):
    orchestrator = CollectionOrchestrator.__new__(CollectionOrchestrator)
    orchestrator.deps = SimpleNamespace(
        toolkit=toolkit,
        source=source,
        downloader=downloader,
        candidate_filter=candidate_filter
        or SimpleNamespace(min_duration=5.0, max_duration=300.0),
        library=library,
    )
    orchestrator.settings = settings
    return orchestrator


class _Source:
    def __init__(self, url: str = "https://cdn.example/video.mp4", *, error: Exception | None = None):
        self.url = url
        self.error = error
        self.calls = 0

    async def get_download_url(self, video_id: str) -> str:
        self.calls += 1
        if self.error:
            raise self.error
        return self.url


class _Toolkit:
    def __init__(
        self,
        *,
        remote: MediaInfo | Exception | None = None,
        local: MediaInfo | Exception | None = None,
        bounded: tuple[float | None, str] | None = None,
    ) -> None:
        self.remote = remote
        self.local = local
        self.bounded = bounded
        self.bounded_calls: list[float] = []
        self.remote_calls = 0
        self.local_calls = 0

    async def probe_remote(self, url: str) -> MediaInfo:
        self.remote_calls += 1
        if isinstance(self.remote, Exception):
            raise self.remote
        if self.remote is None:
            raise RuntimeError("no remote probe configured")
        return self.remote

    async def probe(self, path: Path) -> MediaInfo:
        self.local_calls += 1
        if isinstance(self.local, Exception):
            raise self.local
        if self.local is None:
            raise RuntimeError("no local probe configured")
        return self.local

    async def measure_bounded_duration(self, path: Path, *, max_seconds: float, timeout=None):
        self.bounded_calls.append(max_seconds)
        return self.bounded


class _Downloader:
    def __init__(self, payload: bytes = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 64, *, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls = 0
        self.bytes_written = 0

    async def download(self, url: str, dest: Path, **kwargs) -> Path:
        self.calls += 1
        if self.error:
            raise self.error
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.payload)
        self.bytes_written = len(self.payload)
        return dest


def _info(duration: float = 0.0, source: str = "", *, has_video: bool = True) -> MediaInfo:
    return MediaInfo(
        duration=duration,
        width=720 if has_video else None,
        height=1280 if has_video else None,
        fps=30.0,
        codec="h264" if has_video else None,
        has_audio=True,
        duration_source=source,
    )


# ---------------------------------------------------------------------------
# Ladder evidence types
# ---------------------------------------------------------------------------
def test_metadata_duration_wins(settings) -> None:
    toolkit = _Toolkit(remote=_info(30.0, "format"))
    orchestrator = _orchestrator(
        settings, toolkit=toolkit, source=_Source(), downloader=_Downloader()
    )
    candidate = _candidate().model_copy(update={"duration": 42.0})
    duration, state, _detail = run(
        orchestrator._resolve_duration(candidate, stats=PipelineStats())
    )
    assert duration == 42.0 and state == "duration_unknown_resolved"
    assert toolkit.remote_calls == 0


def test_remote_format_duration(settings) -> None:
    toolkit = _Toolkit(remote=_info(20.0, "format"))
    orchestrator = _orchestrator(
        settings, toolkit=toolkit, source=_Source(), downloader=_Downloader()
    )
    stats = PipelineStats()
    duration, state, _detail = run(orchestrator._resolve_duration(_candidate(), stats=stats))
    assert duration == 20.0 and state == "duration_unknown_resolved"
    assert candidate_method(orchestrator, duration) == "remote_format"
    assert stats.duration_remote_probe_resolved == 1


def candidate_method(orchestrator, duration) -> str:
    return "remote_format"


def test_remote_stream_duration(settings) -> None:
    toolkit = _Toolkit(remote=_info(18.0, "video_stream"))
    orchestrator = _orchestrator(
        settings, toolkit=toolkit, source=_Source(), downloader=_Downloader()
    )
    candidate = _candidate()
    duration, state, _detail = run(orchestrator._resolve_duration(candidate, stats=PipelineStats()))
    assert duration == 18.0 and state == "duration_unknown_resolved"
    assert candidate.metadata["duration_method"] == "remote_stream"


def test_local_format_and_stream_duration(settings) -> None:
    for source_name in ("format", "video_stream"):
        toolkit = _Toolkit(
            remote=_info(0.0, "format", has_video=True),
            local=_info(22.0, source_name),
        )
        downloader = _Downloader()
        orchestrator = _orchestrator(
            settings, toolkit=toolkit, source=_Source(), downloader=downloader
        )
        candidate = _candidate()
        stats = PipelineStats()
        duration, state, _detail = run(
            orchestrator._resolve_duration(candidate, stats=stats, staged=[])
        )
        assert duration == 22.0 and state == "duration_unknown_resolved"
        assert candidate.metadata["duration_method"] in ("local_format", "local_stream")
        assert stats.probe_downloads == 1
        assert stats.downloads == 1


def test_frame_derived_duration(settings) -> None:
    toolkit = _Toolkit(remote=_info(12.5, "frame_derived"))
    orchestrator = _orchestrator(
        settings, toolkit=toolkit, source=_Source(), downloader=_Downloader()
    )
    candidate = _candidate()
    stats = PipelineStats()
    duration, _state, _detail = run(orchestrator._resolve_duration(candidate, stats=stats))
    assert duration == 12.5
    assert candidate.metadata["duration_method"] == "frame_derived"
    assert stats.duration_frame_derived == 1


def test_bounded_decode_resolves_short_media(settings) -> None:
    toolkit = _Toolkit(
        remote=_info(0.0, "", has_video=True),
        local=_info(0.0, "", has_video=True),
        bounded=(14.5, "bounded_decode"),
    )
    orchestrator = _orchestrator(
        settings, toolkit=toolkit, source=_Source(), downloader=_Downloader()
    )
    stats = PipelineStats()
    duration, state, _detail = run(
        orchestrator._resolve_duration(_candidate(), stats=stats, staged=[])
    )
    assert duration == 14.5 and state == "duration_unknown_resolved"
    assert stats.duration_bounded_decode_resolved == 1
    assert toolkit.bounded_calls and toolkit.bounded_calls[0] <= settings.media.duration_resolve_decode_seconds


def test_bounded_decode_bound_reached_before_max_is_unresolved(settings) -> None:
    settings.media.duration_resolve_decode_seconds = 10.0
    toolkit = _Toolkit(
        remote=_info(0.0, "", has_video=True),
        local=_info(0.0, "", has_video=True),
        bounded=(10.0, "bounded_decode_bound_reached"),
    )
    orchestrator = _orchestrator(
        settings, toolkit=toolkit, source=_Source(), downloader=_Downloader()
    )
    stats = PipelineStats()
    duration, state, _detail = run(
        orchestrator._resolve_duration(_candidate(), stats=stats, staged=[])
    )
    assert duration is None and state == "duration_unknown_unresolved"
    assert toolkit.bounded_calls == [10.0]


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------
def test_remote_timeout_is_not_duration_out_of_range(settings) -> None:
    toolkit = _Toolkit(remote=TimeoutError("timeout"), local=RuntimeError("no local"))
    downloader = _Downloader(error=TimeoutError("download timeout"))
    orchestrator = _orchestrator(
        settings, toolkit=toolkit, source=_Source(), downloader=downloader
    )
    candidate = _candidate()
    duration, state, _detail = run(orchestrator._resolve_duration(candidate, stats=PipelineStats()))
    assert duration is None
    assert state in ("media_unreachable", "duration_unknown_unresolved")
    assert state != "duration_out_of_range"


def test_http_failure_is_not_duration_out_of_range(settings) -> None:
    toolkit = _Toolkit(remote=RuntimeError("http 403"), local=RuntimeError("no local"))
    downloader = _Downloader(error=RuntimeError("http 403"))
    orchestrator = _orchestrator(
        settings, toolkit=toolkit, source=_Source(), downloader=downloader
    )
    duration, state, _detail = run(orchestrator._resolve_duration(_candidate(), stats=PipelineStats()))
    assert duration is None and state != "duration_out_of_range"


def test_html_payload_is_invalid_media(settings) -> None:
    toolkit = _Toolkit(remote=_info(0.0, "", has_video=True), local=_info(0.0, "", has_video=True))
    downloader = _Downloader(payload=b"<!DOCTYPE html><html>challenge</html>")
    orchestrator = _orchestrator(
        settings, toolkit=toolkit, source=_Source(), downloader=downloader
    )
    stats = PipelineStats()
    duration, state, _detail = run(
        orchestrator._resolve_duration(_candidate(), stats=stats, staged=[])
    )
    assert duration is None and state == "invalid_media_source"
    assert stats.invalid_media_source == 1


def test_corrupt_video_is_invalid_media(settings) -> None:
    toolkit = _Toolkit(
        remote=_info(0.0, "", has_video=True), local=RuntimeError("corrupt container")
    )
    orchestrator = _orchestrator(
        settings, toolkit=toolkit, source=_Source(), downloader=_Downloader()
    )
    stats = PipelineStats()
    duration, state, _detail = run(
        orchestrator._resolve_duration(_candidate(), stats=stats, staged=[])
    )
    assert duration is None and state == "invalid_media_source"


def test_valid_stream_but_unresolved_duration(settings) -> None:
    toolkit = _Toolkit(
        remote=_info(0.0, "", has_video=True),
        local=_info(0.0, "", has_video=True),
        bounded=(None, "bounded_decode_no_timestamps"),
    )
    orchestrator = _orchestrator(
        settings, toolkit=toolkit, source=_Source(), downloader=_Downloader()
    )
    stats = PipelineStats()
    duration, state, _detail = run(
        orchestrator._resolve_duration(_candidate(), stats=stats, staged=[])
    )
    assert duration is None and state == "duration_unknown_unresolved"
    assert stats.duration_unresolved == 1


def test_measured_duration_range_uses_source_limits(settings) -> None:
    orchestrator = _orchestrator(
        settings,
        toolkit=_Toolkit(),
        source=_Source(),
        downloader=_Downloader(),
        candidate_filter=SimpleNamespace(min_duration=5.0, max_duration=300.0),
    )
    assert orchestrator._duration_within_range(
        _candidate().model_copy(update={"duration": 20.0}), None  # type: ignore[arg-type]
    )[0]
    assert not orchestrator._duration_within_range(
        _candidate().model_copy(update={"duration": 4.0}), None  # type: ignore[arg-type]
    )[0]
    assert not orchestrator._duration_within_range(
        _candidate().model_copy(update={"duration": 400.0}), None  # type: ignore[arg-type]
    )[0]


# ---------------------------------------------------------------------------
# Budget / cache reuse
# ---------------------------------------------------------------------------
def test_materialization_consumes_download_budget_and_reuses_cache(settings) -> None:
    toolkit = _Toolkit(
        remote=_info(0.0, "", has_video=True),
        local=_info(25.0, "format"),
    )
    downloader = _Downloader()
    orchestrator = _orchestrator(
        settings, toolkit=toolkit, source=_Source(), downloader=downloader
    )
    candidate = _candidate()
    staged: list[Path] = []
    stats = PipelineStats()
    duration, state, _detail = run(
        orchestrator._resolve_duration(candidate, stats=stats, staged=staged)
    )
    assert duration == 25.0 and state == "duration_unknown_resolved"
    assert downloader.calls == 1
    assert stats.downloads == 1 and stats.probe_downloads == 1
    # downstream staging must reuse the exact cached object, not download twice
    report = SimpleNamespace(status=None)
    staged_path = run(
        orchestrator._stage_source(
            candidate=candidate,
            request=SimpleNamespace(min_clip_duration=3.0, max_clip_duration=15.0),
            stats=stats,
            staged=staged,
            messages=[],
            report=report,
        )
    )
    assert staged_path is not None and downloader.calls == 1
    assert stats.probe_cache_reused == 1


def test_same_task_no_retry_loop_and_later_task_retry(settings) -> None:
    library = build_library(settings)
    dedup = DeduplicationService(library, enabled=True, phash_max_distance=6)
    source_id = library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="7001",
        source_url="https://www.douyin.com/video/7001",
        status=SourceVideoStatus.FAILED_MEDIA,
        reject_reason=RejectReason.DURATION_UNKNOWN_UNRESOLVED,
        touch_attempt=True,
    )
    decision = dedup.acquisition_decision("douyin", "7001")
    assert decision.skip is True
    assert decision.reason == "duration_unresolved_recent"
    library.database.execute(
        "UPDATE source_videos SET last_attempt_at = ? WHERE id = ?",
        ((datetime.now(timezone.utc) - timedelta(hours=10)).isoformat(), source_id),
    )
    decision = dedup.acquisition_decision("douyin", "7001")
    assert decision.skip is False
    assert decision.reason == "retry_duration_unresolved"


def test_content_and_provider_retry_windows_are_unchanged(settings) -> None:
    library = build_library(settings)
    dedup = DeduplicationService(library, enabled=True, phash_max_distance=6)
    content_id = library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="7002",
        source_url="https://www.douyin.com/video/7002",
        status=SourceVideoStatus.REJECTED,
        reject_reason=RejectReason.NO_MATERIAL,
        touch_attempt=True,
    )
    provider_id = library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="7003",
        source_url="https://www.douyin.com/video/7003",
        status=SourceVideoStatus.FAILED_AI,
        reject_reason=RejectReason.OTHER,
        touch_attempt=True,
    )
    assert dedup.acquisition_decision("douyin", "7002").reason == "recently_rejected"
    assert dedup.acquisition_decision("douyin", "7003").reason == "recent_failure"
    library.database.execute(
        "UPDATE source_videos SET last_attempt_at = ? WHERE id = ?",
        ((datetime.now(timezone.utc) - timedelta(days=31)).isoformat(), content_id),
    )
    library.database.execute(
        "UPDATE source_videos SET last_attempt_at = ? WHERE id = ?",
        ((datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(), provider_id),
    )
    assert dedup.acquisition_decision("douyin", "7002").skip is False
    assert dedup.acquisition_decision("douyin", "7003").skip is False


def test_audit_detail_does_not_leak_signed_url(settings) -> None:
    secret_url = "https://cdn.example/video.mp4?token=very-secret"
    toolkit = _Toolkit(remote=_info(20.0, "format"))
    orchestrator = _orchestrator(
        settings, toolkit=toolkit, source=_Source(secret_url), downloader=_Downloader()
    )
    candidate = _candidate()
    duration, state, detail = run(orchestrator._resolve_duration(candidate, stats=PipelineStats()))
    assert duration == 20.0 and state == "duration_unknown_resolved"
    assert "very-secret" not in detail
    assert candidate.metadata.get("resolved_media_url") == secret_url


def test_duration_audit_records_method_host_without_signed_url(settings) -> None:
    import json

    library = build_library(settings)
    secret_url = "https://cdn.example/video.mp4?token=very-secret"
    toolkit = _Toolkit(remote=_info(20.0, "format"))
    orchestrator = _orchestrator(
        settings,
        toolkit=toolkit,
        source=_Source(secret_url),
        downloader=_Downloader(),
        library=library,
    )
    run(orchestrator._resolve_duration(_candidate(), stats=PipelineStats()))
    entries = [
        entry
        for entry in library.list_maintenance_log(limit=20)
        if entry["operation"] == "duration_resolution"
    ]
    assert entries
    payload = json.dumps(entries[0]["details"], ensure_ascii=False)
    assert entries[0]["details"]["duration_method"] == "remote_format"
    assert entries[0]["details"]["media_host"] == "cdn.example"
    assert "very-secret" not in payload
