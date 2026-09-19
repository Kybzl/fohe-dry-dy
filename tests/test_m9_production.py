"""Milestone 9: coverage-driven production acquisition.

Covers the production gap model, the priority algorithm, objective-specific
query generation, duplicate/rediscovery down-ranking, qualifying vs off-target
semantics, budget enforcement, provider-failure semantics and the CLI.

No test touches Douyin or Qwen: the plan runner uses injected executors and the
library is a scratch SQLite file from the ``settings`` fixture.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.config import load_settings
from core.dependencies import build_library
from core.models import (
    ClipRecord,
    MaterialForm,
    MaterialState,
    PipelineResult,
    PipelineStats,
    ProcessStage,
    ReviewStatus,
    SourceVideoStatus,
    SubtitleType,
    TaskRequest,
    TaskStatus,
)
from core.plan_runner import PlanRunner
from core.plan_service import PlanService
from core.plans import PauseReason, PlanItemStatus, PlanStatus
from core.production import ProductionCoverageService
from core.provenance import DOUYIN_REAL
from storage.plans import PlanRepository


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _add_clip(library, root: Path, **kwargs):
    from tests.test_m5_coverage import _add_clip as add

    return add(library, root, **kwargs)


def _service(settings, library=None) -> ProductionCoverageService:
    return ProductionCoverageService(library or build_library(settings), settings)


def _clip(clip_id: int, *, stage: ProcessStage, category: str = "辣椒干") -> ClipRecord:
    return ClipRecord(
        id=clip_id,
        material="辣椒",
        material_form=MaterialForm.SLICE,
        material_state=MaterialState.DRYING,
        process_stage=stage,
        subtitle_type=SubtitleType.BOTTOM_SIMPLE,
        duration=6.0,
        overall_score=0.85,
        review_status=ReviewStatus.UNREVIEWED,
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
    candidates: int = 5,
    unique: int = 3,
    previews: int = 2,
    downloads: int = 1,
) -> PipelineResult:
    return PipelineResult(
        task_id=task_id,
        material="辣椒",
        status=TaskStatus.SUCCEEDED,
        stats=PipelineStats(
            searched_candidates=candidates,
            unique_candidates=unique,
            prescreened=previews,
            downloads=downloads,
            clips_saved=len(clips),
        ),
        clips=clips,
        ai_usage={"ai_calls": 3, "total_tokens": tokens},
    )


# ---------------------------------------------------------------------------
# 1/2. gap calculation + target configuration
# ---------------------------------------------------------------------------
def test_gap_is_library_state_minus_configured_target(settings) -> None:
    library = build_library(settings)
    _add_clip(library, settings.paths.library_root, index=1, category="辣椒干", stage=ProcessStage.DRYING)
    service = _service(settings, library)
    service.production.process_stage_targets = {"drying": 3}
    rows = {row.stage: row for row in service.gaps(category="辣椒干")}
    assert rows["drying"].current == 1
    assert rows["drying"].target == 3
    assert rows["drying"].gap == 2


def test_covered_stage_is_hidden_unless_requested(settings) -> None:
    library = build_library(settings)
    for index in range(1, 3):
        _add_clip(
            library,
            settings.paths.library_root,
            index=index,
            category="辣椒干",
            stage=ProcessStage.DRYING,
        )
    service = _service(settings, library)
    service.production.process_stage_targets = {"drying": 2}
    assert service.gaps(category="辣椒干") == []
    covered = service.gaps(category="辣椒干", include_covered=True)
    assert [row.stage for row in covered] == ["drying"]
    assert covered[0].status == "covered"


def test_only_configured_stages_become_objectives(settings) -> None:
    library = build_library(settings)
    service = _service(settings, library)
    service.production.process_stage_targets = {"drying": 1}
    stages = {row.stage for row in service.gaps(category="辣椒干")}
    assert stages == {"drying"}, "the coverage enum must not define the objective set"


# ---------------------------------------------------------------------------
# 3. deterministic priority ordering
# ---------------------------------------------------------------------------
def test_priority_ordering_is_deterministic_and_stage_first(settings) -> None:
    library = build_library(settings)
    service = _service(settings, library)
    service.production.process_stage_targets = {"drying": 1, "preparation": 1}
    first = service.gap_rows(category="辣椒干")
    second = service.gap_rows(category="辣椒干")
    assert first == second, "priority must be reproducible"
    stages = [row["process_stage"] for row in first]
    assert stages.index("drying") < stages.index("preparation")
    components = first[0]["priority_components"]
    assert components["stage"] > components.get("gap", 0) or components["stage"] >= 1.0
    # the components must add up to the reported priority
    assert round(sum(components.values()), 3) == pytest.approx(first[0]["priority"], abs=0.01)


def test_priority_rewards_edit_role_and_penalises_rediscovery(settings) -> None:
    library = build_library(settings)
    service = _service(settings, library)
    service.production.process_stage_targets = {"drying": 1}
    baseline = service.gaps(category="辣椒干")[0]
    # a query whose discovered videos are all already processed must rank lower
    task_id = library.create_task(TaskRequest(material="辣椒", target_clip_count=1))
    library.upsert_source_video(
        task_id=task_id,
        platform="douyin",
        platform_video_id="7900000000000000001",
        source_url="https://www.douyin.com/video/7900000000000000001",
        status=SourceVideoStatus.PROCESSED,
        matched_queries=[baseline.best_query],
    )
    service._stats = None
    after = _service(settings, library).gaps(category="辣椒干")[0]
    assert after.rediscovery_rate == 1.0
    assert after.priority_components["rediscovery_penalty"] < 0
    assert after.priority < baseline.priority


# ---------------------------------------------------------------------------
# 4/5. objective-specific queries + query_rank_v2
# ---------------------------------------------------------------------------
def test_queries_are_objective_specific(settings) -> None:
    library = build_library(settings)
    service = _service(settings, library)
    drying = [query.query for query in service.queries_for("辣椒干", "drying")]
    inside = [query.query for query in service.queries_for("辣椒干", "inside_dryer")]
    assert drying and inside
    assert drying[0] != inside[0]
    assert any("烘干" in query for query in drying)
    assert any("内部" in query for query in inside)
    assert all("辣椒" in query for query in drying + inside)


def test_historical_queries_use_query_rank_v2(settings) -> None:
    library = build_library(settings)
    task_id = library.create_task(TaskRequest(material="辣椒", target_clip_count=1))
    library.add_search_yield(
        task_id=task_id,
        platform="douyin",
        query="辣椒烘干",
        candidate_count=20,
        unique_candidate_count=16,
        preview_accept_count=4,
        download_count=2,
        final_clip_count=3,
    )
    service = _service(settings, library)
    queries = service.queries_for("辣椒干", "drying")
    historical = [query for query in queries if query.origin.value == "historical"]
    assert historical, "productive history must be included"
    assert historical[0].rank_version == "query_rank_v2"
    assert historical[0].components, "v2 exposes its components"
    assert historical[0].query == "辣椒烘干"


def test_material_only_query_never_leads_an_objective(settings) -> None:
    """M8 divergence: a bare material query must not override the objective."""

    library = build_library(settings)
    task_id = library.create_task(TaskRequest(material="辣椒", target_clip_count=1))
    library.add_search_yield(
        task_id=task_id,
        platform="douyin",
        query="辣椒",
        candidate_count=30,
        unique_candidate_count=6,
        preview_accept_count=3,
        download_count=3,
        final_clip_count=3,
    )
    service = _service(settings, library)
    queries = [query.query for query in service.queries_for("辣椒干", "drying")]
    assert queries[0] != "辣椒"
    assert any("烘干" in query for query in queries[:2] + [queries[0]])


def test_duplicate_heavy_query_is_down_ranked(settings) -> None:
    library = build_library(settings)
    task_id = library.create_task(TaskRequest(material="辣椒", target_clip_count=1))
    # one clean, one duplicate-heavy query for the same objective
    library.add_search_yield(
        task_id=task_id,
        platform="douyin",
        query="辣椒烘干",
        candidate_count=20,
        unique_candidate_count=18,
        preview_accept_count=4,
        download_count=3,
        final_clip_count=3,
    )
    library.add_search_yield(
        task_id=task_id,
        platform="douyin",
        query="辣椒烘干房",
        candidate_count=30,
        unique_candidate_count=2,
        preview_accept_count=3,
        download_count=3,
        final_clip_count=3,
    )
    service = _service(settings, library)
    scores = {
        query.query: query.score
        for query in service.queries_for("辣椒干", "drying", limit=4)
    }
    assert scores["辣椒烘干"] > scores["辣椒烘干房"]


# ---------------------------------------------------------------------------
# 6. three outcomes: qualifying / off-target / provider failure
# ---------------------------------------------------------------------------
def _production_plan(settings, library, service):
    draft = service.build_plan(category="辣椒干", stage="drying", limit=1)
    plan_service = PlanService(library, settings, repository=PlanRepository(library.database))
    plan, action = plan_service.save_draft(draft, category="辣椒干")
    assert action.ok, action.message
    return plan_service, plan


def test_qualifying_and_off_target_clips_are_distinguished(settings) -> None:
    library = build_library(settings)
    service = _service(settings, library)
    service.production.process_stage_targets = {"drying": 1}
    plan_service, plan = _production_plan(settings, library, service)
    plan_service.edit_item(plan.id, plan.items[0].id, requested_clips=2, max_candidates=20, max_downloads=5)
    plan = plan_service.sync_budgets(plan.id)
    assert plan_service.approve(plan.id).ok

    results = [
        _result([_clip(1, stage=ProcessStage.PREPARATION), _clip(2, stage=ProcessStage.DRYING)], task_id=1),
        _result([_clip(3, stage=ProcessStage.DRYING)], task_id=2),
    ]
    calls = {"n": 0}

    async def executor(request: TaskRequest) -> PipelineResult:
        calls["n"] += 1
        return results[min(calls["n"] - 1, len(results) - 1)]

    outcome = run(PlanRunner(plan.id, library=library, settings=settings, executor=executor).run())
    stored = plan_service.get_plan(plan.id)
    assert outcome.status is PlanStatus.COMPLETED
    assert stored.progress.qualifying_clips == 2, "only the observed drying clips qualify"
    assert stored.progress.clips_saved == 3, "off-target valid clips stay in the library"
    assert stored.progress.stage_breakdown == {"preparation": 1, "drying": 2}
    assert calls["n"] == 2, "the item stops once its qualifying target is met"


def test_item_stops_as_soon_as_the_target_is_met(settings) -> None:
    library = build_library(settings)
    service = _service(settings, library)
    service.production.process_stage_targets = {"drying": 1}
    plan_service, plan = _production_plan(settings, library, service)
    plan_service.edit_item(plan.id, plan.items[0].id, requested_clips=1, max_downloads=5, max_candidates=20)
    plan = plan_service.sync_budgets(plan.id)
    assert plan_service.approve(plan.id).ok
    calls = {"n": 0}

    async def executor(request: TaskRequest) -> PipelineResult:
        calls["n"] += 1
        return _result([_clip(1, stage=ProcessStage.DRYING)], task_id=calls["n"])

    outcome = run(PlanRunner(plan.id, library=library, settings=settings, executor=executor).run())
    stored = plan_service.get_plan(plan.id)
    assert outcome.status is PlanStatus.COMPLETED
    assert calls["n"] == 1
    assert stored.items[0].status is PlanItemStatus.SATISFIED
    assert len(stored.items[0].queries) > 1, "remaining queries must stay unused"


def test_executed_queries_are_recorded_on_the_item(settings) -> None:
    library = build_library(settings)
    service = _service(settings, library)
    service.production.process_stage_targets = {"drying": 1}
    plan_service, plan = _production_plan(settings, library, service)
    plan_service.edit_item(plan.id, plan.items[0].id, requested_clips=3, max_downloads=5, max_candidates=20)
    plan = plan_service.sync_budgets(plan.id)
    assert plan_service.approve(plan.id).ok
    seen: list[str] = []

    async def executor(request: TaskRequest) -> PipelineResult:
        seen.append(request.query_seed or "")
        assert request.explicit_queries == [request.query_seed], (
            "a plan task runs exactly its objective query"
        )
        return _result([], task_id=len(seen))

    run(PlanRunner(plan.id, library=library, settings=settings, executor=executor).run())
    stored = plan_service.get_plan(plan.id)
    assert stored.items[0].progress.executed_queries == seen


def test_provider_failure_is_not_a_content_rejection(settings, monkeypatch) -> None:
    """The invariant: 'no AI verdict' != 'content rejected'."""

    from analyzers.preview_filter import PreviewFilter
    from ai.base import PreviewFilterRequest
    from core.models import PreviewFrame, PreviewSource, VideoCandidate

    class FailingGateway:
        async def preview_filter(self, request: PreviewFilterRequest):
            return None  # every provider failed

    filter_ = PreviewFilter(
        gateway=FailingGateway(),
        policy=settings.pipeline.default_subtitle_policy,
    )
    frame = PreviewFrame(timestamp=0.0, image_path=settings.paths.cache_dir / "x.jpg")
    preview = PreviewSource(
        platform="douyin",
        platform_video_id="1",
        duration=10.0,
        frames=[frame],
    )
    candidate = VideoCandidate(
        platform="douyin",
        platform_video_id="1",
        source_url="https://www.douyin.com/video/1",
        title="t",
        duration=10.0,
    )
    decision = run(
        filter_.evaluate(candidate=candidate, material="辣椒", query="辣椒烘干", preview=preview)
    )
    assert decision.accepted is False
    assert decision.ai_failed is True


def test_failed_ai_source_retries_within_hours_not_days(settings) -> None:
    from storage.dedup import DeduplicationService

    library = build_library(settings)
    task_id = library.create_task(TaskRequest(material="辣椒", target_clip_count=1))
    library.upsert_source_video(
        task_id=task_id,
        platform="douyin",
        platform_video_id="7900000000000000002",
        source_url="https://www.douyin.com/video/7900000000000000002",
        status=SourceVideoStatus.FAILED_AI,
    )
    dedup = DeduplicationService(
        library,
        enabled=settings.dedup.enabled,
        phash_max_distance=settings.dedup.phash_max_distance,
        use_content_key_as_duplicate=settings.dedup.use_content_key_as_duplicate,
    )
    decision = dedup.acquisition_decision("douyin", "7900000000000000002")
    assert decision.skip is True
    assert "hour" in decision.detail or "recent" in decision.reason
    # and it is *not* treated as a rejected content item
    rejected = dedup.acquisition_decision("douyin", "7900000000000000002")
    assert "day" not in rejected.detail


# ---------------------------------------------------------------------------
# 7/8. coverage update + budget enforcement
# ---------------------------------------------------------------------------
def test_coverage_shrinks_after_a_new_clip(settings) -> None:
    library = build_library(settings)
    service = _service(settings, library)
    service.production.process_stage_targets = {"drying": 2}
    before = service.gaps(category="辣椒干")[0]
    _add_clip(library, settings.paths.library_root, index=9, category="辣椒干", stage=ProcessStage.DRYING)
    after = _service(settings, library).gaps(category="辣椒干")[0]
    assert before.gap == 2 and after.gap == 1
    assert after.current == 1


def test_production_plan_budgets_are_conservative_and_enforced(settings) -> None:
    library = build_library(settings)
    service = _service(settings, library)
    service.production.process_stage_targets = {"drying": 1}
    draft = service.build_plan(category="辣椒干", stage="drying", limit=1)
    item = draft.items[0]
    assert item.requested_clips == 1
    assert item.max_candidates <= 10
    assert item.max_downloads <= 4
    assert item.max_tokens <= 60000
    plan_service, plan = _production_plan(settings, library, service)
    plan_service.edit_item(plan.id, plan.items[0].id, requested_clips=5, max_downloads=2, max_candidates=4)
    plan = plan_service.sync_budgets(plan.id)
    assert plan_service.approve(plan.id).ok
    calls = {"n": 0}

    async def executor(request: TaskRequest) -> PipelineResult:
        calls["n"] += 1
        return _result([_clip(100 + calls["n"], stage=ProcessStage.PREPARATION)], task_id=calls["n"])

    outcome = run(PlanRunner(plan.id, library=library, settings=settings, executor=executor).run())
    stored = plan_service.get_plan(plan.id)
    assert outcome.status is PlanStatus.PAUSED
    assert outcome.pause_reason == PauseReason.DOWNLOAD_BUDGET_EXHAUSTED.value
    assert stored.progress.downloads <= 2
    assert stored.items[0].progress.clips_saved == 2


# ---------------------------------------------------------------------------
# 9. metrics + zero-yield visibility
# ---------------------------------------------------------------------------
def test_metrics_keep_buckets_separate_and_show_zero_yield(settings) -> None:
    library = build_library(settings)
    task_id = library.create_task(TaskRequest(material="辣椒", target_clip_count=1))
    library.add_search_yield(
        task_id=task_id,
        platform="douyin",
        query="辣椒烘干",
        candidate_count=12,
        unique_candidate_count=10,
        preview_accept_count=2,
        download_count=1,
        final_clip_count=1,
    )
    library.add_search_yield(
        task_id=task_id,
        platform="douyin",
        query="辣椒烘干房",
        candidate_count=9,
        unique_candidate_count=1,
        preview_accept_count=0,
        download_count=0,
        final_clip_count=0,
    )
    metrics = _service(settings, library).metrics()
    assert set(metrics) >= {"discovery", "preview", "download", "clips", "cost"}
    assert metrics["discovery"]["candidates"] == 21
    assert metrics["discovery"]["unique_candidates"] == 11
    assert "辣椒烘干房" in metrics["zero_yield_queries"]
    report = "\n".join(_service(settings, library).report_lines(category="辣椒干"))
    assert "零产出查询" in report
    assert "提供商错误" in report


# ---------------------------------------------------------------------------
# 10. determinism + linkage + CLI
# ---------------------------------------------------------------------------
def test_production_plan_is_deterministic(settings) -> None:
    library = build_library(settings)
    first = _service(settings, library).build_plan(category="辣椒干", stage="drying", limit=1)
    second = _service(settings, library).build_plan(category="辣椒干", stage="drying", limit=1)
    assert [item.process_stage for item in first.items] == [
        item.process_stage for item in second.items
    ]
    assert [query.query for query in first.items[0].queries] == [
        query.query for query in second.items[0].queries
    ]


def test_production_plan_links_plan_task_source_clip(settings) -> None:
    library = build_library(settings)
    service = _service(settings, library)
    service.production.process_stage_targets = {"drying": 1}
    plan_service, plan = _production_plan(settings, library, service)
    plan_service.edit_item(plan.id, plan.items[0].id, requested_clips=1, max_downloads=3, max_candidates=10)
    plan = plan_service.sync_budgets(plan.id)
    assert plan_service.approve(plan.id).ok
    task_id = library.create_task(TaskRequest(material="辣椒", target_clip_count=1))
    source_id = library.upsert_source_video(
        task_id=task_id,
        platform="douyin",
        platform_video_id="7900000000000000003",
        source_url="https://www.douyin.com/video/7900000000000000003",
        status=SourceVideoStatus.PROCESSED,
    )
    clip_id = _add_clip(
        library,
        settings.paths.library_root,
        index=11,
        category="辣椒干",
        stage=ProcessStage.DRYING,
        provenance=DOUYIN_REAL,
    )
    library.database.execute(
        "UPDATE clips SET task_id = ?, source_video_id = ? WHERE id = ?",
        (task_id, source_id, clip_id),
    )
    plan_service.repo.link_task(
        plan_id=plan.id, plan_item_id=plan.items[0].id, task_id=task_id, query="辣椒烘干"
    )

    from core.acceptance import plan_linkage

    linkage = plan_linkage(library, plan.id)
    assert linkage.item_ids == [plan.items[0].id]
    assert linkage.task_ids == [task_id]
    assert linkage.source_video_ids == [source_id]
    assert linkage.clip_ids == [clip_id]


def test_production_cli_reports_gaps_and_creates_a_draft(settings, capsys) -> None:
    import app as app_module

    library = build_library(settings)
    assert app_module.run_production_gaps(settings, category="辣椒干") == 0
    output = capsys.readouterr().out
    assert "生产覆盖缺口" in output
    assert "drying" in output
    assert "历史指标" in output

    assert app_module.run_create_production_plan(settings, category="辣椒干", stage="drying") == 0
    output = capsys.readouterr().out
    assert "草稿计划" in output
    plans = PlanService(library, settings).list_plans()
    production = [plan for plan in plans if plan.created_from == "production_coverage"]
    assert production, "the production plan must be persisted as a draft"
    plan = production[0]
    assert plan.status is PlanStatus.DRAFT
    assert all(item.queries for item in plan.items)
    assert all(item.requested_clips == 1 for item in plan.items)
