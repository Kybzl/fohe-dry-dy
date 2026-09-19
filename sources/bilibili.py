"""Bilibili adapter placeholder (post Milestone 1)."""

from __future__ import annotations

from core.models import PreviewSource, VideoCandidate, VideoInfo
from sources.base import SourceNotImplementedError, VideoSource


class BilibiliSource(VideoSource):
    platform = "bilibili"

    async def search(self, query: str, limit: int) -> list[VideoCandidate]:
        raise SourceNotImplementedError("BilibiliSource is not implemented yet")

    async def get_video_info(self, video_id: str) -> VideoInfo:
        raise SourceNotImplementedError("BilibiliSource is not implemented yet")

    async def get_preview(self, video_id: str) -> PreviewSource:
        raise SourceNotImplementedError("BilibiliSource is not implemented yet")

    async def get_download_url(self, video_id: str) -> str:
        raise SourceNotImplementedError("BilibiliSource is not implemented yet")
