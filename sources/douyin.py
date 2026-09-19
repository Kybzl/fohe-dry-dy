"""``DouyinSource``: real Douyin acquisition.

Discovery (Milestone 3.5) tries, in order:

1. a dtk keyword-search route, if the connected instance exposes one
2. Playwright browser search over public ``www.douyin.com`` search pages
3. archive / author / mix / manually supplied URLs through dtk

Everything after discovery (metadata, playable media, download) goes through
the separately deployed ``Evil0ctal/Douyin_TikTok_Download_API`` (dtk v5)
backend.  Nothing in ``core/`` knows about Douyin, cookies, signing, the
backend envelope or Playwright.

Preview frames are sampled **straight from the media URL** (section 16/17), so
a candidate that the AI rejects never becomes a full local file.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Sequence

from core.keyword_expander import KeywordExpander
from core.frame_policy import preview_frame_count
from core.models import PreviewFrame, PreviewSource, VideoCandidate, VideoInfo
from media.ffmpeg import MediaToolkit, RemoteMediaError, validate_remote_media_url
from media.frame_sampler import FrameSampler
from sources.base import SourceError, VideoSource
from sources.douyin_backend import (
    BackendCapabilities,
    DouyinBackendClient,
    DouyinBackendError,
)
from sources.douyin_browser_search import DouyinBrowserSearchBackend
from sources.douyin_models import (
    content_to_candidate,
    content_to_video_info,
    extract_contents,
    extract_media_url,
)
from sources.douyin_search import (
    ArchiveSearchBackend,
    AuthorPostsSearchBackend,
    BrowserSearchStatus,
    CompositeSearchBackend,
    DiscoveredDouyinVideo,
    DiscoveryBackend,
    DiscoveryBlockedError,
    DouyinSearchBackend,
    KeywordSearchBackend,
    ManualUrlSearchBackend,
    MixPostsSearchBackend,
    SearchOutcome,
    prioritize_queries,
)

LOGGER = logging.getLogger(__name__)


class DouyinSource(VideoSource):
    """Douyin candidates, metadata, preview frames and media URLs."""

    platform = "douyin"
    search_mode = "keyword"

    def __init__(
        self,
        *,
        client: DouyinBackendClient,
        toolkit: MediaToolkit | None = None,
        search_backends: Sequence[DouyinSearchBackend] | None = None,
        author_sec_uids: Sequence[str] = (),
        mix_ids: Sequence[str] = (),
        manual_urls: Sequence[str] = (),
        archive_search: bool = True,
        enable_remote_preview: bool = True,
        browser_search: DouyinBrowserSearchBackend | None = None,
        enable_browser_search: bool = False,
        preview_dir: Path | None = None,
        preview_frame_count: int = 8,
        preview_max_width: int | None = 640,
        sampling_strategy: str = "uniform",
        adaptive_preview: bool = True,
        preview_frame_bands: Sequence[Any] = (),
        request_timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        super().__init__(request_timeout=request_timeout, max_retries=max_retries)
        self.client = client
        self.toolkit = toolkit
        self.preview_dir = Path(preview_dir) if preview_dir else None
        self.preview_frame_count = max(1, preview_frame_count)
        self.preview_max_width = preview_max_width
        self.sampling_strategy = sampling_strategy
        self.adaptive_preview = bool(adaptive_preview)
        self.preview_frame_bands = tuple(preview_frame_bands)
        self.enable_remote_preview = enable_remote_preview
        # Per-source-instance circuit breaker.  When every remote frame read
        # fails once, later candidates in the same task go straight through
        # the reliable HTTP-download/local-sampling fallback.
        self._remote_preview_healthy = True
        self.browser_search = browser_search
        self.enable_browser_search = bool(enable_browser_search and browser_search is not None)
        self.manual_urls = [str(url) for url in manual_urls if url]

        if search_backends is not None:
            self.backends: list[DouyinSearchBackend] = list(search_backends)
        else:
            configured: list[DouyinSearchBackend] = [KeywordSearchBackend(client)]
            if self.enable_browser_search and self.browser_search is not None:
                configured.append(self.browser_search)
            if archive_search:
                configured.append(ArchiveSearchBackend(client))
            configured.append(AuthorPostsSearchBackend(client, sec_user_ids=list(author_sec_uids)))
            configured.append(MixPostsSearchBackend(client, mix_ids=list(mix_ids)))
            configured.append(ManualUrlSearchBackend(client, urls=self.manual_urls))
            self.backends = configured
        self.search_backend = CompositeSearchBackend(self.backends)
        self._content_cache: dict[str, dict[str, Any]] = {}
        self.last_search_notes: list[str] = []
        self.last_discovery_backend: str = ""
        self.last_search_status: str = ""

    # -- query planning (section 11) ---------------------------------------
    def plan_queries(self, expander: KeywordExpander, material: str) -> list[str]:
        expanded = expander.expand(material)
        prioritized = prioritize_queries(expanded, material=material)
        # manual URLs always come last: they are a debugging aid, not the
        # primary workflow (section 39).
        return prioritized

    # -- VideoSource -------------------------------------------------------
    async def search(self, query: str, limit: int) -> list[VideoCandidate]:
        if not self.client.base_url:
            raise SourceError(
                "Douyin backend base_url is not configured "
                "(sources.douyin.base_url); see docs/douyin_backend.md"
            )
        try:
            outcome: SearchOutcome = await self.search_backend.search(query, limit)
        except DiscoveryBlockedError:
            # every discovery backend is blocked/unavailable: the orchestrator
            # stops the search phase instead of walking the remaining keywords
            raise
        except DouyinBackendError as exc:
            raise SourceError(f"Douyin search failed: {exc}") from exc
        except Exception as exc:  # pragma: no cover - defensive
            raise SourceError(f"Douyin search failed: {exc}") from exc

        self.last_search_notes = list(outcome.notes)
        self.last_discovery_backend = outcome.backend
        self.last_search_status = outcome.status or ""
        for note in outcome.notes:
            LOGGER.debug("douyin search note: %s", note)
        candidates: list[VideoCandidate] = []
        for discovered in outcome.candidates:
            candidate = discovered.to_candidate(platform=self.platform)
            if not candidate.platform_video_id:
                continue
            detail = discovered.detail
            if detail:
                # a discovery backend that already read the post avoids a
                # second dtk round trip for the same video
                self._remember(candidate.platform_video_id, detail)
            candidates.append(candidate)
        # Section 14: the browser only knows ``/video/`` URLs.  Before the local
        # metadata prefilter runs, those candidates need the real dtk metadata
        # (duration, title, author, playable URL) - otherwise every one of them
        # is rejected for an "unknown duration" that dtk could have supplied.
        candidates = await self._enrich_candidates(candidates, query=query)
        LOGGER.info(
            "douyin search %r -> %s candidate(s) via %s%s",
            query,
            len(candidates),
            outcome.backend,
            f" (status={outcome.status})" if outcome.status else "",
        )
        return candidates

    # -- diagnostics -------------------------------------------------------
    async def discovery_state(self) -> dict[str, str]:
        """Per-backend discovery state for CLI/UI reporting (section 4)."""

        states: dict[str, str] = {}
        if isinstance(self.search_backend, CompositeSearchBackend):
            states = await self.search_backend.backend_states()
        states["dtk"] = (
            "ready" if self.client.available else BrowserSearchStatus.BACKEND_UNAVAILABLE.value
        )
        return states

    async def describe_discovery(self) -> list[str]:
        """Report which discovery backend is active right now (section 15)."""

        lines: list[str] = []
        keyword = KeywordSearchBackend(self.client)
        available, note = await keyword.probe()
        lines.append(f"[{'ok' if available else 'warn'}] dtk keyword search: {note}")
        if self.browser_search is not None:
            browser_ok, browser_note = await self.browser_search.probe()
            lines.append(f"[{'ok' if browser_ok else 'warn'}] browser search: {browser_note}")
        else:
            lines.append("[warn] browser search: disabled (sources.douyin.browser_search.enabled)")
        active = (
            DiscoveryBackend.DTK_KEYWORD.value
            if available
            else (
                DiscoveryBackend.BROWSER.value
                if self.enable_browser_search
                else "archive/author/mix/manual"
            )
        )
        lines.append(f"[ok] active discovery backend: {active}")
        return lines

    async def get_video_info(self, video_id: str) -> VideoInfo:
        content = await self._content_for(video_id)
        return content_to_video_info(content, platform=self.platform)

    async def get_preview(self, video_id: str) -> PreviewSource:
        """Sample preview frames from the remote media URL when possible."""

        content = await self._content_for(video_id)
        media_url = extract_media_url(content)
        duration = None
        try:
            info = content_to_video_info(content, platform=self.platform)
            duration = info.duration
        except Exception:  # pragma: no cover - defensive
            duration = None

        frames: list[PreviewFrame] = []
        metadata: dict[str, Any] = {
            "media_url": media_url,
            "discovery": "remote_preview" if media_url else "no_media_url",
        }
        if (
            media_url
            and self.toolkit is not None
            and self.enable_remote_preview
            and self._remote_preview_healthy
        ):
            try:
                remote = validate_remote_media_url(media_url)
            except RemoteMediaError as exc:
                metadata["preview_error"] = str(exc)
            else:
                frame_count = self.preview_frame_count
                if self.adaptive_preview:
                    frame_count = preview_frame_count(
                        duration,
                        bands=self.preview_frame_bands or None,  # type: ignore[arg-type]
                        ceiling=self.preview_frame_count,
                    )
                sampler = FrameSampler(
                    self.toolkit,
                    max_frames=frame_count,
                    max_width=self.preview_max_width,
                    strategy=self.sampling_strategy,
                )
                out_dir = self.preview_dir or Path("previews")
                frames = await sampler.sample_remote(
                    remote,
                    duration or 0.0,
                    frame_count,
                    f"{video_id}_preview",
                    out_dir,
                    max_width=self.preview_max_width,
                )
                if not frames:
                    self._remote_preview_healthy = False
                    metadata["preview_error"] = "remote frame sampling returned no frames"
                    # Some CDNs are downloadable through the HTTP client but
                    # cannot be opened directly by the local FFmpeg build
                    # (proxy/TLS/protocol differences are common).  Never send
                    # an empty preview to the vision model: stage the media and
                    # sample it locally instead.
                    metadata["defer_to_download"] = True
        elif (
            media_url
            and self.toolkit is not None
            and self.enable_remote_preview
            and not self._remote_preview_healthy
        ):
            metadata["preview_error"] = "remote preview bypassed after an earlier failure"
            metadata["defer_to_download"] = True
        elif not self.enable_remote_preview:
            # The operator disabled remote preview: let the orchestrator stage
            # the video first and sample locally instead.
            metadata["defer_to_download"] = True
        elif self.toolkit is None:
            metadata["preview_error"] = "no media toolkit configured"
        else:
            metadata["preview_error"] = "backend supplied no playable media URL"

        return PreviewSource(
            platform=self.platform,
            platform_video_id=video_id,
            duration=duration,
            frames=frames,
            metadata=metadata,
        )

    async def get_download_url(self, video_id: str) -> str:
        """Fresh, validated media URL (signed URLs expire - fetch close to use)."""

        content = await self._content_for(video_id, refresh=True)
        media_url = extract_media_url(content)
        if not media_url:
            raise SourceError(f"backend returned no playable media URL for {video_id}")
        try:
            return validate_remote_media_url(media_url)
        except RemoteMediaError as exc:
            raise SourceError(f"refused media URL for {video_id}: {exc}") from exc

    # -- extra capabilities ------------------------------------------------
    async def health(self, *, deep: bool = False) -> BackendCapabilities:
        return await self.client.health(deep=deep)

    async def describe_backends(self) -> list[str]:
        return await self.search_backend.describe()

    def provenance_for(self, video_id: str) -> dict[str, Any]:
        """Attribution data for the clip rows (section 24/48)."""

        content = self._content_cache.get(video_id) or {}
        if not content:
            return {}
        try:
            candidate = content_to_candidate(content, platform=self.platform)
        except Exception:  # pragma: no cover - defensive
            return {}
        return {
            "source_title": candidate.title,
            "source_author": candidate.author,
            "source_author_id": candidate.author_id,
            "source_publish_time": candidate.published_at.isoformat()
            if candidate.published_at
            else None,
            "statistics": candidate.statistics,
        }

    async def aclose(self) -> None:
        if self.browser_search is not None and not getattr(
            self.browser_search, "shared", False
        ):
            # Milestone 8.3: a *shared* browser session (the operator's own
            # Chrome, verified once and reused by a whole plan run) belongs to
            # the caller - the task must not detach it
            try:
                await self.browser_search.close()
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.debug("closing browser search failed: %s", exc)
        await self.client.aclose()

    # -- internals ---------------------------------------------------------
    async def _content_for(self, video_id: str, *, refresh: bool = False) -> dict[str, Any]:
        """Fetch (and cache) one post's normalized content payload."""

        if not refresh:
            cached = self._content_cache.get(video_id)
            if cached:
                return cached
        try:
            result = await self.client.content_detail(aweme_id=video_id)
        except DouyinBackendError as exc:
            raise SourceError(f"Douyin metadata failed for {video_id}: {exc}") from exc
        contents = extract_contents(result.data)
        if not contents:
            raise SourceError(f"backend returned no content for {video_id}")
        content = contents[0]
        self._content_cache[video_id] = content
        return content

    def _remember(self, video_id: str, content: dict[str, Any] | None) -> None:
        if content:
            self._content_cache[video_id] = content

    async def _enrich_candidates(
        self,
        candidates: Sequence[VideoCandidate],
        *,
        query: str,
    ) -> list[VideoCandidate]:
        """Fill dtk metadata for candidates discovered without it (section 14).

        Bounded and failure tolerant: a video whose metadata cannot be read is
        returned unchanged, so the local prefilter still decides about it.
        """

        if not candidates or not self.client.base_url:
            return list(candidates)
        enriched: list[VideoCandidate] = []
        for candidate in candidates:
            if candidate.duration is not None and candidate.title:
                enriched.append(candidate)
                continue
            try:
                content = await self._content_for(candidate.platform_video_id)
            except Exception as exc:
                LOGGER.debug(
                    "metadata enrichment failed for %s: %s",
                    candidate.platform_video_id,
                    exc,
                )
                enriched.append(candidate)
                continue
            try:
                detail = content_to_candidate(
                    content, query=query, platform=self.platform
                )
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.debug(
                    "metadata payload unusable for %s: %s",
                    candidate.platform_video_id,
                    exc,
                )
                enriched.append(candidate)
                continue
            metadata = dict(candidate.metadata)
            metadata.setdefault("dtk_detail", content)
            metadata.setdefault("discovery", candidate.metadata.get("discovery") or "browser")
            enriched.append(
                candidate.model_copy(
                    update={
                        "title": candidate.title or detail.title,
                        "author": candidate.author or detail.author,
                        "author_id": candidate.author_id or detail.author_id,
                        "duration": candidate.duration or detail.duration,
                        "cover_url": candidate.cover_url or detail.cover_url,
                        "published_at": candidate.published_at or detail.published_at,
                        "statistics": candidate.statistics or detail.statistics,
                        "media_url": candidate.media_url or detail.media_url,
                        "metadata": metadata,
                    }
                )
            )
        return enriched
