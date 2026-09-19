"""Milestone 8 regressions: query_rank_v2 calibration + plan observability.

Every test runs offline: the plan runner is driven by an injected executor and
the media probe is injected too, so nothing here touches Douyin or Qwen.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.acceptance import acceptance_report, plan_linkage, validate_clip
from core.dependencies import build_library
from core.models import (
    ClipArtifact,
    ClipRecord,
    ClipScores,
    ClipTagging,
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
from core.planner import (
    RANK_VERSION_V1,
    RANK_VERSION_V2,
    Planner,
    QueryStatistics,
)
from core.plans import PauseReason, PlanStatus, pause_reason_label
from core.provenance import DOUYIN_REAL
from storage.plans import PlanRepository


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _stats(
    query: str,
    *,
    clips: int = 0,
    candidates: int = 0,
    unique: int = 0,
    accepted: int = 0,
    downloads: int = 0,
    approved: int = 0,
    subtitle: float | None = None,
    tokens_per_clip: float | None = None,
    tokens_total: int = 0,
) -> QueryStatistics:
    return QueryStatistics(
        query=query,
        runs=1,
        candidates=candidates,
        unique_candidates=unique,
        preview_accepted=accepted,
        downloads=downloads,
        clips=clips,
        approved=approved,
        subtitle_rejection_rate=subtitle,
        tokens_per_clip=tokens_per_clip,
        tokens_total=tokens_total,
    )


def _clip(
    clip_id: int,
    *,
    stage: ProcessStage = ProcessStage.DRYING,
    category: str = "苹果干",
) -> ClipRecord:
    return ClipRecord(
        id=clip_id,
        material="苹果",
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
    calls: int = 2,
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


def _seed_library(settings, *, category: str = "苹果干"):
    """Small library: one gap stage plus deterministic query history."""

    library = build_library(settings)
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
        unique_candidate_count=16,
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
    return library


@pytest.fixture
def seeded(settings):
    library = _seed_library(settings)
    service = PlanService(library, settings, repository=PlanRepository(library.database))
    return library, service, settings


# ---------------------------------------------------------------------------
# 12/13. bounded historical volume
# ---------------------------------------------------------------------------
def test_rank_v2_bounds_historical_volume(settings) -> None:
    planner = Planner(build_library(settings), settings)
    small = planner.score_v2(
        _stats("a", clips=4, candidates=20, unique=18, accepted=4, downloads=4)
    )
    large = planner.score_v2(
        _stats("b", clips=40, candidates=200, unique=180, accepted=40, downloads=40)
    )
    v1_small = planner.score_v1(
        _stats("a", clips=4, candidates=20, unique=18, accepted=4, downloads=4)
    )
    v1_large = planner.score_v1(
        _stats("b", clips=40, candidates=200, unique=180, accepted=40, downloads=40)
    )
    # v2 grows logarithmically: 10x the clips is far less than 10x the score
    assert large.score < 10 * small.score
    # v1 is linear in the clip count, which is exactly the pathology we fix
    assert v1_large.score - v1_small.score >= 100
    assert large.score / small.score < (v1_large.score / v1_small.score)


def test_rank_v2_does_not_reward_repeated_runs(settings) -> None:
    planner = Planner(build_library(settings), settings)
    once = planner.score_v2(_stats("q", clips=2, candidates=8, unique=7, accepted=2, downloads=2))
    many = planner.score_v2(
        _stats("q", clips=2, candidates=40, unique=7, accepted=2, downloads=2)
    )
    assert many.score <= once.score


# ---------------------------------------------------------------------------
# 14. duplicate penalty
# ---------------------------------------------------------------------------
def test_rank_v2_strong_duplicate_penalty(settings) -> None:
    planner = Planner(build_library(settings), settings)
    duplicated = planner.score_v2(
        _stats("dup", clips=5, candidates=27, unique=3, accepted=2, downloads=2)
    )
    clean = planner.score_v2(
        _stats("clean", clips=3, candidates=21, unique=17, accepted=2, downloads=2)
    )
    assert duplicated.score < clean.score
    assert duplicated.components["unique_damping"] == pytest.approx(3 / 27, abs=0.01)
    # the same row scores much higher under the old linear semantics
    assert planner.score_v1(
        _stats("dup", clips=5, candidates=27, unique=3, accepted=2, downloads=2)
    ).score > clean.score


def test_rank_v2_zero_unique_candidates_is_zeroed(settings) -> None:
    planner = Planner(build_library(settings), settings)
    scored = planner.score_v2(_stats("q", clips=1, candidates=10, unique=0, accepted=0))
    assert scored.score <= 0
    assert scored.components["unique_damping"] == 0.0


# ---------------------------------------------------------------------------
# 15. sample-size confidence
# ---------------------------------------------------------------------------
def test_rank_v2_sample_size_confidence(settings) -> None:
    planner = Planner(build_library(settings), settings)
    tiny = planner.score_v2(_stats("tiny", clips=1, candidates=1, unique=1, accepted=1, downloads=1))
    proven = planner.score_v2(
        _stats("proven", clips=6, candidates=40, unique=38, accepted=6, downloads=6)
    )
    assert tiny.confidence < proven.confidence
    assert tiny.score < proven.score
    assert tiny.components["confidence_samples"] == 1.0
    # a 100% conversion on one candidate must not dominate a proven query
    assert tiny.score < 1.0


# ---------------------------------------------------------------------------
# 18. token efficiency
# ---------------------------------------------------------------------------
def test_rank_v2_zero_clip_token_penalty(settings) -> None:
    planner = Planner(build_library(settings), settings)
    wasteful = planner.score_v2(
        _stats("waste", clips=0, candidates=37, unique=25, accepted=2, tokens_total=44071)
    )
    unknown = planner.score_v2(_stats("unknown", clips=0, candidates=0, unique=0))
    assert wasteful.score < 0
    assert wasteful.components["zero_clip_token_penalty"] < 0
    assert wasteful.score < unknown.score
    # a query with no attributable tokens keeps only the zero-yield penalty
    idle = planner.score_v2(_stats("idle", clips=0, candidates=22, unique=4))
    assert idle.components["zero_clip_token_penalty"] == 0.0
    assert idle.components["zero_yield_penalty"] < 0


def test_rank_v2_token_penalty_is_bounded(settings) -> None:
    planner = Planner(build_library(settings), settings)
    cheap = planner.score_v2(
        _stats("cheap", clips=2, candidates=10, unique=9, tokens_per_clip=5000)
    )
    expensive = planner.score_v2(
        _stats("expensive", clips=2, candidates=10, unique=9, tokens_per_clip=600000)
    )
    assert expensive.score < cheap.score
    # the penalty saturates at the configured weight instead of exploding
    assert expensive.components["token_penalty"] == pytest.approx(
        -settings.collection_planning.rank2_penalty_tokens, abs=0.001
    )


# ---------------------------------------------------------------------------
# 17. subtitle rejection penalty
# ---------------------------------------------------------------------------
def test_rank_v2_subtitle_rejection_penalty(settings) -> None:
    planner = Planner(build_library(settings), settings)
    noisy = planner.score_v2(
        _stats("noisy", clips=2, candidates=10, unique=10, accepted=1, subtitle=0.8)
    )
    clean = planner.score_v2(
        _stats("clean", clips=2, candidates=10, unique=10, accepted=1, subtitle=0.0)
    )
    assert noisy.score < clean.score
    assert noisy.components["subtitle_penalty"] == pytest.approx(-2.0, abs=0.001)


# ---------------------------------------------------------------------------
# 16. approval signal
# ---------------------------------------------------------------------------
def test_rank_v2_approval_is_a_bonus_not_a_requirement(settings) -> None:
    planner = Planner(build_library(settings), settings)
    unreviewed = planner.score_v2(_stats("a", clips=3, candidates=12, unique=11, accepted=3))
    approved = planner.score_v2(
        _stats("a", clips=3, candidates=12, unique=11, accepted=3, approved=3)
    )
    assert approved.score > unreviewed.score
    # the bonus stays modest: it must not invert a big quality difference
    weaker_but_approved = planner.score_v2(
        _stats("b", clips=1, candidates=40, unique=5, accepted=0, approved=1)
    )
    assert weaker_but_approved.score < unreviewed.score


# ---------------------------------------------------------------------------
# 19/20. explanation + versioning
# ---------------------------------------------------------------------------
def test_rank_v2_explains_every_component(settings) -> None:
    planner = Planner(build_library(settings), settings)
    scored = planner.score_v2(
        _stats("q", clips=2, candidates=10, unique=2, accepted=2, tokens_per_clip=29159)
    )
    expected = {
        "yield",
        "conversion",
        "useful_yield",
        "unique_damping",
        "approval",
        "subtitle_penalty",
        "token_penalty",
        "zero_clip_token_penalty",
        "zero_yield_penalty",
        "subtotal",
        "confidence_factor",
        "final",
    }
    assert expected.issubset(scored.components)
    # the components add up to the subtotal, and the confidence scales it
    total = (
        scored.components["yield"] * scored.components["unique_damping"]
        + scored.components["conversion"] * scored.components["unique_damping"]
        + scored.components["approval"]
        + scored.components["subtitle_penalty"]
        + scored.components["token_penalty"]
        + scored.components["zero_clip_token_penalty"]
        + scored.components["zero_yield_penalty"]
    )
    assert total == pytest.approx(scored.components["subtotal"], abs=0.01)
    assert scored.score == pytest.approx(
        scored.components["subtotal"] * scored.components["confidence_factor"], abs=0.01
    )
    lines = scored.explanation_lines()
    assert any("yield component" in line for line in lines)
    assert any("duplicate damping" in line for line in lines)
    assert any("final score" in line for line in lines)
    assert scored.version == RANK_VERSION_V2


def test_v1_semantics_are_frozen(settings) -> None:
    planner = Planner(build_library(settings), settings)
    stats = _stats("q", clips=5, candidates=27, unique=3, accepted=2, subtitle=0.5)
    conversion = 5 / 27
    duplicate = 1 - 3 / 27
    expected = 3.0 * 5 + 1.0 * 2 + 2.0 * conversion - 2.5 * 0.5 - 1.5 * duplicate
    assert planner.score_v1(stats).score == pytest.approx(round(expected, 3), abs=0.001)
    assert planner.score_query(stats)[0] == planner.score_v1(stats).score
    assert planner.score(stats, version=RANK_VERSION_V1).version == RANK_VERSION_V1


def test_ranking_table_is_deterministic_and_reports_both_versions(settings) -> None:
    planner = Planner(build_library(settings), settings)
    history = {
        "dup": _stats("dup", clips=5, candidates=27, unique=3, accepted=2),
        "clean": _stats("clean", clips=3, candidates=21, unique=17, accepted=2),
        "waste": _stats("waste", clips=0, candidates=37, unique=25, tokens_total=44071),
    }
    first = planner.ranking_table(statistics=history, limit=10)
    second = planner.ranking_table(statistics=history, limit=10)
    assert first == second
    by_query = {row["query"]: row for row in first}
    assert by_query["clean"]["rank_v2"] < by_query["dup"]["rank_v2"]
    assert set(by_query["clean"]["components_v2"]) >= {"yield", "confidence_factor"}
    assert by_query["waste"]["score_v2"] < 0
    assert by_query["dup"]["rank_v1"] == 1, "v1 still ranks the volume query first"


def test_explain_query_returns_component_lines(settings) -> None:
    planner = Planner(build_library(settings), settings)
    history = {"苹果干烘干": _stats("苹果干烘干", clips=3, candidates=21, unique=17, subtitle=0.57)}
    lines = planner.explain_query("苹果干烘干", statistics=history)
    assert lines[0].startswith("苹果干烘干")
    assert any("confidence adjustment" in line for line in lines)
    assert any("依据" in line for line in lines)


# ---------------------------------------------------------------------------
# 23. plan query selection v2
# ---------------------------------------------------------------------------
def test_plan_queries_record_their_ranking_version(seeded) -> None:
    _library, service, settings = seeded
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    queries = [query for item in plan.items for query in item.queries]
    assert queries
    assert all(query.rank_version == RANK_VERSION_V2 for query in queries)
    assert service.plan_ranking_version(plan) == RANK_VERSION_V2
    historical = [query for query in queries if query.origin.value == "historical"]
    if historical:
        assert historical[0].score > 0
        assert historical[0].components


def test_ranking_version_is_configurable(seeded) -> None:
    _library, _service, settings = seeded
    settings.collection_planning.ranking_version = RANK_VERSION_V1
    planner = Planner(_library, settings)
    queries = planner.rank_queries("苹果干", "drying")
    assert queries
    assert all(query.rank_version == RANK_VERSION_V1 for query in queries)


def test_plan_queries_stay_inside_the_material(settings) -> None:
    """A 苹果干 plan must never search another material's keyword."""

    library = build_library(settings)
    task_id = library.create_task(TaskRequest(material="苹果干", target_clip_count=1))
    library.add_search_yield(
        task_id=task_id,
        platform="douyin",
        query="香蕉干",
        candidate_count=1,
        unique_candidate_count=1,
        preview_accept_count=1,
        download_count=1,
        final_clip_count=1,
    )
    planner = Planner(library, settings)
    queries = [entry.query for entry in planner.rank_queries("苹果干", "drying")]
    assert queries, "templates still fill the list"
    assert "香蕉干" not in queries
    assert all("苹果" in query for query in queries)


