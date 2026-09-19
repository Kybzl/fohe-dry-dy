"""Deterministic mock platform source.

``MockVideoSource`` behaves like a real ``VideoSource``: it searches, returns
metadata, produces preview stills and hands out a download URL.  Everything is
derived from a seed plus the query string, so a test or a demo run always sees
the same candidates while still exercising every rejection path:

* ambiguous / irrelevant titles
* too long or too short videos
* duplicated URLs and duplicated platform video ids
* videos whose subtitle layout must be rejected by the AI pre-filter

Each candidate carries ``metadata["mock_scenario"]``; the mock vision provider
uses it to produce realistic verdicts.  Real adapters simply will not set it.
"""

from __future__ import annotations

import hashlib
import logging
import random
from pathlib import Path

from core.models import PreviewFrame, PreviewSource, VideoCandidate, VideoInfo
from media.placeholder import write_placeholder_jpeg
from sources.base import SourceError, VideoSource

LOGGER = logging.getLogger(__name__)

# Scenario vocabulary shared with ``ai/mock.py`` (kept as plain strings so the
# two mock modules stay independent and the adapter contract stays generic).
SCENARIO_CLEAN = "clean"
SCENARIO_BORDERLINE_SUBTITLE = "borderline_subtitle"
SCENARIO_MULTI_REGION_SUBTITLE = "multi_region_subtitle"
SCENARIO_COLORED_TEXT_BLOCK = "colored_text_block"
SCENARIO_SPLIT_SCREEN = "split_screen"
SCENARIO_LOW_QUALITY = "low_quality"
SCENARIO_NO_MATERIAL = "no_material"
SCENARIO_NO_SEGMENT = "no_usable_segment"
SCENARIO_DUPLICATE_URL = "duplicate_url"
SCENARIO_DUPLICATE_VIDEO = "duplicate_video"
SCENARIO_DURATION_OUT_OF_RANGE = "duration_out_of_range"
SCENARIO_IRRELEVANT_TITLE = "irrelevant_title"

#: Cycled across the search result list, so one search page contains usable
#: material plus one of each rejection path.  Index 0 stays "clean" so a demo
#: run always starts from usable footage.
SCENARIO_CYCLE: tuple[str, ...] = (
    SCENARIO_CLEAN,                  # 0 - usable
    SCENARIO_MULTI_REGION_SUBTITLE,  # 1 - AI rejects: subtitles
    SCENARIO_CLEAN,                  # 2 - usable
    SCENARIO_BORDERLINE_SUBTITLE,    # 3 - strict policy rejects: subtitles
    SCENARIO_DUPLICATE_URL,          # 4 - local filter: duplicate url
    SCENARIO_CLEAN,                  # 5 - usable
    SCENARIO_SPLIT_SCREEN,           # 6 - AI rejects: split screen
    SCENARIO_DURATION_OUT_OF_RANGE,  # 7 - local filter: bad duration
    SCENARIO_CLEAN,                  # 8 - usable
    SCENARIO_LOW_QUALITY,            # 9 - AI rejects: low quality
    SCENARIO_CLEAN,                  # 10 - usable
    SCENARIO_NO_MATERIAL,            # 11 - AI rejects: no material
    SCENARIO_CLEAN,                  # 12 - usable
    SCENARIO_IRRELEVANT_TITLE,       # 13 - local filter: unrelated title
    SCENARIO_CLEAN,                  # 14 - usable
    SCENARIO_COLORED_TEXT_BLOCK,     # 15 - AI rejects: text block
)

