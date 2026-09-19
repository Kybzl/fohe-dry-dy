"""One browser launch configuration for every Douyin browser entry point.

``--init-douyin-browser``, ``--check-douyin-browser`` and the real browser
search must resolve the *same* persistent profile, channel, executable,
headless flag and locale.  This module is that single source of truth
(Milestone 3.6, sections 7 and 8).

Selecting an installed browser channel is **session consistency**: Douyin can
serve a different challenge to a bundled Chromium than to the normal browser
the operator already uses.  Nothing here spoofs a fingerprint, injects stealth
scripts, bypasses a CAPTCHA or rotates a proxy.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from core.config import AppSettings

LOGGER = logging.getLogger(__name__)

#: bundled Playwright Chromium: no ``channel`` argument at all
BUNDLED_CHANNEL = "chromium"

#: channels Playwright can launch by name, best first
SUPPORTED_CHANNELS: tuple[str, ...] = ("chrome", "msedge")

#: candidate executables per channel, per platform
CHANNEL_EXECUTABLES: dict[str, tuple[str, ...]] = {
    "chrome": (
        # Windows
        "{PROGRAMFILES}/Google/Chrome/Application/chrome.exe",
        "{PROGRAMFILES(X86)}/Google/Chrome/Application/chrome.exe",
        "{LOCALAPPDATA}/Google/Chrome/Application/chrome.exe",
        # macOS / Linux (kept for completeness)
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ),
    "msedge": (
        "{PROGRAMFILES(X86)}/Microsoft/Edge/Application/msedge.exe",
        "{PROGRAMFILES}/Microsoft/Edge/Application/msedge.exe",
        "{LOCALAPPDATA}/Microsoft/Edge/Application/msedge.exe",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/usr/bin/microsoft-edge",
    ),
}


def channel_executable(channel: str, *, env: Mapping[str, str] | None = None) -> str | None:
    """First existing executable for ``channel``, or ``None``.

    Only paths that exist are returned, so an unexpanded ``%PROGRAMFILES%``
    environment variable can never turn into a bogus executable path.
    """

    environment = os.environ if env is None else env
    for template in CHANNEL_EXECUTABLES.get(channel, ()):
        candidate = template
        try:
            candidate = template.format(**environment)
        except KeyError:
            # a missing environment variable leaves the placeholder intact and
            # the path simply will not exist
            candidate = template
        if "{" in candidate:
            continue
        path = Path(candidate)
        if path.exists():
            return str(path)
    return None


def detect_installed_channels(*, env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Installed, supported browser channels, most preferred first."""

    return tuple(
        channel
        for channel in SUPPORTED_CHANNELS
        if channel_executable(channel, env=env) is not None
    )


@dataclass(frozen=True)
class BrowserLaunchConfig:
    """Resolved browser launch settings, shared by every entry point."""

    profile_dir: Path
    channel: str | None
    executable_path: str | None
    headless: bool
    locale: str = "zh-CN"
    viewport_width: int = 1440
    viewport_height: int = 900
    slow_mo_ms: int = 0
    navigation_timeout_seconds: float = 30.0
    requested_channel: str = BUNDLED_CHANNEL
    installed_channels: tuple[str, ...] = field(default_factory=tuple)
    note: str = ""

    @property
    def channel_label(self) -> str:
        return self.channel or BUNDLED_CHANNEL

    def as_dict(self) -> dict[str, object]:
        """Non-sensitive diagnostics; never contains cookies or profile data."""

        return {
            "profile_dir": str(self.profile_dir),
            "browser_channel": self.channel_label,
            "browser_executable": self.executable_path or "",
            "headless": self.headless,
            "locale": self.locale,
            "installed_channels": list(self.installed_channels),
        }

    def summary_lines(self) -> list[str]:
        lines = [
            f"[info] browser profile: {self.profile_dir}",
            f"[info] browser channel: {self.channel_label}"
            + (f" ({self.executable_path})" if self.executable_path else ""),
            f"[info] browser headless: {self.headless} locale={self.locale}",
        ]
        if self.installed_channels:
            lines.append(
                f"[info] installed channels: {', '.join(self.installed_channels)}"
            )
        if self.note:
            lines.append(f"[info] {self.note}")
        return lines


def resolve_browser_config(
    settings: AppSettings,
    *,
    headless: bool | None = None,
    env: Mapping[str, str] | None = None,
    detect: bool = True,
) -> BrowserLaunchConfig:
    """Resolve the browser launch configuration from ``config.yaml``.

    ``browser_channel`` accepts ``auto`` (default), ``chromium``, ``chrome`` or
    ``msedge``.  ``auto`` prefers an installed Chrome, then Edge, then the
    Playwright bundled Chromium.  An explicit channel that is not installed
    falls back to the bundled browser with an explanatory note instead of
    failing the whole task.
    """

    browser = settings.sources.douyin.browser_search
    profile = Path(browser.profile_dir)
    if not profile.is_absolute():
        profile = settings.project_root / profile

    requested = (browser.browser_channel or BUNDLED_CHANNEL).strip().lower()
    configured_executable = (browser.browser_executable_path or "").strip()
    installed = detect_installed_channels(env=env) if detect else ()
    note = ""
    channel: str | None
    executable: str | None = None

    if requested in ("", BUNDLED_CHANNEL, "bundled", "auto"):
        if requested == "auto" and installed:
            channel = installed[0]
            executable = channel_executable(channel, env=env)
            note = f"browser_channel=auto selected installed {channel}"
        else:
            channel = None
    elif requested in SUPPORTED_CHANNELS:
        executable = channel_executable(requested, env=env)
        if executable is None:
            channel = None
            note = (
                f"configured channel {requested!r} is not installed; "
                f"using bundled {BUNDLED_CHANNEL}"
            )
        else:
            channel = requested
    else:
        channel = None
        note = (
            f"unknown browser_channel {requested!r}; using bundled {BUNDLED_CHANNEL} "
            f"(supported: auto, {BUNDLED_CHANNEL}, {', '.join(SUPPORTED_CHANNELS)})"
        )

    if configured_executable:
        if Path(configured_executable).exists():
            executable = configured_executable
            if channel is None:
                # an explicit executable means "use this browser"; Playwright
                # needs a channel name only for its own downloads, so the
                # executable wins and the channel stays bundled
                note = f"{note} | using configured executable".strip(" |")
        else:
            note = f"{note} | configured executable not found, ignored".strip(" |")

    return BrowserLaunchConfig(
        profile_dir=profile,
        channel=channel,
        executable_path=executable,
        headless=browser.headless if headless is None else bool(headless),
        locale=browser.locale,
        viewport_width=int(browser.viewport_width),
        viewport_height=int(browser.viewport_height),
        slow_mo_ms=int(browser.slow_mo_ms),
        navigation_timeout_seconds=float(browser.navigation_timeout_seconds),
        requested_channel=requested,
        installed_channels=installed,
        note=note,
    )