def test_approved_plan_queries_do_not_mutate(seeded) -> None:
    _library, service, _settings = seeded
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    item = plan.items[0]
    before = [query.query for query in item.queries]
    assert service.approve(plan.id, note="校准验收").ok

    action = service.regenerate_queries(plan.id, item.id)
    assert action.ok is False
    stored = service.get_plan(plan.id)
    assert [query.query for query in stored.items[0].queries] == before


def test_draft_plan_queries_can_be_regenerated(seeded) -> None:
    _library, service, _settings = seeded
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    item = plan.items[0]
    action = service.regenerate_queries(plan.id, item.id)
    assert action.ok, action.message
    stored = service.get_plan(plan.id)
    assert stored.items[0].queries
    assert all(query.rank_version == RANK_VERSION_V2 for query in stored.items[0].queries)
    events = [entry["event"] for entry in service.repo.events(plan.id)]
    assert "queries_regenerated" in events


# ---------------------------------------------------------------------------
# 24/25/26/27. observability
# ---------------------------------------------------------------------------
def test_budget_progress_text(seeded) -> None:
    _library, service, _settings = seeded
    text = service.budget_progress_text(3, 8)
    assert "3 / 8" in text
    assert "[" in text and "]" in text
    assert "38%" in text
    assert service.budget_progress_text(0, 0).startswith("0 / -")