TITLE_TEMPLATES: dict[str, tuple[str, ...]] = {
    SCENARIO_CLEAN: (
        "{query}全过程实拍，工人师傅操作细节",
        "{material}烘干现场，托盘摆放整齐",
        "热泵烘干房里的{material}，成品颜色漂亮",
        "{material}从原料到成品的完整记录",
        "烘干设备运行实拍：{query}",
    ),
    SCENARIO_BORDERLINE_SUBTITLE: (
        "{query}教程，带字幕讲解流程",
        "{material}烘干经验分享，字幕版",
    ),
    SCENARIO_MULTI_REGION_SUBTITLE: (
        "【速看】{query}，上中下三处字幕说明",
        "{material}烘干价格表与流程说明",
    ),
    SCENARIO_COLORED_TEXT_BLOCK: (
        "红黄大字：{query}限时优惠",
        "满屏弹幕式文字介绍{material}烘干",
    ),
    SCENARIO_SPLIT_SCREEN: (
        "分屏对比：{material}烘干前后",
        "画中画讲解{query}",
    ),
    SCENARIO_LOW_QUALITY: (
        "手机随手拍 {material}烘干（很糊）",
        "抖动严重：{query}记录",
    ),
    SCENARIO_NO_MATERIAL: (
        "烘干房设备介绍（不含{material}）",
        "厂区外景航拍",
    ),
    SCENARIO_NO_SEGMENT: (
        "{material}烘干，全程只有主播说话",
    ),
    SCENARIO_DUPLICATE_URL: (
        "{query}实拍（重复来源）",
    ),
    SCENARIO_DUPLICATE_VIDEO: (
        "{query}实拍（同一视频再次出现）",
    ),
    SCENARIO_DURATION_OUT_OF_RANGE: (
        "{material}烘干超长合集",
        "{query}十秒速览",
    ),
    SCENARIO_IRRELEVANT_TITLE: (
        "手机支架开箱与使用评测",
        "汽车内饰清洁小技巧",
    ),
}

AUTHORS = (
    "烘干设备老张",
    "农产品加工日记",
    "热泵烘干机厂家",
    "乡村致富经",
    "食品机械小课堂",
)


