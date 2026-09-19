"""Normalization of the backend's ``Content`` payload into our domain models.

Field names follow the upstream ``dtk`` content model (v5.0.3):

``Content{platform, content_id, kind, web_url, title, description, created_at,
duration_ms, is_deleted, is_private, author{uid, sec_uid, nickname, unique_id,
avatar, verified, stats}, stats{play_count, digg_count, ...},
media{covers[], video{url, urls, width, height, bitrate, size_bytes, watermark},
streams[], images[]}, music, tags, location}``

Keeping the mapping in one module means the rest of the application never sees
a raw provider structure (section 8).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from core.models import VideoCandidate, VideoInfo

LOGGER = logging.getLogger(__name__)

#: media kinds that cannot produce video clips
NON_VIDEO_KINDS = frozenset({"image", "images", "album", "photo", "note"})


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp or a unix epoch (seconds/ms)."""

    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:  # milliseconds
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def content_id_of(content: Mapping[str, Any]) -> str:
    payload = _as_mapping(content)
    return str(payload.get("content_id") or payload.get("aweme_id") or "").strip()


def content_kind(content: Mapping[str, Any]) -> str:
    payload = _as_mapping(content)
    kind = str(payload.get("kind") or "").strip().lower()
    if kind:
        return kind
    media = _as_mapping(payload.get("media"))
    if _as_list(media.get("images")) and not _stream_urls(media):
        return "images"
    return "video"


def is_video(content: Mapping[str, Any]) -> bool:
    return content_kind(content) not in NON_VIDEO_KINDS


def duration_seconds(content: Mapping[str, Any]) -> float | None:
    payload = _as_mapping(content)
    milliseconds = payload.get("duration_ms")
    if milliseconds in (None, ""):
        seconds = payload.get("duration")
        try:
            return float(seconds) if seconds not in (None, "") else None
        except (TypeError, ValueError):
            return None
    try:
        return round(float(milliseconds) / 1000.0, 3)
    except (TypeError, ValueError):
        return None


def _stream_entry_urls(entry: Mapping[str, Any]) -> list[str]:
    urls: list[str] = []
    single = entry.get("url")
    if isinstance(single, str) and single:
        urls.append(single)
    for item in _as_list(entry.get("urls")):
        if isinstance(item, str) and item:
            urls.append(item)
        elif isinstance(item, Mapping) and item.get("url"):
            urls.append(str(item["url"]))
    return urls


def _stream_urls(media: Mapping[str, Any]) -> list[str]:
    candidates: list[str] = []
    video = _as_mapping(media.get("video"))
    candidates.extend(_stream_entry_urls(video))
    for stream in _as_list(media.get("streams")):
        candidates.extend(_stream_entry_urls(_as_mapping(stream)))
    return candidates


def media_streams(content: Mapping[str, Any]) -> list[dict[str, Any]]:
    """All playable streams, best first (non-watermark, higher bitrate wins)."""

    media = _as_mapping(_as_mapping(content).get("media"))
    entries: list[dict[str, Any]] = []
    video = _as_mapping(media.get("video"))
    if video:
        entries.append(dict(video))
    entries.extend(dict(_as_mapping(stream)) for stream in _as_list(media.get("streams")))

    expanded: list[dict[str, Any]] = []
    for entry in entries:
        urls = _stream_entry_urls(entry)
        if not urls:
            continue
        for index, url in enumerate(urls):
            expanded.append({**entry, "url": url, "_order": index, "urls": urls})

    def score(entry: Mapping[str, Any]) -> tuple[int, float, int, int]:
        watermark = 1 if entry.get("watermark") else 0
        try:
            bitrate = float(entry.get("bitrate") or 0)
        except (TypeError, ValueError):
            bitrate = 0.0
        try:
            size = int(entry.get("size_bytes") or 0)
        except (TypeError, ValueError):
            size = 0
        # prefer: no watermark > higher bitrate > bigger file > first listed
        return (-watermark, bitrate, size, -int(entry.get("_order") or 0))

    expanded.sort(key=score, reverse=True)
    return expanded


def extract_media_url(content: Mapping[str, Any]) -> str | None:
    """Best playable/downloadable media URL, or ``None`` for image albums."""

    streams = media_streams(content)
    for entry in streams:
        url = entry.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
    return None


def extract_cover_url(content: Mapping[str, Any]) -> str | None:
    media = _as_mapping(_as_mapping(content).get("media"))
    for cover in _as_list(media.get("covers")):
        entry = _as_mapping(cover)
        url = entry.get("url") or (_as_list(entry.get("urls")) or [None])[0]
        if isinstance(url, str) and url:
            return url
    return None


def extract_author(content: Mapping[str, Any]) -> tuple[str, str | None, str | None]:
    """``(nickname, author_id, avatar)``."""

    author = _as_mapping(_as_mapping(content).get("author"))
    nickname = str(author.get("nickname") or author.get("unique_id") or "")
    author_id = author.get("sec_uid") or author.get("uid")
    avatar = author.get("avatar")
    return nickname, (str(author_id) if author_id else None), (
        str(avatar) if isinstance(avatar, str) else None
    )