def test_pause_reason_labels_are_human_readable() -> None:
    assert pause_reason_label(PauseReason.HUMAN_VERIFICATION_REQUIRED) == "需要人工完成抖音验证"
    assert pause_reason_label(PauseReason.BACKEND_UNAVAILABLE) == "dtk 后端不可用"
    assert pause_reason_label(PauseReason.TOKEN_BUDGET_EXHAUSTED) == "AI token 预算已用尽"
    assert pause_reason_label(PauseReason.DOWNLOAD_BUDGET_EXHAUSTED) == "下载预算已用尽"
    assert pause_reason_label(PauseReason.PREVIEW_BUDGET_EXHAUSTED) == "候选/预览预算已用尽"
    assert pause_reason_label(PauseReason.OPERATOR) == "操作者手动暂停"
    assert pause_reason_label(PauseReason.INTERRUPTED) == "进程中断后暂停（需人工恢复）"
    assert pause_reason_label("something_new") == "something_new"


def _paused_plan(seeded):
    library, service, settings = seeded
    settings.collection_planning.max_items_per_plan = 1
    plan, _action = service.create_plan("苹果干")
    item = plan.items[0]
    service.edit_item(
        plan.id,
        item.id,
        requested_clips=1,
        max_candidates=8,
        max_downloads=3,
        max_tokens=20000,
        queries=["预警-查询1", "预警-查询2"],
    )
    assert service.approve(plan.id).ok

    async def execute(request: TaskRequest) -> PipelineResult:
        return _result(
            [],
            task_id=1,
            status=TaskStatus.PARTIAL,
            candidates=0,
            unique=0,
            previews=0,
            downloads=0,
            tokens=0,
            discovery_blocked=True,
            discovery_states={"browser": "verification_required"},
        )

    outcome = run(PlanRunner(plan.id, library=library, settings=settings, executor=execute).run())
    return library, service, plan, outcome


