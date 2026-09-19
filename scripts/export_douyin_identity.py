"""Export the current CDP Douyin identity without printing credentials."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


async def export_identity(cdp_url: str, output: Path) -> None:
    playwright = await async_playwright().start()
    browser = await playwright.chromium.connect_over_cdp(cdp_url)
    try:
        if not browser.contexts:
            raise RuntimeError("the CDP browser has no context")
        context = browser.contexts[0]
        page = next((item for item in context.pages if "douyin.com" in item.url), None)
        if page is None:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(
                "https://www.douyin.com/",
                wait_until="domcontentloaded",
                timeout=60_000,
            )

        cookies = await context.cookies(["https://www.douyin.com/"])
        user_agent = await page.evaluate("() => navigator.userAgent")
        header = "; ".join(f"{item['name']}={item['value']}" for item in cookies)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            f"COOKIE={header}\nUSER_AGENT={user_agent}\n",
            encoding="utf-8",
        )

        names = {item["name"] for item in cookies if item.get("value")}
        login_names = {"sessionid", "sessionid_ss", "sid_tt", "sid_guard"}
        print(f"COOKIE_COUNT={len(cookies)}")
        print(f"HAS_TTWID={'true' if 'ttwid' in names else 'false'}")
        print(f"HAS_LOGIN_SESSION={'true' if names & login_names else 'false'}")
        print(f"OUTPUT={output.resolve()}")
    finally:
        await browser.close()
        await playwright.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9230")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(export_identity(args.cdp_url, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
