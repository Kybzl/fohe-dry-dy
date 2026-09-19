"""Milestone 8.3: attach to the operator's own browser (V3.2 session model).

The real-machine finding this encodes:

* Playwright-launched Chrome (automation flags) -> Douyin renders an untrusted
  "empty shell": N empty ``<li>`` cards, no ``/video/`` links, an invisible
  ``captcha_container`` intercepting clicks.
* The *same* profile launched normally and attached over CDP -> real cards with
  real ``//www.douyin.com/video/<id>`` hrefs.

Every test here uses fakes: no real browser, no network, no CAPTCHA.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from sources.douyin_browser_search import (
    GATEWAY_PAGE_MARKERS,
    DouyinBrowserSearchBackend,
)
from sources.douyin_search import BrowserSearchStatus


def run(coro):
    return asyncio.run(coro)


#: the real, verified search page as observed on 2026-09-15 (sanitized):
#: cards are ``<li data-e2e="scroll-list"> > li`` and each holds a real anchor.
CDP_REAL_HTML = """
<html><head><title>发现更多精彩视频 - 抖音搜索</title></head><body>
<div id="douyin-navigation"><a href="//www.douyin.com/jingxuan">精选</a></div>
<ul data-e2e="scroll-list">
  <li><div class="UlbwFjuW"><a href="//www.douyin.com/video/7618927102871924111">合集</a></div></li>
  <li><div class="UlbwFjuW"><a href="//www.douyin.com/video/7619610243768618246">苹果片烘干</a></div></li>
  <li><div class="UlbwFjuW"><a href="https://www.douyin.com/video/7571407518955835017">烘干数据</a></div></li>
  <li><div class="UlbwFjuW"><a href="//www.douyin.com/user/MS4wLjABAAAA">作者</a></div></li>
  <li><div class="UlbwFjuW"><a href="https://live.douyin.com/12345">直播中</a></div></li>
</ul>
</body></html>
"""

#: the untrusted shell Playwright-launched Chrome receives: cards with no links
SHELL_HTML = """
<html><head><title>发现更多精彩视频 - 抖音搜索</title></head><body>
<ul data-e2e="scroll-list">
  <li><div class="UlbwFjuW"><div class="hwRv6t8S"></div></div></li>
  <li><div class="UlbwFjuW"><div class="hwRv6t8S"></div></div></li>
