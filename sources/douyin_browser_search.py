"""Playwright based Douyin discovery (Milestone 3.5).

Responsibility split (section 2): the browser **only discovers public video
URLs**.  Downloading, metadata and media streams still go through the existing
dtk backend and ``DouyinSource``.

Explicitly *not* implemented here:

* no CAPTCHA / slider solving and no SMS handling
* no stealth or anti-bot evasion (no UA spoofing, no fingerprint patching)
* no credential entry, no cookie export, no private content

When Douyin asks for a slider challenge or a login we report
``verification_required`` / ``login_required`` and never pass the challenge
ourselves.

Milestone 8.1: an *interactive* session gate keeps the **same** context and
page alive while the operator completes the challenge normally, then re-checks
that very page for real ``/video/`` results.  This replaced the old
init → close → re-check loop, which relied on the persistent profile carrying
the verification across a process restart (it does not, reliably).
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence
from urllib.parse import quote, unquote, urlparse

from sources.douyin_search import (
    BrowserSearchStatus,
    DiscoveredDouyinVideo,
    DiscoveryBackend,
    DouyinSearchBackend,
    SearchOutcome,
    TRANSIENT_UPSTREAM_STATUSES,
)

LOGGER = logging.getLogger(__name__)

#: The general (综合) search tab currently renders an SSR shell without any
#: result payload for an automated client.  The **video** tab renders real
#: ``/video/`` links, so it is the primary entry point; the plain URL is kept as
#: a bounded fallback in case the platform changes its mind again.
SEARCH_URL_TEMPLATE = "https://www.douyin.com/search/{query}?type=video"
SEARCH_URL_TEMPLATE_PLAIN = "https://www.douyin.com/search/{query}"
VIDEO_URL_TEMPLATE = "https://www.douyin.com/video/{video_id}"

#: anchors that carry a public video/note permalink
VIDEO_HREF_SELECTORS: tuple[str, ...] = (
    'a[href*="/video/"]',
    'a[href*="/note/"]',
)
#: containers that only exist once the result list has rendered
RESULT_CONTAINER_SELECTORS: tuple[str, ...] = (
    '[data-e2e="scroll-list"]',
    '[data-e2e="search-result-list"]',
    'div[class*="search-result"]',
)
#: the search input on the search page (used to re-submit when needed)
SEARCH_INPUT_SELECTORS: tuple[str, ...] = (
    '[data-e2e="searchbar-input"]',
    'input[type="search"]',
    'input[placeholder*="搜索"]',
)
#: shells that only exist once the search SPA rendered (even with 0 results)
SEARCH_SHELL_SELECTORS: tuple[str, ...] = (
    '[data-e2e="search-container"]',
    'div[class*="search-result"]',
    'div[class*="search-shell"]',
)

#: page markers that mean "the operator must act" (never solved by code)
INTERMEDIATE_PAGE_TITLES: tuple[str, ...] = ("验证中间页", "验证")
#: upstream/edge gateway failure pages (Douyin's kngx edge answers these)
GATEWAY_PAGE_MARKERS: tuple[str, ...] = (
    "502 bad gateway",
    "bad gateway",
    "503 service unavailable",
    "504 gateway time-out",
    "504 gateway timeout",
    "gateway time-out",
    "gateway timeout",
    "kngx",
)
VERIFICATION_MARKERS: tuple[str, ...] = (
    "verifycenter",
    "sr-captcha",
    "captcha",
    "滑动验证",
    "滑块验证",
)
#: phrases that only appear when a **real** challenge is rendered.  Douyin
#: loads its anti-bot SDK on every page, so a bare ``captcha`` substring in the
#: HTML is *not* evidence of a challenge (it produced a false
#: ``verification_required`` on a normal search page); the rendered text is.
VERIFICATION_TEXT_MARKERS: tuple[str, ...] = (
    "请完成安全验证",
    "请完成验证",
    "滑动验证",
    "滑块验证",
    "拖动滑块",
    "安全验证",
    "验证码",
)
#: tags whose *content* must never count as rendered page text
_NON_VISIBLE_TAGS = ("script", "style", "noscript", "template")
LOGIN_MARKERS: tuple[str, ...] = (
    "login-full-panel",
    "登录后",
    "请先登录",
    "登录抖音",
    "扫码登录",
)

_VIDEO_ID_IN_URL = re.compile(r"/(?:video|note)/(\d{6,})")
_VIDEO_ID_IN_HTML = re.compile(r"/(?:video|note)/(\d{6,})")
_AWEME_ID_IN_JSON = re.compile(r'"(?:aweme_id|awemeId|aweme_id_str)"\s*:\s*"?(\d{10,})"?')

#: walls that stop browser discovery for the remainder of a task
STICKY_WALL_STATUSES: frozenset[str] = frozenset(
    {
        BrowserSearchStatus.LOGIN_REQUIRED.value,
        BrowserSearchStatus.VERIFICATION_REQUIRED.value,
        BrowserSearchStatus.DOUYIN_UNREACHABLE.value,
        BrowserSearchStatus.BROWSER_UNAVAILABLE.value,
        BrowserSearchStatus.UPSTREAM_BAD_GATEWAY.value,
        BrowserSearchStatus.UPSTREAM_HTTP_ERROR.value,
    }
)

#: statuses that are transient upstream failures (bounded retry applies)
UPSTREAM_STATUSES: frozenset[str] = frozenset(
    {
        BrowserSearchStatus.UPSTREAM_BAD_GATEWAY.value,
        BrowserSearchStatus.UPSTREAM_HTTP_ERROR.value,
    }
)

#: walls a human can clear in the same browser window (section 1 of M8.1)
HUMAN_WALL_STATUSES: frozenset[str] = frozenset(
    {
        BrowserSearchStatus.VERIFICATION_REQUIRED.value,
        BrowserSearchStatus.LOGIN_REQUIRED.value,
    }
)

#: states an interactive gate keeps waiting on: a human wall, or a page that
#: already cleared the challenge but has not rendered results yet (M8.2)
WAITING_STATUSES: frozenset[str] = frozenset(
    {
        *HUMAN_WALL_STATUSES,
        BrowserSearchStatus.SEARCH_PENDING.value,
    }
)

#: Playwright navigation errors raised when the page navigates itself while a
#: goto is in flight.  This is a *recoverable* race after human verification,
#: not evidence that Douyin is unreachable (M8.2 section 4).
ABORTED_NAVIGATION_MARKERS: tuple[str, ...] = (
    "err_aborted",
    "net::err_aborted",
)

#: normal Douyin search titles that must never be read as a verification wall
SEARCH_PAGE_TITLE_MARKERS: tuple[str, ...] = (
    "抖音搜索",
    "精彩视频",
    "- 抖音",
)

INTERACTIVE_NOTICE = (
    "检测到抖音人工验证。\n"
    "请在当前已经打开的项目浏览器窗口中正常完成滑块/扫码/验证。\n"
    "程序不会破解验证码。\n"
    "\n"
    "完成后回到终端按 Enter 继续检查。\n"
    "（也可以直接等在浏览器里完成，程序会自动轮询检查；Ctrl+C 可取消。）"
)

MESSAGE_VERIFICATION_CLEARED = "人工验证已解除，正在等待抖音搜索结果加载……"
MESSAGE_SEARCH_HYDRATING = "搜索页已加载，等待视频结果渲染……"
MESSAGE_SELF_NAVIGATION = "页面正在自行跳转，重新检查当前页面……"


@dataclass
class BrowserSearchResult:
    """Raw outcome of one browser search (before adaptation)."""

    videos: list[DiscoveredDouyinVideo] = field(default_factory=list)
    status: str = BrowserSearchStatus.OK.value
    detail: str = ""
    scrolls: int = 0
    page_url: str = ""
    diagnostics: "BrowserDiagnostics" = field(default_factory=lambda: BrowserDiagnostics())


@dataclass
class SessionCheck:
    """Result of checking one live search session (Milestone 8.1 sections 2/9).

    ``usable`` is only ever true with *real discovery evidence*: at least one
    ``/video/`` link rendered in the same context.  A page that merely stopped
    showing the challenge title is not enough.
    """

    usable: bool
    status: str
    detail: str = ""
    video_ids: list[str] = field(default_factory=list)
    page_url: str = ""
    page_title: str = ""
    http_status: int | None = None
    rounds: int = 0
    cancelled: bool = False
    waited_seconds: float = 0.0
    #: Milestone 8.2: the challenge cleared but results are still hydrating
    transitional: bool = False
    #: a page.goto() was aborted because the page navigated itself
    aborted_navigation: bool = False
    #: whether this check had to navigate (False = the live page was reused)
    reused_current_page: bool = False

    @property
    def video_count(self) -> int:
        return len(self.video_ids)

    def summary_lines(self) -> list[str]:
        state = "session_usable" if self.usable else self.status or "unknown"
        lines = [
            f"[{'ok' if self.usable else 'warn'}] session state: {state}",
            f"[info] final_url: {self.page_url}",
            f"[info] page_title: {self.page_title}",
        ]
        if self.http_status is not None:
            lines.append(f"[info] http_status: {self.http_status}")
        if self.reused_current_page:
            lines.append("[info] 复用了当前已打开的搜索页（没有重新导航）")
        if self.aborted_navigation:
            lines.append(
                "[info] 检测到可恢复的 net::ERR_ABORTED（页面自行跳转），"
                "已按当前页面重新判定"
            )
        if self.transitional:
            lines.append("[info] 人工验证已解除，搜索页仍在渲染结果（search_pending）")
        if self.usable:
            lines.append(
                f"[ok] 真实搜索结果: {self.video_count} 个 /video/ 链接"
                f"（例如 https://www.douyin.com/video/{self.video_ids[0]}）"
            )
        elif self.detail:
            lines.append(f"[warn] {self.detail}")
        if self.rounds:
            lines.append(
                f"[info] 人工检查轮次: {self.rounds}"
                + (f"，等待 {self.waited_seconds:.0f}s" if self.waited_seconds else "")
            )
        return lines


@dataclass
class BrowserDiagnostics:
    """Non-sensitive diagnostic fields for one browser attempt.

    Deliberately excludes cookies, headers and anything credential-like.
    """

    requested_url: str = ""
    final_url: str = ""
    http_status: int | None = None
    page_title: str = ""
    browser_status: str = ""
    attempts: int = 0
    #: which browser actually ran (section 8): never a cookie or profile dump
    browser_channel: str = ""
    browser_executable: str = ""
    profile_dir: str = ""
    headless: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "http_status": self.http_status,
            "page_title": self.page_title,
            "browser_status": self.browser_status,
            "attempts": self.attempts,
            "browser_channel": self.browser_channel,
            "browser_executable": self.browser_executable,
            "profile_dir": self.profile_dir,
            "headless": self.headless,
        }

    def summary_lines(self) -> list[str]:
        lines = [
            f"[info] requested_url: {self.requested_url}",
            f"[info] final_url: {self.final_url or '(none)'}",
            f"[info] http_status: {self.http_status if self.http_status is not None else '(unknown)'}",
            f"[info] page_title: {self.page_title or '(empty)'}",
            f"[info] browser_status: {self.browser_status or '(unknown)'}",
        ]
        if self.browser_channel or self.browser_executable:
            lines.append(
                f"[info] browser: {self.browser_channel or 'chromium'}"
                + (f" ({self.browser_executable})" if self.browser_executable else "")
            )
        if self.profile_dir:
            lines.append(f"[info] profile_dir: {self.profile_dir}")
        if self.attempts > 1:
            lines.append(f"[info] attempts: {self.attempts}")
        return lines


def is_video_url(url: str) -> bool:
    """True only for public Douyin video/note permalinks."""

    if not url:
        return False
    text = url.strip()
    if text.startswith("//"):
        text = f"https:{text}"
    parsed = urlparse(text)
    host = (parsed.netloc or "").lower()
    if not host.endswith("douyin.com"):
        return False
    return bool(_VIDEO_ID_IN_URL.search(parsed.path))


def normalize_video_url(url: str) -> str:
    """``https://www.douyin.com/video/<id>`` with tracking parameters removed."""

    text = url.strip()
    if text.startswith("//"):
        text = f"https:{text}"
    parsed = urlparse(text)
    match = _VIDEO_ID_IN_URL.search(parsed.path)
    if not match:
        return text.split("?", 1)[0]
    return VIDEO_URL_TEMPLATE.format(video_id=match.group(1))


