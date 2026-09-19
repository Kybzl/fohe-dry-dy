"""M9.7.1 focused regression: provider recovery + browser-gated query retry."""

from __future__ import annotations

from pathlib import Path

from core.dependencies import build_library
from core.models import TaskRequest
from core.plan_runner import PlanRunner
from core.plan_service import PlanService
from core.plans import PauseReason, PlanStatus
from scripts.repair_m9_7_1_plan18 import repair_plan_state
from tests.test_m9_1_production_hardening import _clip, _plan, _result, run


def _with_discovery(result, *, status: str = "", states=None, blocked: bool = False):
    return result.model_copy(
        update={
            "discovery_status": status,
            "discovery_states": dict(states or {}),
            "discovery_blocked": blocked,
        }
    )


def _approve(service, plan) -> None:
    action = service.approve(plan.id)
    assert action.ok, action.message


def _blocked_result(status: str, *, task_id: int = 97, query: str = ""):
    base = _result(
        [],
        task_id=task_id,
        candidates=0,
        unique=0,
        previews=0,
        downloads=0,
        tokens=0,
    ).model_copy(update={"ai_usage": {"ai_calls": 0, "total_tokens": 0}})
    result = _with_discovery(
        base,
        status=status,
        states={"browser": status},
    )
    if query:
        result = result.model_copy(
            update={
                "query_audit": [
                    {
                        "query": query,
                        "candidates": 0,
                        "unique": 0,
                        "new_to_system": 0,
                        "known_source": 0,
                        "current_run_duplicate": 0,
                        "already_processed": 0,
                        "already_represented": 0,
                    }
                ]
            }
        )
    return result


# ---------------------------------------------------------------------------
# ISSUE A: current provider state must not leak historical quota data
# ---------------------------------------------------------------------------
def test_readiness_success_clears_stale_current_provider_state(settings) -> None:
    library = build_library(settings)
    service, plan = _plan(settings, library, queries=["红薯热泵烘干"], candidates=8)
    repo = service.repo
    # historical quota pause (stays in events)
    repo.log_event(
        plan.id,
        "provider_unavailable",
        details={"failure_class": "account_blocking", "subtype": "quota_exhausted"},
    )
    repo.set_status(
        plan.id, PlanStatus.PAUSED, pause_reason=PauseReason.PROVIDER_UNAVAILABLE
    )
    repo.set_provider_state(
        plan.id,
        failure_class="account_blocking",
        subtype="quota_exhausted",
        ready=False,
        provider="qwen",
        model="qwen3-vl-plus-2025-09-23",
        operation="readiness",
    )
    # live-equivalent readiness success
    repo.set_provider_state(
        plan.id,
        failure_class="",
        subtype="",
        ready=True,
        provider="qwen",
        model="qwen-vl-max",
        operation="readiness",
    )
    repo.set_status(
        plan.id,
        PlanStatus.PAUSED,
        pause_reason=PauseReason.HUMAN_VERIFICATION_REQUIRED,
    )
    stored = service.get_plan(plan.id)
    assert stored is not None
    assert stored.provider_ready is True
    assert stored.provider_name == "qwen"
    assert stored.provider_model == "qwen-vl-max"
    assert stored.provider_failure_class == ""
    assert stored.provider_failure_subtype == ""
    assert stored.pause_reason == PauseReason.HUMAN_VERIFICATION_REQUIRED.value
    # historical event remains
    assert any(
        event["event"] == "provider_unavailable"
        for event in repo.events(plan.id)
    )
    text = "\n".join(service.status_lines(stored))
    assert "quota_exhausted" not in text
    assert "qwen3-vl-plus-2025-09-23" not in text
    assert "ready=True" in text and "qwen-vl-max" in text
    assert "human_verification_required" in service.pause_reason_text(stored)


