"""Builds the concrete dependency graph for one runtime configuration.

Swapping implementations is a ``config.yaml`` change (or a per-request
override), never an edit to the orchestrator:

* ``sources.active_source: local | mock | douyin``
* ``ai.active_provider:    qwen | volcano | mock``
* ``media.backend:         ffmpeg | mock | auto``
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Sequence

from ai.audit import AICallRecord
from ai.gateway import AIGateway
from ai.mock import MockVisionProvider
from ai.qwen import QwenProvider
from ai.volcano import VolcanoProvider
from analyzers.candidate_filter import CandidateFilter
from analyzers.preview_filter import PreviewFilter
from analyzers.quality_gate import QualityGate
from analyzers.scene_refiner import SceneRefiner, detector_from_settings
from analyzers.video_analyzer import VideoAnalyzer
from core.config import AppSettings
from core.browser_config import resolve_browser_config
from core.frame_policy import clip_frame_ratios
from core.models import SubtitlePolicy
from core.orchestrator import OrchestratorDependencies
from media.clipper import ClipCutter
from media.downloader import (
    HttpDownloader,
    LocalFileDownloader,
    MockDownloader,
    VideoDownloader,
)
from media.ffmpeg import (
    DEFAULT_REMOTE_USER_AGENT,
    EncodeSettings,
    FFmpegToolkit,
    MediaToolkit,
    MockMediaToolkit,
)
from media.frame_sampler import FrameSampler
from sources.base import VideoSource
from sources.douyin import DouyinSource
from sources.douyin_backend import DouyinBackendClient
from sources.douyin_browser_search import DouyinBrowserSearchBackend
from sources.local import LocalFileSource
from sources.mock import MockVideoSource
from storage.database import Database
from storage.dedup import DeduplicationService
from storage.library import MaterialLibrary

LOGGER = logging.getLogger(__name__)


def build_library(settings: AppSettings, *, library_root: Path | None = None) -> MaterialLibrary:
    database = Database(settings.paths.database)
    library = MaterialLibrary(database, library_root or settings.paths.library_root)
    library.initialize()
    return library


def resolve_source_name(settings: AppSettings, override: str | None = None) -> str:
    return (override or settings.sources.active_source or "local").lower()


def resolve_media_backend(settings: AppSettings, override: str | None = None) -> str:
    return (override or settings.media.backend or settings.pipeline.media_backend or "auto").lower()


def build_subtitle_analyzer(
    settings: AppSettings, *, cache: Any | None = None, media_backend: str | None = None
):
    """Build the measured subtitle analyzer (Milestone 6).

    Returns ``None`` when the feature is disabled so the orchestrator keeps
    using the VLM subtitle verdict only.
    """

    config = getattr(settings, "subtitle_analysis", None)
    if config is None or not getattr(config, "enabled", True):
        return None
    # Placeholder frames (the mock media backend) carry no visual information:
    # measuring them would burn CPU for nothing.  Real runs always analyse.
    if (
        resolve_media_backend(settings, media_backend) == "mock"
        and getattr(config, "skip_with_mock_media", True)
        and not getattr(config, "force_with_mock", False)
    ):
        LOGGER.info(
            "subtitle analysis skipped: media backend is 'mock' "
            "(set subtitle_analysis.force_with_mock=true to override)"
        )
        return None
    from analyzers.subtitle_analysis import SubtitleAnalyzer
    from analyzers.subtitle_settings import build_subtitle_settings

    subtitle_settings = build_subtitle_settings(config, base=settings.project_root)
    analyzer = SubtitleAnalyzer(subtitle_settings, cache=cache)
    usable, note = analyzer.detector.available()
    LOGGER.info(
        "subtitle analysis: engine=%s available=%s (%s), max_frames=%s",
        analyzer.detector.name,
        usable,
        note,
        subtitle_settings.max_frames,
    )
    return analyzer


def build_source(
    settings: AppSettings,
    *,
    name: str | None = None,
    local_files: Sequence[Path | str] = (),
    douyin_urls: Sequence[str] = (),
    toolkit: MediaToolkit | None = None,
    browser_backend: "DouyinBrowserSearchBackend | None" = None,
) -> VideoSource:
    """Instantiate the configured ``VideoSource``."""

    source_name = resolve_source_name(settings, name)
    local_settings = settings.sources.local

    if source_name == "local":
        preview_dir = settings.paths.cache_dir / "previews"
        preview_frames = min(
            settings.analysis.preview_max_frames, settings.ai.limits.max_preview_frames
        )
        source = LocalFileSource(
            local_files,
            extensions=local_settings.normalised_extensions(),
            recursive=local_settings.recursive,
            toolkit=toolkit,
            preview_dir=preview_dir,
            preview_frame_count=preview_frames,
            preview_max_width=settings.analysis.preview_max_width,
            sampling_strategy=settings.analysis.sampling_strategy,
            request_timeout=settings.sources.request_timeout_seconds,
            max_retries=settings.sources.max_retries,
        )
        LOGGER.info("using LocalFileSource with %s file(s)", len(source.files))
        return source

    if source_name == "mock":
        mock_config = settings.sources.mock or {}
        preview_dir = settings.paths.cache_dir / "previews"
        preview_dir.mkdir(parents=True, exist_ok=True)
        LOGGER.info("using MockVideoSource (seed=%s)", mock_config.get("seed", 20260914))
        return MockVideoSource(
            seed=int(mock_config.get("seed", 20260914)),
            preview_dir=preview_dir,
            preview_frame_count=min(
                settings.analysis.preview_max_frames, settings.ai.limits.max_preview_frames
            ),
            write_placeholder_media=bool(mock_config.get("write_placeholder_media", True)),
            request_timeout=settings.sources.request_timeout_seconds,
            max_retries=settings.sources.max_retries,
        )

    if source_name == "douyin":
        douyin = settings.sources.douyin
        client = build_douyin_client(settings)
        preview_frames = max(
            1, min(douyin.remote_preview_frames, settings.analysis.preview_max_frames)
        )
        manual_urls = list(dict.fromkeys([*douyin.discovery.manual_urls, *douyin_urls]))
        browser_settings = douyin.browser_search
        browser_available, browser_note = DouyinBrowserSearchBackend.playwright_available()
        enable_browser = bool(browser_settings.enabled and browser_available)
        # Milestone 8.1: a caller (interactive verification / one-plan run) may
        # inject an already-open backend so the verified session is reused
        # instead of launching a second browser.
        injected_browser = browser_backend
        browser_backend = None
        if enable_browser:
            browser_backend = injected_browser or build_browser_search(settings)
        elif browser_settings.enabled:
            LOGGER.warning("browser search requested but unavailable: %s", browser_note)
        LOGGER.info(
            "using DouyinSource backend=%s base_url=%s manual_urls=%s browser_search=%s",
            douyin.backend,
            client.base_url or "(unset)",
            len(manual_urls),
            enable_browser,
        )
        return DouyinSource(
            client=client,
            toolkit=toolkit,
            author_sec_uids=douyin.discovery.author_sec_uids,
            mix_ids=douyin.discovery.mix_ids,
            manual_urls=manual_urls,
            archive_search=douyin.discovery.archive_search,
            enable_remote_preview=douyin.enable_remote_preview,
            browser_search=browser_backend,
            enable_browser_search=enable_browser,
            preview_dir=settings.paths.cache_dir / "previews",
            preview_frame_count=preview_frames,
            preview_max_width=settings.analysis.preview_max_width,
            sampling_strategy=settings.analysis.sampling_strategy,
            adaptive_preview=settings.analysis.preview_frame_adaptive,
            preview_frame_bands=settings.analysis.preview_bands(),
            request_timeout=douyin.request_timeout_seconds,
            max_retries=settings.network.max_retries,
        )
    raise ValueError(f"unknown source: {source_name!r} (see sources.registry in config.yaml)")


def cdp_endpoint_available(port: int, *, host: str = "127.0.0.1", timeout: float = 0.8) -> str:
    """Return the CDP URL when an operator browser answers on ``port``.

    Milestone 8.3: this is a plain reachability probe of the operator's own
    Chrome/Edge debug port (V3.2 model).  No automation flags are involved.
    """

    import urllib.request

    url = f"http://{host}:{int(port)}"
    try:
        with urllib.request.urlopen(f"{url}/json/version", timeout=timeout) as response:
            if response.status == 200:
                return url
    except Exception:
        return ""
    return ""


def resolve_cdp_url(settings: AppSettings) -> str:
    """Effective CDP target: explicit config, ``off``, or auto-detect."""

    browser = settings.sources.douyin.browser_search
    configured = (browser.cdp_url or "").strip()
    if configured.lower() in ("off", "none", "disabled", "false"):
        return ""
    if configured:
        return configured
    return cdp_endpoint_available(int(browser.cdp_port))


def build_browser_search(
    settings: AppSettings,
    *,
    headless: bool | None = None,
    keep_open_on_challenge: bool | None = None,
    keep_page_on_challenge: bool | None = None,
    cdp_url: str | None = None,
) -> DouyinBrowserSearchBackend:
    """Playwright discovery backend built from ``sources.douyin.browser_search``.

    ``--init-douyin-browser``, ``--check-douyin-browser`` and the real search all
    go through this function, so they necessarily share one profile, channel,
    executable, locale and headless flag (sections 7 and 8).

    ``keep_page_on_challenge`` (Milestone 8.1) keeps the challenged **page** in
    the same context alive for the interactive verification gate; the plain
    discovery path leaves it off so a blocked task still tears its pages down.
    """

    browser = settings.sources.douyin.browser_search
    launch = resolve_browser_config(settings, headless=headless)
    effective_cdp = resolve_cdp_url(settings) if cdp_url is None else cdp_url
    return DouyinBrowserSearchBackend(
        profile_dir=launch.profile_dir,
        headless=launch.headless,
        max_scrolls_per_query=browser.max_scrolls_per_query,
        scroll_delay_seconds=browser.scroll_delay_seconds,
        max_results_per_query=browser.max_results_per_query,
        navigation_timeout_seconds=browser.navigation_timeout_seconds,
        browser_channel=launch.channel,
        browser_executable_path=launch.executable_path,
        locale=launch.locale,
        viewport_width=launch.viewport_width,
        viewport_height=launch.viewport_height,
        slow_mo_ms=launch.slow_mo_ms,
        page_settle_seconds=browser.page_settle_seconds,
        keep_open_on_challenge=(
            browser.keep_open_on_challenge
            if keep_open_on_challenge is None
            else keep_open_on_challenge
        ),
        keep_page_on_challenge=(
            browser.auto_resume_after_verification
            if keep_page_on_challenge is None
            else keep_page_on_challenge
        ),
        auto_resume_after_verification=browser.auto_resume_after_verification,
        challenge_wait_timeout_seconds=browser.challenge_wait_timeout_seconds,
        challenge_poll_seconds=browser.challenge_poll_seconds,
        cdp_url=effective_cdp,
        upstream_retry_count=browser.upstream_retry_count,
        upstream_retry_backoff_seconds=browser.upstream_retry_backoff_seconds,
        search_settle_timeout_seconds=browser.search_settle_timeout_seconds,
        search_settle_poll_seconds=browser.search_settle_poll_seconds,
        empty_result_limit=browser.empty_result_limit,
    )


def build_douyin_client(settings: AppSettings) -> DouyinBackendClient:
    """Client for the self-hosted Douyin backend (dtk v5 contract)."""

    douyin = settings.sources.douyin
    return DouyinBackendClient(
        base_url=settings.douyin_base_url(),
        api_key=settings.douyin_api_key(),
        session_cookie=settings.douyin_session_cookie(),
        timeout=douyin.request_timeout_seconds or settings.network.timeout_seconds,
        max_retries=settings.network.max_retries or settings.sources.max_retries,
        backoff_seconds=settings.network.backoff_seconds,
        task_wait_seconds=douyin.task_wait_seconds,
        task_poll_interval_seconds=douyin.task_poll_interval_seconds,
        concurrency=min(
            douyin.concurrent_requests, max(1, settings.performance.douyin_concurrency)
        ),
        user_agent=settings.media.remote_user_agent or DEFAULT_REMOTE_USER_AGENT,
        search_endpoint_candidates=douyin.search_endpoint_candidates,
        # None = auto: no environment proxy for a localhost backend
        trust_env=douyin.trust_env,
    )


def build_provider(settings: AppSettings, name: str):
    """Instantiate one ``VisionProvider`` by registry name."""

    provider = (name or "").lower()
    limits = settings.ai.limits
    timeout = settings.ai.request_timeout_seconds or settings.network.timeout_seconds
    retries = limits.max_retries or settings.ai.max_retries
    backoff = settings.network.backoff_seconds or settings.ai.retry_backoff_seconds
    max_images = min(limits.max_preview_frames, settings.analysis.preview_max_frames)

    if provider == "mock":
        seed = int((settings.sources.mock or {}).get("seed", 20260914))
        return MockVisionProvider(seed=seed, timeout=timeout, max_retries=retries)

    if provider == "qwen":
        env_model = settings.secret("QWEN_VISION_MODEL")
        return QwenProvider(
            api_key=settings.secret("QWEN_API_KEY"),
            base_url=settings.secret("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            model=env_model,
            preview_model=settings.ai.provider_option("qwen", "preview_model", ""),
            analysis_model=settings.ai.provider_option("qwen", "analysis_model", ""),
            fallback_model=settings.ai.provider_option("qwen", "fallback_model", ""),
            timeout=timeout,
            max_retries=retries,
            backoff_seconds=backoff,
            json_mode=bool(settings.ai.provider_option("qwen", "use_json_mode", True)),
            max_images=max_images,
            confidence_escalation_threshold=limits.confidence_escalation_threshold,
        )

    if provider == "volcano":
        return VolcanoProvider(
            api_key=settings.secret("VOLCANO_API_KEY"),
            base_url=settings.secret(
                "VOLCANO_ENDPOINT",
                settings.ai.provider_option("volcano", "endpoint", ""),
            ),
            model=settings.secret(
                "VOLCANO_MODEL", settings.ai.provider_option("volcano", "model", "")
            ),
            timeout=timeout,
            max_retries=retries,
            backoff_seconds=backoff,
            json_mode=bool(settings.ai.provider_option("volcano", "use_json_mode", True)),
            max_images=max_images,
        )
    raise ValueError(f"unknown ai provider: {provider!r} (see ai.registry in config.yaml)")


def build_gateway(
    settings: AppSettings,
    *,
    provider_name: str | None = None,
    on_call: Callable[[AICallRecord], None] | None = None,
) -> AIGateway:
    primary_name = (provider_name or settings.ai.active_provider or "mock").lower()
    primary = build_provider(settings, primary_name)
    fallback_name = (settings.ai.fallback_provider or "").strip().lower()
    fallback = build_provider(settings, fallback_name) if fallback_name else None
    if fallback is not None and not getattr(fallback, "configured", True):
        LOGGER.info(
            "fallback provider %s is not configured; it will be skipped", fallback.name
        )
    LOGGER.info(
        "ai gateway: primary=%s(%s) fallback=%s",
        primary.name,
        getattr(primary, "configured", True),
        fallback.name if fallback else None,
    )
    return AIGateway(
        primary,
        fallback,
        timeout=settings.ai.request_timeout_seconds or settings.network.timeout_seconds,
        max_retries=settings.ai.limits.max_retries or settings.ai.max_retries,
        backoff_seconds=settings.ai.retry_backoff_seconds,
        on_call=on_call,
    )


def build_downloader(settings: AppSettings, *, source_name: str) -> VideoDownloader:
    """Pick the downloader from the source, not from the media backend.

    Local files are staged (copied) from disk, mock candidates get placeholder
    files, and real platforms go through the retrying HTTP downloader.
    """

    if source_name == "local":
        return LocalFileDownloader(copy_file=settings.sources.local.copy_to_cache)
    if source_name == "mock":
        return MockDownloader()

    headers: dict[str, str] = {}
    if source_name == "douyin":
        # Douyin's CDN expects a browser-ish referer for media reads.
        headers["Referer"] = "https://www.douyin.com/"
    return HttpDownloader(
        timeout=settings.network.timeout_seconds or settings.pipeline.download_timeout_seconds,
        max_retries=settings.network.max_retries or settings.sources.max_retries,
        headers=headers,
        user_agent=settings.media.remote_user_agent or DEFAULT_REMOTE_USER_AGENT,
    )


def build_toolkit(
    settings: AppSettings,
    *,
    backend: str | None = None,
) -> MediaToolkit:
    """Real FFmpeg toolkit, mock toolkit, or auto detection."""

    resolved = resolve_media_backend(settings, backend)
    encode_settings = EncodeSettings(
        video_codec=settings.media.video_codec,
        crf=settings.media.crf,
        preset=settings.media.preset,
        audio_codec=settings.media.audio_codec,
        audio_bitrate=settings.media.audio_bitrate,
    )
    if resolved == "mock":
        return MockMediaToolkit()

    toolkit = FFmpegToolkit(
        ffmpeg_bin=settings.media.ffmpeg_path or "ffmpeg",
        ffprobe_bin=settings.media.ffprobe_path or "ffprobe",
        timeout=settings.media.command_timeout_seconds,
        encode_settings=encode_settings,
        precise_cut=settings.media.precise_cut,
        remote_user_agent=settings.media.remote_user_agent or DEFAULT_REMOTE_USER_AGENT,
    )
    if toolkit.is_available:
        LOGGER.info("media backend: %s", toolkit.describe())
        return toolkit

    if resolved == "ffmpeg":
        LOGGER.warning(
            "ffmpeg/ffprobe not available (%s); falling back to the placeholder media "
            "backend. Install FFmpeg or fix media.ffmpeg_path / media.ffprobe_path.",
            toolkit.describe(),
        )
    return MockMediaToolkit()


def build_dependencies(
    settings: AppSettings,
    *,
    library: MaterialLibrary | None = None,
    subtitle_policy: SubtitlePolicy | None = None,
    source_name: str | None = None,
    provider_name: str | None = None,
    media_backend: str | None = None,
    local_files: Sequence[Path | str] = (),
    douyin_urls: Sequence[str] = (),
    on_ai_call: Callable[[AICallRecord], None] | None = None,
    browser_backend: "DouyinBrowserSearchBackend | None" = None,
) -> OrchestratorDependencies:
    """Assemble every collaborator the orchestrator needs."""

    pipeline = settings.pipeline
    analysis = settings.analysis
    policy = subtitle_policy or SubtitlePolicy(pipeline.default_subtitle_policy)
    library = library or build_library(settings)
    resolved_source = resolve_source_name(settings, source_name)
    toolkit = build_toolkit(settings, backend=media_backend)
    gateway = build_gateway(settings, provider_name=provider_name, on_call=on_ai_call)

    dedup = DeduplicationService(
        library,
        enabled=settings.dedup.enabled,
        phash_max_distance=settings.dedup.phash_max_distance,
        use_content_key_as_duplicate=settings.dedup.use_content_key_as_duplicate,
    )

    # measured subtitle analysis (Milestone 6); the library doubles as the
    # (sha256 + version) cache so identical clips are never OCR'd twice
    subtitle_analyzer = build_subtitle_analyzer(
        settings, cache=library, media_backend=media_backend
    )

    preview_frames = min(analysis.preview_max_frames, settings.ai.limits.max_preview_frames)
    analysis_sampler = FrameSampler(
        toolkit,
        max_frames=analysis.analysis_max_frames,
        max_width=analysis.analysis_max_width,
        strategy=analysis.sampling_strategy,
    )
    scene_detection = bool(
        analysis.scene_detector_enabled and not isinstance(toolkit, MockMediaToolkit)
    )

    return OrchestratorDependencies(
        source=build_source(
            settings,
            name=resolved_source,
            local_files=local_files,
            douyin_urls=douyin_urls,
            toolkit=toolkit,
            browser_backend=browser_backend,
        ),
        gateway=gateway,
        downloader=build_downloader(settings, source_name=resolved_source),
        toolkit=toolkit,
        library=library,
        dedup=dedup,
        candidate_filter=CandidateFilter(
            dedup,
            min_duration=pipeline.candidate_min_duration,
            max_duration=pipeline.candidate_max_duration,
            retry_rejected_after_days=settings.sources.douyin.retry_rejected_after_days,
            retry_failed_after_hours=settings.sources.douyin.retry_failed_after_hours,
            duration_resolution_retry_hours=settings.sources.douyin.duration_resolution_retry_hours,
        ),
        preview_filter=PreviewFilter(
            gateway,
            policy=policy,
            max_frames=preview_frames,
            subtitle_score_limit=settings.quality.subtitle_score_limit,
        ),
        analyzer=VideoAnalyzer(
            gateway,
            analysis_sampler,
            frame_count=analysis.analysis_max_frames,
            min_segment_duration=pipeline.segment_min_duration,
            max_segment_duration=pipeline.segment_max_duration,
            max_segments=pipeline.max_segments_per_video,
        ),
        scene_refiner=SceneRefiner(
            detector_from_settings(scene_detection, threshold=analysis.scene_detector_threshold),
            tolerance_seconds=analysis.scene_boundary_max_adjustment_seconds,
            max_expansion_ratio=pipeline.scene_refine_max_expansion_ratio,
        ),
        quality_gate=QualityGate(
            policy=policy,
            min_overall_score=settings.quality.min_overall_score,
            min_material_score=settings.quality.min_material_score,
            min_visual_quality_score=settings.quality.min_visual_quality_score,
            subtitle_score_limit=settings.quality.subtitle_score_limit,
        ),
        clipper=ClipCutter(
            toolkit,
            thumbnail_timestamp_ratio=settings.dedup.representative_frame_timestamp_ratio,
            thumbnail_candidates=settings.media.thumbnail_candidates,
            precise_cut=settings.media.precise_cut,
            encode_settings=EncodeSettings(
                video_codec=settings.media.video_codec,
                crf=settings.media.crf,
                preset=settings.media.preset,
                audio_codec=settings.media.audio_codec,
                audio_bitrate=settings.media.audio_bitrate,
            ),
            tagging_frame_ratios=clip_frame_ratios(
                analysis.clip_tagging_max_frames,
                ratios=tuple(analysis.clip_frame_ratios) or None,
            ),
            # image tokens dominate clip tagging: send bounded frames while the
            # delivered thumbnail keeps the source resolution (section 9/13)
            tagging_frame_max_width=analysis.preview_max_width,
        ),
        settings=settings,
        subtitle_analyzer=subtitle_analyzer,
    )
