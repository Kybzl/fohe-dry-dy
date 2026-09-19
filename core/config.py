"""Configuration loading and logging setup.

``config.yaml`` holds non-secret runtime settings.  Secrets (API keys) are
read from the environment / ``.env`` only, and are never logged.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from core.subtitle_cleanup_models import SubtitleCleanupConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

LOGGER = logging.getLogger(__name__)

_SECRET_ENV_KEYS = (
    "QWEN_API_KEY",
    "VOLCANO_API_KEY",
    "DOUYIN_COOKIE",
    "DOUYIN_USER_AGENT",
)

#: Default root of the material library (clip files and thumbnails).
DEFAULT_LIBRARY_ROOT = Path("D:/素材库2")


def save_cloud_cleanup_limits(
    config_path: Path,
    *,
    per_run: int,
    per_day: int,
) -> None:
    """Atomically update only the two cloud paid-task limits in YAML."""

    path = Path(config_path).resolve()
    text = path.read_text(encoding="utf-8")
    values = {
        "max_paid_tasks_per_run": max(1, min(100, int(per_run))),
        "max_paid_tasks_per_day": max(1, min(1000, int(per_day))),
    }
    for key, value in values.items():
        pattern = re.compile(rf"^(\s*{re.escape(key)}\s*:\s*)\d+(.*)$", re.MULTILINE)
        text, count = pattern.subn(rf"\g<1>{value}\g<2>", text, count=1)
        if count != 1:
            raise ValueError(f"missing cloud cleanup setting: {key}")
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def is_absolute_path(value: Path | str) -> bool:
    """True for POSIX absolute paths and Windows drive/UNC paths.

    Implemented with ``pathlib`` only: separating the flavours means a config
    such as ``D:/素材库2`` stays absolute even when the code is executed on a
    platform whose native ``Path`` would treat it as relative (and vice versa).
    """

    text = str(value).strip()
    if not text:
        return False
    if PureWindowsPath(text).is_absolute():
        return True
    return PurePosixPath(text).is_absolute()


class AppMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = "工业烘干短视频素材库 Agent"
    log_level: str = "INFO"


class PathSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    data_dir: Path = Path("data")
    cache_dir: Path = Path("cache")
    library_root: Path = DEFAULT_LIBRARY_ROOT
    log_dir: Path = Path("logs")
    database: Path = Path("data/library.db")

    def resolved(self, base: Path) -> PathSettings:
        """Anchor every relative path to ``base``; absolute paths pass through."""

        def _abs(value: Path) -> Path:
            return value if is_absolute_path(value) else (base / value)

        return PathSettings(
            data_dir=_abs(self.data_dir),
            cache_dir=_abs(self.cache_dir),
            library_root=_abs(self.library_root),
            log_dir=_abs(self.log_dir),
            database=_abs(self.database),
        )

    def ensure_directories(self) -> None:
        for directory in (self.data_dir, self.cache_dir, self.library_root, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)


class StorageSettings(BaseModel):
    """Milestone 2 canonical storage layout (``storage:`` in config.yaml)."""

    model_config = ConfigDict(extra="ignore")
    library_root: Path = Path("D:/素材库2")
    database_path: Path = Path("data/library.db")
    cache_root: Path = Path("cache")


class LocalSourceSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    extensions: list[str] = Field(default_factory=lambda: [".mp4", ".mov", ".m4v"])
    recursive: bool = False
    copy_to_cache: bool = True

    def normalised_extensions(self) -> tuple[str, ...]:
        return tuple(
            (extension if extension.startswith(".") else f".{extension}").lower()
            for extension in self.extensions
        )


class DouyinDiscoverySettings(BaseModel):
    """Supported discovery sources used when the backend has no keyword search.

    The upstream backend (dtk v5) exposes content/user/mix/archive reads but no
    platform keyword search; these seeds are the documented alternatives.
    """

    model_config = ConfigDict(extra="ignore")
    #: author ``sec_user_id`` seeds -> GET /api/v1/douyin/user/posts
    author_sec_uids: list[str] = Field(default_factory=list)
    #: mix/playlist ids -> GET /api/v1/douyin/mix/posts
    mix_ids: list[str] = Field(default_factory=list)
    #: search the backend's own archive -> GET /api/v1/archive?q=
    archive_search: bool = True
    #: manually supplied post URLs
    manual_urls: list[str] = Field(default_factory=list)


class DouyinBrowserSearchSettings(BaseModel):
    """Playwright discovery settings (Milestone 3.5, section 3/9/18)."""

    model_config = ConfigDict(extra="ignore")
    enabled: bool = True
    profile_dir: Path = Path("browser_data/douyin")
    headless: bool = False
    #: ``auto`` (installed Chrome/Edge, else bundled), ``chromium``, ``chrome``
    #: or ``msedge`` -- the operator's normal browser sessions are often more
    #: consistent with the persistent profile than the bundled Chromium
    browser_channel: str = "auto"
    #: optional explicit executable; wins when it exists
    browser_executable_path: str = ""
    #: Milestone 8.3 (V3.2 session model): when set (e.g.
    #: ``http://127.0.0.1:9222``) the backend attaches to an operator-launched
    #: Chrome/Edge over CDP instead of launching its own browser.  Douyin
    #: serves an untrusted empty shell to automation-flagged browsers, so a
    #: normally launched browser (same profile, operator logs in) is the
    #: reliable path.  Empty = auto (attach when ``cdp_port`` answers,
    #: otherwise launch our own browser); ``off`` forces our own browser.
    cdp_url: str = ""
    #: port used by ``--open-douyin-browser`` when starting that browser
    cdp_port: int = 9222
    locale: str = "zh-CN"
    viewport_width: int = 1440
    viewport_height: int = 900
    slow_mo_ms: int = 0
    max_scrolls_per_query: int = 8
    scroll_delay_seconds: float = 1.0
    max_results_per_query: int = 50
    navigation_timeout_seconds: float = 30.0
    page_settle_seconds: float = 6.0
    #: reuse one browser window across the queries of a task
    keep_context_open: bool = True
    #: leave the window open when Douyin asks for manual verification
    keep_open_on_challenge: bool = True
    #: keep the live task paused while the operator clears a rendered wall,
    #: then retry the same query automatically (never solves the challenge)
    auto_resume_after_verification: bool = True
    challenge_wait_timeout_seconds: float = 300.0
    challenge_poll_seconds: float = 4.0
    #: after N consecutive successful-but-empty searches the browser is skipped
    #: for the rest of the task (a yield guard, never a "blocked" state)
    empty_result_limit: int = 3
    #: bounded retries for transient upstream/gateway failures (502/503/504)
    upstream_retry_count: int = 2
    upstream_retry_backoff_seconds: float = 2.0
    #: Milestone 8.2: bounded machine settling after the rendered challenge
    #: clears (Douyin hydrates / SPA-routes the search page).  This is page
    #: settling, never a human-verification timeout.
    search_settle_timeout_seconds: float = 20.0
    search_settle_poll_seconds: float = 1.0


class DouyinSettings(BaseModel):
    """Real Douyin acquisition through a self-hosted backend (dtk v5)."""

    model_config = ConfigDict(extra="ignore")
    backend: str = "dtk"
    base_url: str = "http://127.0.0.1:8000"
    #: Backends tried, in order, when the configured ``base_url`` is not
    #: reachable/authorized (Milestone 3.6, section 3).  Add a working remote
    #: dtk instance here (or set ``DOUYIN_BACKEND_BASE_URL``) instead of
    #: assuming a local one is running.
    fallback_base_urls: list[str] = Field(default_factory=list)
    api_key_env: str = "DOUYIN_BACKEND_API_KEY"
    #: inline key; prefer the environment variable (never logged)
    api_key: str = ""
    request_timeout_seconds: float = 30.0
    task_wait_seconds: float = 20.0
    task_poll_interval_seconds: float = 1.0
    search_page_size: int = 20
    max_search_pages_per_query: int = 3
    max_candidates_per_query: int = 50
    max_candidates_per_task: int = 200
    concurrent_requests: int = 2
    enable_remote_preview: bool = True
    remote_preview_frames: int = 8
    #: how many times a media URL is refreshed after an authorization error
    media_url_refresh_attempts: int = 1
    #: httpx environment-proxy policy for the backend connection.
    #: ``null``/omitted = auto: disabled for http://127.0.0.1 / localhost,
    #: enabled for remote backends (do not disable proxies globally).
    trust_env: bool | None = None
    retry_rejected_after_days: int = 30
    retry_failed_after_hours: int = 24
    #: media/duration resolution failures are execution retries, not content
    #: rejections: a separate short window keeps the 30-day cooldown untouched
    duration_resolution_retry_hours: float = 6.0
    discovery: DouyinDiscoverySettings = Field(default_factory=DouyinDiscoverySettings)
    browser_search: DouyinBrowserSearchSettings = Field(
        default_factory=DouyinBrowserSearchSettings
    )
    #: routes probed on the live backend to detect a keyword search capability
    search_endpoint_candidates: list[str] = Field(
        default_factory=lambda: [
            "/api/v1/douyin/search",
            "/api/v1/douyin/search/general",
            "/api/v1/search",
            "/api/v1/content/search",
        ]
    )


class CollectionBudgetSettings(BaseModel):
    """Guard rails that stop a collection task from running away (section 29)."""

    model_config = ConfigDict(extra="ignore")
    max_candidates_per_task: int = 200
    max_source_downloads_per_task: int = 50
    max_ai_calls_per_task: int = 300
    max_task_runtime_minutes: float = 60.0
    initial_candidate_multiplier: int = 5
    stop_when_target_reached: bool = True


class DebugSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    #: keep staged source videos in cache/ after processing (debugging only)
    keep_source_videos: bool = False
    #: keep rejected/failed source videos too
    keep_rejected_sources: bool = False


class PerformanceSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    douyin_concurrency: int = 2
    downloads: int = 2
    ai_analysis: int = 2


class PreviewFrameBandSettings(BaseModel):
    """One step of the adaptive preview ladder (``analysis.preview_frame_bands``)."""

    model_config = ConfigDict(extra="ignore")
    max_seconds: float = 0
    frames: int = 8


class AnalysisSettings(BaseModel):
    """Frame sampling and scene boundary refinement (``analysis:``)."""

    model_config = ConfigDict(extra="ignore")
    sampling_strategy: str = "uniform"
    preview_max_frames: int = 16
    preview_max_width: int = 640
    #: adaptive preview cost control (Milestone 3.7, section 11): the frame
    #: count follows the video duration instead of always using the maximum.
    preview_frame_adaptive: bool = True
    preview_frame_bands: list[PreviewFrameBandSettings] = Field(
        default_factory=lambda: [
            PreviewFrameBandSettings(max_seconds=15, frames=6),
            PreviewFrameBandSettings(max_seconds=45, frames=8),
            PreviewFrameBandSettings(max_seconds=90, frames=10),
            PreviewFrameBandSettings(max_seconds=0, frames=12),
        ]
    )
    analysis_max_frames: int = 12
    analysis_max_width: int = 720
    #: final-clip tagging observes 20/40/60/80% of the clip (section 7)
    clip_tagging_max_frames: int = 4
    clip_frame_ratios: list[float] = Field(default_factory=lambda: [0.2, 0.4, 0.6, 0.8])
    scene_boundary_max_adjustment_seconds: float = 1.5
    scene_detector_threshold: float = 27.0
    scene_detector_enabled: bool = True

    def preview_bands(self) -> list[PreviewFrameBand]:
        from core.frame_policy import PreviewFrameBand

        bands = [
            PreviewFrameBand(max_seconds=float(band.max_seconds), frames=int(band.frames))
            for band in self.preview_frame_bands
        ]
        if bands:
            return bands
        from core.frame_policy import DEFAULT_PREVIEW_BANDS

        return list(DEFAULT_PREVIEW_BANDS)


class MediaSettings(BaseModel):
    """Real FFmpeg cutting configuration (``media:``)."""

    model_config = ConfigDict(extra="ignore")
    backend: str = "ffmpeg"
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    video_codec: str = "libx264"
    crf: int = 18
    preset: str = "medium"
    audio_codec: str = "aac"
    audio_bitrate: str = "128k"
    precise_cut: bool = True
    thumbnail_candidates: int = 5
    command_timeout_seconds: float = 300.0
    #: UA used for remote (HTTP) media reads and downloads; empty = built-in
    remote_user_agent: str = ""
    #: bounded duration-resolution ladder (Milestone 9.6)
    duration_resolve_download: bool = True
    duration_resolve_decode_seconds: float = 30.0
    duration_resolve_timeout_seconds: float = 60.0

    def encode_settings(self) -> "EncodeSettings":
        from media.ffmpeg import EncodeSettings

        return EncodeSettings(
            video_codec=self.video_codec,
            crf=int(self.crf),
            preset=self.preset,
            audio_codec=self.audio_codec,
            audio_bitrate=self.audio_bitrate,
        )


class NetworkSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    timeout_seconds: float = 60.0
    max_retries: int = 2
    backoff_seconds: float = 1.5


class AILimitsSettings(BaseModel):
    """Guard rails that keep paid AI calls bounded (``ai.limits``)."""

    model_config = ConfigDict(extra="ignore")
    max_preview_frames: int = 16
    max_ai_calls_per_source: int = 5
    max_retries: int = 2
    confidence_escalation_threshold: float = 0.70


class ProviderReadinessSettings(BaseModel):
    """Bounded AI provider readiness gate (Milestone 9.7)."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    #: readiness probe timeout; never a full acquisition call
    timeout_seconds: float = 20.0
    #: bounded transient retry policy (kept small; no background polling)
    transient_retry_count: int = 2
    transient_backoff_seconds: float = 1.5


