"""Plan execution (Milestone 7, sections 12-29).

The runner orchestrates the **existing** acquisition pipeline - one
``TaskRequest`` per query, executed by ``TaskRunner`` - and owns nothing else:
no downloader, no Qwen client, no FFmpeg wrapper is duplicated here.

Safety rules that this module enforces:

* a plan must be approved before it can run
* every item/plan budget is a hard limit with a configurable token reserve
* pause is cooperative: the current task finishes, then nothing new starts
* cancel keeps every already-produced clip and leaves the tasks in place
* human verification / backend failures pause the plan instead of burning the
  remaining queries
* progress is checkpointed in small transactions after every unit of work
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from core.config import AppSettings
from core.models import (
    PipelineStats,
    PipelineResult,
    ProcessStage,
    ProgressEvent,
    TaskRequest,
    TaskStatus,
)
from core.planner import Planner
from core.plans import (
    CollectionPlan,
    CollectionPlanItem,
    PauseReason,
    PlanItemStatus,
    PlanQuery,
    PlanProgress,
    PlanStatus,
)
from storage.library import MaterialLibrary
from storage.plans import PlanRepository

LOGGER = logging.getLogger(__name__)

TaskExecutor = Callable[[TaskRequest], Awaitable[PipelineResult]]

#: browser states that mean "a human must act" (section 25)
HUMAN_STATES: frozenset[str] = frozenset(
    {
        "verification_required",
        "login_required",
        "human_verification_required",
    }
)

#: discovery states that are operational interruptions, never a completed
#: query-space result.  The query remains retryable after the blocker clears.
OPERATIONAL_DISCOVERY_STATES: frozenset[str] = frozenset(
    {
        "login_required",
        "verification_required",
        "human_verification_required",
        "browser_unavailable",
        "browser_crashed",
        "upstream_bad_gateway",
        "upstream_http_error",
        "douyin_unreachable",
        "discovery_blocked",
        "backend_unavailable",
        "search_pending",
        "search_timeout",
        "search_dom_changed",
    }
)


@dataclass
class PlanRunResult:
    """Outcome of one ``run`` call."""

    plan_id: int
    status: PlanStatus = PlanStatus.DRAFT
    pause_reason: str = ""
    dry_run: bool = False
    refused: str = ""
    queries_run: int = 0
    tasks_run: int = 0
    clips_saved: int = 0
    qualifying_clips: int = 0
    ai_tokens: int = 0
    cleanup_attempts: int = 0
    cleanup_successes: int = 0
    provider_failure_class: str = ""
    provider_failure_subtype: str = ""
    provider_model: str = ""
    provider_operation: str = ""
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.refused


class PlanRunner:
    """Runs one approved plan against the existing acquisition pipeline."""

    def __init__(
        self,
        plan_id: int,
        *,
        library: MaterialLibrary,
        settings: AppSettings,
        repository: PlanRepository | None = None,
        planner: Planner | None = None,
        executor: TaskExecutor | None = None,
        on_event: Callable[[ProgressEvent], None] | None = None,
        pause_event: threading.Event | None = None,
        cancel_event: threading.Event | None = None,
        browser_backend: Any | None = None,
        cleanup_new: bool = False,
        cleanup_new_limit: int = 0,
        cleanup_router: Any | None = None,
    ) -> None:
        self.plan_id = int(plan_id)
        self.library = library
        self.settings = settings
        self.repo = repository or PlanRepository(library.database)
        self.planner = planner or Planner(library, settings, repository=self.repo)
        self._executor = executor
        self.on_event = on_event
        self.pause_event = pause_event or threading.Event()
        self.cancel_event = cancel_event or threading.Event()
        #: Milestone 8.1: reuse an already-verified browser session (if any)
        self.browser_backend = browser_backend
        #: Milestone 9.4: classify every newly accepted clip; destructive
        #: cleanup remains an explicit per-run opt-in.
        self.cleanup_new = bool(cleanup_new)
        self.cleanup_new_limit = max(0, int(cleanup_new_limit or 0))
        self.cleanup_router = cleanup_router
        self._novelty_analyzer = None
        self._pause_reason = PauseReason.NONE

    # -- cooperative control (sections 21/22/23) ---------------------------
    def request_pause(self, reason: PauseReason = PauseReason.OPERATOR) -> None:
        """Ask the runner to stop after the current safe unit of work."""

        self._pause_reason = reason
        self.pause_event.set()

    def request_resume(self) -> None:
        self._pause_reason = PauseReason.NONE
        self.pause_event.clear()

    def request_cancel(self) -> None:
        self.cancel_event.set()

    def _paused(self) -> bool:
        return self.pause_event.is_set()

    def _cancelled(self) -> bool:
        return self.cancel_event.is_set()

    # -- plan bookkeeping --------------------------------------------------
    def _load(self) -> CollectionPlan | None:
        return self.repo.get_plan(self.plan_id)

    def _checkpoint(
        self,
        plan: CollectionPlan,
        item: CollectionPlanItem | None = None,
    ) -> None:
        """Persist progress in a small transaction (sections 50/51)."""

        self.repo.save_progress(
            plan_id=self.plan_id,
            plan_progress=plan.progress,
            item_id=item.id if item is not None else None,
            item_progress=item.progress if item is not None else None,
        )
        if item is not None and item.id is not None:
            self.repo.set_item_status(item.id, item.status)

    def _emit(self, stage: str, message: str, details: dict[str, Any] | None = None) -> None:
        LOGGER.info("[plan %s] %s | %s", self.plan_id, stage, message)
        self.repo.log_event(self.plan_id, stage, details=details or {})
        if self.on_event is not None:
            try:
                self.on_event(
                    ProgressEvent(stage=stage, message=message, stats=PipelineStats())
                )
            except Exception as exc:  # pragma: no cover - UI callback must not break a run
                LOGGER.debug("plan progress callback failed: %s", exc)

    # -- budget helpers (sections 27/28/29) -------------------------------
    def _budget_pause_reason(self, plan: CollectionPlan, item: CollectionPlanItem) -> PauseReason:
        reserve_ratio = max(0.0, float(self.settings.collection_planning.token_reserve_ratio))
        reserve = int(plan.max_ai_tokens * reserve_ratio)
        if plan.max_ai_tokens and plan.tokens_remaining <= reserve:
            return PauseReason.TOKEN_BUDGET_EXHAUSTED
        if item.max_tokens and item.tokens_remaining <= int(item.max_tokens * reserve_ratio):
            return PauseReason.TOKEN_BUDGET_EXHAUSTED
        if plan.max_downloads and plan.downloads_remaining <= 0:
            return PauseReason.DOWNLOAD_BUDGET_EXHAUSTED
        if item.max_downloads and item.downloads_remaining <= 0:
            return PauseReason.DOWNLOAD_BUDGET_EXHAUSTED
        if plan.max_preview_candidates and plan.previews_remaining <= 0:
            return PauseReason.PREVIEW_BUDGET_EXHAUSTED
        if item.max_candidates and item.candidates_remaining <= 0:
            # the *candidate* budget is what ran out - not the preview budget
            # (Milestone 9.1: 0 previews must never be reported as
            # preview_budget_exhausted)
            return PauseReason.CANDIDATE_BUDGET_EXHAUSTED
        return PauseReason.NONE

    # -- execution (section 12/13/14/15/24) -------------------------------
    async def run(self, *, dry_run: bool = False) -> PlanRunResult:
        """Execute the plan; ``dry_run`` performs zero external calls."""

        plan = self._load()
        if plan is None:
            return PlanRunResult(
                plan_id=self.plan_id, refused=f"计划 #{self.plan_id} 不存在"
            )
        if dry_run:
            result = PlanRunResult(plan_id=self.plan_id, status=plan.status, dry_run=True)
            result.messages.append("dry-run：不会调用 Douyin / Qwen / 下载")
            return result
        if plan.archived:
            return PlanRunResult(
                plan_id=self.plan_id,
                status=plan.status,
                refused="计划已归档：归档计划不会被批准/执行（如需继续请先取消归档）",
            )
        if plan.status in (PlanStatus.DRAFT,) or plan.approved_at is None:
            return PlanRunResult(
                plan_id=self.plan_id,
                status=plan.status,
                refused="计划尚未批准：请先执行 approve（草稿不能运行）",
            )
        if plan.status in (
            PlanStatus.COMPLETED,
            PlanStatus.CANCELLED,
            PlanStatus.FAILED,
            PlanStatus.PARTIALLY_COMPLETED,
        ):
            return PlanRunResult(
                plan_id=self.plan_id,
                status=plan.status,
                refused=f"计划状态为 {plan.status}，不再执行（可新建计划）",
            )

        started = time.perf_counter()
        self.request_resume()
        self.repo.set_status(self.plan_id, PlanStatus.RUNNING, mark_started=True)
        self._emit("started", f"开始执行计划 #{self.plan_id}")
        result = PlanRunResult(plan_id=self.plan_id, status=PlanStatus.RUNNING)
        pause_reason = PauseReason.NONE

        executor = self._executor or self._default_executor()
        for item in plan.sorted_items():
            if item.satisfied:
                item.status = PlanItemStatus.SATISFIED
                continue
            if self._cancelled() or self._paused():
                break
            item.status = PlanItemStatus.RUNNING
            self._emit(
                "item_started",
                f"目标 {item.process_stage}: 需要 {item.remaining_clips} 个片段",
                {"item_id": item.id, "stage": item.process_stage},
            )
            reason = await self._run_item(
                plan, item, executor=executor, result=result
            )
            if reason is PauseReason.QUERY_SPACE_EXHAUSTED:
                # this *item* ran out of eligible queries; the remaining items
                # still deserve their own run (Milestone 9.1)
                pause_reason = reason
                continue
            if reason is not PauseReason.NONE and reason is not PauseReason.OPERATOR:
                pause_reason = reason
                break
            if self._cancelled() or self._paused():
                break

        elapsed = round(time.perf_counter() - started, 2)
        plan.progress.runtime_seconds = round(
            plan.progress.runtime_seconds + elapsed, 2
        )

        # -- final status (Milestone 9.1 stop reasons) ----------------------
        terminal_reasons = {
            PauseReason.QUERY_SPACE_EXHAUSTED,
            PauseReason.SOURCE_EXHAUSTED,
        }
        if self._cancelled():
            status = PlanStatus.CANCELLED
            pause_reason = PauseReason.NONE
        elif pause_reason in terminal_reasons:
            # honest exhaustion: the plan finished, it did not pause
            satisfied = all(item.satisfied for item in plan.items) and bool(plan.items)
            status = PlanStatus.COMPLETED if satisfied else PlanStatus.PARTIALLY_COMPLETED
            if satisfied:
                pause_reason = PauseReason.TARGET_REACHED
        elif all(item.satisfied for item in plan.items) and plan.items:
            status = PlanStatus.COMPLETED
            pause_reason = PauseReason.TARGET_REACHED
        elif pause_reason is not PauseReason.NONE:
            status = PlanStatus.PAUSED
        elif self._paused():
            status = PlanStatus.PAUSED
            pause_reason = self._pause_reason or PauseReason.OPERATOR
        elif all(item.satisfied for item in plan.items) and plan.items:
            status = PlanStatus.COMPLETED
        elif plan.progress.clips_saved > 0:
            status = PlanStatus.PARTIALLY_COMPLETED
        else:
            status = PlanStatus.PARTIALLY_COMPLETED

        plan.coverage_after = self.planner.coverage_snapshot(plan.library_category)
        self.repo.save_coverage(self.plan_id, after=plan.coverage_after)
        self.repo.set_status(
            self.plan_id,
            status,
            pause_reason=pause_reason,
            mark_finished=status
            in (
                PlanStatus.COMPLETED,
                PlanStatus.PARTIALLY_COMPLETED,
                PlanStatus.CANCELLED,
            ),
        )
        if result.provider_failure_class or result.provider_failure_subtype:
            self.repo.set_provider_state(
                self.plan_id,
                failure_class=result.provider_failure_class,
                subtype=result.provider_failure_subtype,
                ready=False,
                provider="",
                model=result.provider_model,
                operation=result.provider_operation,
            )
        self._checkpoint(plan)
        self._emit(
            "finished",
            f"计划结束: {status}"
            + (f"（{pause_reason}）" if pause_reason else "")
            + f"，产出 {result.clips_saved} 片段（目标命中 {result.qualifying_clips}）",
            {"status": str(status), "pause_reason": str(pause_reason)},
        )
        result.status = status
        result.pause_reason = str(pause_reason)
        result.messages.append(
            f"计划 #{self.plan_id} 结束: {status}，查询 {result.queries_run} 个，"
            f"任务 {result.tasks_run} 个，片段 {result.clips_saved}"
        )
        return result

    async def _run_item(
        self,
        plan: CollectionPlan,
        item: CollectionPlanItem,
        *,
        executor: TaskExecutor,
        result: PlanRunResult,
    ) -> PauseReason:
        """Run one objective until satisfied or its budget is exhausted."""

        while not item.satisfied:
            if self._cancelled() or self._paused():
                item.status = (
                    PlanItemStatus.SATISFIED if item.satisfied else PlanItemStatus.PAUSED
                )
                return self._pause_reason if self._paused() else PauseReason.NONE
            # Stop precedence (section 5.1): budgets are checked before
            # query-space exhaustion, so a plan whose budget ran out reports the
            # budget that actually stopped it - and only a plan that still had
            # budget left when its queries ran out is "honest exhaustion".
            reason = self._budget_pause_reason(plan, item)
            if reason is not PauseReason.NONE:
                LOGGER.warning(
                    "plan %s item %s paused: %s", self.plan_id, item.process_stage, reason
                )
                item.status = PlanItemStatus.PAUSED
                self._checkpoint(plan, item)
                self._emit(
                    "budget_warning",
                    f"目标 {item.process_stage} 预算用尽（{reason}）",
                    {"item_id": item.id, "reason": str(reason)},
                )
                return reason
            query = self._select_query(item)
            if query is None:
                item.status = PlanItemStatus.EXHAUSTED
                self._checkpoint(plan, item)
                self._emit(
                    "item_exhausted",
                    f"目标 {item.process_stage} 的可行动查询已用完，"
                    f"命中 {item.progress.qualifying_clips}/{item.requested_clips}",
                    {"item_id": item.id},
                )
                return PauseReason.QUERY_SPACE_EXHAUSTED

            request = self._build_request(plan, item, query)
            self._emit(
                "query_started",
                f"目标 {item.process_stage} 开始查询「{query.query}」"
                f"（{query.origin}）",
                {"item_id": item.id, "query": query.query},
            )
            try:
                task_result = await executor(request)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - executor safety net
                LOGGER.exception("plan task failed")
                item.status = PlanItemStatus.FAILED
                self._checkpoint(plan, item)
                self._emit(
                    "task_failed",
                    f"查询「{query.query}」执行失败: {exc}",
                    {"item_id": item.id, "query": query.query},
                )
                return PauseReason.NONE

            outcome = self._apply_task_result(plan, item, task_result, query.query)
            cleanup_decisions = await self._route_new_clips(
                task_result.clips, plan=plan, item=item, query=query.query
            )
            result.cleanup_attempts += sum(
                1 for decision in cleanup_decisions if decision.cleanup_attempted
            )
            result.cleanup_successes += sum(
                1
                for decision in cleanup_decisions
                if str(decision.cleanup_result_status) == "succeeded"
            )
            result.queries_run += 1
            result.tasks_run += 1
            result.clips_saved = plan.progress.clips_saved
            result.qualifying_clips = sum(
                entry.progress.qualifying_clips for entry in plan.items
            )
            result.ai_tokens = plan.progress.ai_tokens
            self._checkpoint(plan, item)
            self._emit(
                "task_completed",
                f"查询「{query.query}」完成: 片段 +{outcome['clips']}，"
                f"命中 +{outcome['qualifying']}，tokens {outcome['tokens']}",
                {
                    "item_id": item.id,
                    "query": query.query,
                    "task_id": task_result.task_id,
                    "clips": outcome["clips"],
                    "qualifying": outcome["qualifying"],
                    "tokens": outcome["tokens"],
                },
            )
            if outcome["task_id"] is not None and item.id is not None:
                self.repo.link_task(
                    plan_id=self.plan_id,
                    plan_item_id=item.id,
                    task_id=int(outcome["task_id"]),
                    query=query.query,
                )

            block_reason = self._blocking_reason(task_result)
            if getattr(task_result, "provider_unavailable", False):
                result.provider_failure_class = str(
                    getattr(task_result, "provider_failure_class", "") or ""
                )
                result.provider_failure_subtype = str(
                    getattr(task_result, "provider_failure_subtype", "") or ""
                )
                result.provider_model = str(
                    getattr(task_result, "provider_model", "") or ""
                )
                result.provider_operation = str(
                    getattr(task_result, "provider_operation", "") or ""
                )
            if block_reason is not PauseReason.NONE:
                item.status = PlanItemStatus.PAUSED
                self._checkpoint(plan, item)
                self._emit(
                    "paused",
                    f"目标 {item.process_stage} 暂停: {block_reason}",
                    {"item_id": item.id, "reason": str(block_reason)},
                )
                return block_reason

        item.status = PlanItemStatus.SATISFIED
        self._checkpoint(plan, item)
        self._emit(
            "item_satisfied",
            f"目标 {item.process_stage} 已达成: "
            f"{item.progress.qualifying_clips}/{item.requested_clips}",
            {"item_id": item.id},
        )
        return PauseReason.NONE

    def _select_query(self, item: CollectionPlanItem) -> PlanQuery | None:
        """Novelty-aware primary/reserve selection (Milestone 9.5).

        A primary query already known to be saturated is skipped instead of
        consuming candidate budget.  Reserve queries are activated only after
        the live primary set is exhausted or all remaining primaries are
        saturated, and the activation reason is recorded in the audit.
        """

        executed = set(item.progress.executed_queries)
        pending = [query for query in item.queries if query.query not in executed]
        primaries = [query for query in pending if query.role != "reserve"]
        live_primaries = [
            query for query in primaries if query.query_saturation != "saturated"
        ]
        if live_primaries:
            return live_primaries[0]
        if primaries:
            for query in primaries:
                already = any(
                    entry.get("query") == query.query and entry.get("skipped")
                    for entry in item.progress.query_audit
                )
                if not already:
                    item.progress.query_audit.append(
                        {
                            "query": query.query,
                            "query_family": query.family,
                            "skipped": True,
                            "stop_reason": "primary_saturated",
                            "query_saturation": query.query_saturation,
                            "planned_order": query.planned_order,
                        }
                    )
        if not self.settings.novelty.reserve_enabled:
            return None
        reserves = [query for query in pending if query.role == "reserve"]
        if not reserves:
            return None
        reason = "primary_saturated" if primaries else "remaining_budget_reallocation"
        if not primaries:
            audits = [
                entry
                for entry in item.progress.query_audit
                if not entry.get("skipped")
            ]
            if audits:
                last = audits[-1]
                if int(last.get("new_to_system") or 0) == 0 and int(
                    last.get("candidates") or 0
                ) > 0:
                    reason = "primary_zero_novelty"
                elif str(last.get("discovery_status") or "") not in ("", "ok"):
                    reason = "browser_failure"
        reserves[0].reserve_activation_reason = reason
        return reserves[0]

    async def _route_new_clips(
        self,
        clips: list[Any],
        *,
        plan: CollectionPlan,
        item: CollectionPlanItem,
        query: str,
    ) -> list[Any]:
        """Milestone 9.4: route every clip saved by this task.

        Classification always runs (metadata-only).  Destructive cleanup only
        runs when the operator explicitly enabled it for this plan execution.
        """

        clip_ids = [int(clip.id) for clip in clips if getattr(clip, "id", None)]
        if not clip_ids:
            return []
        from core.cleanup_routing import CleanupRouter

        router = self.cleanup_router or CleanupRouter(self.library, self.settings)
        limit = (
            self.cleanup_new_limit
            or int(self.settings.subtitle_cleanup.post_acquisition_max_cleanup)
        )
        decisions = await router.route_clips(
            clip_ids,
            attempt_cleanup=self.cleanup_new,
            max_attempts=limit,
            context={
                "plan_id": self.plan_id,
                "plan_item_id": item.id,
                "process_stage": item.process_stage,
                "query": query,
                "origin": "plan_runner",
            },
        )
        eligible = [decision for decision in decisions if decision.eligible]
        if decisions:
            self._emit(
                "cleanup_routing",
                f"新片段清理路由: {len(decisions)} 个，eligible {len(eligible)}，"
                f"尝试清理 {sum(1 for d in decisions if d.cleanup_attempted)}",
                {
                    "item_id": item.id,
                    "query": query,
                    "clip_ids": [decision.clip_id for decision in decisions],
                    "eligible": [decision.clip_id for decision in eligible],
                },
            )
        return decisions

    def _build_request(
        self,
        plan: CollectionPlan,
        item: CollectionPlanItem,
        query: PlanQuery,
    ) -> TaskRequest:
        """One acquisition task for one query (reuses the existing pipeline)."""

        share = self._candidate_share(item, query)
        return TaskRequest(
            material=plan.library_category,
            query_seed=query.query,
            # Milestone 9: run exactly this objective query - no material-level
            # keyword expansion may add unrelated searches to a plan item
            explicit_queries=[query.query],
            # Milestone 9.1: fair share of the item's candidate budget so every
            # objective query gets a first opportunity
            max_candidates=share,
            library_category=plan.library_category,
            target_clip_count=max(1, item.remaining_clips),
            target_process_stage=ProcessStage(item.process_stage),
            min_clip_duration=self.settings.pipeline.default_min_clip_duration,
            max_clip_duration=self.settings.pipeline.default_max_clip_duration,
            subtitle_policy=self.settings.pipeline.default_subtitle_policy,
            library_root=self.settings.paths.library_root,
            source="douyin",
            provider=self.settings.ai.active_provider,
            media_backend=self.settings.media.backend,
            plan_id=self.plan_id,
            plan_item_id=item.id,
            query_family=query.family,
            planned_order=query.planned_order,
            candidate_cap=share,
            was_reserve=query.role == "reserve",
            reserve_activation_reason=query.reserve_activation_reason,
        )

    def _candidate_share(
        self, item: CollectionPlanItem, query: PlanQuery | None = None
    ) -> int:
        """Discovery budget this query may spend (round-robin fair share).

        ``remaining_budget // remaining_queries`` (minimum 1) means: while the
        item still has candidate budget and the target is unmet, an unexecuted
        objective query always gets a turn.  Returns ``0`` when nothing is left.
        """

        remaining_budget = max(
            0, int(item.max_candidates) - int(item.progress.unique_candidates)
        )
        executed = set(item.progress.executed_queries)
        remaining_queries = max(
            1,
            len(
                [
                    candidate
                    for candidate in item.queries
                    if candidate.query not in executed
                    and candidate.query_saturation != "saturated"
                ]
            ),
        )
        if remaining_budget <= 0:
            return 0
        base = max(1, remaining_budget // remaining_queries)
        if query is None:
            return base
        weight = self._novelty().share_weight(query.query_saturation)
        if weight <= 0:
            return 0
        return max(1, min(remaining_budget, int(base * weight)))

    def _novelty(self):
        if self._novelty_analyzer is None:
            from core.novelty import NoveltyAnalyzer

            self._novelty_analyzer = NoveltyAnalyzer(self.library, self.settings)
        return self._novelty_analyzer

    def _apply_task_result(
        self,
        plan: CollectionPlan,
        item: CollectionPlanItem,
        task_result: PipelineResult,
        query: str,
    ) -> dict[str, Any]:
        """Fold one task's outcome into the item/plan counters (section 15)."""

        item.progress.queries_attempted += 1
        operational, discovery_state = self._operational_discovery(task_result)
        if not operational and query not in item.progress.executed_queries:
            item.progress.executed_queries.append(query)
        stats = task_result.stats
        item.progress.candidates_seen += int(stats.searched_candidates)
        item.progress.unique_candidates += int(stats.unique_candidates)
        item.progress.preview_calls += int(stats.prescreened)
        item.progress.downloads += int(stats.downloads)
        tokens = int((task_result.ai_usage or {}).get("total_tokens") or 0)
        calls = int((task_result.ai_usage or {}).get("ai_calls") or 0)
        item.progress.ai_tokens += tokens
        item.progress.ai_calls += calls
        item.progress.queries_remaining = len(
            [
                candidate
                for candidate in item.queries
                if candidate.query not in set(item.progress.executed_queries)
            ]
        )
        # Milestone 9.5: keep the per-query novelty audit on the plan item.
        for entry in getattr(task_result, "query_audit", []) or []:
            audit_entry = {
                **dict(entry),
                "task_id": task_result.task_id,
                "discovery_status": task_result.discovery_status,
                "discovery_backend": task_result.discovery_backend,
                "completed": not operational,
                "operational_blocker": operational,
                "operational_state": discovery_state,
            }
            if operational:
                audit_entry["stop_reason"] = "operational_blocker"
            item.progress.query_audit.append(audit_entry)

        qualifying = 0
        saved = 0
        for clip in task_result.clips:
            saved += 1
            stage = str(clip.process_stage)
            item.progress.stage_breakdown[stage] = (
                item.progress.stage_breakdown.get(stage, 0) + 1
            )
            plan.progress.stage_breakdown[stage] = (
                plan.progress.stage_breakdown.get(stage, 0) + 1
            )
            # only a clip that really shows the target stage satisfies the goal
            if stage == item.process_stage:
                qualifying += 1
        item.progress.clips_saved += saved
        item.progress.qualifying_clips += qualifying

        delta = PlanProgress(
            candidates_seen=int(stats.searched_candidates),
            unique_candidates=int(stats.unique_candidates),
            preview_calls=int(stats.prescreened),
            downloads=int(stats.downloads),
            clips_saved=saved,
            qualifying_clips=qualifying,
            ai_calls=calls,
            ai_tokens=tokens,
            queries_attempted=1,
        )
        plan.progress = plan.progress.add(delta)
        return {
            "clips": saved,
            "qualifying": qualifying,
            "tokens": tokens,
            "task_id": task_result.task_id,
            "calls": calls,
        }

    @staticmethod
    def _operational_discovery(task_result: PipelineResult) -> tuple[bool, str]:
        """Classify a discovery outcome as operational vs genuinely completed.

        Only ``ok`` / ``no_results`` (or an empty status on legacy/mock paths)
        count as a completed query.  A login wall, browser outage or upstream
        gateway failure pauses the plan and leaves the query retryable.
        """

        status = str(getattr(task_result, "discovery_status", "") or "")
        if getattr(task_result, "discovery_blocked", False):
            return True, status or "discovery_blocked"
        if status in ("", "ok", "no_results"):
            return False, status or "ok"
        if status in OPERATIONAL_DISCOVERY_STATES:
            return True, status
        # Unknown explicit states are treated as retryable operational
        # interruptions rather than silently consuming the query slot.
        return True, status

    @staticmethod
    def _blocking_reason(task_result: PipelineResult) -> PauseReason:
        """Human verification or a dead backend pauses instead of looping (25/26)."""

        if getattr(task_result, "provider_unavailable", False):
            return PauseReason.PROVIDER_UNAVAILABLE
        status = str(getattr(task_result, "discovery_status", "") or "")
        if status in HUMAN_STATES:
            return PauseReason.HUMAN_VERIFICATION_REQUIRED
        states = task_result.discovery_states or {}
        if any(value in HUMAN_STATES for value in states.values()):
            return PauseReason.HUMAN_VERIFICATION_REQUIRED
        if task_result.discovery_blocked:
            return PauseReason.BACKEND_UNAVAILABLE
        if status in (
            "browser_unavailable",
            "browser_crashed",
            "upstream_bad_gateway",
            "upstream_http_error",
            "douyin_unreachable",
        ):
            return PauseReason.BACKEND_UNAVAILABLE
        return PauseReason.NONE

    def _default_executor(self) -> TaskExecutor:
        """Wire the plan to the real, existing acquisition pipeline."""

        from core.task_runner import TaskRunner

        runner = TaskRunner(
            self.settings, library=self.library, browser_backend=self.browser_backend
        )

        async def execute(request: TaskRequest) -> PipelineResult:
            return await runner.run_async(request, on_event=self.on_event)

        return execute
