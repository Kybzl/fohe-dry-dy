"""M8.3 diagnostic: read the *public* rendered DOM of a Douyin search page.

Read-only.  It opens the same persistent Chrome profile the app uses, navigates
to the search page, waits (bounded) for the search SPA, and prints/saves a
sanitized description of a few result cards so we can see how video links are
actually exposed.  No cookies, no tokens, no network calls to private APIs, no
downloads, no CAPTCHA handling.

Usage:
    python scripts/diag_douyin_dom.py "苹果干烘干" [--profile PATH] [--seconds 25]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import load_settings  # noqa: E402
from sources.douyin_browser_search import (  # noqa: E402
    GATEWAY_PAGE_MARKERS,
    INTERMEDIATE_PAGE_TITLES,
    RESULT_CONTAINER_SELECTORS,
    VERIFICATION_MARKERS,
    VERIFICATION_TEXT_MARKERS,
    extract_video_ids_from_html,
    is_video_url,
    normalize_video_url,
)

SAFE_ATTR_PREFIXES = ("data-", "aria-", "role", "title", "href", "id", "class")


def _sanitize(value: str, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


async def _card_snapshot(page: Any, *, limit: int = 6) -> list[dict[str, Any]]:
    script = """
    (limit) => {
      const nodes = Array.from(document.querySelectorAll('a[href], [data-e2e], li, article'))
        .filter(el => el.tagName === 'A' ? true : true);
      const cards = [];
      const seen = new Set();
      for (const el of nodes) {
        const container = el.closest('[data-e2e], li, article') || el;
        if (seen.has(container)) continue;
        seen.add(container);
        const anchor = container.querySelector('a[href]');
        const target = anchor || el;
        const attrs = {};
        for (const a of (target.attributes || [])) {
          if (a.name && a.name.length < 40) attrs[a.name] = String(a.value).slice(0, 200);
        }
        const parent = container.parentElement;
        cards.push({
          tag: target.tagName,
          attrs: attrs,
          href_attr: target.getAttribute ? target.getAttribute('href') : null,
          href_prop: target.href || null,
          text: (target.textContent || '').slice(0, 120),
          container_tag: container.tagName,
          container_attrs: (() => {
            const out = {};
            for (const a of (container.attributes || [])) {
              if (a.name && a.name.length < 40) out[a.name] = String(a.value).slice(0, 200);
            }
            return out;
          })(),
          parent_tag: parent ? parent.tagName : null,
          parent_attrs: parent ? (() => {
            const out = {};
            for (const a of (parent.attributes || [])) {
              if (a.name && a.name.length < 40) out[a.name] = String(a.value).slice(0, 200);
            }
            return out;
          })() : null,
          has_anchor: Boolean(anchor),
          anchor_count: container.querySelectorAll('a[href]').length,
        });
        if (cards.length >= limit) break;
      }
      return cards;
    }
    """
    try:
        return await page.evaluate(script, limit)
    except Exception as exc:  # pragma: no cover - diagnostic
        return [{"error": f"{type(exc).__name__}: {exc}"}]


async def _anchor_snapshot(page: Any, *, limit: int = 12) -> list[dict[str, Any]]:
    script = """
    (limit) => {
      const out = [];
      for (const a of document.querySelectorAll('a[href]')) {
        out.push({
          href_attr: a.getAttribute('href'),
          href_prop: a.href,
          text: (a.textContent || '').slice(0, 60),
          cls: String(a.className || '').slice(0, 120),
        });
        if (out.length >= limit) break;
      }
      return out;
    }
    """
    try:
        return await page.evaluate(script, limit)
    except Exception as exc:  # pragma: no cover - diagnostic
        return [{"error": f"{type(exc).__name__}: {exc}"}]


async def _scroll_list_snapshot(page: Any, *, items: int = 3, depth: int = 4) -> dict[str, Any]:
    """Structure of the first result items inside ``[data-e2e=scroll-list]``."""

    script = """
    ({items, depth}) => {
      const lists = Array.from(document.querySelectorAll('[data-e2e="scroll-list"]'));
      const out = { list_count: lists.length, lists: [] };
      for (const list of lists.slice(0, 3)) {
        const children = Array.from(list.children).slice(0, items).map((child) => {
          const describe = (el, level) => {
            const attrs = {};
            for (const a of (el.attributes || [])) {
              if (a.name && a.name.length < 40) attrs[a.name] = String(a.value).slice(0, 160);
            }
            const node = {
              tag: el.tagName,
              attrs: attrs,
              text: (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 80),
              child_count: el.children.length,
            };
            if (level > 1 && el.children.length) {
              node.children = Array.from(el.children).slice(0, 4).map((c) => describe(c, level - 1));
            }
            return node;
          };
          return describe(child, depth);
        });
        out.lists.push({
          attrs: (() => {
            const a = {};
            for (const at of (list.attributes || [])) a[at.name] = String(at.value).slice(0, 160);
            return a;
          })(),
          child_count: list.children.length,
          children: children,
        });
      }
      // every attribute name that appears anywhere under the result lists
      const names = new Set();
      for (const list of lists) {
        for (const el of list.querySelectorAll('*')) {
          for (const a of (el.attributes || [])) names.add(a.name);
        }
      }
      out.attribute_names = Array.from(names).slice(0, 60);
      return out;
    }
    """
    try:
        return await page.evaluate(script, {"items": items, "depth": depth})
    except Exception as exc:  # pragma: no cover - diagnostic
        return {"error": f"{type(exc).__name__}: {exc}"}


async def _embedded_payload_probe(page: Any) -> dict[str, Any]:
    """Look for publicly rendered page JSON that carries video ids."""

    script = """
    () => {
      const globals = ['_ROUTER_DATA', '__INITIAL_STATE__', '__NUXT__', 'RENDER_DATA', '__NEXT_DATA__'];
      const present = {};
      for (const name of globals) {
        try { present[name] = typeof window[name] !== 'undefined'; } catch (e) { present[name] = false; }
      }
      const scripts = Array.from(document.querySelectorAll('script'));
      let withAwemeId = 0;
      const sample = [];
      for (const s of scripts) {
        const text = s.textContent || '';
        const matches = text.match(/aweme_id"?\\s*[:=]\\s*"?\\d{6,}/g);
        if (matches) {
          withAwemeId += 1;
          if (sample.length < 5) sample.push(matches.slice(0, 3).join(' | ').slice(0, 200));
        }
      }
      return {
        globals: present,
        script_count: scripts.length,
        scripts_with_aweme_id: withAwemeId,
        samples: sample,
        body_text_has_video_path: location.href.includes('/video/'),
      };
    }
    """
    try:
        return await page.evaluate(script)
    except Exception as exc:  # pragma: no cover - diagnostic
        return {"error": f"{type(exc).__name__}: {exc}"}


async def _render_data_probe(page: Any) -> dict[str, Any]:
    """Inspect the page's own SSR JSON holder (if present) - no secrets."""

    script = """
    () => {
      const el = document.getElementById('RENDER_DATA');
      const out = { present: Boolean(el), length: el ? (el.textContent || '').length : 0 };
      if (el) {
        const text = el.textContent || '';
        out.has_aweme = text.includes('aweme');
        out.has_search = text.includes('search');
        out.aweme_id_hits = (text.match(/aweme_id/g) || []).length;
        out.head = text.slice(0, 300);
      }
      return out;
    }
    """
    try:
        return await page.evaluate(script)
    except Exception as exc:  # pragma: no cover - diagnostic
        return {"error": f"{type(exc).__name__}: {exc}"}


