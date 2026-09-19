"""Milestone 9.5 regression tests: novelty-aware production acquisition.

No real Douyin/Qwen call is made.  The tests use the synthetic library and
existing fake executors to prove novelty definitions, saturation, primary /
reserve scheduling and actionability.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.config import load_settings
from core.dependencies import build_library
from core.models import SourceVideoStatus, SubtitleType, TaskRequest
from core.novelty import (
    SATURATION_FRESH,
    SATURATION_SATURATED,
    SATURATION_UNKNOWN,
    NoveltyAnalyzer,
)
from core.plan_runner import PlanRunner
from core.plans import CollectionPlanItem, PlanItemStatus, PlanQuery, QueryOrigin
from core.production import ProductionCoverageService
from tests.test_m9_1_production_hardening import _clip, _plan, _result, run
from tests.test_m9_2_subtitle_cleanup import _add_clip, _service


class _RepoStub:
    def log_event(self, *args, **kwargs) -> None:
        return None


def _runner(settings, library) -> PlanRunner:
    return PlanRunner(
        999,
        library=library,
        settings=settings,
        repository=_RepoStub(),
    )


def _source(
    library,
    *,
    video_id: str,
    query: str,
    status: SourceVideoStatus = SourceVideoStatus.PROCESSED,
):
    return library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id=video_id,
        source_url=f"https://www.douyin.com/video/{video_id}",
        status=status,
        matched_queries=[query],
    )


def _yield(
    library,
    *,
    query: str,
    candidates: int,
    unique: int,
    new_to_system: int = 0,
    known_source: int = 0,
    already_processed: int = 0,
    already_represented: int = 0,
    run_duplicate: int = 0,
    clips: int = 0,
    created_at: str | None = None,
) -> int:
    row_id = library.add_search_yield(
        task_id=None,
        platform="douyin",
        query=query,
        candidate_count=candidates,
        unique_candidate_count=unique,
        new_to_system_count=new_to_system,
        known_source_count=known_source,
        current_run_duplicate_count=run_duplicate,
        already_processed_count=already_processed,
        already_represented_count=already_represented,
        final_clip_count=clips,
    )
    if created_at:
        library.database.execute(
            "UPDATE search_yields SET created_at = ? WHERE id = ?",
            (created_at, row_id),
        )
    return row_id


# ---------------------------------------------------------------------------
# Candidate novelty definitions
# ---------------------------------------------------------------------------
def test_current_run_unique_is_not_new_to_system(settings) -> None:
    library = build_library(settings)
    source_id = _source(library, video_id="1001", query="辣椒烘干过程")
    library.database.execute(
        "UPDATE source_videos SET status = ? WHERE id = ?",
        (str(SourceVideoStatus.PROCESSED), source_id),
    )
    _yield(
        library,
        query="辣椒烘干过程",
        candidates=2,
        unique=2,
        new_to_system=0,
        known_source=2,
        already_processed=2,
    )
    stats = NoveltyAnalyzer(library, settings).stats("辣椒烘干过程")
    assert stats.historical_unique == 2
    assert stats.historical_new_to_system == 0
    assert stats.known_source_count == 1
    assert stats.known_source_rate == 1.0
    assert stats.library_novelty_rate == 0.0


def test_known_source_without_clip_counts_known(settings) -> None:
    library = build_library(settings)
    _source(library, video_id="1002", query="辣椒烘干过程")
    _yield(
        library,
        query="辣椒烘干过程",
        candidates=1,
        unique=1,
        known_source=1,
    )
    stats = NoveltyAnalyzer(library, settings).stats("辣椒烘干过程")
    assert stats.known_source_count == 1
    assert stats.already_represented == 0


def test_known_failed_ai_source_counts_as_known_not_novel(settings) -> None:
    library = build_library(settings)
    _source(
        library,
        video_id="1003",
        query="辣椒烘干过程",
        status=SourceVideoStatus.FAILED_AI,
    )
    _yield(
        library,
        query="辣椒烘干过程",
        candidates=1,
        unique=1,
        known_source=1,
        already_processed=1,
    )
    stats = NoveltyAnalyzer(library, settings).stats("辣椒烘干过程")
    assert stats.known_source_count == 1
    assert stats.historical_new_to_system == 0
    assert stats.saturation == SATURATION_SATURATED or stats.known_source_rate == 1.0


def test_already_represented_source_is_separate(settings) -> None:
    library = build_library(settings)
    clip_id = _add_clip(library, settings.paths.library_root, index=1)
    clip = library.get_clip(clip_id)
    library.database.execute(
        "UPDATE clips SET source_video_id = ? WHERE id = ?", (None, clip_id)
    )
    source_id = _source(library, video_id="1004", query="辣椒烘干过程")
    library.database.execute(
        "UPDATE clips SET source_video_id = ? WHERE id = ?", (source_id, clip_id)
    )
    _yield(
        library,
        query="辣椒烘干过程",
        candidates=1,
        unique=1,
        known_source=1,
        already_represented=1,
    )
    stats = NoveltyAnalyzer(library, settings).stats("辣椒烘干过程")
    assert stats.already_represented == 1
    assert stats.known_source_count == 1


# ---------------------------------------------------------------------------
# Saturation model
# ---------------------------------------------------------------------------
def test_saturated_query_classification(settings) -> None:
    library = build_library(settings)
    for index in range(4):
        _source(library, video_id=f"20{index}", query="辣椒干 烘干过程")
    _yield(
        library,
        query="辣椒干 烘干过程",
        candidates=8,
        unique=8,
        new_to_system=0,
        known_source=8,
        already_processed=8,
    )
    stats = NoveltyAnalyzer(library, settings).stats("辣椒干 烘干过程")
    assert stats.saturation == SATURATION_SATURATED
    assert stats.known_source_rate == 1.0
    assert stats.library_novelty_rate == 0.0


def test_fresh_query_classification(settings) -> None:
    library = build_library(settings)
    _yield(
        library,
        query="红薯热泵烘干",
        candidates=4,
        unique=4,
        new_to_system=3,
        known_source=1,
    )
    stats = NoveltyAnalyzer(library, settings).stats("红薯热泵烘干")
    assert stats.saturation == SATURATION_FRESH
    assert stats.library_novelty_rate == 0.75


def test_saturation_expires(settings) -> None:
    library = build_library(settings)
    old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    _yield(
        library,
        query="辣椒干 烘干过程",
        candidates=8,
        unique=8,
        known_source=8,
        created_at=old,
    )
    stats = NoveltyAnalyzer(library, settings).stats("辣椒干 烘干过程")
    assert stats.expired
    assert stats.saturation == SATURATION_UNKNOWN


# ---------------------------------------------------------------------------
# Query families / diversity / actionability
# ---------------------------------------------------------------------------
def test_query_family_assignment(settings) -> None:
    analyzer = NoveltyAnalyzer(build_library(settings), settings)
    assert analyzer.query_family("辣椒热泵烘干") == "heat_pump"
    assert analyzer.query_family("辣椒烘干设备") == "dryer_equipment"
    assert analyzer.query_family("辣椒烘干房内部") == "inside_dryer"
    assert analyzer.query_family("辣椒烘干车间") == "factory_line"
    assert analyzer.query_family("辣椒烘干成品") == "finished_product"
    assert analyzer.query_family("辣椒烘干过程") == "material_process"


def test_lexical_duplicates_do_not_monopolize_primary_queries(settings) -> None:
    library = build_library(settings)
    service = ProductionCoverageService(library, settings)
    primary = service.queries_for("辣椒干", "drying", limit=4)
    families = {query.family for query in primary}
    assert len(families) >= 3
    reserves = service.reserve_queries_for(
        "辣椒干", "drying", existing=primary, limit=4
    )
    assert reserves
    assert all(query.role == "reserve" for query in reserves)
    assert len({query.family for query in primary + reserves}) >= 4


def test_production_gap_actionability_penalises_saturated_primary(settings) -> None:
    library = build_library(settings)
    # Make the 辣椒 drying space saturated.
    for index in range(4):
        _source(library, video_id=f"30{index}", query="辣椒烘干过程")
    _yield(
        library,
        query="辣椒烘干过程",
        candidates=8,
        unique=8,
        known_source=8,
        already_processed=8,
    )
    service = ProductionCoverageService(library, settings)
    gaps = {gap.category: gap for gap in service.gaps(stage="drying")}
    chili = gaps.get("辣椒干")
    fresh = gaps.get("红薯干") or gaps.get("香蕉干")
    assert chili is not None and fresh is not None
    assert chili.saturated_primary_count >= 1
    assert chili.query_actionability < fresh.query_actionability
    assert chili.effective_priority < fresh.effective_priority


def test_gap_order_is_deterministic(settings) -> None:
    library = build_library(settings)
    service = ProductionCoverageService(library, settings)
    first = [(gap.category, gap.stage) for gap in service.gaps()]
    second = [(gap.category, gap.stage) for gap in service.gaps()]
    assert first == second


# ---------------------------------------------------------------------------
# Primary / reserve scheduling
# ---------------------------------------------------------------------------
def _item(queries: list[PlanQuery], *, candidates: int = 8) -> CollectionPlanItem:
    return CollectionPlanItem(
        process_stage="drying",
        requested_clips=1,
        max_candidates=candidates,
        max_downloads=4,
        max_tokens=60000,
        queries=queries,
    )


def test_saturated_primary_is_skipped_and_reserve_activated(settings) -> None:
    library = build_library(settings)
    runner = _runner(settings, library)
    saturated = PlanQuery(
        query="辣椒干 烘干过程",
        query_saturation=SATURATION_SATURATED,
        role="primary",
        planned_order=1,
    )
    reserve = PlanQuery(
        query="辣椒烘干设备",
        query_saturation=SATURATION_UNKNOWN,
        role="reserve",
        planned_order=2,
    )
    item = _item([saturated, reserve])
    selected = runner._select_query(item)
    assert selected is reserve
    assert selected.reserve_activation_reason == "primary_saturated"
    assert any(entry.get("skipped") for entry in item.progress.query_audit)


def test_primary_zero_novelty_activates_reserve(settings) -> None:
    library = build_library(settings)
    runner = _runner(settings, library)
    primary = PlanQuery(
        query="辣椒干 烘干过程",
        query_saturation=SATURATION_UNKNOWN,
        role="primary",
    )
    reserve = PlanQuery(query="辣椒烘干设备", role="reserve")
    item = _item([primary, reserve])
    item.progress.executed_queries.append(primary.query)
    item.progress.query_audit.append(
        {"query": primary.query, "candidates": 2, "new_to_system": 0}
    )
    selected = runner._select_query(item)
    assert selected is reserve
    assert selected.reserve_activation_reason == "primary_zero_novelty"


def test_saturated_primary_receives_no_budget(settings) -> None:
    library = build_library(settings)
    runner = _runner(settings, library)
    saturated = PlanQuery(query="辣椒干 烘干过程", query_saturation=SATURATION_SATURATED)
    fresh = PlanQuery(query="辣椒烘干设备", query_saturation=SATURATION_FRESH)
    item = _item([saturated, fresh], candidates=10)
    assert runner._candidate_share(item, saturated) == 0
    assert runner._candidate_share(item, fresh) >= 1


def test_candidate_hard_budget_is_not_exceeded(settings) -> None:
    library = build_library(settings)
    runner = _runner(settings, library)
    query = PlanQuery(query="辣椒烘干设备", query_saturation=SATURATION_FRESH)
    item = _item([query], candidates=3)
    item.progress.unique_candidates = 2
    assert runner._candidate_share(item, query) <= 1


def test_target_reached_still_stops_immediately(settings) -> None:
    library = build_library(settings)
    queries = ["辣椒烘干设备", "辣椒热泵烘干"]
    service, plan = _plan(settings, library, queries=queries, candidates=8, requested=1)
    assert service.approve(plan.id).ok
    seen: list[str] = []

    async def executor(request) -> object:
        seen.append(request.query_seed or "")
        return _result([_clip(1, stage="drying")], task_id=1)

    outcome = run(PlanRunner(plan.id, library=library, settings=settings, executor=executor).run())
    assert seen == [queries[0]]
    assert outcome.qualifying_clips == 1


def test_query_space_exhausted_with_saturated_queries_only(settings) -> None:
    library = build_library(settings)
    runner = _runner(settings, library)
    item = _item(
        [
            PlanQuery(query="辣椒干 烘干过程", query_saturation=SATURATION_SATURATED),
            PlanQuery(query="辣椒烘干过程", query_saturation=SATURATION_SATURATED),
        ]
    )
    assert runner._select_query(item) is None
    assert all(entry.get("skipped") for entry in item.progress.query_audit)


# ---------------------------------------------------------------------------
# formula / dedup / cleanup non-interference
# ---------------------------------------------------------------------------
def test_raw_query_rank_v2_is_exposed_without_formula_change(settings) -> None:
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
    service = ProductionCoverageService(library, settings)
    stats = service.query_statistics()["辣椒烘干"]
    raw = service.planner.score(stats, version="query_rank_v2").score
    queries = service.queries_for("辣椒干", "drying")
    target = next(query for query in queries if query.query == "辣椒烘干")
    assert target.raw_score == pytest.approx(raw, abs=0.001)
    assert target.rank_version == "query_rank_v2"


def test_cleanup_derivative_does_not_change_novelty(settings) -> None:
    library = build_library(settings)
    _source(library, video_id="4001", query="辣椒烘干过程")
    _yield(
        library,
        query="辣椒烘干过程",
        candidates=4,
        unique=4,
        known_source=4,
    )
    analyzer = NoveltyAnalyzer(library, settings)
    before = analyzer.stats("辣椒烘干过程").as_dict()
    service, _detector, _toolkit, _engine = _service(settings)
    clip_id = _add_clip(service.library, settings.paths.library_root, index=1)
    run(service.cleanup_clip(clip_id))
    after = NoveltyAnalyzer(service.library, settings).stats("辣椒烘干过程").as_dict()
    assert before == after
