"""Browser discovery: URL parsing, page states, bounded scrolling, routing.

No real browser and no network: page objects are faked and the DOM parsing is
exercised with minimal synthetic fixtures (section 26).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from sources.douyin_browser_search import (
    BrowserSearchResult,
    SEARCH_URL_TEMPLATE,
    SessionCheck,
    DouyinBrowserSearchBackend,
    build_discovered,
    dedupe_discovered,
    extract_pairs_from_html,
    extract_video_id,
    extract_video_ids_from_html,
    is_video_url,
    normalize_video_url,
)
from sources.douyin_search import (
    BrowserSearchStatus,
    CandidateCollector,
    DiscoveredDouyinVideo,
    DiscoveryBackend,
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# URL parsing (section 7)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "https://www.douyin.com/video/7652321152866089979",
        "https://www.douyin.com/video/7652321152866089979?previous_page=search",
        "//www.douyin.com/video/7652321152866089979",
        "https://www.douyin.com/note/7654146961570338534",
    ],
)
def test_is_video_url_accepts_public_permalinks(url: str) -> None:
    assert is_video_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.douyin.com/search/%E8%8B%B9%E6%9E%9C%E5%B9%B2",
        "https://www.douyin.com/user/MS4wLjABAAAA",
        "https://www.douyin.com/",
        "https://live.douyin.com/123",
        "https://evil.example/video/7652321152866089979",
        "",
        "not a url",
    ],
)
def test_is_video_url_rejects_non_video_pages(url: str) -> None:
    assert is_video_url(url) is False


def test_normalize_video_url_strips_tracking_and_keeps_id() -> None:
    dirty = "https://www.douyin.com/video/7652321152866089979?previous_page=search&modeFrom=user"
    assert normalize_video_url(dirty) == "https://www.douyin.com/video/7652321152866089979"
    assert normalize_video_url("//www.douyin.com/video/7652321152866089979") == (
        "https://www.douyin.com/video/7652321152866089979"
    )
    assert normalize_video_url("https://www.douyin.com/note/7654146961570338534") == (
        "https://www.douyin.com/video/7654146961570338534"
    )


def test_extract_video_id() -> None:
    assert extract_video_id("https://www.douyin.com/video/7652321152866089979") == (
        "7652321152866089979"
    )
    assert extract_video_id("https://www.douyin.com/search/abc") is None
    assert extract_video_id("https://www.douyin.com/video/123") is None  # too short


def test_search_url_template_handles_chinese_queries() -> None:
    from urllib.parse import quote

    url = SEARCH_URL_TEMPLATE.format(query=quote("苹果干烘干", safe=""))
    assert url.startswith("https://www.douyin.com/search/")
    assert "%E8%8B%B9%E6%9E%9C" in url
    assert "苹果" not in url


# ---------------------------------------------------------------------------
# HTML/DOM parsing (section 6/23)
# ---------------------------------------------------------------------------
MINIMAL_SEARCH_HTML = """
<html><body>
  <div data-e2e="scroll-list">
    <a href="//www.douyin.com/video/7652321152866089979?previous_page=search">卡一</a>
    <a href="/video/7654146961570338534?previous_page=search">卡二</a>
    <a href="https://www.douyin.com/user/MS4wLjABAAAA">作者</a>
    <a href="https://www.douyin.com/search/%E8%8B%B9%E6%9E%9C">搜索</a>
  </div>
  <script>window.__DATA__ = {"aweme_id": "7660000000000000001"};</script>
