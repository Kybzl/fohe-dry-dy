"""The platform agnostic ``VideoSource`` contract.

Every platform (Douyin, Bilibili, TikTok, Xiaohongshu ...) must implement this
interface.  Core business code depends on this module only -- never on a
concrete platform adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.models import PreviewSource, VideoCandidate, VideoInfo


class SourceError(RuntimeError):
    """Raised when a platform call fails (network, parsing, rate limit...)."""


class SourceNotImplementedError(SourceError, NotImplementedError):
    """Raised by placeholder adapters that are scheduled for a later milestone."""


class VideoSource(ABC):
    """Unified search / metadata / download-URL interface."""

    #: short platform identifier, e.g. ``"douyin"``
    platform: str = "unknown"
    #: ``"keyword"`` = one search per generated keyword (Douyin, TikTok ...);
    #: ``"collection"`` = the source already holds the full candidate set and
    #: is queried once (local files).
    search_mode: str = "keyword"

    def __init__(self, *, request_timeout: float = 20.0, max_retries: int = 3) -> None:
        self.request_timeout = request_timeout
        self.max_retries = max_retries

    @abstractmethod
    async def search(self, query: str, limit: int) -> list[VideoCandidate]:
        """Search the platform and return lightweight candidates."""

    @abstractmethod
    async def get_video_info(self, video_id: str) -> VideoInfo:
        """Return stored metadata for a single video."""

    @abstractmethod
    async def get_preview(self, video_id: str) -> PreviewSource:
        """Return cheap preview stills used by the AI pre-filter.

        Called *before* the full video is downloaded.
        """

    @abstractmethod
    async def get_download_url(self, video_id: str) -> str:
        """Return a direct, time limited media URL for the video stream."""

    def plan_queries(self, expander: "KeywordExpander", material: str) -> list[str]:
        """Turn a material into the ordered search queries.

        The default keeps the Milestone 1 behaviour; adapters may reorder the
        terms (Douyin runs the most specific phrases first, section 11).
        """

        from core.keyword_expander import KeywordExpander as _KeywordExpander

        if not isinstance(expander, _KeywordExpander):  # pragma: no cover - defensive
            raise TypeError("plan_queries expects a KeywordExpander")
        return expander.expand(material)

    async def close(self) -> None:
        """Release transport resources.  Optional."""

    async def __aenter__(self) -> VideoSource:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<{type(self).__name__} platform={self.platform}>"
