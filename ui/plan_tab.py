"""采集计划 tab (Milestone 7, sections 18/44/45).

Operator flow: generate a draft from the real coverage gaps, inspect and edit
it, approve it, then execute it under hard budgets with live status and
pause/resume/cancel.  Nothing here starts acquisition on its own.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from core.config import AppSettings
from core.plan_runner import PlanRunner
from core.plan_service import PlanService
from core.plans import PlanStatus, PauseReason
from core.task_runner import TaskRunner

LOGGER = logging.getLogger(__name__)

try:  # gradio is optional at import time (see ui/gradio_app.py)
    import gradio as gr

    GRADIO_AVAILABLE = True
except Exception:  # pragma: no cover - depends on the environment
    gr = None  # type: ignore[assignment]
    GRADIO_AVAILABLE = False

PLAN_TABLE_HEADERS = [
    "计划",
    "名称",
    "分类",
    "状态",
    "目标",
    "命中",
    "片段",
    "tokens",
    "下载",
    "暂停原因",
    "标记",
]

ITEM_TABLE_HEADERS = [
    "目标ID",
    "工序",
    "优先级",
    "需要",
    "命中",
    "状态",
    "查询(已用/总)",
    "下载(已用/上限)",
    "tokens(已用/上限)",
    "候选(已用/上限)",
]


def plan_report_markdown(service: PlanService, plan_id: int | None) -> str:
    """Markdown report for one plan (empty when nothing is selected)."""

    if plan_id in (None, ""):
        return "请选择或生成一个计划。"
    plan = service.get_plan(int(plan_id))
    if plan is None:
        return f"计划 #{plan_id} 不存在。"
    lines = service.report_lines(plan)
    problems = service.validate(plan)
    if problems:
        lines.extend(["", "**校验问题**: " + "；".join(problems)])
    return "\n".join(lines)


def plan_rows(service: PlanService, *, include_archived: bool = False) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for row in service.history_rows(include_archived=include_archived):
        marks = []
        if row.get("archived"):
            marks.append("归档")
        if row.get("test_plan"):
            marks.append("测试")
        rows.append(
            [
                row["plan_id"],
                row["name"],
                row["category"],
                row["status"],
                row["target"],
                row["qualifying_clips"],
                row["saved_clips"],
                row["ai_tokens"],
                row["downloads"],
                row.get("pause_reason_label") or "-",
                ",".join(marks) or "-",
            ]
        )
    return rows


def plan_status_markdown(service: PlanService, plan_id: int | None) -> str:
    """Live status: current item/query + budget progress (M8 sections 25/26)."""

    if plan_id in (None, ""):
        return ""
    plan = service.get_plan(int(plan_id))
    if plan is None:
        return ""
    return "```\n" + "\n".join(service.status_lines(plan)) + "\n```"


def plan_timeline_markdown(service: PlanService, plan_id: int | None) -> str:
    """Execution timeline rendered from ``collection_plan_events`` (§24)."""

    if plan_id in (None, ""):
        return ""
    plan = service.get_plan(int(plan_id))
    if plan is None:
        return ""
    lines = service.timeline_lines(plan)
    if not lines:
        return ""
    return "**执行时间线**\n\n" + "\n".join(f"- `{line}`" for line in lines)


def item_rows(service: PlanService, plan_id: int | None) -> list[list[Any]]:
    if plan_id in (None, ""):
        return []
    plan = service.get_plan(int(plan_id))
    if plan is None:
        return []
    rows: list[list[Any]] = []
    for item in plan.sorted_items():
        rows.append(
            [
                item.id,
                item.process_stage,
                item.priority,
                item.requested_clips,
                item.progress.qualifying_clips,
                str(item.status),
                f"{item.progress.queries_attempted}/{len(item.queries)}",
                f"{item.progress.downloads}/{item.max_downloads}",
                f"{item.progress.ai_tokens}/{item.max_tokens}",
                f"{item.progress.unique_candidates}/{item.max_candidates}",
            ]
        )
    return rows


def build_plan_tab(settings: AppSettings, runner: TaskRunner) -> None:
    """Render the 采集计划 tab (call inside a ``gr.Tabs()`` context)."""

    service = PlanService(runner.library, settings)
    active: dict[int, PlanRunner] = {}
    with gr.Tab("采集计划"):
        gr.Markdown(
            "### 采集计划（人工审批 + 受预算限制的执行）\n"
            "流程: 从覆盖缺口生成草稿 → 查看/编辑 → **批准** → 执行（可暂停/恢复/取消）。"
            "计划不会自行运行，也不会自动重试或新建计划。"
        )
        with gr.Row():
            category_box = gr.Dropdown(
                label="素材分类",
                choices=[name for name, _count in service.planner.coverage.categories()],
                value=(service.planner.coverage.categories()[0][0]
                       if service.planner.coverage.categories() else ""),
                allow_custom_value=True,
            )
            count_mode_box = gr.Radio(
                label="覆盖统计口径",
                choices=[("全部片段", "all"), ("仅已批准", "approved")],
                value=settings.coverage.count_mode,
            )
            include_healthy_box = gr.Checkbox(label="包含已达标的工序", value=False)
            name_box = gr.Textbox(label="计划名称（可选）", value="")
            create_button = gr.Button("从覆盖缺口生成草稿", variant="primary")

        status_box = gr.Markdown("")
        with gr.Row():
            refresh_button = gr.Button("刷新计划列表", variant="secondary")
            include_archived_box = gr.Checkbox(label="显示已归档/测试计划", value=False)
            plan_box = gr.Dropdown(label="计划", choices=[], value=None, allow_custom_value=True)
        history_table = gr.Dataframe(
            headers=PLAN_TABLE_HEADERS, label="计划历史", wrap=True, interactive=False
        )
        detail_box = gr.Markdown("请选择或生成一个计划。")
        live_box = gr.Markdown(label="当前状态 / 预算进度")
        items_table = gr.Dataframe(
            headers=ITEM_TABLE_HEADERS, label="目标明细", wrap=True, interactive=False
        )
        timeline_box = gr.Markdown(label="执行时间线")

        with gr.Accordion("编辑草稿（仅草稿可编辑）", open=False):
            with gr.Row():
                item_box = gr.Dropdown(label="目标", choices=[], value=None)
                requested_box = gr.Number(label="需要片段数", value=None, precision=0)
                candidates_box = gr.Number(label="候选上限", value=None, precision=0)
                downloads_box = gr.Number(label="下载上限", value=None, precision=0)
                tokens_box = gr.Number(label="token 上限", value=None, precision=0)
                priority_box = gr.Dropdown(
                    label="优先级",
                    choices=["critical", "high", "medium", "healthy"],
                    value=None,
                )
            queries_box = gr.Textbox(
                label="查询词（每行一个，留空表示不改）", lines=4, value=""
            )
            with gr.Row():
                save_edit_button = gr.Button("保存草稿修改")
                regenerate_button = gr.Button("用当前排序版本重新生成查询词")

        with gr.Row():
            approve_button = gr.Button("批准计划")
            dry_run_button = gr.Button("dry-run（不调用任何外部服务）")
            run_confirm = gr.Checkbox(
                label="我确认按上述预算执行（会调用真实 Douyin/Qwen）", value=False
            )
            run_button = gr.Button("执行计划", variant="primary")
        with gr.Row():
            pause_button = gr.Button("暂停")
            resume_button = gr.Button("恢复")
            cancel_button = gr.Button("取消", variant="stop")
        with gr.Row():
            archive_button = gr.Button("归档计划")
            unarchive_button = gr.Button("取消归档")
            mark_test_button = gr.Button("标记为验收/测试计划")
            unmark_test_button = gr.Button("取消验收标记")

        # -- callbacks -----------------------------------------------------
        def _refresh(selected: Any = None, include_archived: bool = False) -> tuple[Any, ...]:
            rows = plan_rows(service, include_archived=bool(include_archived))
            choices = [(f"#{row[0]} {row[1]} [{row[3]}]", row[0]) for row in rows]
            current = selected
            if current in (None, "") and choices:
                current = choices[0][1]
            items = item_rows(service, current)
            item_choices = [
                (f"#{row[0]} {row[1]} [{row[5]}]", row[0]) for row in items
            ]
            return (
                rows,
                gr.update(choices=choices, value=current),
                plan_report_markdown(service, current),
                plan_status_markdown(service, current),
                plan_timeline_markdown(service, current),
                items,
                gr.update(choices=item_choices, value=None),
            )

        def _create(category: str, count_mode: str, include_healthy: bool, name: str):
            if not (category or "").strip():
                return ("请先选择素材分类。", *[gr.update()] * 0, *(()))
            plan, action = service.create_plan(
                category.strip(),
                count_mode=count_mode,
                include_healthy=bool(include_healthy),
                name=(name or "").strip() or None,
            )
            rows, plan_update, report, live, timeline, items, item_update = _refresh(
                plan.id if plan else None
            )
            return (
                action.message,
                rows,
                plan_update,
                report,
                live,
                timeline,
                items,
                item_update,
            )

        def _select(plan_id: Any) -> tuple[Any, ...]:
            return (
                plan_report_markdown(service, plan_id),
                plan_status_markdown(service, plan_id),
                plan_timeline_markdown(service, plan_id),
                item_rows(service, plan_id),
                gr.update(
                    choices=[
                        (f"#{row[0]} {row[1]} [{row[5]}]", row[0])
                        for row in item_rows(service, plan_id)
                    ],
                    value=None,
                ),
            )

        def _save_edit(plan_id, item_id, requested, candidates, downloads, tokens, priority, queries):
            if plan_id in (None, "") or item_id in (None, ""):
                return ("请先选择计划与目标。", *[gr.update()] * 3, [], gr.update())
            query_list = [line.strip() for line in str(queries or "").splitlines() if line.strip()]
            action = service.edit_item(
                int(plan_id),
                int(item_id),
                requested_clips=int(requested) if requested else None,
                max_candidates=int(candidates) if candidates else None,
                max_downloads=int(downloads) if downloads else None,
                max_tokens=int(tokens) if tokens else None,
                priority=priority or None,
                queries=query_list or None,
            )
            report, live, timeline, items, item_update = _select(plan_id)
            return action.message, report, live, timeline, items, item_update

        def _approve(plan_id, note: str = ""):
            if plan_id in (None, ""):
                return ("请先选择计划。", *[gr.update()] * 3, gr.update(), [], gr.update())
            action = service.approve(int(plan_id), note=note)
            report, live, timeline, items, item_update = _select(plan_id)
            rows = plan_rows(service)
            return action.message, report, live, timeline, items, item_update, rows

        def _dry_run(plan_id):
            if plan_id in (None, ""):
                return "请先选择计划。"
            plan = service.get_plan(int(plan_id))
            if plan is None:
                return f"计划 #{plan_id} 不存在。"
            estimate = service.estimate(plan)
            lines = ["**dry-run（没有任何外部调用）**", ""]
            for item in plan.sorted_items():
                lines.append(
                    f"- `{item.process_stage}` [{item.priority}] 需要 {item.remaining_clips} 片段，"
                    f"候选 ≤ {item.max_candidates}，下载 ≤ {item.max_downloads}，tokens ≤ {item.max_tokens}"
                )
                for query in item.queries:
                    lines.append(f"    - {query.query} ({query.origin})")
            lines.append("")
            lines.append(
                f"计划预算: 预览 ≤ {plan.max_preview_candidates}，下载 ≤ {plan.max_downloads}，"
                f"tokens ≤ {plan.max_ai_tokens}；预计 tokens ≈ {estimate.ai_tokens} "
                f"(confidence={estimate.confidence})"
            )
            return "\n".join(lines)

        def _run(plan_id, confirmed: bool):
            if not confirmed:
                return "请先勾选确认（执行会调用真实 Douyin 与 Qwen，并可能产生费用）。"
            if plan_id in (None, ""):
                return "请先选择计划。"
            numeric = int(plan_id)
            plan = service.get_plan(numeric)
            if plan is None:
                return f"计划 #{numeric} 不存在。"
            if plan.status is PlanStatus.DRAFT:
                return "草稿不能执行：请先点击「批准计划」。"
            if numeric in active and active[numeric]._running:  # noqa: SLF001
                return f"计划 #{numeric} 正在运行中。"

            plan_runner = PlanRunner(
                numeric,
                library=runner.library,
                settings=settings,
                on_event=None,
            )
            active[numeric] = plan_runner

            def _work() -> None:
                import asyncio

                try:
                    asyncio.run(plan_runner.run())
                except Exception as exc:  # pragma: no cover - defensive
                    LOGGER.exception("plan run failed")
                    plan_runner.repo.log_event(
                        numeric, "failed", details={"error": str(exc)[:200]}
                    )
                finally:
                    active.pop(numeric, None)

            thread = threading.Thread(target=_work, daemon=True, name=f"plan-{numeric}")
            thread.start()
            return (
                f"计划 #{numeric} 已开始执行（后台线程）。"
                "点击「刷新计划列表」查看进度；暂停/取消会等到当前任务结束。"
            )

        def _control(plan_id, action: str):
            if plan_id in (None, ""):
                return "请先选择计划。", gr.update(), []
            numeric = int(plan_id)
            live = active.get(numeric)
            plan = service.get_plan(numeric)
            if action == "pause":
                if live is not None:
                    live.request_pause(PauseReason.OPERATOR)
                message = service.pause(numeric).message
            elif action == "resume":
                if live is not None:
                    live.request_resume()
                message = service.resume(numeric).message
            else:
                if live is not None:
                    live.request_cancel()
                message = service.cancel(numeric).message
            rows = plan_rows(service)
            report, live, timeline, items, item_update = _select(numeric)
            plan_record = plan if plan is not None else service.get_plan(numeric)
            if plan_record is not None and action == "pause" and plan_record.status is PlanStatus.RUNNING:
                # the runner pauses after the current task; surface that clearly
                message += "（将在当前任务完成后进入 paused）"
            return message, report, live, timeline, items, item_update, rows

        def _marks(plan_id, action: str):
            """Archive / test markers / draft query regeneration (M8 §23/§29/§30)."""

            if plan_id in (None, ""):
                return "请先选择计划。", *[gr.update()] * 3, [], gr.update(), []
            numeric = int(plan_id)
            if action == "archive":
                message = service.archive(numeric, archived=True).message
            elif action == "unarchive":
                message = service.archive(numeric, archived=False).message
            elif action == "mark_test":
                message = service.mark_test_plan(numeric, flag=True).message
            elif action == "unmark_test":
                message = service.mark_test_plan(numeric, flag=False).message
            else:  # regenerate queries needs an item
                plan = service.get_plan(numeric)
                target = next(
                    (item for item in (plan.items if plan else []) if not item.satisfied),
                    None,
                )
                if target is None or target.id is None:
                    message = "没有可重新生成查询词的目标。"
                else:
                    message = service.regenerate_queries(numeric, target.id).message
            plan = service.get_plan(numeric)
            report, live, timeline, items, item_update = _select(numeric)
            rows = plan_rows(service, include_archived=bool(plan and plan.archived))
            return message, report, live, timeline, items, item_update, rows

        create_button.click(
            fn=_create,
            inputs=[category_box, count_mode_box, include_healthy_box, name_box],
            outputs=[
                status_box,
                history_table,
                plan_box,
                detail_box,
                live_box,
                timeline_box,
                items_table,
                item_box,
            ],
        )
        refresh_button.click(
            fn=lambda selected, archived: _refresh(selected, archived),
            inputs=[plan_box, include_archived_box],
            outputs=[history_table, plan_box, detail_box, live_box, timeline_box, items_table, item_box],
        )
        plan_box.change(
            fn=_select,
            inputs=[plan_box],
            outputs=[detail_box, live_box, timeline_box, items_table, item_box],
        )
        save_edit_button.click(
            fn=_save_edit,
            inputs=[
                plan_box,
                item_box,
                requested_box,
                candidates_box,
                downloads_box,
                tokens_box,
                priority_box,
                queries_box,
            ],
            outputs=[status_box, detail_box, live_box, timeline_box, items_table, item_box],
        )
        regenerate_button.click(
            fn=lambda plan_id: _marks(plan_id, "regenerate"),
            inputs=[plan_box],
            outputs=[
                status_box,
                detail_box,
                live_box,
                timeline_box,
                items_table,
                item_box,
                history_table,
            ],
        )
        approve_button.click(
            fn=_approve,
            inputs=[plan_box],
            outputs=[
                status_box,
                detail_box,
                live_box,
                timeline_box,
                items_table,
                item_box,
                history_table,
            ],
        )
        dry_run_button.click(fn=_dry_run, inputs=[plan_box], outputs=[status_box])
        run_button.click(fn=_run, inputs=[plan_box, run_confirm], outputs=[status_box])
        pause_button.click(
            fn=lambda plan_id: _control(plan_id, "pause"),
            inputs=[plan_box],
            outputs=[
                status_box,
                detail_box,
                live_box,
                timeline_box,
                items_table,
                item_box,
                history_table,
            ],
        )
        resume_button.click(
            fn=lambda plan_id: _control(plan_id, "resume"),
            inputs=[plan_box],
            outputs=[
                status_box,
                detail_box,
                live_box,
                timeline_box,
                items_table,
                item_box,
                history_table,
            ],
        )
        cancel_button.click(
            fn=lambda plan_id: _control(plan_id, "cancel"),
            inputs=[plan_box],
            outputs=[
                status_box,
                detail_box,
                live_box,
                timeline_box,
                items_table,
                item_box,
                history_table,
            ],
        )
        for button, action in (
            (archive_button, "archive"),
            (unarchive_button, "unarchive"),
            (mark_test_button, "mark_test"),
            (unmark_test_button, "unmark_test"),
        ):
            button.click(
                fn=lambda plan_id, step=action: _marks(plan_id, step),
                inputs=[plan_box],
                outputs=[
                    status_box,
                    detail_box,
                    live_box,
                    timeline_box,
                    items_table,
                    item_box,
                    history_table,
                ],
            )
        gr.Markdown(
            "说明：计划只做「要采什么、采多少、花多少预算」的编排，采集本身仍走"
            " `素材采集` 使用的同一套 Douyin/dtk/Qwen/FFmpeg 流程；"
            "预算到达上限会自动暂停，不会无限采集，也不会自动新建计划。"
        )