</ul>
<div id="captcha_container"></div>
</body></html>
"""


class FakeLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count


class FakeMouse:
    def __init__(self, page: "FakePage") -> None:
        self.page = page

    async def wheel(self, _x: int, _y: int) -> None:
        self.page.scrolls += 1


class FakePage:
    def __init__(self, html: str, title: str, url: str = "") -> None:
        self.html = html
        self.title_text = title
        self.url = url
        self.closed = False
        self.scrolls = 0
        self.mouse = FakeMouse(self)

    async def goto(self, url: str, **_kwargs: Any) -> Any:
        self.url = url
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
        if "scroll-list" in selector:
            return FakeLocator(1 if 'data-e2e="scroll-list"' in self.html else 0)
        return FakeLocator(0)

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


class FakeAttachedBrowser:
    """A CDP-attached browser: ``close()`` must mean "disconnect", not "kill"."""

    def __init__(self, context: FakeContext) -> None:
        self.contexts = [context]
        self.closed = False
        self.new_context_calls = 0

    async def new_context(self) -> FakeContext:
        self.new_context_calls += 1
        return self.contexts[0]

    async def close(self) -> None:
        # Playwright's connect_over_cdp close() only detaches from the browser
        self.closed = True


class FakePlaywright:
    def __init__(self, attached: FakeAttachedBrowser) -> None:
        self._attached = attached
        self.stopped = False
        self.connected_to = ""
        self.chromium = self

    async def connect_over_cdp(self, url: str) -> FakeAttachedBrowser:
        self.connected_to = url
        return self._attached

    async def start(self) -> "FakePlaywright":
        return self

    async def stop(self) -> None:
        self.stopped = True


def make_cdp_backend(page: FakePage, *, cdp_url: str = "http://127.0.0.1:9222", **kwargs: Any):
    context = FakeContext(page)
    attached = FakeAttachedBrowser(context)
    playwright = FakePlaywright(attached)

    async def connect(_url: str) -> tuple[FakePlaywright, FakeAttachedBrowser]:
        return playwright, attached

    payload: dict[str, Any] = {
        "cdp_url": cdp_url,
        "cdp_connect_factory": connect,
        "keep_page_on_challenge": True,
        "headless": False,
        "page_settle_seconds": 0.01,
        "scroll_delay_seconds": 0.0,
        "max_scrolls_per_query": 2,
        "max_results_per_query": 10,
        "upstream_retry_count": 0,
        "search_settle_timeout_seconds": 0.2,
        "search_settle_poll_seconds": 0.05,
    }
    payload.update(kwargs)
    backend = DouyinBrowserSearchBackend(**payload)
    return backend, context, attached, playwright


# ---------------------------------------------------------------------------
# session model
# ---------------------------------------------------------------------------
def test_cdp_attach_reuses_the_operator_context() -> None:
    page = FakePage(CDP_REAL_HTML, "发现更多精彩视频 - 抖音搜索")
    backend, context, attached, playwright = make_cdp_backend(page)

    async def flow() -> None:
        await backend.open()
        assert backend.using_cdp is True
        assert backend._context is context, "the operator's own context is reused"
        assert attached.new_context_calls == 0
        await backend.open()  # second call must not attach again
        assert backend.cdp_url == "http://127.0.0.1:9222"
        await backend.close()
        assert playwright.stopped is True

    run(flow())


def test_cdp_close_does_not_close_the_operator_browser() -> None:
    page = FakePage(CDP_REAL_HTML, "发现更多精彩视频 - 抖音搜索")
    backend, context, attached, _playwright = make_cdp_backend(page)

    async def flow() -> None:
        await backend.open()
        await backend.close()

    run(flow())
    assert context.closed is False, "the operator's context must survive"


def test_cdp_detach_never_closes_the_operator_browser() -> None:
    page = FakePage(CDP_REAL_HTML, "发现更多精彩视频 - 抖音搜索")
    backend, _context, attached, playwright = make_cdp_backend(page)

    async def flow() -> None:
        await backend.open()
        await backend.close()

    run(flow())
    assert attached.closed is False, "an attached browser is detached, never closed"
    assert playwright.stopped is True
    assert backend.using_cdp is False


def test_describe_mode_reports_cdp() -> None:
    backend, *_ = make_cdp_backend(FakePage(CDP_REAL_HTML, "t"))
    assert backend.describe_mode().startswith("cdp-attach")
    plain = DouyinBrowserSearchBackend(headless=False)
    assert plain.describe_mode().startswith("playwright-launch")


# ---------------------------------------------------------------------------
# extraction against the real (sanitized) card layout
# ---------------------------------------------------------------------------
def test_real_card_layout_yields_candidates() -> None:
    page = FakePage(CDP_REAL_HTML, "发现更多精彩视频 - 抖音搜索")
    backend, *_ = make_cdp_backend(page)
    outcome = run(backend.search("苹果干烘干", 10))
    assert outcome.status == BrowserSearchStatus.OK.value
    ids = [item.platform_video_id for item in outcome.candidates]
    assert ids[:3] == [
        "7618927102871924111",
        "7619610243768618246",
        "7571407518955835017",
    ]
    assert "MS4wLjABAAAA" not in " ".join(ids), "author links are ignored"
    assert len(ids) == len(set(ids)), "candidates are de-duplicated"
    for candidate in outcome.candidates:
        assert candidate.source_url == (
            f"https://www.douyin.com/video/{candidate.platform_video_id}"
        )


def test_protocol_relative_and_absolute_links_normalize() -> None:
    html = """
    <html><head><title>发现更多精彩视频 - 抖音搜索</title></head><body>
    <ul data-e2e="scroll-list">
      <li><a href="//www.douyin.com/video/7618927102871924111">a</a></li>
      <li><a href="https://www.douyin.com/video/7619610243768618246">b</a></li>
      <li><a href="/video/7571407518955835017?previous_page=search">c</a></li>
      <li><a href="https://live.douyin.com/999">直播</a></li>
      <li><a href="https://www.douyin.com/user/MS4wLjABAAAA">作者</a></li>
    </ul></body></html>
    """
    page = FakePage(html, "发现更多精彩视频 - 抖音搜索")
    backend, *_ = make_cdp_backend(page)
    outcome = run(backend.search("苹果干烘干", 10))
    ids = sorted(item.platform_video_id for item in outcome.candidates)
    assert ids == [
        "7571407518955835017",
        "7618927102871924111",
        "7619610243768618246",
    ], "relative/absolute/protocol-relative forms all count; live/user links do not"


def test_untrusted_shell_reports_dom_changed_not_verification() -> None:
    page = FakePage(SHELL_HTML, "发现更多精彩视频 - 抖音搜索")
    backend, *_ = make_cdp_backend(page)
    outcome = run(backend.search("苹果干烘干", 5))
    assert outcome.status == BrowserSearchStatus.SEARCH_DOM_CHANGED.value
    assert outcome.status != BrowserSearchStatus.VERIFICATION_REQUIRED.value
    assert outcome.candidates == []


def test_shell_page_is_not_reported_as_a_human_wall() -> None:
    """The shell carries a captcha container; that must not become a CAPTCHA."""

    page = FakePage(SHELL_HTML, "发现更多精彩视频 - 抖音搜索")
    backend, *_ = make_cdp_backend(page)
    messages: list[str] = []
    check = run(
        backend.check_session("苹果干烘干", limit=5, on_message=messages.append)
    )
    assert check.status == BrowserSearchStatus.SEARCH_DOM_CHANGED.value
    assert check.status not in (
        BrowserSearchStatus.VERIFICATION_REQUIRED.value,
        BrowserSearchStatus.LOGIN_REQUIRED.value,
    )
    assert not any("仍在验证/登录页" in text for text in messages)


def test_cdp_session_usable_requires_real_candidates() -> None:
    page = FakePage(CDP_REAL_HTML, "发现更多精彩视频 - 抖音搜索")
    backend, *_ = make_cdp_backend(page)
    check = run(backend.check_session("苹果干烘干", limit=5))
    assert check.usable is True
    assert check.status == BrowserSearchStatus.OK.value
    assert check.video_ids
    assert check.page_title.startswith("发现更多精彩视频")


def test_gateway_page_still_classified_on_cdp() -> None:
    html = "<html><body><h1>502 Bad Gateway</h1>kngx</body></html>"
    page = FakePage(html, "")
    backend, *_ = make_cdp_backend(page)
    outcome = run(backend.search("苹果干烘干", 5))
    assert outcome.status == BrowserSearchStatus.UPSTREAM_BAD_GATEWAY.value
    assert any(marker in html.lower() for marker in GATEWAY_PAGE_MARKERS)


# ---------------------------------------------------------------------------
# configuration / CLI surface
# ---------------------------------------------------------------------------
def test_cdp_auto_detect_uses_the_configured_port(settings, monkeypatch) -> None:
    from core import dependencies

    calls: dict[str, Any] = {}

    def fake_probe(port: int, **_kwargs: Any) -> str:
        calls["port"] = port
        return "http://127.0.0.1:9222"

    monkeypatch.setattr(dependencies, "cdp_endpoint_available", fake_probe)
    settings.sources.douyin.browser_search.cdp_url = ""
    settings.sources.douyin.browser_search.cdp_port = 9222
    assert dependencies.resolve_cdp_url(settings) == "http://127.0.0.1:9222"
    assert calls["port"] == 9222


def test_cdp_off_is_respected(settings, monkeypatch) -> None:
    from core import dependencies

    settings.sources.douyin.browser_search.cdp_url = "off"
    monkeypatch.setattr(
        dependencies, "cdp_endpoint_available", lambda *a, **k: "http://127.0.0.1:9222"
    )
    assert dependencies.resolve_cdp_url(settings) == ""


def test_explicit_cdp_url_wins(settings, monkeypatch) -> None:
    from core import dependencies

    settings.sources.douyin.browser_search.cdp_url = "http://127.0.0.1:9333"
    monkeypatch.setattr(
        dependencies, "cdp_endpoint_available", lambda *a, **k: "http://127.0.0.1:9222"
    )
    assert dependencies.resolve_cdp_url(settings) == "http://127.0.0.1:9333"


def test_build_browser_search_passes_cdp_url(settings, monkeypatch) -> None:
    from core import dependencies

    monkeypatch.setattr(
        dependencies, "cdp_endpoint_available", lambda *a, **k: "http://127.0.0.1:9222"
    )
    settings.sources.douyin.browser_search.cdp_url = ""
    backend = dependencies.build_browser_search(settings)
    assert backend.cdp_url == "http://127.0.0.1:9222"
    assert backend.describe_mode().startswith("cdp-attach")


def test_verify_cli_reports_dom_mismatch_instead_of_captcha(settings, monkeypatch, capsys) -> None:
    import app as app_module

    page = FakePage(SHELL_HTML, "发现更多精彩视频 - 抖音搜索")
    backend, *_ = make_cdp_backend(page)
    monkeypatch.setattr("core.dependencies.build_browser_search", lambda *a, **k: backend)

    usable, lines = run(app_module._verify_douyin_browser(settings, query="苹果干烘干"))
    output = "\n".join(lines)
    assert usable is False
    assert "抖音验证已通过，但当前搜索结果 DOM 暂未识别" in output
    assert "仍被验证拦截" not in output


def test_open_browser_command_reports_existing_endpoint(settings, monkeypatch, capsys) -> None:
    import app as app_module
    from core import dependencies

    monkeypatch.setattr(
        dependencies, "cdp_endpoint_available", lambda *a, **k: "http://127.0.0.1:9222"
    )
    assert app_module.run_open_douyin_browser(settings) == 0
    output = capsys.readouterr().out
    assert "已经有一个可接入的浏览器" in output


def test_verify_cli_labels_a_saturated_browser_as_unavailable(
    settings, monkeypatch, capsys
) -> None:
    """Any non-CAPTCHA failure must not be reported as a verification wall."""

    import app as app_module
    from sources.douyin_browser_search import SessionCheck

    page = FakePage(CDP_REAL_HTML, "发现更多精彩视频 - 抖音搜索")
    backend, *_ = make_cdp_backend(page)

    async def unavailable(*_args: Any, **_kwargs: Any) -> SessionCheck:
        return SessionCheck(
            usable=False,
            status="browser_unavailable",
            detail="BrowserType.connect_over_cdp: Timeout 180000ms exceeded.",
        )

    backend.ensure_interactive_session = unavailable  # type: ignore[assignment]
    monkeypatch.setattr("core.dependencies.build_browser_search", lambda *a, **k: backend)
    usable, lines = run(app_module._verify_douyin_browser(settings, query="芒果烘干"))
    assert usable is False
    output = "\n".join(lines)
    assert "browser_unavailable" in output
    assert "会话不可用" in output
    assert "仍被验证拦截" not in output


def test_verify_cli_keeps_the_captcha_label_for_real_walls(
    settings, monkeypatch, capsys
) -> None:
    import app as app_module
    from sources.douyin_browser_search import SessionCheck

    page = FakePage(CDP_REAL_HTML, "发现更多精彩视频 - 抖音搜索")
    backend, *_ = make_cdp_backend(page)

    async def walled(*_args: Any, **_kwargs: Any) -> SessionCheck:
        return SessionCheck(
            usable=False,
            status="verification_required",
            detail="Douyin requires a manual slider/verification challenge",
        )

    backend.ensure_interactive_session = walled  # type: ignore[assignment]
    monkeypatch.setattr("core.dependencies.build_browser_search", lambda *a, **k: backend)
    usable, lines = run(app_module._verify_douyin_browser(settings, query="芒果烘干"))
    assert usable is False
    assert any("仍处于验证/登录页" in line for line in lines) or any(
        "verification_required" in line for line in lines
    )


def test_open_browser_command_launches_a_plain_browser(settings, monkeypatch) -> None:
    """No automation flags, and the operator's browser is left running."""

    import subprocess as subprocess_module

    import app as app_module
    from core import dependencies

    launched: dict[str, Any] = {}

    class FakeProcess:
        pid = 4242

    def fake_popen(args, **_kwargs):
        launched["args"] = list(args)
        return FakeProcess()

    monkeypatch.setattr(dependencies, "cdp_endpoint_available", lambda *a, **k: "")
    monkeypatch.setattr(subprocess_module, "Popen", fake_popen)
    settings.sources.douyin.browser_search.cdp_port = 9222
    code = app_module.run_open_douyin_browser(settings)
    assert code == 0
    args = launched["args"]
    assert "--remote-debugging-port=9222" in args
    assert any(str(a).startswith("--user-data-dir=") for a in args)
    for forbidden in ("--enable-automation", "--headless", "--disable-blink-features"):
        assert forbidden not in " ".join(map(str, args))