class VolcengineVodSettings(BaseModel):
    """Volcano Engine VOD refined subtitle erase settings (Milestone 9.8).

    Credentials are environment-only (names below); they are never stored in
    config.yaml, SQLite or logs.
    """

    model_config = ConfigDict(extra="ignore")

    endpoint: str = "https://vod.volcengineapi.com"
    region: str = ""
    space_name: str = ""
    access_key_env: str = "VOLCENGINE_ACCESS_KEY_ID"
    secret_key_env: str = "VOLCENGINE_SECRET_ACCESS_KEY"
    space_env: str = "VOLCENGINE_VOD_SPACE_NAME"
    region_env: str = "VOLCENGINE_REGION"
    storage_domain_env: str = "VOLCENGINE_VOD_STORAGE_DOMAIN"
    url_auth_key_env: str = "VOLCENGINE_VOD_URL_AUTH_KEY"
    url_auth_ttl_seconds: int = Field(default=600, ge=60, le=86400)
    api_version: str = "2025-01-01"
    poll_interval_seconds: float = 10.0
    max_poll_seconds: float = 1800.0
    request_timeout_seconds: float = 30.0
    #: fixed by policy: never automatically Text/watermark removal
    auto_type: str = "Subtitle"
    locations_margin_ratio: float = 0.01
    sdk_enabled: bool = True
    #: read-only ListSpace probes in standard regions when the configured
    #: region returns no spaces (never rewrites .env automatically)
    discover_space_region: bool = True
    #: automatic moving/floating watermark guard (never used to remove them)
    watermark_min_persistence: float = 0.30
    watermark_max_vertical_jitter: float = 0.08
    watermark_max_union_growth: float = 1.50


