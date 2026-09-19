"""Milestone 7 regressions: plans, budgets, approval, execution, recovery.

The plan runner is exercised with an injected task executor, so no test touches
Douyin, Qwen or the network.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.dependencies import build_library
from core.models import (
    ClipRecord,
    MaterialForm,
    MaterialState,
    PipelineResult,
    PipelineStats,
    ProcessStage,
    ReviewStatus,
    SegmentTiming,
    ShotType,
    SourceVideoStatus,
    SubtitleType,
    TaskRequest,
    TaskStatus,
)
from core.plan_runner import PlanRunner
from core.plan_service import PlanService
from core.planner import Planner
from core.plans import (
    CollectionPlanItem,
    PauseReason,
    PlanItemStatus,
    PlanProgress,
    PlanStatus,
    QueryOrigin,
)
from core.provenance import DOUYIN_REAL
from storage.plans import PlanRepository


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _clip(
    clip_id: int,
    *,
    stage: ProcessStage,
    category: str = "苹果干",
    score: float = 0.85,
    review: ReviewStatus = ReviewStatus.UNREVIEWED,
) -> ClipRecord:
    """A lightweight ClipRecord for executor results (no files needed)."""

    return ClipRecord(
        id=clip_id,
        material="苹果",
        material_form=MaterialForm.SLICE,
        material_state=MaterialState.DRYING,
        process_stage=stage,
        subtitle_type=SubtitleType.BOTTOM_SIMPLE,
        duration=6.0,
        overall_score=score,
        review_status=review,
        library_category=category,
        file_path=Path(f"/library/{clip_id}.mp4"),
        source_start=0.0,
        source_end=6.0,
    )


def _result(
    clips: list[ClipRecord],
    *,
    task_id: int = 1,
    tokens: int = 1000,
    calls: int = 3,
    candidates: int = 5,
    unique: int = 4,
    previews: int = 4,
    downloads: int = 1,
    status: TaskStatus = TaskStatus.SUCCEEDED,
    discovery_blocked: bool = False,
    discovery_states: dict[str, str] | None = None,
) -> PipelineResult:
    return PipelineResult(
        task_id=task_id,
        material="苹果干",
        status=status,
        stats=PipelineStats(
            searched_candidates=candidates,
            unique_candidates=unique,
            prescreened=previews,
            downloads=downloads,
            clips_saved=len(clips),
        ),
        clips=clips,
        ai_usage={"ai_calls": calls, "total_tokens": tokens},
        discovery_blocked=discovery_blocked,
        discovery_states=discovery_states or {},
    )


class FakeExecutor:
    """Deterministic task executor: one scripted result per call."""

    def __init__(self, results: list[PipelineResult]) -> None:
        self.results = results
        self.requests: list[TaskRequest] = []

    async def __call__(self, request: TaskRequest) -> PipelineResult:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.results) - 1)
        return self.results[index]


def _seed_gap_library(settings, *, category: str = "苹果干") -> Any:
    """A tiny library with a clear coverage gap and query history."""

    library = build_library(settings)
    # one clip in tray_arrangement -> inside_dryer/drying are missing
    from tests.test_m5_coverage import _add_clip

    _add_clip(
        library,
        settings.paths.library_root,
        index=1,
        category=category,
        stage=ProcessStage.TRAY_ARRANGEMENT,
        provenance=DOUYIN_REAL,
    )
    task_id = library.create_task(TaskRequest(material=category, target_clip_count=2))
    library.add_search_yield(
        task_id=task_id,
        platform="douyin",
        query="苹果干烘干",
        candidate_count=20,
        unique_candidate_count=12,
        preview_accept_count=4,
        download_count=2,
        final_clip_count=3,
    )
    library.add_search_yield(
        task_id=task_id,
        platform="douyin",
        query="苹果烘干机",
        candidate_count=30,
        unique_candidate_count=3,
        preview_accept_count=0,
        download_count=0,
        final_clip_count=0,
    )
    library.add_search_yield(
        task_id=task_id,
        platform="douyin",
        query="苹果片烘干",
        candidate_count=10,
        unique_candidate_count=8,
        preview_accept_count=2,
        download_count=1,
        final_clip_count=1,
    )
    # subtitle rejection history: the low-yield query is the subtitle-heavy one
    library.upsert_source_video(
        task_id=task_id,
        platform="douyin",
        platform_video_id="7652321152866000200",
        source_url="https://www.douyin.com/video/7652321152866000200",
        status=SourceVideoStatus.REJECTED_PREVIEW,
        matched_queries=["苹果烘干机"],
    )
    library.database.execute(
        "UPDATE source_videos SET reject_reason = ? WHERE platform_video_id = ?",
        ("subtitle_too_complex", "7652321152866000200"),
    )
    return library


@pytest.fixture
def seeded(settings):
    library = _seed_gap_library(settings)
    service = PlanService(library, settings, repository=PlanRepository(library.database))
    return library, service, settings


# ---------------------------------------------------------------------------
# 1/2/39. schema + repository
# ---------------------------------------------------------------------------
def test_plan_tables_exist_and_migration_is_idempotent(settings) -> None:
    library = build_library(settings)
    assert library.database.missing_tables() == []
    for table in (
        "collection_plans",
        "collection_plan_items",
        "collection_plan_tasks",
        "collection_plan_events",
    ):
        assert library.database.count(table) == 0
    library.database.initialize()  # second call must not fail or duplicate rows
    assert library.database.missing_tables() == []


def test_repository_round_trip(settings) -> None:
    library = build_library(settings)
    repo = PlanRepository(library.database)
    plan_id = repo.create_plan(name="t", library_category="苹果干")
    item = CollectionPlanItem(
        process_stage="drying", current_count=0, target_count=8, gap=8, requested_clips=3
    )
    item_id = repo.add_item(plan_id, item)
    repo.log_event(plan_id, "created", plan_item_id=item_id, details={"x": 1})
    repo.link_task(plan_id=plan_id, plan_item_id=item_id, task_id=7, query="q")
    plan = repo.get_plan(plan_id)
    assert plan is not None
    assert plan.items[0].process_stage == "drying"
    assert repo.events(plan_id)[0]["event"] == "created"
    assert repo.linked_task_ids(plan_id) == [7]
    assert repo.item_task_ids(item_id) == [7]


# ---------------------------------------------------------------------------
# 3/4/5/6. generation, healthy filter, priority, ranking
# ---------------------------------------------------------------------------
def test_draft_plan_from_real_gaps(seeded) -> None:
    _library, service, _settings = seeded
    plan, action = service.create_plan("苹果干")
    assert action.ok and plan is not None
    assert plan.status is PlanStatus.DRAFT
    assert plan.approved_at is None
    stages = {item.process_stage for item in plan.items}
    assert "drying" in stages and "tray_arrangement" not in stages
    for item in plan.items:
        assert item.requested_clips <= service.settings.collection_planning.max_requested_clips_per_stage
        assert item.queries, "every item needs at least one query"
        assert item.max_tokens > 0 and item.max_downloads > 0 and item.max_candidates > 0
    assert plan.max_ai_tokens <= service.settings.collection_planning.max_plan_ai_tokens
    assert plan.coverage_before["process_stage"]["drying"]["current"] == 0


def test_healthy_stages_are_excluded_unless_requested(seeded) -> None:
    _library, service, settings = seeded
    planner = service.planner
    # enough slots that the healthy stage is reachable at all
    settings.collection_planning.max_items_per_plan = 20
    settings.collection_planning.max_plan_target_clips = 100
    # make tray_arrangement healthy: its target equals what the library already has
    planner.coverage.settings.preferred_process_stages["tray_arrangement"] = 1
    draft = planner.build_plan("苹果干")
    assert "tray_arrangement" not in {item.process_stage for item in draft.items}
    with_healthy = planner.build_plan("苹果干", include_healthy=True)
    assert "tray_arrangement" in {item.process_stage for item in with_healthy.items}


def test_priority_ordering_critical_first(seeded) -> None:
    _library, service, settings = seeded
    settings.coverage.preferred_process_stages["drying"] = 2
    settings.coverage.preferred_process_stages["inside_dryer"] = 40
    draft = service.planner.build_plan("苹果干")
    order = [item.process_stage for item in draft.sorted_items()]
    priorities = [item.priority for item in draft.sorted_items()]
    assert priorities == sorted(
        priorities, key=lambda value: {"critical": 0, "high": 1, "medium": 2}.get(value, 9)
    )
    assert "inside_dryer" in order


def test_query_ranking_prefers_productive_history(seeded) -> None:
    _library, service, _settings = seeded
    queries = service.planner.rank_queries("苹果干", "drying")
    assert queries[0].query == "苹果干烘干", "the productive historical query wins"
    assert queries[0].origin is QueryOrigin.HISTORICAL
    assert queries[0].clips == 3
    zero_yield = [query for query in queries if query.query == "苹果烘干机"]
    assert not zero_yield, "a query with zero clips is not recommended from history"
    assert any(query.origin is QueryOrigin.GENERATED_TEMPLATE for query in queries)


def test_subtitle_and_duplicate_penalties(seeded) -> None:
    _library, service, _settings = seeded
    planner = service.planner
    clean = planner.score_query(
        planner.query_statistics()["苹果干烘干"]
    )[0]
    # a query with identical yield but a high subtitle rejection rate scores lower
    from core.planner import QueryStatistics

    noisy = QueryStatistics(
        query="x", runs=1, candidates=20, unique_candidates=12,
        preview_accepted=4, downloads=2, clips=3, subtitle_rejection_rate=0.8,
    )
    quiet = QueryStatistics(
        query="y", runs=1, candidates=20, unique_candidates=12,
        preview_accepted=4, downloads=2, clips=3, subtitle_rejection_rate=0.0,
    )
    assert planner.score_query(noisy)[0] < planner.score_query(quiet)[0]
    # heavy rediscovery (few unique candidates) is penalised as well
    duplicated = QueryStatistics(
        query="z", runs=1, candidates=30, unique_candidates=3,
        preview_accepted=4, downloads=2, clips=3,
    )
    assert planner.score_query(duplicated)[0] < planner.score_query(quiet)[0]
    assert clean > 0


def test_token_efficiency_affects_ranking(seeded) -> None:
    _library, service, _settings = seeded
    planner = service.planner
    from core.planner import QueryStatistics

    cheap = QueryStatistics(
        query="cheap", runs=1, candidates=10, unique_candidates=8,
        preview_accepted=3, downloads=2, clips=2, tokens_per_clip=5000,
    )
    expensive = QueryStatistics(
        query="expensive", runs=1, candidates=10, unique_candidates=8,
        preview_accepted=3, downloads=2, clips=2, tokens_per_clip=90000,
    )
    assert planner.score_query(cheap)[0] > planner.score_query(expensive)[0]


# ---------------------------------------------------------------------------
# 8/9/10/11. budgets
# ---------------------------------------------------------------------------
def test_budgets_fit_global_plan_limits(seeded) -> None:
    _library, service, settings = seeded
    settings.collection_planning.max_requested_clips_per_stage = 5
    settings.collection_planning.max_plan_target_clips = 20
    settings.collection_planning.max_plan_previews = 10
    settings.collection_planning.max_plan_downloads = 4
    settings.collection_planning.max_plan_ai_tokens = 50000
    (settings.collection_planning.max_items_per_plan) = 3
    draft = service.planner.build_plan("苹果干")
    # the item budgets are scaled down so their sums fit the plan ceilings
    assert sum(item.max_tokens for item in draft.items) <= draft.max_ai_tokens
    assert sum(item.max_downloads for item in draft.items) <= draft.max_downloads
    assert sum(item.max_candidates for item in draft.items) <= draft.max_preview_candidates
    assert draft.max_preview_candidates <= 10
    assert draft.max_downloads <= 4
    assert draft.max_ai_tokens <= 50000
    assert service.validate(draft) == []


def test_plan_validation_rejects_oversized_budgets(seeded) -> None:
    _library, service, settings = seeded
    draft = service.planner.build_plan("苹果干")
    draft.max_ai_tokens = settings.collection_planning.max_plan_ai_tokens + 1
    draft.max_downloads = settings.collection_planning.max_plan_downloads + 5
    problems = service.validate(draft)
    assert any("token" in problem for problem in problems)
    assert any("下载" in problem for problem in problems)


def test_item_edit_validation(seeded) -> None:
    _library, service, settings = seeded
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    item = plan.sorted_items()[0]

    too_many = service.edit_item(plan.id, item.id, requested_clips=99)
    assert not too_many.ok and "不能超过" in too_many.message
    zero = service.edit_item(plan.id, item.id, max_tokens=0)
    assert not zero.ok
    bad_priority = service.edit_item(plan.id, item.id, priority="urgent")
    assert not bad_priority.ok
    empty_queries = service.edit_item(plan.id, item.id, queries=["   "])
    assert not empty_queries.ok

    ok = service.edit_item(
        plan.id,
        item.id,
        requested_clips=2,
        max_candidates=6,
        max_downloads=2,
        max_tokens=20000,
        priority="high",
        queries=["苹果干 烘干机内部", "苹果切片烘干"],
    )
    assert ok.ok
    updated = service.get_plan(plan.id)
    assert updated is not None
    entry = next(entry for entry in updated.items if entry.id == item.id)
    assert entry.requested_clips == 2
    assert entry.max_tokens == 20000
    assert [query.query for query in entry.queries] == ["苹果干 烘干机内部", "苹果切片烘干"]
    assert updated.target_final_clips >= 2


def test_edit_refused_after_approval(seeded) -> None:
    _library, service, _settings = seeded
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    item = plan.items[0]
    assert service.approve(plan.id).ok
    refused = service.edit_item(plan.id, item.id, requested_clips=1)
    assert not refused.ok
    assert "只有草稿可以编辑" in refused.message


# ---------------------------------------------------------------------------
# 11/12/13. approval workflow
# ---------------------------------------------------------------------------
def test_approval_is_required_before_running(seeded) -> None:
    library, service, settings = seeded
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    executor = FakeExecutor([_result([_clip(1, stage=ProcessStage.DRYING)])])
    runner = PlanRunner(
        plan.id, library=library, settings=settings, executor=executor
    )
    result = run(runner.run())
    assert result.refused and "批准" in result.refused
    assert executor.requests == [], "an unapproved plan must not execute anything"
    assert service.get_plan(plan.id).status is PlanStatus.DRAFT


def test_approved_plan_runs(seeded) -> None:
    library, service, settings = seeded
    settings.collection_planning.max_items_per_plan = 2
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    for item in plan.items:
        service.edit_item(
            plan.id,
            item.id,
            requested_clips=1,
            max_candidates=30,
            max_downloads=5,
            max_tokens=200000,
        )
    refreshed = service.get_plan(plan.id)
    assert refreshed is not None
    target_stages = [item.process_stage for item in refreshed.sorted_items()]
    calls = {"n": 0}
    assert service.approve(plan.id, note="acceptance").ok

    async def execute(request: TaskRequest) -> PipelineResult:
        calls["n"] += 1
        stage = target_stages[min(calls["n"] - 1, len(target_stages) - 1)]
        return _result(
            [_clip(calls["n"], stage=ProcessStage(stage))], task_id=calls["n"]
        )

    runner = PlanRunner(plan.id, library=library, settings=settings, executor=execute)
    result = run(runner.run())
    assert result.ok
    assert result.status in (PlanStatus.COMPLETED, PlanStatus.PARTIALLY_COMPLETED)
    assert calls["n"] == len(target_stages), "each objective is satisfied by one task"
    assert result.qualifying_clips >= 1
    repo = PlanRepository(library.database)
    assert repo.linked_task_ids(plan.id), "plan → task linkage must be persisted"


def test_approval_rejects_invalid_plan(seeded) -> None:
    library, service, settings = seeded
    repo = PlanRepository(library.database)
    plan_id = repo.create_plan(name="empty", library_category="苹果干")
    action = service.approve(plan_id)
    assert not action.ok
    assert "校验" in action.message or "目标" in action.message


# ---------------------------------------------------------------------------
# 14/15/16/17. execution semantics
# ---------------------------------------------------------------------------
def test_qualifying_vs_off_target_clips(seeded) -> None:
    library, service, settings = seeded
    settings.collection_planning.max_items_per_plan = 1
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    item = plan.items[0]
    service.edit_item(plan.id, item.id, requested_clips=2, max_downloads=4, max_tokens=100000)
    refreshed = service.get_plan(plan.id)
    assert refreshed is not None
    target_stage = refreshed.sorted_items()[0].process_stage
    assert service.approve(plan.id).ok

    async def execute(request: TaskRequest) -> PipelineResult:
        # a valid clip that does NOT show the target stage
        return _result([_clip(1, stage=ProcessStage.PREPARATION)], task_id=1)

    runner = PlanRunner(plan.id, library=library, settings=settings, executor=execute)
    result = run(runner.run())
    updated = service.get_plan(plan.id)
    assert updated is not None
    assert updated.progress.clips_saved >= 1, "off-target material still enters the library"
    assert updated.progress.qualifying_clips == 0, "off-target clips do not satisfy the goal"
    assert result.qualifying_clips == 0
    effectiveness = service.effectiveness(updated)
    assert effectiveness["off_target_clips"] >= 1
    assert effectiveness["objective_hit_rate"] == 0.0
    assert target_stage in {
        entry.process_stage for entry in updated.items
    }


def test_stops_when_objective_reached(seeded) -> None:
    library, service, settings = seeded
    settings.collection_planning.max_items_per_plan = 1
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    item = plan.items[0]
    service.edit_item(plan.id, item.id, requested_clips=1, max_tokens=200000, max_downloads=5)
    refreshed = service.get_plan(plan.id)
    assert refreshed is not None
    stage = refreshed.sorted_items()[0].process_stage
    assert service.approve(plan.id).ok

    calls = {"n": 0}

    async def execute(request: TaskRequest) -> PipelineResult:
        calls["n"] += 1
        # first call produces an off-target clip, second the qualifying one
        if calls["n"] == 1:
            return _result([_clip(1, stage=ProcessStage.PREPARATION)], task_id=1)
        return _result([_clip(2, stage=ProcessStage(stage))], task_id=2)

    runner = PlanRunner(plan.id, library=library, settings=settings, executor=execute)
    result = run(runner.run())
    assert calls["n"] == 2, "the item stops as soon as the objective is met"
    assert result.qualifying_clips >= 1


def test_preview_budget_stops_an_item(seeded) -> None:
    library, service, settings = seeded
    settings.collection_planning.max_items_per_plan = 1
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    item = plan.items[0]
    service.edit_item(plan.id, item.id, requested_clips=5, max_candidates=1, max_tokens=100000)
    assert service.approve(plan.id).ok

    async def execute(request: TaskRequest) -> PipelineResult:
        return _result([], task_id=1, unique=5, candidates=5)

    runner = PlanRunner(plan.id, library=library, settings=settings, executor=execute)
    result = run(runner.run())
    assert result.status is PlanStatus.PAUSED
    assert result.pause_reason in (
        PauseReason.PREVIEW_BUDGET_EXHAUSTED.value,
        PauseReason.TOKEN_BUDGET_EXHAUSTED.value,
        PauseReason.DOWNLOAD_BUDGET_EXHAUSTED.value,
    )


def test_download_budget_stops_an_item(seeded) -> None:
    library, service, settings = seeded
    settings.collection_planning.max_items_per_plan = 1
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    item = plan.items[0]
    service.edit_item(plan.id, item.id, requested_clips=5, max_downloads=1, max_tokens=500000, max_candidates=50)
    assert service.approve(plan.id).ok

    async def execute(request: TaskRequest) -> PipelineResult:
        return _result([_clip(1, stage=ProcessStage.PREPARATION)], task_id=1, downloads=1)

    runner = PlanRunner(plan.id, library=library, settings=settings, executor=execute)
    result = run(runner.run())
    assert result.status is PlanStatus.PAUSED
    assert result.pause_reason == PauseReason.DOWNLOAD_BUDGET_EXHAUSTED.value


def test_token_hard_limit_stops_an_item(seeded) -> None:
    library, service, settings = seeded
    settings.collection_planning.max_items_per_plan = 1
    settings.collection_planning.token_reserve_ratio = 0.1
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    item = plan.items[0]
    service.edit_item(
        plan.id, item.id, requested_clips=5, max_tokens=10000, max_candidates=50, max_downloads=10
    )
    assert service.approve(plan.id).ok

    async def execute(request: TaskRequest) -> PipelineResult:
        # a single task that consumes the whole item token budget
        return _result([_clip(1, stage=ProcessStage.PREPARATION)], task_id=1, tokens=12000)

    runner = PlanRunner(plan.id, library=library, settings=settings, executor=execute)
    result = run(runner.run())
    assert result.status is PlanStatus.PAUSED
    assert result.pause_reason == PauseReason.TOKEN_BUDGET_EXHAUSTED.value


# ---------------------------------------------------------------------------
# 18-23. pause / resume / cancel / crash recovery / blocking states
# ---------------------------------------------------------------------------
def test_pause_and_resume_are_cooperative(seeded) -> None:
    library, service, settings = seeded
    settings.collection_planning.max_items_per_plan = 2
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    for item in plan.items:
        service.edit_item(plan.id, item.id, requested_clips=3, max_tokens=500000, max_downloads=10)
    refreshed = service.get_plan(plan.id)
    assert refreshed is not None
    assert service.approve(plan.id).ok

    calls = {"n": 0}
    runner_holder: dict[str, PlanRunner] = {}

    async def execute(request: TaskRequest) -> PipelineResult:
        calls["n"] += 1
        # ask for a pause: the runner must stop after this (safe) unit of work
        runner_holder["runner"].request_pause(PauseReason.OPERATOR)
        return _result([_clip(calls["n"], stage=ProcessStage.PREPARATION)], task_id=calls["n"])

    runner = PlanRunner(plan.id, library=library, settings=settings, executor=execute)
    runner_holder["runner"] = runner
    result = run(runner.run())
    assert result.status is PlanStatus.PAUSED
    assert result.pause_reason == PauseReason.OPERATOR.value
    assert calls["n"] == 1, "the current unit of work finished before pausing"
    stored = service.get_plan(plan.id)
    assert stored is not None and stored.status is PlanStatus.PAUSED
    assert stored.progress.clips_saved >= 1, "completed work is checkpointed"

    # resume: persisted state continues instead of restarting the queries
    runner.request_resume()
    second = run(runner.run())
    assert second.ok
    assert calls["n"] >= 2


def test_cancel_keeps_results_and_tasks(seeded) -> None:
    library, service, settings = seeded
    settings.collection_planning.max_items_per_plan = 2
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    for item in plan.items:
        service.edit_item(plan.id, item.id, requested_clips=3, max_tokens=500000, max_downloads=10)
    assert service.approve(plan.id).ok

    runner_holder: dict[str, PlanRunner] = {}

    async def execute(request: TaskRequest) -> PipelineResult:
        runner_holder["runner"].request_cancel()
        return _result([_clip(1, stage=ProcessStage.PREPARATION)], task_id=1)

    runner = PlanRunner(plan.id, library=library, settings=settings, executor=execute)
    runner_holder["runner"] = runner
    result = run(runner.run())
    assert result.status is PlanStatus.CANCELLED
    repo = PlanRepository(library.database)
    assert repo.linked_task_ids(plan.id), "cancelled plans keep their tasks"
    stored = service.get_plan(plan.id)
    assert stored is not None and stored.progress.clips_saved >= 1
    assert service.cancel(plan.id).ok is False or service.get_plan(plan.id).status is PlanStatus.CANCELLED


def test_crash_recovery_normalizes_running_plans(seeded) -> None:
    library, service, _settings = seeded
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    repo = PlanRepository(library.database)
    repo.set_status(plan.id, PlanStatus.RUNNING, mark_started=True)
    normalized = repo.normalize_interrupted_plans()
    assert normalized == [plan.id]
    stored = repo.get_plan(plan.id)
    assert stored is not None
    assert stored.status is PlanStatus.PAUSED
    assert stored.pause_reason == PauseReason.INTERRUPTED.value
    events = [entry["event"] for entry in repo.events(plan.id)]
    assert "interrupted" in events
    # nothing is auto-resumed
    assert repo.normalize_interrupted_plans() == []


def test_human_verification_pauses_the_plan(seeded) -> None:
    library, service, settings = seeded
    settings.collection_planning.max_items_per_plan = 2
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    for item in plan.items:
        service.edit_item(plan.id, item.id, requested_clips=3, max_tokens=500000, max_downloads=10)
    assert service.approve(plan.id).ok

    async def execute(request: TaskRequest) -> PipelineResult:
        return _result(
            [],
            task_id=1,
            status=TaskStatus.PARTIAL,
            discovery_blocked=True,
            discovery_states={"browser": "verification_required"},
        )

    runner = PlanRunner(plan.id, library=library, settings=settings, executor=execute)
    result = run(runner.run())
    assert result.status is PlanStatus.PAUSED
    assert result.pause_reason == PauseReason.HUMAN_VERIFICATION_REQUIRED.value
    stored = service.get_plan(plan.id)
    assert stored is not None and stored.pause_reason == PauseReason.HUMAN_VERIFICATION_REQUIRED.value


def test_backend_unavailable_pauses_without_burning_queries(seeded) -> None:
    library, service, settings = seeded
    settings.collection_planning.max_items_per_plan = 2
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    for item in plan.items:
        service.edit_item(plan.id, item.id, requested_clips=3, max_tokens=500000, max_downloads=10)
    calls = {"n": 0}
    assert service.approve(plan.id).ok

    async def execute(request: TaskRequest) -> PipelineResult:
        calls["n"] += 1
        return _result(
            [],
            task_id=calls["n"],
            status=TaskStatus.PARTIAL,
            discovery_blocked=True,
            discovery_states={"dtk": "backend_unavailable"},
        )

    runner = PlanRunner(plan.id, library=library, settings=settings, executor=execute)
    result = run(runner.run())
    assert result.status is PlanStatus.PAUSED
    assert result.pause_reason == PauseReason.BACKEND_UNAVAILABLE.value
    assert calls["n"] == 1, "a dead backend must not consume the remaining queries"


# ---------------------------------------------------------------------------
# 24-29. checkpoints, snapshots, effectiveness, dry-run
# ---------------------------------------------------------------------------
def test_progress_is_checkpointed_after_each_task(seeded) -> None:
    library, service, settings = seeded
    settings.collection_planning.max_items_per_plan = 1
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    for item in plan.items:
        service.edit_item(plan.id, item.id, requested_clips=1, max_tokens=500000, max_downloads=10)
    assert service.approve(plan.id).ok

    seen: list[dict[str, Any]] = []
    repo = PlanRepository(library.database)

    async def execute(request: TaskRequest) -> PipelineResult:
        stored = repo.get_plan(plan.id)
        seen.append(stored.progress.model_dump() if stored else {})
        return _result([_clip(1, stage=ProcessStage.PREPARATION)], task_id=1, tokens=500)

    runner = PlanRunner(plan.id, library=library, settings=settings, executor=execute)
    run(runner.run())
    stored = repo.get_plan(plan.id)
    assert stored is not None
    assert stored.progress.clips_saved >= 1
    assert stored.progress.ai_tokens >= 500
    assert stored.progress.downloads >= 1
    assert stored.progress.stage_breakdown.get("preparation", 0) >= 1
    assert seen, "the runner persisted progress before the next task"


def test_coverage_snapshots_and_delta(seeded) -> None:
    library, service, settings = seeded
    settings.collection_planning.max_items_per_plan = 1
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    for item in plan.items:
        service.edit_item(plan.id, item.id, requested_clips=2, max_tokens=500000, max_downloads=10)
    refreshed = service.get_plan(plan.id)
    assert refreshed is not None
    target_stage = refreshed.sorted_items()[0].process_stage
    assert service.approve(plan.id).ok

    # simulate the library gaining a clip in the target stage
    from tests.test_m5_coverage import _add_clip

    _add_clip(
        library,
        settings.paths.library_root,
        index=2,
        category="苹果干",
        stage=ProcessStage(target_stage),
        provenance=DOUYIN_REAL,
    )

    async def execute(request: TaskRequest) -> PipelineResult:
        return _result([_clip(9, stage=ProcessStage(target_stage))], task_id=1)

    runner = PlanRunner(plan.id, library=library, settings=settings, executor=execute)
    run(runner.run())
    stored = service.get_plan(plan.id)
    assert stored is not None
    assert stored.coverage_after, "the post-run snapshot must be stored"
    effectiveness = service.effectiveness(stored)
    assert effectiveness["coverage_delta"].get(target_stage, 0) >= 1
    assert effectiveness["before"] and effectiveness["after"]


def test_effectiveness_metrics(seeded) -> None:
    library, service, settings = seeded
    settings.collection_planning.max_items_per_plan = 1
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    for item in plan.items:
        service.edit_item(plan.id, item.id, requested_clips=1, max_tokens=500000, max_downloads=10)
    refreshed = service.get_plan(plan.id)
    assert refreshed is not None
    stage = refreshed.sorted_items()[0].process_stage
    calls = {"n": 0}
    assert service.approve(plan.id).ok

    async def execute(request: TaskRequest) -> PipelineResult:
        calls["n"] += 1
        # one qualifying clip plus one off-target clip in the same task
        return _result(
            [
                _clip(calls["n"] * 2, stage=ProcessStage(stage)),
                _clip(calls["n"] * 2 + 1, stage=ProcessStage.PACKAGING),
            ],
            task_id=calls["n"],
            tokens=1000,
            downloads=1,
        )

    runner = PlanRunner(plan.id, library=library, settings=settings, executor=execute)
    run(runner.run())
    stored = service.get_plan(plan.id)
    assert stored is not None
    effectiveness = service.effectiveness(stored)
    assert effectiveness["qualifying_clips"] >= 1
    assert effectiveness["off_target_clips"] >= 1
    assert effectiveness["objective_hit_rate"] is not None
    assert effectiveness["tokens_per_qualifying_clip"] is not None
    assert effectiveness["downloads_per_qualifying_clip"] is not None
    assert effectiveness["stage_breakdown"]


def test_dry_run_makes_no_external_calls(seeded) -> None:
    library, service, settings = seeded
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    calls = {"n": 0}

    async def execute(request: TaskRequest) -> PipelineResult:
        calls["n"] += 1
        return _result([], task_id=1)

    runner = PlanRunner(plan.id, library=library, settings=settings, executor=execute)
    result = run(runner.run(dry_run=True))
    assert result.dry_run and result.ok
    assert calls["n"] == 0
    assert service.get_plan(plan.id).status is PlanStatus.DRAFT


# ---------------------------------------------------------------------------
# 30/31. CLI + Gradio
# ---------------------------------------------------------------------------
def test_plan_cli_lifecycle(seeded, capsys) -> None:
    import app as app_module

    library, service, settings = seeded
    args = app_module.parse_args(["--create-collection-plan", "苹果干"])
    assert app_module.run_create_collection_plan(args, settings) == 0
    output = capsys.readouterr().out
    assert "已创建草稿计划" in output
    plans = service.list_plans()
    assert plans
    plan_id = plans[0].id

    assert app_module.run_list_collection_plans(settings) == 0
    assert app_module.run_show_collection_plan(settings, plan_id) == 0
    assert app_module.run_plan_dry_run(settings, plan_id) == 0
    output = capsys.readouterr().out
    assert "dry-run" in output and "没有调用 Douyin" in output
    assert app_module.run_approve_collection_plan(settings, plan_id, note="cli") == 0
    assert service.get_plan(plan_id).status is PlanStatus.APPROVED


def test_plan_control_cli(seeded, capsys) -> None:
    import app as app_module

    library, service, settings = seeded
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    service.approve(plan.id)
    repo = PlanRepository(library.database)
    repo.set_status(plan.id, PlanStatus.RUNNING, mark_started=True)
    assert app_module.run_plan_control(settings, pause_id=plan.id) == 0
    assert service.get_plan(plan.id).status is PlanStatus.PAUSED
    assert app_module.run_plan_control(settings, resume_id=plan.id) == 0
    assert service.get_plan(plan.id).status is PlanStatus.RUNNING
    assert app_module.run_plan_control(settings, cancel_id=plan.id) == 0
    assert service.get_plan(plan.id).status is PlanStatus.CANCELLED
    capsys.readouterr()


@pytest.mark.skipif(
    not __import__("ui.gradio_app", fromlist=["GRADIO_AVAILABLE"]).GRADIO_AVAILABLE,
    reason="gradio is not installed",
)
def test_gradio_plan_tab_builds(settings, runner) -> None:
    from ui.gradio_app import build_ui

    demo = build_ui(settings, runner)
    labels = [getattr(block, "label", None) for block in demo.blocks.values()]
    for tab in ("素材采集", "素材库", "素材覆盖", "采集计划", "任务记录", "系统检查"):
        assert tab in labels, f"missing tab: {tab}"