</body></html>
"""


def test_extract_video_ids_from_html_includes_links_and_json() -> None:
    ids = extract_video_ids_from_html(MINIMAL_SEARCH_HTML)
    assert ids[:2] == ["7652321152866089979", "7654146961570338534"]
    assert "7660000000000000001" in ids
    assert "MS4wLjABAAAA" not in ids


def test_extract_pairs_from_html_returns_normalized_urls() -> None:
    pairs = extract_pairs_from_html(MINIMAL_SEARCH_HTML)
    urls = [url for url, _title in pairs]
    assert urls[0] == "https://www.douyin.com/video/7652321152866089979"
    assert len(urls) == 2, "search/user anchors must never become candidates"


def test_build_discovered_creates_typed_models_and_dedupes() -> None:
    urls = [
        "https://www.douyin.com/video/7652321152866089979?x=1",
        "https://www.douyin.com/video/7652321152866089979",
        "https://www.douyin.com/search/abc",
        "https://www.douyin.com/video/7654146961570338534",
    ]
    items = build_discovered(urls, query="苹果干烘干")
    assert [item.platform_video_id for item in items] == [
        "7652321152866089979",
        "7654146961570338534",
    ]
    assert all(item.search_query == "苹果干烘干" for item in items)
    assert all(item.discovery_backend == DiscoveryBackend.BROWSER.value for item in items)
    assert items[0].source_url == "https://www.douyin.com/video/7652321152866089979"


def test_dedupe_discovered_respects_limit_and_order() -> None:
    items = [
        DiscoveredDouyinVideo(platform_video_id=str(index), source_url=f"https://www.douyin.com/video/{index:019d}")
        for index in range(1, 6)
    ]
    assert [item.platform_video_id for item in dedupe_discovered(items, limit=3)] == ["1", "2", "3"]


def test_to_candidate_maps_discovery_metadata() -> None:
    item = DiscoveredDouyinVideo(
        platform_video_id="7652321152866089979",
        source_url="https://www.douyin.com/video/7652321152866089979",
        search_query="苹果干烘干",
        visible_title="苹果烘干实拍",
        visible_author="烘干老张",
        discovery_backend=DiscoveryBackend.BROWSER.value,
        detail={"content_id": "7652321152866089979", "title": "苹果烘干实拍"},
    )
    candidate = item.to_candidate()
    assert candidate.platform_video_id == "7652321152866089979"
    assert candidate.title == "苹果烘干实拍"
    assert candidate.author == "烘干老张"
    assert candidate.matched_queries == ["苹果干烘干"]
    assert candidate.metadata["discovery"] == "browser"
    assert candidate.metadata["dtk_detail"]["title"] == "苹果烘干实拍"


def test_candidate_collector_merges_queries_for_discovered_items() -> None:
    collector = CandidateCollector()
    first = build_discovered(["https://www.douyin.com/video/7652321152866089979"], query="苹果干烘干")
    again = build_discovered(["https://www.douyin.com/video/7652321152866089979"], query="苹果片烘干")
    assert len(collector.add_discovered(first, query="苹果干烘干")) == 1
    assert collector.add_discovered(again, query="苹果片烘干") == []
    assert collector.discovered[0].matched_queries == ["苹果干烘干", "苹果片烘干"]


# ---------------------------------------------------------------------------
# fake page/context used by the backend tests
# ---------------------------------------------------------------------------
class FakeMouse:
    def __init__(self, page: "FakePage") -> None:
        self.page = page

    async def wheel(self, _x: int, _y: int) -> None:
        self.page.scrolls += 1


class FakeLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count


class FakePage:
    """Mimics the subset of the Playwright page API the backend uses."""

    def __init__(
        self,
        *,
        html_pages: list[str],
        title: str = "苹果干精彩视频 - 抖音",
        navigation_error: Exception | None = None,
        status: int = 200,
        status_sequence: list[int] | None = None,
        has_containers: bool = True,
    ) -> None:
        self.html_pages = html_pages
        self.title_text = title
        self.navigation_error = navigation_error
        self.status = status
        self.status_sequence = status_sequence
        self.navigations = 0
        self.scrolls = 0
        self.url = ""
        self.closed = False
        self.mouse = FakeMouse(self)
        self._has_containers = has_containers
        self.anchors: list[str] = []

    async def goto(self, url: str, **_kwargs: Any) -> Any:
        self.url = url
        self.navigations += 1
        if self.navigation_error is not None:
            raise self.navigation_error
        status = self.status
        if self.status_sequence:
            index = min(self.navigations - 1, len(self.status_sequence) - 1)
            status = self.status_sequence[index]
        return type("Response", (), {"status": status})()

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def title(self) -> str:
        return self.title_text

    async def content(self) -> str:
        index = min(self.scrolls, len(self.html_pages) - 1)
        return self.html_pages[index]

    async def eval_on_selector_all(self, selector: str, _script: str) -> list[Any]:
        import re

        html = await self.content()
        if "href" in selector:
            return re.findall(r'href="([^"]+)"', html)
        return []

    def locator(self, _selector: str) -> FakeLocator:
        return FakeLocator(1 if self._has_containers else 0)

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False

    async def new_page(self) -> FakePage:
        return self.page

    def set_default_navigation_timeout(self, _ms: int) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


def make_backend(page: FakePage, **kwargs: Any) -> DouyinBrowserSearchBackend:
    payload: dict[str, Any] = {
        "context_factory": lambda: _context(page),
        "headless": True,
        "page_settle_seconds": 0.01,
        "scroll_delay_seconds": 0.0,
        "max_scrolls_per_query": 3,
        "max_results_per_query": 10,
        # gateway retries stay off unless a test asks for them
        "upstream_retry_count": 0,
        "upstream_retry_backoff_seconds": 0.0,
    }
    payload.update(kwargs)
    return DouyinBrowserSearchBackend(**payload)


async def _context(page: FakePage) -> FakeContext:
    return FakeContext(page)


VIDEO_HTML = """
<html><body><div data-e2e="scroll-list">
<a href="//www.douyin.com/video/7652321152866089979">a</a>
<a href="/video/7654146961570338534">b</a>
</div></body></html>
"""

MORE_VIDEO_HTML = """
<html><body><div data-e2e="scroll-list">
<a href="//www.douyin.com/video/7652321152866089979">a</a>
<a href="/video/7654146961570338534">b</a>
<a href="/video/7660000000000000001">c</a>
</div></body></html>
"""

CHALLENGE_HTML = """
<html><head><title>验证中间页</title></head><body>
<iframe src="https://rmc.bytedance.com/verifycenter/captcha/v2?subtype=slide"></iframe>
<script src="https://lf-cdn.sec.bytescm.com/captcha/index.js"></script>
</body></html>
"""

LOGIN_HTML = """
<html><body><div id="login-full-panel-x">扫码登录 登录后查看更多内容</div></body></html>
"""

CAPTCHA_HTML = """
<html><body>
<script src="https://lf-rc1.yhgfb-cn-static.com/obj/rc-verifycenter/rmc-captcha/index.js"></script>
<div>请完成安全验证</div>
</body></html>
"""

#: rendered edge failure page (Douyin's kngx edge answers this shape)
GATEWAY_502_HTML = """
<html><head><title>验证中间页</title></head><body>
<center><h1>502 Bad Gateway</h1></center>
<hr><center>kngx/1.10.2</center>
</body></html>
"""

GATEWAY_503_HTML = "<html><body><h1>503 Service Unavailable</h1></body></html>"
GATEWAY_504_HTML = "<html><body><h1>504 Gateway Time-out</h1></body></html>"

EMPTY_RESULT_HTML = """
<html><body><div data-e2e="scroll-list"></div></body></html>
"""


# ---------------------------------------------------------------------------
# backend behaviour
# ---------------------------------------------------------------------------
def test_browser_search_returns_videos_and_scrolls_until_limit() -> None:
    page = FakePage(html_pages=[VIDEO_HTML, MORE_VIDEO_HTML])
    backend = make_backend(page, max_scrolls_per_query=3)
    outcome = run(backend.search("苹果干烘干", 3))
    assert outcome.status == BrowserSearchStatus.OK.value
    assert [item.platform_video_id for item in outcome.candidates] == [
        "7652321152866089979",
        "7654146961570338534",
        "7660000000000000001",
    ]
    assert page.url.startswith("https://www.douyin.com/search/")
    assert page.closed is True


def test_browser_search_stops_when_scrolling_yields_nothing_new() -> None:
    page = FakePage(html_pages=[VIDEO_HTML, VIDEO_HTML, VIDEO_HTML, VIDEO_HTML])
    backend = make_backend(page, max_scrolls_per_query=8)
    outcome = run(backend.search("苹果干烘干", 10))
    assert len(outcome.candidates) == 2
    assert page.scrolls <= 2, "must stop once a scroll produced no new videos"


def test_browser_search_respects_max_scrolls() -> None:
    page = FakePage(html_pages=[VIDEO_HTML])
    backend = make_backend(page, max_scrolls_per_query=2)
    outcome = run(backend.search("苹果干烘干", 10))
    assert outcome.status == BrowserSearchStatus.OK.value
    assert page.scrolls <= 2


def test_browser_search_respects_result_budget() -> None:
    page = FakePage(html_pages=[MORE_VIDEO_HTML])
    backend = make_backend(page, max_results_per_query=2)
    outcome = run(backend.search("苹果干烘干", 50))
    assert len(outcome.candidates) == 2


def test_verification_challenge_is_reported_not_bypassed() -> None:
    page = FakePage(html_pages=[CHALLENGE_HTML], title="验证中间页")
    backend = make_backend(page)
    outcome = run(backend.search("苹果干烘干", 5))
    assert outcome.status == BrowserSearchStatus.VERIFICATION_REQUIRED.value
    assert outcome.candidates == []
    assert "manual" in outcome.detail


def test_login_wall_is_reported() -> None:
    page = FakePage(html_pages=[LOGIN_HTML])
    backend = make_backend(page)
    outcome = run(backend.search("苹果干烘干", 5))
    assert outcome.status == BrowserSearchStatus.LOGIN_REQUIRED.value
    assert "--init-douyin-browser" in outcome.detail


def test_captcha_only_page_reports_verification_required() -> None:
    page = FakePage(html_pages=[CAPTCHA_HTML], title="抖音")
    backend = make_backend(page)
    outcome = run(backend.search("苹果干烘干", 5))
    assert outcome.status == BrowserSearchStatus.VERIFICATION_REQUIRED.value


def test_result_cards_without_links_report_dom_change() -> None:
    page = FakePage(html_pages=[EMPTY_RESULT_HTML], has_containers=True)
    backend = make_backend(page)
    outcome = run(backend.search("苹果干烘干", 5))
    assert outcome.status == BrowserSearchStatus.SEARCH_DOM_CHANGED.value


def test_page_without_result_cards_reports_no_results() -> None:
    page = FakePage(html_pages=["<html><body>nothing</body></html>"], has_containers=False)
    backend = make_backend(page)
    outcome = run(backend.search("苹果干烘干", 5))
    assert outcome.status == BrowserSearchStatus.NO_RESULTS.value


def test_navigation_failure_reports_douyin_unreachable() -> None:
    # Milestone 8.2: ``net::ERR_ABORTED`` is a recoverable self-navigation race
    # and is deliberately *not* "unreachable" anymore; a genuine network error
    # still is.
    page = FakePage(
        html_pages=[VIDEO_HTML],
        navigation_error=RuntimeError("net::ERR_NAME_NOT_RESOLVED"),
    )
    backend = make_backend(page)
    outcome = run(backend.search("苹果干烘干", 5))
    assert outcome.status == BrowserSearchStatus.DOUYIN_UNREACHABLE.value
    assert "navigation failed" in outcome.detail


def test_aborted_navigation_is_recovered_from_the_current_page() -> None:
    """M8.2 §4: an aborted goto classifies the page we actually landed on."""

    page = FakePage(
        html_pages=[VIDEO_HTML], navigation_error=RuntimeError("net::ERR_ABORTED")
    )
    backend = make_backend(page)
    outcome = run(backend.search("苹果干烘干", 5))
    assert outcome.status == BrowserSearchStatus.OK.value
    assert [item.platform_video_id for item in outcome.candidates] == [
        "7652321152866089979",
        "7654146961570338534",
    ]


def test_http_502_navigation_response_reports_upstream_bad_gateway() -> None:
    """Requirement 1: the navigation response status is checked directly."""

    page = FakePage(html_pages=[EMPTY_RESULT_HTML], status=502, has_containers=False)
    backend = make_backend(page, upstream_retry_count=0)
    outcome = run(backend.search("苹果干烘干", 5))
    assert outcome.status == BrowserSearchStatus.UPSTREAM_BAD_GATEWAY.value
    assert outcome.status != BrowserSearchStatus.VERIFICATION_REQUIRED.value
    assert "502" in outcome.detail
    assert outcome.diagnostics["http_status"] == 502


def test_rendered_502_gateway_page_is_detected() -> None:
    """Requirement 2: a rendered '502 Bad Gateway' page is not a CAPTCHA."""

    page = FakePage(html_pages=[GATEWAY_502_HTML], title="验证中间页")
    backend = make_backend(page, upstream_retry_count=0)
    outcome = run(backend.search("苹果干烘干", 5))
    assert outcome.status == BrowserSearchStatus.UPSTREAM_BAD_GATEWAY.value
    assert outcome.status != BrowserSearchStatus.VERIFICATION_REQUIRED.value
    assert outcome.status != BrowserSearchStatus.LOGIN_REQUIRED.value
    assert outcome.status != BrowserSearchStatus.SEARCH_DOM_CHANGED.value
    assert "not a CAPTCHA" in outcome.detail


def test_rendered_503_and_504_pages_report_upstream_http_error() -> None:
    for html in (GATEWAY_503_HTML, GATEWAY_504_HTML):
        page = FakePage(html_pages=[html], title="")
        backend = make_backend(page, upstream_retry_count=0)
        outcome = run(backend.search("苹果干烘干", 5))
        assert outcome.status == BrowserSearchStatus.UPSTREAM_HTTP_ERROR.value, html
        assert outcome.status != BrowserSearchStatus.VERIFICATION_REQUIRED.value


def test_http_503_and_504_navigation_responses_are_transient_upstream() -> None:
    for code in (503, 504):
        page = FakePage(html_pages=[EMPTY_RESULT_HTML], status=code, has_containers=False)
        backend = make_backend(page, upstream_retry_count=0)
        outcome = run(backend.search("苹果干烘干", 5))
        assert outcome.status == BrowserSearchStatus.UPSTREAM_HTTP_ERROR.value
        assert outcome.diagnostics["http_status"] == code


def test_gateway_page_is_not_classified_as_verification() -> None:
    """The observed real page: title 验证中间页 + body 502 Bad Gateway."""

    page = FakePage(html_pages=[GATEWAY_502_HTML], title="验证中间页")
    backend = make_backend(page, upstream_retry_count=0)
    outcome = run(backend.search("苹果干烘干", 5))
    assert outcome.status == BrowserSearchStatus.UPSTREAM_BAD_GATEWAY.value


def test_real_captcha_page_still_reports_verification_required() -> None:
    page = FakePage(html_pages=[CAPTCHA_HTML], title="抖音")
    backend = make_backend(page, upstream_retry_count=0)
    outcome = run(backend.search("苹果干烘干", 5))
    assert outcome.status == BrowserSearchStatus.VERIFICATION_REQUIRED.value


def test_upstream_retries_are_bounded_with_backoff() -> None:
    """A 502 is retried a small configured number of times, then given up."""

    page = FakePage(html_pages=[EMPTY_RESULT_HTML], status=502, has_containers=False)
    backend = make_backend(
        page, upstream_retry_count=2, upstream_retry_backoff_seconds=0.0
    )
    outcome = run(backend.search("苹果干烘干", 5))
    assert outcome.status == BrowserSearchStatus.UPSTREAM_BAD_GATEWAY.value
    assert outcome.diagnostics["attempts"] == 3, "1 initial attempt + 2 retries"


def test_upstream_retry_recovers_when_the_gateway_recovers() -> None:
    page = FakePage(html_pages=[VIDEO_HTML], status=502, status_sequence=[502, 200])
    backend = make_backend(page, upstream_retry_count=2, upstream_retry_backoff_seconds=0.0)
    outcome = run(backend.search("苹果干烘干", 5))
    assert outcome.status == BrowserSearchStatus.OK.value
    assert len(outcome.candidates) == 2
    assert outcome.diagnostics["attempts"] == 2


def test_upstream_failure_records_diagnostic_fields() -> None:
    """Requirement 3: url/status/title fields, never cookies or headers."""

    page = FakePage(html_pages=[GATEWAY_502_HTML], title="验证中间页", status=502)
    backend = make_backend(page, upstream_retry_count=0)
    outcome = run(backend.search("苹果干烘干", 5))
    diagnostics = outcome.diagnostics
    assert diagnostics["requested_url"].startswith("https://www.douyin.com/search/")
    assert diagnostics["final_url"].startswith("https://www.douyin.com/search/")
    assert diagnostics["http_status"] == 502
    assert diagnostics["page_title"] == "验证中间页"
    assert diagnostics["browser_status"] == BrowserSearchStatus.UPSTREAM_BAD_GATEWAY.value
    assert set(diagnostics) == {
        "requested_url",
        "final_url",
        "http_status",
        "page_title",
        "browser_status",
        "attempts",
        # section 7/8 of Milestone 3.6: which browser actually ran
        "browser_channel",
        "browser_executable",
        "profile_dir",
        "headless",
    }
    blob = json.dumps(diagnostics, ensure_ascii=False).lower()
    for forbidden in ("cookie", "authorization", "set-cookie", "token"):
        assert forbidden not in blob


def test_browser_crash_is_classified_and_context_recovered() -> None:
    class CrashingPage(FakePage):
        async def content(self) -> str:
            raise RuntimeError("Target closed")

    page = CrashingPage(html_pages=[VIDEO_HTML])
    backend = make_backend(page)
    outcome = run(backend.search("苹果干烘干", 5))
    assert outcome.status == BrowserSearchStatus.BROWSER_CRASHED.value
    assert backend._context is None, "a crashed context must be dropped for the next query"


def test_missing_playwright_reports_browser_unavailable(monkeypatch) -> None:
    backend = make_backend(FakePage(html_pages=[VIDEO_HTML]))
    monkeypatch.setattr(
        DouyinBrowserSearchBackend,
        "playwright_available",
        staticmethod(lambda: (False, "playwright package missing")),
    )
    outcome = run(backend.search("苹果干烘干", 5))
    assert outcome.status == BrowserSearchStatus.BROWSER_UNAVAILABLE.value


def test_context_is_reused_between_queries() -> None:
    page = FakePage(html_pages=[VIDEO_HTML])
    backend = make_backend(page)

    async def flow() -> None:
        await backend.search("苹果干烘干", 2)
        context_after_first = backend._context
        await backend.search("苹果片烘干", 2)
        assert backend._context is context_after_first
        await backend.close()

    run(flow())


def test_wall_stops_browser_search_for_the_rest_of_the_task() -> None:
    page = FakePage(html_pages=[LOGIN_HTML])
    backend = make_backend(page)

    async def flow() -> None:
        first = await backend.search("苹果干烘干", 3)
        assert first.status == BrowserSearchStatus.LOGIN_REQUIRED.value
        page.url = ""  # would be set again if a second page were opened
        second = await backend.search("苹果片烘干", 3)
        assert second.status == BrowserSearchStatus.LOGIN_REQUIRED.value
        assert page.url == "", "a wall must not be re-tried for every query"
        backend.reset_block()
        await backend.search("苹果片烘干", 3)
        assert page.url != "", "reset_block allows another attempt"
        await backend.close()

    run(flow())


def test_search_auto_resumes_same_query_after_verification() -> None:
    backend = DouyinBrowserSearchBackend(
        keep_page_on_challenge=True,
        auto_resume_after_verification=True,
        challenge_wait_timeout_seconds=8,
        challenge_poll_seconds=2,
    )
    video = build_discovered(
        ["https://www.douyin.com/video/7652321152866089979"],
        query="香菇装盘",
    )[0]
    results = [
        BrowserSearchResult(
            status=BrowserSearchStatus.VERIFICATION_REQUIRED.value,
            detail="slider",
        ),
        BrowserSearchResult(videos=[video], status=BrowserSearchStatus.OK.value),
    ]
    seen: list[str] = []

    async def open_stub() -> None:
        return None

    async def search_stub(query: str, limit: int) -> BrowserSearchResult:
        seen.append(query)
        return results.pop(0)

    async def wait_stub(*args, **kwargs) -> SessionCheck:
        assert kwargs["max_rounds"] == 4
        return SessionCheck(
            usable=True,
            status=BrowserSearchStatus.OK.value,
            video_ids=[video.platform_video_id],
            waited_seconds=2.0,
        )

    backend.open = open_stub  # type: ignore[method-assign]
    backend._search_once = search_stub  # type: ignore[method-assign]
    backend.wait_for_human_verification = wait_stub  # type: ignore[method-assign]

    outcome = run(backend.search("香菇装盘", 5))

    assert outcome.status == BrowserSearchStatus.OK.value
    assert [item.platform_video_id for item in outcome.candidates] == [
        video.platform_video_id
    ]
    assert seen == ["香菇装盘", "香菇装盘"]


def test_slow_mo_and_headless_defaults_come_from_config(settings) -> None:
    from core.dependencies import build_browser_search

    backend = build_browser_search(settings)
    assert backend.profile_dir == settings.project_root / settings.sources.douyin.browser_search.profile_dir
    assert backend.max_scrolls_per_query == settings.sources.douyin.browser_search.max_scrolls_per_query
    assert backend.max_results_per_query == settings.sources.douyin.browser_search.max_results_per_query


# ---------------------------------------------------------------------------
# discovery backend routing (section 15)
# ---------------------------------------------------------------------------
def _dtk_stub(openapi_paths: dict, *, items: list[dict] | None = None):
    """Small httpx-backed dtk client for routing tests."""

    import httpx

    from sources.douyin_backend import DouyinBackendClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200, json={"openapi": "3.1.0", "info": {"version": "5.0.3"}, "paths": openapi_paths}
            )
        if "search" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"items": items or [], "cursor": None, "has_more": False},
                    "error": None,
                    "meta": {},
                },
            )
        if request.url.path == "/api/v1/archive":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"items": items or [], "cursor": None, "has_more": False},
                    "error": None,
                    "meta": {},
                },
            )
        return httpx.Response(
            404,
            json={"success": False, "data": None, "error": {"code": "NOT_FOUND"}, "meta": {}},
        )

    return DouyinBackendClient(
        base_url="http://backend.test",
        api_key="k",
        task_wait_seconds=0.0,
        max_retries=1,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=5.0
        ),
    )


BASE_PATHS = {
    "/api/v1/{platform}/video": {"get": {}},
    "/api/v1/archive": {"get": {}},
    "/api/v1/tasks/{task_id}": {"get": {}},
}


def test_routing_prefers_dtk_keyword_search_when_the_backend_has_it() -> None:
    from sources.douyin_search import CompositeSearchBackend, KeywordSearchBackend

    items = [
        {
            "content_id": "7000000000000000001",
            "kind": "video",
            "web_url": "https://www.douyin.com/video/7000000000000000001",
            "title": "API 搜索命中",
        }
    ]
    client = _dtk_stub({**BASE_PATHS, "/api/v1/douyin/search": {"get": {}}}, items=items)
    browser_page = FakePage(html_pages=[VIDEO_HTML])
    browser = make_backend(browser_page)
    composite = CompositeSearchBackend([KeywordSearchBackend(client), browser])
    outcome = run(composite.search("苹果干烘干", 5))
    assert outcome.backend == "keyword"
    assert [item.platform_video_id for item in outcome.candidates] == ["7000000000000000001"]
    assert browser_page.url == "", "browser must not be used when the API can search"


def test_routing_falls_back_to_browser_without_keyword_search() -> None:
    from sources.douyin_search import (
        ArchiveSearchBackend,
        CompositeSearchBackend,
        KeywordSearchBackend,
    )

    client = _dtk_stub(BASE_PATHS)
    browser_page = FakePage(html_pages=[VIDEO_HTML])
    browser = make_backend(browser_page)
    composite = CompositeSearchBackend(
        [KeywordSearchBackend(client), browser, ArchiveSearchBackend(client)]
    )
    outcome = run(composite.search("苹果干烘干", 5))
    assert outcome.backend == "browser"
    assert len(outcome.candidates) == 2
    assert any("no keyword search" in note for note in outcome.notes)


def test_routing_falls_back_to_archive_when_browser_is_disabled() -> None:
    from sources.douyin_search import ArchiveSearchBackend, CompositeSearchBackend

    items = [
        {
            "content_id": "7652321152866089979",
            "kind": "video",
            "web_url": "https://www.douyin.com/video/7652321152866089979",
            "title": "苹果烘干实拍",
        }
    ]
    client = _dtk_stub(BASE_PATHS, items=items)
    composite = CompositeSearchBackend([ArchiveSearchBackend(client)])
    outcome = run(composite.search("苹果干烘干", 5))
    assert outcome.backend == "archive"
    assert outcome.candidates[0].discovery_backend == "archive"