def test_status_lines_show_current_query_and_budgets(seeded) -> None:
    _library, service, plan, outcome = _paused_plan(seeded)
    assert outcome.pause_reason == PauseReason.HUMAN_VERIFICATION_REQUIRED.value
    stored = service.get_plan(plan.id)
    lines = "\n".join(service.status_lines(stored))
    assert "需要人工完成抖音验证 (human_verification_required)" in lines
    # M9.7.1: the login wall did not complete query 1, so it remains the next
    # retryable query instead of silently consuming the slot.
    assert "下一条查询" in lines and "预警-查询1" in lines
    assert "previews :" in lines and "downloads:" in lines and "tokens   :" in lines
    assert "第 1/2 条" in lines


def test_timeline_formats_the_real_events(seeded) -> None:
    _library, service, plan, _outcome = _paused_plan(seeded)
    stored = service.get_plan(plan.id)
    timeline = service.timeline_lines(stored)
    text = "\n".join(timeline)
    assert "计划创建" in text
    assert "人工批准" in text
    assert "开始执行" in text
    assert "查询 1/2「预警-查询1」" in text
    assert "暂停：人工完成抖音验证后恢复 [human_verification_required]" in text
    assert timeline[0].startswith("[") and "]" in timeline[0]


def test_timeline_shows_resume_without_redoing_queries(seeded) -> None:
    library, service, plan, _outcome = _paused_plan(seeded)
    assert service.resume(plan.id).ok
    calls = {"n": 0}

    async def execute(request: TaskRequest) -> PipelineResult:
        calls["n"] += 1
        return _result([_clip(100 + calls["n"], stage=ProcessStage.DRYING)], task_id=50 + calls["n"])

    outcome = run(PlanRunner(plan.id, library=library, settings=seeded[2], executor=execute).run())
    assert calls["n"] == 1, "resume must continue, not restart the item"
    assert outcome.status is PlanStatus.COMPLETED
    stored = service.get_plan(plan.id)
    assert stored.progress.queries_attempted == 2
    timeline = "\n".join(service.timeline_lines(stored))
    assert "恢复执行" in timeline
    # the login-blocked query is retried, not skipped
    assert "查询 1/2「预警-查询1」" in timeline
    assert "「预警-查询1」（重试）" in timeline
    assert "目标 drying 已达成" in timeline
    assert stored.items[0].progress.qualifying_clips == 1


