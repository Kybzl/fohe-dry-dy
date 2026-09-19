"""Douyin discovery backends (section 47 compliant).

The reference backend (**dtk v5.0.3**) exposes content reads, author/mix reads
and an archive search, but **no platform keyword search**.  Instead of
inventing a route we:

1. detect a keyword-search route on the *operator's* backend when it has one
   (:class:`KeywordSearchBackend` -- auto-discovered from ``/openapi.json``)
2. fall back to documented, supported discovery mechanisms:
   :class:`ArchiveSearchBackend` (``q=`` over what the instance collected),
   :class:`AuthorPostsSearchBackend` (``user/posts``), 
   :class:`MixPostsSearchBackend` (``mix/posts``) and
   :class:`ManualUrlSearchBackend` (explicit post URLs)
3. report exactly what is missing through ``python app.py --check-douyin``
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from core.models import VideoCandidate
from sources.douyin_backend import (
    DouyinBackendClient,
    DouyinBackendError,
    DouyinBackendSchemaError,
    TaskResult,
)
from sources.douyin_models import content_to_candidate, extract_contents, page_items

LOGGER = logging.getLogger(__name__)


class DiscoveryBackend(StrEnum):
    """Which discovery path produced a candidate (section 15/24)."""

    DTK_KEYWORD = "dtk_keyword"
    BROWSER = "browser"
    ARCHIVE = "archive"
    AUTHOR = "author"
    MIX = "mix"
    MANUAL_URL = "manual_url"


class BrowserSearchStatus(StrEnum):
    """Explicit browser-search states (section 22)."""

    OK = "ok"
    BROWSER_UNAVAILABLE = "browser_unavailable"
    DOUYIN_UNREACHABLE = "douyin_unreachable"
    #: upstream/edge gateway answered 502 (Bad Gateway) - not a CAPTCHA,
    #: not a login wall: the platform's gateway is failing for us
    UPSTREAM_BAD_GATEWAY = "upstream_bad_gateway"
    #: upstream/edge answered another server error (503/504/5xx)
    UPSTREAM_HTTP_ERROR = "upstream_http_error"
    LOGIN_REQUIRED = "login_required"
    VERIFICATION_REQUIRED = "verification_required"
    #: Milestone 8.2: the rendered challenge is gone but the search page has
    #: not produced usable results yet (Douyin is still hydrating / SPA
    #: routing).  This is *not* a verification state and *not* unreachable.
    SEARCH_PENDING = "search_pending"
    SEARCH_TIMEOUT = "search_timeout"
    SEARCH_DOM_CHANGED = "search_dom_changed"
    NO_RESULTS = "no_results"
    BROWSER_CRASHED = "browser_crashed"
    #: no discovery backend can run at all (see CompositeSearchBackend)
    DISCOVERY_BLOCKED = "discovery_blocked"
    #: the configured backend is unusable for this task
    BACKEND_UNAVAILABLE = "backend_unavailable"


#: HTTP statuses treated as bounded, transient upstream/gateway failures
TRANSIENT_UPSTREAM_STATUSES: frozenset[int] = frozenset({502, 503, 504})


class DiscoveryBlockedError(RuntimeError):
    """No discovery backend can execute: stop the search phase for this task."""

    def __init__(self, detail: str, *, states: dict[str, str] | None = None) -> None:
        super().__init__(detail)
        self.states = dict(states or {})


class DiscoveredDouyinVideo(BaseModel):
    """A public Douyin video found by a discovery backend (section 8)."""

    model_config = ConfigDict(extra="ignore")

    platform_video_id: str
    source_url: str
    search_query: str = ""
    visible_title: str | None = None
    visible_author: str | None = None
    discovery_backend: str = DiscoveryBackend.BROWSER.value
    #: optional fields a richer backend (dtk content read) already knows
    duration: float | None = None
    cover_url: str | None = None
    author_id: str | None = None
    published_at: datetime | None = None
    statistics: dict[str, Any] = Field(default_factory=dict)
    matched_queries: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: raw dtk content payload when this candidate came from a dtk read
    detail: dict[str, Any] | None = None

    def to_candidate(self, *, platform: str = "douyin") -> VideoCandidate:
        """Adapt to the platform-independent ``VideoCandidate``."""

        queries = list(dict.fromkeys([*self.matched_queries, *([self.search_query] if self.search_query else [])]))
        metadata = dict(self.metadata)
        if self.visible_title:
            metadata.setdefault("visible_title", self.visible_title)
        if self.visible_author:
            metadata.setdefault("visible_author", self.visible_author)
        metadata.setdefault("discovery", self.discovery_backend)
        # provenance marker (Milestone 3.7, section 23)
        metadata.setdefault("source_adapter", "douyin")
        if self.detail is not None:
            metadata.setdefault("dtk_detail", self.detail)
        return VideoCandidate(
            platform=platform,
            platform_video_id=self.platform_video_id,
            source_url=self.source_url,
            title=self.visible_title or "",
            author=self.visible_author or "",
            author_id=self.author_id,
            duration=self.duration,
            cover_url=self.cover_url,
            published_at=self.published_at,
            statistics=dict(self.statistics),
            matched_queries=queries,
            metadata=metadata,
        )

#: qualifiers that make a keyword search specific rather than broad
SPECIFIC_QUALIFIERS: tuple[str, ...] = (
    "热泵",
    "烘干房",
    "烘干机",
    "生产线",
    "加工厂",
    "设备",
    "工艺",
    "流程",
    "技术",
    "设备",
    "干燥机",
    "车间",
)


def prioritize_queries(queries: Sequence[str], *, material: str = "") -> list[str]:
    """Order search terms most-specific-first (section 11).

    Specific phrases ("苹果片烘干", "苹果热泵烘干") run before broad ones
    ("苹果干"), so a task usually reaches its clip target before spending
    requests on terms that mostly return unrelated footage.
    """

    def score(query: str) -> tuple[int, int, int]:
        text = query.strip()
        qualifier_hits = sum(1 for word in SPECIFIC_QUALIFIERS if word in text)
        # a query that is only the material (or shorter) is the broadest kind
        is_bare = bool(material) and text in (material, material.rstrip("干片粉条丝"))
        length = len(text)
        return (0 if is_bare else 1, qualifier_hits, length)

    ordered = sorted(dict.fromkeys(query.strip() for query in queries if query.strip()),
                     key=score, reverse=True)
    return ordered


@dataclass
class SearchOutcome:
    """Result of one discovery call."""

    candidates: list[DiscoveredDouyinVideo] = field(default_factory=list)
    backend: str = ""
    notes: list[str] = field(default_factory=list)
    exhausted: bool = True
    cursor: str | None = None
    status: str = ""
    detail: str = ""
    #: non-sensitive diagnostics (urls, http status, page title, browser state)
    diagnostics: dict[str, Any] = field(default_factory=dict)


class CandidateCollector:
    """Global candidate deduplication before any AI work (section 12).

    Duplicates collapse by ``platform + platform_video_id`` and by normalized
    source URL; the search terms that produced them are merged into
    ``matched_queries`` so search quality can be tuned later.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, VideoCandidate] = {}
        self._discovered: dict[str, DiscoveredDouyinVideo] = {}
        self._by_url: dict[str, str] = {}
        self.total_seen = 0

    def add(self, candidates: Sequence[VideoCandidate], *, query: str = "") -> list[VideoCandidate]:
        """Add candidates; return only the ones that were new."""

        fresh: list[VideoCandidate] = []
        for candidate in candidates:
            self.total_seen += 1
            key = candidate.dedup_key
            url_key = candidate.normalized_url
            existing = self._by_id.get(key)
            if existing is None and url_key:
                existing = self._by_id.get(self._by_url.get(url_key, ""))
            if existing is not None:
                if query and query not in existing.matched_queries:
                    existing.matched_queries.append(query)
                continue
            if query and query not in candidate.matched_queries:
                candidate.matched_queries.append(query)
            self._by_id[key] = candidate
            if url_key:
                self._by_url[url_key] = key
            fresh.append(candidate)
        return fresh

    def add_discovered(
        self, candidates: Sequence[DiscoveredDouyinVideo], *, query: str = ""
    ) -> list[DiscoveredDouyinVideo]:
        """Same as :meth:`add` but for ``DiscoveredDouyinVideo`` items."""

        fresh: list[DiscoveredDouyinVideo] = []
        for candidate in candidates:
            self.total_seen += 1
            key = f"douyin:{candidate.platform_video_id}"
            url_key = candidate.source_url.split("?", 1)[0].rstrip("/")
            existing = self._discovered.get(key)
            if existing is None and url_key:
                existing_key = self._by_url.get(url_key)
                if existing_key:
                    existing = self._discovered.get(existing_key)
            if existing is not None:
                if query and query not in existing.matched_queries:
                    existing.matched_queries.append(query)
                continue
            if query and query not in candidate.matched_queries:
                candidate.matched_queries.append(query)
            self._discovered[key] = candidate
            if url_key:
                self._by_url[url_key] = key
            fresh.append(candidate)
        return fresh

    @property
    def discovered(self) -> list[DiscoveredDouyinVideo]:
        return list(self._discovered.values())

    @property
    def unique_count(self) -> int:
        return len(self._by_id)

    def all(self) -> list[VideoCandidate]:
        return list(self._by_id.values())