class MockVideoSource(VideoSource):
    """Offline, deterministic stand-in for a real platform adapter."""

    platform = "douyin"

    def __init__(
        self,
        *,
        seed: int = 20260914,
        preview_dir: Path | None = None,
        preview_frame_count: int = 4,
        write_placeholder_media: bool = True,
        request_timeout: float = 20.0,
        max_retries: int = 3,
    ) -> None:
        super().__init__(request_timeout=request_timeout, max_retries=max_retries)
        self.seed = seed
        self.preview_dir = preview_dir
        self.preview_frame_count = max(1, preview_frame_count)
        self.write_placeholder_media = write_placeholder_media
        self._known: dict[str, VideoCandidate] = {}

    # -- VideoSource -------------------------------------------------------
    async def search(self, query: str, limit: int) -> list[VideoCandidate]:
        if limit <= 0:
            return []
        candidates: list[VideoCandidate] = []
        previous: VideoCandidate | None = None

        for index in range(limit):
            scenario = SCENARIO_CYCLE[index % len(SCENARIO_CYCLE)]
            rng = random.Random(self._rng_seed(query, index, "candidate"))
            video_id = self._video_id(query, index)
            duration = self._duration_for(scenario, rng)
            title = rng.choice(TITLE_TEMPLATES[scenario]).format(
                query=query, material=self._material_from_query(query)
            )
            author = rng.choice(AUTHORS)
            url = f"https://www.douyin.com/video/{video_id}"

            if scenario == SCENARIO_DUPLICATE_URL and previous is not None:
                url = previous.source_url
            if scenario == SCENARIO_DUPLICATE_VIDEO and previous is not None:
                video_id = previous.platform_video_id
                url = previous.source_url

            candidate = VideoCandidate(
                platform=self.platform,
                platform_video_id=video_id,
                source_url=url,
                title=title,
                author=author,
                duration=duration,
                cover_url=f"https://mock.local/cover/{video_id}.jpg",
                metadata={
                    "source_adapter": "mock",
                    "mock_scenario": scenario,
                    "mock_index": index,
                    "query": query,
                    "stats": {
                        "digg_count": rng.randint(120, 90000),
                        "comment_count": rng.randint(5, 3000),
                    },
                },
            )
            candidates.append(candidate)
            self._known[candidate.platform_video_id] = candidate
            previous = candidate

        LOGGER.debug("mock search %r -> %s candidates", query, len(candidates))
        return candidates

    async def get_video_info(self, video_id: str) -> VideoInfo:
        candidate = self._known.get(video_id)
        if candidate is None:
            candidate = self._synthesize(video_id)
        rng = random.Random(self._rng_seed(video_id, 0, "info"))
        height = rng.choice([1920, 1080, 1280])
        width = rng.choice([1080, 720, 1920])
        return VideoInfo(
            platform=self.platform,
            platform_video_id=video_id,
            source_url=candidate.source_url,
            title=candidate.title,
            author=candidate.author,
            description=f"{candidate.title} #烘干 #加工 #{self._material_from_query(candidate.title)}",
            duration=candidate.duration or 45.0,
            width=width,
            height=height,
            fps=rng.choice([25.0, 30.0, 60.0]),
            cover_url=candidate.cover_url,
            metadata=dict(candidate.metadata),
        )

    async def get_preview(self, video_id: str) -> PreviewSource:
        candidate = self._known.get(video_id)
        if candidate is None:
            candidate = self._synthesize(video_id)
        duration = float(candidate.duration or 45.0)
        frames: list[PreviewFrame] = []
        for index in range(self.preview_frame_count):
            ratio = (index + 0.5) / self.preview_frame_count
            timestamp = round(ratio * duration, 2)
            path = self._preview_path(video_id, index, candidate)
            frames.append(PreviewFrame(timestamp=timestamp, image_path=path, source="preview"))
        return PreviewSource(
            platform=self.platform,
            platform_video_id=video_id,
            duration=duration,
            frames=frames,
            metadata=dict(candidate.metadata),
        )

    async def get_download_url(self, video_id: str) -> str:
        if not video_id:
            raise SourceError("video_id must not be empty")
        return f"https://mock.local/media/{self.platform}/{video_id}.mp4"

    # -- helpers -----------------------------------------------------------
    def _preview_path(self, video_id: str, index: int, candidate: VideoCandidate) -> Path:
        if self.preview_dir is None:
            return Path(f"{video_id}_preview_{index}.jpg")
        scenario = str(candidate.metadata.get("mock_scenario", SCENARIO_CLEAN))
        path = self.preview_dir / f"{video_id}_preview_{index}.jpg"
        if self.write_placeholder_media:
            signature = f"{video_id}|{scenario}|{index}".encode("utf-8")
            write_placeholder_jpeg(path, signature)
        return path

    def _video_id(self, query: str, index: int) -> str:
        digest = hashlib.sha1(f"{self.seed}|{query}|{index}".encode("utf-8")).hexdigest()
        return f"dy{digest[:14]}"

    def _rng_seed(self, *parts: object) -> int:
        joined = "|".join(str(part) for part in (self.seed, *parts))
        return int(hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12], 16)

    @staticmethod
    def _duration_for(scenario: str, rng: random.Random) -> float:
        if scenario == SCENARIO_DURATION_OUT_OF_RANGE:
            return float(rng.choice([900, 4]))
        if scenario == SCENARIO_LOW_QUALITY:
            return round(rng.uniform(12, 40), 1)
        return round(rng.uniform(24, 96), 1)

    @staticmethod
    def _material_from_query(query: str) -> str:
        stripped = query
        for suffix in ("烘干机", "烘干房", "热泵烘干", "烘干", "干制作", "干加工", "干生产", "制作", "加工", "生产", "干燥", "干", "片", "粉"):
            if stripped.endswith(suffix) and len(stripped) > len(suffix):
                stripped = stripped[: -len(suffix)]
                break
        return stripped or query

    def _synthesize(self, video_id: str) -> VideoCandidate:
        """Reconstruct a plausible candidate for an unknown mock video id."""

        rng = random.Random(self._rng_seed(video_id, "synthetic"))
        scenario = SCENARIO_CYCLE[rng.randrange(len(SCENARIO_CYCLE))]
        LOGGER.debug("mock source: synthesised metadata for unknown id %s", video_id)
        candidate = VideoCandidate(
            platform=self.platform,
            platform_video_id=video_id,
            source_url=f"https://www.douyin.com/video/{video_id}",
            title="mock video",
            author=rng.choice(AUTHORS),
            duration=round(rng.uniform(24, 96), 1),
            metadata={"source_adapter": "mock", "mock_scenario": scenario, "synthetic": True},
        )
        self._known[video_id] = candidate
        return candidate
