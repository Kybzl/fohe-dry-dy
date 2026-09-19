"""Milestone 8.2: post-verification navigation race (search_pending + ERR_ABORTED).

All fake Playwright objects; no real CAPTCHA, no network, no bypass.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import pytest

from sources.douyin_browser_search import (
    INTERACTIVE_NOTICE,
    MESSAGE_SELF_NAVIGATION,
    MESSAGE_VERIFICATION_CLEARED,
    DouyinBrowserSearchBackend,
    SessionCheck,
)
from sources.douyin_search import BrowserSearchStatus


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
CHALLENGE_HTML = """
<html><head><title>验证中间页</title></head><body>
<iframe src="https://rmc.bytedance.com/verifycenter/captcha/v2?subtype=slide"></iframe>
</body></html>
"""

#: the real cleared state observed in the M8.1 run: normal search title, SDK
#: still present, results not rendered yet
CLEARED_TITLE = "发现更多精彩视频 - 抖音搜索"
CLEARED_HTML = """
<html><head><title>发现更多精彩视频 - 抖音搜索</title></head><body>
<div class="search-shell">搜索结果</div>
<script src="https://lf-cdn.sec.bytescm.com/captcha/index.js"></script>
</body></html>
"""

VIDEO_HTML = """
<html><head><title>发现更多精彩视频 - 抖音搜索</title></head><body>
<div data-e2e="scroll-list">
<a href="//www.douyin.com/video/7652321152866089979">一</a>
<a href="/video/7654146961570338534">二</a>
</div></body></html>
"""

GATEWAY_HTML = """
<html><head><title>验证码中间页</title></head><body>
<h1>502 Bad Gateway</h1><p>kngx</p>
</body></html>
"""


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
    """Page whose html/title/url can change over time (a scripted transition)."""

    def __init__(
        self,
        *,
        html: str,
        title: str,
        url: str = "",
        has_containers: bool = False,
        search_ui: bool = False,
        goto_error: Exception | None = None,
        goto_effect: tuple[str, str, str, bool] | None = None,
    ) -> None:
        self.html = html
        self.title_text = title
        self.url = url
        self.has_containers = has_containers
        #: the search SPA rendered (search box / result shell) - distinguishes
        #: "hydrated but empty" from "still hydrating"
        self.search_ui = search_ui
        self.goto_error = goto_error
        #: (html, title, url, has_containers) applied when goto "succeeds"
        self.goto_effect = goto_effect
        self.navigations = 0
        self.scrolls = 0
        self.closed = False
        self.mouse = FakeMouse(self)

    def script(self, *, html: str, title: str, url: str = "", has_containers: bool = False):
        self.html, self.title_text, self.has_containers = html, title, has_containers
        if url:
            self.url = url

    async def goto(self, url: str, **_kwargs: Any) -> Any:
        self.navigations += 1
        if self.goto_error is not None:
            # the page navigated itself: it lands somewhere before raising
            if self.goto_effect is not None:
                html, title, effect_url, containers = self.goto_effect
                self.script(html=html, title=title, url=effect_url, has_containers=containers)
            raise self.goto_error
        self.url = url
        if self.goto_effect is not None:
            html, title, effect_url, containers = self.goto_effect
            self.script(html=html, title=title, url=effect_url or url, has_containers=containers)
        return type("Response", (), {"status": 200})()

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def title(self) -> str:
        return self.title_text

    async def content(self) -> str:
        return self.html

    async def eval_on_selector_all(self, selector: str, _script: str) -> list[Any]:
        if "href" in selector:
            return re.findall(r'href="([^"]+)"', self.html)
        return []

    def locator(self, selector: str) -> FakeLocator:
        if "scroll-list" in selector or "search-result-list" in selector:
            return FakeLocator(1 if self.has_containers else 0)
        if self.search_ui and (
            "searchbar" in selector
            or "input" in selector
            or "search-container" in selector
            or "search-shell" in selector
        ):
            return FakeLocator(1)
        return FakeLocator(0)

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False
        self.new_page_calls = 0

    async def new_page(self) -> FakePage:
        self.new_page_calls += 1
        return self.page

    def set_default_navigation_timeout(self, _ms: int) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


def make_backend(page: FakePage, context: FakeContext | None = None, **kwargs: Any):
    ctx = context or FakeContext(page)

    async def factory() -> FakeContext:
        return ctx

    payload: dict[str, Any] = {
        "context_factory": factory,
        "headless": False,
        "keep_page_on_challenge": True,
        "page_settle_seconds": 0.01,
        "scroll_delay_seconds": 0.0,
        "max_scrolls_per_query": 2,
        "max_results_per_query": 10,
        "upstream_retry_count": 0,
        "search_settle_timeout_seconds": 0.2,
        "search_settle_poll_seconds": 0.05,
    }
    payload.update(kwargs)
    return DouyinBrowserSearchBackend(**payload)


async def _operator_confirms() -> bool:
    return True


def make_check(backend: DouyinBrowserSearchBackend, **kwargs: Any) -> SessionCheck:
    payload: dict[str, Any] = {"on_message": lambda _t: None}
    payload.update(kwargs)
    return run(backend.check_session("苹果干烘干", limit=5, **payload))


# ---------------------------------------------------------------------------
# 11.1 cleared challenge + no links yet = search_pending (never verification)
# ---------------------------------------------------------------------------
def test_cleared_challenge_without_results_is_search_pending() -> None:
    page = FakePage(html=CHALLENGE_HTML, title="验证中间页")
    backend = make_backend(page)
    run(backend.search("苹果干烘干", 5))  # challenge detected, page kept
    assert backend._challenge_page is page


def test_navigation_content_race_stays_pending_and_keeps_page() -> None:
    class NavigatingPage(FakePage):
        async def content(self) -> str:
            raise RuntimeError(
                "Page.content: Unable to retrieve content because the page is "
                "navigating and changing the content."
            )

    page = NavigatingPage(html=CHALLENGE_HTML, title="验证码中间页")
    backend = make_backend(page)
    backend._challenge_page = page

    check = make_check(backend)

    assert check.status == BrowserSearchStatus.SEARCH_PENDING.value
    assert check.transitional is True
    assert backend._challenge_page is page
    assert backend._context is not None

    # the operator cleared it; Douyin is still hydrating the search page
    page.script(
        html=CLEARED_HTML,
        title=CLEARED_TITLE,
        url="https://www.douyin.com/search/%E8%8B%B9%E6%9E%9C%E5%B9%B2%E7%83%98%E5%B9%B2?type=video",
        has_containers=False,
    )
    check = make_check(backend)

    assert check.usable is False
    assert check.status == BrowserSearchStatus.SEARCH_PENDING.value
    assert check.transitional is True
    assert check.status != BrowserSearchStatus.VERIFICATION_REQUIRED.value
    assert "发现更多精彩视频" not in check.detail
    assert page.closed is False, "still transitional: keep the page open"


def test_normal_search_title_is_never_a_verification_wall() -> None:
    """The SDK script alone must not re-report a wall after verification."""

    page = FakePage(html=CLEARED_HTML, title=CLEARED_TITLE)
    backend = make_backend(page)
    check = make_check(backend)
    assert check.status == BrowserSearchStatus.SEARCH_PENDING.value
    assert check.status != BrowserSearchStatus.VERIFICATION_REQUIRED.value


# ---------------------------------------------------------------------------
# 11.2 no redundant goto when already on the target search
# ---------------------------------------------------------------------------
def test_current_target_search_page_is_reused_without_goto() -> None:
    target = "https://www.douyin.com/search/%E8%8B%B9%E6%9E%9C%E5%B9%B2%E7%83%98%E5%B9%B2?type=video"
    page = FakePage(html=VIDEO_HTML, title=CLEARED_TITLE, url=target, has_containers=True)
    backend = make_backend(page)
    check = make_check(backend)
    assert check.usable is True
    assert page.navigations == 0, "an already-correct search page must not be re-goto'd"
    assert check.reused_current_page is True
    assert check.video_ids == ["7652321152866089979", "7654146961570338534"]


def test_plain_search_variant_also_counts_as_the_target() -> None:
    page = FakePage(
        html=VIDEO_HTML,
        title=CLEARED_TITLE,
        url="https://www.douyin.com/search/苹果干烘干",
        has_containers=True,
    )
    backend = make_backend(page)
    check = make_check(backend)
    assert check.usable is True
    assert page.navigations == 0


def test_a_different_page_is_still_navigated_once() -> None:
    page = FakePage(
        html="<html><body>home</body></html>",
        title="抖音",
        url="https://www.douyin.com/",
        goto_effect=(VIDEO_HTML, CLEARED_TITLE, "", True),
    )
    backend = make_backend(page)
    check = make_check(backend)
    assert check.usable is True
    assert page.navigations == 1, "a neutral page is navigated exactly once"
    assert check.reused_current_page is False


# ---------------------------------------------------------------------------
# 11.3/11.4/11.5/11.6 ERR_ABORTED handling
# ---------------------------------------------------------------------------
def test_aborted_goto_with_real_results_is_usable() -> None:
    page = FakePage(
        html="<html><body>transition</body></html>",
        title="",
        url="https://www.douyin.com/",
        goto_error=RuntimeError(
            "Page.goto: net::ERR_ABORTED at https://www.douyin.com/search/x"
        ),
        goto_effect=(VIDEO_HTML, CLEARED_TITLE, "https://www.douyin.com/search/x?type=video", True),
    )
    backend = make_backend(page)
    messages: list[str] = []
    check = make_check(backend, on_message=messages.append)
    assert check.usable is True
    assert check.aborted_navigation is True
    assert check.status == BrowserSearchStatus.OK.value
    assert check.video_ids
    assert MESSAGE_SELF_NAVIGATION in messages


def test_aborted_goto_still_on_the_challenge_stays_verification_required() -> None:
    page = FakePage(
        html="<html><body>transition</body></html>",
        title="",
        url="https://www.douyin.com/",
        goto_error=RuntimeError("Page.goto: net::ERR_ABORTED"),
        goto_effect=(CHALLENGE_HTML, "验证中间页", "https://www.douyin.com/search/x", False),
    )
    backend = make_backend(page)
    check = make_check(backend)
    assert check.usable is False
    assert check.status == BrowserSearchStatus.VERIFICATION_REQUIRED.value
    assert check.status != BrowserSearchStatus.DOUYIN_UNREACHABLE.value
    assert backend._challenge_page is page, "still walled: keep waiting in place"


def test_aborted_goto_that_becomes_502_is_upstream_bad_gateway() -> None:
    page = FakePage(
        html="<html><body>transition</body></html>",
        title="",
        url="https://www.douyin.com/",
        goto_error=RuntimeError("Page.goto: net::ERR_ABORTED"),
        goto_effect=(GATEWAY_HTML, "验证码中间页", "https://www.douyin.com/search/x", False),
    )
    backend = make_backend(page)
    check = make_check(backend)
    assert check.usable is False
    assert check.status == BrowserSearchStatus.UPSTREAM_BAD_GATEWAY.value
    assert check.status != BrowserSearchStatus.VERIFICATION_REQUIRED.value
    assert check.status != BrowserSearchStatus.DOUYIN_UNREACHABLE.value


def test_aborted_navigation_is_never_reported_as_unreachable() -> None:
    page = FakePage(
        html="<html><body>transition</body></html>",
        title="",
        goto_error=RuntimeError("net::ERR_ABORTED"),
    )
    backend = make_backend(page)
    check = make_check(backend)
    assert check.status != BrowserSearchStatus.DOUYIN_UNREACHABLE.value


def test_a_real_network_error_is_still_unreachable() -> None:
    page = FakePage(
        html="<html><body></body></html>",
        title="",
        goto_error=RuntimeError("Page.goto: net::ERR_CONNECTION_REFUSED"),
    )
    backend = make_backend(page)
    check = make_check(backend)
    assert check.status == BrowserSearchStatus.DOUYIN_UNREACHABLE.value
    assert "navigation failed" in check.detail


# ---------------------------------------------------------------------------
# 11.7 empty title transition
# ---------------------------------------------------------------------------
def test_empty_title_during_transition_is_waited_out() -> None:
    """An empty title alone must not end the check; results still count."""

    page = FakePage(html=CLEARED_HTML, title="", has_containers=False)
    backend = make_backend(page)

    async def flip() -> None:
        await asyncio.sleep(0.05)
        page.script(html=VIDEO_HTML, title=CLEARED_TITLE, has_containers=True)

    async def flow() -> SessionCheck:
        task = asyncio.ensure_future(flip())
        check = await backend.check_session("苹果干烘干", limit=5, on_message=lambda _t: None)
        await task
        return check

    check = run(flow())
    assert check.usable is True
    assert check.video_ids, "the check waited for the transition to finish"


# ---------------------------------------------------------------------------
# 11.8 search UI without links after the bounded wait
# ---------------------------------------------------------------------------
def test_search_page_without_links_is_not_unreachable() -> None:
    # hydrated search page (search box rendered) that genuinely has no results
    page = FakePage(
        html=CLEARED_HTML, title=CLEARED_TITLE, has_containers=False, search_ui=True
    )
    backend = make_backend(page)
    started = time.perf_counter()
    check = make_check(backend)
    elapsed = time.perf_counter() - started
    assert check.status == BrowserSearchStatus.NO_RESULTS.value
    assert check.status != BrowserSearchStatus.DOUYIN_UNREACHABLE.value
    assert check.usable is False
    # bounded: the settle window is 0.2s in these tests
    assert elapsed < 5.0


def test_result_cards_without_links_report_dom_change() -> None:
    page = FakePage(html=CLEARED_HTML, title=CLEARED_TITLE, has_containers=True)
    backend = make_backend(page)
    check = make_check(backend)
    assert check.status == BrowserSearchStatus.SEARCH_DOM_CHANGED.value
    assert check.status != BrowserSearchStatus.DOUYIN_UNREACHABLE.value


# ---------------------------------------------------------------------------
# 11.9 same context/page identity
# ---------------------------------------------------------------------------
def test_post_verification_recovery_keeps_the_same_context_and_page() -> None:
    page = FakePage(html=CHALLENGE_HTML, title="验证中间页")
    context = FakeContext(page)
    backend = make_backend(page, context)

    async def flow() -> None:
        await backend.search("苹果干烘干", 5)
        first_context = backend._context
        page.script(
            html=CLEARED_HTML,
            title=CLEARED_TITLE,
            url="https://www.douyin.com/search/%E8%8B%B9%E6%9E%9C%E5%B9%B2%E7%83%98%E5%B9%B2?type=video",
        )
        pending = await backend.check_session("苹果干烘干", limit=5)
        assert pending.status == BrowserSearchStatus.SEARCH_PENDING.value
        page.script(html=VIDEO_HTML, title=CLEARED_TITLE, has_containers=True)
        usable = await backend.check_session("苹果干烘干", limit=5)
        assert usable.usable is True
        assert usable.video_ids
        assert backend._context is first_context
        assert context.new_page_calls == 1, "no relaunch, no extra page"
        await backend.close()

    run(flow())
    assert context.closed is True


def test_interactive_loop_waits_through_search_pending() -> None:
    page = FakePage(html=CHALLENGE_HTML, title="验证中间页")
    backend = make_backend(page)
    messages: list[str] = []

    async def operator_confirms_and_results_appear() -> bool:
        page.script(html=VIDEO_HTML, title=CLEARED_TITLE, has_containers=True)
        return True

    async def flow() -> SessionCheck:
        await backend.search("苹果干烘干", 5)
        # the operator cleared the challenge: normal title, no results yet
        page.script(
            html=CLEARED_HTML,
            title=CLEARED_TITLE,
            url="https://www.douyin.com/search/%E8%8B%B9%E6%9E%9C%E5%B9%B2%E7%83%98%E5%B9%B2?type=video",
        )
        return await backend.ensure_interactive_session(
            "苹果干烘干",
            limit=5,
            on_message=messages.append,
            wait_for_operator=operator_confirms_and_results_appear,
            poll_seconds=0.05,
        )

    check = run(flow())
    assert check.usable is True
    joined = "\n".join(messages)
    assert MESSAGE_VERIFICATION_CLEARED in joined, (
        "a cleared-but-hydrating page must be reported as cleared, not as a wall"
    )
    assert "仍在验证/登录页" not in joined, "the loop must not keep claiming verification"


def test_round_messages_describe_the_actual_state() -> None:
    backend = make_backend(FakePage(html=CLEARED_HTML, title=CLEARED_TITLE))
    cleared = SessionCheck(
        usable=False, status=BrowserSearchStatus.SEARCH_PENDING.value, transitional=True
    )
    assert "人工验证已解除" in backend._round_message(cleared, short=True)
    aborted = SessionCheck(
        usable=False, status=BrowserSearchStatus.SEARCH_PENDING.value, aborted_navigation=True
    )
    assert backend._round_message(aborted) == MESSAGE_SELF_NAVIGATION
    walled = SessionCheck(
        usable=False, status=BrowserSearchStatus.VERIFICATION_REQUIRED.value
    )
    assert "仍在验证/登录页" in backend._round_message(walled)
    other = SessionCheck(usable=False, status=BrowserSearchStatus.NO_RESULTS.value, detail="d")
    assert BrowserSearchStatus.NO_RESULTS.value in backend._round_message(other)


# ---------------------------------------------------------------------------
# 11.10/11.11 Ctrl+C + bounded diagnostic
# ---------------------------------------------------------------------------
def test_ctrl_c_during_a_transitional_wait_cleans_up() -> None:
    page = FakePage(html=CLEARED_HTML, title=CLEARED_TITLE)
    context = FakeContext(page)
    backend = make_backend(page, context)

    async def cancelled() -> None:
        task = asyncio.ensure_future(
            backend.wait_for_human_verification(
                "苹果干烘干",
                on_message=lambda _t: None,
                poll_seconds=0.05,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await backend.close()

    run(cancelled())
    assert page.closed is True
    assert context.closed is True


def test_non_interactive_diagnostic_stays_bounded(settings, monkeypatch) -> None:
    """The check command is unchanged: it never enters the settle/human loop."""

    import app as app_module

    page = FakePage(html=CHALLENGE_HTML, title="验证中间页")
    backend = make_backend(page, search_settle_timeout_seconds=30.0)
    monkeypatch.setattr("core.dependencies.build_browser_search", lambda *a, **k: backend)
    started = time.perf_counter()
    ok, lines = asyncio.run(app_module._douyin_browser_doctor(settings))
    elapsed = time.perf_counter() - started
    assert ok is False
    assert elapsed < 20.0
    assert "--verify-douyin-browser" in "\n".join(lines)