def extract_stats(content: Mapping[str, Any]) -> dict[str, Any]:
    stats = _as_mapping(_as_mapping(content).get("stats"))
    return {key: value for key, value in stats.items() if value is not None}


def extract_tags(content: Mapping[str, Any]) -> list[str]:
    tags: list[str] = []
    for tag in _as_list(_as_mapping(content).get("tags")):
        if isinstance(tag, str):
            tags.append(tag)
        elif isinstance(tag, Mapping) and tag.get("name"):
            tags.append(str(tag["name"]))
    return tags


def content_to_candidate(
    content: Mapping[str, Any],
    *,
    query: str | None = None,
    platform: str = "douyin",
    extra_metadata: Mapping[str, Any] | None = None,
) -> VideoCandidate:
    """Build a normalized ``VideoCandidate`` from one backend content item."""

    payload = _as_mapping(content)
    content_id = content_id_of(payload)
    author, author_id, avatar = extract_author(payload)
    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip()
    media_url = extract_media_url(payload)
    metadata: dict[str, Any] = {
        "kind": content_kind(payload),
        "description": description,
        "tags": extract_tags(payload),
        "is_deleted": bool(payload.get("is_deleted")),
        "is_private": bool(payload.get("is_private")),
        "fetched_at": payload.get("fetched_at"),
        "author_avatar": avatar,
    }
    if query:
        metadata["query"] = query
    if extra_metadata:
        metadata.update(dict(extra_metadata))
    if media_url:
        metadata["media_url"] = media_url
    if not is_video(payload):
        metadata["no_video"] = True

    return VideoCandidate(
        platform=platform,
        platform_video_id=content_id,
        source_url=str(payload.get("web_url") or f"https://www.douyin.com/video/{content_id}"),
        title=title or description[:60],
        author=author,
        author_id=author_id,
        duration=duration_seconds(payload),
        cover_url=extract_cover_url(payload),
        published_at=parse_datetime(payload.get("created_at")),
        media_url=media_url,
        statistics=extract_stats(payload),
        matched_queries=[query] if query else [],
        metadata=metadata,
    )


def content_to_video_info(
    content: Mapping[str, Any],
    *,
    platform: str = "douyin",
) -> VideoInfo:
    """Build a ``VideoInfo`` from one backend content item."""

    payload = _as_mapping(content)
    streams = media_streams(payload)
    best = streams[0] if streams else {}
    author, author_id, _avatar = extract_author(payload)
    content_id = content_id_of(payload)
    return VideoInfo(
        platform=platform,
        platform_video_id=content_id,
        source_url=str(payload.get("web_url") or f"https://www.douyin.com/video/{content_id}"),
        title=str(payload.get("title") or ""),
        author=author,
        description=str(payload.get("description") or ""),
        duration=duration_seconds(payload),
        width=int(best["width"]) if best.get("width") else None,
        height=int(best["height"]) if best.get("height") else None,
        fps=None,
        cover_url=extract_cover_url(payload),
        metadata={
            "kind": content_kind(payload),
            "author_id": author_id,
            "publish_time": payload.get("created_at"),
            "statistics": extract_stats(payload),
            "tags": extract_tags(payload),
            "media_url": extract_media_url(payload),
            "stream_count": len(streams),
        },
    )


def page_items(payload: Any) -> tuple[list[dict[str, Any]], str | None, bool]:
    """Read a ``Page{items, cursor, has_more}`` payload defensively.

    A bare list is accepted too: a backend build that returns ``[...]`` instead
    of the documented page object still works, we just stop paging.
    """

    if isinstance(payload, Mapping):
        items = [item for item in _as_list(payload.get("items")) if isinstance(item, Mapping)]
        cursor = payload.get("cursor")
        has_more = bool(payload.get("has_more", False))
        return [dict(item) for item in items], (str(cursor) if cursor else None), has_more
    if isinstance(payload, list):
        items = [dict(item) for item in payload if isinstance(item, Mapping)]
        return items, None, False
    return [], None, False


def extract_contents(payload: Any) -> list[dict[str, Any]]:
    """Pull content items out of any documented read shape.

    Handles a single ``Content`` object, a ``Page`` and a plain list, because
    callers should not have to care which endpoint produced the payload.
    """

    if isinstance(payload, Mapping):
        if "content_id" in payload or "aweme_id" in payload:
            return [dict(payload)]
        if "items" in payload:
            items, _cursor, _has_more = page_items(payload)
            return items
        for key in ("contents", "data", "posts", "list"):
            nested = payload.get(key)
            if nested is not None:
                return extract_contents(nested)
    if isinstance(payload, Iterable) and not isinstance(payload, (str, bytes)):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    return []
