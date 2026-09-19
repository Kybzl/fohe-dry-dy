"""Opt-in live Douyin checks - never run by a normal ``pytest``.

These touch the real Douyin website and a real dtk backend, so they are gated
behind environment variables and are skipped otherwise (section 25):

    $env:FOHE_DOUYIN_LIVE = "1"
    $env:DOUYIN_BACKEND_BASE_URL = "https://demo.douyin.wtf"   # or your own
    $env:DOUYIN_BACKEND_SESSION_COOKIE = "dtk_session=..."     # or an API key
    python -m pytest tests/test_douyin_browser_live.py -v
"""

from __future__ import annotations

import asyncio
import os

import pytest

from core.config import load_settings
from core.dependencies import build_browser_search, build_douyin_client
from sources.douyin_backend import DouyinBackendError
from sources.douyin_search import BrowserSearchStatus

LIVE = os.environ.get("FOHE_DOUYIN_LIVE") == "1"

pytestmark = pytest.mark.skipif(
    not LIVE, reason="set FOHE_DOUYIN_LIVE=1 to run live Douyin checks"
)


def run(coro):
    return asyncio.run(coro)


def test_live_browser_search_reports_a_real_state() -> None:
    """Real browser search: either results, or an honest wall (no bypass)."""

    settings = load_settings()
    backend = build_browser_search(settings, headless=True)

    async def flow() -> tuple[str, list[str], str]:
        try:
            outcome = await backend.search("苹果干烘干", 5)
        finally:
            await backend.close()
        return (
            outcome.status,
            [item.platform_video_id for item in outcome.candidates],
            outcome.detail,
        )

    status, ids, detail = run(flow())
    assert status in {item.value for item in BrowserSearchStatus}, status
    if status == BrowserSearchStatus.OK.value:
        assert ids, "an OK status must come with discovered public video ids"
    else:
        assert detail, "a non-OK status must explain itself"


def test_live_dtk_backend_reaches_a_real_douyin_video() -> None:
    """Real dtk backend must return metadata for a real public Douyin post."""

    settings = load_settings()
    client = build_douyin_client(settings)
    if not client.base_url or not client.authenticated:
        pytest.skip("no real dtk backend credentials configured")

    async def flow():
        try:
            return await client.archive_search(q="苹果", limit=3)
        finally:
            await client.aclose()

    try:
        result = run(flow())
    except DouyinBackendError as exc:
        pytest.skip(f"backend unavailable: {exc}")
    items = (result.data or {}).get("items") if isinstance(result.data, dict) else None
    assert items, "expected at least one archived Douyin post"
    first = items[0]
    assert first["platform"] == "douyin"
    assert first["content_id"]
    assert first["web_url"].startswith("https://www.douyin.com/")