async def _click_probe(page: Any, context: Any, *, max_seconds: float = 8.0) -> dict[str, Any]:
    """Click the first public result card and see how the public URL appears."""

    report: dict[str, Any] = {}
    try:
        cards = await page.query_selector_all('[data-e2e="scroll-list"] > li')
        report["card_count"] = len(cards)
        if not cards:
            return report
        first = cards[0]
        report["card_text"] = _sanitize(await first.inner_text(), 80)
        before_pages = len(context.pages)
        before_url = str(page.url)
        box = await first.bounding_box()
        report["has_box"] = bool(box)
        await first.scroll_into_view_if_needed()
        await asyncio.sleep(0.4)
        await first.click(timeout=5000)
        deadline = time.perf_counter() + max_seconds
        while time.perf_counter() < deadline:
            pages = list(context.pages)
            if len(pages) > before_pages:
                new_page = pages[-1]
                try:
                    await new_page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                report["opened_new_tab"] = True
                report["new_tab_url"] = str(new_page.url)
                report["new_tab_title"] = _sanitize(await new_page.title(), 80)
                await new_page.close()
                break
            if str(page.url) != before_url:
                report["opened_new_tab"] = False
                report["same_tab_url"] = str(page.url)
                report["same_tab_title"] = _sanitize(await page.title(), 80)
                await page.go_back(wait_until="domcontentloaded")
                break
            await asyncio.sleep(0.3)
        report["final_url_after_probe"] = str(page.url)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    return report


