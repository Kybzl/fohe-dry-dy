"""Plan lifecycle for the CLI and the Gradio tab (Milestone 7, sections 11-23/42).

Thin orchestration on top of :class:`~core.planner.Planner`,
:class:`~storage.plans.PlanRepository` and :class:`~core.plan_runner.PlanRunner`:

* create a draft from the real coverage gaps
* edit a draft (validated fields only)
* approve / pause / resume / cancel with an audit event
* report the plan, its estimate and its effectiveness

No acquisition logic lives here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from core.config import AppSettings
from core.planner import Planner
from core.plans import (
    EDITABLE_PLAN_STATUSES,
    CollectionPlan,
    CollectionPlanItem,
    PauseReason,
    PlanEstimate,
    PlanQuery,
    PlanStatus,
    QueryOrigin,
    pause_reason_label,
)
from storage.library import MaterialLibrary
from storage.plans import PlanRepository

LOGGER = logging.getLogger(__name__)


@dataclass
class PlanAction:
    """Result of one lifecycle action."""

    ok: bool
    message: str
    plan_id: int | None = None
    status: PlanStatus | None = None


class PlanService:
    """Create, edit, approve and control plans."""

    def __init__(
        self,
        library: MaterialLibrary,
        settings: AppSettings,
        *,
        repository: PlanRepository | None = None,
    ) -> None:
        self.library = library
        self.settings = settings
        self.repo = repository or PlanRepository(library.database)
        self.planner = Planner(library, settings, repository=self.repo)

    # -- creation ----------------------------------------------------------
    def create_plan(
        self,
        category: str,
        *,
        count_mode: str | None = None,
        include_healthy: bool = False,
        name: str | None = None,
        created_from: str = "coverage_gap",
    ) -> tuple[CollectionPlan | None, PlanAction]:
        """Generate a **draft** plan from the real coverage gaps."""

        draft = self.planner.build_plan(
            category,
            count_mode=count_mode,
            include_healthy=include_healthy,
            name=name,
            created_from=created_from,
        )
        return self.save_draft(draft, category=category)

    def save_draft(
        self, draft: CollectionPlan, *, category: str = ""
    ) -> tuple[CollectionPlan | None, PlanAction]:
        """Persist an already-built draft plan (Milestone 9 production plans).

        The validation and persistence path is shared with
        :meth:`create_plan`, so a production plan behaves exactly like any
        other draft: it still needs explicit human approval before it can run.
        """

        problems = self.planner.validate(draft)
        if problems:
            return None, PlanAction(
                ok=False, message="计划生成失败: " + "；".join(problems)
            )
        plan_id = self.repo.create_plan(
            name=draft.name,
            library_category=draft.library_category,
            status=PlanStatus.DRAFT,
            count_mode=draft.count_mode,
            created_from=draft.created_from,
            coverage_before=draft.coverage_before,
        )
        for item in draft.items:
            item_id = self.repo.add_item(plan_id, item)
            item.id = item_id
            item.plan_id = plan_id
        self.repo.update_plan_budget(
            plan_id,
            target_final_clips=draft.target_final_clips,
            max_preview_candidates=draft.max_preview_candidates,
            max_downloads=draft.max_downloads,
            max_ai_tokens=draft.max_ai_tokens,
            max_runtime_minutes=draft.max_runtime_minutes,
        )
        self.repo.log_event(
            plan_id,
            "created",
            details={
                "category": category or draft.library_category,
                "created_from": draft.created_from,
                "items": len(draft.items),
                "target": draft.target_final_clips,
                "count_mode": draft.count_mode,
            },
        )
        return self.repo.get_plan(plan_id), PlanAction(
            ok=True,
            message=(
                f"已创建草稿计划 #{plan_id}（{draft.library_category}，"
                f"{len(draft.items)} 个目标，共 {draft.target_final_clips} 片段）"
                "；批准前不会执行任何采集。"
            ),
            plan_id=plan_id,
            status=PlanStatus.DRAFT,
        )

    # -- reading -----------------------------------------------------------
    def get_plan(self, plan_id: int) -> CollectionPlan | None:
        return self.repo.get_plan(plan_id)

    def list_plans(self, *, limit: int = 50) -> list[CollectionPlan]:
        return self.repo.list_plans(limit=limit)

    def estimate(self, plan: CollectionPlan) -> PlanEstimate:
        return self.planner.estimate(plan)

    def validate(self, plan: CollectionPlan) -> list[str]:
        return self.planner.validate(plan)

    def effectiveness(self, plan: CollectionPlan) -> dict[str, Any]:
        return self.planner.effectiveness(plan).model_dump(mode="json")

    def normalize_interrupted(self) -> list[int]:
        return self.repo.normalize_interrupted_plans()

    # -- operator marks (sections 29/30) -----------------------------------
    def archive(self, plan_id: int, *, archived: bool = True) -> PlanAction:
        """Hide a plan from the active list while keeping it fully auditable."""

        plan = self.repo.get_plan(plan_id)
        if plan is None:
            return PlanAction(ok=False, message=f"计划 #{plan_id} 不存在")
        self.repo.set_flags(plan_id, archived=archived)
        self.repo.log_event(
            plan_id,
            "archived" if archived else "unarchived",
            details={"status": str(plan.status), "test_plan": plan.test_plan},
        )
        return PlanAction(
            ok=True,
            message=(
                f"计划 #{plan_id} 已{'归档' if archived else '取消归档'}"
                "（记录与任务关联保留，归档计划不可执行）"
            ),
            plan_id=plan_id,
            status=plan.status,
        )

    def mark_test_plan(self, plan_id: int, *, flag: bool = True) -> PlanAction:
        """Mark a plan as an acceptance/stub plan (section 29)."""

        plan = self.repo.get_plan(plan_id)
        if plan is None:
            return PlanAction(ok=False, message=f"计划 #{plan_id} 不存在")
        self.repo.set_flags(plan_id, test_plan=flag)
        self.repo.log_event(
            plan_id,
            "marked_test_plan" if flag else "unmarked_test_plan",
            details={"status": str(plan.status)},
        )
        return PlanAction(
            ok=True,
            message=f"计划 #{plan_id} 已标记为{'验收/测试计划' if flag else '正式计划'}",
            plan_id=plan_id,
            status=plan.status,
        )

    def regenerate_queries(self, plan_id: int, item_id: int) -> PlanAction:
        """Draft-only: re-rank the query list with the current ranking version."""

        plan = self.repo.get_plan(plan_id)
        if plan is None:
            return PlanAction(ok=False, message=f"计划 #{plan_id} 不存在")
        if plan.status not in EDITABLE_PLAN_STATUSES:
            return PlanAction(
                ok=False,
                message=f"计划状态为 {plan.status}，只有草稿可以重新生成查询词",
                plan_id=plan_id,
                status=plan.status,
            )
        item = next((entry for entry in plan.items if entry.id == item_id), None)
        if item is None:
            return PlanAction(ok=False, message=f"目标 #{item_id} 不存在", plan_id=plan_id)
        version = self.settings.collection_planning.ranking_version
        queries = self.planner.rank_queries(
            plan.library_category,
            item.process_stage,
            limit=max(2, len(item.queries) or 4),
            version=version,
        )
        if not queries:
            return PlanAction(ok=False, message="没有可用的查询词", plan_id=plan_id)
        self.repo.update_item(item_id, queries=queries)
        self.repo.log_event(
            plan_id,
            "queries_regenerated",
            plan_item_id=item_id,
            details={"version": version, "queries": [query.query for query in queries]},
        )
        return PlanAction(
            ok=True,
            message=f"目标 #{item_id} 已用 {version} 重新生成查询词（{len(queries)} 个）",
            plan_id=plan_id,
            status=plan.status,
        )

    # -- editing (sections 19/20) -----------------------------------------
    def edit_item(
        self,
        plan_id: int,
        item_id: int,
        *,
        requested_clips: int | None = None,
        max_candidates: int | None = None,
        max_downloads: int | None = None,
        max_tokens: int | None = None,
        priority: str | None = None,
        queries: Sequence[str] | None = None,
    ) -> PlanAction:
        """Draft-only structural edit; every value is validated."""

        plan = self.repo.get_plan(plan_id)
        if plan is None:
            return PlanAction(ok=False, message=f"计划 #{plan_id} 不存在")
        if plan.status not in EDITABLE_PLAN_STATUSES:
            return PlanAction(
                ok=False,
                message=f"计划状态为 {plan.status}，只有草稿可以编辑",
                plan_id=plan_id,
                status=plan.status,
            )
        item = next((entry for entry in plan.items if entry.id == item_id), None)
        if item is None:
            return PlanAction(ok=False, message=f"目标 #{item_id} 不存在", plan_id=plan_id)

        p = self.settings.collection_planning
        problems: list[str] = []
        if requested_clips is not None:
            if requested_clips <= 0:
                problems.append("requested_clips 必须 > 0")
            if requested_clips > p.max_requested_clips_per_stage:
                problems.append(
                    f"requested_clips 不能超过 {p.max_requested_clips_per_stage}"
                )
        for label, value, limit in (
            ("max_candidates", max_candidates, p.max_plan_previews),
            ("max_downloads", max_downloads, p.max_plan_downloads),
            ("max_tokens", max_tokens, p.max_plan_ai_tokens),
        ):
            if value is not None and (value <= 0 or value > limit):
                problems.append(f"{label} 必须在 1..{limit} 之间")
        if priority is not None and priority not in {"critical", "high", "medium", "healthy"}:
            problems.append(f"未知优先级 {priority!r}")
        plan_queries = None
        if queries is not None:
            cleaned = [str(query).strip() for query in queries if str(query).strip()]
            if not cleaned:
                problems.append("查询词不能为空")
            elif len(cleaned) > 8:
                problems.append("单个目标最多 8 个查询词")
            else:
                plan_queries = [
                    PlanQuery(query=query, origin=QueryOrigin.GENERATED_TEMPLATE,
                              evidence=["操作者手动编辑"])
                    for query in cleaned
                ]
        if problems:
            return PlanAction(ok=False, message="；".join(problems), plan_id=plan_id)

        self.repo.update_item(
            item_id,
            requested_clips=requested_clips,
            queries=plan_queries,
            max_candidates=max_candidates,
            max_downloads=max_downloads,
            max_tokens=max_tokens,
            priority=priority,
        )
        # keep the plan level budgets consistent with the edited items
        self.sync_budgets(plan_id)
        self.repo.log_event(
            plan_id,
            "edited",
            plan_item_id=item_id,
            details={
                "requested_clips": requested_clips,
                "max_candidates": max_candidates,
                "max_downloads": max_downloads,
                "max_tokens": max_tokens,
                "priority": priority,
                "queries": list(queries) if queries else None,
            },
        )
        return PlanAction(
            ok=True, message=f"已更新目标 #{item_id}", plan_id=plan_id, status=plan.status
        )

    def sync_budgets(self, plan_id: int) -> CollectionPlan | None:
        """Re-distribute the item budgets inside the plan ceilings and persist.

        An edit may push the item budgets past the plan budget; the plan
        ceilings win and the scaled values are written back to every item
        (section 9).
        """

        plan = self.repo.get_plan(plan_id)
        if plan is None:
            return None
        self.planner.apply_budget_defaults(plan)
        for item in plan.items:
            self.repo.update_item(
                item.id or 0,
                max_candidates=item.max_candidates,
                max_downloads=item.max_downloads,
                max_tokens=item.max_tokens,
            )
        self.repo.update_plan_budget(
            plan_id,
            target_final_clips=plan.target_final_clips,
            max_preview_candidates=plan.max_preview_candidates,
            max_downloads=plan.max_downloads,
            max_ai_tokens=plan.max_ai_tokens,
            max_runtime_minutes=plan.max_runtime_minutes,
        )
        return self.repo.get_plan(plan_id)

    # -- lifecycle (sections 11/21/22/23) ---------------------------------
    def approve(self, plan_id: int, *, note: str = "") -> PlanAction:
        plan = self.repo.get_plan(plan_id)
        if plan is None:
            return PlanAction(ok=False, message=f"计划 #{plan_id} 不存在")
        if plan.status is not PlanStatus.DRAFT:
            return PlanAction(
                ok=False,
                message=f"计划状态为 {plan.status}，只有草稿需要批准",
                plan_id=plan_id,
                status=plan.status,
            )
        problems = self.planner.validate(plan)
        if problems:
            return PlanAction(
                ok=False, message="计划未通过校验: " + "；".join(problems), plan_id=plan_id
            )
        self.repo.set_status(
            plan_id,
            PlanStatus.APPROVED,
            mark_approved=True,
            approval_note=note,
        )
        self.repo.log_event(plan_id, "approved", details={"note": note})
        return PlanAction(
            ok=True,
            message=f"计划 #{plan_id} 已批准（人工作业授权），可以执行",
            plan_id=plan_id,
            status=PlanStatus.APPROVED,
        )

    def pause(self, plan_id: int, *, reason: PauseReason = PauseReason.OPERATOR) -> PlanAction:
        plan = self.repo.get_plan(plan_id)
        if plan is None:
            return PlanAction(ok=False, message=f"计划 #{plan_id} 不存在")
        if plan.status is not PlanStatus.RUNNING:
            return PlanAction(
                ok=False,
                message=f"计划状态为 {plan.status}，只有运行中的计划可以暂停",
                plan_id=plan_id,
                status=plan.status,
            )
        self.repo.set_status(plan_id, PlanStatus.PAUSED, pause_reason=reason)
        self.repo.log_event(plan_id, "paused", details={"reason": str(reason)})
        return PlanAction(
            ok=True,
            message=f"计划 #{plan_id} 已请求暂停（当前任务完成后停止）",
            plan_id=plan_id,
            status=PlanStatus.PAUSED,
        )

    def resume(self, plan_id: int) -> PlanAction:
        plan = self.repo.get_plan(plan_id)
        if plan is None:
            return PlanAction(ok=False, message=f"计划 #{plan_id} 不存在")
        if plan.status is not PlanStatus.PAUSED:
            return PlanAction(
                ok=False,
                message=f"计划状态为 {plan.status}，只有暂停的计划可以恢复",
                plan_id=plan_id,
                status=plan.status,
            )
        if plan.approved_at is None:
            return PlanAction(ok=False, message="计划未批准，不能恢复执行", plan_id=plan_id)
        self.repo.set_status(plan_id, PlanStatus.RUNNING, pause_reason=PauseReason.NONE)
        self.repo.log_event(plan_id, "resumed", details={"from": str(plan.pause_reason)})
        return PlanAction(
            ok=True,
            message=f"计划 #{plan_id} 已恢复运行",
            plan_id=plan_id,
            status=PlanStatus.RUNNING,
        )

    def cancel(self, plan_id: int) -> PlanAction:
        plan = self.repo.get_plan(plan_id)
        if plan is None:
            return PlanAction(ok=False, message=f"计划 #{plan_id} 不存在")
        if plan.status in (PlanStatus.COMPLETED, PlanStatus.CANCELLED):
            return PlanAction(
                ok=False,
                message=f"计划状态为 {plan.status}，无需取消",
                plan_id=plan_id,
                status=plan.status,
            )
        self.repo.set_status(
            plan_id, PlanStatus.CANCELLED, pause_reason=PauseReason.NONE, mark_finished=True
        )
        self.repo.log_event(plan_id, "cancelled", details={"previous": str(plan.status)})
        return PlanAction(
            ok=True,
            message=(
                f"计划 #{plan_id} 已取消；已产出的片段与任务记录保留在素材库/任务表"
            ),
            plan_id=plan_id,
            status=PlanStatus.CANCELLED,
        )

    # -- reporting ---------------------------------------------------------
    @staticmethod
    def budget_progress_text(used: int, limit: int, *, width: int = 10) -> str:
        """``3 / 8 [####------]`` - text is enough, no dashboard (section 26)."""

        limit = int(limit or 0)
        used = int(used or 0)
        if limit <= 0:
            return f"{used} / - "
        ratio = min(1.0, max(0.0, used / limit))
        filled = int(round(ratio * width))
        bar = "#" * filled + "-" * (width - filled)
        return f"{used:,} / {limit:,}  [{bar}] {ratio:>4.0%}"

    def pause_reason_text(self, plan: CollectionPlan) -> str:
        """Human readable pause reason, raw code kept in brackets (section 27)."""

        raw = str(plan.pause_reason or "").strip()
        label = pause_reason_label(raw, fallback="未暂停")
        if not raw:
            return label
        if raw == "provider_unavailable" and str(plan.provider_failure_subtype or ""):
            return (
                f"{label} ({raw} / {plan.provider_failure_subtype}，"
                f"model={plan.provider_model or '-'})"
            )
        return f"{label} ({raw})"

    def current_query(self, item: CollectionPlanItem) -> PlanQuery | None:
        """The in-flight (running) or next (paused) query of one item."""

        executed = set(item.progress.executed_queries)
        for query in item.queries:
            if query.query in executed:
                continue
            if query.query_saturation == "saturated":
                continue
            return query
        return None

    def status_lines(self, plan: CollectionPlan) -> list[str]:
        """Live operator status: objective, current query, budgets (sections 25/26)."""

        progress = plan.progress
        qualifying = sum(item.progress.qualifying_clips for item in plan.items)
        off_target = max(0, progress.clips_saved - qualifying)
        running = plan.status is PlanStatus.RUNNING
        lines = [
            f"状态: {plan.status} | 暂停原因: {self.pause_reason_text(plan)}",
            *(
                [
                    f"AI provider: {plan.provider_failure_class or '-'} | "
                    f"subtype: {plan.provider_failure_subtype or '-'} | "
                    f"model: {plan.provider_model or '-'}",
                    f"最近 provider 检查: {plan.provider_checked_at or '-'} | "
                    f"恢复方式: 修复 provider 后执行 --run-collection-plan {plan.id}",
                ]
                if str(plan.pause_reason or "") == "provider_unavailable"
                else [
                    f"AI provider: ready={bool(plan.provider_ready)} | "
                    f"provider={plan.provider_name or '-'} | "
                    f"model={plan.provider_model or '-'} | "
                    f"checked_at={plan.provider_checked_at or '-'}"
                ]
                if plan.provider_ready or plan.provider_model
                else []
            ),
            f"素材分类: {plan.library_category} | 计划查询词版本: "
            f"{self.plan_ranking_version(plan)}",
            "目标:",
        ]
        for item in plan.sorted_items():
            query = self.current_query(item)
            if query is not None:
                position = item.queries.index(query) + 1
                query_text = f"{query.query}（第 {position}/{len(item.queries)} 条）"
            else:
                query_text = (
                    f"无（已完成 {len(item.progress.executed_queries)}/"
                    f"{len(item.queries)} 条）"
                )
            label = "当前查询" if running else "下一条查询"
            lines.append(
                f"  {item.process_stage:<18}[{item.priority}] 命中 {item.progress.qualifying_clips}"
                f"/{item.requested_clips} [{item.status}]"
            )
            lines.append(f"      {label}: {query_text}")
        lines.extend(
            [
                "预算进度:",
                f"  previews : {self.budget_progress_text(progress.preview_calls, plan.max_preview_candidates)}",
                f"  downloads: {self.budget_progress_text(progress.downloads, plan.max_downloads)}",
                f"  tokens   : {self.budget_progress_text(progress.ai_tokens, plan.max_ai_tokens)}",
                f"  候选: {progress.candidates_seen}（唯一 {progress.unique_candidates}）| "
                f"目标命中片段: {qualifying} | 非目标有效片段: {off_target} | "
                f"AI 调用: {progress.ai_calls}",
            ]
        )
        if progress.stage_breakdown:
            lines.append(f"  实际工序分布: {progress.stage_breakdown}")
        if plan.archived or plan.test_plan:
            marks = []
            if plan.archived:
                marks.append("已归档")
            if plan.test_plan:
                marks.append("验收/测试计划")
            lines.append("标记: " + "，".join(marks))
        return lines

    @staticmethod
    def plan_ranking_version(plan: CollectionPlan) -> str:
        """Which ranking version produced the stored query list (section 19)."""

        versions = [
            query.rank_version
            for item in plan.items
            for query in item.queries
            if query.rank_version
        ]
        if not versions:
            return "未记录（历史计划）"
        unique = sorted(set(versions))
        return unique[0] if len(unique) == 1 else " / ".join(unique)

    def timeline_lines(self, plan: CollectionPlan, *, limit: int = 60) -> list[str]:
        """Concise execution timeline built from ``collection_plan_events`` (§24)."""

        if plan.id is None:
            return []
        events = self.repo.events(plan.id, limit=limit)
        if not events:
            return ["（还没有事件记录）"]
        stages = {item.id: item.process_stage for item in plan.items}
        total_queries = {item.id: len(item.queries) for item in plan.items}
        item_by_id = {item.id: item for item in plan.items}
        seen_queries: dict[int, list[str]] = {}
        lines: list[str] = []
        for event in events:
            stamp = str(event.get("created_at") or "")[11:19] or "--:--:--"
            name = str(event.get("event") or "")
            details = event.get("details") or {}
            # the runner records the item in the event details; the dedicated
            # column is used by the lifecycle actions of the service
            item_id = event.get("plan_item_id")
            if item_id is None:
                item_id = details.get("item_id")
            stage = stages.get(item_id, "")
            text = name
            if name == "created":
                text = (
                    f"计划创建（{details.get('category', plan.library_category)}，"
                    f"{details.get('items', len(plan.items))} 个目标，"
                    f"目标 {details.get('target', plan.target_final_clips)} 片段）"
                )
            elif name == "edited":
                changed = [k for k, v in details.items() if v not in (None, [])]
                text = f"操作者编辑目标 {stage}（{', '.join(changed) or '无变化'}）"
            elif name == "approved":
                note = details.get("note") or ""
                text = "人工批准" + (f"（{note}）" if note else "")
            elif name == "started":
                text = "开始执行"
            elif name == "item_started":
                text = f"目标 {stage} 开始"
            elif name == "query_started":
                query_text = str(details.get("query", "") or "")
                history = seen_queries.setdefault(item_id, [])
                item = item_by_id.get(item_id)
                position = next(
                    (
                        index + 1
                        for index, query in enumerate(item.queries)
                        if query.query == query_text
                    ),
                    len(history) + 1,
                ) if item is not None else len(history) + 1
                retry = "（重试）" if query_text in history else ""
                history.append(query_text)
                total = total_queries.get(item_id) or position
                text = f"查询 {position}/{total}「{query_text}」{retry}"
            elif name == "task_completed":
                text = (
                    f"任务 #{details.get('task_id')} 完成：片段 +{details.get('clips', 0)}，"
                    f"目标命中 +{details.get('qualifying', 0)}，tokens {details.get('tokens', 0)}"
                )
            elif name == "task_failed":
                text = f"查询「{details.get('query', '')}」执行失败"
            elif name == "item_satisfied":
                text = f"目标 {stage} 已达成（objective reached）"
            elif name == "item_exhausted":
                text = f"目标 {stage} 的查询已用完"
            elif name == "budget_warning":
                text = (
                    f"预算用尽暂停：{pause_reason_label(details.get('reason', ''))}"
                    f" [{details.get('reason', '')}]"
                )
            elif name == "paused":
                reason = str(details.get("reason") or "")
                if reason == PauseReason.HUMAN_VERIFICATION_REQUIRED:
                    text = f"暂停：人工完成抖音验证后恢复 [{reason}]"
                else:
                    text = f"暂停：{pause_reason_label(reason)} [{reason}]"
            elif name == "resumed":
                text = f"恢复执行（原暂停原因 {pause_reason_label(details.get('from', ''))}）"
            elif name == "finished":
                text = (
                    f"计划结束：{details.get('status', '')}"
                    + (
                        f"（{pause_reason_label(details.get('pause_reason', ''))}）"
                        if details.get("pause_reason")
                        else ""
                    )
                )
            elif name == "cancelled":
                text = f"操作者取消（原状态 {details.get('previous', '')}）"
            elif name == "interrupted":
                text = "进程中断，启动时归一化为暂停（不自动恢复）"
            elif name in ("archived", "unarchived"):
                text = "归档" if name == "archived" else "取消归档"
            elif name in ("marked_test_plan", "unmarked_test_plan"):
                text = "标记为验收/测试计划" if name == "marked_test_plan" else "取消验收标记"
            elif name == "queries_regenerated":
                text = (
                    f"重新生成查询词（{details.get('version', '')}）："
                    + "、".join(details.get("queries") or [])
                )
            lines.append(f"[{stamp}] {text}")
        return lines

    def report_lines(self, plan: CollectionPlan) -> list[str]:
        """Operator report: target, before/after coverage, budgets, results."""

        estimate = self.planner.estimate(plan)
        effectiveness = self.planner.effectiveness(plan)
        progress = plan.progress
        lines = [
            f"计划 #{plan.id} {plan.name} [{plan.status}]",
            f"素材分类: {plan.library_category} | 统计口径: {plan.count_mode} | "
            f"创建: {plan.created_at}",
            f"批准: {plan.approved_at or '未批准'}"
            + (f"（{plan.approval_note}）" if plan.approval_note else ""),
            f"暂停原因: {self.pause_reason_text(plan)}",
            f"查询词排序版本: {self.plan_ranking_version(plan)}",
            "",
            f"目标片段: {plan.target_final_clips} | 已完成(目标命中): "
            f"{effectiveness.qualifying_clips} | 其它有效片段: {effectiveness.off_target_clips}",
            f"候选: {progress.candidates_seen}（唯一 {progress.unique_candidates}）| "
            f"预览: {progress.preview_calls} | 下载: {progress.downloads} | "
            f"AI 调用: {progress.ai_calls} | tokens: {progress.ai_tokens}",
            f"预计: 预览 <= {estimate.previews} | 下载 <= {estimate.downloads} | "
            f"tokens ≈ {estimate.ai_tokens} (confidence={estimate.confidence})",
            f"预算使用: 预览 {progress.preview_calls}/{plan.max_preview_candidates} | "
            f"下载 {progress.downloads}/{plan.max_downloads} | "
            f"tokens {progress.ai_tokens}/{plan.max_ai_tokens}",
            "",
            "目标明细:",
        ]
        for item in plan.sorted_items():
            lines.append(
                f"  {item.process_stage:<18} 命中 {item.progress.qualifying_clips}"
                f"/{item.requested_clips} [{item.status}] "
                f"查询 {item.progress.queries_attempted}/{len(item.queries)} "
                f"下载 {item.progress.downloads}/{item.max_downloads} "
                f"tokens {item.progress.ai_tokens}/{item.max_tokens}"
            )
        if progress.stage_breakdown:
            lines.append(f"  实际工序分布: {progress.stage_breakdown}")
        delta = self.planner.coverage_delta(plan.coverage_before, plan.coverage_after)
        lines.append("")
        lines.append(f"覆盖变化: {delta or '（尚未执行）'}")
        if plan.coverage_before:
            before = {
                stage: entry.get("current")
                for stage, entry in (plan.coverage_before.get("process_stage") or {}).items()
            }
            after = {
                stage: entry.get("current")
                for stage, entry in (plan.coverage_after.get("process_stage") or {}).items()
            }
            lines.append(f"  执行前: {before}")
            if after:
                lines.append(f"  执行后: {after}")
        if estimate.basis:
            lines.append("")
            lines.append("估算依据: " + "；".join(estimate.basis))
        lines.append(
            "效果: 目标命中率 "
            + (f"{effectiveness.objective_hit_rate:.0%}" if effectiveness.objective_hit_rate is not None else "n/a")
            + " | tokens/命中片段 "
            + (
                f"{effectiveness.tokens_per_qualifying_clip:.0f}"
                if effectiveness.tokens_per_qualifying_clip is not None
                else "n/a"
            )
            + " | 下载/命中片段 "
            + (
                f"{effectiveness.downloads_per_qualifying_clip:.2f}"
                if effectiveness.downloads_per_qualifying_clip is not None
                else "n/a"
            )
        )
        return lines

    def history_rows(
        self, *, limit: int = 50, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        """Plan history table (section 37)."""

        rows: list[dict[str, Any]] = []
        for plan in self.repo.list_plans(limit=limit, include_archived=include_archived):
            effectiveness = self.planner.effectiveness(plan)
            delta = self.planner.coverage_delta(plan.coverage_before, plan.coverage_after)
            rows.append(
                {
                    "plan_id": plan.id,
                    "name": plan.name,
                    "category": plan.library_category,
                    "created_at": str(plan.created_at or ""),
                    "status": str(plan.status),
                    "target": plan.target_final_clips,
                    "qualifying_clips": effectiveness.qualifying_clips,
                    "saved_clips": plan.progress.clips_saved,
                    "off_target_clips": effectiveness.off_target_clips,
                    "ai_tokens": plan.progress.ai_tokens,
                    "downloads": plan.progress.downloads,
                    "coverage_delta": delta,
                    "pause_reason": plan.pause_reason,
                    "pause_reason_label": plan.pause_reason_label,
                    "archived": plan.archived,
                    "test_plan": plan.test_plan,
                    "ranking_version": self.plan_ranking_version(plan),
                }
            )
        return rows
