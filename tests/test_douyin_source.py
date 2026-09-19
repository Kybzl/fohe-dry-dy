"""DouyinSource normalization, discovery backends and retry policy."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from core.models import RejectReason, SourceVideoStatus
from media.ffmpeg import MockMediaToolkit
from sources.base import SourceError
from sources.douyin import DouyinSource
from sources.douyin_backend import DouyinBackendClient
from sources.douyin_models import (
    content_to_candidate,
    content_to_video_info,
    extract_media_url,
)
from sources.douyin_search import (
    ArchiveSearchBackend,
    AuthorPostsSearchBackend,
    CandidateCollector,
    CompositeSearchBackend,
    KeywordSearchBackend,
    ManualUrlSearchBackend,
    MixPostsSearchBackend,
    prioritize_queries,
)


def run(coro):
    return asyncio.run(coro)


def content(
    content_id: str = "7123456789",
    *,
    title: str = "苹果片烘干实拍",
    description: str = "热泵烘干房里的苹果干",
    duration_ms: int = 48000,
    kind: str = "video",
    media: dict | None = None,
) -> dict:
    return {
        "platform": "douyin",
        "content_id": content_id,
        "kind": kind,
        "web_url": f"https://www.douyin.com/video/{content_id}",
        "title": title,
        "description": description,
        "created_at": "2026-09-01T10:00:00Z",
        "duration_ms": duration_ms,
        "is_deleted": False,
        "is_private": False,
        "author": {"nickname": "烘干老张", "sec_uid": "MS4wLjABAAAA", "uid": "99"},
        "stats": {"digg_count": 1200, "play_count": 45000, "comment_count": 33},
        "media": media
        or {
            "covers": [{"url": "https://cdn.example/cover.jpg"}],
            "video": {
                "url": "https://cdn.example/high.mp4?sig=1",
                "width": 1080,
                "height": 1920,
                "bitrate": 2_000_000,
                "watermark": False,
            },
            "streams": [
                {"url": "https://cdn.example/low.mp4", "bitrate": 400_000, "watermark": True}
            ],
        },
        "tags": ["烘干", "苹果"],
    }


class FakeBackend:
    """Minimal backend used to drive DouyinSource without HTTP."""

    def __init__(self, responses: list[dict], paths: list[str] | None = None) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []
        self.extra_paths = paths or []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/openapi.json":
            body = {
                "openapi": "3.1.0",
                "info": {"version": "5.0.3"},
                "paths": {
                    "/api/v1/{platform}/video": {"get": {}},
                    "/api/v1/archive": {"get": {}},
                    **{path: {"get": {}} for path in self.extra_paths},
                },
            }
            return httpx.Response(200, json=body)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        entry = self.responses[index]
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, tuple):
            return httpx.Response(entry[0], json=entry[1])
        return httpx.Response(200, json={"success": True, "data": entry, "error": None, "meta": {}})


def client_for(backend: FakeBackend, **kwargs) -> DouyinBackendClient:
    payload = {
        "base_url": "http://backend.test",
        "api_key": "dtk_test",
        "timeout": 5.0,
        "max_retries": 1,
        "backoff_seconds": 0.0,
        "task_wait_seconds": 0.0,
        "task_poll_interval_seconds": 0.01,
        "client_factory": lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(backend.handler), timeout=5.0
        ),
    }
    payload.update(kwargs)
    return DouyinBackendClient(**payload)


def source_for(backend: FakeBackend, tmp_path: Path, **kwargs) -> DouyinSource:
    payload = {
        "client": client_for(backend),
        "toolkit": MockMediaToolkit(),
        "preview_dir": tmp_path / "previews",
        "preview_frame_count": 4,
        "preview_max_width": 320,
    }
    payload.update(kwargs)
    return DouyinSource(**payload)


# ---------------------------------------------------------------------------
# query planning
# ---------------------------------------------------------------------------
def test_prioritize_queries_is_specific_first() -> None:
    queries = ["苹果干", "苹果", "苹果片烘干", "苹果热泵烘干", "苹果烘干房"]
    ordered = prioritize_queries(queries, material="苹果干")
    assert ordered.index("苹果热泵烘干") < ordered.index("苹果烘干房")
    assert ordered[-1] in ("苹果", "苹果干"), ordered
    assert ordered.index("苹果干") > 0


def test_source_plan_queries_uses_priorities() -> None:
    from core.keyword_expander import KeywordExpander

    source = source_for(FakeBackend([content()]), Path("."))
    planned = source.plan_queries(KeywordExpander(), "苹果干")
    assert planned[0] in ("苹果热泵烘干", "苹果烘干机", "苹果烘干房")
    assert len(planned) == 10


# ---------------------------------------------------------------------------
# candidate collector (section 12)
# ---------------------------------------------------------------------------
def test_candidate_collector_dedups_by_id_and_url() -> None:
    collector = CandidateCollector()
    first = content_to_candidate(content("1"), query="苹果干")
    duplicate_id = content_to_candidate(content("1"), query="苹果片烘干")
    duplicate_url = content_to_candidate(content("2"), query="苹果烘干")
    duplicate_url.source_url = first.source_url

    fresh = collector.add([first], query="苹果干")
    assert len(fresh) == 1
    assert collector.add([duplicate_id], query="苹果片烘干") == []
    assert collector.add([duplicate_url], query="苹果烘干") == []
    assert collector.unique_count == 1
    assert first.matched_queries == ["苹果干", "苹果片烘干", "苹果烘干"]
    assert collector.total_seen == 3


def test_normalized_url_ignores_query_and_trailing_slash() -> None:
    candidate = content_to_candidate(content("1"))
    candidate.source_url = "https://www.douyin.com/video/1/?a=b#frag"
    assert candidate.normalized_url == "https://www.douyin.com/video/1"


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------
def test_content_to_candidate_normalizes_fields() -> None:
    candidate = content_to_candidate(content(), query="苹果片烘干")
    assert candidate.platform == "douyin"
    assert candidate.platform_video_id == "7123456789"
    assert candidate.duration == 48.0
    assert candidate.author == "烘干老张"
    assert candidate.author_id == "MS4wLjABAAAA"
    assert candidate.published_at is not None
    assert candidate.statistics["digg_count"] == 1200
    assert candidate.matched_queries == ["苹果片烘干"]
    assert candidate.media_url == "https://cdn.example/high.mp4?sig=1"
    assert candidate.cover_url == "https://cdn.example/cover.jpg"
    assert candidate.metadata["tags"] == ["烘干", "苹果"]


def test_media_url_prefers_non_watermark_high_bitrate() -> None:
    payload = content(
        media={
            "video": {"url": "https://cdn.example/wm.mp4", "bitrate": 900_000, "watermark": True},
            "streams": [
                {"url": "https://cdn.example/best.mp4", "bitrate": 3_000_000, "watermark": False},
                {"url": "https://cdn.example/alt.mp4", "bitrate": 2_000_000, "watermark": False},
            ],
        }
    )
    assert extract_media_url(payload) == "https://cdn.example/best.mp4"


def test_image_album_is_marked_as_no_video() -> None:
    payload = content(
        kind="images",
        media={"images": [{"url": "https://cdn.example/1.jpg"}], "video": None, "streams": []},
    )
    candidate = content_to_candidate(payload)
    assert candidate.metadata["no_video"] is True
    assert extract_media_url(payload) is None


def test_video_info_reports_resolution_and_media_url() -> None:
    info = content_to_video_info(content())
    assert info.duration == 48.0
    assert (info.width, info.height) == (1080, 1920)
    assert info.metadata["author_id"] == "MS4wLjABAAAA"
    assert info.metadata["media_url"].startswith("https://cdn.example/high.mp4")


def test_missing_duration_is_none_not_zero() -> None:
    payload = content()
    payload.pop("duration_ms")
    assert content_to_candidate(payload).duration is None


# ---------------------------------------------------------------------------
# discovery backends
# ---------------------------------------------------------------------------
def test_archive_backend_pages_with_cursor(tmp_path: Path) -> None:
    backend = FakeBackend(
        [
            {"items": [content("1")], "cursor": "CUR2", "has_more": True},
            {"items": [content("2")], "cursor": None, "has_more": False},
        ]
    )
    client = client_for(backend)
    archive = ArchiveSearchBackend(client, page_size=1, max_pages=3)
    outcome = run(archive.search("苹果干", 10))
    assert [item.platform_video_id for item in outcome.candidates] == ["1", "2"]
    assert outcome.exhausted is True
    assert backend.requests[0].url.params["q"] == "苹果干"
    assert backend.requests[1].url.params["cursor"] == "CUR2"


def test_archive_backend_respects_limit(tmp_path: Path) -> None:
    backend = FakeBackend([{"items": [content("1"), content("2"), content("3")], "has_more": False}])
    outcome = run(ArchiveSearchBackend(client_for(backend)).search("苹果干", 2))
    assert len(outcome.candidates) == 2


def test_archive_backend_reports_backend_errors() -> None:
    backend = FakeBackend(
        [
            (
                500,
                {"success": False, "data": None, "error": {"code": "UPSTREAM", "message": "boom"}, "meta": {}},
            )
        ]
    )
    outcome = run(ArchiveSearchBackend(client_for(backend), max_pages=1).search("苹果干", 5))
    assert outcome.candidates == []
    assert outcome.notes and "failed" in outcome.notes[0]


def test_keyword_backend_is_unavailable_without_a_route() -> None:
    backend = FakeBackend([content()])
    keyword = KeywordSearchBackend(client_for(backend))
    available, note = run(keyword.probe())
    assert available is False
    assert "no keyword search" in note
    outcome = run(keyword.search("苹果干", 5))
    assert outcome.candidates == []


def test_keyword_backend_uses_an_available_route() -> None:
    backend = FakeBackend(
        [{"items": [content("9")], "has_more": False}], paths=["/api/v1/douyin/search"]
    )
    keyword = KeywordSearchBackend(client_for(backend))
    available, note = run(keyword.probe())
    assert available is True and "/api/v1/douyin/search" in note
    outcome = run(keyword.search("苹果干", 5))
    assert [item.platform_video_id for item in outcome.candidates] == ["9"]
    search_requests = [
        request for request in backend.requests if request.url.path == "/api/v1/douyin/search"
    ]
    assert search_requests, backend.paths
    assert search_requests[0].url.params["keyword"] == "苹果干"


def test_author_backend_ranks_keyword_matches_first() -> None:
    backend = FakeBackend(
        [
            {
                "items": [
                    content("1", title="工厂日常", description="无关内容"),
                    content("2", title="苹果片烘干流程", description="热泵烘干"),
                ],
                "has_more": False,
            }
        ]
    )
    source = AuthorPostsSearchBackend(
        client_for(backend), sec_user_ids=["MS4wLjABC"], page_size=10
    )
    available, note = run(source.probe())
    assert available and "author seed" in note
    outcome = run(source.search("苹果片烘干", 10))
    assert [item.platform_video_id for item in outcome.candidates] == ["2", "1"]
    assert outcome.candidates[0].metadata["keyword_match"] is True
    assert outcome.candidates[1].metadata["keyword_match"] is False


def test_author_backend_without_seeds_is_unavailable() -> None:
    available, note = run(AuthorPostsSearchBackend(client_for(FakeBackend([content()]))).probe())
    assert available is False and "no author seeds" in note


def test_mix_backend_reads_a_playlist() -> None:
    backend = FakeBackend([{"items": [content("7")], "has_more": False}])
    mix = MixPostsSearchBackend(client_for(backend), mix_ids=["mix-1"])
    outcome = run(mix.search("苹果干", 5))
    assert [item.platform_video_id for item in outcome.candidates] == ["7"]
    assert backend.requests[0].url.params["mix_id"] == "mix-1"


def test_manual_url_backend_parses_links() -> None:
    backend = FakeBackend([content("manual-1")])
    manual = ManualUrlSearchBackend(
        client_for(backend), urls=["https://www.douyin.com/video/manual-1"]
    )
    outcome = run(manual.search("苹果干", 5))
    assert [item.platform_video_id for item in outcome.candidates] == ["manual-1"]
    assert backend.requests[0].method == "POST"
    assert backend.requests[0].url.path == "/api/v1/parse"


def test_composite_backend_falls_back_and_collects_notes() -> None:
    backend = FakeBackend([{"items": [content("5")], "has_more": False}])
    client = client_for(backend)
    composite = CompositeSearchBackend(
        [
            KeywordSearchBackend(client),
            ArchiveSearchBackend(client),
            AuthorPostsSearchBackend(client, sec_user_ids=[]),
        ]
    )
    outcome = run(composite.search("苹果干", 5))
    assert [item.platform_video_id for item in outcome.candidates] == ["5"]
    assert "archive" in outcome.backend
    assert any("no keyword search" in note for note in outcome.notes)
    lines = "\n".join(run(composite.describe()))
    assert "keyword" in lines and "author_posts" in lines


# ---------------------------------------------------------------------------
# DouyinSource
# ---------------------------------------------------------------------------
def test_source_search_normalizes_candidates(tmp_path: Path) -> None:
    backend = FakeBackend([{"items": [content("1"), content("2")], "has_more": False}])
    source = source_for(backend, tmp_path)
    candidates = run(source.search("苹果干", 10))
    assert [item.platform_video_id for item in candidates] == ["1", "2"]
    assert all(item.duration == 48.0 for item in candidates)


def test_browser_discovered_url_is_handed_to_dtk(tmp_path: Path) -> None:
    """Browser discovery + dtk detail/media is the Milestone 3.5 handoff."""

    from sources.douyin_browser_search import DouyinBrowserSearchBackend
    from tests.test_douyin_browser_search import FakeContext, FakePage

    VIDEO_HTML = (
        '<html><body><a href="//www.douyin.com/video/7652321152866089979">a</a></body></html>'
    )
    detail = content("7652321152866089979", title="苹果烘干实拍")
    backend = FakeBackend([detail], paths=[])
    client = client_for(backend)
    page = FakePage(html_pages=[VIDEO_HTML])

    browser = DouyinBrowserSearchBackend(
        headless=True,
        page_settle_seconds=0.01,
        scroll_delay_seconds=0.0,
        context_factory=lambda: _fake_context(page),
    )
    source = DouyinSource(
        client=client,
        toolkit=MockMediaToolkit(),
        search_backends=[browser],
        preview_dir=tmp_path / "previews",
        preview_frame_count=2,
    )

    candidates = run(source.search("苹果干烘干", 5))
    assert [item.platform_video_id for item in candidates] == ["7652321152866089979"]
    candidate = candidates[0]
    assert candidate.source_url == "https://www.douyin.com/video/7652321152866089979"
    assert candidate.metadata["discovery"] == "browser"
    assert candidate.matched_queries == ["苹果干烘干"]

    # the dtk backend still owns metadata / media
    info = run(source.get_video_info("7652321152866089979"))
    assert info.title == "苹果烘干实拍"
    assert backend.requests, "dtk/content must be called for the discovered video"
    assert backend.requests[0].url.path == "/api/v1/douyin/video"
    assert run(source.get_download_url("7652321152866089979")).startswith("https://cdn.example")


async def _fake_context(page):
    from tests.test_douyin_browser_search import FakeContext

    return FakeContext(page)


def test_source_search_without_base_url_is_a_clear_error(tmp_path: Path) -> None:
    source = DouyinSource(client=DouyinBackendClient(base_url=""), toolkit=MockMediaToolkit())
    with pytest.raises(SourceError) as excinfo:
        run(source.search("苹果干", 1))
    assert "base_url" in str(excinfo.value)


def test_source_video_info(tmp_path: Path) -> None:
    backend = FakeBackend([content("1")])
    source = source_for(backend, tmp_path)
    info = run(source.get_video_info("1"))
    assert info.platform_video_id == "1"
    assert info.duration == 48.0
    assert backend.requests[0].url.path == "/api/v1/douyin/video"


def test_source_preview_samples_remote_frames(tmp_path: Path) -> None:
    backend = FakeBackend([content("1")])
    source = source_for(backend, tmp_path)
    preview = run(source.get_preview("1"))
    assert len(preview.frames) == 4
    assert all(frame.image_path and Path(frame.image_path).exists() for frame in preview.frames)
    assert all(str(frame.image_path).startswith(str(tmp_path)) for frame in preview.frames)
    assert preview.metadata["discovery"] == "remote_preview"


def test_source_preview_defers_to_download_when_disabled(tmp_path: Path) -> None:
    backend = FakeBackend([content("1")])
    source = source_for(backend, tmp_path, enable_remote_preview=False)
    preview = run(source.get_preview("1"))
    assert preview.frames == []
    assert preview.metadata["defer_to_download"] is True


def test_source_preview_defers_to_download_when_remote_sampling_is_empty(
    tmp_path: Path,
) -> None:
    backend = FakeBackend([content("1")])
    toolkit = MockMediaToolkit()
    calls = 0

    async def no_remote_frames(*args, **kwargs):
        nonlocal calls
        calls += 1
        return []

    toolkit.sample_remote_frames = no_remote_frames  # type: ignore[method-assign]
    source = source_for(backend, tmp_path, toolkit=toolkit)

    preview = run(source.get_preview("1"))

    assert preview.frames == []
    assert preview.metadata["defer_to_download"] is True
    assert "no frames" in preview.metadata["preview_error"]

    second = run(source.get_preview("1"))
    assert second.metadata["defer_to_download"] is True
    assert "bypassed" in second.metadata["preview_error"]
    assert calls == 1, "the failed remote path must be circuit-broken for this task"


def test_source_preview_without_media_url_reports_why(tmp_path: Path) -> None:
    payload = content(
        kind="images",
        media={"images": [{"url": "https://cdn.example/a.jpg"}], "streams": []},
    )
    source = source_for(FakeBackend([payload]), tmp_path)
    preview = run(source.get_preview("1"))
    assert preview.frames == []
    assert "no playable media URL" in preview.metadata["preview_error"]


def test_source_download_url_is_validated(tmp_path: Path) -> None:
    bad = content(media={"video": {"url": "file:///etc/passwd"}, "streams": []})
    source = source_for(FakeBackend([bad]), tmp_path)
    with pytest.raises(SourceError) as excinfo:
        run(source.get_download_url("1"))
    # a non-http(s) stream is never accepted as a downloadable media URL
    assert "media URL" in str(excinfo.value)


def test_source_download_url_refreshes_each_call(tmp_path: Path) -> None:
    backend = FakeBackend([content("1")])
    source = source_for(backend, tmp_path)
    first = run(source.get_download_url("1"))
    second = run(source.get_download_url("1"))
    assert first == second
    assert len(backend.requests) == 2, "the media URL must be fetched close to use"


def test_source_provenance_for_returns_attribution(tmp_path: Path) -> None:
    backend = FakeBackend([content("1")])
    source = source_for(backend, tmp_path)
    run(source.get_video_info("1"))
    provenance = source.provenance_for("1")
    assert provenance["source_author"] == "烘干老张"
    assert provenance["source_author_id"] == "MS4wLjABAAAA"
    assert provenance["source_publish_time"].startswith("2026-09-01")


# ---------------------------------------------------------------------------
# retry policy (section 14)
# ---------------------------------------------------------------------------
def _library(settings):
    from storage.database import Database
    from storage.library import MaterialLibrary

    library = MaterialLibrary(Database(settings.paths.database), settings.paths.library_root)
    library.initialize()
    return library


def test_already_processed_video_is_skipped(settings) -> None:
    from analyzers.candidate_filter import CandidateFilter
    from storage.dedup import DeduplicationService

    library = _library(settings)
    library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="1",
        source_url="https://www.douyin.com/video/1",
        status=SourceVideoStatus.PROCESSED,
    )
    filter_ = CandidateFilter(DeduplicationService(library))
    decision = filter_.evaluate(content_to_candidate(content("1")), material="苹果干")
    assert decision.accepted is False
    assert decision.reason is RejectReason.ALREADY_PROCESSED


def test_recently_rejected_video_is_skipped_but_old_one_is_retried(settings) -> None:
    from analyzers.candidate_filter import CandidateFilter
    from storage.dedup import DeduplicationService

    library = _library(settings)
    library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="1",
        source_url="https://www.douyin.com/video/1",
        status=SourceVideoStatus.REJECTED_PREVIEW,
        reject_reason=RejectReason.MULTI_REGION_SUBTITLE,
        touch_attempt=True,
    )
    dedup = DeduplicationService(library)
    filter_ = CandidateFilter(dedup, retry_rejected_after_days=30)
    recent = filter_.evaluate(content_to_candidate(content("1")), material="苹果干")
    assert recent.accepted is False

    # pretend the rejection happened 40 days ago
    library.database.execute(
        "UPDATE source_videos SET last_attempt_at = ? WHERE platform_video_id = '1'",
        ((utc_now().replace(year=2020)).isoformat(),),
    )
    allowed = filter_.evaluate(content_to_candidate(content("1")), material="苹果干")
    assert allowed.accepted is True


def test_recent_failure_is_skipped(settings) -> None:
    from analyzers.candidate_filter import CandidateFilter
    from storage.dedup import DeduplicationService

    library = _library(settings)
    library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="2",
        source_url="https://www.douyin.com/video/2",
        status=SourceVideoStatus.FAILED_DOWNLOAD,
        touch_attempt=True,
    )
    filter_ = CandidateFilter(DeduplicationService(library), retry_failed_after_hours=24)
    decision = filter_.evaluate(content_to_candidate(content("2")), material="苹果干")
    assert decision.accepted is False
    assert "recent_failure" in decision.detail


from core.models import utc_now  # noqa: E402  (used by the retry test above)