# ---------------------------------------------------------------------------
# ISSUE B: operational discovery blockers do not consume the query slot
# ---------------------------------------------------------------------------
def test_login_required_attempts_but_does_not_complete_query(settings) -> None:
    library = build_library(settings)
    service, plan = _plan(
        settings, library, queries=["红薯热泵烘干", "红薯烘干设备"], candidates=8
    )
    _approve(service, plan)
    seen: list[str] = []

    async def executor(request):
        seen.append(request.query_seed or "")
        return _blocked_result(
            "login_required", query=request.query_seed or ""
        )

    outcome = run(
        PlanRunner(plan.id, library=library, settings=settings, executor=executor).run()
    )
    stored = service.get_plan(plan.id)
    item = stored.items[0]
    assert outcome.pause_reason == PauseReason.HUMAN_VERIFICATION_REQUIRED.value
    assert item.progress.executed_queries == []
    assert item.progress.queries_attempted == 1
    assert item.progress.queries_remaining == 2
    assert item.progress.candidates_seen == 0
    assert item.progress.preview_calls == 0
    assert item.progress.downloads == 0
    assert item.progress.ai_calls == 0
    audit = item.progress.query_audit[-1]
    assert audit["discovery_status"] == "login_required"
    assert audit["completed"] is False
    assert audit["operational_blocker"] is True


def test_same_query_retry_after_login_recovery_completes_once(settings) -> None:
    library = build_library(settings)
    service, plan = _plan(
        settings, library, queries=["红薯热泵烘干", "红薯烘干设备"], candidates=8
    )
    _approve(service, plan)

    async def blocked(request):
        return _blocked_result("login_required")

    run(PlanRunner(plan.id, library=library, settings=settings, executor=blocked).run())
    resumed_seen: list[str] = []

    async def recovered(request):
        resumed_seen.append(request.query_seed or "")
        return _result([_clip(1, stage="drying")], task_id=98)

    outcome = run(
        PlanRunner(plan.id, library=library, settings=settings, executor=recovered).run()
    )
    stored = service.get_plan(plan.id)
    assert resumed_seen == ["红薯热泵烘干"]
    assert outcome.qualifying_clips == 1
    assert stored.items[0].progress.executed_queries == ["红薯热泵烘干"]
    assert stored.progress.clips_saved == 1


def test_no_results_is_a_completed_query(settings) -> None:
    library = build_library(settings)
    service, plan = _plan(settings, library, queries=["红薯热泵烘干"], candidates=8)
    _approve(service, plan)

    async def executor(request):
        return _with_discovery(_result([], task_id=1), status="no_results")

    outcome = run(
        PlanRunner(plan.id, library=library, settings=settings, executor=executor).run()
    )
    stored = service.get_plan(plan.id)
    assert stored.items[0].progress.executed_queries == ["红薯热泵烘干"]
    assert outcome.pause_reason == PauseReason.QUERY_SPACE_EXHAUSTED.value


def test_browser_unavailable_and_upstream_bad_gateway_do_not_exhaust(settings) -> None:
    for status in ("browser_unavailable", "upstream_bad_gateway"):
        library = build_library(settings)
        service, plan = _plan(
            settings, library, queries=["红薯热泵烘干", "红薯烘干设备"], candidates=8
        )
        _approve(service, plan)

        async def executor(request, _status=status):
            return _blocked_result(_status, task_id=1)

        outcome = run(
            PlanRunner(
                plan.id, library=library, settings=settings, executor=executor
            ).run()
        )
        stored = service.get_plan(plan.id)
        assert stored.items[0].progress.executed_queries == []
        assert outcome.pause_reason in (
            PauseReason.BACKEND_UNAVAILABLE.value,
            PauseReason.HUMAN_VERIFICATION_REQUIRED.value,
        )


def test_already_completed_query_is_not_reopened_on_resume(settings) -> None:
    library = build_library(settings)
    service, plan = _plan(
        settings,
        library,
        queries=["红薯热泵烘干", "红薯烘干设备"],
        candidates=8,
    )
    _approve(service, plan)
    calls: list[str] = []

    async def executor(request):
        calls.append(request.query_seed or "")
        if len(calls) == 1:
            return _with_discovery(_result([], task_id=1), status="ok")
        return _blocked_result("login_required", task_id=2)

    run(PlanRunner(plan.id, library=library, settings=settings, executor=executor).run())
    paused = service.get_plan(plan.id)
    assert paused.items[0].progress.executed_queries == ["红薯热泵烘干"]

    resumed: list[str] = []

    async def recovered(request):
        resumed.append(request.query_seed or "")
        return _result([_clip(1, stage="drying")], task_id=3)

    run(PlanRunner(plan.id, library=library, settings=settings, executor=recovered).run())
    stored = service.get_plan(plan.id)
    assert resumed == ["红薯烘干设备"]
    assert stored.items[0].progress.executed_queries == [
        "红薯热泵烘干",
        "红薯烘干设备",
    ]


