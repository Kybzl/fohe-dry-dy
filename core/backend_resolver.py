"""Douyin backend resolution and preflight (Milestone 3.6, sections 2/3/5/6).

A base URL in ``config.yaml`` is a *wish*, not a fact.  The classic failure
this module removes: ``127.0.0.1:8000`` is configured, nothing listens there,
and an eleven keyword collection walks into connection refusals one query at a
time.

Resolution order (first backend that answers **and** authenticates wins):

1. the configured/derived base URL (CLI/env/config)
2. the configured fallback backends (``sources.douyin.fallback_base_urls``)
3. ``backend_blocked`` with an exact reason -- the caller must not start a
   collection loop.

Only connectivity and authentication are proven here; no keyword search is
performed, so a preflight costs one ``/healthz`` and one ``/auth/me`` call per
candidate backend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Sequence
from urllib.parse import urlparse

from core.config import AppSettings
from media.ffmpeg import DEFAULT_REMOTE_USER_AGENT
from sources.douyin_backend import (
    UPSTREAM_API_VERSION,
    UPSTREAM_PROJECT,
    BackendCapabilities,
    DouyinBackendClient,
    is_loopback_url,
)

LOGGER = logging.getLogger(__name__)

#: origins, most authoritative first (used in reports and tests)
ORIGIN_CONFIGURED = "configured"
ORIGIN_FALLBACK = "fallback"


@dataclass(frozen=True)
class BackendCandidate:
    """One backend URL worth trying, with where it came from."""

    base_url: str
    origin: str = ORIGIN_CONFIGURED
    note: str = ""

    @property
    def local(self) -> bool:
        return is_loopback_url(self.base_url)


@dataclass
class BackendProbe:
    """Result of probing one backend."""

    base_url: str
    origin: str = ORIGIN_CONFIGURED
    reachable: bool = False
    authorized: bool = False
    version: str = ""
    keyword_search: bool = False
    archive_search: bool = False
    content_read: bool = False
    task_support: bool = False
    http_status: int | None = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        """A backend is usable when it answers *and* accepts our credentials."""

        return bool(self.reachable and self.authorized)

    @property
    def local(self) -> bool:
        return is_loopback_url(self.base_url)

    def as_dict(self) -> dict[str, object]:
        return {
            "base_url": self.base_url,
            "origin": self.origin,
            "local": self.local,
            "reachable": self.reachable,
            "authorized": self.authorized,
            "version": self.version,
            "keyword_search": self.keyword_search,
            "archive_search": self.archive_search,
            "http_status": self.http_status,
            "detail": self.detail,
        }

    def summary_lines(self) -> list[str]:
        kind = "local" if self.local else "remote"
        lines = [
            f"[info] candidate backend ({self.origin}, {kind}): {self.base_url or '(unset)'}",
            f"[{'ok' if self.reachable else 'warn'}] reachable: {self.reachable}",
            f"[{'ok' if self.authorized else 'warn'}] authentication accepted: {self.authorized}",
        ]
        if self.version:
            lines.append(f"[ok] backend version: {self.version}")
        lines.append(
            f"[{'ok' if self.keyword_search else 'warn'}] keyword search: "
            f"{'available' if self.keyword_search else 'not provided by this backend'}"
        )
        lines.append(
            f"[{'ok' if self.archive_search else 'warn'}] archive search: "
            f"{'available' if self.archive_search else 'unavailable'}"
        )
        if self.detail:
            lines.append(f"[info] {self.detail}")
        return lines


@dataclass
class BackendSelection:
    """Outcome of the preflight: the backend to use, or why none can be used."""

    selected: BackendProbe | None = None
    candidates: list[BackendProbe] = field(default_factory=list)
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.selected is not None and self.selected.usable

    @property
    def blocked(self) -> bool:
        return not self.usable

    @property
    def base_url(self) -> str:
        return self.selected.base_url if self.selected else ""

    @property
    def origin(self) -> str:
        return self.selected.origin if self.selected else ""

    def summary_lines(self) -> list[str]:
        lines = [
            f"[info] upstream contract: {UPSTREAM_PROJECT} {UPSTREAM_API_VERSION}",
        ]
        for probe in self.candidates:
            lines.extend(probe.summary_lines())
        if self.usable and self.selected is not None:
            kind = "local" if self.selected.local else "remote"
            lines.append(
                f"[ok] selected {kind} backend ({self.selected.origin}): "
                f"{self.selected.base_url}"
            )
        else:
            lines.append(f"[warn] backend_blocked: {self.reason}")
            lines.append(
                "[info] no collection task will start until a backend answers "
                "and accepts the configured credentials"
            )
        return lines

    def as_dict(self) -> dict[str, object]:
        return {
            "usable": self.usable,
            "blocked": self.blocked,
            "reason": self.reason,
            "selected": self.selected.as_dict() if self.selected else None,
            "candidates": [probe.as_dict() for probe in self.candidates],
        }


def backend_candidates(settings: AppSettings) -> list[BackendCandidate]:
    """Ordered, de-duplicated backend candidates for this configuration."""

    douyin = settings.sources.douyin
    candidates: list[BackendCandidate] = []
    primary = settings.douyin_base_url().strip()
    if primary:
        candidates.append(
            BackendCandidate(primary, origin=settings.douyin_backend_source())
        )
    fallbacks: list[str] = []
    env_fallbacks = settings.secret("DOUYIN_BACKEND_FALLBACK_URLS")
    if env_fallbacks:
        fallbacks.extend(part.strip() for part in env_fallbacks.split(","))
    fallbacks.extend(url.strip() for url in douyin.fallback_base_urls)
    for url in fallbacks:
        if not url:
            continue
        if any(existing.base_url == url for existing in candidates):
            continue
        candidates.append(BackendCandidate(url, origin=ORIGIN_FALLBACK))
    return candidates


def _probe_from_capabilities(
    capabilities: BackendCapabilities,
    *,
    base_url: str,
    origin: str,
) -> BackendProbe:
    return BackendProbe(
        base_url=base_url,
        origin=origin,
        reachable=bool(capabilities.reachable),
        authorized=bool(capabilities.authorized),
        version=capabilities.version,
        keyword_search=bool(capabilities.keyword_search),
        archive_search=bool(capabilities.archive_search),
        content_read=bool(capabilities.content_read),
        task_support=bool(capabilities.task_support),
        detail="; ".join(capabilities.notes[:2]),
    )


async def probe_backend(
    settings: AppSettings,
    candidate: BackendCandidate,
) -> BackendProbe:
    """Connectivity + authentication probe for one backend URL."""

    client = DouyinBackendClient(
        base_url=candidate.base_url,
        api_key=settings.douyin_api_key(),
        session_cookie=settings.douyin_session_cookie(),
        timeout=min(settings.sources.douyin.request_timeout_seconds, 15.0),
        max_retries=2,
        backoff_seconds=0.5,
        user_agent=settings.media.remote_user_agent or DEFAULT_REMOTE_USER_AGENT,
        trust_env=settings.sources.douyin.trust_env,
    )
    try:
        capabilities = await client.health(deep=True)
        probe = _probe_from_capabilities(
            capabilities, base_url=candidate.base_url, origin=candidate.origin
        )
        if not probe.reachable:
            probe.detail = _unreachable_detail(candidate.base_url, probe.detail)
        return probe
    except Exception as exc:  # pragma: no cover - defensive
        return BackendProbe(
            base_url=candidate.base_url,
            origin=candidate.origin,
            detail=_unreachable_detail(candidate.base_url, str(exc)[:200]),
        )
    finally:
        await client.aclose()


def _unreachable_detail(base_url: str, detail: str) -> str:
    if is_loopback_url(base_url):
        return (
            f"no dtk backend answered on {base_url}; verify that dtk is running "
            f"on this port and that localhost traffic is not routed through a "
            f"system proxy ({detail})".strip()
        )
    host = urlparse(base_url).hostname or base_url
    return f"{host} did not answer as a dtk backend ({detail})".strip()


async def resolve_douyin_backend(
    settings: AppSettings,
    *,
    candidates: Sequence[BackendCandidate] | None = None,
    prober: Callable[[AppSettings, BackendCandidate], Awaitable[BackendProbe]] | None = None,
) -> BackendSelection:
    """Probe every candidate and select the first usable backend.

    ``prober`` is injectable so tests never touch the network.
    """

    plan = list(candidates) if candidates is not None else backend_candidates(settings)
    probe = prober or probe_backend
    selection = BackendSelection()
    if not plan:
        selection.reason = (
            "sources.douyin.base_url is empty and no fallback backend is configured"
        )
        LOGGER.warning("douyin backend resolution: %s", selection.reason)
        return selection

    for candidate in plan:
        try:
            probe_result = await probe(settings, candidate)
        except Exception as exc:  # pragma: no cover - defensive
            probe_result = BackendProbe(
                base_url=candidate.base_url,
                origin=candidate.origin,
                detail=str(exc)[:200],
            )
        selection.candidates.append(probe_result)
        if probe_result.usable:
            selection.selected = probe_result
            LOGGER.info(
                "douyin backend selected (%s, %s): %s",
                probe_result.origin,
                "local" if probe_result.local else "remote",
                probe_result.base_url,
            )
            return selection

    tried = ", ".join(probe.base_url or "(unset)" for probe in selection.candidates)
    failures = "; ".join(
        f"{probe.base_url}: "
        + ("authentication refused" if probe.reachable else probe.detail or "unreachable")
        for probe in selection.candidates
    )
    selection.reason = f"no usable dtk backend among [{tried}] ({failures})"
    LOGGER.warning("douyin backend resolution: %s", selection.reason)
    return selection


def apply_backend_selection(settings: AppSettings, selection: BackendSelection) -> AppSettings:
    """Point the runtime at the selected backend without touching config files.

    Only a *different* URL is applied: when the configured backend is the one
    that answered, nothing changes and no hidden override is introduced.
    """

    if not selection.usable or selection.selected is None:
        return settings
    if selection.selected.base_url != settings.douyin_base_url().strip():
        settings.douyin_backend_override = selection.selected.base_url
        settings.douyin_backend_origin = selection.selected.origin
    return settings
