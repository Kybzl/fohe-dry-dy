"""SQLite repository for collection plans (Milestone 7, sections 38/39/40).

Kept separate from ``MaterialLibrary`` so the acquisition tables stay focused;
both share the same ``Database`` and therefore the same SQLite file.

All writes are small, independent transactions (section 50) - a plan runner
must never hold a transaction open across a browser search, AI call, download
or FFmpeg run.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Mapping

from core.models import utc_now
from core.plans import (
    CollectionPlan,
    CollectionPlanItem,
    PauseReason,
    PlanItemStatus,
    PlanProgress,
    PlanQuery,
    PlanStatus,
    QueryOrigin,
)
from storage.database import Database

LOGGER = logging.getLogger(__name__)


def _iso(moment: Any | None = None) -> str:
    return (moment or utc_now()).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):  # pragma: no cover - defensive
        return default
    return parsed


class PlanRepository:
    """CRUD + progress checkpoints for plans, items, links and events."""

    def __init__(self, database: Database) -> None:
        self.database = database

    # -- plans -------------------------------------------------------------
    def create_plan(
        self,
        *,
        name: str,
        library_category: str,
        status: PlanStatus = PlanStatus.DRAFT,
        count_mode: str = "all",
        created_from: str = "coverage_gap",
        coverage_before: Mapping[str, Any] | None = None,
        created_at: Any | None = None,
    ) -> int:
        now = _iso(created_at)
        plan_id = self.database.insert(
            "collection_plans",
            {
                "name": name,
                "status": str(status),
                "library_category": library_category,
                "count_mode": count_mode,
                "created_from": created_from,
                "target_final_clips": 0,
                "max_preview_candidates": 0,
                "max_downloads": 0,
                "max_ai_tokens": 0,
                "max_runtime_minutes": 0.0,
                "coverage_before_json": _json(coverage_before or {}),
                "coverage_after_json": None,
                "progress_json": _json(PlanProgress().model_dump(mode="json")),
                "pause_reason": "",
                "approval_note": "",
                "created_at": now,
                "updated_at": now,
                "approved_at": None,
                "started_at": None,
                "finished_at": None,
            },
        )
        LOGGER.info("collection plan #%s created (%s)", plan_id, library_category)
        return plan_id

    def add_item(self, plan_id: int, item: CollectionPlanItem) -> int:
        now = _iso()
        return self.database.insert(
            "collection_plan_items",
            {
                "plan_id": plan_id,
                "process_stage": item.process_stage,
                "current_count": int(item.current_count),
                "target_count": int(item.target_count),
                "gap": int(item.gap),
                "requested_clips": int(item.requested_clips),
                "priority": item.priority,
                "queries_json": _json([query.as_dict() for query in item.queries]),
                "max_candidates": int(item.max_candidates),
                "max_downloads": int(item.max_downloads),
                "max_tokens": int(item.max_tokens),
                "progress_json": _json(item.progress.model_dump(mode="json")),
                "status": str(item.status),
                "created_at": now,
                "updated_at": now,
            },
        )

    def update_plan_budget(
        self,
        plan_id: int,
        *,
        target_final_clips: int,
        max_preview_candidates: int,
        max_downloads: int,
        max_ai_tokens: int,
        max_runtime_minutes: float,
    ) -> None:
        self.database.execute(
            "UPDATE collection_plans SET target_final_clips = ?, max_preview_candidates = ?, "
            "max_downloads = ?, max_ai_tokens = ?, max_runtime_minutes = ?, updated_at = ? "
            "WHERE id = ?",
            (
                int(target_final_clips),
                int(max_preview_candidates),
                int(max_downloads),
                int(max_ai_tokens),
                float(max_runtime_minutes),
                _iso(),
                plan_id,
            ),
        )

    def update_item(
        self,
        item_id: int,
        *,
        requested_clips: int | None = None,
        queries: Iterable[PlanQuery] | None = None,
        max_candidates: int | None = None,
        max_downloads: int | None = None,
        max_tokens: int | None = None,
        priority: str | None = None,
    ) -> None:
        assignments: list[str] = []
        params: list[Any] = []
        if requested_clips is not None:
            assignments.append("requested_clips = ?")
            params.append(int(requested_clips))
        if queries is not None:
            assignments.append("queries_json = ?")
            params.append(_json([query.as_dict() for query in queries]))
        if max_candidates is not None:
            assignments.append("max_candidates = ?")
            params.append(int(max_candidates))
        if max_downloads is not None:
            assignments.append("max_downloads = ?")
            params.append(int(max_downloads))
        if max_tokens is not None:
            assignments.append("max_tokens = ?")
            params.append(int(max_tokens))
        if priority is not None:
            assignments.append("priority = ?")
            params.append(str(priority))
        if not assignments:
            return
        assignments.append("updated_at = ?")
        params.append(_iso())
        params.append(int(item_id))
        self.database.execute(
            f"UPDATE collection_plan_items SET {', '.join(assignments)} WHERE id = ?", params
        )

    def set_status(
        self,
        plan_id: int,
        status: PlanStatus,
        *,
        pause_reason: PauseReason | str = "",
        mark_approved: bool = False,
        approval_note: str | None = None,
        mark_started: bool = False,
        mark_finished: bool = False,
    ) -> None:
        assignments = ["status = ?", "pause_reason = ?", "updated_at = ?"]
        params: list[Any] = [str(status), str(pause_reason), _iso()]
        if mark_approved:
            assignments.append("approved_at = ?")
            params.append(_iso())
        if approval_note is not None:
            assignments.append("approval_note = ?")
            params.append(approval_note)
        if mark_started:
            assignments.append("started_at = COALESCE(started_at, ?)")
            params.append(_iso())
        if mark_finished:
            assignments.append("finished_at = ?")
            params.append(_iso())
        params.append(plan_id)
        self.database.execute(
            f"UPDATE collection_plans SET {', '.join(assignments)} WHERE id = ?", params
        )

    def set_provider_state(
        self,
        plan_id: int,
        *,
        failure_class: str = "",
        subtype: str = "",
        ready: bool | None = None,
        provider: str = "",
        model: str = "",
        operation: str = "",
        checked_at: str | None = None,
    ) -> None:
        """Persist current provider evidence while keeping history untouched."""

        assignments = [
            "provider_failure_class = ?",
            "provider_failure_subtype = ?",
            "provider_model = ?",
            "provider_operation = ?",
            "provider_checked_at = ?",
            "updated_at = ?",
        ]
        params: list[Any] = [
            str(failure_class or ""),
            str(subtype or ""),
            str(model or ""),
            str(operation or ""),
            checked_at or _iso(),
            _iso(),
        ]
        if ready is not None:
            assignments.append("provider_ready = ?")
            params.append(1 if ready else 0)
        if provider:
            assignments.append("provider_name = ?")
            params.append(str(provider))
        params.append(int(plan_id))
        self.database.execute(
            f"UPDATE collection_plans SET {', '.join(assignments)} WHERE id = ?",
            params,
        )

    def set_item_status(self, item_id: int, status: PlanItemStatus) -> None:
        self.database.execute(
            "UPDATE collection_plan_items SET status = ?, updated_at = ? WHERE id = ?",
            (str(status), _iso(), int(item_id)),
        )

    def set_flags(
        self,
        plan_id: int,
        *,
        archived: bool | None = None,
        test_plan: bool | None = None,
    ) -> None:
        """Operator archive / acceptance-test markers (Milestone 8 sections 29/30).

        Flags never delete anything: an archived plan stays fully auditable and
        only disappears from the default (active) listing.
        """

        assignments: list[str] = []
        params: list[Any] = []
        if archived is not None:
            assignments.append("archived = ?")
            params.append(1 if archived else 0)
        if test_plan is not None:
            assignments.append("test_plan = ?")
            params.append(1 if test_plan else 0)
        if not assignments:
            return
        assignments.append("updated_at = ?")
        params.extend([_iso(), int(plan_id)])
        self.database.execute(
            f"UPDATE collection_plans SET {', '.join(assignments)} WHERE id = ?", params
        )

    def save_progress(
        self,
        *,
        plan_id: int,
        plan_progress: PlanProgress,
        item_id: int | None = None,
        item_progress: PlanProgress | None = None,
    ) -> None:
        """Small checkpoint write (sections 50/51)."""

        self.database.execute(
            "UPDATE collection_plans SET progress_json = ?, updated_at = ? WHERE id = ?",
            (_json(plan_progress.model_dump(mode="json")), _iso(), int(plan_id)),
        )
        if item_id is not None and item_progress is not None:
            self.database.execute(
                "UPDATE collection_plan_items SET progress_json = ?, updated_at = ? WHERE id = ?",
                (_json(item_progress.model_dump(mode="json")), _iso(), int(item_id)),
            )

    def save_coverage(self, plan_id: int, *, before: Any = None, after: Any = None) -> None:
        assignments: list[str] = []
        params: list[Any] = []
        if before is not None:
            assignments.append("coverage_before_json = ?")
            params.append(_json(before))
        if after is not None:
            assignments.append("coverage_after_json = ?")
            params.append(_json(after))
        if not assignments:
            return
        assignments.append("updated_at = ?")
        params.extend([_iso(), plan_id])
        self.database.execute(
            f"UPDATE collection_plans SET {', '.join(assignments)} WHERE id = ?", params
        )

    # -- links + events ----------------------------------------------------
    def link_task(self, *, plan_id: int, plan_item_id: int, task_id: int, query: str = "") -> int:
        return self.database.insert(
            "collection_plan_tasks",
            {
                "plan_id": int(plan_id),
                "plan_item_id": int(plan_item_id),
                "task_id": int(task_id),
                "query": query,
                "created_at": _iso(),
            },
        )

    def linked_task_ids(self, plan_id: int) -> list[int]:
        rows = self.database.query(
            "SELECT task_id FROM collection_plan_tasks WHERE plan_id = ? ORDER BY id",
            (int(plan_id),),
        )
        return [int(row["task_id"]) for row in rows]

    def item_task_ids(self, item_id: int) -> list[int]:
        rows = self.database.query(
            "SELECT task_id FROM collection_plan_tasks WHERE plan_item_id = ? ORDER BY id",
            (int(item_id),),
        )
        return [int(row["task_id"]) for row in rows]

    def log_event(
        self,
        plan_id: int,
        event: str,
        *,
        plan_item_id: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> int:
        return self.database.insert(
            "collection_plan_events",
            {
                "plan_id": int(plan_id),
                "plan_item_id": int(plan_item_id) if plan_item_id is not None else None,
                "event": event,
                "details_json": _json(details or {}),
                "created_at": _iso(),
            },
        )

    def events(self, plan_id: int, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.database.query(
            "SELECT * FROM collection_plan_events WHERE plan_id = ? ORDER BY id LIMIT ?",
            (int(plan_id), int(limit)),
        )
        return [
            {
                "id": int(row["id"]),
                "plan_item_id": row["plan_item_id"],
                "event": row["event"],
                "details": _loads(row["details_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # -- reading -----------------------------------------------------------
    def get_plan(self, plan_id: int) -> CollectionPlan | None:
        row = self.database.query_one(
            "SELECT * FROM collection_plans WHERE id = ?", (int(plan_id),)
        )
        if row is None:
            return None
        items = self.items_for_plan(int(plan_id))
        return CollectionPlan(
            id=int(row["id"]),
            name=row["name"],
            status=PlanStatus(row["status"]),
            library_category=row["library_category"],
            count_mode=row["count_mode"] or "all",
            created_from=row["created_from"] or "",
            target_final_clips=int(row["target_final_clips"] or 0),
            max_preview_candidates=int(row["max_preview_candidates"] or 0),
            max_downloads=int(row["max_downloads"] or 0),
            max_ai_tokens=int(row["max_ai_tokens"] or 0),
            max_runtime_minutes=float(row["max_runtime_minutes"] or 0.0),
            coverage_before=_loads(row["coverage_before_json"], {}),
            coverage_after=_loads(row["coverage_after_json"], {}),
            progress=PlanProgress.model_validate(_loads(row["progress_json"], {})),
            pause_reason=row["pause_reason"] or "",
            provider_failure_class=row["provider_failure_class"] or "",
            provider_failure_subtype=row["provider_failure_subtype"] or "",
            provider_ready=bool(row["provider_ready"]),
            provider_name=row["provider_name"] or "",
            provider_model=row["provider_model"] or "",
            provider_operation=row["provider_operation"] or "",
            provider_checked_at=row["provider_checked_at"] or "",
            approval_note=row["approval_note"] or "",
            archived=bool(row["archived"]),
            test_plan=bool(row["test_plan"]),
            items=items,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            approved_at=row["approved_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    def items_for_plan(self, plan_id: int) -> list[CollectionPlanItem]:
        rows = self.database.query(
            "SELECT * FROM collection_plan_items WHERE plan_id = ? ORDER BY id",
            (int(plan_id),),
        )
        return [self._row_to_item(row) for row in rows]

    def get_item(self, item_id: int) -> CollectionPlanItem | None:
        row = self.database.query_one(
            "SELECT * FROM collection_plan_items WHERE id = ?", (int(item_id),)
        )
        return self._row_to_item(row) if row else None

    @staticmethod
    def _row_to_item(row: Any) -> CollectionPlanItem:
        queries = [
            PlanQuery.model_validate(entry)
            for entry in _loads(row["queries_json"], [])
            if isinstance(entry, dict)
        ]
        return CollectionPlanItem(
            id=int(row["id"]),
            plan_id=int(row["plan_id"]),
            process_stage=row["process_stage"],
            current_count=int(row["current_count"] or 0),
            target_count=int(row["target_count"] or 0),
            gap=int(row["gap"] or 0),
            requested_clips=int(row["requested_clips"] or 0),
            priority=row["priority"] or "medium",
            queries=queries,
            max_candidates=int(row["max_candidates"] or 0),
            max_downloads=int(row["max_downloads"] or 0),
            max_tokens=int(row["max_tokens"] or 0),
            progress=PlanProgress.model_validate(_loads(row["progress_json"], {})),
            status=PlanItemStatus(row["status"] or "pending"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_plans(
        self,
        *,
        statuses: Iterable[PlanStatus] | None = None,
        library_category: str | None = None,
        include_archived: bool = False,
        archived_only: bool = False,
        limit: int = 50,
    ) -> list[CollectionPlan]:
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(str(status) for status in statuses)
        if library_category:
            clauses.append("library_category = ?")
            params.append(library_category)
        if archived_only:
            clauses.append("archived = 1")
        elif not include_archived:
            clauses.append("COALESCE(archived, 0) = 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        rows = self.database.query(
            f"SELECT id FROM collection_plans {where} ORDER BY id DESC LIMIT ?", params
        )
        plans: list[CollectionPlan] = []
        for row in rows:
            plan = self.get_plan(int(row["id"]))
            if plan is not None:
                plans.append(plan)
        return plans

    def delete_item(self, item_id: int) -> int:
        """Draft-only structural edit: remove one objective."""

        return self.database.execute(
            "DELETE FROM collection_plan_items WHERE id = ?", (int(item_id),)
        )

    def normalize_interrupted_plans(self) -> list[int]:
        """Plans left ``running`` by a crash become paused with a reason.

        Called on startup; nothing is resumed automatically (section 24).
        """

        rows = self.database.query(
            "SELECT id FROM collection_plans WHERE status = ?", (str(PlanStatus.RUNNING),)
        )
        ids = [int(row["id"]) for row in rows]
        for plan_id in ids:
            self.set_status(
                plan_id,
                PlanStatus.PAUSED,
                pause_reason=PauseReason.INTERRUPTED,
            )
            self.log_event(
                plan_id,
                "interrupted",
                details={"reason": "process restart while running"},
            )
        if ids:
            LOGGER.warning("normalized %s interrupted plan(s) to paused: %s", len(ids), ids)
        return ids
