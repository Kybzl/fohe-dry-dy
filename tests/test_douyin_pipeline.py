"""End-to-end Douyin workflow with a fully mocked backend (no network).

Covers the acceptance path: search -> global dedup -> remote preview ->
AI filter -> download only accepted -> M2 pipeline -> provenance -> budget.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai.base import (
    ClipTaggingRequest,
    PreviewFilterRequest,
    SegmentDetectionRequest,
    VisionProvider,
)
from ai.gateway import AIGateway
from analyzers.candidate_filter import CandidateFilter
from analyzers.preview_filter import PreviewFilter
from analyzers.quality_gate import QualityGate
from analyzers.scene_refiner import SceneRefiner, StaticBoundaryDetector
from analyzers.video_analyzer import VideoAnalyzer
from core.models import (
    ClipScores,
    ClipTagging,
    ComplexityLevel,
    DetectedSegment,
    EditRole,
    MaterialForm,
    MaterialState,
    PreviewFilterResult,
    ProcessStage,
    RejectReason,
    SegmentDetectionResult,
    ShotType,
    SourceVideoStatus,
    SubtitlePolicy,
    SubtitleType,
    TaskRequest,
    TaskStatus,
)
from core.orchestrator import CollectionOrchestrator, OrchestratorDependencies
from media.clipper import ClipCutter
from media.downloader import MediaUrlExpiredError, VideoDownloader
from media.ffmpeg import MockMediaToolkit
from media.frame_sampler import FrameSampler
from media.placeholder import write_placeholder_mp4
from sources.douyin import DouyinSource
from sources.douyin_backend import DouyinBackendClient
from sources.douyin_search import ManualUrlSearchBackend
from storage.database import Database
from storage.dedup import DeduplicationService
from storage.library import MaterialLibrary


def run(coro):
    return asyncio.run(coro)


def content(
    content_id: str,
    *,
    title: str = "苹果片烘干实拍",
    duration_ms: int = 60000,
    media_url: str | None = None,
) -> dict:
    return {
        "content_id": content_id,
        "kind": "video",
        "web_url": f"https://www.douyin.com/video/{content_id}",
        "title": title,
        "description": f"{title} - 热泵烘干房",
        "created_at": "2026-09-01T08:00:00Z",
        "duration_ms": duration_ms,
        "author": {"nickname": "烘干老张", "sec_uid": "MS4wLjABAAAA"},
        "stats": {"digg_count": 999, "play_count": 12000},
        "media": {
            "covers": [{"url": f"https://cdn.example/{content_id}.jpg"}],
            "video": {
                "url": media_url or f"https://cdn.example/{content_id}.mp4?sig=1",
                "width": 1080,
                "height": 1920,
                "bitrate": 1_500_000,
                "watermark": False,
            },
            "streams": [],
        },
        "tags": ["烘干"],
    }


class BackendStub:
    """Serves /openapi.json, /archive and /douyin/video from memory."""

    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.requests: list[httpx.Request] = []
        self.detail_requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/openapi.json":
            body = {
                "openapi": "3.1.0",
                "info": {"version": "5.0.3"},
                "paths": {
                    "/api/v1/{platform}/video": {"get": {}},
                    "/api/v1/archive": {"get": {}},
                    "/api/v1/tasks/{task_id}": {"get": {}},
                },
            }
            return httpx.Response(200, json=body)
        if path == "/api/v1/archive":
            index = min(len(self.requests) - 1, len(self.pages) - 1)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": self.pages[index],
                    "error": None,
                    "meta": {"request_id": "r"},
                },
            )
        if path == "/api/v1/parse":
            body = json.loads(request.content or b"{}")
            url = str(body.get("url") or "")
            content_id = url.rstrip("/").rsplit("/", 1)[-1] or "777"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": content(content_id),
                    "error": None,
                    "meta": {"request_id": "r"},
                },
            )
        if path.endswith("/video"):
            self.detail_requests += 1
            aweme_id = request.url.params.get("aweme_id")
            payload = next(
                (
                    item
                    for page in self.pages
                    for item in page.get("items", [])
                    if item["content_id"] == aweme_id
                ),
                None,
            )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": payload,
                    "error": None,
                    "meta": {"request_id": "r"},
                },
            )
        return httpx.Response(
            404,
            json={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": f"no route {path}"},
                "meta": {},
            },
        )


class ScriptedProvider(VisionProvider):
    """Deterministic AI stand-in driven by the post title."""

    name = "scripted"

    async def preview_filter(self, request: PreviewFilterRequest) -> PreviewFilterResult:
        title = request.title or ""
        if "多字幕" in title or "价格表" in title:
            return PreviewFilterResult(
                accept=False,
                material_visible=True,
                material_relevance=0.7,
                subtitle_complexity=ComplexityLevel.HIGH,
                visual_complexity=ComplexityLevel.HIGH,
                quality_score=0.5,
                reject_reason=RejectReason.MULTI_REGION_SUBTITLE,
            )
        if "无关" in title:
            return PreviewFilterResult(
                accept=False,
                material_visible=False,
                material_relevance=0.05,
                subtitle_complexity=ComplexityLevel.LOW,
                visual_complexity=ComplexityLevel.LOW,
                quality_score=0.8,
                reject_reason=RejectReason.NO_MATERIAL,
            )
        return PreviewFilterResult(
            accept=True,
            material_visible=True,
            material_relevance=0.92,
            subtitle_complexity=ComplexityLevel.LOW,
            visual_complexity=ComplexityLevel.LOW,
            quality_score=0.88,
        )

    async def detect_segments(self, request: SegmentDetectionRequest) -> SegmentDetectionResult:
        # vary the description per video so two sources do not look identical
        # (the mock media backend makes identical content produce identical bytes)
        suffix = request.platform_video_id or "x"
        return SegmentDetectionResult(
            segments=[
                DetectedSegment(
                    start=4.0,
                    end=11.0,
                    description=f"苹果片均匀铺在托盘上 #{suffix}",
                    material_relevance=0.94,
                ),
                DetectedSegment(
                    start=18.0,
                    end=26.0,
                    description=f"烘干后的苹果干成品 #{suffix}",
                    material_relevance=0.9,
                ),
            ]
        )

    async def tag_clip(self, request: ClipTaggingRequest) -> ClipTagging:
        scores = ClipScores(
            material_relevance=0.94,
            visual_quality=0.9,
            subtitle_cleanliness=0.9,
            stability=0.9,
            composition=0.88,
            overall=0.92,
        )
        return ClipTagging(
            material="苹果",
            material_form=MaterialForm.SLICE,
            material_state=MaterialState.DRYING,
            process_stage=ProcessStage.TRAY_ARRANGEMENT,
            shot_type=ShotType.CLOSE_UP,
            subtitle_type=SubtitleType.BOTTOM_SIMPLE,
            subtitle_score=0.1,
            edit_roles=[EditRole.PROCESS, EditRole.DETAIL],
            description=request.segment_description or "苹果片铺盘",
            scores=scores,
        )


class StageAwareProvider(ScriptedProvider):
    async def tag_clip(self, request: ClipTaggingRequest) -> ClipTagging:
        tagging = await super().tag_clip(request)
        if "烘干后的" in request.segment_description:
            return tagging.model_copy(update={"process_stage": ProcessStage.DRYING})
        return tagging


class StubDownloader(VideoDownloader):
    """Writes placeholder videos; can simulate an expired URL once."""

    def __init__(self, *, expire_first: bool = False) -> None:
        self.expire_first = expire_first
        self.calls: list[str] = []
        self._expired = False

    async def download(self, url, dest: Path, *, metadata=None, headers=None) -> Path:
        self.calls.append(url)
        if self.expire_first and not self._expired:
            self._expired = True
            raise MediaUrlExpiredError("HTTP 403 expired")
        write_placeholder_mp4(dest, signature=url.encode("utf-8"))
        return dest


def build_orchestrator(
    settings,
    *,
    pages: list[dict],
    downloader: VideoDownloader | None = None,
    provider: VisionProvider | None = None,
    library: MaterialLibrary | None = None,
    cancel_event=None,
) -> tuple[CollectionOrchestrator, MaterialLibrary, BackendStub, StubDownloader]:
    backend = BackendStub(pages)
    client = DouyinBackendClient(
        base_url="http://backend.test",
        api_key="dtk_test",
        timeout=5.0,
        max_retries=1,
        backoff_seconds=0.0,
        task_wait_seconds=0.0,
        task_poll_interval_seconds=0.01,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(backend.handler), timeout=5.0
        ),
    )
    toolkit = MockMediaToolkit()
    library = library or MaterialLibrary(
        Database(settings.paths.database), settings.paths.library_root
    )
    library.initialize()
    gateway = AIGateway(
        provider or ScriptedProvider(), max_retries=1, timeout=5.0, on_call=library.add_ai_run
    )
    dedup = DeduplicationService(library)
    downloader = downloader or StubDownloader()
    source = DouyinSource(
        client=client,
        toolkit=toolkit,
        preview_dir=settings.paths.cache_dir / "previews",
        preview_frame_count=4,
        preview_max_width=320,
    )
    dependencies = OrchestratorDependencies(
        source=source,
        gateway=gateway,
        downloader=downloader,
        toolkit=toolkit,
        library=library,
        dedup=dedup,
        candidate_filter=CandidateFilter(dedup, min_duration=1.0, max_duration=600.0),
        preview_filter=PreviewFilter(gateway, policy=SubtitlePolicy.STRICT, max_frames=8),
        analyzer=VideoAnalyzer(
            gateway,
            FrameSampler(toolkit, max_frames=8),
            frame_count=8,
            min_segment_duration=3.0,
            max_segment_duration=15.0,
        ),
        scene_refiner=SceneRefiner(StaticBoundaryDetector(), tolerance_seconds=1.5),
        quality_gate=QualityGate(policy=SubtitlePolicy.STRICT),
        clipper=ClipCutter(toolkit, thumbnail_candidates=2),
        settings=settings,
    )
    return CollectionOrchestrator(dependencies, cancel_event=cancel_event), library, backend, downloader


def make_request(settings, *, target: int = 2, material: str = "苹果干") -> TaskRequest:
    return TaskRequest(
        material=material,
        target_clip_count=target,
        min_clip_duration=3.0,
        max_clip_duration=15.0,
        library_root=settings.paths.library_root,
        source="douyin",
        provider="mock",
        media_backend="mock",
    )


def test_search_limit_overscans_plan_evaluation_share(settings) -> None:
    """A one-candidate plan share must not hide later search results."""

    orchestrator, _library, _backend, _downloader = build_orchestrator(
        settings, pages=[{"items": [], "has_more": False, "cursor": None}]
    )
    request = make_request(settings, target=1)
    request.max_candidates = 1

    assert orchestrator._search_limit(request) == min(
        settings.sources.douyin.max_candidates_per_query,
        settings.collection.initial_candidate_multiplier,
    )


PAGE = {
    "items": [
        content("1"),  # accepted
        content("1"),  # duplicate of the one above
        content("2", title="苹果干价格表多字幕版"),  # subtitle rejection
        content("3", title="与烘干无关的厂区航拍"),  # no material
        content("4"),  # accepted
    ],
    "has_more": False,
    "cursor": None,
}


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------
def test_douyin_end_to_end_creates_clips_with_provenance(settings) -> None:
    orchestrator, library, backend, downloader = build_orchestrator(settings, pages=[PAGE])
    # target 3 forces the run past the first source so the dedup and rejection
    # paths are actually exercised before the target is reached.
    result = run(orchestrator.collect(make_request(settings, target=3)))

    assert result.status is TaskStatus.SUCCEEDED
    # stop-on-target: the third clip lands while source "4" is being processed
    assert len(result.clips) == 3
    stats = result.stats
    assert stats.searched_candidates == 5
    assert stats.unique_candidates == 4, "the duplicated content id must collapse"
    assert stats.examined_candidates == 5
    assert stats.prescreened == 4
    assert stats.subtitle_rejected == 1
    assert stats.other_rejected == 1
    assert stats.downloads == 2
    assert stats.analyzed == 2
    assert {clip.platform_video_id for clip in result.clips} == {"1", "4"}

    for clip in result.clips:
        assert clip.platform == "douyin"
        assert clip.source_url.startswith("https://www.douyin.com/video/")
        assert clip.source_title
        assert clip.source_author == "烘干老张"
        assert clip.source_author_id == "MS4wLjABAAAA"
        assert clip.source_publish_time is not None
        assert Path(clip.file_path).exists()
        assert clip.tags

    # provenance is persisted, not just returned
    stored = library.list_clips(material="苹果")
    assert {clip.source_author for clip in stored} == {"烘干老张"}
    assert all(clip.source_title for clip in stored)

    assert len(result.source_videos) == 4, "one report row per unique source video"

    # search yield metrics (section 31)
    yields = library.list_search_yields(task_id=result.task_id)
    assert yields and yields[0]["candidate_count"] == 5
    assert yields[0]["unique_candidate_count"] == 4
    assert yields[0]["final_clip_count"] == 3

    # AI audit rows exist for every call
    assert len(library.list_ai_runs(task_id=result.task_id)) >= 4


def test_target_stage_skips_earlier_adjacent_segment(settings) -> None:
    orchestrator, _library, _backend, _downloader = build_orchestrator(
        settings,
        pages=[{"items": [content("stage-target")], "has_more": False}],
        provider=StageAwareProvider(),
    )
    request = make_request(settings, target=1)
    request.target_process_stage = ProcessStage.DRYING

    result = run(orchestrator.collect(request))

    assert len(result.clips) == 1
    assert result.clips[0].process_stage is ProcessStage.DRYING
    report = result.source_videos[0]
    assert report.segments[0].reject_reason is RejectReason.TARGET_STAGE_MISMATCH


def test_only_accepted_candidates_are_downloaded(settings) -> None:
    orchestrator, _library, _backend, downloader = build_orchestrator(settings, pages=[PAGE])
    run(orchestrator.collect(make_request(settings, target=3)))
    assert len(downloader.calls) == 2
    assert all("cdn.example" in url for url in downloader.calls)


def test_matched_queries_merge_across_keywords(settings) -> None:
    """The same post found by several search terms keeps every term (section 12)."""

    orchestrator, library, _backend, _downloader = build_orchestrator(settings, pages=[PAGE])
    # an unreachable target keeps the run walking all ten keywords
    result = run(orchestrator.collect(make_request(settings, target=99)))
    record = library.get_source_video("douyin", "1")
    assert record is not None
    assert len(record.matched_queries) > 1, record.matched_queries
    assert "苹果热泵烘干" in record.matched_queries
    assert len(result.source_videos) == 4, "duplicates must not add report rows"


def test_rejected_candidates_are_not_downloaded(settings) -> None:
    rejected_page = {
        "items": [
            content("9", title="苹果干价格表多字幕版"),
            content("10", title="与烘干无关的厂区航拍"),
        ],
        "has_more": False,
    }
    orchestrator, library, _backend, downloader = build_orchestrator(
        settings, pages=[rejected_page]
    )
    result = run(orchestrator.collect(make_request(settings, target=2)))
    assert downloader.calls == []
    assert result.clips == []
    statuses = {report.platform_video_id: report.status for report in result.source_videos}
    assert statuses["9"] is SourceVideoStatus.REJECTED_PREVIEW
    assert statuses["10"] is SourceVideoStatus.REJECTED_PREVIEW


def test_cache_is_cleaned_after_the_run(settings) -> None:
    orchestrator, _library, _backend, _downloader = build_orchestrator(settings, pages=[PAGE])
    run(orchestrator.collect(make_request(settings, target=2)))
    leftovers = [path for path in settings.paths.cache_dir.rglob("*") if path.is_file()]
    assert leftovers == [], leftovers


# ---------------------------------------------------------------------------
# budgets, stop conditions, resume
# ---------------------------------------------------------------------------
def test_stops_as_soon_as_the_target_is_reached(settings) -> None:
    big_page = {
        "items": [content(str(index)) for index in range(1, 9)],
        "has_more": False,
    }
    orchestrator, _library, _backend, downloader = build_orchestrator(
        settings, pages=[big_page]
    )
    result = run(orchestrator.collect(make_request(settings, target=1)))
    assert len(result.clips) == 1
    assert result.stats.analyzed == 1, "must stop after the first source that yields clips"
    assert len(downloader.calls) == 1


def test_download_budget_stops_the_task(settings) -> None:
    settings.collection.max_source_downloads_per_task = 1
    big_page = {"items": [content(str(index)) for index in range(1, 6)], "has_more": False}
    orchestrator, _library, _backend, downloader = build_orchestrator(
        settings, pages=[big_page]
    )
    result = run(orchestrator.collect(make_request(settings, target=5)))
    assert len(downloader.calls) == 1
    assert result.status is TaskStatus.PARTIAL
    assert any("下载上限" in message for message in result.messages)


def test_candidate_budget_limits_search_work(settings) -> None:
    settings.collection.max_candidates_per_task = 2
    orchestrator, _library, _backend, _downloader = build_orchestrator(settings, pages=[PAGE])
    result = run(orchestrator.collect(make_request(settings, target=5)))
    assert result.stats.examined_candidates <= 2
    assert any("候选上限" in message for message in result.messages)


def test_resume_continues_without_duplicating_clips(settings) -> None:
    settings.media.thumbnail_candidates = 1
    orchestrator, library, _backend, _downloader = build_orchestrator(
        settings, pages=[{"items": [content("1")], "has_more": False}]
    )
    first = run(orchestrator.collect(make_request(settings, target=3)))
    assert len(first.clips) == 2
    assert first.status is TaskStatus.PARTIAL

    # resume the same task with another source video available
    settings.collection.max_source_downloads_per_task = 50
    resumed_orchestrator, _library, _backend2, _downloader2 = build_orchestrator(
        settings,
        pages=[{"items": [content("1"), content("42")], "has_more": False}],
        library=library,
    )
    request = make_request(settings, target=4)
    request.resume_task_id = first.task_id
    second = run(resumed_orchestrator.collect(request, resume_task_id=first.task_id))

    assert second.task_id == first.task_id
    assert len(second.clips) > len(first.clips)
    # source 1 is already processed: not downloaded again
    assert all("42" in url for url in _downloader2.calls)
    content_keys = [
        row["content_key"]
        for row in library.database.query("SELECT content_key FROM clips")
    ]
    assert len(content_keys) == len(set(content_keys)), "resume must not duplicate clips"


def test_resume_skips_completed_queries_but_keeps_remaining_queries(settings) -> None:
    request = make_request(settings, target=4)
    request.explicit_queries = ["已完成搜索词", "待续采搜索词"]
    orchestrator, library, backend, _downloader = build_orchestrator(
        settings, pages=[{"items": [content("88")], "has_more": False}]
    )
    task_id = library.create_task(request, status=TaskStatus.PARTIAL)
    library.add_search_yield(
        task_id=task_id,
        platform="douyin",
        query="已完成搜索词",
        stop_reason="completed",
        actual_order=1,
    )

    result = run(orchestrator.collect(request, resume_task_id=task_id))

    archive_requests = [
        call for call in backend.requests if call.url.path == "/api/v1/archive"
    ]
    assert len(archive_requests) == 1
    assert archive_requests[0].url.params["q"] == "待续采搜索词"
    assert any("已跳过 1 个完成的搜索词" in message for message in result.messages)


def test_exhausted_query_persists_completed_checkpoint(settings) -> None:
    request = make_request(settings, target=4)
    request.explicit_queries = ["单一搜索词"]
    orchestrator, library, _backend, _downloader = build_orchestrator(
        settings, pages=[{"items": [], "has_more": False}]
    )

    result = run(orchestrator.collect(request))

    yields = library.list_search_yields(task_id=result.task_id)
    assert len(yields) == 1
    assert yields[0]["stop_reason"] == "completed"


def test_cancel_stops_the_task(settings) -> None:
    import threading

    cancel = threading.Event()
    cancel.set()
    orchestrator, _library, _backend, downloader = build_orchestrator(
        settings, pages=[PAGE], cancel_event=cancel
    )
    result = run(orchestrator.collect(make_request(settings, target=2)))
    assert result.status is TaskStatus.CANCELLED
    assert downloader.calls == []


# ---------------------------------------------------------------------------
# media URL expiry
# ---------------------------------------------------------------------------
def test_expired_media_url_is_refreshed_once(settings) -> None:
    downloader = StubDownloader(expire_first=True)
    orchestrator, _library, backend, _downloader = build_orchestrator(
        settings, pages=[{"items": [content("1")], "has_more": False}], downloader=downloader
    )
    result = run(orchestrator.collect(make_request(settings, target=1)))
    assert result.clips, "the second attempt must succeed"
    assert len(downloader.calls) == 2
    assert backend.detail_requests >= 2, "the media URL is re-resolved after a 403"
    assert any("过期" in message for message in result.messages)


# ---------------------------------------------------------------------------
# manual URL workflow (section 39)
# ---------------------------------------------------------------------------
def test_query_seed_runs_first(settings) -> None:
    """``--douyin-search 苹果干烘干`` keeps the phrase as the first query."""

    orchestrator, _library, _backend, _downloader = build_orchestrator(settings, pages=[PAGE])
    request = make_request(settings, target=1, material="苹果干")
    request.query_seed = "苹果干烘干"
    result = run(orchestrator.collect(request))
    assert result.queries[0] == "苹果干烘干"
    assert len(set(result.queries)) == len(result.queries)


def test_manual_douyin_url_is_processed(settings) -> None:
    backend = BackendStub([{"items": [content("777")], "has_more": False}])
    client = DouyinBackendClient(
        base_url="http://backend.test",
        api_key="dtk_test",
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(backend.handler), timeout=5.0
        ),
        task_wait_seconds=0.0,
        max_retries=1,
    )
    toolkit = MockMediaToolkit()
    library = MaterialLibrary(Database(settings.paths.database), settings.paths.library_root)
    library.initialize()
    gateway = AIGateway(ScriptedProvider(), max_retries=1, timeout=5.0, on_call=library.add_ai_run)
    dedup = DeduplicationService(library)
    manual_source = DouyinSource(
        client=client,
        toolkit=toolkit,
        search_backends=[ManualUrlSearchBackend(client, urls=["https://v.douyin.com/777/"])],
        preview_dir=settings.paths.cache_dir / "previews",
        preview_frame_count=2,
    )
    dependencies = OrchestratorDependencies(
        source=manual_source,
        gateway=gateway,
        downloader=StubDownloader(),
        toolkit=toolkit,
        library=library,
        dedup=dedup,
        candidate_filter=CandidateFilter(dedup, min_duration=1.0, max_duration=600.0),
        preview_filter=PreviewFilter(gateway, policy=SubtitlePolicy.STRICT),
        analyzer=VideoAnalyzer(gateway, FrameSampler(toolkit, max_frames=8)),
        scene_refiner=SceneRefiner(StaticBoundaryDetector()),
        quality_gate=QualityGate(policy=SubtitlePolicy.STRICT),
        clipper=ClipCutter(toolkit, thumbnail_candidates=1),
        settings=settings,
    )
    result = run(CollectionOrchestrator(dependencies).collect(make_request(settings, target=1)))
    assert result.clips and result.clips[0].platform_video_id == "777"
    assert any(request.url.path == "/api/v1/parse" for request in backend.requests)