def test_repeated_human_verification_pause_keeps_budget(seeded) -> None:
    library, service, plan, first = _paused_plan(seeded)
    assert first.pause_reason == PauseReason.HUMAN_VERIFICATION_REQUIRED.value
    assert service.resume(plan.id).ok

    async def execute(request: TaskRequest) -> PipelineResult:
        return _result(
            [_clip(7, stage=ProcessStage.PREPARATION)],
            task_id=9,
            tokens=2500,
            calls=1,
            candidates=5,
            unique=4,
            previews=2,
            downloads=1,
            discovery_blocked=True,
            discovery_states={"browser": "verification_required"},
        )

    second = run(PlanRunner(plan.id, library=library, settings=seeded[2], executor=execute).run())
    assert second.status is PlanStatus.PAUSED
    assert second.pause_reason == PauseReason.HUMAN_VERIFICATION_REQUIRED.value
    stored = service.get_plan(plan.id)
    progress = stored.progress
    assert progress.queries_attempted == 2
    assert progress.ai_tokens == 2500
    assert progress.clips_saved == 1
    assert progress.qualifying_clips == 0, "off-target clips never count as qualifying"
    assert stored.pause_reason == PauseReason.HUMAN_VERIFICATION_REQUIRED.value
    events = [entry["event"] for entry in service.repo.events(plan.id)]
    assert events.count("paused") == 2
    assert events.count("resumed") == 1


def test_archive_hides_plan_and_blocks_execution(seeded) -> None:
    library, service, _settings = seeded
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    assert service.archive(plan.id).ok
    stored = service.get_plan(plan.id)
    assert stored.archived is True
    assert stored.runnable is False
    assert plan.id not in [entry["plan_id"] for entry in service.history_rows()]
    assert plan.id in [
        entry["plan_id"] for entry in service.history_rows(include_archived=True)
    ]
    outcome = run(PlanRunner(plan.id, library=library, settings=_settings).run())
    assert outcome.refused and "归档" in outcome.refused
    assert service.archive(plan.id, archived=False).ok
    assert service.get_plan(plan.id).archived is False


def test_test_plan_marker(seeded) -> None:
    _library, service, _settings = seeded
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    assert service.mark_test_plan(plan.id).ok
    assert service.get_plan(plan.id).test_plan is True
    events = [entry["event"] for entry in service.repo.events(plan.id)]
    assert "marked_test_plan" in events
    assert service.mark_test_plan(plan.id, flag=False).ok
    assert service.get_plan(plan.id).test_plan is False


