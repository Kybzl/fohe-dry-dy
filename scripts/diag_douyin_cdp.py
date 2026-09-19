"""M8.3 diagnostic: does an *operator-launched* Chrome (V3.2 model) get real results?

Launches Chrome exactly the way V3.2's ``scripts/start_browser.bat`` does
(``--remote-debugging-port=9222 --user-data-dir=<profile>``, no automation
flags), attaches with Playwright over CDP, opens the public search page and
reports what the rendered page exposes.  Read-only: no clicks, no downloads,
no private endpoints, no cookie/token logging.

Usage:
    python scripts/diag_douyin_cdp.py "苹果干烘干" [--profile PATH] [--seconds 25]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import load_settings  # noqa: E402
from sources.douyin_browser_search import (  # noqa: E402
    extract_video_ids_from_html,
    is_video_url,
)

CHROME_CANDIDATES = (
    r"C:\Users\kb\AppData\Local\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def _chrome_binary() -> str:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise SystemExit("no Chrome/Edge binary found")


def _launch_chrome(binary: str, profile: Path, port: int) -> subprocess.Popen:
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    return subprocess.Popen(args, close_fds=True)


async def _wait_for_cdp(port: int, seconds: float = 25.0) -> bool:
    import urllib.request

    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            await asyncio.sleep(0.5)
    return False


async def diagnose(
    query: str,
    *,
    profile: Path,
    port: int,
    settle_seconds: float,
    reuse: bool,
) -> dict[str, Any]:
    from playwright.async_api import async_playwright

    binary = _chrome_binary()
    process = None
    report: dict[str, Any] = {
        "chrome": binary,
        "profile": str(profile),
        "port": port,
        "cdp_launch": not reuse,
    }
    if not reuse:
        process = _launch_chrome(binary, profile, port)
        report["chrome_pid"] = process.pid
        if not await _wait_for_cdp(port):
            report["error"] = "CDP endpoint did not come up"
            return report
    target = f"https://www.douyin.com/search/{quote(query, safe='')}?type=video"
    playwright = await async_playwright().start()
    browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = await context.new_page()
    try:
        report["webdriver_flag"] = await page.evaluate("() => navigator.webdriver")
        response = await page.goto(target, wait_until="domcontentloaded")
        report["http_status"] = getattr(response, "status", None)
        deadline = time.perf_counter() + settle_seconds
        while True:
            html = ""
            try:
                html = await page.content()
            except Exception:
                html = ""
            ids = extract_video_ids_from_html(html)
            anchors = await page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href]'))
                    .map(a => ({attr: a.getAttribute('href'), prop: a.href}))
                    .filter(x => (x.attr || x.prop || '').includes('/video/')).slice(0, 8)"""
            )
            cards = await page.evaluate(
                """() => {
                    const lists = Array.from(document.querySelectorAll('[data-e2e="scroll-list"]'));
                    const first = lists[0];
                    const items = first ? Array.from(first.children) : [];
                    return {
                      list_count: lists.length,
                      card_count: items.length,
                      first_card_text: items[0] ? (items[0].textContent || '').replace(/\\s+/g,' ').trim().slice(0, 80) : '',
                      captcha_container: Boolean(document.querySelector('#captcha_container')),
                      anchor_count: document.querySelectorAll('a[href]').length,
                    };
                }"""
            )
            if ids or anchors:
                break
            if time.perf_counter() >= deadline:
                break
            await asyncio.sleep(2.0)
        report.update(
            {
                "final_url": str(page.url),
                "page_title": (await page.title()) or "",
                "video_ids": ids[:10],
                "video_anchors": anchors,
                "cards": cards,
                "anchor_count": cards.get("anchor_count"),
            }
        )
    finally:
        try:
            await page.close()
        except Exception:
            pass
        try:
            await browser.close()
        finally:
            await playwright.stop()
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--seconds", type=float, default=25.0)
    parser.add_argument("--reuse", action="store_true", help="attach to an already running CDP browser")
    args = parser.parse_args()

    settings = load_settings()
    profile = Path(args.profile) if args.profile else Path(
        r"E:\Codex\fohe-dy\browser_data\douyin-cdp"
    )
    report = asyncio.run(
        diagnose(
            args.query,
            profile=profile,
            port=args.port,
            settle_seconds=args.seconds,
            reuse=args.reuse,
        )
    )
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(settings.project_root) / "logs" / f"douyin-cdp-dom-{stamp}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**report, "saved": str(out)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