class CloudCleanupSettings(BaseModel):
    """Opt-in cloud cleanup policy (Milestone 9.8)."""

    model_config = ConfigDict(extra="ignore")

    #: cloud cleanup is never implicit; CLI must pass --engine volcengine
    enabled: bool = False
    engine: str = "local"
    #: hard safety cap for newly submitted paid tasks in one batch command
    max_paid_tasks_per_run: int = Field(default=3, ge=1, le=100)
    #: hard safety cap across all commands for one UTC calendar day
    max_paid_tasks_per_day: int = Field(default=10, ge=1, le=1000)
    volcengine: VolcengineVodSettings = Field(default_factory=VolcengineVodSettings)


class SourceSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    active_source: str = "douyin"
    registry: dict[str, str] = Field(default_factory=dict)
    enabled: list[str] = Field(default_factory=lambda: ["douyin"])
    search_limit_per_query: int = 8
    request_timeout_seconds: float = 20.0
    max_retries: int = 3
    local: LocalSourceSettings = Field(default_factory=LocalSourceSettings)
    douyin: DouyinSettings = Field(default_factory=DouyinSettings)
    mock: dict[str, Any] = Field(default_factory=dict)


class AIProviderSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    active_provider: str = "qwen"
    fallback_provider: str | None = None
    registry: dict[str, str] = Field(default_factory=dict)
    request_timeout_seconds: float = 60.0
    max_retries: int = 2
    retry_backoff_seconds: float = 1.5
    qwen: dict[str, Any] = Field(default_factory=dict)
    volcano: dict[str, Any] = Field(default_factory=dict)
    limits: AILimitsSettings = Field(default_factory=AILimitsSettings)
    provider_readiness: ProviderReadinessSettings = Field(
        default_factory=ProviderReadinessSettings
    )

    def provider_option(self, provider: str, key: str, default: Any = None) -> Any:
        """Read ``ai.<provider>.<key>`` without hard coding a provider schema."""

        section = getattr(self, provider, None)
        if isinstance(section, dict):
            value = section.get(key, default)
            return default if value in (None, "") else value
        return default


class PipelineSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    default_target_clip_count: int = 5
    default_min_clip_duration: float = 3.0
    default_max_clip_duration: float = 15.0
    default_subtitle_policy: str = "strict"
    keyword_max_queries: int = 10
    max_candidates_per_task: int = 60
    preview_frame_count: int = 4
    analysis_frame_count: int = 8
    candidate_min_duration: float = 5.0
    candidate_max_duration: float = 300.0
    segment_min_duration: float = 3.0
    segment_max_duration: float = 15.0
    max_segments_per_video: int = 6
    scene_refine_tolerance_seconds: float = 2.0
    scene_refine_max_expansion_ratio: float = 0.35
    download_timeout_seconds: float = 120.0
    delete_source_after_processing: bool = True
    cleanup_cache_on_finish: bool = True
    cleanup_cache_max_age_hours: float = 24.0
    media_backend: str = "mock"


class QualitySettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    min_overall_score: float = 0.55
    min_material_score: float = 0.6
    min_visual_quality_score: float = 0.5
    subtitle_score_limit: dict[str, float] = Field(
        default_factory=lambda: {"strict": 0.25, "balanced": 0.45, "loose": 0.65, "off": 1.0}
    )


class DedupSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool = True
    phash_max_distance: int = 6
    representative_frame_timestamp_ratio: float = 0.5
    #: content_key is similarity metadata only; never a hard duplicate.
    use_content_key_as_duplicate: bool = False


class UISettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    host: str = "127.0.0.1"
    port: int = 7860
    share: bool = False


class LibrarySettings(BaseModel):
    """Material-library browsing, review and export (Milestone 4, section 18)."""

    model_config = ConfigDict(extra="ignore")
    default_page_size: int = 20
    max_page_size: int = 100
    max_export_rows: int = 5000
    #: the operator view favors real material; mock/local clips are one switch away
    default_provenance_filter: str = "douyin_real"
    #: where JSON/CSV manifests are written (never inside the material library)
    exports_dir: Path = Path("exports")
    #: where orphan files are moved to (never deleted, Milestone 5 section 26)
    quarantine_dir: Path = Path("quarantine")
    #: where review CSV import/export files live
    review_dir: Path = Path("exports")

    def page_sizes(self) -> list[int]:
        sizes = {20, 50, 100, int(self.default_page_size), int(self.max_page_size)}
        return sorted(size for size in sizes if 0 < size <= max(1, int(self.max_page_size)))

    def clamp_page_size(self, value: int | str | None) -> int:
        try:
            size = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            size = int(self.default_page_size)
        return max(1, min(int(self.max_page_size), size))