class DouyinSearchBackend(ABC):
    """One way of discovering Douyin candidates."""

    name = "base"
    #: When True and this backend is available, its results are used alone
    #: (the dtk keyword API is authoritative when an instance exposes it).
    exclusive: bool = False

    def __init__(
        self,
        client: DouyinBackendClient,
        *,
        platform: str = "douyin",
        page_size: int = 20,
        max_pages: int = 3,
        max_candidates: int = 50,
    ) -> None:
        self.client = client
        self.platform = platform
        self.page_size = max(1, min(page_size, 50))
        self.max_pages = max(1, max_pages)
        self.max_candidates = max(1, max_candidates)

    async def probe(self) -> tuple[bool, str]:
        """Whether this backend can run against the configured service."""

        return True, ""

    def unavailable_reason(self) -> str:
        """Why this backend cannot run right now (empty when it can)."""

        return ""

    @abstractmethod
    async def search(self, query: str, limit: int) -> SearchOutcome:
        """Return up to ``limit`` discovered videos for ``query``."""

    # -- helpers -----------------------------------------------------------
    def _candidates_from_payload(
        self,
        payload: Any,
        *,
        query: str = "",
        extra_metadata: Mapping[str, Any] | None = None,
    ) -> list[DiscoveredDouyinVideo]:
        candidates: list[DiscoveredDouyinVideo] = []
        for content in extract_contents(payload):
            try:
                candidate = content_to_candidate(content, platform=self.platform)
                if not candidate.platform_video_id:
                    continue
                candidates.append(
                    DiscoveredDouyinVideo(
                        platform_video_id=candidate.platform_video_id,
                        source_url=candidate.source_url,
                        search_query=query,
                        visible_title=candidate.title or None,
                        visible_author=candidate.author or None,
                        discovery_backend=self.name,
                        duration=candidate.duration,
                        cover_url=candidate.cover_url,
                        author_id=candidate.author_id,
                        published_at=candidate.published_at,
                        statistics=dict(candidate.statistics),
                        metadata={**dict(candidate.metadata), **dict(extra_metadata or {})},
                        detail=dict(content),
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.warning("could not normalize a Douyin item: %s", exc)
        return candidates

    @staticmethod
    def _pages(payload: Any) -> tuple[list[dict[str, Any]], str | None, bool]:
        items, cursor, has_more = page_items(payload)
        if items:
            return items, cursor, has_more
        return extract_contents(payload), cursor, has_more


class KeywordSearchBackend(DouyinSearchBackend):
    """Uses a keyword-search route **only if the backend exposes one**."""

    name = "keyword"
    exclusive = True

    async def probe(self) -> tuple[bool, str]:
        if not self.client.available:
            return False, f"backend unavailable ({self.client.unavailable_reason})"
        # one cached capability probe per client (never per keyword)
        state = await self.client.probe_capabilities()
        if not state.available:
            return False, f"backend unavailable ({state.status})"
        if state.keyword_search_endpoint:
            return True, f"keyword search available at {state.keyword_search_endpoint}"
        return (
            False,
            "backend exposes no keyword search endpoint "
            "(dtk v5 has none); using supported discovery instead",
        )

    async def search(self, query: str, limit: int) -> SearchOutcome:
        endpoint = await self.client.discover_search_endpoint()
        if not endpoint:
            return SearchOutcome(backend=self.name, exhausted=True, notes=["no keyword endpoint"])
        candidates: list[DiscoveredDouyinVideo] = []
        cursor: str | None = None
        notes: list[str] = []
        for page in range(self.max_pages):
            try:
                result = await self.client.keyword_search(
                    query=query, cursor=cursor, limit=self.page_size
                )
            except DouyinBackendError as exc:
                notes.append(f"page {page + 1} failed: {exc}")
                break
            items, cursor, has_more = self._pages(result.data)
            candidates.extend(self._candidates_from_payload(items, query=query))
            if len(candidates) >= min(limit, self.max_candidates) or not has_more or not cursor:
                break
        return SearchOutcome(
            candidates=candidates[: max(limit, 0)],
            backend=self.name,
            notes=notes,
            exhausted=not cursor,
            cursor=cursor,
        )


class ArchiveSearchBackend(DouyinSearchBackend):
    """Substring search over what the backend instance already collected."""

    name = "archive"

    async def probe(self) -> tuple[bool, str]:
        if not self.client.available:
            return False, f"backend unavailable ({self.client.unavailable_reason})"
        state = await self.client.probe_capabilities()
        if not state.available:
            return False, f"backend unavailable ({state.status})"
        if not state.archive_supported:
            return False, "archive search endpoint not exposed by this backend"
        return True, "archive search ready"

    async def search(self, query: str, limit: int) -> SearchOutcome:
        if not self.client.available:
            # sticky: the backend failed earlier in this task, do not retry
            return SearchOutcome(
                backend=self.name,
                exhausted=True,
                notes=[f"backend unavailable ({self.client.unavailable_reason})"],
                status=BrowserSearchStatus.BACKEND_UNAVAILABLE.value,
                detail=self.client.unavailable_reason,
            )
        candidates: list[DiscoveredDouyinVideo] = []
        cursor: str | None = None
        notes: list[str] = []
        for page in range(self.max_pages):
            try:
                result = await self.client.archive_search(
                    q=query or None, cursor=cursor, limit=self.page_size, platform=self.platform
                )
            except DouyinBackendSchemaError as exc:
                notes.append(f"archive search unsupported: {exc}")
                break
            except DouyinBackendError as exc:
                notes.append(f"archive page {page + 1} failed: {exc}")
                break
            items, cursor, has_more = self._pages(result.data)
            candidates.extend(
                self._candidates_from_payload(items, query=query, extra_metadata={"discovery": "archive"})
            )
            if len(candidates) >= min(limit, self.max_candidates) or not has_more or not cursor:
                break
        return SearchOutcome(
            candidates=candidates[: max(limit, 0)],
            backend=self.name,
            notes=notes,
            exhausted=not cursor,
            cursor=cursor,
        )


class _SeededPostsBackend(DouyinSearchBackend):
    """Shared logic for the two supported "walk a list of posts" backends."""

    name = "seeded"
    #: config key holding the seed ids
    seeds: tuple[str, ...] = ()

    def _keyword_rank(self, candidate: DiscoveredDouyinVideo, query: str) -> tuple[int, int]:
        haystack = " ".join(
            [
                candidate.visible_title or "",
                str(candidate.metadata.get("description") or ""),
                " ".join(str(tag) for tag in candidate.metadata.get("tags") or []),
            ]
        )
        terms = [term for term in query.split() if term] or [query]
        hits = sum(1 for term in terms if term and term in haystack)
        return (1 if hits else 0, hits)

    async def _fetch_page(self, seed: str, cursor: str | None) -> TaskResult:
        raise NotImplementedError

    async def search(self, query: str, limit: int) -> SearchOutcome:
        candidates: list[DiscoveredDouyinVideo] = []
        notes: list[str] = []
        exhausted = True
        for seed in self.seeds:
            if len(candidates) >= limit:
                break
            cursor: str | None = None
            for _page in range(self.max_pages):
                try:
                    result = await self._fetch_page(seed, cursor)
                except DouyinBackendError as exc:
                    notes.append(f"{self.name} seed {seed[:12]} failed: {exc}")
                    break
                items, cursor, has_more = self._pages(result.data)
                candidates.extend(
                    self._candidates_from_payload(
                        items,
                        query=query,
                        extra_metadata={"discovery": self.name, "seed": seed},
                    )
                )
                if (
                    len(candidates) >= min(limit, self.max_candidates)
                    or not has_more
                    or not cursor
                ):
                    break
            if cursor:
                exhausted = False

        # keyword-matching posts first, but keep the rest: a weak title must not
        # disqualify a video that actually contains the material (section 13)
        candidates.sort(key=lambda item: self._keyword_rank(item, query), reverse=True)
        for candidate in candidates:
            candidate.metadata["keyword_match"] = self._keyword_rank(candidate, query)[0] == 1
        return SearchOutcome(
            candidates=candidates[: max(limit, 0)],
            backend=self.name,
            notes=notes,
            exhausted=exhausted,
        )


class AuthorPostsSearchBackend(_SeededPostsBackend):
    """``GET /api/v1/douyin/user/posts`` for configured author seeds."""

    name = "author_posts"

    def __init__(self, client: DouyinBackendClient, *, sec_user_ids: Sequence[str] = (), **kwargs: Any) -> None:
        super().__init__(client, **kwargs)
        self.seeds = tuple(seed for seed in sec_user_ids if seed)

    async def probe(self) -> tuple[bool, str]:
        if not self.seeds:
            return False, "no author seeds configured (sources.douyin.discovery.author_sec_uids)"
        return True, f"{len(self.seeds)} author seed(s)"

    async def _fetch_page(self, seed: str, cursor: str | None) -> TaskResult:
        return await self.client.user_posts(
            sec_user_id=seed, cursor=cursor, count=self.page_size, platform=self.platform
        )


class MixPostsSearchBackend(_SeededPostsBackend):
    """``GET /api/v1/douyin/mix/posts`` for configured mix/playlist seeds."""

    name = "mix_posts"

    def __init__(self, client: DouyinBackendClient, *, mix_ids: Sequence[str] = (), **kwargs: Any) -> None:
        super().__init__(client, **kwargs)
        self.seeds = tuple(seed for seed in mix_ids if seed)

    async def probe(self) -> tuple[bool, str]:
        if not self.seeds:
            return False, "no mix seeds configured (sources.douyin.discovery.mix_ids)"
        return True, f"{len(self.seeds)} mix seed(s)"

    async def _fetch_page(self, seed: str, cursor: str | None) -> TaskResult:
        return await self.client.mix_posts(
            mix_id=seed, cursor=cursor, count=self.page_size, platform=self.platform
        )


class ManualUrlSearchBackend(DouyinSearchBackend):
    """Explicit post URLs (``--douyin-url`` / UI field), resolved through /parse."""

    name = "manual_url"

    def __init__(self, client: DouyinBackendClient, *, urls: Sequence[str] = (), **kwargs: Any) -> None:
        super().__init__(client, **kwargs)
        self.urls = [str(url) for url in urls if url]

    async def probe(self) -> tuple[bool, str]:
        if not self.urls:
            return False, "no manual URLs supplied"
        return True, f"{len(self.urls)} manual URL(s)"

    async def search(self, query: str, limit: int) -> SearchOutcome:
        candidates: list[DiscoveredDouyinVideo] = []
        notes: list[str] = []
        for url in self.urls[: max(limit, 1)]:
            try:
                result = await self.client.parse_url(url)
            except DouyinBackendError as exc:
                notes.append(f"manual URL failed: {exc}")
                continue
            candidates.extend(
                self._candidates_from_payload(
                    result.data, query=query, extra_metadata={"discovery": "manual_url"}
                )
            )
        return SearchOutcome(
            candidates=candidates[: max(limit, 0)], backend=self.name, notes=notes, exhausted=True
        )


class CompositeSearchBackend:
    """Runs the configured backends in priority order and merges the results.

    Priority is deliberate: a real keyword search (if the operator's backend
    has one) beats archive search, which beats walking configured author/mix
    seeds, which beats explicitly supplied URLs.
    """

    name = "composite"

    def __init__(self, backends: Sequence[DouyinSearchBackend]) -> None:
        self.backends = list(backends)

    async def backend_states(self) -> dict[str, str]:
        """Per-backend state for diagnostics: ``{name: state}``.

        States are either ``ready`` or a machine readable blocked/unavailable
        reason such as ``verification_required`` / ``backend_unavailable`` /
        ``no_keyword_endpoint`` / ``not_configured``.
        """

        states: dict[str, str] = {}
        for backend in self.backends:
            reason = backend.unavailable_reason()
            if reason:
                states[backend.name] = reason
                continue
            try:
                available, note = await backend.probe()
            except Exception as exc:  # pragma: no cover - defensive
                states[backend.name] = f"probe_failed: {exc}"
                continue
            states[backend.name] = "ready" if available else self._blocked_label(backend, note)
        return states

    @staticmethod
    def _blocked_label(backend: DouyinSearchBackend, note: str) -> str:
        """Map a human note onto a short machine readable state."""

        lowered = (note or "").lower()
        for status in (
            BrowserSearchStatus.VERIFICATION_REQUIRED.value,
            BrowserSearchStatus.LOGIN_REQUIRED.value,
            BrowserSearchStatus.UPSTREAM_BAD_GATEWAY.value,
            BrowserSearchStatus.UPSTREAM_HTTP_ERROR.value,
            BrowserSearchStatus.BROWSER_UNAVAILABLE.value,
            BrowserSearchStatus.DOUYIN_UNREACHABLE.value,
        ):
            if status in lowered:
                return status
        if "backend unavailable" in lowered:
            return BrowserSearchStatus.BACKEND_UNAVAILABLE.value
        if "no keyword search" in lowered:
            return "no_keyword_endpoint"
        if "not exposed" in lowered:
            return "endpoint_missing"
        if "no author seeds" in lowered or "no mix seeds" in lowered or "no manual urls" in lowered:
            return "not_configured"
        return "unavailable"

    async def has_viable_backend(self) -> tuple[bool, dict[str, str]]:
        states = await self.backend_states()
        return any(state == "ready" for state in states.values()), states

    async def search(self, query: str, limit: int) -> SearchOutcome:
        viable, states = await self.has_viable_backend()
        if not viable:
            detail = "; ".join(f"{name}: {state}" for name, state in states.items())
            raise DiscoveryBlockedError(
                f"no discovery backend can run ({detail})", states=states
            )
        combined: list[DiscoveredDouyinVideo] = []
        notes: list[str] = []
        used: list[str] = []
        exhausted = True
        status = ""
        detail = ""
        for backend in self.backends:
            if len(combined) >= limit:
                exhausted = False
                break
            try:
                available, note = await backend.probe()
            except Exception as exc:  # pragma: no cover - defensive
                notes.append(f"{backend.name}: {exc}")
                continue
            if not available:
                if note:
                    notes.append(f"{backend.name}: {note}")
                continue
            outcome = await backend.search(query, limit - len(combined))
            combined.extend(outcome.candidates)
            notes.extend(f"{backend.name}: {item}" for item in outcome.notes)
            if outcome.status and outcome.status != BrowserSearchStatus.OK and not status:
                # keep the first meaningful state (e.g. the browser wall) so a
                # later fallback note cannot mask it
                status, detail = outcome.status, outcome.detail
            elif outcome.status and outcome.status != BrowserSearchStatus.OK:
                notes.append(f"{backend.name}: {outcome.status}")
            if outcome.candidates:
                used.append(backend.name)
                if not status:
                    status = BrowserSearchStatus.OK
            if getattr(backend, "exclusive", False) and outcome.candidates:
                # an authoritative backend answered: no need to spend requests
                # on the fallback chain
                break
            exhausted = exhausted and outcome.exhausted
        return SearchOutcome(
            candidates=combined,
            backend="+".join(used) or self.name,
            notes=notes,
            exhausted=exhausted,
            status=status,
            detail=detail,
        )

    async def describe(self) -> list[str]:
        """Human readable capability list for ``--check-douyin``."""

        lines: list[str] = []
        for backend in self.backends:
            try:
                available, note = await backend.probe()
            except Exception as exc:  # pragma: no cover - defensive
                lines.append(f"[warn] {backend.name}: {exc}")
                continue
            lines.append(f"[{'ok' if available else 'warn'}] {backend.name}: {note or 'ready'}")
        return lines