# ---------------------------------------------------------------------------
# 34/37. acceptance linkage + clip validation
# ---------------------------------------------------------------------------
def _link_plan_to_clip(library, plan_id: int, item_id: int, *, category="苹果干"):
    from tests.test_m5_coverage import _add_clip

    task_id = library.create_task(TaskRequest(material=category, target_clip_count=1))
    source_id = library.upsert_source_video(
        task_id=task_id,
        platform="douyin",
        platform_video_id="7900000000000000001",
        source_url="https://www.douyin.com/video/7900000000000000001",
        status=SourceVideoStatus.PROCESSED,
    )
    clip_id = _add_clip(
        library,
        library.settings.paths.library_root if hasattr(library, "settings") else None,
        index=42,
        category=category,
        stage=ProcessStage.DRYING,
        provenance=DOUYIN_REAL,
    )
    library.database.execute(
        "UPDATE clips SET task_id = ?, source_video_id = ? WHERE id = ?",
        (task_id, source_id, clip_id),
    )
    PlanRepository(library.database).link_task(
        plan_id=plan_id, plan_item_id=item_id, task_id=task_id, query="苹果干烘干"
    )
    return task_id, source_id, clip_id


def test_plan_linkage_walks_the_full_chain(seeded) -> None:
    library, service, settings = seeded
    plan, _action = service.create_plan("苹果干")
    assert plan is not None
    item = plan.items[0]
    root = settings.paths.library_root
    root.mkdir(parents=True, exist_ok=True)
    task_id = library.create_task(TaskRequest(material="苹果干", target_clip_count=1))
    source_id = library.upsert_source_video(
        task_id=task_id,
        platform="douyin",
        platform_video_id="7900000000000000002",
        source_url="https://www.douyin.com/video/7900000000000000002",
        status=SourceVideoStatus.PROCESSED,
    )
    from tests.test_m5_coverage import _add_clip

    clip_id = _add_clip(
        library, root, index=77, category="苹果干", stage=ProcessStage.DRYING, provenance=DOUYIN_REAL
    )
    library.database.execute(
        "UPDATE clips SET task_id = ?, source_video_id = ? WHERE id = ?",
        (task_id, source_id, clip_id),
    )
    service.repo.link_task(
        plan_id=plan.id, plan_item_id=item.id, task_id=task_id, query="苹果干烘干"
    )

    linkage = plan_linkage(library, plan.id)
    assert linkage.plan_id == plan.id
    assert linkage.item_ids == [item.id]
    assert linkage.task_ids == [task_id]
    assert linkage.source_video_ids == [source_id]
    assert linkage.clip_ids == [clip_id]
    text = "\n".join(linkage.lines())
    assert "collection_plan_item" in text and "clips" in text


class _FakeInfo:
    def __init__(self, duration: float, *, has_video: bool = True) -> None:
        self.duration = duration
        self.width = 1080
        self.height = 1920
        self.has_video = has_video


def test_validate_clip_acceptance_contract(seeded) -> None:
    library, _service, settings = seeded
    from tests.test_m5_coverage import _add_clip

    clip_id = _add_clip(
        library,
        settings.paths.library_root,
        index=5,
        category="苹果干",
        stage=ProcessStage.DRYING,
        provenance=DOUYIN_REAL,
        duration=6.0,
    )

    async def probe(path):
        return _FakeInfo(6.0)

    validation = validate_clip(library, settings, clip_id, probe=probe)
    assert validation.ok, validation.failures
    assert validation.details["duration"] == 6.0
    assert validation.details["provenance"] == DOUYIN_REAL

    async def too_long(path):
        return _FakeInfo(30.0)

    long_clip = validate_clip(library, settings, clip_id, probe=too_long)
    assert long_clip.ok is False
    assert "duration_in_range" in long_clip.failures

    async def no_video(path):
        return _FakeInfo(6.0, has_video=False)

    silent = validate_clip(library, settings, clip_id, probe=no_video)
    assert "video_stream" in silent.failures