class CoverageSettings(BaseModel):
    """Coverage targets and gap priorities (Milestone 5, sections 7/18).

    These are *library health targets* used for recommendations only - the
    acquisition pipeline never starts a task because of them.
    """

    model_config = ConfigDict(extra="ignore")
    default_minimum_per_stage: int = 5
    preferred_process_stages: dict[str, int] = Field(
        default_factory=lambda: {
            "raw_material": 5,
            "preparation": 5,
            "cutting": 5,
            "tray_arrangement": 8,
            "drying": 8,
            "inside_dryer": 5,
            "unloading": 5,
            "finished_product": 8,
            "equipment": 5,
        }
    )
    #: "all" counts every clip, "approved" counts only human approved clips
    count_mode: str = "all"
    #: current/target ratio below ``high`` -> 高优先级, 0 -> 严重缺口
    priority_high_ratio: float = 0.5
    #: ratio at or above ``healthy`` counts as covered
    priority_healthy_ratio: float = 1.0

    def target_for(self, stage: str) -> int:
        """Configured target for one process stage (default when not listed)."""

        value = self.preferred_process_stages.get(stage)
        if value is None:
            return max(0, int(self.default_minimum_per_stage))
        try:
            return max(0, int(value))
        except (TypeError, ValueError):  # pragma: no cover - bad config value
            return max(0, int(self.default_minimum_per_stage))


class ProductionCoverageSettings(BaseModel):
    """Production coverage targets (Milestone 9, sections 1/3/7/11).

    Targets live in configuration, never in SQL or UI code, so the operator can
    tune them without a migration.  The library state stays authoritative: a
    stage only counts as covered when its clip count really reaches the target.
    """

    model_config = ConfigDict(extra="ignore")

    #: small explicit production material list; per-material state comes from
    #: the library (not every material needs the same coverage)
    materials: list[str] = Field(
        default_factory=lambda: [
            "苹果干",
            "香菇干",
            "红薯干",
            "香蕉干",
            "芒果干",
            "辣椒干",
        ]
    )
    #: desired minimum clip count per process stage
    process_stage_targets: dict[str, int] = Field(
        default_factory=lambda: {
            "drying": 2,
            "inside_dryer": 1,
            "before_drying": 1,
            "finished_product": 2,
            "equipment": 1,
            "factory": 1,
            "loading": 1,
            "tray_arrangement": 1,
            "unloading": 1,
            "packaging": 1,
            "preparation": 1,
        }
    )
    #: explicit stage priority (1 = highest); unlisted stages use the default
    stage_priorities: dict[str, int] = Field(
        default_factory=lambda: {
            "drying": 1,
            "inside_dryer": 2,
            "before_drying": 3,
            "finished_product": 4,
            "equipment": 5,
            "factory": 6,
            "loading": 7,
            "tray_arrangement": 8,
            "unloading": 9,
            "packaging": 10,
            "preparation": 11,
        }
    )
    stage_priority_default: int = 20

    #: per-item production budgets (conservative by default, section 7)
    default_item_target_clips: int = 1
    default_item_candidates: int = 10
    default_item_downloads: int = 4
    default_item_tokens: int = 60000
    #: how many gap objectives one suggested plan may contain
    max_items_per_plan: int = 3
    #: how many objective queries one item carries
    queries_per_item: int = 4

    #: transparent, deterministic priority weights (section 2)
    weight_stage: float = 3.0
    weight_gap: float = 1.0
    weight_edit_role: float = 1.5
    weight_query_yield: float = 2.0
    penalty_duplicate: float = 2.0
    penalty_subtitle: float = 1.5
    penalty_tokens: float = 1.0
    token_reference: int = 60000
    #: rediscovery is the main production inefficiency: a query whose videos
    #: are mostly already processed is down-ranked
    recently_processed_weight: float = 2.0