def extract_video_id(url: str) -> str | None:
    text = url.strip()
    if text.startswith("//"):
        text = f"https:{text}"
    match = _VIDEO_ID_IN_URL.search(urlparse(text).path)
    return match.group(1) if match else None


def extract_video_ids_from_html(html: str) -> list[str]:
    """Pull aweme ids out of a rendered page (links first, then JSON blobs)."""

    ids: list[str] = []
    for pattern in (_VIDEO_ID_IN_HTML, _AWEME_ID_IN_JSON):
        for match in pattern.finditer(html or ""):
            value = match.group(1)
            if value not in ids:
                ids.append(value)
    return ids


def extract_pairs_from_html(html: str) -> list[tuple[str, str | None]]:
    """``(url, visible_title)`` pairs, best effort, for logging/debugging."""

    pairs: list[tuple[str, str | None]] = []
    for match in re.finditer(r'href="([^"]*/(?:video|note)/\d{6,}[^"]*)"', html or ""):
        url = normalize_video_url(unquote(match.group(1)))
        pairs.append((url, None))
    return pairs


def dedupe_discovered(
    items: Iterable[DiscoveredDouyinVideo],
    *,
    limit: int | None = None,
) -> list[DiscoveredDouyinVideo]:
    """Deduplicate by video id (and normalized URL), keeping insertion order."""

    seen: set[str] = set()
    ordered: list[DiscoveredDouyinVideo] = []
    for item in items:
        key = item.platform_video_id or normalize_video_url(item.source_url)
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(item)
        if limit is not None and len(ordered) >= limit:
            break
    return ordered


def build_discovered(
    urls: Sequence[str],
    *,
    query: str,
    titles: Sequence[str | None] | None = None,
    authors: Sequence[str | None] | None = None,
    backend: str = DiscoveryBackend.BROWSER.value,
) -> list[DiscoveredDouyinVideo]:
    """Turn raw URLs (+ optional visible text) into typed discovered videos."""

    items: list[DiscoveredDouyinVideo] = []
    for index, url in enumerate(urls):
        video_id = extract_video_id(url)
        if not video_id:
            continue
        title = titles[index] if titles and index < len(titles) else None
        author = authors[index] if authors and index < len(authors) else None
        items.append(
            DiscoveredDouyinVideo(
                platform_video_id=video_id,
                source_url=normalize_video_url(url),
                search_query=query,
                visible_title=(title or None),
                visible_author=(author or None),
                discovery_backend=backend,
            )
        )
    return dedupe_discovered(items)


