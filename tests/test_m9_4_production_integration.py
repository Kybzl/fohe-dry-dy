"""Milestone 9.4 regression tests: production expansion + cleanup routing.

Normal tests use the deterministic M9.2 fakes; no web, Douyin or Qwen call is
made.  The real bounded production run is a separate operator acceptance.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.cleanup_routing import (
    ROUTING_ELIGIBLE,
    ROUTING_INELIGIBLE,
    ROUTING_NOT_NEEDED,
    ROUTING_UNMEASURED,
    CleanupRouter,
)
from core.coverage import CoverageAnalyzer
from core.library_service import LibraryService
from core.plan_runner import PlanRunner
from core.production_ready import ProductionReadyService
from core.subtitle_cleanup_models import CleanupStatus
from tests.test_m9_2_subtitle_cleanup import _add_clip, _service, run


def _set_analysis(library, clip_id: int, classification: str, cleanliness: float = 0.9) -> None:
    payload = {
        "classification": classification,
        "cleanliness_score": cleanliness,
        "analysis_version": "subtitle_analysis_v1",
        "decision_source": "local",
        "engine": "scripted",
    }
    library.database.execute(
        "UPDATE clips SET subtitle_analysis_json = ?, subtitle_type = ? WHERE id = ?",
        (json.dumps(payload, ensure_ascii=False), classification, int(clip_id)),
    )


def _router_with_fakes(settings):
    service, detector, toolkit, engine = _service(settings)
    router = CleanupRouter(service.library, settings, service=service)
    return router, service, detector, toolkit, engine


def _clip_with_analysis(router, settings, *, index: int = 1, classification: str = "bottom_simple", stage: str = "drying"):
    clip_id = _add_clip(router.library, settings.paths.library_root, index=index)
    if stage != "drying":
        router.library.database.execute(
            "UPDATE clips SET process_stage = ? WHERE id = ?", (stage, clip_id)
        )
    _set_analysis(router.library, clip_id, classification)
    return clip_id


# ---------------------------------------------------------------------------
# Routing classification
# ---------------------------------------------------------------------------
def test_new_accepted_clip_enters_cleanup_classification(settings) -> None:
    router, _service_obj, _detector, _toolkit, _engine = _router_with_fakes(settings)
    clip_id = _clip_with_analysis(router, settings, classification="bottom_simple")
    clip = router.library.get_clip(clip_id)
    decision = router.classify_clip(clip)
    assert decision.clip_id == clip_id
    assert decision.classification == "bottom_simple"
    assert decision.routing == ROUTING_ELIGIBLE


@pytest.mark.parametrize("classification", ["none", "watermark_only"])
def test_not_needed_routing(settings, classification: str) -> None:
    router, _service_obj, _detector, _toolkit, _engine = _router_with_fakes(settings)
    clip_id = _clip_with_analysis(router, settings, classification=classification)
    decision = router.classify_clip(router.library.get_clip(clip_id))
    assert decision.routing == ROUTING_NOT_NEEDED


@pytest.mark.parametrize("classification", ["multi_region", "colored_block", "complex"])
def test_ineligible_routing(settings, classification: str) -> None:
    router, _service_obj, _detector, _toolkit, _engine = _router_with_fakes(settings)
    clip_id = _clip_with_analysis(router, settings, classification=classification)
    decision = router.classify_clip(router.library.get_clip(clip_id))
    assert decision.routing == ROUTING_INELIGIBLE


@pytest.mark.parametrize("classification", ["bottom_simple", "top_simple", "single_region"])
def test_eligible_routing(settings, classification: str) -> None:
    router, _service_obj, _detector, _toolkit, _engine = _router_with_fakes(settings)
    clip_id = _clip_with_analysis(router, settings, classification=classification)
    decision = router.classify_clip(router.library.get_clip(clip_id))
    assert decision.routing == ROUTING_ELIGIBLE


def test_unmeasured_routing_is_not_eligible(settings) -> None:
    router, _service_obj, _detector, _toolkit, _engine = _router_with_fakes(settings)
    clip_id = _add_clip(router.library, settings.paths.library_root, index=1)
    decision = router.classify_clip(router.library.get_clip(clip_id))
    assert decision.routing == ROUTING_UNMEASURED
    assert not decision.eligible


def test_off_target_valid_clip_still_receives_cleanup_analysis(settings) -> None:
    router, _service_obj, _detector, _toolkit, _engine = _router_with_fakes(settings)
    clip_id = _clip_with_analysis(
        router, settings, classification="bottom_simple", stage="preparation"
    )
    decision = router.classify_clip(router.library.get_clip(clip_id))
    assert decision.routing == ROUTING_ELIGIBLE
    assert decision.details["process_stage"] == "preparation"


# ---------------------------------------------------------------------------
# Bounded post-acquisition cleanup
# ---------------------------------------------------------------------------
def test_cleanup_success_stays_pending(settings) -> None:
    router, service, _detector, _toolkit, engine = _router_with_fakes(settings)
    clip_id = _clip_with_analysis(router, settings)
    decisions = run(
        router.route_clips([clip_id], attempt_cleanup=True, max_attempts=2)
    )
    assert decisions[0].cleanup_attempted
    assert decisions[0].cleanup_result_status == str(CleanupStatus.SUCCEEDED)
    record = service.library.subtitle_cleanup(clip_id)
    assert record is not None
    assert record["status"] == str(CleanupStatus.SUCCEEDED)
    assert record["review_status"] == "pending"
    assert len(engine.calls) == 1


def test_cleanup_attempts_are_bounded(settings) -> None:
    router, _service_obj, _detector, _toolkit, engine = _router_with_fakes(settings)
    ids = [
        _clip_with_analysis(router, settings, index=index)
        for index in range(1, 4)
    ]
    decisions = run(
        router.route_clips(ids, attempt_cleanup=True, max_attempts=2)
    )
    assert sum(1 for item in decisions if item.cleanup_attempted) == 2
    assert len(engine.calls) == 2
    assert settings.subtitle_cleanup.post_acquisition_max_cleanup == 2


def test_acquisition_failure_does_not_trigger_cleanup(settings) -> None:
    router, service, _detector, _toolkit, engine = _router_with_fakes(settings)
    decisions = run(router.route_clips([], attempt_cleanup=True, max_attempts=2))
    assert decisions == []
    assert engine.calls == []
    assert service.library.list_subtitle_cleanups(limit=10) == []


class _RecordingRouter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def route_clips(self, clip_ids, **kwargs):
        self.calls.append({"clip_ids": list(clip_ids), **kwargs})
        return []


class _RepoStub:
    def log_event(self, *args, **kwargs) -> None:
        return None


def test_plan_runner_wires_cleanup_router(settings) -> None:
    router, service, _detector, _toolkit, _engine = _router_with_fakes(settings)
    clip_id = _clip_with_analysis(router, settings)
    recording = _RecordingRouter()
    runner = PlanRunner(
        999,
        library=service.library,
        settings=settings,
        repository=_RepoStub(),
        cleanup_new=True,
        cleanup_new_limit=1,
        cleanup_router=recording,
    )
    clip = service.library.get_clip(clip_id)
    plan = SimpleNamespace()
    item = SimpleNamespace(id=7, process_stage="drying")
    decisions = run(
        runner._route_new_clips([clip], plan=plan, item=item, query="辣椒热泵烘干")
    )
    assert decisions == []
    assert recording.calls
    assert recording.calls[0]["attempt_cleanup"] is True
    assert recording.calls[0]["max_attempts"] == 1


# ---------------------------------------------------------------------------
# Semantic duplication / coverage / preferred media
# ---------------------------------------------------------------------------
def test_derivative_does_not_increment_clip_count_or_coverage(settings) -> None:
    router, service, _detector, _toolkit, _engine = _router_with_fakes(settings)
    clip_id = _clip_with_analysis(router, settings)
    analyzer = CoverageAnalyzer(service.library, settings.coverage)
    before = (
        service.library.count_clips(),
        analyzer.report().total,
        [(stage.stage, stage.total) for stage in analyzer.report().stages],
    )
    run(router.route_clips([clip_id], attempt_cleanup=True, max_attempts=1))
    after = (
        service.library.count_clips(),
        analyzer.report().total,
        [(stage.stage, stage.total) for stage in analyzer.report().stages],
    )
    assert before == after


def test_pending_cleanup_does_not_replace_original(settings) -> None:
    router, service, _detector, _toolkit, _engine = _router_with_fakes(settings)
    clip_id = _clip_with_analysis(router, settings)
    run(router.route_clips([clip_id], attempt_cleanup=True, max_attempts=1))
    clip = service.library.get_clip(clip_id)
    assert service.library.preferred_media_path(clip) == Path(clip.file_path)


def test_approved_cleanup_resolves_derivative_and_rejected_resolves_original(settings) -> None:
    router, service, _detector, _toolkit, _engine = _router_with_fakes(settings)
    clip_id = _clip_with_analysis(router, settings)
    decisions = run(router.route_clips([clip_id], attempt_cleanup=True, max_attempts=1))
    derivative = Path(decisions[0].cleanup_output_path)
    clip = service.library.get_clip(clip_id)
    assert service.review_cleanup(clip_id, status="approved")[0]
    assert service.library.preferred_media_path(clip) == derivative
    assert service.review_cleanup(
        clip_id, status="rejected", failure_class="other", note="test"
    )[0]
    assert service.library.preferred_media_path(clip) == Path(clip.file_path)


def test_missing_approved_derivative_falls_back_to_original(settings) -> None:
    router, service, _detector, _toolkit, _engine = _router_with_fakes(settings)
    clip_id = _clip_with_analysis(router, settings)
    decisions = run(router.route_clips([clip_id], attempt_cleanup=True, max_attempts=1))
    derivative = Path(decisions[0].cleanup_output_path)
    assert service.review_cleanup(clip_id, status="approved")[0]
    derivative.unlink()
    clip = service.library.get_clip(clip_id)
    assert service.library.preferred_media_path(clip) == Path(clip.file_path)
    report = ProductionReadyService(service.library, settings).report()
    assert report.aggregates["missing_approved_derivatives"] == 1


# ---------------------------------------------------------------------------
# Production-ready report / export integration
# ---------------------------------------------------------------------------
def test_production_ready_report(settings) -> None:
    router, service, _detector, _toolkit, _engine = _router_with_fakes(settings)
    first_id = _clip_with_analysis(router, settings, index=1)
    second_id = _add_clip(service.library, settings.paths.library_root, index=2)
    _set_analysis(service.library, second_id, "none")
    report = ProductionReadyService(service.library, settings).report()
    assert report.aggregates["total_semantic_clips"] == 2
    assert report.aggregates["production_ready_clips"] == 2
    rows = {row.clip_id: row for row in report.rows}
    assert rows[first_id].preferred_kind == "original"
    assert rows[first_id].production_ready
    assert rows[second_id].subtitle_class == "none"


def test_production_ready_report_counts_approved_cleaned(settings) -> None:
    router, service, _detector, _toolkit, _engine = _router_with_fakes(settings)
    clip_id = _clip_with_analysis(router, settings)
    run(router.route_clips([clip_id], attempt_cleanup=True, max_attempts=1))
    assert service.review_cleanup(clip_id, status="approved")[0]
    report = ProductionReadyService(service.library, settings).report()
    row = report.rows[0]
    assert row.preferred_kind == "cleaned"
    assert report.aggregates["cleaned_preferred"] == 1
    assert report.aggregates["cleanup_approved"] == 1


def test_export_manifest_exposes_preferred_media_without_changing_original(settings) -> None:
    router, service, _detector, _toolkit, _engine = _router_with_fakes(settings)
    clip_id = _clip_with_analysis(router, settings)
    decisions = run(router.route_clips([clip_id], attempt_cleanup=True, max_attempts=1))
    clip = service.library.get_clip(clip_id)
    manifest = LibraryService(service.library, settings).record(clip)
    assert manifest["preferred_media_kind"] == "original"
    assert manifest["file_path"] == str(clip.file_path)
    assert service.review_cleanup(clip_id, status="approved")[0]
    manifest = LibraryService(service.library, settings).record(clip)
    assert manifest["preferred_media_kind"] == "cleaned"
    assert manifest["preferred_media_path"] == decisions[0].cleanup_output_path
    assert manifest["file_path"] == str(clip.file_path)


def test_report_aggregation_with_failed_and_rejected(settings) -> None:
    router, service, _detector, _toolkit, _engine = _router_with_fakes(settings)
    approved_id = _clip_with_analysis(router, settings, index=1)
    rejected_id = _clip_with_analysis(router, settings, index=2)
    run(router.route_clips([approved_id, rejected_id], attempt_cleanup=True, max_attempts=2))
    service.review_cleanup(approved_id, status="approved")
    service.review_cleanup(
        rejected_id, status="rejected", failure_class="visible_blur_patch", note="blur"
    )
    report = ProductionReadyService(service.library, settings).report()
    assert report.aggregates["cleanup_approved"] == 1
    assert report.aggregates["cleanup_rejected"] == 1
    assert report.aggregates["missing_preferred_media"] == 0