class CollectionPlanningSettings(BaseModel):
    """Operator-controlled collection planning (Milestone 7, sections 5/9/48/49).

    Bounds only: a plan never starts itself, and nothing here can bypass the
    human approval step.
    """

    model_config = ConfigDict(extra="ignore")

    #: per stage safety cap: a gap of 20 requests at most this many clips
    max_requested_clips_per_stage: int = 5
    #: cap for the whole plan
    max_plan_target_clips: int = 20
    #: how many items (process stages) one plan may contain
    max_items_per_plan: int = 6

    #: plan level ceilings the item budgets must fit into
    max_plan_previews: int = 60
    max_plan_downloads: int = 20
    max_plan_ai_tokens: int = 300000
    max_plan_runtime_minutes: float = 120.0

    #: default per-item budgets used when generating a plan
    #: a real item usually needs 2-4 queries; each previews ~8 candidates
    default_item_candidates: int = 24
    default_item_downloads: int = 4
    default_item_tokens: int = 60000
    max_item_runtime_minutes: float = 30.0

    #: hard token limit safety reserve (fraction of the plan budget)
    token_reserve_ratio: float = 0.10
    #: token estimate per preview call when history is missing
    estimated_tokens_per_preview: int = 7000
    #: token estimate per analysed (downloaded) source when history is missing
    estimated_tokens_per_analysis: int = 18000

    #: query ranking weights (documented in core/planner.py)
    query_weight_clips: float = 3.0
    query_weight_accepted: float = 1.0
    query_weight_conversion: float = 2.0
    query_weight_approved: float = 0.5
    query_penalty_subtitle: float = 2.5
    query_penalty_duplicate: float = 1.5
    query_penalty_tokens: float = 1.5
    #: tokens per clip considered "expensive" when normalising the penalty
    query_token_reference: int = 60000

    # -- query_rank_v2 (Milestone 8 sections 12-22) ------------------------
    #: which ranking new plans use; ``query_rank_v1`` is kept for comparison
    ranking_version: str = "query_rank_v2"
    #: bounded historical volume: ``w * log1p(final_clips)``
    rank2_weight_yield: float = 3.0
    #: ``w * (clips / candidates)``
    rank2_weight_conversion: float = 2.0
    #: ``w * log1p(approved_clips)`` - a bonus, never a requirement
    rank2_weight_approved: float = 0.6
    #: useful yield is multiplied by ``unique_rate ** rank2_duplicate_power``
    rank2_duplicate_power: float = 1.0
    rank2_penalty_subtitle: float = 2.5
    rank2_penalty_tokens: float = 1.5
    #: queries that spent tokens without any final clip (section 18)
    rank2_penalty_zero_clip_tokens: float = 3.0
    #: queries that produced candidates but no clip at all (section 22)
    rank2_penalty_zero_yield: float = 1.0
    rank2_zero_yield_min_candidates: int = 5
    #: tokens per clip treated as "expensive" when normalising the penalty
    rank2_token_reference: int = 60000
    #: sample-size confidence: ``samples / (samples + k)`` (section 15)
    rank2_confidence_k: float = 5.0

    #: conservative default: never run two Douyin acquisitions at once
    max_concurrent_collection_tasks: int = 1
    #: plans run only when the operator starts them (no scheduling)
    require_approval: bool = True


class NoveltySettings(BaseModel):
    """Deterministic query-space novelty / saturation policy (Milestone 9.5).

    This is a layer *around* ``query_rank_v2``: the raw ranking formula stays
    frozen, while these thresholds decide how much discovery budget a query
    space deserves and whether reserve queries should be activated.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    #: only evidence inside this window can declare a query saturated
    window_days: int = 30
    #: a saturated classification expires after this many days without evidence
    saturation_expiry_days: int = 30
    #: strong saturation evidence needs at least this many candidates
    min_candidates_for_saturation: int = 4
    #: ...or this many recent yields with a very high known-source rate
    min_samples_for_saturation: int = 3
    saturated_known_source_rate: float = 0.90
    mixed_known_source_rate: float = 0.50
    recent_zero_novelty_runs: int = 2
    fresh_min_novelty_rate: float = 0.25
    #: planning prior for a generated, untested template (raw_rank_v2 is 0)
    unverified_query_prior: float = 0.5
    #: actionability weights (coverage priority * actionability)
    actionability_fresh: float = 1.0
    actionability_unknown: float = 0.70
    actionability_mixed: float = 0.50
    actionability_saturated: float = 0.10
    #: reduce actionability when some primary families are saturated
    saturated_primary_penalty: float = 0.50
    #: candidate-budget share weights by saturation status
    share_weight_fresh: float = 1.0
    share_weight_unknown: float = 1.0
    share_weight_mixed: float = 0.5
    share_weight_saturated: float = 0.0
    #: primary/reserve query families per objective
    primary_queries_per_item: int = 4
    reserve_queries_per_item: int = 4
    reserve_enabled: bool = True


class SubtitleAnalysisSettings(BaseModel):
    """Measured subtitle analysis (Milestone 6, section 40).

    Detection + geometry only: the pipeline never removes or alters pixels.
    Every threshold is configurable because the defaults were calibrated
    against a very small real library.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    #: auto | rapidocr | opencv | none
    engine: str = "auto"
    #: frames measured for a final clip (recognition on: promotion keywords)
    max_frames: int = 4
    #: frames measured for a source preview (detection only, cheaper)
    preview_max_frames: int = 3
    #: placeholder frames (mock media backend) carry no content: skip them
    skip_with_mock_media: bool = True
    #: explicit override for debugging with the mock backend
    force_with_mock: bool = False
    #: ask Qwen only when the local measurement is ambiguous
    hybrid_qwen: bool = True
    #: reuse a cached measurement for the same clip hash + analysis version
    cache: bool = True
    #: optional annotated-frame output ("cache/debug" by default is opt-in)
    debug_dir: str = ""

    # -- screen zones (section 8) --
    center_zone_start: float = 0.25
    bottom_zone_start: float = 0.72
    band_min_width: float = 0.70

    # -- classification thresholds (sections 12-18) --
    simple_bottom_max_area: float = 0.12
    simple_bottom_max_regions: int = 3
    large_center_min_area: float = 0.10
    dense_text_area: float = 0.25
    multi_region_min_persistence: float = 0.40
    center_text_reject_persistence: float = 0.50
    band_min_persistence: float = 0.40

    # -- cleanliness penalties (section 20) --
    cleanliness_weights: dict[str, float] = Field(default_factory=dict)