__all__ = [
    "BrowserSearchResult",
    "DouyinBrowserSearchBackend",
    "build_discovered",
    "dedupe_discovered",
    "extract_pairs_from_html",
    "extract_video_id",
    "extract_video_ids_from_html",
    "is_video_url",
    "normalize_video_url",
]


class DouyinBrowserSearchBackend(DouyinSearchBackend):
    """Playwright discovery over public Douyin search pages."""

    name = DiscoveryBackend.BROWSER.value

    def __init__(
        self,
        *,
        profile_dir: Path | str = "browser_data/douyin",
        headless: bool = False,
        max_scrolls_per_query: int = 8,
        scroll_delay_seconds: float = 1.0,
        max_results_per_query: int = 50,
        navigation_timeout_seconds: float = 30.0,
        browser_channel: str | None = None,
        browser_executable_path: str | None = None,
        locale: str = "zh-CN",
        viewport_width: int = 1440,
        viewport_height: int = 900,
        slow_mo_ms: int = 0,
        page_settle_seconds: float = 6.0,
        keep_open_on_challenge: bool = True,
        keep_page_on_challenge: bool = False,
        auto_resume_after_verification: bool = False,
        challenge_wait_timeout_seconds: float = 300.0,
        challenge_poll_seconds: float = 4.0,
        cdp_url: str = "",
        upstream_retry_count: int = 2,
        upstream_retry_backoff_seconds: float = 2.0,
        search_settle_timeout_seconds: float = 20.0,
        search_settle_poll_seconds: float = 1.0,
        empty_result_limit: int = 3,
        playwright_factory: Any = None,
        context_factory: Any = None,
        cdp_connect_factory: Any = None,
    ) -> None:
        # ``client`` is unused here: the browser replaces the HTTP search call.
        super().__init__(client=None)  # type: ignore[arg-type]
        self.profile_dir = Path(profile_dir)
        self.headless = bool(headless)
        self.max_scrolls_per_query = max(0, int(max_scrolls_per_query))
        self.scroll_delay_seconds = max(0.0, float(scroll_delay_seconds))
        self.max_results_per_query = max(1, int(max_results_per_query))
        self.navigation_timeout_seconds = max(5.0, float(navigation_timeout_seconds))
        self.browser_channel = browser_channel or None
        self.browser_executable_path = browser_executable_path or None
        self.locale = locale or "zh-CN"
        self.viewport_width = max(320, int(viewport_width))
        self.viewport_height = max(240, int(viewport_height))
        self.slow_mo_ms = max(0, int(slow_mo_ms))
        self.page_settle_seconds = max(1.0, float(page_settle_seconds))
        self.keep_open_on_challenge = keep_open_on_challenge
        #: bounded retries for transient upstream/gateway failures (502/503/504)
        self.upstream_retry_count = max(0, int(upstream_retry_count))
        self.upstream_retry_backoff_seconds = max(0.0, float(upstream_retry_backoff_seconds))
        #: Milestone 8.2: bounded page settling after the challenge clears
        self.search_settle_timeout_seconds = max(0.5, float(search_settle_timeout_seconds))
        self.search_settle_poll_seconds = max(0.05, float(search_settle_poll_seconds))
        #: Milestone 8.1: keep the challenged page alive inside the same context
        #: so a human can clear it there (used by the interactive gate)
        self.keep_page_on_challenge = bool(keep_page_on_challenge)
        self.auto_resume_after_verification = bool(auto_resume_after_verification)
        self.challenge_wait_timeout_seconds = max(
            0.0, float(challenge_wait_timeout_seconds)
        )
        self.challenge_poll_seconds = max(0.2, float(challenge_poll_seconds))
        #: Milestone 8.3 (V3.2 session model): attach to an operator-launched
        #: Chrome/Edge over CDP instead of launching our own browser.  Douyin
        #: serves an untrusted empty shell to automation-flagged browsers, while
        #: a normally launched browser (same profile) returns real results.
        self.cdp_url = (cdp_url or "").strip()
        #: after this many consecutive *successful* searches that produced no
        #: results, stop driving the browser for the remaining keywords.  This
        #: is a yield optimisation, **not** a blocked state: the task still
        #: reports "搜索正常执行，但 0 个结果".
        self.empty_result_limit = max(0, int(empty_result_limit))
        self._consecutive_empty = 0
        self.last_status: str = BrowserSearchStatus.OK.value
        self.last_detail: str = ""
        self.last_diagnostics = BrowserDiagnostics()
        self._playwright_factory = playwright_factory
        #: test seam: build a fake persistent context instead of a real browser
        self._context_factory = context_factory
        self._cdp_connect_factory = cdp_connect_factory
        self._playwright: Any = None
        self._context: Any = None
        #: set when this backend attached to an existing browser (CDP)
        self._attached: Any = None
        self.using_cdp = False
        #: Milestone 8.3: an injected/shared session (interactive gate + plan
        #: runner in one process) must not be torn down by a single task
        self.shared = False
        #: the live page that is currently showing a human wall (if kept)
        self._challenge_page: Any = None
        #: whatever page is currently open (closed by ``close()`` on any exit,
        #: including cancellation while a check is still in flight)
        self._live_page: Any = None
        self._operator_thread: Any = None
        self._operator_event: Any = None
        self._stdin_exhausted = False
        #: last non-empty title seen (a background tab can briefly report "")
        self.last_page_title = ""
        #: set when Douyin handed us a wall: stop hammering it for this task
        self._blocked_status: str = ""
        self._blocked_detail: str = ""

    # -- availability ------------------------------------------------------
    @staticmethod
    def playwright_available() -> tuple[bool, str]:
        """Whether the Playwright package (and its browsers) can be used."""

        try:
            import playwright  # noqa: F401
            from playwright.async_api import async_playwright  # noqa: F401
        except Exception as exc:  # pragma: no cover - depends on the environment
            return False, f"playwright package missing: {exc}"
        return True, "playwright available"

    async def probe(self) -> tuple[bool, str]:
        available, note = self.playwright_available()
        if not available:
            return False, note
        if self._blocked_status:
            return False, f"{self._blocked_status} ({self._blocked_detail})"
        return True, (
            f"browser search enabled (channel={self.browser_channel or 'chromium'}, "
            f"headless={self.headless}, profile={self.profile_dir})"
        )

    def _base_diagnostics(self, *, requested_url: str = "") -> BrowserDiagnostics:
        """Diagnostics pre-filled with which browser actually runs (section 7/8)."""

        return BrowserDiagnostics(
            requested_url=requested_url,
            browser_channel=self.browser_channel or "chromium",
            browser_executable=self.browser_executable_path or "",
            profile_dir=str(self.profile_dir),
            headless=self.headless,
        )

    def unavailable_reason(self) -> str:
        """Sticky blocked state for this task (empty when the browser is usable)."""

        return self._blocked_status

    # -- lifecycle ---------------------------------------------------------
    async def open(self) -> None:
        """Start the persistent browser context (reused across queries)."""

        if self._context is not None:
            return
        available, note = self.playwright_available()
        if not available:
            raise RuntimeError(note)
        if self.cdp_url:
            await self._open_via_cdp()
            return
        if self._context_factory is not None:
            self._context = await self._context_factory()
            try:
                self._context.set_default_navigation_timeout(
                    self.navigation_timeout_seconds * 1000
                )
            except Exception:  # pragma: no cover - fake contexts may not support it
                pass
            return
        from playwright.async_api import async_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
            "locale": self.locale,
            "viewport": {"width": self.viewport_width, "height": self.viewport_height},
            "slow_mo": self.slow_mo_ms,
        }
        if self.browser_executable_path:
            # an explicit executable and a channel are mutually exclusive
            launch_kwargs["executable_path"] = self.browser_executable_path
        elif self.browser_channel:
            launch_kwargs["channel"] = self.browser_channel
        self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        self._context.set_default_navigation_timeout(self.navigation_timeout_seconds * 1000)
        LOGGER.info(
            "douyin browser search: channel=%s executable=%s headless=%s persistent profile %s",
            self.browser_channel or "chromium",
            self.browser_executable_path or "(playwright managed)",
            self.headless,
            self.profile_dir,
        )

    async def _open_via_cdp(self) -> None:
        """Attach to the operator's own browser (Milestone 8.3 / V3.2 model).

        The operator starts Chrome/Edge with ``--remote-debugging-port`` and
        logs in / passes verification there; we attach to that already-trusted
        browser and read the public rendered page.  We never launch it with
        automation flags, never spoof anything, and never close the operator's
        browser when we are done.
        """

        if self._cdp_connect_factory is not None:
            self._playwright, self._attached = await self._cdp_connect_factory(self.cdp_url)
        else:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._attached = await self._playwright.chromium.connect_over_cdp(self.cdp_url)
        contexts = list(getattr(self._attached, "contexts", []) or [])
        self._context = contexts[0] if contexts else await self._attached.new_context()
        try:
            self._context.set_default_navigation_timeout(
                self.navigation_timeout_seconds * 1000
            )
        except Exception:  # pragma: no cover - fake contexts may not support it
            pass
        self.using_cdp = True
        LOGGER.info(
            "douyin browser search: attached over CDP to %s (operator browser, "
            "%s existing context(s)); profile untouched",
            self.cdp_url,
            len(contexts),
        )

    async def close(self) -> None:
        pages = [self._challenge_page, self._live_page]
        self._challenge_page = None
        self._live_page = None
        seen: list[int] = []
        for page in pages:
            if page is None or id(page) in seen:
                continue
            seen.append(id(page))
            try:
                await page.close()
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.debug("closing a browser page failed: %s", exc)
        closers: list[Any] = []
        if not self.using_cdp:
            # an attached browser belongs to the operator: never close it
            closers.append(getattr(self._context, "close", None))
            closers.append(getattr(self._attached, "close", None))
        else:
            # CDP: ``playwright.stop()`` only drops the connection we opened;
            # the operator's window (and its session) stays exactly as it is
            LOGGER.info("detaching from the operator browser (it stays open)")
        closers.append(getattr(self._playwright, "stop", None))
        for closer in closers:
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.debug("closing browser resource failed: %s", exc)
        self._context = None
        self._playwright = None
        self._attached = None
        self.using_cdp = False

    def describe_mode(self) -> str:
        """Which browser model this backend uses (section 12 diagnostics)."""

        if self.cdp_url:
            return f"cdp-attach ({self.cdp_url})"
        return f"playwright-launch (channel={self.browser_channel or 'chromium'})"

    async def aclose(self) -> None:
        await self.close()

    async def __aenter__(self) -> "DouyinBrowserSearchBackend":
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # -- search ------------------------------------------------------------
    async def search(self, query: str, limit: int) -> SearchOutcome:
        """Discover public videos for ``query`` with bounded scrolling."""

        if self._blocked_status:
            outcome = self._outcome(
                [], self._blocked_status, self._blocked_detail, blocked=True
            )
            outcome.diagnostics = self.last_diagnostics.as_dict()
            return outcome
        if self.empty_result_limit and self._consecutive_empty >= self.empty_result_limit:
            outcome = self._outcome(
                [],
                BrowserSearchStatus.NO_RESULTS.value,
                f"browser search already returned no results for "
                f"{self._consecutive_empty} consecutive queries in this task; "
                "skipping the remaining keywords",
            )
            outcome.diagnostics = self.last_diagnostics.as_dict()
            return outcome
        effective_limit = min(limit, self.max_results_per_query)
        try:
            await self.open()
        except Exception as exc:
            return self._outcome([], BrowserSearchStatus.BROWSER_UNAVAILABLE, str(exc))

        try:
            result = await self._search_once(query, effective_limit)
        except Exception as exc:
            LOGGER.warning("browser search crashed: %s", exc)
            await self._recover()
            return self._outcome([], BrowserSearchStatus.BROWSER_CRASHED, str(exc)[:200])

        if (
            result.status in HUMAN_WALL_STATUSES
            and self.auto_resume_after_verification
            and self.keep_page_on_challenge
            and self.challenge_wait_timeout_seconds > 0
        ):
            rounds = max(
                1,
                int(
                    self.challenge_wait_timeout_seconds
                    / self.challenge_poll_seconds
                ),
            )
            LOGGER.warning(
                "browser discovery paused at %s; polling for up to %.0fs and "
                "will retry the same query after verification",
                result.status,
                self.challenge_wait_timeout_seconds,
            )
            check = await self.wait_for_human_verification(
                query,
                limit=effective_limit,
                poll_seconds=self.challenge_poll_seconds,
                max_rounds=rounds,
            )
            if check.usable:
                self.reset_block()
                result = await self._search_once(query, effective_limit)
            else:
                result.detail = (
                    f"{result.detail}; automatic resume timed out after "
                    f"{check.waited_seconds:.1f}s"
                )

        self.last_status = result.status
        self.last_detail = result.detail
        self.last_diagnostics = result.diagnostics
        if (
            result.status
            in (BrowserSearchStatus.NO_RESULTS.value, BrowserSearchStatus.SEARCH_DOM_CHANGED.value)
            and not result.videos
        ):
            self._consecutive_empty += 1
        else:
            self._consecutive_empty = 0
        if result.status in STICKY_WALL_STATUSES:
            self._blocked_status = result.status
            self._blocked_detail = result.detail
            LOGGER.warning(
                "browser discovery paused for this task: %s (%s)",
                result.status,
                result.detail,
            )
        outcome = self._outcome(result.videos, result.status, result.detail, scrolls=result.scrolls)
        outcome.diagnostics = result.diagnostics.as_dict()
        return outcome

    def reset_block(self) -> None:
        """Allow another browser attempt (e.g. after manual verification)."""

        self._blocked_status = ""
        self._blocked_detail = ""
        self._consecutive_empty = 0

    # -- interactive session gate (Milestone 8.1 sections 1/2/9/10) --------
    async def check_session(
        self,
        query: str,
        *,
        limit: int | None = None,
        url_template: str = SEARCH_URL_TEMPLATE,
        settle_timeout: float | None = None,
        on_message: Callable[[str], None] | None = None,
    ) -> SessionCheck:
        """Navigate the live session and require real search evidence.

        The page that is currently challenged (if any) is reused, so a human
        verification performed in that window is never thrown away by a
        relaunch.  ``usable`` requires at least one rendered ``/video/`` link;
        a page that merely stopped showing the challenge is *not* enough.

        Milestone 8.2: the **current** page is inspected first (challenge? login?
        gateway? results already there?) and ``page.goto`` is only issued when
        the live page is not already the requested search.  An ``ERR_ABORTED``
        raised by Douyin's own post-verification transition is recovered by
        classifying the page we actually landed on.
        """

        limit = int(limit or min(5, self.max_results_per_query))
        emit = on_message or (lambda text: None)
        try:
            await self.open()
        except Exception as exc:
            return SessionCheck(
                usable=False,
                status=BrowserSearchStatus.BROWSER_UNAVAILABLE.value,
                detail=str(exc)[:200],
            )
        page = self._challenge_page
        self._challenge_page = None
        if page is None:
            page = await self._context.new_page()
        self._live_page = page
        target_url = url_template.format(query=quote(query, safe=""))
        settle_timeout = (
            float(self.search_settle_timeout_seconds)
            if settle_timeout is None
            else float(settle_timeout)
        )
        wall_state: tuple[str, str] | None = None
        aborted = False
        reused = False
        http_status = getattr(page, "_dtk_status", None)
        try:
            # 1. inspect the page we already have - no navigation yet
            wall_state = await self._detect_page_state(page, structural_fallback=False)
            existing = (
                []
                if wall_state is not None
                else await self._collect_videos(page, query, limit)
            )
            already_there = self._is_target_search_url(self._current_url(page), query)
            if existing or (wall_state is None and already_there):
                # 2. the live page is already (or already shows) the search:
                #    never re-goto, just let it hydrate
                reused = True
                status, videos, detail = await self._stabilise_search_page(
                    page,
                    query,
                    limit,
                    timeout=settle_timeout,
                    poll_seconds=self.search_settle_poll_seconds,
                    on_message=emit,
                )
            elif wall_state is not None:
                status, videos, detail = wall_state[0], [], wall_state[1]
            else:
                # 3. only navigate when the page is genuinely somewhere else
                try:
                    http_status, aborted = await self._goto_tolerating_abort(
                        page, target_url
                    )
                except Exception as exc:
                    LOGGER.warning("session navigation failed: %s", exc)
                    return SessionCheck(
                        usable=False,
                        status=BrowserSearchStatus.DOUYIN_UNREACHABLE.value,
                        detail=f"navigation failed: {str(exc)[:160]}",
                        page_url=self._current_url(page),
                    )
                if aborted:
                    emit(MESSAGE_SELF_NAVIGATION)
                status, videos, detail = await self._stabilise_search_page(
                    page,
                    query,
                    limit,
                    timeout=settle_timeout,
                    poll_seconds=self.search_settle_poll_seconds,
                    on_message=emit,
                )
        except Exception as exc:
            message = str(exc)
            lowered = message.lower()
            if (
                "page is navigating" in lowered
                or "changing the content" in lowered
                or "execution context was destroyed" in lowered
            ):
                # Douyin replaces the verification document while routing back
                # to search.  Reading DOM/content during that small window is
                # expected; keep the same page and let the bounded poll retry.
                status = BrowserSearchStatus.SEARCH_PENDING.value
                videos = []
                detail = "verification page is navigating back to search"
                aborted = True
                wall_state = None
            else:
                LOGGER.warning("session check crashed: %s", exc)
                await self._recover()
                return SessionCheck(
                    usable=False,
                    status=BrowserSearchStatus.BROWSER_CRASHED.value,
                    detail=message[:200],
                )
        finally:
            pass
        keep = bool(
            self.keep_page_on_challenge
            and status in WAITING_STATUSES
            and not self.headless
        )
        self._live_page = None
        if keep:
            self._challenge_page = page
        else:
            try:
                await page.close()
            except Exception:  # pragma: no cover - defensive
                pass
        video_ids = [item.platform_video_id for item in videos]
        page_url = self._current_url(page)
        try:
            page_title = (await page.title()) or ""
        except Exception:  # pragma: no cover - defensive
            page_title = ""
        if not page_title.strip() and self.last_page_title:
            # a background tab can report an empty title; keep the best we saw
            page_title = self.last_page_title
        check = SessionCheck(
            usable=bool(video_ids),
            status=BrowserSearchStatus.OK.value if video_ids else status,
            detail=detail,
            video_ids=video_ids,
            page_url=page_url,
            page_title=page_title,
            http_status=http_status,
            transitional=status == BrowserSearchStatus.SEARCH_PENDING.value,
            aborted_navigation=aborted,
            reused_current_page=reused,
        )
        if check.usable:
            # a verified, usable session clears the sticky wall for this task
            self.reset_block()
            LOGGER.info("douyin browser session is usable (%s videos)", check.video_count)
        return check

    async def _operator_signal_default(
        self,
        *,
        poll_seconds: float,
        sleep: Callable[[float], Awaitable[None]],
    ) -> bool:
        """Wait for Enter (a daemon stdin reader) or the poll window.

        Returns ``True`` when the operator pressed Enter, ``False`` when the
        poll window elapsed first (the caller re-checks the page either way).
        No short CAPTCHA timeout is applied: the window is the configured
        ``poll_seconds``, and the loop simply repeats (section 10).
        """

        import threading

        if self._operator_event is None:
            self._operator_event = threading.Event()
            # only a real terminal can deliver the "press Enter" signal: under
            # pytest/CI stdin is captured, and a blocked reader thread there is
            # useless (polling alone is enough)
            stdin = sys.stdin
            usable_stdin = bool(stdin is not None and getattr(stdin, "isatty", None))
            if usable_stdin:
                try:
                    usable_stdin = bool(stdin.isatty())
                except Exception:  # pragma: no cover - defensive
                    usable_stdin = False
            if usable_stdin and not self._stdin_exhausted:
                event = self._operator_event

                def _reader() -> None:
                    try:
                        while True:
                            line = stdin.readline()
                            if line == "":  # EOF (piped / closed stdin)
                                break
                            if line.strip() == "":
                                event.set()
                                return
                    except Exception:  # pragma: no cover - defensive
                        pass

                self._operator_thread = threading.Thread(
                    target=_reader, name="douyin-verify-stdin", daemon=True
                )
                self._operator_thread.start()
        deadline = time.perf_counter() + max(0.2, float(poll_seconds))
        while time.perf_counter() < deadline:
            if self._operator_event is not None and self._operator_event.is_set():
                self._operator_event.clear()
                return True
            await sleep(min(0.25, max(0.02, deadline - time.perf_counter())))
            # always yield to the event loop, so an injected (instant) sleep in
            # tests can never turn this poll into a busy loop and Ctrl+C /
            # cancellation stay responsive
            await asyncio.sleep(0)
        if self._operator_event is not None and self._operator_event.is_set():
            self._operator_event.clear()
            return True
        return False

    async def wait_for_human_verification(
        self,
        query: str,
        *,
        limit: int | None = None,
        on_message: Callable[[str], None] | None = None,
        wait_for_operator: Callable[[], Awaitable[bool]] | None = None,
        poll_seconds: float = 4.0,
        max_rounds: int = 0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> SessionCheck:
        """Keep the same context alive while the operator clears the wall.

        The program never touches the challenge: it only re-reads the page and
        reports.  It is also acceptable for the operator to finish *without*
        pressing Enter — the page is polled as well.
        """

        emit = on_message or (lambda text: LOGGER.info(text))
        sleep_fn = sleep or asyncio.sleep
        check = await self.check_session(query, limit=limit)
        if check.usable or check.status not in WAITING_STATUSES:
            return check
        started = time.perf_counter()
        rounds = 0
        while True:
            if max_rounds and rounds >= max_rounds:
                check.rounds = rounds
                check.waited_seconds = time.perf_counter() - started
                return check
            rounds += 1
            if rounds == 1:
                emit(INTERACTIVE_NOTICE)
            else:
                emit(self._round_message(check))
            if wait_for_operator is not None:
                signalled = bool(await wait_for_operator())
            else:
                signalled = await self._operator_signal_default(
                    poll_seconds=poll_seconds, sleep=sleep_fn
                )
            check = await self.check_session(query, limit=limit, on_message=emit)
            check.rounds = rounds
            check.waited_seconds = round(time.perf_counter() - started, 1)
            if check.usable or check.status not in WAITING_STATUSES:
                return check
            if not signalled:
                emit(
                    f"第 {rounds} 轮（{check.page_title or 'no title'}）："
                    f"{self._round_message(check, short=True)}"
                )

    @staticmethod
    def _round_message(check: "SessionCheck", *, short: bool = False) -> str:
        """Human wording for the state the live page is actually in (M8.2 §9)."""

        if check.aborted_navigation:
            return MESSAGE_SELF_NAVIGATION
        if check.transitional or check.status == BrowserSearchStatus.SEARCH_PENDING.value:
            return MESSAGE_VERIFICATION_CLEARED if short else (
                MESSAGE_VERIFICATION_CLEARED + "（无需再次验证，程序会自动重试；Ctrl+C 可取消）"
            )
        if check.status in HUMAN_WALL_STATUSES:
            return "仍在验证/登录页。请在浏览器中完成验证，然后按 Enter 立即重试" + (
                "" if short else "（不按 Enter 也会自动轮询）。Ctrl+C 可取消。"
            )
        return f"页面状态 {check.status}：{check.detail}"

    async def ensure_interactive_session(
        self,
        query: str,
        *,
        limit: int | None = None,
        on_message: Callable[[str], None] | None = None,
        wait_for_operator: Callable[[], Awaitable[bool]] | None = None,
        poll_seconds: float = 4.0,
        max_rounds: int = 0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> SessionCheck:
        """Check the live session, involving the operator only when needed."""

        emit = on_message or (lambda text: LOGGER.info(text))
        check = await self.check_session(query, limit=limit, on_message=emit)
        if check.usable:
            return check
        if check.status not in WAITING_STATUSES:
            emit(
                f"浏览器会话不可用（{check.status}）：{check.detail}"
            )
            return check
        return await self.wait_for_human_verification(
            query,
            limit=limit,
            on_message=on_message,
            wait_for_operator=wait_for_operator,
            poll_seconds=poll_seconds,
            max_rounds=max_rounds,
            sleep=sleep,
        )

    async def _search_once(self, query: str, limit: int) -> BrowserSearchResult:
        """One query, with bounded retries and a bounded tab fallback.

        The video tab is tried first; if it renders no results at all (no wall,
        no error) the plain search URL is tried once, because the two tabs are
        rendered by different front-end bundles.
        """

        assert self._context is not None
        result = await self._search_url(query, limit, SEARCH_URL_TEMPLATE)
        if result.videos or result.status not in (
            BrowserSearchStatus.NO_RESULTS.value,
            BrowserSearchStatus.SEARCH_DOM_CHANGED.value,
        ):
            return result
        alternate = await self._search_url(query, limit, SEARCH_URL_TEMPLATE_PLAIN)
        if alternate.videos or alternate.status not in (
            BrowserSearchStatus.NO_RESULTS.value,
            BrowserSearchStatus.SEARCH_DOM_CHANGED.value,
        ):
            return alternate
        return result

    async def _search_url(
        self, query: str, limit: int, url_template: str
    ) -> BrowserSearchResult:
        """Drive one search URL, with bounded retries for gateway failures."""

        attempts = 0
        result: BrowserSearchResult | None = None
        while True:
            attempts += 1
            page = await self._context.new_page()
            keep_page = False
            try:
                result = await self._run_search_flow(
                    page, query, limit, url_template=url_template
                )
                # Milestone 8.1: a human wall keeps this page in the same
                # context so the operator can clear it right here
                keep_page = bool(
                    self.keep_page_on_challenge
                    and result.status in HUMAN_WALL_STATUSES
                    and not self.headless
                )
            finally:
                if not keep_page:
                    try:
                        await page.close()
                    except Exception:  # pragma: no cover - defensive
                        pass
            if keep_page:
                self._challenge_page = page
                LOGGER.info(
                    "keeping the challenged page open for human verification "
                    "(same context, page kept for the interactive gate)"
                )
            result.diagnostics.attempts = attempts
            if result.status not in UPSTREAM_STATUSES or attempts > self.upstream_retry_count:
                break
            delay = self.upstream_retry_backoff_seconds * attempts
            LOGGER.warning(
                "upstream %s from Douyin (attempt %s/%s); retrying in %.1fs",
                result.status,
                attempts,
                self.upstream_retry_count + 1,
                delay,
            )
            if delay:
                await asyncio.sleep(delay)
        assert result is not None
        return result

    async def _run_search_flow(
        self,
        page: Any,
        query: str,
        limit: int,
        *,
        url_template: str = SEARCH_URL_TEMPLATE,
    ) -> BrowserSearchResult:
        url = url_template.format(query=quote(query, safe=""))
        diagnostics = self._base_diagnostics(requested_url=url)
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            if not self._is_aborted_navigation(exc):
                return BrowserSearchResult(
                    status=BrowserSearchStatus.DOUYIN_UNREACHABLE.value,
                    detail=f"navigation failed: {str(exc)[:160]}",
                    page_url=url,
                    diagnostics=diagnostics,
                )
            # Milestone 8.2: Douyin navigated itself (post-verification SPA
            # transition).  Fall through and classify the page we landed on
            # instead of calling the platform unreachable.
            LOGGER.info(
                "page.goto aborted by the page's own navigation (%s); "
                "classifying the current page",
                str(exc)[:120],
            )
            await self._aborted_navigation_recovery(page)
            response = None
        status = getattr(response, "status", None)
        diagnostics.http_status = int(status) if status else None
        await page.wait_for_timeout(int(self.page_settle_seconds * 1000))
        diagnostics.final_url = str(page.url or url)
        try:
            diagnostics.page_title = (await page.title()) or ""
        except Exception:  # pragma: no cover - defensive
            diagnostics.page_title = ""

        # 0. upstream/edge gateway failures come first: a 502 page is neither a
        #    CAPTCHA nor a login wall, and must never be classified as one
        upstream_status = self._upstream_status(diagnostics.http_status)
        if upstream_status is not None:
            diagnostics.browser_status = upstream_status
            return BrowserSearchResult(
                status=upstream_status,
                detail=self._upstream_detail(diagnostics.http_status),
                page_url=diagnostics.final_url,
                diagnostics=diagnostics,
            )

        state = await self._detect_page_state(page, diagnostics)
        if state is not None:
            diagnostics.browser_status = state[0]
            return BrowserSearchResult(
                status=state[0], detail=state[1], page_url=page.url, diagnostics=diagnostics
            )

        videos: list[DiscoveredDouyinVideo] = []
        scrolls = 0
        while len(videos) < limit and scrolls <= self.max_scrolls_per_query:
            found = await self._extract_from_page(page, query)
            before = len(videos)
            videos = dedupe_discovered([*videos, *found], limit=limit)
            if len(videos) >= limit:
                break
            if scrolls >= self.max_scrolls_per_query:
                break
            if scrolls > 0 and len(videos) == before:
                LOGGER.debug("no new results after scroll %s; stopping", scrolls)
                break
            await page.mouse.wheel(0, 2600)
            await page.wait_for_timeout(int(self.scroll_delay_seconds * 1000))
            scrolls += 1
            state = await self._detect_page_state(page, diagnostics)
            if state is not None:
                diagnostics.browser_status = state[0]
                return BrowserSearchResult(
                    videos=videos,
                    status=state[0],
                    detail=state[1],
                    scrolls=scrolls,
                    page_url=page.url,
                    diagnostics=diagnostics,
                )

        state = await self._detect_page_state(page, diagnostics)
        if state is not None and not videos:
            diagnostics.browser_status = state[0]
            return BrowserSearchResult(
                status=state[0], detail=state[1], page_url=page.url, diagnostics=diagnostics
            )
        if not videos:
            has_cards = await self._has_result_containers(page)
            status_value = (
                BrowserSearchStatus.SEARCH_DOM_CHANGED.value
                if has_cards
                else BrowserSearchStatus.NO_RESULTS.value
            )
            detail = (
                "result cards are present but no recognised /video/ links were found"
                if has_cards
                else "no result cards on the page"
            )
            diagnostics.browser_status = status_value
            return BrowserSearchResult(
                status=status_value,
                detail=detail,
                scrolls=scrolls,
                page_url=page.url,
                diagnostics=diagnostics,
            )
        diagnostics.browser_status = BrowserSearchStatus.OK.value
        return BrowserSearchResult(
            videos=videos,
            status=BrowserSearchStatus.OK.value,
            detail=f"{len(videos)} video(s) after {scrolls} scroll(s)",
            scrolls=scrolls,
            page_url=page.url,
            diagnostics=diagnostics,
        )

    # -- page helpers ------------------------------------------------------
    @staticmethod
    def _upstream_status(http_status: int | None) -> str | None:
        """Map an HTTP status onto a transient upstream state (502/503/504)."""

        if http_status is None or http_status not in TRANSIENT_UPSTREAM_STATUSES:
            return None
        if http_status == 502:
            return BrowserSearchStatus.UPSTREAM_BAD_GATEWAY.value
        return BrowserSearchStatus.UPSTREAM_HTTP_ERROR.value

    @staticmethod
    def _upstream_detail(http_status: int | None) -> str:
        label = {502: "502 Bad Gateway", 503: "503 Service Unavailable", 504: "504 Gateway Timeout"}
        described = label.get(int(http_status)) if http_status else "an upstream error"
        return (
            f"Douyin upstream returned HTTP {described}; this is an upstream/network "
            "gateway response, not a CAPTCHA or login classification"
        )

    async def _detect_page_state(
        self,
        page: Any,
        diagnostics: BrowserDiagnostics | None = None,
        *,
        structural_fallback: bool = True,
    ) -> tuple[str, str] | None:
        """Recognise gateway / login / verification walls without passing them.

        ``structural_fallback=False`` (Milestone 8.2, interactive gate) keeps the
        deliberately conservative SDK-only rule out of the decision: right after
        a human verification Douyin keeps loading its anti-bot SDK while the
        search page hydrates, and that must not be re-reported as a wall.
        """

        # infrastructure failures (closed target, crashed renderer) must reach
        # the caller so it can drop the context; only page *content* is
        # classified here
        title = (await page.title()) or ""
        html = await page.content()
        lowered = html.lower()
        if diagnostics is not None:
            diagnostics.page_title = diagnostics.page_title or title
            diagnostics.final_url = diagnostics.final_url or str(getattr(page, "url", "") or "")

        # 1. upstream/edge gateway failure pages (Douyin's kngx edge answers
        #    these). Checked FIRST so a gateway page is never mistaken for a
        #    CAPTCHA/login/DOM change.
        if any(marker in lowered for marker in GATEWAY_PAGE_MARKERS):
            gateway_status = BrowserSearchStatus.UPSTREAM_BAD_GATEWAY.value
            if "503" in lowered or "504" in lowered:
                gateway_status = BrowserSearchStatus.UPSTREAM_HTTP_ERROR.value
            status_label = "502 Bad Gateway" if gateway_status.endswith("bad_gateway") else "5xx"
            return (
                gateway_status,
                f"Douyin returned an upstream gateway error page ({status_label}); "
                "this is an upstream/network response, not a CAPTCHA classification",
            )
        # 2. Douyin's intermediate page: the request was challenged outright
        if any(marker in title for marker in INTERMEDIATE_PAGE_TITLES):
            detail = "Douyin requires a manual slider/verification challenge"
            if self.keep_open_on_challenge and not self.headless:
                detail += "; the browser window is left open for the operator"
            return BrowserSearchStatus.VERIFICATION_REQUIRED.value, detail
        # 3. A login wall in front of the results (what the live search page
        #    shows for anonymous clients): the operator logs in once.
        if any(marker in html for marker in LOGIN_MARKERS):
            return (
                BrowserSearchStatus.LOGIN_REQUIRED.value,
                "Douyin shows a login wall before search results; run "
                "'python app.py --init-douyin-browser' and log in once",
            )
        # 4. A challenge that is actually *rendered* (visible text).  Douyin's
        #    anti-bot SDK is present on every page, so the raw HTML substring
        #    check below is only a fallback for pages with no search UI at all.
        visible_text = await self._visible_text(page, html)
        matched = [marker for marker in VERIFICATION_TEXT_MARKERS if marker in visible_text]
        if matched:
            return (
                BrowserSearchStatus.VERIFICATION_REQUIRED.value,
                f"Douyin rendered a verification challenge ({matched[0]}); "
                "no usable results were rendered",
            )
        # 5. Structural fallback: a captcha/verify payload without any search UI.
        #    Deliberately *not* applied to a page that rendered result containers
        #    or video links, so a page that merely loads the SDK is not
        #    misreported as a human-verification wall.
        if structural_fallback and any(marker in lowered for marker in VERIFICATION_MARKERS):
            has_ui = await self._has_result_containers(page) or bool(
                extract_video_ids_from_html(html)
            )
            if not has_ui:
                return (
                    BrowserSearchStatus.VERIFICATION_REQUIRED.value,
                    "Douyin loaded its verification/captcha system and no usable "
                    "results were rendered",
                )
        return None

    # -- Milestone 8.2: post-verification navigation race ------------------
    @staticmethod
    def _current_url(page: Any) -> str:
        return str(getattr(page, "url", "") or "")

    @staticmethod
    def _is_aborted_navigation(exc: BaseException) -> bool:
        """``net::ERR_ABORTED``: the page navigated itself mid-goto."""

        text = f"{type(exc).__name__}: {exc}".lower()
        return any(marker in text for marker in ABORTED_NAVIGATION_MARKERS)

    @staticmethod
    def _is_target_search_url(url: str, query: str) -> bool:
        """Is this URL already the requested ``/search/<query>`` page?"""

        if not url or "/search/" not in url:
            return False
        tail = url.split("/search/", 1)[1]
        tail = tail.split("?", 1)[0].split("#", 1)[0].strip("/")
        if not tail:
            return False
        try:
            decoded = unquote(tail)
        except Exception:  # pragma: no cover - defensive
            decoded = tail
        wanted = (query or "").strip()
        return decoded == wanted or (wanted and wanted in decoded)

    @staticmethod
    def _title_looks_like_search(title: str) -> bool:
        return any(marker in (title or "") for marker in SEARCH_PAGE_TITLE_MARKERS)

    async def _aborted_navigation_recovery(
        self, page: Any, *, settle_seconds: float = 0.5
    ) -> None:
        """Wait briefly for the page's own transition before inspecting it."""

        await asyncio.sleep(max(0.0, settle_seconds))

    async def _goto_tolerating_abort(
        self, page: Any, url: str
    ) -> tuple[int | None, bool]:
        """``page.goto`` that survives the page navigating itself.

        Returns ``(http_status, aborted)``.  A non-abort navigation error still
        propagates so real network failures keep their old classification.
        """

        try:
            response = await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            if not self._is_aborted_navigation(exc):
                raise
            LOGGER.info(
                "page.goto was aborted by Douyin's own navigation (%s); "
                "classifying the page we actually landed on",
                str(exc)[:120],
            )
            await self._aborted_navigation_recovery(page)
            return None, True
        status = getattr(response, "status", None)
        return (int(status) if status else None), False

    async def _collect_videos(self, page: Any, query: str, limit: int) -> list[Any]:
        try:
            found = await self._extract_from_page(page, query)
        except Exception:  # pragma: no cover - defensive
            return []
        return dedupe_discovered(found, limit=limit)

    async def _stabilise_search_page(
        self,
        page: Any,
        query: str,
        limit: int,
        *,
        timeout: float,
        poll_seconds: float,
        on_message: Callable[[str], None] | None = None,
    ) -> tuple[str, list[Any], str]:
        """Bounded wait for the search page to render real results (section 3).

        Returns ``(status, videos, detail)``.  The deadline bounds *machine*
        settling only - it is never a human-verification timeout.
        """

        emit = on_message or (lambda text: LOGGER.debug(text))
        deadline = time.perf_counter() + max(0.5, float(timeout))
        announced_cleared = False
        announced_hydrating = False
        while True:
            videos = await self._collect_videos(page, query, limit)
            if videos:
                return (
                    BrowserSearchStatus.OK.value,
                    videos,
                    f"{len(videos)} video(s) rendered in the current page",
                )
            state = await self._detect_page_state(page, structural_fallback=False)
            if state is not None and state[0] in HUMAN_WALL_STATUSES:
                return state[0], [], state[1]
            if state is not None and state[0] in UPSTREAM_STATUSES:
                return state[0], [], state[1]
            title = ""
            try:
                title = (await page.title()) or ""
            except Exception:  # pragma: no cover - defensive
                title = ""
            if title.strip():
                self.last_page_title = title.strip()
            has_cards = await self._has_result_containers(page)
            if not announced_cleared and self._title_looks_like_search(title):
                emit(MESSAGE_VERIFICATION_CLEARED)
                announced_cleared = True
            elif not announced_hydrating and has_cards:
                emit(MESSAGE_SEARCH_HYDRATING)
                announced_hydrating = True
            if time.perf_counter() >= deadline:
                break
            await asyncio.sleep(max(0.05, float(poll_seconds)))
        # deadline reached: classify what is actually rendered (never "unreachable")
        has_cards = await self._has_result_containers(page)
        if has_cards:
            return (
                BrowserSearchStatus.SEARCH_DOM_CHANGED.value,
                [],
                "result cards are present but no recognised /video/ links were found",
            )
        if await self._has_search_shell(page):
            return (
                BrowserSearchStatus.NO_RESULTS.value,
                [],
                "the search page rendered but produced no /video/ results",
            )
        return (
            BrowserSearchStatus.SEARCH_PENDING.value,
            [],
            "the page is not showing a challenge anymore, but no search results "
            "have rendered yet",
        )

    async def _visible_text(self, page: Any, html: str) -> str:
        """Rendered page text: never script/style/iframe contents."""

        inner_text = getattr(page, "inner_text", None)
        if inner_text is not None:
            try:
                text = await inner_text("body")
            except Exception:  # pragma: no cover - defensive
                text = ""
            if text:
                return text
        stripped = html
        for tag in _NON_VISIBLE_TAGS:
            stripped = re.sub(
                rf"<{tag}\b.*?</{tag}>", " ", stripped, flags=re.IGNORECASE | re.DOTALL
            )
        return stripped

    async def _has_result_containers(self, page: Any) -> bool:
        for selector in RESULT_CONTAINER_SELECTORS:
            try:
                if await page.locator(selector).count() > 0:
                    return True
            except Exception:  # pragma: no cover - defensive
                continue
        return False

    async def _has_search_shell(self, page: Any) -> bool:
        """Whether the search SPA itself rendered (search box / result shell).

        Milestone 8.2: this distinguishes "the search page is up and genuinely
        has no results" from "the page has not hydrated yet" -- the latter must
        stay ``search_pending`` instead of being misread as a wall/failure.
        """

        for selector in (*RESULT_CONTAINER_SELECTORS, *SEARCH_INPUT_SELECTORS, *SEARCH_SHELL_SELECTORS):
            try:
                if await page.locator(selector).count() > 0:
                    return True
            except Exception:  # pragma: no cover - defensive
                continue
        return False

    async def _extract_from_page(self, page: Any, query: str) -> list[DiscoveredDouyinVideo]:
        """Collect public video links from the rendered search results."""

        urls: list[str] = []
        for selector in VIDEO_HREF_SELECTORS:
            try:
                hrefs = await page.eval_on_selector_all(
                    selector, "els => els.map(e => e.getAttribute('href'))"
                )
            except Exception:  # pragma: no cover - defensive
                continue
            urls.extend(href for href in hrefs if href)

        try:
            html = await page.content()
        except Exception:  # pragma: no cover - defensive
            html = ""
        urls.extend(url for url, _title in extract_pairs_from_html(html))
        if not urls:
            # last resort: aweme ids embedded in the page data
            urls.extend(
                VIDEO_URL_TEMPLATE.format(video_id=video_id)
                for video_id in extract_video_ids_from_html(html)
            )

        valid = [url for url in urls if is_video_url(url)]
        return build_discovered(valid, query=query)

    async def _recover(self) -> None:
        """Drop the broken context so the next query starts a fresh browser."""

        await self.close()

    def _outcome(
        self,
        videos: Sequence[DiscoveredDouyinVideo],
        status: str,
        detail: str,
        *,
        scrolls: int = 0,
        blocked: bool = False,
    ) -> SearchOutcome:
        notes = [f"status={status}"] if status else []
        if detail:
            notes.append(detail)
        if scrolls:
            notes.append(f"scrolls={scrolls}")
        if blocked:
            notes.append("browser discovery paused for this task (wall detected earlier)")
        exhausted = status != BrowserSearchStatus.OK.value or len(videos) == 0
        return SearchOutcome(
            candidates=list(videos),
            backend=self.name,
            notes=notes,
            exhausted=exhausted,
            status=status,
            detail=detail,
        )
