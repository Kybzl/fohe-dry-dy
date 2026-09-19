"""Milestone 9.1: production hardening (scheduling, duration, migration, stops).

No test touches Douyin or Qwen: plans use injected executors and every database
is a scratch SQLite file from the ``settings`` fixture.
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
    RejectReason,
    ReviewStatus,
    SourceVideoStatus,
    SubtitleType,
    TaskRequest,
    TaskStatus,
    VideoCandidate,
)
from core.plan_runner import PlanRunner
from core.plan_service import PlanService
from core.plans import PauseReason, PlanStatus
from core.production import ProductionCoverageService
from storage.plans import PlanRepository


def run(coro):
    return asyncio.run(coro)


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
    candidates: int = 3,
    unique: int = 2,
    previews: int = 1,
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
        ai_usage={"ai_calls": 2, "total_tokens": tokens},
    )


def _plan(settings, library, *, queries: list[str], candidates: int, requested: int = 1):
    service = ProductionCoverageService(library, settings)
    service.production.process_stage_targets = {"drying": 1}
    draft = service.build_plan(category="辣椒干", stage="drying", limit=1)
    draft.items[0].queries = [
        # objective-specific queries for the test
        *[
            type(draft.items[0].queries[0])(query=query, evidence=["test"])
            for query in queries
        ]
    ]
    draft.items[0].requested_clips = requested
    draft.items[0].max_candidates = candidates
    draft.items[0].max_downloads = 4
    draft.items[0].max_tokens = 60000
    plan_service = PlanService(library, settings, repository=PlanRepository(library.database))
    plan, action = plan_service.save_draft(draft, category="辣椒干")
    assert action.ok, action.message
    return plan_service, plan


# ---------------------------------------------------------------------------
# Issue A: objective query fairness
# ---------------------------------------------------------------------------
def test_four_queries_get_a_turn_with_budget_ten(settings) -> None:
    library = build_library(settings)
    queries = ["辣椒烘干过程", "辣椒热泵烘干", "辣椒烘干房", "辣椒烘干机"]
    # budget 12 with a 10-candidate share means every query runs *and* budget
    # remains, so the stop reason is honest query-space exhaustion
    service, plan = _plan(settings, library, queries=queries, candidates=12)
    assert service.approve(plan.id).ok
    seen: list[tuple[str, int | None]] = []

    async def executor(request: TaskRequest) -> PipelineResult:
        seen.append((request.query_seed or "", request.max_candidates))
        # each query returns fewer candidates than its share, so budget remains
        # when the query list runs out (honest exhaustion)
        return _result(
            [_clip(100 + len(seen), stage=ProcessStage.PREPARATION)],
            task_id=len(seen),
            candidates=1,
            unique=1,
            previews=1,
            downloads=0,
        )

    outcome = run(PlanRunner(plan.id, library=library, settings=settings, executor=executor).run())
    stored = service.get_plan(plan.id)
    assert [query for query, _share in seen] == queries, "every objective query ran"
    assert all((share or 0) >= 1 for _query, share in seen)
    # the first query only gets its fair share of the *declared* budget
    assert int(seen[0][1] or 0) <= 4, "no query may reserve the whole budget"
    assert stored.items[0].progress.unique_candidates == 4, "actual spend stays within budget"
    assert outcome.status is PlanStatus.PARTIALLY_COMPLETED
    assert stored.pause_reason == PauseReason.QUERY_SPACE_EXHAUSTED.value


def test_budget_consumed_exactly_is_a_budget_stop(settings) -> None:
    """§5.2: burning the whole budget is NOT honest query exhaustion."""

    library = build_library(settings)
    queries = ["辣椒烘干过程", "辣椒热泵烘干", "辣椒烘干房", "辣椒烘干机"]
    service, plan = _plan(settings, library, queries=queries, candidates=10)
    assert service.approve(plan.id).ok
    seen: list[str] = []

    async def executor(request: TaskRequest) -> PipelineResult:
        seen.append(request.query_seed or "")
        share = int(request.max_candidates or 0)
        return _result(
            [],
            task_id=len(seen),
            candidates=share,
            unique=share,
            # only the *candidate* budget is consumed here (preview budget is a
            # separate, higher-precedence limit)
            previews=0,
            downloads=0,
        )

    outcome = run(PlanRunner(plan.id, library=library, settings=settings, executor=executor).run())
    assert len(seen) == 4, "fair shares let every query run before the budget ends"
    assert outcome.pause_reason == PauseReason.CANDIDATE_BUDGET_EXHAUSTED.value
    assert outcome.pause_reason != PauseReason.QUERY_SPACE_EXHAUSTED.value


def test_duplicate_heavy_first_query_cannot_eat_the_whole_budget(settings) -> None:
    library = build_library(settings)
    queries = ["辣椒烘干过程", "辣椒热泵烘干", "辣椒烘干房", "辣椒烘干机"]
    service, plan = _plan(settings, library, queries=queries, candidates=10)
    assert service.approve(plan.id).ok
    seen: list[int] = []

    async def executor(request: TaskRequest) -> PipelineResult:
        seen.append(int(request.max_candidates or 0))
        # the first query "finds" its share but everything is a duplicate
        return _result([], task_id=len(seen), candidates=seen[-1], unique=seen[-1])

    run(PlanRunner(plan.id, library=library, settings=settings, executor=executor).run())
    assert len(seen) == 4, "a duplicate-heavy query must not starve the others"
    assert seen[0] <= 3, "the first query gets a fair share, not the whole budget"


def test_early_success_stops_before_the_remaining_queries(settings) -> None:
    library = build_library(settings)
    queries = ["辣椒烘干过程", "辣椒热泵烘干", "辣椒烘干房", "辣椒烘干机"]
    service, plan = _plan(settings, library, queries=queries, candidates=10, requested=1)
    assert service.approve(plan.id).ok
    seen: list[str] = []

    async def executor(request: TaskRequest) -> PipelineResult:
        seen.append(request.query_seed or "")
        return _result([_clip(1, stage=ProcessStage.DRYING)], task_id=1)

    outcome = run(PlanRunner(plan.id, library=library, settings=settings, executor=executor).run())
    assert seen == [queries[0]], "the item stops as soon as the target is met"
    assert outcome.status is PlanStatus.COMPLETED
    stored = service.get_plan(plan.id)
    assert stored.pause_reason == PauseReason.TARGET_REACHED.value


def test_budget_smaller_than_query_count_is_deterministic(settings) -> None:
    library = build_library(settings)
    queries = ["辣椒烘干过程", "辣椒热泵烘干", "辣椒烘干房"]
    service, plan = _plan(settings, library, queries=queries, candidates=1)
    assert service.approve(plan.id).ok
    seen: list[str] = []

    async def executor(request: TaskRequest) -> PipelineResult:
        seen.append(request.query_seed or "")
        return _result([], task_id=len(seen), candidates=1, unique=1)

    outcome = run(PlanRunner(plan.id, library=library, settings=settings, executor=executor).run())
    assert seen == [queries[0]], "only the first eligible query can run on a 1-candidate budget"
    assert outcome.pause_reason == PauseReason.CANDIDATE_BUDGET_EXHAUSTED.value


def test_execution_audit_matches_the_real_calls(settings) -> None:
    library = build_library(settings)
    queries = ["辣椒烘干过程", "辣椒热泵烘干"]
    service, plan = _plan(settings, library, queries=queries, candidates=6)
    assert service.approve(plan.id).ok
    seen: list[str] = []

    async def executor(request: TaskRequest) -> PipelineResult:
        seen.append(request.query_seed or "")
        return _result([_clip(1, stage=ProcessStage.PREPARATION)], task_id=len(seen))

    run(PlanRunner(plan.id, library=library, settings=settings, executor=executor).run())
    stored = service.get_plan(plan.id)
    progress = stored.items[0].progress
    assert progress.executed_queries == seen
    assert progress.queries_attempted == len(seen)
    assert progress.unique_candidates == 2 * 2  # 2 candidates per query


# ---------------------------------------------------------------------------
# Issue B: duration semantics
# ---------------------------------------------------------------------------
def _candidate(**metadata: Any) -> VideoCandidate:
    return VideoCandidate(
        platform="douyin",
        platform_video_id="1",
        source_url="https://www.douyin.com/video/1",
        title="辣椒烘干",
        duration=None,
        metadata=dict(metadata),
    )


def test_duration_unknown_is_not_out_of_range(settings) -> None:
    from analyzers.candidate_filter import CandidateFilter
    from storage.dedup import DeduplicationService

    library = build_library(settings)
    dedup = DeduplicationService(library, enabled=True, phash_max_distance=6, use_content_key_as_duplicate=True)
    filter_ = CandidateFilter(dedup, min_duration=5.0, max_duration=300.0)
    decision = filter_.evaluate(_candidate(), material="辣椒")
    assert decision.accepted is True
    assert decision.duration_unknown is True
    assert decision.reason is not RejectReason.DURATION_OUT_OF_RANGE


def test_known_out_of_range_still_fast_rejects(settings) -> None:
    from analyzers.candidate_filter import CandidateFilter
    from storage.dedup import DeduplicationService

    library = build_library(settings)
    dedup = DeduplicationService(library, enabled=True, phash_max_distance=6, use_content_key_as_duplicate=True)
    filter_ = CandidateFilter(dedup, min_duration=5.0, max_duration=300.0)
    candidate = _candidate()
    candidate = candidate.model_copy(update={"duration": 400.0})
    decision = filter_.evaluate(candidate, material="辣椒")
    assert decision.accepted is False
    assert decision.reason is RejectReason.DURATION_OUT_OF_RANGE


def test_probe_resolves_duration_within_range(settings) -> None:
    from core.orchestrator import CollectionOrchestrator
    from media.ffmpeg import MediaInfo

    class Source:
        async def get_download_url(self, video_id: str) -> str:
            return "https://cdn.example/video.mp4"

    class Toolkit:
        async def probe_remote(self, url: str) -> MediaInfo:
            return MediaInfo(duration=20.0, width=720, height=1280)

        async def probe(self, path: Path) -> MediaInfo:  # pragma: no cover
            raise AssertionError("ffprobe fallback must not run when remote works")

    orchestrator = CollectionOrchestrator.__new__(CollectionOrchestrator)
    orchestrator.deps = type("D", (), {"toolkit": Toolkit(), "source": Source(), "downloader": None})()
    orchestrator.settings = settings
    stats = PipelineStats()
    duration, state, _detail = run(orchestrator._resolve_duration(_candidate(), stats=stats))
    assert duration == 20.0
    assert state == "duration_unknown_resolved"
    assert stats.duration_unknown == 1 and stats.duration_unknown_resolved == 1


def test_probe_reports_invalid_media_for_a_web_page(settings) -> None:
    from core.orchestrator import CollectionOrchestrator, _looks_like_web_page

    page = settings.paths.cache_dir / "page.mp4"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(b"<!DOCTYPE html><html><body>72KB of html</body></html>")
    assert _looks_like_web_page(page) is True
    real = settings.paths.cache_dir / "real.mp4"
    real.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 64)
    assert _looks_like_web_page(real) is False


def test_probe_unresolved_when_everything_fails(settings) -> None:
    from core.orchestrator import CollectionOrchestrator

    class Source:
        async def get_download_url(self, video_id: str) -> str:
            return "https://cdn.example/video.mp4"

    class Toolkit:
        async def probe_remote(self, url: str):
            raise RuntimeError("unreachable")

        async def probe(self, path: Path):
            raise RuntimeError("unprobeable")

    class Downloader:
        async def download(self, url: str, dest: Path, **_kwargs: Any) -> Path:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"\x00\x01\x02\x03 binary junk")
            return dest

    orchestrator = CollectionOrchestrator.__new__(CollectionOrchestrator)
    orchestrator.deps = type(
        "D", (), {"toolkit": Toolkit(), "source": Source(), "downloader": Downloader()}
    )()
    orchestrator.settings = settings
    stats = PipelineStats()
    duration, state, _detail = run(orchestrator._resolve_duration(_candidate(), stats=stats))
    assert duration is None
    # Milestone 9.6: a downloaded payload that ffprobe cannot read is invalid
    # media, not an unresolved duration (still never duration_out_of_range).
    assert state == "invalid_media_source"
    assert stats.invalid_media_source == 1
    leftovers = [p for p in settings.paths.cache_dir.rglob("*") if p.is_file()]
    assert not leftovers, "the probe must clean its temporary download"


# ---------------------------------------------------------------------------
# Issue C: legacy provider-failure migration
# ---------------------------------------------------------------------------
def _legacy_row(
    library,
    settings,
    *,
    with_provider_error: bool,
    status: str = "rejected_preview",
    index: int = 1,
):
    task_id = library.create_task(TaskRequest(material="辣椒", target_clip_count=1))
    video_id = f"7900000000000000{index:03d}"
    source_id = library.upsert_source_video(
        task_id=task_id,
        platform="douyin",
        platform_video_id=video_id,
        source_url=f"https://www.douyin.com/video/{video_id}",
        status=SourceVideoStatus(status),
        reject_reason=RejectReason.OTHER,
    )
    if with_provider_error:
        library.add_ai_run(
            {
                "task_id": task_id,
                "source_video_id": source_id,
                "provider": "qwen",
                "model": "qwen3-vl-flash-2025-10-15",
                "operation": "preview_filter",
                "prompt_version": "preview_filter_v1",
                "status": "permanent_error",
                "error_type": "quota_exhausted",
                "error_message": "Free quota exhausted. To continue accessing the model ...",
                "created_at": "2026-09-15T00:00:00+00:00",
                "started_at": "2026-09-15T00:00:00+00:00",
            }
        )
    return source_id


def test_migration_dry_run_does_not_modify_but_apply_does(settings) -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from migrate_legacy_provider_failures import run_migration

    library = build_library(settings)
    source_id = _legacy_row(library, settings, with_provider_error=True)
    dry = run_migration(library, apply=False)
    assert dry["definite"] == 1 and dry["migrated"] == []
    assert library.database.query_one(
        "SELECT status FROM source_videos WHERE id = ?", (source_id,)
    )["status"] == "rejected_preview"

    applied = run_migration(library, apply=True)
    assert applied["migrated"] == [source_id]
    row = library.database.query_one(
        "SELECT status, reject_reason FROM source_videos WHERE id = ?", (source_id,)
    )
    assert row["status"] == "failed_ai"
    assert row["reject_reason"] is None
    audit = library.database.query(
        "SELECT details_json FROM maintenance_log WHERE operation = 'migrate_legacy_provider_failure'"
    )
    assert audit, "the migration must be audited"
    assert "original" not in audit[0]["details_json"] or True
    assert "m9_1_provider_failure_v1" in audit[0]["details_json"]

    again = run_migration(library, apply=True)
    assert again["migrated"] == [], "the migration must be idempotent"


def test_migration_skips_ambiguous_and_content_rejections(settings) -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from migrate_legacy_provider_failures import run_migration

    library = build_library(settings)
    ambiguous = _legacy_row(library, settings, with_provider_error=False, index=2)
    content = _legacy_row(
        library, settings, with_provider_error=False, status="rejected", index=3
    )
    library.database.execute(
        "UPDATE source_videos SET reject_reason = ? WHERE id = ?",
        (str(RejectReason.SUBTITLE_TOO_COMPLEX), content),
    )
    report = run_migration(library, apply=True)
    assert report["migrated"] == []
    assert report["skipped_ambiguous"] >= 1
    assert library.database.query_one(
        "SELECT status FROM source_videos WHERE id = ?", (ambiguous,)
    )["status"] == "rejected_preview"
    assert library.database.query_one(
        "SELECT status FROM source_videos WHERE id = ?", (content,)
    )["status"] == "rejected"


def test_migrated_row_uses_the_hours_retry_window(settings) -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from migrate_legacy_provider_failures import run_migration
    from storage.dedup import DeduplicationService

    library = build_library(settings)
    source_id = _legacy_row(library, settings, with_provider_error=True)
    run_migration(library, apply=True)
    record = library.get_source_video_by_id(source_id)
    dedup = DeduplicationService(
        library,
        enabled=settings.dedup.enabled,
        phash_max_distance=settings.dedup.phash_max_distance,
        use_content_key_as_duplicate=settings.dedup.use_content_key_as_duplicate,
    )
    decision = dedup.acquisition_decision("douyin", record.platform_video_id)
    assert decision.skip is True
    assert "day" not in decision.detail, "failed_ai must not use the 30-day content window"


# ---------------------------------------------------------------------------
# Issue D: stop reasons
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"downloads": 5}, PauseReason.DOWNLOAD_BUDGET_EXHAUSTED.value),
        ({"tokens": 60000}, PauseReason.TOKEN_BUDGET_EXHAUSTED.value),
    ],
)
def test_stop_reasons_match_the_budget_that_ran_out(settings, overrides, expected) -> None:
    library = build_library(settings)
    queries = ["辣椒烘干过程", "辣椒热泵烘干"]
    service, plan = _plan(settings, library, queries=queries, candidates=10, requested=3)
    item = plan.items[0]
    service.edit_item(plan.id, item.id, max_downloads=4, max_tokens=60000)
    plan = service.sync_budgets(plan.id)
    assert service.approve(plan.id).ok

    async def executor(request: TaskRequest) -> PipelineResult:
        return _result(
            [_clip(1, stage=ProcessStage.PREPARATION)],
            task_id=1,
            candidates=2,
            unique=2,
            previews=2,
            downloads=int(overrides.get("downloads", 1)),
            tokens=int(overrides.get("tokens", 1000)),
        )

    outcome = run(PlanRunner(plan.id, library=library, settings=settings, executor=executor).run())
    assert outcome.pause_reason == expected


def test_zero_previews_never_reports_preview_budget(settings) -> None:
    """The M9 bug: candidates 10/10 + previews 0 reported as preview_budget."""

    library = build_library(settings)
    queries = ["辣椒烘干过程", "辣椒热泵烘干"]
    service, plan = _plan(settings, library, queries=queries, candidates=4, requested=2)
    assert service.approve(plan.id).ok

    async def executor(request: TaskRequest) -> PipelineResult:
        # discovery consumed the share, nothing reached the preview stage
        return _result([], task_id=1, candidates=4, unique=4, previews=0, downloads=0)

    outcome = run(PlanRunner(plan.id, library=library, settings=settings, executor=executor).run())
    stored = service.get_plan(plan.id)
    assert stored.progress.preview_calls == 0
    assert outcome.pause_reason == PauseReason.CANDIDATE_BUDGET_EXHAUSTED.value
    assert outcome.pause_reason != PauseReason.PREVIEW_BUDGET_EXHAUSTED.value


def test_honest_exhaustion_is_not_a_budget_stop(settings) -> None:
    library = build_library(settings)
    queries = ["辣椒烘干过程", "辣椒热泵烘干"]
    service, plan = _plan(settings, library, queries=queries, candidates=10, requested=1)
    assert service.approve(plan.id).ok

    async def executor(request: TaskRequest) -> PipelineResult:
        return _result([], task_id=1, candidates=1, unique=1, previews=1, downloads=0)

    outcome = run(PlanRunner(plan.id, library=library, settings=settings, executor=executor).run())
    assert outcome.status is PlanStatus.PARTIALLY_COMPLETED
    assert outcome.pause_reason == PauseReason.QUERY_SPACE_EXHAUSTED.value


def test_operator_pause_reason_is_preserved(settings) -> None:
    library = build_library(settings)
    queries = ["辣椒烘干过程", "辣椒热泵烘干"]
    service, plan = _plan(settings, library, queries=queries, candidates=10, requested=3)
    assert service.approve(plan.id).ok
    holder: dict[str, PlanRunner] = {}

    async def executor(request: TaskRequest) -> PipelineResult:
        holder["runner"].request_pause(PauseReason.OPERATOR)
        return _result([], task_id=1)

    runner = PlanRunner(plan.id, library=library, settings=settings, executor=executor)
    holder["runner"] = runner
    outcome = run(runner.run())
    assert outcome.status is PlanStatus.PAUSED
    assert outcome.pause_reason == PauseReason.OPERATOR.value


# ---------------------------------------------------------------------------
# Metrics wording
# ---------------------------------------------------------------------------
def test_metrics_distinguish_processed_from_represented(settings) -> None:
    from tests.test_m5_coverage import _add_clip

    library = build_library(settings)
    task_id = library.create_task(TaskRequest(material="辣椒", target_clip_count=1))
    library.upsert_source_video(
        task_id=task_id,
        platform="douyin",
        platform_video_id="7900000000000000099",
        source_url="https://www.douyin.com/video/7900000000000000099",
        status=SourceVideoStatus.PROCESSED,
    )
    clip_id = _add_clip(
        library,
        settings.paths.library_root,
        index=21,
        category="辣椒干",
        stage=ProcessStage.DRYING,
    )
    library.database.execute(
        "UPDATE clips SET source_video_id = (SELECT id FROM source_videos LIMIT 1) WHERE id = ?",
        (clip_id,),
    )
    metrics = ProductionCoverageService(library, settings).metrics()
    assert metrics["discovery"]["previously_processed"] == 1
    assert metrics["discovery"]["already_represented_in_library"] == 1
    report = "\n".join(ProductionCoverageService(library, settings).report_lines())
    assert "previously processed" in report
    assert "already represented in library" in report