class AppSettings(BaseModel):
    """Root settings object handed to every component."""

    model_config = ConfigDict(extra="ignore")

    app: AppMeta = Field(default_factory=AppMeta)
    paths: PathSettings = Field(default_factory=PathSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    sources: SourceSettings = Field(default_factory=SourceSettings)
    ai: AIProviderSettings = Field(default_factory=AIProviderSettings)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings)
    media: MediaSettings = Field(default_factory=MediaSettings)
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    quality: QualitySettings = Field(default_factory=QualitySettings)
    dedup: DedupSettings = Field(default_factory=DedupSettings)
    collection: CollectionBudgetSettings = Field(default_factory=CollectionBudgetSettings)
    debug: DebugSettings = Field(default_factory=DebugSettings)
    performance: PerformanceSettings = Field(default_factory=PerformanceSettings)
    ui: UISettings = Field(default_factory=UISettings)
    library: LibrarySettings = Field(default_factory=LibrarySettings)
    coverage: CoverageSettings = Field(default_factory=CoverageSettings)
    collection_planning: CollectionPlanningSettings = Field(
        default_factory=CollectionPlanningSettings
    )
    production_coverage: ProductionCoverageSettings = Field(
        default_factory=ProductionCoverageSettings
    )
    novelty: NoveltySettings = Field(default_factory=NoveltySettings)
    cloud_cleanup: CloudCleanupSettings = Field(default_factory=CloudCleanupSettings)
    subtitle_analysis: SubtitleAnalysisSettings = Field(
        default_factory=SubtitleAnalysisSettings
    )
    #: conservative local subtitle cleanup (Milestone 9.2): derivative only,
    #: never touches the authoritative library clip.
    subtitle_cleanup: SubtitleCleanupConfig = Field(
        default_factory=SubtitleCleanupConfig
    )
    project_root: Path = PROJECT_ROOT
    secrets: dict[str, str] = Field(default_factory=dict)
    #: Runtime-only override chosen by the backend preflight (section 3).  It is
    #: never written back to ``config.yaml`` and never contains a secret.
    douyin_backend_override: str | None = None
    #: Where ``douyin_base_url()`` came from: ``cli``/``env``/``config``/``preflight``
    douyin_backend_origin: str = ""

    def secret(self, key: str, default: str = "") -> str:
        """Read a secret from the environment, falling back to the loaded .env."""

        return os.environ.get(key) or self.secrets.get(key, "") or default

    def douyin_api_key(self) -> str:
        """Backend credential: environment first, then the inline config value.

        The value is never logged, never persisted and never echoed into an
        exception message by the Douyin client.
        """

        env_name = self.sources.douyin.api_key_env or "DOUYIN_BACKEND_API_KEY"
        return self.secret(env_name) or self.sources.douyin.api_key or ""

    def douyin_base_url(self) -> str:
        """Backend base URL: ``DOUYIN_BACKEND_BASE_URL`` overrides config.yaml.

        Useful for pointing at a staging backend (or the local dev stub in
        ``scripts/douyin_backend_stub.py``) without editing files.

        Precedence: backend preflight override > ``DOUYIN_BACKEND_BASE_URL``
        from the environment/``.env`` > ``sources.douyin.base_url``.
        """

        if self.douyin_backend_override:
            return self.douyin_backend_override
        return self.secret("DOUYIN_BACKEND_BASE_URL") or self.sources.douyin.base_url or ""

    def douyin_backend_source(self) -> str:
        """Non-secret provenance of the effective backend URL (section 30)."""

        if self.douyin_backend_override:
            return "preflight"
        if self.secret("DOUYIN_BACKEND_BASE_URL"):
            return "env"
        return "config" if self.sources.douyin.base_url else "unset"

    def douyin_session_cookie(self) -> str:
        """Optional console session cookie for the backend (env only).

        Some deployments authenticate with a ``dtk_session`` cookie rather than
        an API key.  The value is read from the environment only: it is never
        written to config.yaml, logged, or stored in SQLite.
        """

        return self.secret("DOUYIN_BACKEND_SESSION_COOKIE")


def _load_dotenv(env_path: Path) -> dict[str, str]:
    """Load ``.env`` without ever logging the values."""

    if not env_path.exists():
        return {}
    try:
        from dotenv import dotenv_values

        raw = dotenv_values(env_path)
        return {key: value for key, value in raw.items() if value is not None}
    except Exception:  # pragma: no cover - python-dotenv is optional at runtime
        values: dict[str, str] = {}
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
        return values