def test_validate_clip_rejects_wrong_provenance_and_missing_file(seeded) -> None:
    library, _service, settings = seeded
    from tests.test_m5_coverage import _add_clip

    local_id = _add_clip(
        library,
        settings.paths.library_root,
        index=6,
        category="苹果干",
        provenance="local_test",
    )

    async def probe(path):
        return _FakeInfo(6.0)

    validation = validate_clip(library, settings, local_id, probe=probe)
    assert "provenance_is_real" in validation.failures

    missing_id = _add_clip(
        library,
        settings.paths.library_root,
        index=7,
        category="苹果干",
        with_video=False,
        with_thumbnail=False,
    )
    missing = validate_clip(library, settings, missing_id, probe=probe)
    assert "file_exists" in missing.failures
    assert "thumbnail_exists" in missing.failures
    assert "ffprobe_readable" in missing.failures

    absent = validate_clip(library, settings, 999999, probe=probe)
    assert absent.ok is False and absent.checks == {"clip_record_exists": False}


def test_acceptance_report_counts_validated_clips(seeded) -> None:
    library, service, settings = seeded
    from tests.test_m5_coverage import _add_clip

    plan, _action = service.create_plan("苹果干")
    item = plan.items[0]
    task_id = library.create_task(TaskRequest(material="苹果干", target_clip_count=1))
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
        index=8,
        category="苹果干",
        stage=ProcessStage.DRYING,
        provenance=DOUYIN_REAL,
    )
    library.database.execute(
        "UPDATE clips SET task_id = ?, source_video_id = ? WHERE id = ?",
        (task_id, source_id, clip_id),
    )
    service.repo.link_task(
        plan_id=plan.id, plan_item_id=item.id, task_id=task_id, query="苹果干烘干"
    )

    async def probe(path):
        return _FakeInfo(5.0)

    report = acceptance_report(library, settings, plan.id, probe=probe)
    assert report["clips_total"] == 1
    assert report["clips_ok"] == 1
    assert report["linkage"].clip_ids == [clip_id]


# ---------------------------------------------------------------------------
# CLI + Gradio
# ---------------------------------------------------------------------------
def test_cli_ranking_and_acceptance_commands(seeded, capsys) -> None:
    import app as app_module

    _library, service, settings = seeded
    plan, _action = service.create_plan("苹果干")
    assert app_module.run_query_ranking_report(settings) == 0
    output = capsys.readouterr().out
    assert "query_rank_v1 vs query_rank_v2" in output
    assert "苹果干烘干" in output

    assert app_module.run_query_ranking_report(settings, explain="苹果干烘干") == 0
    output = capsys.readouterr().out
    assert "confidence adjustment" in output

    assert app_module.run_acceptance_checks(settings, plan_id=plan.id) == 0
    output = capsys.readouterr().out
    assert "collection_plan_item" in output

    assert app_module.run_plan_control(settings, archive_id=plan.id) == 0
    assert service.get_plan(plan.id).archived is True
    assert app_module.run_plan_control(settings, unarchive_id=plan.id) == 0
    assert app_module.run_plan_control(settings, mark_test_id=plan.id) == 0
    assert service.get_plan(plan.id).test_plan is True
    assert app_module.run_plan_control(settings, unmark_test_id=plan.id) == 0


def test_cli_list_can_include_archived(seeded, capsys) -> None:
    import app as app_module

    _library, service, settings = seeded
    plan, _action = service.create_plan("苹果干")
    service.archive(plan.id)
    assert app_module.run_list_collection_plans(settings) == 0
    assert str(plan.id) not in capsys.readouterr().out
    assert app_module.run_list_collection_plans(settings, include_archived=True) == 0
    output = capsys.readouterr().out
    assert str(plan.id) in output and "归档" in output


@pytest.mark.skipif(
    not __import__("ui.gradio_app", fromlist=["GRADIO_AVAILABLE"]).GRADIO_AVAILABLE,
    reason="gradio is not installed",
)
def test_gradio_plan_tab_exposes_observability(settings, runner) -> None:
    from ui.gradio_app import build_ui

    demo = build_ui(settings, runner)
    # tabs expose ``label``; buttons/checkboxes keep their text in ``value``
    labels = [
        text
        for block in demo.blocks.values()
        for text in (getattr(block, "label", None), getattr(block, "value", None))
        if isinstance(text, str)
    ]
    for tab in ("素材采集", "素材库", "素材覆盖", "采集计划", "任务记录", "系统检查"):
        assert tab in labels
    for button in (
        "归档计划",
        "取消归档",
        "标记为验收/测试计划",
        "用当前排序版本重新生成查询词",
    ):
        assert button in labels, f"missing control: {button}"
    assert "显示已归档/测试计划" in labels