async def diagnose(
    query: str,
    *,
    profile_dir: Path,
    channel: str | None,
    settle_seconds: float,
    poll_seconds: float = 2.0,
    headless: bool = False,
    click_probe: bool = False,
) -> dict[str, Any]:
    from playwright.async_api import async_playwright

    target = f"https://www.douyin.com/search/{quote(query, safe='')}?type=video"
    plain = f"https://www.douyin.com/search/{quote(query, safe='')}"
    launch: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": headless,
        "locale": "zh-CN",
        "viewport": {"width": 1440, "height": 900},
    }
    if channel:
        launch["channel"] = channel
    report: dict[str, Any] = {
        "query": query,
        "profile_dir": str(profile_dir),
        "headless": headless,
        "target_url": target,
    }
    playwright = await async_playwright().start()
    context = await playwright.chromium.launch_persistent_context(**launch)
    page = await context.new_page()
    try:
        started = time.perf_counter()
        response = await page.goto(target, wait_until="domcontentloaded")
        report["http_status"] = getattr(response, "status", None)
        deadline = started + settle_seconds
        best: dict[str, Any] = {}
        while True:
            title = ""
            try:
                title = (await page.title()) or ""
            except Exception:
                title = ""
            html = ""
            try:
                html = await page.content()
            except Exception:
                html = ""
            lowered = html.lower()
            visible = ""
            try:
                visible = await page.inner_text("body")
            except Exception:
                visible = ""
            ids = extract_video_ids_from_html(html)
            anchors = await _anchor_snapshot(page)
            video_anchors = [a for a in anchors if is_video_url(a.get("href_attr") or a.get("href_prop") or "")]
            best = {
                "page_url": str(page.url),
                "page_title": title,
                "anchor_count": len(anchors),
                "video_anchor_count": len(video_anchors),
                "html_video_ids": ids[:10],
                "challenge_title": any(m in title for m in INTERMEDIATE_PAGE_TITLES),
                "challenge_visible": [m for m in VERIFICATION_TEXT_MARKERS if m in (visible or "")],
                "challenge_markers": [m for m in VERIFICATION_MARKERS if m in lowered],
                "gateway": [m for m in GATEWAY_PAGE_MARKERS if m in lowered],
                "elapsed": round(time.perf_counter() - started, 1),
            }
            if ids or video_anchors or best["challenge_title"]:
                break
            if time.perf_counter() >= deadline:
                break
            await asyncio.sleep(poll_seconds)
        report["first_pass"] = dict(best)
        report["anchors"] = await _anchor_snapshot(page, limit=15)
        report["cards"] = await _card_snapshot(page, limit=6)
        report["scroll_lists"] = await _scroll_list_snapshot(page)
        report["embedded_payloads"] = await _embedded_payload_probe(page)
        report["render_data"] = await _render_data_probe(page)
        if click_probe:
            report["click_probe"] = await _click_probe(page, context)
        try:
            containers = {}
            for selector in RESULT_CONTAINER_SELECTORS:
                containers[selector] = await page.locator(selector).count()
            report["container_counts"] = containers
        except Exception as exc:  # pragma: no cover - diagnostic
            report["container_counts"] = {"error": str(exc)}
        # a second look at the plain (non-video) search URL, only if the first
        # pass found nothing at all
        if not report["first_pass"].get("html_video_ids") and not report["first_pass"].get(
            "video_anchor_count"
        ):
            try:
                await page.goto(plain, wait_until="domcontentloaded")
                await asyncio.sleep(min(6.0, poll_seconds * 3))
                html = await page.content()
                report["plain_pass"] = {
                    "page_url": str(page.url),
                    "page_title": (await page.title()) or "",
                    "html_video_ids": extract_video_ids_from_html(html)[:10],
                    "anchors": await _anchor_snapshot(page, limit=10),
                }
            except Exception as exc:  # pragma: no cover - diagnostic
                report["plain_pass"] = {"error": f"{type(exc).__name__}: {exc}"}
        normalized = [
            normalize_video_url(f"https://www.douyin.com/video/{vid}")
            for vid in report["first_pass"].get("html_video_ids", [])
        ]
        report["normalized_examples"] = normalized[:5]
    finally:
        try:
            await context.close()
        finally:
            await playwright.stop()
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--seconds", type=float, default=25.0)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--click-probe", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    profile = Path(args.profile) if args.profile else settings.project_root / settings.sources.douyin.browser_search.profile_dir
    report = asyncio.run(
        diagnose(
            args.query,
            profile_dir=profile,
            channel=args.channel or None,
            settle_seconds=args.seconds,
            headless=args.headless,
            click_probe=args.click_probe,
        )
    )
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(settings.project_root) / "logs" / f"douyin-search-dom-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    first = report.get("first_pass", {})
    print(json.dumps(
        {
            "target_url": report.get("target_url"),
            "http_status": report.get("http_status"),
            "page_url": first.get("page_url"),
            "page_title": first.get("page_title"),
            "challenge_title": first.get("challenge_title"),
            "challenge_visible": first.get("challenge_visible"),
            "gateway": first.get("gateway"),
            "anchor_count": first.get("anchor_count"),
            "video_anchor_count": first.get("video_anchor_count"),
            "html_video_ids": first.get("html_video_ids"),
            "container_counts": report.get("container_counts"),
            "elapsed": first.get("elapsed"),
            "saved": str(out),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