def _anchor(value: Path | str, base: Path) -> Path:
    """Anchor a relative path to ``base``; absolute paths pass through."""

    path = Path(value)
    return path if is_absolute_path(path) else (base / path)


def _reconcile_sections(payload: dict[str, Any]) -> dict[str, Any]:
    """Fold the Milestone 2 sections into the legacy ``paths``/``pipeline`` keys.

    ``storage`` is the canonical Milestone 2 layout; the Milestone 1 ``paths``
    entries still work as a fallback so older config files keep loading.
    """

    payload = dict(payload)
    storage = dict(payload.get("storage") or {})
    legacy_paths = dict(payload.get("paths") or {})
    if "library_root" not in storage and "library_root" in legacy_paths:
        storage["library_root"] = legacy_paths["library_root"]
    if "database_path" not in storage and "database" in legacy_paths:
        storage["database_path"] = legacy_paths["database"]
    if "cache_root" not in storage and "cache_dir" in legacy_paths:
        storage["cache_root"] = legacy_paths["cache_dir"]
    if storage:
        payload["storage"] = storage

    media = dict(payload.get("media") or {})
    pipeline = dict(payload.get("pipeline") or {})
    if "media_backend" not in pipeline and media.get("backend"):
        pipeline["media_backend"] = media["backend"]
    payload["pipeline"] = pipeline
    return payload


def load_settings(
    config_path: Path | str | None = None,
    env_path: Path | str | None = None,
    overrides: dict[str, Any] | None = None,
) -> AppSettings:
    """Load settings from YAML, apply optional overrides, resolve paths."""

    config_file = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    payload: dict[str, Any] = {}
    if config_file.exists():
        loaded = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"config file {config_file} must contain a YAML mapping")
        payload = loaded
    else:
        LOGGER.warning("config file %s not found, using defaults", config_file)

    env_file = Path(env_path) if env_path else PROJECT_ROOT / ".env"
    secrets = _load_dotenv(env_file)

    # Portable runtime paths: process environment wins over the local .env,
    # and both win over machine-specific defaults in config.yaml.  Keeping
    # these outside Git lets the same checkout move between drive letters.
    def runtime_value(name: str) -> str:
        return str(os.environ.get(name) or secrets.get(name) or "").strip()

    portable_overrides: dict[str, Any] = {"storage": {}, "media": {}}
    for env_name, field_name in (
        ("FOHE_LIBRARY_ROOT", "library_root"),
        ("FOHE_DATABASE_PATH", "database_path"),
        ("FOHE_CACHE_ROOT", "cache_root"),
    ):
        value = runtime_value(env_name)
        if value:
            portable_overrides["storage"][field_name] = value
    for env_name, field_name in (
        ("FOHE_FFMPEG_PATH", "ffmpeg_path"),
        ("FOHE_FFPROBE_PATH", "ffprobe_path"),
    ):
        value = runtime_value(env_name)
        if value:
            portable_overrides["media"][field_name] = value
    portable_overrides = {
        section: values for section, values in portable_overrides.items() if values
    }
    if portable_overrides:
        payload = _deep_merge(payload, portable_overrides)
    if overrides:
        payload = _deep_merge(payload, overrides)

    settings = AppSettings.model_validate(_reconcile_sections(payload))
    # Every .env value is available through ``settings.secret()``; the dict is
    # internal and is never logged or persisted.
    settings.secrets = dict(secrets)
    settings.paths = settings.paths.resolved(settings.project_root)
    # Canonical storage layout wins over the legacy ``paths`` entries.
    settings.paths.library_root = _anchor(settings.storage.library_root, settings.project_root)
    settings.paths.database = _anchor(settings.storage.database_path, settings.project_root)
    settings.paths.cache_dir = _anchor(settings.storage.cache_root, settings.project_root)
    settings.library.exports_dir = _anchor(
        settings.library.exports_dir, settings.project_root
    )
    settings.library.quarantine_dir = _anchor(
        settings.library.quarantine_dir, settings.project_root
    )
    settings.library.review_dir = _anchor(settings.library.review_dir, settings.project_root)
    settings.subtitle_cleanup.reports_dir = _anchor(
        settings.subtitle_cleanup.reports_dir, settings.project_root
    )
    level = os.environ.get("LOG_LEVEL") or secrets.get("LOG_LEVEL")
    if level:
        settings.app.log_level = level
    return settings


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def configure_logging(level: str = "INFO", log_dir: Path | None = None) -> logging.Logger:
    """Configure console logging, plus a file handler when ``log_dir`` is given."""

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Re-configuring must not duplicate handlers (tests call this repeatedly).
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "agent.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Third party libraries are noisy at DEBUG level.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "matplotlib", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("agent")


def load_object(dotted_path: str) -> type:
    """Import ``package.module:ClassName`` style registry entries."""

    module_name, _, attribute = dotted_path.partition(":")
    if not module_name or not attribute:
        raise ValueError(f"invalid registry entry: {dotted_path!r} (expected 'module:Class')")
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attribute)
    except AttributeError as exc:  # pragma: no cover - configuration error
        raise ImportError(f"{attribute} not found in module {module_name}") from exc
