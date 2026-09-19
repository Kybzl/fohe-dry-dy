"""Idempotent M9.7.1 repair for the real plan #18 login-wall checkpoint.

Historical task #97 and plan timeline events are preserved.  The repair only
reopens the query that returned ``login_required`` and refreshes the current
provider state to the proven healthy routing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import load_settings  # noqa: E402
from core.dependencies import build_library, build_provider  # noqa: E402
from storage.plans import PlanRepository  # noqa: E402

PLAN_ID = 18
RETRYABLE_QUERY = "红薯热泵烘干"
OPERATIONAL_STATE = "login_required"


def repair_plan_state(
    library,
    *,
    plan_id: int = PLAN_ID,
    retryable_query: str = RETRYABLE_QUERY,
    operational_state: str = OPERATIONAL_STATE,
    provider_name: str,
    provider_model: str,
    apply: bool = True,
) -> dict:
    repo = PlanRepository(library.database)
    plan = repo.get_plan(plan_id)
    if plan is None:
        return {"ok": False, "reason": f"plan #{plan_id} not found"}

    item = next(
        (entry for entry in plan.items if entry.process_stage == "drying"),
        plan.items[0] if plan.items else None,
    )
    if item is None:
        return {"ok": False, "reason": f"plan #{plan_id} has no item"}

    before_executed = list(item.progress.executed_queries)
    query_in_executed = retryable_query in item.progress.executed_queries
    audit_changed = False
    for entry in item.progress.query_audit:
        if entry.get("query") != retryable_query or entry.get("task_id") != 97:
            continue
        if str(entry.get("discovery_status") or "") != operational_state:
            continue
        if entry.get("completed") is False and entry.get("operational_blocker") is True:
            continue
        entry["completed"] = False
        entry["operational_blocker"] = True
        entry["operational_state"] = operational_state
        entry["stop_reason"] = "operational_blocker"
        audit_changed = True

    provider_needs_update = not (
        plan.provider_ready
        and plan.provider_name == provider_name
        and plan.provider_model == provider_model
        and not plan.provider_failure_class
        and not plan.provider_failure_subtype
    )
    if not query_in_executed and not audit_changed and not provider_needs_update:
        return {
            "ok": True,
            "changed": False,
            "reason": "already repaired",
            "plan_id": plan_id,
            "query": retryable_query,
            "provider_ready": plan.provider_ready,
            "provider_model": plan.provider_model,
        }

    if apply:
        if query_in_executed:
            item.progress.executed_queries = [
                query
                for query in item.progress.executed_queries
                if query != retryable_query
            ]
        item.progress.queries_remaining = len(
            [
                query
                for query in item.queries
                if query.query not in set(item.progress.executed_queries)
            ]
        )
        repo.save_progress(
            plan_id=plan_id,
            plan_progress=plan.progress,
            item_id=item.id,
            item_progress=item.progress,
        )
        repo.set_provider_state(
            plan_id,
            failure_class="",
            subtype="",
            ready=True,
            provider=provider_name,
            model=provider_model,
            operation="readiness",
        )
        library.log_maintenance(
            "m9_7_1_plan18_repair",
            target_type="collection_plan",
            target_id=plan_id,
            details={
                "plan_id": plan_id,
                "task_id": 97,
                "query": retryable_query,
                "before_executed": before_executed,
                "after_executed": list(item.progress.executed_queries),
                "operational_state": operational_state,
                "provider_name": provider_name,
                "provider_model": provider_model,
                "provider_ready": True,
                "status": str(plan.status),
                "pause_reason": str(plan.pause_reason),
            },
        )
    return {
        "ok": True,
        "changed": True,
        "plan_id": plan_id,
        "task_id": 97,
        "query": retryable_query,
        "before_executed": before_executed,
        "after_executed": list(item.progress.executed_queries),
        "provider_ready": True,
        "provider_model": provider_model,
        "provider_name": provider_name,
        "pause_reason": str(plan.pause_reason),
    }


def run_repair(*, plan_id: int = PLAN_ID, apply: bool = True) -> dict:
    settings = load_settings()
    library = build_library(settings)
    provider = build_provider(settings, settings.ai.active_provider)
    return repair_plan_state(
        library,
        plan_id=plan_id,
        provider_name=str(getattr(provider, "name", settings.ai.active_provider)),
        provider_model=provider.model_for("preview_filter"),
        apply=apply,
    )


def main() -> int:
    result = run_repair(apply=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
