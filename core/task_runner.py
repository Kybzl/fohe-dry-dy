"""``TaskRunner``: the synchronous entry point used by the UI and by tests."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, Callable

from core.config import AppSettings, is_absolute_path
from core.dependencies import build_dependencies, build_library
from core.keyword_expander import KeywordExpander
from core.models import (
    PipelineResult,
    ProgressEvent,
    SubtitlePolicy,
    TaskRequest,
    TaskStatus,
)
from core.orchestrator import CollectionOrchestrator
from storage.library import MaterialLibrary

LOGGER = logging.getLogger(__name__)

ProgressCallback = Callable[[ProgressEvent], None]


class TaskRunner:
    """Owns one library instance and executes collection tasks on demand."""

    def __init__(
        self,
        settings: AppSettings,
        *,
        library: MaterialLibrary | None = None,
        browser_backend: Any | None = None,
    ) -> None:
        self.settings = settings
        self.library = library or build_library(settings)
        self.recovered_tasks = self.library.normalize_interrupted_tasks()
        #: Milestone 8.1: an already-open Douyin browser session (interactive
        #: verification / one-plan run) reused instead of launching a new one
        self.browser_backend = browser_backend
        self._lock = threading.Lock()
        self.last_result: PipelineResult | None = None
        self._cancel_event = threading.Event()
        self._active_orchestrator: CollectionOrchestrator | None = None

    # -- introspection -----------------------------------------------------
    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def reset_cancel(self) -> None:
        self._cancel_event.clear()

    def recent_clips(self, material: str | None = None, limit: int = 24):
        return self.library.list_clips(material=material, limit=limit)

    # -- execution ---------------------------------------------------------
    def run(
        self,
        request: TaskRequest,
        *,
        on_event: ProgressCallback | None = None,
    ) -> PipelineResult:
        """Blocking wrapper around :meth:`run_async`."""

        try:
            return asyncio.run(self.run_async(request, on_event=on_event))
        except KeyboardInterrupt:
            # Ctrl+C: the orchestrator already cleaned up and marked the task
            LOGGER.warning("task interrupted by the operator (Ctrl+C)")
            return PipelineResult(
                task_id=getattr(self._active_orchestrator, "last_task_id", None),
                material=request.material,
                status=TaskStatus.CANCELLED,
                error="interrupted by user",
                messages=["任务被用户中断（Ctrl+C），临时文件已清理"],
            )
        except asyncio.CancelledError:  # pragma: no cover - defensive
            return PipelineResult(
                task_id=getattr(self._active_orchestrator, "last_task_id", None),
                material=request.material,
                status=TaskStatus.CANCELLED,
                error="cancelled",
                messages=["任务已取消"],
            )

    async def run_async(
        self,
        request: TaskRequest,
        *,
        on_event: ProgressCallback | None = None,
    ) -> PipelineResult:
        """Execute one collection task; never raises for pipeline failures."""

        if not self._lock.acquire(blocking=False):
            LOGGER.warning("another task is already running")
            return PipelineResult(
                material=request.material,
                status=TaskStatus.FAILED,
                error="another task is already running",
                messages=["已有任务在执行中，请稍后再试"],
            )
        try:
            library = self._library_for(request)
            resume_error = self._validate_resume_request(library, request)
            if resume_error is not None:
                return PipelineResult(
                    task_id=request.resume_task_id,
                    material=request.material,
                    status=TaskStatus.FAILED,
                    error=resume_error,
                    messages=[f"无法恢复任务: {resume_error}"],
                )
            source_name = (
                request.source or self.settings.sources.active_source or "local"
            ).lower()
            local_files = list(request.local_files)
            if source_name == "local" and not local_files:
                return PipelineResult(
                    material=request.material,
                    status=TaskStatus.FAILED,
                    error="no local video selected",
                    messages=["请先选择本地测试视频（或把来源切换为模拟数据）"],
                )
            dependencies = build_dependencies(
                self.settings,
                library=library,
                subtitle_policy=request.subtitle_policy,
                source_name=source_name,
                provider_name=request.provider,
                media_backend=request.media_backend,
                local_files=local_files,
                douyin_urls=list(request.douyin_urls),
                on_ai_call=library.add_ai_run,
                browser_backend=self.browser_backend,
            )
            orchestrator = CollectionOrchestrator(
                dependencies,
                keyword_expander=KeywordExpander(
                    max_queries=self.settings.pipeline.keyword_max_queries
                ),
                on_event=on_event,
                cancel_event=self._cancel_event,
            )
            self._active_orchestrator = orchestrator
            result = await orchestrator.collect(
                request, resume_task_id=request.resume_task_id
            )
            self.last_result = result
            return result
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.exception("task runner failed")
            return PipelineResult(
                material=request.material,
                status=TaskStatus.FAILED,
                error=str(exc),
                messages=[f"任务失败: {exc}"],
            )
        finally:
            self._cancel_event.clear()
            self._lock.release()

    # -- helpers -----------------------------------------------------------
    def _library_for(self, request: TaskRequest) -> MaterialLibrary:
        """Honour a per-request save path, otherwise reuse the shared library."""

        if request.library_root is None:
            return self.library
        # Accept values pasted from the UI, quotes included.
        requested = Path(str(request.library_root).strip().strip('"').strip("'")).expanduser()
        if not is_absolute_path(requested):
            requested = self.settings.project_root / requested
        if requested.resolve() == self.library.root.resolve():
            return self.library
        LOGGER.info("task uses a custom library root: %s", requested)
        return build_library(self.settings, library_root=requested)

    @staticmethod
    def _validate_resume_request(
        library: MaterialLibrary,
        request: TaskRequest,
    ) -> str | None:
        """Reject an unsafe resume before providers or media tools are built.

        The target count may intentionally be raised on resume, but the fields
        that define clip identity must still match the persisted task.  This
        prevents a typo or stale task id from mixing unrelated clips into one
        audit trail.
        """

        if request.resume_task_id is None:
            return None
        task = library.get_task(request.resume_task_id)
        if task is None:
            return f"任务 #{request.resume_task_id} 不存在"

        mismatches: list[str] = []
        if request.material != task.material:
            mismatches.append(f"素材 {request.material!r} != {task.material!r}")
        if request.min_clip_duration != task.min_clip_duration:
            mismatches.append(
                f"最短时长 {request.min_clip_duration:g} != {task.min_clip_duration:g}"
            )
        if request.max_clip_duration != task.max_clip_duration:
            mismatches.append(
                f"最长时长 {request.max_clip_duration:g} != {task.max_clip_duration:g}"
            )
        if request.subtitle_policy is not task.subtitle_policy:
            mismatches.append(
                f"字幕策略 {request.subtitle_policy.value} != {task.subtitle_policy.value}"
            )
        if mismatches:
            return f"任务 #{task.id} 的恢复参数与原任务不一致: " + "; ".join(
                mismatches
            )
        return None