def test_target_reached_behavior_unchanged(settings) -> None:
    library = build_library(settings)
    service, plan = _plan(
        settings, library, queries=["红薯热泵烘干", "红薯烘干设备"], candidates=8
    )
    _approve(service, plan)
    seen: list[str] = []

    async def executor(request):
        seen.append(request.query_seed or "")
        return _result([_clip(1, stage="drying")], task_id=1)

    outcome = run(
        PlanRunner(plan.id, library=library, settings=settings, executor=executor).run()
    )
    assert seen == ["红薯热泵烘干"]
    assert outcome.status is PlanStatus.COMPLETED


# ---------------------------------------------------------------------------
# Idempotent plan #18-style repair
# ---------------------------------------------------------------------------
def test_repair_plan_state_is_idempotent_and_preserves_history(settings) -> None:
    library = build_library(settings)
    service, plan = _plan(
        settings, library, queries=["红薯热泵烘干", "红薯烘干设备"], candidates=8
    )
    repo = service.repo
    item = service.get_plan(plan.id).items[0]
    item.progress.queries_attempted = 1
    item.progress.executed_queries = ["红薯热泵烘干"]
    item.progress.queries_remaining = 1
    item.progress.query_audit = [
        {
            "query": "红薯热泵烘干",
            "task_id": 97,
            "discovery_status": "login_required",
            "candidates": 0,
        }
    ]
    repo.save_progress(
        plan_id=plan.id,
        plan_progress=service.get_plan(plan.id).progress,
        item_id=item.id,
        item_progress=item.progress,
    )
    repo.set_status(
        plan.id,
        PlanStatus.PAUSED,
        pause_reason=PauseReason.HUMAN_VERIFICATION_REQUIRED,
    )
    repo.set_provider_state(
        plan.id,
        failure_class="account_blocking",
        subtype="quota_exhausted",
        ready=False,
        provider="qwen",
        model="qwen3-vl-plus-2025-09-23",
    )
    repo.log_event(plan.id, "task_completed", details={"task_id": 97})
    task_count_before = library.count_clips()
    task_id = library.create_task(TaskRequest(material="红薯干", target_clip_count=1))
    tasks_before = library.database.query_one("SELECT COUNT(*) n FROM tasks")["n"]
    sources_before = library.database.query_one(
        "SELECT COUNT(*) n FROM source_videos"
    )["n"]

    first = repair_plan_state(
        library,
        plan_id=plan.id,
        provider_name="qwen",
        provider_model="qwen-vl-max",
    )
    assert first["changed"] is True
    repaired = service.get_plan(plan.id)
    repaired_item = repaired.items[0]
    assert repaired_item.progress.executed_queries == []
    assert repaired_item.progress.queries_attempted == 1
    assert repaired_item.progress.query_audit[0]["completed"] is False
    assert repaired_item.progress.query_audit[0]["operational_blocker"] is True
    assert repaired.provider_ready is True
    assert repaired.provider_model == "qwen-vl-max"
    assert repaired.provider_failure_class == ""
    assert repaired.provider_failure_subtype == ""
    assert repaired.pause_reason == PauseReason.HUMAN_VERIFICATION_REQUIRED.value
    assert library.database.query_one("SELECT COUNT(*) n FROM tasks")["n"] == tasks_before
    assert (
        library.database.query_one("SELECT COUNT(*) n FROM source_videos")["n"]
        == sources_before
    )
    assert service.repo.get_plan(plan.id) is not None
    events = [event["event"] for event in repo.events(plan.id)]
    assert "task_completed" in events

    second = repair_plan_state(
        library,
        plan_id=plan.id,
        provider_name="qwen",
        provider_model="qwen-vl-max",
    )
    assert second["changed"] is False
    assert task_id is not None and task_count_before >= 0
