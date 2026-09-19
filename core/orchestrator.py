"""The collection workflow.

    material -> prioritized keywords -> source search -> global dedup
    -> metadata prefilter -> remote preview frames -> AI pre-filter
    -> stage/download accepted sources -> FFprobe -> scene boundaries
    -> AI segment detection -> timestamp validation -> scene refinement
    -> FFmpeg cut -> thumbnail -> AI tagging -> dedup -> quality gate
    -> SQLite (with provenance) -> cache cleanup

Budget limited (candidates / downloads / AI calls / wall clock), resumable,
and platform independent: every collaborator is injected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ai.base import AuditContext, ClipTaggingRequest
from ai.gateway import AIGateway
from analyzers.candidate_filter import CandidateFilter
from analyzers.preview_filter import SUBTITLE_REASONS, PreviewFilter
from analyzers.quality_gate import QualityGate
from analyzers.scene_refiner import SceneRefiner
from analyzers.subtitle_analysis import SubtitleAnalyzer, classification_bucket
from analyzers.video_analyzer import VideoAnalyzer
from core.config import AppSettings
from core.frame_policy import preview_frame_count
from core.keyword_expander import KeywordExpander
from core.normalization import normalize_tagging
from core.provenance import classify_provenance
from storage.library import TERMINAL_STATUSES
from core.models import (
    ClipArtifact,
    ClipRecord,
    ClipScores,
    ClipTagging,
    DetectedSegment,
    MaterialState,
    PipelineResult,
    PipelineStats,
    PreviewFrame,
    ProcessStage,
    ProgressEvent,
    RejectReason,
    SegmentReport,
    SegmentTiming,
    SourceVideoReport,
    SourceVideoStatus,
    SubtitleType,
    TaskRequest,
    TaskStatus,
    VideoCandidate,
    utc_now,
)
from media.clipper import ClipCutter
from media.downloader import DownloadError, MediaUrlExpiredError, VideoDownloader
from media.ffmpeg import (
    MediaInfo,
    MediaToolkit,
    RemoteMediaError,
    validate_remote_media_url,
)
from media.frame_sampler import FrameSampler
from sources.base import SourceError, VideoSource
from sources.douyin_search import BrowserSearchStatus, DiscoveryBlockedError
from storage.dedup import DeduplicationService
from storage.library import MaterialLibrary

LOGGER = logging.getLogger(__name__)

#: bytes that identify a web page / JSON payload served in place of a video
_WEB_PAYLOAD_MARKERS: tuple[bytes, ...] = (
    b"<!doctype",
    b"<html",
    b"<?xml",
    b"{\"",
)


def _looks_like_web_page(path: Path) -> bool:
    """Whether a downloaded payload is obviously *not* a media container.

    A 72 KB HTML page served by a broken media URL must be reported as
    ``invalid_media_source`` instead of being probed as a video (Milestone 9.1).
    """

    try:
        with path.open("rb") as handle:
            head = handle.read(64).lower().lstrip()
    except OSError:  # pragma: no cover - defensive
        return False
    if not head:
        return True
    if any(head.startswith(marker) for marker in _WEB_PAYLOAD_MARKERS):
        return True
    # container magic bytes we do accept (mp4/mov/m4v, webm/mkv)
    if head[4:8] == b"ftyp" or head.startswith(b"\x1a\x45\xdf\xa3"):
        return False
    # anything else that is printable ASCII is a text payload, not media
    return all(32 <= byte < 127 or byte in (9, 10, 13) for byte in head[:16])


def _describe_size(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:  # pragma: no cover - defensive
        return "size unknown"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"

ProgressCallback = Callable[[ProgressEvent], None]

#: rejection reasons that end up in the "duplicates" counter
DUPLICATE_REASONS = frozenset(
    {
        RejectReason.DUPLICATE_URL,
        RejectReason.DUPLICATE_VIDEO,
        RejectReason.DUPLICATE_CLIP,
        RejectReason.ALREADY_PROCESSED,
    }
)


@dataclass
class OrchestratorDependencies:
    """Everything the orchestrator needs, injected from the outside."""

    source: VideoSource
    gateway: AIGateway
    downloader: VideoDownloader
    toolkit: MediaToolkit
    library: MaterialLibrary
    dedup: DeduplicationService
    candidate_filter: CandidateFilter
    preview_filter: PreviewFilter
    analyzer: VideoAnalyzer
    scene_refiner: SceneRefiner
    quality_gate: QualityGate
    clipper: ClipCutter
    settings: AppSettings
    #: measured subtitle analysis (Milestone 6); optional for older tests
    subtitle_analyzer: SubtitleAnalyzer | None = None


@dataclass
class _QueryYield:
    """Per-query yield counters (section 31)."""

    query: str
    candidates: int = 0
    unique: int = 0
    accepted: int = 0
    downloads: int = 0
    clips: int = 0
    new_to_system: int = 0
    known_source: int = 0
    current_run_duplicate: int = 0
    already_processed: int = 0
    already_represented: int = 0
    planned_order: int = 0
    actual_order: int = 0
    candidate_cap: int = 0
    stop_reason: str = ""
    reserve_activation_reason: str = ""
    was_reserve: bool = False
    query_family: str = ""
    plan_id: int | None = None
    plan_item_id: int | None = None

    def as_audit(self) -> dict:
        return {
            "query": self.query,
            "query_family": self.query_family,
            "candidates": self.candidates,
            "unique": self.unique,
            "new_to_system": self.new_to_system,
            "known_source": self.known_source,
            "current_run_duplicate": self.current_run_duplicate,
            "already_processed": self.already_processed,
            "already_represented": self.already_represented,
            "preview_accept": self.accepted,
            "downloads": self.downloads,
            "clips": self.clips,
            "planned_order": self.planned_order,
            "actual_order": self.actual_order,
            "candidate_cap": self.candidate_cap,
            "stop_reason": self.stop_reason,
            "reserve_activation_reason": self.reserve_activation_reason,
            "was_reserve": self.was_reserve,
            "plan_id": self.plan_id,
            "plan_item_id": self.plan_item_id,
        }


class CollectionOrchestrator:
    """Runs one collection task from search to stored clips."""

    def __init__(
        self,
        dependencies: OrchestratorDependencies,
        *,
        keyword_expander: KeywordExpander | None = None,
        on_event: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.deps = dependencies
        self.keyword_expander = keyword_expander or KeywordExpander()
        self.on_event = on_event
        self.cancel_event = cancel_event
        self.settings = dependencies.settings
        self._ai_calls_at_source_start = 0
        self._deadline = 0.0
        #: task row currently being processed (used when the run is interrupted)
        self.last_task_id: int | None = None

    # -- public API --------------------------------------------------------
    async def collect(
        self,
        request: TaskRequest,
        *,
        resume_task_id: int | None = None,
    ) -> PipelineResult:
        """Execute the full workflow and return the produced clips.

        ``resume_task_id`` continues an earlier task: the task row is reused and
        already processed sources/clips are skipped by the dedup policy.
        """

        started = time.perf_counter()
        collection = self.settings.collection
        self._deadline = started + max(1.0, collection.max_task_runtime_minutes) * 60.0
        stats = PipelineStats()
        messages: list[str] = []
        clips: list[ClipRecord] = []
        reports: list[SourceVideoReport] = []
        library = self.deps.library
        pipeline = self.settings.pipeline
        cache_dir = self.settings.paths.cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        staged: list[Path] = []

        queries = self._plan_queries(request)
        stats.queries_generated = len(queries)

        if resume_task_id is not None:
            task_id = resume_task_id
            existing = library.clips_for_task(task_id)
            clips.extend(existing)
            library.update_task_request(task_id, request)
            library.update_task_status(
                task_id, TaskStatus.RUNNING, error=f"runner_pid:{os.getpid()}"
            )
            messages.append(f"继续任务 #{task_id}，已有 {len(existing)} 个片段")
        else:
            task_id = library.create_task(
                request,
                status=TaskStatus.RUNNING,
                error=f"runner_pid:{os.getpid()}",
            )
        self.last_task_id = task_id

        result_status = TaskStatus.SUCCEEDED
        seen_video_ids: set[str] = {clip.platform_video_id for clip in clips if clip.platform_video_id}
        seen_urls: set[str] = set()
        reported_ids: set[str] = set(seen_video_ids)
        considered = 0
        query_audit: list[dict] = []
        provider_circuit = False
        stop_reason = ""
        discovery_blocked = False
        discovery_block_detail = ""

        prior_yields = (
            library.list_search_yields(task_id=task_id, limit=10000)
            if resume_task_id is not None
            else []
        )
        completed_queries = {
            str(row.get("query") or "")
            for row in prior_yields
            if str(row.get("stop_reason") or "") == "completed"
        }
        remaining_queries = [query for query in queries if query not in completed_queries]
        # Once a complete query cycle is exhausted, a later resume is a refresh:
        # rerun the cycle so newly published search results can still be found.
        if completed_queries and remaining_queries:
            queries = remaining_queries
            messages.append(f"断点续采已跳过 {len(completed_queries)} 个完成的搜索词")

        self._emit(
            stats,
            "start",
            f"任务 #{task_id}: {request.material} 目标 {request.target_clip_count} 个片段，"
            f"来源 {self.deps.source.platform}，生成 {len(queries)} 个搜索词",
        )

        try:
            for query in queries:
                if self._should_stop(clips, request) or self._budget_reason(stats, clips):
                    break
                yield_row = _QueryYield(
                    query=query,
                    query_family=str(request.query_family or ""),
                    planned_order=int(request.planned_order or 0),
                    candidate_cap=int(request.candidate_cap or 0),
                    reserve_activation_reason=str(request.reserve_activation_reason or ""),
                    was_reserve=bool(request.was_reserve),
                    plan_id=request.plan_id,
                    plan_item_id=request.plan_item_id,
                )
                clips_before = len(clips)
                query_exhausted = False
                try:
                    candidates = await self.deps.source.search(
                        query, self._search_limit(request)
                    )
                except DiscoveryBlockedError as exc:
                    # no discovery backend can execute: stop the search phase
                    # instead of repeating the same failure for every keyword
                    discovery_blocked = True
                    discovery_block_detail = str(exc)
                    messages.append(f"发现被阻断（discovery_blocked）: {exc}")
                    self._emit(
                        stats,
                        "discovery_blocked",
                        f"发现被阻断，停止搜索: {exc}",
                        level="warning",
                    )
                    break
                except Exception as exc:
                    stats.errors += 1
                    messages.append(f"搜索失败 {query}: {exc}")
                    LOGGER.warning("search failed for %r: %s", query, exc)
                    self._emit(
                        stats, "search_failed", f"搜索失败: {query} ({exc})", level="warning"
                    )
                    continue

                yield_row.candidates = len(candidates)
                stats.searched_candidates += len(candidates)
                self._emit(stats, "search", f"关键词「{query}」返回 {len(candidates)} 个候选")

                for candidate in candidates:
                    if self._should_stop(clips, request):
                        yield_row.stop_reason = "target_reached"
                        break
                    reason = self._budget_reason(stats, clips)
                    if reason:
                        stop_reason = reason
                        yield_row.stop_reason = "budget"
                        break
                    if self._max_candidates_reached(considered):
                        stop_reason = "达到候选上限，提前结束搜索"
                        yield_row.stop_reason = "candidate_budget_exhausted"
                        break
                    considered += 1
                    stats.examined_candidates += 1
                    was_unique = candidate.dedup_key not in seen_video_ids
                    if was_unique:
                        yield_row.unique += 1
                        stats.unique_candidates += 1
                    else:
                        yield_row.current_run_duplicate += 1
                        stats.current_run_duplicate += 1
                    known = library.get_source_video(
                        candidate.platform, candidate.platform_video_id
                    )
                    if known is None:
                        yield_row.new_to_system += 1
                        stats.new_to_system += 1
                    else:
                        yield_row.known_source += 1
                        stats.known_source += 1
                        if known.status in TERMINAL_STATUSES:
                            yield_row.already_processed += 1
                            stats.already_processed += 1
                        if known.id is not None and library.clips_for_source_video(known.id):
                            yield_row.already_represented += 1
                            stats.already_represented += 1

                    report, saved = await self._process_candidate(
                        candidate=candidate,
                        query=query,
                        request=request,
                        task_id=task_id,
                        stats=stats,
                        clips=clips,
                        staged=staged,
                        seen_video_ids=seen_video_ids,
                        seen_urls=seen_urls,
                        messages=messages,
                        yield_row=yield_row,
                    )
                    if report.platform_video_id not in reported_ids:
                        reports.append(report)
                        reported_ids.add(report.platform_video_id)
                    if saved:
                        yield_row.clips += 1
                    if getattr(self.deps.gateway, "circuit_open", False):
                        provider_circuit = True
                        stop_reason = "provider_unavailable"
                        yield_row.stop_reason = "provider_unavailable"
                        break
                else:
                    query_exhausted = True

                yield_row.clips = max(yield_row.clips, len(clips) - clips_before)
                if query_exhausted and not yield_row.stop_reason:
                    yield_row.stop_reason = "completed"
                yield_row.actual_order = len(prior_yields) + len(query_audit) + 1
                self._persist_yield(task_id, yield_row)
                query_audit.append(yield_row.as_audit())
                if stop_reason:
                    messages.append(stop_reason)
                    break
                if provider_circuit:
                    break
                if self._cancelled():
                    result_status = TaskStatus.CANCELLED
                    messages.append("任务已取消")
                    break

            if self._cancelled():
                result_status = TaskStatus.CANCELLED
            elif discovery_blocked:
                result_status = TaskStatus.PARTIAL
            elif len(clips) >= request.target_clip_count:
                result_status = TaskStatus.SUCCEEDED
            elif clips:
                result_status = TaskStatus.PARTIAL
                messages.append(
                    f"仅采集到 {len(clips)}/{request.target_clip_count} 个片段"
                )
            else:
                result_status = TaskStatus.PARTIAL
                messages.append("没有找到可用片段")
        except Exception as exc:  # pragma: no cover - last resort safety net
            LOGGER.exception("collection task failed")
            result_status = TaskStatus.FAILED
            stats.errors += 1
            messages.append(f"任务异常: {exc}")
            library.update_task_status(task_id, result_status, error=str(exc))
        except (asyncio.CancelledError, KeyboardInterrupt):
            # Ctrl+C / programmatic cancellation: clean up and mark the task
            LOGGER.warning("collection task cancelled by the operator")
            result_status = TaskStatus.CANCELLED
            messages.append("任务被中断，已清理临时文件")
            raise
        finally:
            try:
                messages.extend(await self._cleanup_cache(staged))
            finally:
                await self._close_sources()
            if result_status is TaskStatus.CANCELLED:
                released = library.release_in_progress_sources(task_id)
                if released:
                    messages.append(f"已释放 {released} 个中断中的来源视频，可立即重试")
            if result_status is not TaskStatus.FAILED:
                library.update_task_status(task_id, result_status)

        elapsed = round(time.perf_counter() - started, 3)
        self._emit(
            stats,
            "done",
            f"任务 #{task_id} 结束: 保存 {len(clips)} 个片段，耗时 {elapsed:.2f}s",
        )
        discovery_backend = str(getattr(self.deps.source, "last_discovery_backend", "") or "")
        discovery_status = str(getattr(self.deps.source, "last_search_status", "") or "")
        discovery_detail = ""
        if discovery_status and discovery_status not in ("ok", ""):
            notes = list(getattr(self.deps.source, "last_search_notes", []) or [])
            discovery_detail = notes[-1] if notes else discovery_status
            messages.append(f"发现方式状态: {discovery_status}（{discovery_detail}）")
        discovery_states: dict[str, str] = {}
        try:
            state_getter = getattr(self.deps.source, "discovery_state", None)
            if state_getter is not None:
                discovery_states = await state_getter()
        except Exception as exc:  # pragma: no cover - reporting must not fail a task
            LOGGER.debug("could not collect discovery state: %s", exc)
        if discovery_blocked:
            discovery_status = BrowserSearchStatus.DISCOVERY_BLOCKED.value
            discovery_detail = discovery_block_detail or discovery_detail
        circuit = (
            self.deps.gateway.circuit_state()
            if hasattr(self.deps.gateway, "circuit_state")
            else {}
        )
        gateway_stats = getattr(self.deps.gateway, "stats", None)
        if gateway_stats is not None:
            stats.provider_circuit_trips = int(
                getattr(gateway_stats, "circuit_trips", 0)
            )
            stats.provider_calls_skipped = int(
                getattr(gateway_stats, "skipped_circuit", 0)
            )
        return PipelineResult(
            task_id=task_id,
            material=request.material,
            status=result_status,
            queries=queries,
            stats=stats,
            clips=clips,
            source_videos=reports,
            ai_usage=self.deps.gateway.stats.usage_summary(),
            discovery_backend=discovery_backend,
            discovery_status=discovery_status,
            discovery_detail=discovery_detail,
            discovery_blocked=discovery_blocked,
            discovery_states=discovery_states,
            provider_unavailable=bool(circuit.get("open")),
            provider_failure_class=str(circuit.get("failure_class") or ""),
            provider_failure_subtype=str(circuit.get("failure_subtype") or ""),
            provider_model=str(circuit.get("model") or ""),
            provider_operation=str(circuit.get("operation") or ""),
            provider_detail=str(circuit.get("detail") or ""),
            messages=messages,
            query_audit=query_audit,
            elapsed_seconds=elapsed,
        )

    # -- one candidate ------------------------------------------------------
    async def _process_candidate(
        self,
        *,
        candidate: VideoCandidate,
        query: str,
        request: TaskRequest,
        task_id: int,
        stats: PipelineStats,
        clips: list[ClipRecord],
        staged: list[Path],
        seen_video_ids: set[str],
        seen_urls: set[str],
        messages: list[str],
        yield_row: _QueryYield,
    ) -> tuple[SourceVideoReport, bool]:
        report = SourceVideoReport(
            platform=candidate.platform,
            platform_video_id=candidate.platform_video_id,
            title=candidate.title,
            source_author=candidate.author,
            discovery_backend=str(candidate.metadata.get("discovery") or ""),
            source_url=candidate.source_url,
            source_path=str(candidate.metadata.get("file_path", "")),
            duration=candidate.duration,
            status=SourceVideoStatus.DISCOVERED,
            matched_queries=list(candidate.matched_queries),
        )
        library = self.deps.library

        # --- local metadata prefilter (section 13/14) ---------------------
        decision = self.deps.candidate_filter.evaluate(
            candidate,
            material=request.material,
            seen_video_ids=seen_video_ids,
            seen_urls=seen_urls,
        )
        if not decision.accepted:
            self._count_rejection(stats, decision.reason)
            bookkeeping = self._is_rediscovery(
                library, candidate, decision.reason
            )
            if bookkeeping:
                # Meeting the video again (another keyword, another task) is
                # *discovery bookkeeping*: merge the query, never overwrite the
                # stored content verdict and never write a misleading
                # "rejected [duplicate_video]" row (section 1 / 20).
                stored = library.merge_discovery(
                    platform=candidate.platform,
                    platform_video_id=candidate.platform_video_id,
                    query=query,
                    source_url=candidate.source_url,
                    title=candidate.title,
                    author=candidate.author,
                )
                report.status = (
                    stored.status if stored is not None else SourceVideoStatus.DISCOVERED
                )
                report.reject_reason = stored.reject_reason if stored is not None else None
                report.matched_queries = (
                    list(stored.matched_queries) if stored is not None else [query]
                )
                report.preview_detail = f"rediscovered ({decision.reason})"
                self._emit(
                    stats,
                    "rediscovered",
                    f"重复发现 [{decision.reason}] {self._label(candidate)}"
                    f"（保留原有结论 {report.status}"
                    + (f"/{report.reject_reason}" if report.reject_reason else "")
                    + "）",
                    level="info",
                )
                return report, False

            report.status = SourceVideoStatus.REJECTED
            report.reject_reason = decision.reason
            report.preview_detail = decision.detail
            library.upsert_source_video(
                task_id=task_id,
                platform=candidate.platform,
                platform_video_id=candidate.platform_video_id,
                source_url=candidate.source_url,
                title=candidate.title,
                author=candidate.author,
                author_id=candidate.author_id,
                cover_url=candidate.cover_url,
                duration=candidate.duration,
                status=SourceVideoStatus.REJECTED,
                reject_reason=decision.reason,
                matched_queries=candidate.matched_queries,
                statistics=candidate.statistics,
                # section 25: record the attempt even when the *local* filter
                # removed the candidate, so attempt_count/last_attempt_at show
                # when we last looked at this video
                touch_attempt=True,
            )
            self._emit(
                stats,
                "candidate_rejected",
                f"本地淘汰 [{decision.reason}] {self._label(candidate)}: {decision.detail}",
            )
            return report, False

        if decision.duration_unknown:
            # Milestone 9.1: discovery gave no duration - probe the real media
            # instead of mislabelling it as "duration_out_of_range".
            duration, probe_state, probe_detail = await self._resolve_duration(
                candidate, stats=stats, staged=staged
            )
            resolved_media = str(candidate.metadata.get("resolved_media_url") or "")
            if resolved_media:
                candidate = candidate.model_copy(update={"media_url": resolved_media})
            if duration is None:
                reason = (
                    RejectReason.INVALID_MEDIA_SOURCE
                    if probe_state == "invalid_media_source"
                    else RejectReason.UNREACHABLE
                    if probe_state == "media_unreachable"
                    else RejectReason.DURATION_UNKNOWN_UNRESOLVED
                )
                source_status = (
                    SourceVideoStatus.FAILED_MEDIA
                    if probe_state
                    in (
                        "duration_unknown_unresolved",
                        "media_unreachable",
                        "invalid_media_source",
                    )
                    else SourceVideoStatus.REJECTED
                )
                self._count_rejection(stats, reason)
                return self._store_candidate_rejection(
                    candidate=candidate,
                    task_id=task_id,
                    stats=stats,
                    report=report,
                    reason=reason,
                    detail=probe_detail,
                    library=library,
                    status=source_status,
                ), False
            candidate = candidate.model_copy(update={"duration": duration})
            limit_ok, limit_detail = self._duration_within_range(candidate, request)
            if not limit_ok:
                self._count_rejection(stats, RejectReason.DURATION_OUT_OF_RANGE)
                return self._store_candidate_rejection(
                    candidate=candidate,
                    task_id=task_id,
                    stats=stats,
                    report=report,
                    reason=RejectReason.DURATION_OUT_OF_RANGE,
                    detail=limit_detail,
                    library=library,
                ), False

        seen_video_ids.add(candidate.dedup_key)
        seen_urls.add(candidate.source_url)
        source_video_id = library.upsert_source_video(
            task_id=task_id,
            platform=candidate.platform,
            platform_video_id=candidate.platform_video_id,
            source_url=candidate.source_url,
            title=candidate.title,
            author=candidate.author,
            author_id=candidate.author_id,
            cover_url=candidate.cover_url,
            publish_time=candidate.published_at.isoformat() if candidate.published_at else None,
            duration=candidate.duration,
            status=SourceVideoStatus.METADATA_CHECKED,
            media_url=candidate.media_url,
            matched_queries=candidate.matched_queries,
            statistics=candidate.statistics,
            touch_attempt=True,
        )
        report.status = SourceVideoStatus.METADATA_CHECKED

        # --- preview ------------------------------------------------------
        preview_decision, staged_path, media = await self._acquire_preview(
            candidate=candidate,
            query=query,
            request=request,
            task_id=task_id,
            source_video_id=source_video_id,
            stats=stats,
            staged=staged,
            messages=messages,
            report=report,
        )
        report.preview_accept = preview_decision.accepted if preview_decision else None
        if preview_decision is not None:
            report.preview_reason = preview_decision.reason
            report.preview_detail = preview_decision.detail

        if preview_decision is None or not preview_decision.accepted:
            # Milestone 9 invariant: "the AI produced no verdict" is an
            # execution failure, never a content rejection.  A provider error
            # must not consume a 30-day rejection window (dedup retries
            # failed_* after hours, not days) and must not be reported as a
            # content judgement.
            provider_failed = preview_decision is not None and bool(
                getattr(preview_decision, "ai_failed", False)
            )
            if provider_failed:
                stats.errors += 1
                report.status = SourceVideoStatus.FAILED_AI
                report.reject_reason = None
            else:
                if preview_decision is not None:
                    self._count_rejection(
                        stats, preview_decision.reason, preview_decision.subtitle_rejected
                    )
                report.status = (
                    SourceVideoStatus.FAILED_PREVIEW
                    if preview_decision is None
                    else SourceVideoStatus.REJECTED_PREVIEW
                )
                report.reject_reason = (
                    preview_decision.reason if preview_decision else RejectReason.OTHER
                )
            library.upsert_source_video(
                task_id=task_id,
                platform=candidate.platform,
                platform_video_id=candidate.platform_video_id,
                source_url=candidate.source_url,
                title=candidate.title,
                author=candidate.author,
                duration=candidate.duration,
                status=report.status,
                reject_reason=report.reject_reason,
                preview_material_score=preview_decision.material_score if preview_decision else None,
                preview_subtitle_score=preview_decision.subtitle_score if preview_decision else None,
                preview_quality_score=preview_decision.quality_score if preview_decision else None,
                matched_queries=candidate.matched_queries,
            )
            if staged_path is not None:
                self._discard_source(staged_path)
            self._cleanup_frames(candidate.platform_video_id)
            if provider_failed:
                self._emit(
                    stats,
                    "preview_unavailable",
                    f"预筛不可用（AI 提供商错误，不计为内容淘汰） {self._label(candidate)}",
                )
            else:
                self._emit(
                    stats,
                    "preview_rejected",
                    f"预筛淘汰 [{report.reject_reason}] {self._label(candidate)}",
                )
            return report, False

        yield_row.accepted += 1
        library.upsert_source_video(
            task_id=task_id,
            platform=candidate.platform,
            platform_video_id=candidate.platform_video_id,
            source_url=candidate.source_url,
            title=candidate.title,
            author=candidate.author,
            duration=candidate.duration,
            status=SourceVideoStatus.QUALIFIED,
            preview_material_score=preview_decision.material_score,
            preview_subtitle_score=preview_decision.subtitle_score,
            preview_quality_score=preview_decision.quality_score,
            matched_queries=candidate.matched_queries,
        )

        # --- stage the source video ---------------------------------------
        if staged_path is None:
            staged_path = await self._stage_source(
                candidate=candidate,
                request=request,
                stats=stats,
                staged=staged,
                messages=messages,
                report=report,
                source_video_id=source_video_id,
            )
            if staged_path is None:
                self._cleanup_frames(candidate.platform_video_id)
                return report, False
            yield_row.downloads += 1

        if media is None:
            try:
                media = await self.deps.toolkit.probe(staged_path)
            except Exception as exc:
                stats.errors += 1
                messages.append(f"探测失败: {self._label(candidate)} ({exc})")
                report.status = SourceVideoStatus.FAILED_MEDIA
                library.update_source_video_status(
                    source_video_id, SourceVideoStatus.FAILED_MEDIA
                )
                self._discard_source(staged_path)
                self._cleanup_frames(candidate.platform_video_id)
                return report, False

        duration = float(media.duration or candidate.duration or 0.0)
        if duration <= 0:
            messages.append(f"无法读取时长: {self._label(candidate)}")
            library.update_source_video_status(
                source_video_id, SourceVideoStatus.FAILED_MEDIA
            )
            self._discard_source(staged_path)
            self._cleanup_frames(candidate.platform_video_id)
            return report, False
        report.duration = duration

        # --- scene boundaries + AI analysis --------------------------------
        boundaries: list[float] = []
        try:
            boundaries = await self.deps.scene_refiner.boundaries_for(staged_path)
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("scene detection failed for %s: %s", staged_path.name, exc)

        library.update_source_video_status(source_video_id, SourceVideoStatus.ANALYZING)
        if not self._ai_budget_available():
            # The per-source budget guards the expensive analysis stage; the
            # task level budget already bounds the per-clip tagging calls.
            messages.append(f"AI 调用预算用尽，跳过深度分析: {self._label(candidate)}")
            library.update_source_video_status(source_video_id, SourceVideoStatus.FAILED_AI)
            report.status = SourceVideoStatus.FAILED_AI
            self._discard_source(staged_path)
            self._cleanup_frames(candidate.platform_video_id)
            return report, False
        segments = await self.deps.analyzer.analyze(
            video_path=staged_path,
            duration=duration,
            material=request.material,
            query=query,
            platform=candidate.platform,
            platform_video_id=candidate.platform_video_id,
            title=candidate.title,
            frames_dir=self.settings.paths.cache_dir / "frames",
            context=dict(candidate.metadata),
            audit=AuditContext(task_id=task_id, source_video_id=source_video_id),
            boundaries=boundaries,
            target_process_stage=(
                str(request.target_process_stage) if request.target_process_stage else ""
            ),
        )
        stats.analyzed += 1
        stats.segments_found += len(segments)
        if not segments:
            library.update_source_video_status(
                source_video_id,
                SourceVideoStatus.NO_USABLE_SEGMENT,
                reject_reason=RejectReason.NO_USABLE_SEGMENT,
            )
            report.status = SourceVideoStatus.NO_USABLE_SEGMENT
            report.reject_reason = RejectReason.NO_USABLE_SEGMENT
            self._discard_source(staged_path)
            self._cleanup_frames(candidate.platform_video_id)
            self._emit(stats, "no_segment", f"未发现有效片段: {self._label(candidate)}")
            return report, False

        self._emit(
            stats, "segments", f"{self._label(candidate)} 发现 {len(segments)} 个候选时间段"
        )

        provenance = self._provenance(candidate)
        saved_here = 0
        segment_reports: list[SegmentReport] = []
        for segment in segments:
            if self._should_stop(clips, request) or self._budget_reason(stats, clips):
                break
            segment_report = SegmentReport(
                start=round(segment.start, 3),
                end=round(segment.end, 3),
                description=segment.description,
                material_relevance=segment.material_relevance,
            )
            clip = await self._process_segment(
                segment=segment,
                candidate=candidate,
                query=query,
                request=request,
                task_id=task_id,
                source_video_id=source_video_id,
                video_path=staged_path,
                duration=duration,
                boundaries=boundaries,
                has_audio=media.has_audio,
                stats=stats,
                messages=messages,
                segment_report=segment_report,
                provenance=provenance,
            )
            segment_reports.append(segment_report)
            if clip is not None:
                clips.append(clip)
                saved_here += 1
                self._tag_diagnostics(clips, clip, stats=stats, messages=messages)
                self._emit(
                    stats,
                    "clip_saved",
                    f"保存片段 #{clip.id} {clip.process_stage} {clip.source_start:.2f}-"
                    f"{clip.source_end:.2f}s ({clip.duration:.1f}s) score={clip.overall_score:.2f}",
                )

        try:
            library.update_source_video_status(source_video_id, SourceVideoStatus.PROCESSED)
        except Exception:  # pragma: no cover - defensive
            LOGGER.warning("could not mark source video %s as processed", source_video_id)
        self._discard_source(staged_path)
        self._cleanup_frames(candidate.platform_video_id)
        report.status = SourceVideoStatus.PROCESSED
        report.segments_found = len(segments)
        report.clips_saved = saved_here
        report.ai_calls = self.deps.gateway.stats.calls - self._ai_calls_at_source_start
        report.segments = segment_reports
        return report, saved_here > 0

    # -- preview / download -------------------------------------------------
    async def _acquire_preview(
        self,
        *,
        candidate: VideoCandidate,
        query: str,
        request: TaskRequest,
        task_id: int,
        source_video_id: int,
        stats: PipelineStats,
        staged: list[Path],
        messages: list[str],
        report: SourceVideoReport,
    ):
        """Remote preview first; stage the video only when the source asks for it."""

        library = self.deps.library
        self._ai_calls_at_source_start = self.deps.gateway.stats.calls
        try:
            preview = await self.deps.source.get_preview(candidate.platform_video_id)
        except Exception as exc:
            stats.errors += 1
            messages.append(f"预览获取失败 {candidate.platform_video_id}: {exc}")
            library.update_source_video_status(
                source_video_id, SourceVideoStatus.FAILED_PREVIEW
            )
            return None, None, None

        staged_path: Path | None = None
        media: MediaInfo | None = None
        prefetched = candidate.metadata.get("prefetched_path")
        cached_media = Path(str(prefetched)) if prefetched else None
        if cached_media is not None and not cached_media.exists():
            cached_media = None
        if cached_media is not None or (
            not preview.frames and preview.metadata.get("defer_to_download")
        ):
            # Remote preview disabled: download once, then sample locally.  A
            # rejected candidate is deleted again right away (section 16).
            if cached_media is not None:
                # Milestone 9.6: reuse the media that duration resolution
                # already materialized instead of fetching it again.
                staged_path = cached_media
                if cached_media not in staged:
                    staged.append(cached_media)
                stats.probe_cache_reused += 1
                candidate.metadata["prefetched_reused"] = True
            else:
                staged_path = await self._stage_source(
                    candidate=candidate,
                    request=request,
                    stats=stats,
                    staged=staged,
                    messages=messages,
                    report=report,
                    source_video_id=source_video_id,
                )
            if staged_path is None:
                return None, None, None
            try:
                media = await self.deps.toolkit.probe(staged_path)
            except Exception as exc:
                messages.append(f"探测失败: {self._label(candidate)} ({exc})")
                return None, staged_path, None
            duration = float(media.duration or candidate.duration or 0.0)
            sampler = FrameSampler(
                self.deps.toolkit,
                max_frames=self.deps.preview_filter.max_frames,
                max_width=self.settings.analysis.preview_max_width,
                strategy=self.settings.analysis.sampling_strategy,
            )
            preview_frames = self._preview_frame_count(duration)
            preview.frames = await sampler.sample(
                staged_path,
                duration,
                preview_frames,
                f"{candidate.platform_video_id}_preview",
                self.settings.paths.cache_dir / "previews",
                max_width=self.settings.analysis.preview_max_width,
            )
            preview.duration = duration

        library.update_source_video_status(source_video_id, SourceVideoStatus.PREVIEWING)
        # measured subtitle evidence from the same frames (section 23/27)
        measured = await self._measure_subtitles(
            preview.frames,
            key=f"source:{candidate.platform}:{candidate.platform_video_id}",
            recognize=False,
            preview=True,
        )
        if measured is not None:
            report.subtitle_analysis = measured.model_dump(mode="json")
            try:
                library.save_source_subtitle_analysis(
                    candidate.platform, candidate.platform_video_id, measured
                )
            except Exception as exc:  # pragma: no cover - persistence is best effort
                LOGGER.debug("could not persist the source subtitle analysis: %s", exc)
        decision = await self.deps.preview_filter.evaluate(
            candidate=candidate,
            material=request.material,
            query=query,
            preview=preview,
            audit=AuditContext(task_id=task_id, source_video_id=source_video_id),
            subtitle=measured,
        )
        stats.prescreened += 1
        return decision, staged_path, media

    async def _stage_source(
        self,
        *,
        candidate: VideoCandidate,
        request: TaskRequest,
        stats: PipelineStats,
        staged: list[Path],
        messages: list[str],
        report: SourceVideoReport,
        source_video_id: int | None = None,
    ) -> Path | None:
        """Download/stage the media, refreshing an expired URL once."""

        prefetched = candidate.metadata.get("prefetched_path")
        if prefetched:
            cached = Path(str(prefetched))
            if cached.exists() and cached.stat().st_size > 0:
                # Milestone 9.6: the controlled duration probe already
                # materialized this exact media file; never fetch it twice.
                stats.probe_cache_reused += 1
                if cached not in staged:
                    staged.append(cached)
                candidate.metadata["prefetched_reused"] = True
                report.status = SourceVideoStatus.DOWNLOADING
                return cached
        library = self.deps.library
        target = self._cache_path(candidate)
        attempts = 1 + max(0, self.settings.sources.douyin.media_url_refresh_attempts)
        for attempt in range(1, attempts + 1):
            url = await self._resolve_download_url(candidate)
            try:
                await self.deps.downloader.download(
                    url,
                    target,
                    metadata={
                        "duration": candidate.duration,
                        "platform_video_id": candidate.platform_video_id,
                    },
                )
                stats.downloads += 1
                staged.append(target)
                report.status = SourceVideoStatus.DOWNLOADING
                return target
            except MediaUrlExpiredError as exc:
                messages.append(
                    f"media URL 过期，刷新后重试 ({attempt}/{attempts}): {self._label(candidate)}"
                )
                LOGGER.info("media url expired, refreshing: %s", exc)
                continue
            except DownloadError as exc:
                stats.errors += 1
                messages.append(f"获取源视频失败 {candidate.platform_video_id}: {exc}")
                report.status = SourceVideoStatus.FAILED_DOWNLOAD
                target_id = source_video_id or self._source_video_id(candidate)
                if target_id:
                    library.update_source_video_status(
                        target_id, SourceVideoStatus.FAILED_DOWNLOAD
                    )
                return None
        stats.errors += 1
        messages.append(f"media URL 刷新后仍不可用: {self._label(candidate)}")
        return None

    # -- one segment --------------------------------------------------------
    async def _process_segment(
        self,
        *,
        segment: DetectedSegment,
        candidate: VideoCandidate,
        query: str,
        request: TaskRequest,
        task_id: int,
        source_video_id: int,
        video_path: Path,
        duration: float,
        boundaries: list[float],
        has_audio: bool,
        stats: PipelineStats,
        messages: list[str],
        segment_report: SegmentReport | None = None,
        provenance: dict | None = None,
    ) -> ClipRecord | None:
        library = self.deps.library
        timing = self.deps.scene_refiner.refine(segment, boundaries, duration=duration)
        if segment_report is not None:
            segment_report.refined_start = timing.start
            segment_report.refined_end = timing.end
        timing = self._clamp_timing(timing, request)
        if timing is None:
            if segment_report is not None:
                segment_report.reject_reason = RejectReason.SEGMENT_TOO_SHORT
            return None

        content_key = ClipCutter.compute_content_key(
            request.material, segment.description, timing.duration
        )
        # The physical folder is the task's library category, never the
        # AI-observed form/state (Milestone 3.7, sections 18/19).
        library_category = self._library_category(request)
        clip_path, thumbnail_path = library.next_clip_paths(library_category)
        tagging_frames_dir = self._clip_frames_dir(clip_path.stem)
        try:
            artifact = await self.deps.clipper.cut(
                source_video=video_path,
                timing=timing,
                dest=clip_path,
                thumbnail_dest=thumbnail_path,
                content_key=content_key,
                has_audio=has_audio,
                scratch_dir=self.settings.paths.cache_dir / "thumbnails",
                tagging_frames_dir=tagging_frames_dir,
            )
        except Exception as exc:
            stats.errors += 1
            messages.append(f"切片失败 {candidate.platform_video_id}: {exc}")
            LOGGER.warning("cutting failed for %s: %s", candidate.platform_video_id, exc)
            return None

        duplicate = self.deps.dedup.find_duplicate(
            sha256=artifact.sha256, phash=artifact.phash, material=request.material
        )
        if duplicate is not None:
            self._discard_files(artifact)
            stats.duplicates_rejected += 1
            if segment_report is not None:
                segment_report.reject_reason = RejectReason.DUPLICATE_CLIP
            self._emit(
                stats,
                "duplicate",
                f"去重淘汰 [{duplicate.detector}] 与片段 #{duplicate.clip_id} 重复 "
                f"({segment.description[:18]})",
            )
            return None

        tagging: ClipTagging | None = None
        tagging_record = None
        clip_subtitle = None
        clip_frames = self._tagging_frames(artifact)
        try:
            tagging = await self.deps.gateway.tag_clip(
                ClipTaggingRequest(
                    # ``material``/``requested_material`` are task intent; the
                    # tagger sees the final clip frames and decides what is
                    # actually visible (Milestone 3.7, sections 1/2).
                    material=request.material,
                    requested_material=request.material,
                    query=query,
                    platform=candidate.platform,
                    platform_video_id=candidate.platform_video_id,
                    title=candidate.title,
                    duration=duration,
                    context=dict(candidate.metadata),
                    start=timing.start,
                    end=timing.end,
                    segment_description=segment.description,
                    segment_relevance=segment.material_relevance,
                    frames=clip_frames,
                    audit=AuditContext(task_id=task_id, source_video_id=source_video_id),
                )
            )
            tagging_record = self.deps.gateway.last_record
            # Milestone 6: measure the same frames *before* they are cleaned up
            # (the tagger and the subtitle analyzer share one extraction pass).
            clip_subtitle = await self._measure_subtitles(
                clip_frames, key=f"clip:{artifact.sha256 or clip_path.stem}"
            )
        finally:
            self._discard_frames_dir(tagging_frames_dir)
        if tagging is None:
            stats.errors += 1
            messages.append(f"标签生成失败，使用降级标签: {segment.description[:20]}")
            tagging = self._fallback_tagging(request.material, segment)
        # the measured subtitle result is authoritative for the final clip's
        # subtitle fields when it is available (sections 26/29)
        if clip_subtitle is not None and not clip_subtitle.is_unavailable:
            tagging = tagging.model_copy(
                update={
                    "subtitle_type": clip_subtitle.classification,
                    "subtitle_score": round(1.0 - clip_subtitle.cleanliness_score, 3),
                }
            )
            measured_scores = tagging.scores.model_copy(
                update={"subtitle_cleanliness": clip_subtitle.cleanliness_score}
            )
            tagging = tagging.model_copy(update={"scores": measured_scores})
            if classification_bucket(clip_subtitle.classification) == "complex":
                self._emit(
                    stats,
                    "subtitle_measured",
                    f"字幕测量判定为复杂: {clip_subtitle.classification} "
                    f"(洁净度 {clip_subtitle.cleanliness_score:.2f}, "
                    f"{clip_subtitle.decision_source})",
                    level="debug",
                )
        # One canonical material ontology for every tagging path (section 18):
        # the library folder stays 苹果干, the structured tag becomes
        # material=苹果 + material_state=dried.
        tagging = normalize_tagging(tagging, request_material=request.material)

        if (
            request.target_process_stage is not None
            and tagging.process_stage != request.target_process_stage
        ):
            self._discard_files(artifact)
            stats.quality_rejected += 1
            if segment_report is not None:
                segment_report.reject_reason = RejectReason.TARGET_STAGE_MISMATCH
            self._emit(
                stats,
                "stage_rejected",
                f"工序淘汰 [{tagging.process_stage}] 目标 {request.target_process_stage}",
            )
            return None

        gate = self.deps.quality_gate.evaluate(
            tagging=tagging,
            duration=timing.duration,
            min_duration=request.min_clip_duration,
            max_duration=request.max_clip_duration,
            required_material=library_category,
        )
        if not gate.accepted:
            self._discard_files(artifact)
            self._count_rejection(stats, gate.reason, gate.subtitle_rejected, quality=True)
            if segment_report is not None:
                segment_report.reject_reason = gate.reason
            self._emit(stats, "gate_rejected", f"质检淘汰 [{gate.reason}] {gate.detail}")
            return None

        provenance = provenance or {}
        exit_provenance = self._provenance_label(candidate)
        try:
            clip_id = library.insert_clip(
                task_id=task_id,
                source_video_id=source_video_id,
                platform=candidate.platform,
                platform_video_id=candidate.platform_video_id,
                source_url=candidate.source_url,
                tagging=tagging,
                timing=timing,
                artifact=artifact,
                content_key=content_key,
                source_title=str(provenance.get("source_title") or candidate.title or ""),
                source_author=str(provenance.get("source_author") or candidate.author or ""),
                source_author_id=provenance.get("source_author_id") or candidate.author_id,
                source_publish_time=provenance.get("source_publish_time"),
                library_category=library_category,
                provenance=exit_provenance,
                tag_prompt_version=self._tag_prompt_version(tagging_record),
                subtitle_analysis_json=(
                    json.dumps(clip_subtitle.model_dump(mode="json"), ensure_ascii=False)
                    if clip_subtitle is not None
                    else ""
                ),
            )
        except Exception as exc:
            stats.errors += 1
            messages.append(f"入库失败 {clip_path.name}: {exc}")
            LOGGER.exception("inserting clip failed")
            self._discard_files(artifact)
            return None

        # section 16: the tagging audit row is written before the clip exists,
        # so link it now that the clip id is known
        if tagging_record is not None and tagging_record.row_id:
            try:
                library.update_ai_run_clip(tagging_record.row_id, clip_id)
            except Exception as exc:  # pragma: no cover - audit must not fail a clip
                LOGGER.debug("could not link ai_run %s to clip %s: %s", tagging_record.row_id, clip_id, exc)

        stats.clips_saved += 1
        if segment_report is not None:
            segment_report.saved = True
        # exclude the clip we just stored: a self-match is not a similarity hit
        similar = [
            hit
            for hit in self.deps.dedup.find_similar(content_key=content_key, limit=4)
            if hit.clip_id != clip_id
        ]
        if similar:
            self._emit(
                stats,
                "similar",
                f"片段 #{clip_id} 与 #{similar[0].clip_id} 内容相似（保留，不视为重复）",
                level="debug",
            )
        return library.get_clip(clip_id)

    # -- helpers ------------------------------------------------------------
    def _library_category(self, request: TaskRequest) -> str:
        """Physical folder for this task's clips (section 18/19)."""

        return (request.library_category or request.material or "unknown").strip() or "unknown"

    def _clip_frames_dir(self, clip_stem: str) -> Path:
        """Where the final-clip tagging frames live while the clip is tagged."""

        return self.settings.paths.cache_dir / "frames" / f"clip_{clip_stem}"

    def _preview_frame_count(self, duration: float | None) -> int:
        """Adaptive preview cost (section 11), capped by the AI/preview limits."""

        analysis = self.settings.analysis
        ceiling = min(
            analysis.preview_max_frames,
            self.settings.ai.limits.max_preview_frames,
            self.deps.preview_filter.max_frames,
        )
        if not analysis.preview_frame_adaptive:
            return max(1, ceiling)
        return preview_frame_count(duration, bands=analysis.preview_bands(), ceiling=ceiling)

    @staticmethod
    def _tagging_frames(artifact: ClipArtifact) -> list[PreviewFrame]:
        """Adapt the cutter's frame files to the provider request model."""

        return [
            PreviewFrame(timestamp=0.0, image_path=path, source="clip")
            for path in artifact.tagging_frames
        ]

    @staticmethod
    def _discard_frames_dir(path: Path | None) -> None:
        if path is None:
            return
        try:
            if path.exists():
                for file in path.rglob("*"):
                    if file.is_file():
                        file.unlink(missing_ok=True)
                path.rmdir()
        except OSError as exc:  # pragma: no cover - defensive
            LOGGER.debug("could not remove tagging frames %s: %s", path, exc)

    @staticmethod
    def _tag_prompt_version(record) -> str:
        return str(getattr(record, "prompt_version", "") or "")

    async def _measure_subtitles(
        self, frames, *, key: str, recognize: bool = True, preview: bool = False
    ):
        """Measure text regions on a frame set (Milestone 6).

        Returns ``None`` when the analyzer is disabled or unavailable, in which
        case the pipeline keeps using the VLM subtitle verdict.
        """

        analyzer = self.deps.subtitle_analyzer
        if analyzer is None or not frames:
            return None
        try:
            limit = (
                analyzer.settings.preview_max_frames if preview else analyzer.settings.max_frames
            )
            return await analyzer.analyze_frames(
                frames, cache_key=key, recognize=recognize, max_frames=limit
            )
        except Exception as exc:  # pragma: no cover - measurement must never break a task
            LOGGER.warning("subtitle analysis failed for %s: %s", key, exc)
            return None

    @staticmethod
    def _provenance_label(candidate: VideoCandidate) -> str:
        """mock / local_test / douyin_real, derived from data (section 23)."""

        adapter = str(candidate.metadata.get("source_adapter") or "")
        return classify_provenance(
            candidate.platform,
            candidate.platform_video_id,
            source_adapter=adapter,
            source_url=candidate.source_url,
        )

    def _tag_diagnostics(
        self,
        clips: list[ClipRecord],
        clip: ClipRecord,
        *,
        stats: PipelineStats,
        messages: list[str],
    ) -> None:
        """Flag suspiciously identical tagging output (section 5/27).

        This is a *diagnostic*, never a rejection: genuinely similar footage may
        legitimately produce similar tags.
        """

        if not clip.description:
            return
        earlier = [item for item in clips if item.id != clip.id]
        if not earlier:
            return
        same_description = [
            item for item in earlier if item.description and item.description == clip.description
        ]
        same_scores = [
            item
            for item in earlier
            if (
                item.material_score,
                item.visual_quality_score,
                item.subtitle_cleanliness_score,
                item.stability_score,
                item.composition_score,
                item.overall_score,
            )
            == (
                clip.material_score,
                clip.visual_quality_score,
                clip.subtitle_cleanliness_score,
                clip.stability_score,
                clip.composition_score,
                clip.overall_score,
            )
        ]
        notes: list[str] = []
        if same_description:
            notes.append(f"与 #{same_description[0].id} 描述逐字相同")
        if same_scores:
            notes.append(f"与 #{same_scores[0].id} 全部分数完全相同")
        if not notes:
            return
        message = f"标签诊断（仅供参考，不影响入库）: clip #{clip.id} " + "、".join(notes)
        messages.append(message)
        self._emit(stats, "tag_diagnostic", message, level="warning")

    # -- Milestone 9.1: duration semantics ---------------------------------
    async def _resolve_duration(
        self,
        candidate: VideoCandidate,
        *,
        stats: PipelineStats,
        staged: list[Path] | None = None,
    ) -> tuple[float | None, str, str]:
        """Bounded duration-resolution ladder (Milestone 9.6).

        A: trusted metadata -> B: cached/dtK media URL -> C: remote ffprobe ->
        D: controlled cache materialization -> E: local ffprobe -> F: bounded
        decode.  A real download used here consumes the existing download
        budget and its file is reused downstream instead of fetched twice.
        """

        stats.duration_unknown += 1
        toolkit = self.deps.toolkit
        source = self.deps.source
        video_id = candidate.platform_video_id
        started = time.perf_counter()
        media_url = ""

        def audit(
            state: str,
            detail: str,
            method: str = "",
            *,
            media_url: str = "",
            error_category: str = "",
        ) -> None:
            library = getattr(self.deps, "library", None)
            if library is None:
                return
            host = ""
            try:
                from urllib.parse import urlparse

                host = urlparse(media_url).hostname or ""
            except Exception:  # pragma: no cover - defensive
                host = ""
            try:
                library.log_maintenance(
                    "duration_resolution",
                    target_type="source_video",
                    target_id=candidate.platform_video_id,
                    details={
                        "platform_video_id": candidate.platform_video_id,
                        "state": state,
                        "duration_method": method,
                        "detail": detail[:200],
                        "media_host": host,
                        "error_category": error_category,
                        "latency_ms": int((time.perf_counter() - started) * 1000),
                    },
                )
            except Exception as exc:  # pragma: no cover - audit must not fail a task
                LOGGER.debug("duration audit write failed: %s", exc)

        def finish(duration: float, state: str, detail: str, method: str) -> tuple[float, str, str]:
            stats.duration_unknown_resolved += 1
            stats.probe_latency_ms += int((time.perf_counter() - started) * 1000)
            if method == "remote_stream" or method == "local_stream":
                if method.startswith("remote"):
                    stats.duration_remote_probe_resolved += 1
                else:
                    stats.duration_local_probe_resolved += 1
            elif method == "frame_derived":
                stats.duration_frame_derived += 1
            elif method.startswith("remote"):
                stats.duration_remote_probe_resolved += 1
            elif method.startswith("local"):
                stats.duration_local_probe_resolved += 1
            candidate.metadata["duration_method"] = method
            audit(state, detail, method, media_url=media_url)
            return float(duration), state, detail

        # A. trusted metadata supplied by discovery
        if candidate.duration and float(candidate.duration) > 0:
            stats.duration_known_from_metadata += 1
            return finish(
                float(candidate.duration),
                "duration_unknown_resolved",
                f"trusted discovery metadata ({candidate.duration:.1f}s)",
                "metadata",
            )

        # B. media URL already present in the candidate payload / dtk detail
        for raw in (
            candidate.media_url,
            candidate.metadata.get("media_url"),
            candidate.metadata.get("play_url"),
        ):
            if raw:
                try:
                    media_url = validate_remote_media_url(str(raw))
                    break
                except RemoteMediaError:
                    continue
        if not media_url and isinstance(candidate.metadata.get("dtk_detail"), dict):
            from sources.douyin_models import extract_media_url

            try:
                extracted = extract_media_url(candidate.metadata["dtk_detail"])
                if extracted:
                    media_url = validate_remote_media_url(extracted)
            except RemoteMediaError:
                media_url = ""
        getter = getattr(source, "get_download_url", None)
        if not media_url and callable(getter):
            try:
                media_url = await getter(video_id)
            except Exception as exc:
                LOGGER.debug("no media url for %s: %s", video_id, exc)
        if media_url:
            candidate.metadata["resolved_media_url"] = media_url

        # C. remote ffprobe
        if media_url:
            try:
                info = await toolkit.probe_remote(media_url)
            except Exception as exc:
                LOGGER.debug("remote probe failed for %s: %s", video_id, exc)
                info = None
            if info is not None:
                if not info.has_video:
                    stats.invalid_media_source += 1
                    audit(
                        "invalid_media_source",
                        "remote payload has no video stream",
                        media_url=media_url,
                        error_category="invalid_media",
                    )
                    return None, "invalid_media_source", "remote payload has no video stream"
                if info.duration and float(info.duration) > 0:
                    method = (
                        "frame_derived"
                        if info.duration_source == "frame_derived"
                        else "remote_stream"
                        if info.duration_source in ("video_stream", "audio_stream")
                        else "remote_format"
                    )
                    return finish(
                        float(info.duration),
                        "duration_unknown_resolved",
                        f"remote ffprobe resolved {info.duration:.1f}s ({info.duration_source or 'format'})",
                        method,
                    )

        # D/E/F. controlled materialization + local probe + bounded decode
        media_config = self.settings.media
        if media_url and bool(media_config.duration_resolve_download):
            dest = self._cache_path(candidate)
            downloaded = False
            try:
                bytes_before = dest.stat().st_size if dest.exists() else 0
                await self.deps.downloader.download(
                    media_url,
                    dest,
                    metadata={"duration": candidate.duration, "platform_video_id": video_id},
                )
                stats.probe_downloads += 1
                stats.downloads += 1
                stats.probe_bytes += max(0, dest.stat().st_size - bytes_before)
                if staged is not None and dest not in staged:
                    staged.append(dest)
                candidate.metadata["prefetched_path"] = str(dest)
                downloaded = True
            except Exception as exc:
                LOGGER.debug("duration probe download failed for %s: %s", video_id, exc)
                try:
                    dest.unlink(missing_ok=True)
                except OSError:  # pragma: no cover - defensive
                    pass
                if staged is not None and dest in staged:
                    staged.remove(dest)

            if downloaded:
                if _looks_like_web_page(dest) or dest.stat().st_size <= 0:
                    dest.unlink(missing_ok=True)
                    if staged is not None and dest in staged:
                        staged.remove(dest)
                    stats.invalid_media_source += 1
                    audit(
                        "invalid_media_source",
                        f"media URL returned an invalid payload ({_describe_size(dest)})",
                        media_url=media_url,
                        error_category="invalid_payload",
                    )
                    return None, "invalid_media_source", (
                        f"media URL returned an invalid payload ({_describe_size(dest)})"
                    )
                try:
                    info = await toolkit.probe(dest)
                except Exception as exc:
                    dest.unlink(missing_ok=True)
                    if staged is not None and dest in staged:
                        staged.remove(dest)
                    stats.invalid_media_source += 1
                    audit(
                        "invalid_media_source",
                        f"downloaded media could not be probed: {str(exc)[:120]}",
                        media_url=media_url,
                        error_category="corrupt_media",
                    )
                    return None, "invalid_media_source", (
                        f"downloaded media could not be probed: {str(exc)[:120]}"
                    )
                if info.duration and float(info.duration) > 0:
                    method = (
                        "frame_derived"
                        if info.duration_source == "frame_derived"
                        else "local_stream"
                        if info.duration_source in ("video_stream", "audio_stream")
                        else "local_format"
                    )
                    return finish(
                        float(info.duration),
                        "duration_unknown_resolved",
                        f"local ffprobe resolved {info.duration:.1f}s ({info.duration_source or 'format'})",
                        method,
                    )
                if not info.has_video:
                    dest.unlink(missing_ok=True)
                    if staged is not None and dest in staged:
                        staged.remove(dest)
                    stats.invalid_media_source += 1
                    audit(
                        "invalid_media_source",
                        "local payload has no video stream",
                        media_url=media_url,
                        error_category="invalid_media",
                    )
                    return None, "invalid_media_source", "local payload has no video stream"
                max_source = float(
                    getattr(self.deps.candidate_filter, "max_duration", 300.0) or 300.0
                )
                bound = min(
                    max(1.0, float(media_config.duration_resolve_decode_seconds)),
                    max_source + 1.0,
                )
                try:
                    duration, decode_state = await toolkit.measure_bounded_duration(
                        dest,
                        max_seconds=bound,
                        timeout=float(media_config.duration_resolve_timeout_seconds),
                    )
                except Exception as exc:
                    LOGGER.debug("bounded decode failed for %s: %s", video_id, exc)
                    duration, decode_state = None, "bounded_decode_failed"
                if duration and float(duration) > 0:
                    if decode_state == "bounded_decode_bound_reached" and bound < max_source:
                        dest.unlink(missing_ok=True)
                        if staged is not None and dest in staged:
                            staged.remove(dest)
                        audit(
                            "duration_unknown_unresolved",
                            f"bounded decode reached {bound:.1f}s",
                            "bounded_decode",
                            media_url=media_url,
                            error_category="bounded_decode_bound_reached",
                        )
                        return None, "duration_unknown_unresolved", (
                            f"bounded decode reached {bound:.1f}s before a reliable source "
                            "duration could be derived"
                        )
                    stats.duration_bounded_decode_resolved += 1
                    return finish(
                        float(duration),
                        "duration_unknown_resolved",
                        f"bounded decode resolved {duration:.1f}s ({decode_state})",
                        "bounded_decode",
                    )

        if not media_url:
            stats.media_unreachable += 1
            audit(
                "media_unreachable",
                "no playable media URL could be resolved",
                error_category="no_media_url",
            )
            return None, "media_unreachable", "no playable media URL could be resolved"
        stats.duration_unknown_unresolved += 1
        stats.duration_unresolved += 1
        stats.probe_latency_ms += int((time.perf_counter() - started) * 1000)
        audit(
            "duration_unknown_unresolved",
            "media was reachable but no bounded method could establish a reliable duration",
            error_category="duration_unresolved",
            media_url=media_url,
        )
        return None, "duration_unknown_unresolved", (
            "media was reachable but no bounded method could establish a reliable duration"
        )

    def _duration_within_range(
        self, candidate: VideoCandidate, request: TaskRequest
    ) -> tuple[bool, str]:
        duration = float(candidate.duration or 0.0)
        # Source duration is governed by the candidate limits, never by the
        # final clip min/max (a 40s source can legitimately produce 6s clips).
        minimum = float(
            getattr(
                self.deps.candidate_filter,
                "min_duration",
                self.settings.pipeline.candidate_min_duration,
            )
            or 0.0
        )
        maximum = float(
            getattr(
                self.deps.candidate_filter,
                "max_duration",
                self.settings.pipeline.candidate_max_duration,
            )
            or 0.0
        )
        if minimum and duration < minimum:
            return False, f"duration {duration:.1f}s below {minimum:.0f}s"
        if maximum and duration > maximum:
            return False, f"duration {duration:.1f}s above {maximum:.0f}s"
        return True, ""

    def _store_candidate_rejection(
        self,
        *,
        candidate: VideoCandidate,
        task_id: int,
        stats: PipelineStats,
        report: SourceVideoReport,
        reason: RejectReason,
        detail: str,
        library: MaterialLibrary,
        status: SourceVideoStatus = SourceVideoStatus.REJECTED,
    ) -> SourceVideoReport:
        """Record one local rejection with an explicit reason (no mislabelling)."""

        report.status = SourceVideoStatus.REJECTED
        report.reject_reason = reason
        report.preview_detail = detail
        library.upsert_source_video(
            task_id=task_id,
            platform=candidate.platform,
            platform_video_id=candidate.platform_video_id,
            source_url=candidate.source_url,
            title=candidate.title,
            author=candidate.author,
            author_id=candidate.author_id,
            cover_url=candidate.cover_url,
            duration=candidate.duration,
            status=status,
            reject_reason=reason,
            matched_queries=candidate.matched_queries,
            statistics=candidate.statistics,
            touch_attempt=True,
        )
        self._emit(
            stats,
            "candidate_rejected",
            f"本地淘汰 [{reason}] {self._label(candidate)}: {detail}",
        )
        return report

    def _plan_queries(self, request: TaskRequest) -> list[str]:
        """Keyword expansion is platform specific: let the source order them."""

        explicit = [str(query).strip() for query in (request.explicit_queries or []) if str(query).strip()]
        if explicit:
            # Milestone 9: a plan item owns its objective queries verbatim; the
            # material expander must never override the objective (M8 showed the
            # two could diverge).
            return explicit
        if getattr(self.deps.source, "search_mode", "keyword") == "collection":
            return [request.material]

        # A stage-targeted ad-hoc run must stay inside that stage's query
        # family.  Previously only approved collection-plan items had this
        # property; ``--target-stage cutting`` ran the seed once and then fell
        # back to the generic material expander (mostly drying queries).
        if request.target_process_stage is not None:
            from core.coverage import CoverageAnalyzer

            seed = (request.query_seed or "").strip()
            limit = max(1, int(self.settings.pipeline.keyword_max_queries))
            analyzer = CoverageAnalyzer(self.deps.library, self.settings.coverage)
            stage_queries = analyzer.recommended_queries(
                request.library_category or request.material,
                str(request.target_process_stage),
                limit=limit,
            )
            return self.keyword_expander._dedupe(
                [seed, *stage_queries] if seed else stage_queries
            )[:limit]

        planned = self.deps.source.plan_queries(self.keyword_expander, request.material)
        seed = (request.query_seed or "").strip()
        if seed:
            # A user supplied search phrase ("苹果干烘干") runs first, verbatim.
            planned = [seed, *[query for query in planned if query != seed]]
        return planned

    def _search_limit(self, request: TaskRequest) -> int:
        # ``request.max_candidates`` is an evaluation budget assigned by the
        # plan runner, not a discovery-page size.  Clamping the source search
        # to that share (often 1) prevents local deduplication from ever seeing
        # candidates beyond the first, even when the browser found many.
        share = int(request.max_candidates or 0)
        if getattr(self.deps.source, "search_mode", "keyword") == "collection":
            limit = max(
                self.settings.pipeline.max_candidates_per_task,
                request.target_clip_count * 2,
            )
            return min(limit, share) if share else limit
        douyin = self.settings.sources.douyin
        # Never ask for more than the per-query cap: a target of 20 must not
        # become "200 results per keyword" (section 29/30).
        desired = request.target_clip_count * max(
            1, self.settings.collection.initial_candidate_multiplier
        )
        limit = max(1, min(douyin.max_candidates_per_query, desired))
        return limit

    def _max_candidates_reached(self, considered: int) -> bool:
        # The task budget honours both the Milestone 3 collection section and
        # the Milestone 1 pipeline key (the tighter of the two wins).
        limits = [
            limit
            for limit in (
                self.settings.collection.max_candidates_per_task,
                self.settings.pipeline.max_candidates_per_task,
                self.settings.sources.douyin.max_candidates_per_task,
            )
            if limit and limit > 0
        ]
        limit = min(limits) if limits else 0
        return limit > 0 and considered >= limit

    def _budget_reason(self, stats: PipelineStats, clips: list[ClipRecord]) -> str:
        """Return a human reason when a task budget is exhausted."""

        collection = self.settings.collection
        if collection.max_source_downloads_per_task > 0 and (
            stats.downloads >= collection.max_source_downloads_per_task
        ):
            return f"达到下载上限 {collection.max_source_downloads_per_task}"
        if collection.max_ai_calls_per_task > 0 and (
            self.deps.gateway.stats.calls >= collection.max_ai_calls_per_task
        ):
            return f"达到 AI 调用上限 {collection.max_ai_calls_per_task}"
        if collection.max_task_runtime_minutes > 0 and time.perf_counter() >= self._deadline:
            return f"达到运行时长上限 {collection.max_task_runtime_minutes} 分钟"
        return ""

    def _ai_budget_available(self) -> bool:
        limit = self.settings.ai.limits.max_ai_calls_per_source
        if limit <= 0:
            return True
        used = self.deps.gateway.stats.calls - self._ai_calls_at_source_start
        if used >= limit:
            LOGGER.warning("AI call budget for this source reached (%s); skipping call", limit)
            return False
        return True

    def _persist_yield(self, task_id: int, row: _QueryYield) -> None:
        try:
            self.deps.library.add_search_yield(
                task_id=task_id,
                platform=self.deps.source.platform,
                query=row.query,
                candidate_count=row.candidates,
                unique_candidate_count=row.unique,
                preview_accept_count=row.accepted,
                download_count=row.downloads,
                final_clip_count=row.clips,
                new_to_system_count=row.new_to_system,
                known_source_count=row.known_source,
                current_run_duplicate_count=row.current_run_duplicate,
                already_processed_count=row.already_processed,
                already_represented_count=row.already_represented,
                query_family=row.query_family,
                plan_id=row.plan_id,
                plan_item_id=row.plan_item_id,
                planned_order=row.planned_order,
                actual_order=row.actual_order,
                candidate_cap=row.candidate_cap,
                stop_reason=row.stop_reason,
                reserve_activation_reason=row.reserve_activation_reason,
                was_reserve=row.was_reserve,
            )
        except Exception as exc:  # pragma: no cover - metrics must never break a task
            LOGGER.debug("could not persist search yield: %s", exc)

    @staticmethod
    def _provenance(candidate: VideoCandidate) -> dict:
        return {
            "source_title": candidate.title,
            "source_author": candidate.author,
            "source_author_id": candidate.author_id,
            "source_publish_time": candidate.published_at.isoformat()
            if candidate.published_at
            else None,
        }

    def _source_video_id(self, candidate: VideoCandidate) -> int | None:
        record = self.deps.library.get_source_video(
            candidate.platform, candidate.platform_video_id
        )
        return record.id if record else None

    def _clamp_timing(self, timing: SegmentTiming, request: TaskRequest) -> SegmentTiming | None:
        duration = timing.duration
        if duration < request.min_clip_duration:
            return None
        if duration > request.max_clip_duration:
            timing = timing.model_copy(
                update={"end": round(timing.start + request.max_clip_duration, 3)}
            )
        return timing

    @staticmethod
    def _is_rediscovery(
        library: MaterialLibrary,
        candidate: VideoCandidate,
        reason: RejectReason,
    ) -> bool:
        """True when the local filter rejected a video we already know.

        Only *the same video* counts as rediscovery: the stored status and
        reject reason describe that video, so a later keyword must not replace
        them (section 1/20).  A different post that merely shares a URL with an
        earlier one is a real ``duplicate_url`` rejection and keeps its own row.
        """

        if reason is RejectReason.DUPLICATE_VIDEO:
            return True
        if reason is not RejectReason.ALREADY_PROCESSED:
            return False
        return (
            library.get_source_video(candidate.platform, candidate.platform_video_id)
            is not None
        )

    @staticmethod
    def _fallback_tagging(material: str, segment: DetectedSegment) -> ClipTagging:
        base = material
        for suffix in ("干", "片", "粉", "条"):
            if base.endswith(suffix) and len(base) > len(suffix):
                base = base[: -len(suffix)]
                break
        scores = ClipScores(material_relevance=max(0.6, segment.material_relevance))
        scores.overall = scores.recompute_overall()
        return ClipTagging(
            material=base or material,
            material_state=MaterialState.UNKNOWN,
            process_stage=ProcessStage.OTHER,
            subtitle_type=SubtitleType.UNKNOWN,
            subtitle_score=0.0,
            description=segment.description,
            scores=scores,
        )

    @staticmethod
    def _label(candidate: VideoCandidate) -> str:
        name = (
            candidate.metadata.get("file_name")
            or candidate.title
            or candidate.platform_video_id
        )
        return str(name)[:28]

    def _discard_source(self, path: Path | None) -> None:
        """Remove a staged source video unless debugging keeps it."""

        if path is None:
            return
        if self.settings.debug.keep_source_videos:
            LOGGER.info("debug.keep_source_videos=true: keeping %s", path)
            return
        for candidate_path in (path, path.with_suffix(path.suffix + ".meta.json")):
            try:
                candidate_path.unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover - defensive
                LOGGER.debug("could not delete %s: %s", candidate_path, exc)

    @staticmethod
    def _discard_files(artifact) -> None:
        for path in (artifact.file_path, artifact.thumbnail_path):
            if path is None:
                continue
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:  # pragma: no cover - defensive
                LOGGER.debug("could not delete %s", path)

    @staticmethod
    def _count_rejection(
        stats: PipelineStats,
        reason: RejectReason | None,
        subtitle_rejected: bool = False,
        *,
        quality: bool = False,
    ) -> None:
        if reason is not None and reason in DUPLICATE_REASONS:
            stats.duplicates_rejected += 1
        elif subtitle_rejected or (reason is not None and reason in SUBTITLE_REASONS):
            stats.subtitle_rejected += 1
        elif quality:
            stats.quality_rejected += 1
        else:
            stats.other_rejected += 1

    def _cache_path(self, candidate: VideoCandidate) -> Path:
        safe_id = "".join(
            char for char in candidate.platform_video_id if char.isalnum() or char in "-_"
        )
        return self.settings.paths.cache_dir / f"{candidate.platform}_{safe_id}.mp4"

    async def _resolve_download_url(self, candidate: VideoCandidate) -> str:
        """Ask the adapter for a fresh media URL, falling back to the page URL."""

        if candidate.media_url:
            try:
                return validate_remote_media_url(candidate.media_url)
            except RemoteMediaError:
                pass
        try:
            url = await self.deps.source.get_download_url(candidate.platform_video_id)
            return url or candidate.source_url
        except SourceError:
            raise
        except Exception as exc:
            LOGGER.warning(
                "could not resolve a direct download url for %s (%s); using the page url",
                candidate.platform_video_id,
                exc,
            )
            return candidate.source_url

    def _should_stop(self, clips: list[ClipRecord], request: TaskRequest) -> bool:
        if self._cancelled():
            return True
        if not self.settings.collection.stop_when_target_reached:
            return False
        return len(clips) >= request.target_clip_count

    def _cancelled(self) -> bool:
        return bool(self.cancel_event and self.cancel_event.is_set())

    async def _cleanup_cache(self, staged: list[Path]) -> list[str]:
        """Delete temporary source copies and stale cache entries."""

        messages: list[str] = []
        pipeline = self.settings.pipeline
        keep = self.settings.debug.keep_source_videos
        if pipeline.delete_source_after_processing and not keep:
            removed = 0
            for path in staged:
                for candidate_path in (path, path.with_suffix(path.suffix + ".meta.json")):
                    try:
                        candidate_path.unlink(missing_ok=True)
                        removed += 1
                    except OSError as exc:  # pragma: no cover - defensive
                        LOGGER.debug("cache cleanup failed for %s: %s", candidate_path, exc)
            if removed:
                messages.append(f"已删除 {removed} 个临时文件")
        elif keep:
            messages.append("debug.keep_source_videos=true: 保留临时源视频")

        removed_partials = self.purge_partial_files()
        if removed_partials:
            messages.append(f"清理未完成下载 {removed_partials} 个")

        if pipeline.delete_source_after_processing and not keep:
            # belt and braces: any staged source left in the cache root goes,
            # even if the run was interrupted before the path was tracked
            removed_staged = self.purge_staged_sources()
            if removed_staged:
                messages.append(f"清理暂存源视频 {removed_staged} 个")
            # a background copy thread may still land its bytes a moment after
            # cancellation; one short second pass closes that race
            await asyncio.sleep(0.05)
            removed_late = self.purge_staged_sources()
            if removed_late:
                LOGGER.debug("purged %s late staged file(s)", removed_late)

        # Derived artifacts (frames / previews / thumbnails) are regenerable and
        # must never survive a run - including one that was interrupted halfway.
        removed_derived = self.purge_derived_cache()
        if removed_derived:
            LOGGER.debug("purged %s derived cache file(s)", removed_derived)

        if pipeline.cleanup_cache_on_finish:
            removed = self.purge_stale_cache(pipeline.cleanup_cache_max_age_hours)
            if removed:
                messages.append(f"清理过期缓存 {removed} 个文件")
        return messages

    def purge_staged_sources(self) -> int:
        """Delete staged source videos (and sidecars) from the cache root."""

        cache_dir = self.settings.paths.cache_dir
        if not cache_dir.exists():
            return 0
        removed = 0
        patterns = ("*.mp4", "*.mov", "*.m4v", "*.mp4.meta.json", "*.mov.meta.json", "*.m4v.meta.json")
        for pattern in patterns:
            for path in cache_dir.glob(pattern):
                try:
                    if path.is_file():
                        path.unlink()
                        removed += 1
                except OSError:  # pragma: no cover - defensive
                    continue
        return removed

    def purge_derived_cache(self) -> int:
        """Delete sampled frames, previews, thumbnail scratch and ``.part`` files."""

        cache_dir = self.settings.paths.cache_dir
        if not cache_dir.exists():
            return 0
        removed = 0
        for name in ("frames", "previews", "thumbnails"):
            directory = cache_dir / name
            if not directory.exists():
                continue
            for path in sorted(directory.rglob("*"), reverse=True):
                try:
                    if path.is_file():
                        path.unlink()
                        removed += 1
                    elif path.is_dir() and not any(path.iterdir()):
                        path.rmdir()
                except OSError:  # pragma: no cover - defensive
                    continue
        return removed

    def purge_partial_files(self) -> int:
        """Delete abandoned ``.part`` files left by interrupted downloads."""

        cache_dir = self.settings.paths.cache_dir
        if not cache_dir.exists():
            return 0
        removed = 0
        for path in cache_dir.rglob("*.part"):
            try:
                path.unlink()
                removed += 1
            except OSError:  # pragma: no cover - defensive
                continue
        return removed

    async def _close_sources(self) -> None:
        """Release per-task resources (browser context, HTTP clients)."""

        for closer in (
            getattr(self.deps.source, "aclose", None),
            getattr(self.deps.gateway, "aclose", None),
            getattr(self.deps.downloader, "aclose", None),
        ):
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:  # pragma: no cover - cleanup must not fail a task
                LOGGER.debug("closing a task resource failed: %s", exc)

    def purge_stale_cache(self, max_age_hours: float) -> int:
        """Delete cache files older than ``max_age_hours``; returns the count."""

        cache_dir = self.settings.paths.cache_dir
        if not cache_dir.exists():
            return 0
        cutoff = utc_now().timestamp() - max_age_hours * 3600
        removed = 0
        for path in sorted(cache_dir.rglob("*"), reverse=True):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            except OSError:  # pragma: no cover - defensive
                continue
        return removed

    def _cleanup_frames(self, platform_video_id: str) -> None:
        """Delete the sampled frames of one source video."""

        if not self.settings.pipeline.delete_source_after_processing:
            return
        safe_id = "".join(
            char for char in platform_video_id if char.isalnum() or char in "-_"
        )
        cache_dir = self.settings.paths.cache_dir
        targets = (
            (cache_dir / "frames", f"{safe_id}_analysis_*.jpg"),
            (cache_dir / "previews", f"{safe_id}_preview_*.jpg"),
            (cache_dir / "thumbnails", "*.jpg"),
        )
        for directory, pattern in targets:
            if not directory.exists():
                continue
            for path in directory.glob(pattern):
                try:
                    path.unlink(missing_ok=True)
                except OSError:  # pragma: no cover - defensive
                    continue

    def _emit(
        self,
        stats: PipelineStats,
        stage: str,
        message: str,
        *,
        level: str = "info",
    ) -> None:
        LOGGER.info("[%s] %s", stage, message)
        if self.on_event is None:
            return
        try:
            self.on_event(
                ProgressEvent(stage=stage, message=message, stats=stats.model_copy(), level=level)
            )
        except Exception as exc:  # pragma: no cover - UI callback must never break the task
            LOGGER.debug("progress callback failed: %s", exc)
