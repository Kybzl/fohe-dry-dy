"""素材库 / 任务记录 / 系统检查 tabs (Milestone 4).

The acquisition tab keeps its original behaviour; these tabs are the new
operator surface:

* 素材库: filter, page, sort, thumbnail gallery, video preview, full metadata,
  provenance, human review, favorite, retag, safe delete and manifest export
* 任务记录: read-only task history with search yields / sources / clips
* 系统检查: environment and library health reports

All data access goes through :class:`core.library_service.LibraryService`, so
these callbacks contain no SQL and no filesystem policy.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Sequence

from core.config import AppSettings, save_cloud_cleanup_limits
from core.library_service import EXPORT_FIELDS, ClipFilters, ClipPage, LibraryService
from core.models import (
    CameraMotion,
    ClipRecord,
    EditRole,
    MaterialForm,
    MaterialState,
    PersonRole,
    ProcessStage,
    REVIEW_STATUS_CHOICES,
    ReviewStatus,
    ShotType,
    SubtitleType,
)
from core.provenance import DOUYIN_REAL, LOCAL_TEST, MOCK, UNKNOWN
from core.task_runner import TaskRunner

LOGGER = logging.getLogger(__name__)

try:  # gradio is optional at import time (see ui/gradio_app.py)
    import gradio as gr

    GRADIO_AVAILABLE = True
except Exception:  # pragma: no cover - depends on the environment
    gr = None  # type: ignore[assignment]
    GRADIO_AVAILABLE = False

PROVENANCE_CHOICES: list[tuple[str, str]] = [
    ("真实素材（抖音）", DOUYIN_REAL),
    ("全部素材", ""),
    ("本地测试素材", LOCAL_TEST),
    ("模拟/演示素材", MOCK),
    ("未分类", UNKNOWN),
]

REVIEW_LABELS: dict[str, str] = {value: label for label, value in REVIEW_STATUS_CHOICES}

#: widget order of the filter row (must match ``LibraryTab.filters_from_values``)
FILTER_FIELD_ORDER: tuple[str, ...] = (
    "library_category",
    "material",
    "material_form",
    "material_state",
    "process_stage",
    "shot_type",
    "camera_motion",
    "person_role",
    "equipment_visible",
    "people",
    "edit_role",
    "provenance",
    "tag_prompt_version",
    "review_status",
    "favorite",
    "subtitle_type",
    "free_text",
    "min_overall_score",
    "min_duration",
    "max_duration",
    "created_after",
    "created_before",
)

CLIP_TABLE_HEADERS = [
    "id",
    "素材分类",
    "识别物料",
    "形态",
    "状态",
    "工序",
    "时长(s)",
    "总分",
    "字幕",
    "审核",
    "收藏",
    "来源",
    "文件",
]

TASK_TABLE_HEADERS = [
    "任务",
    "物料",
    "分类",
    "目标",
    "状态",
    "创建时间",
    "保存片段",
    "AI 调用",
    "tokens",
    "搜索词",
]

YIELD_TABLE_HEADERS = ["搜索词", "候选", "唯一", "通过预筛", "下载", "最终片段"]
SOURCE_TABLE_HEADERS = [
    "来源ID",
    "视频ID",
    "状态",
    "拒绝原因",
    "标题",
    "作者",
    "匹配搜索词",
    "产出片段",
]
AI_TABLE_HEADERS = [
    "id",
    "operation",
    "provider",
    "model",
    "prompt_version",
    "延迟(ms)",
    "tokens",
    "origin",
    "status",
    "created_at",
]


def _enum_choices(enum_type: Any) -> list[tuple[str, str]]:
    return [("全部", ""), *[(item.value, item.value) for item in enum_type]]


def _labels(enum_type: Any) -> dict[str, str]:
    """Chinese-ish labels for common enum values (falls back to the raw value)."""

    return {item.value: item.value for item in enum_type}


def clip_table_rows(clips: Sequence[ClipRecord], service: LibraryService) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for clip in clips:
        problems = service.missing_files(clip)
        rows.append(
            [
                clip.id,
                clip.library_category or "未记录",
                clip.material,
                str(clip.material_form),
                str(clip.material_state),
                str(clip.process_stage),
                round(float(clip.duration or 0.0), 2),
                round(float(clip.overall_score or 0.0), 2),
                str(clip.subtitle_type),
                REVIEW_LABELS.get(str(clip.review_status), str(clip.review_status)),
                "★" if clip.favorite else "",
                clip.provenance or "legacy",
                "、".join(problems) if problems else "ok",
            ]
        )
    return rows


def gallery_items(clips: Sequence[ClipRecord], service: LibraryService) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for clip in clips:
        thumb = service.thumbnail_path(clip)
        if thumb is None:
            continue
        caption = (
            f"#{clip.id} {clip.library_category or '未记录'} · {clip.material} "
            f"{clip.material_state} · {clip.duration:.1f}s · {clip.overall_score:.2f}"
        )
        items.append((str(thumb), caption))
    return items


def library_header_markdown(overview: dict[str, Any], page: ClipPage) -> str:
    return (
        f"### 素材库总览\n"
        f"素材总数 **{overview['total']}** · 真实抖音素材 **{overview['real']}** · "
        f"待审核 **{overview['unreviewed']}** · 已批准 **{overview['approved']}** · "
        f"收藏 **{overview['favorite']}**\n\n"
        f"当前筛选结果 **{overview['filtered']}** 段 · 总时长 "
        f"**{overview['filtered_seconds']:.1f}s**\n\n"
        f"{page.summary()}"
    )


def category_markdown(counts: Sequence[tuple[str, int]]) -> str:
    if not counts:
        return "**素材分类**: 暂无数据"
    joined = " · ".join(f"{name} {count}" for name, count in counts)
    return f"**素材分类计数**: {joined}"


def detail_markdown(
    clip: ClipRecord,
    *,
    service: LibraryService,
    source: dict[str, Any],
) -> str:
    missing = service.missing_files(clip)
    edit_roles = "、".join(str(role) for role in clip.edit_roles) or "-"
    lines = [
        f"### 素材 #{clip.id}",
        f"| 字段 | 值 |",
        f"|---|---|",
        f"| 素材分类 (library_category) | **{clip.library_category or '未记录'}** |",
        f"| 识别物料 (material) | {clip.material} |",
        f"| 物料形态 | {clip.material_form} |",
        f"| 物料状态 | {clip.material_state} |",
        f"| 工序阶段 | {clip.process_stage} |",
        f"| 设备 | {clip.equipment_type or '-'} (可见={clip.equipment_visible}) |",
        f"| 场景 | {clip.scene or '-'} |",
        f"| 景别 / 运镜 | {clip.shot_type} / {clip.camera_motion} |",
        f"| 人物 | {clip.people}（人数 {clip.people_count}，角色 {clip.person_role}） |",
        f"| 字幕 | {clip.subtitle_type}（分数 {clip.subtitle_score:.2f}） |",
        f"| 剪辑角色 | {edit_roles} |",
        f"| 描述 | {clip.description or '-'} |",
        f"| 评分 | 物料 {clip.material_score:.2f} · 画质 {clip.visual_quality_score:.2f} · "
        f"字幕洁净 {clip.subtitle_cleanliness_score:.2f} · 稳定 {clip.stability_score:.2f} · "
        f"构图 {clip.composition_score:.2f} · 总分 **{clip.overall_score:.2f}** |",
        f"| 规格 | {clip.duration:.2f}s · {clip.width}x{clip.height} · fps {clip.fps} |",
        f"| 来源类型 (provenance) | **{clip.provenance or 'legacy'}** |",
        f"| 打标 prompt | {clip.tag_prompt_version or '未记录'} |",
        f"| 人工审核 | **{REVIEW_LABELS.get(str(clip.review_status), clip.review_status)}**"
        f" |",
        f"| 审核备注 | {clip.review_note or '-'} |",
        f"| 收藏 | {'★ 是' if clip.favorite else '否'} |",
        f"| 来源平台 / 视频ID | {clip.platform} / {clip.platform_video_id} |",
        f"| 原视频链接 | {clip.source_url or '-'} |",
        f"| 剪辑时间段 | {clip.source_start:.2f}s - {clip.source_end:.2f}s |",
        f"| 入库时间 | {clip.created_at.isoformat() if clip.created_at else '-'} |",
        f"| 文件 | {clip.file_path} |",
        f"| 缩略图 | {clip.thumbnail_path or '-'} |",
        f"| 文件状态 | {'、'.join(missing) if missing else '正常'} |",
    ]
    if source:
        lines.extend(
            [
                "",
                f"**来源视频**: {source.get('title') or '(无标题)'} · 作者 {source.get('author') or '-'}"
                f" · 发布 {source.get('publish_time') or '-'}",
                f"**来源状态**: {source.get('status')} "
                f"(原因 {source.get('reject_reason') or '-'}) · "
                f"该来源共产出 {source.get('clips')} 个片段 · "
                f"匹配搜索词 {source.get('matched_queries') or '-'}",
            ]
        )
    measured = clip.subtitle_analysis if isinstance(clip.subtitle_analysis, dict) else None
    if measured:
        lines.extend(
            [
                "",
                "**字幕测量证据（本地文字检测，Milestone 6）**",
                "",
                f"| 指标 | 值 |",
                f"|---|---|",
                f"| 字幕分类 | {measured.get('classification')} |",
                f"| 字幕洁净度 | {measured.get('cleanliness_score')} |",
                f"| 文字区域数（平均/最多） | "
                f"{measured.get('avg_text_regions')} / {measured.get('max_text_regions')} |",
                f"| 文字覆盖率（平均） | {measured.get('total_text_area_ratio_avg')} |",
                f"| 底部字幕持续比例 | {measured.get('bottom_persistence')} |",
                f"| 中央文字持续比例 | {measured.get('center_persistence')} |",
                f"| 多区域持续比例 | {measured.get('multi_region_persistence')} |",
                f"| 判定方式 | {measured.get('decision_source')} |",
                f"| 分析版本 | {measured.get('analysis_version')} |",
                f"| 检测引擎 | {measured.get('engine')} |",
            ]
        )
        checks = (measured.get("evidence") or {}).get("checks") or []
        if checks:
            lines.append(f"| 判定依据 | {'；'.join(str(item) for item in checks[:3])} |")
    else:
        lines.extend(
            [
                "",
                "*字幕测量证据: 未记录（历史片段，或分析未运行）*",
            ]
        )
    cleanup = service.library.subtitle_cleanup(int(clip.id or 0)) if clip.id else None
    if cleanup:
        lines.extend(
            [
                "",
                "**字幕清理状态（Milestone 9.2，派生文件）**",
                "",
                "| 字段 | 值 |",
                "|---|---|",
                f"| 清理状态 | **{cleanup.get('status')}** |",
                f"| 清理版本 | {cleanup.get('version')} |",
                f"| 清理引擎 | {cleanup.get('engine') or '-'} |",
                f"| 清理前/后洁净度 | "
                f"{(cleanup.get('before_metrics') or {}).get('cleanliness_score', '-')} → "
                f"{(cleanup.get('after_metrics') or {}).get('cleanliness_score', '-')} |",
                f"| 人工复核状态 | **{cleanup.get('review_status') or 'pending'}** |",
                f"| 复核失败分类 | {cleanup.get('review_failure_class') or '-'} |",
                f"| 复核备注 | {cleanup.get('review_note') or '-'} |",
                f"| 是否满足清理条件 | {'是' if cleanup.get('eligible') else '否'} |",
                f"| 原因/状态说明 | {cleanup.get('skip_reason') or '-'} |",
                f"| 清理文件 | {cleanup.get('output_path') or '-'} |",
            ]
        )
    else:
        lines.extend(["", "*字幕清理状态: 未运行*"])
    return "\n".join(lines)


def task_detail_markdown(service: LibraryService, task_id: int) -> str:
    detail = service.task_detail(task_id)
    clips = detail["clips"]
    sources = detail["sources"]
    lines = [
        f"### 任务 #{task_id}",
        f"产出片段 **{len(clips)}** · 参与来源 **{len(sources)}** · "
        f"搜索词 **{len(detail['yields'])}**",
    ]
    if clips:
        lines.append(
            "片段: "
            + "、".join(
                f"#{row['clip_id']}({row['library_category']}/{row['material_state']})"
                for row in clips[:20]
            )
        )
    return "\n".join(lines)


class LibraryTab:
    """Encapsulates the 素材库 tab callbacks so the state stays explicit."""

    def __init__(self, service: LibraryService, settings: AppSettings) -> None:
        self.service = service
        self.settings = settings

    # -- helpers -----------------------------------------------------------
    def default_filters(self) -> ClipFilters:
        return ClipFilters(provenance=self.settings.library.default_provenance_filter or "")

    def filters_from_values(
        self,
        category: str,
        material: str,
        material_form: str,
        material_state: str,
        process_stage: str,
        shot_type: str,
        camera_motion: str,
        person_role: str,
        equipment_visible: str,
        people: str,
        edit_role: str,
        provenance: str,
        prompt_version: str,
        review_status: Sequence[str],
        favorite: str,
        subtitle_types: Sequence[str],
        free_text: str,
        min_score: float | None,
        min_duration: float | None,
        max_duration: float | None,
        created_after: str,
        created_before: str,
    ) -> ClipFilters:
        return ClipFilters(
            library_category=category,
            material=material,
            material_form=material_form,
            material_state=material_state,
            process_stage=process_stage,
            shot_type=shot_type,
            camera_motion=camera_motion,
            person_role=person_role,
            equipment_visible=equipment_visible,
            people=people,
            edit_role=edit_role,
            provenance=provenance,
            tag_prompt_version=prompt_version,
            review_status=list(review_status or []),
            favorite=favorite,
            subtitle_type=list(subtitle_types or []),
            free_text=free_text,
            min_overall_score=min_score,
            min_duration=min_duration,
            max_duration=max_duration,
            created_after=created_after,
            created_before=created_before,
        )

    def refresh(
        self,
        filters: ClipFilters,
        sort_by: str,
        page: int,
        page_size: int,
    ) -> tuple[Any, ...]:
        """One page + the header numbers + the selection choices."""

        try:
            page_result = self.service.fetch_page(
                filters, sort_by=sort_by, page=int(page or 1), page_size=page_size
            )
            overview = self.service.overview(filters)
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.exception("library page failed")
            return (
                f"⚠ 素材库查询失败: {exc}",
                "**素材分类计数**: -",
                [],
                [],
                gr.update(choices=[], value=[]),
                gr.update(choices=[], value=None),
                "查询失败，请查看日志。",
            )
        header = library_header_markdown(overview, page_result)
        categories = category_markdown(self.service.category_counts(filters))
        rows = clip_table_rows(page_result.clips, self.service)
        gallery = gallery_items(page_result.clips, self.service)
        choices = [
            (
                f"#{clip.id} {clip.library_category or '未记录'} · {clip.material} "
                f"{clip.material_state} · {clip.duration:.1f}s",
                clip.id,
            )
            for clip in page_result.clips
        ]
        return (
            header,
            categories,
            rows,
            gallery,
            gr.update(choices=choices, value=[]),
            gr.update(choices=choices, value=choices[0][1] if choices else None),
            page_result.summary(),
        )

    def load_detail(self, clip_id: Any) -> tuple[Any, ...]:
        """Detail panel, video, thumbnail, source panel and AI audit."""

        if clip_id in (None, ""):
            return ("请选择一条素材。", None, None, None, "（未选择素材）", [], "")
        try:
            clip = self.service.library.get_clip(int(clip_id))
        except (TypeError, ValueError):
            return ("素材 ID 无效。", None, None, None, "", [], "")
        if clip is None:
            return (f"素材 #{clip_id} 不存在。", None, None, None, "", [], "")
        source = self.service.source_detail(clip)
        # the original is always exposed; the cleaned derivative is a separate
        # player so a pending/rejected derivative can never hide the original
        original = self.service.video_path(clip)
        cleanup = (
            self.service.library.subtitle_cleanup(int(clip.id or 0))
            if clip.id
            else None
        )
        cleaned = (
            self.service.library.safe_media_path(cleanup.get("output_path"))
            if cleanup and str(cleanup.get("status")) == "succeeded"
            else None
        )
        if cleaned is not None and not cleaned.exists():
            cleaned = None
        thumb = self.service.thumbnail_path(clip)
        audit = self.service.ai_audit(int(clip.id or 0))
        audit_rows = [
            [
                row.get("id"),
                row.get("operation"),
                row.get("provider"),
                row.get("model"),
                row.get("prompt_version"),
                row.get("latency_ms"),
                row.get("total_tokens"),
                row.get("origin"),
                row.get("status"),
                row.get("created_at"),
            ]
            for row in audit
        ]
        latest = next(
            (row for row in audit if row.get("operation") == "clip_tagging"), None
        )
        raw = latest.get("result_json") if latest else ""
        return (
            detail_markdown(clip, service=self.service, source=source),
            str(original) if original else None,
            str(cleaned) if cleaned else None,
            str(thumb) if thumb else None,
            "（原视频文件缺失或位于素材库之外，无法播放）" if original is None else "",
            audit_rows,
            raw or "",
        )

    # -- actions -----------------------------------------------------------
    def review(self, clip_ids: Sequence[Any], status: str, note: str) -> str:
        ids = [int(value) for value in (clip_ids or []) if str(value).strip().isdigit()]
        if not ids:
            return "请先选择素材。"
        if status not in {item.value for item in ReviewStatus}:
            return f"未知审核状态: {status}"
        changed = self.service.set_review(
            ids, status=ReviewStatus(status), note=note or ""
        )
        return f"已把 {changed} 条素材标记为 {REVIEW_LABELS.get(status, status)}。"

    def clear_review(self, clip_ids: Sequence[Any]) -> str:
        ids = [int(value) for value in (clip_ids or []) if str(value).strip().isdigit()]
        if not ids:
            return "请先选择素材。"
        changed = self.service.set_review(ids, status=ReviewStatus.UNREVIEWED, note="")
        return f"已清除 {changed} 条素材的审核状态。"

    def favorite(self, clip_ids: Sequence[Any], value: bool) -> str:
        ids = [int(value) for value in (clip_ids or []) if str(value).strip().isdigit()]
        if not ids:
            return "请先选择素材。"
        changed = self.service.set_favorite(ids, value)
        return f"已{'收藏' if value else '取消收藏'} {changed} 条素材。"

    def export(self, filters: ClipFilters, sort_by: str, fmt: str) -> str:
        result = self.service.export_filtered(filters, fmt=fmt, sort_by=sort_by)
        if not result.get("ok"):
            return f"导出失败: {result.get('error')}"
        return f"已导出 {result['rows']} 条 -> {result['path']}"

    def export_selected(self, clip_ids: Sequence[Any], fmt: str) -> str:
        ids = [int(value) for value in (clip_ids or []) if str(value).strip().isdigit()]
        if not ids:
            return "请先在左侧选择要导出的素材。"
        result = self.service.export_by_ids(ids, fmt=fmt)
        if not result.get("ok"):
            return f"导出失败: {result.get('error')}"
        return f"已导出 {result['rows']} 条 -> {result['path']}"

    def delete(self, clip_id: Any, confirmation: str, filters: ClipFilters, sort_by: str, page: int, page_size: int):
        """Safe delete: the operator must echo the clip id to confirm."""

        try:
            target = int(clip_id)
        except (TypeError, ValueError):
            return ("请先选择要删除的素材。", *self.refresh(filters, sort_by, page, page_size))
        expected = f"删除素材 #{target}"
        if str(confirmation or "").strip() != expected:
            return (
                f"为避免误删，请在确认框里输入: {expected}",
                *self.refresh(filters, sort_by, page, page_size),
            )
        report = self.service.library.remove_clip(target)
        if not report.found:
            message = f"素材 #{target} 不存在。"
        elif report.refused_files:
            message = (
                f"素材 #{target} 的记录已删除，但以下路径不在素材库内，已拒绝删除文件: "
                f"{report.refused_files}"
            )
        else:
            message = (
                f"已删除素材 #{target}（文件 {len(report.removed_files)} 个，"
                f"孤立标签 {report.orphaned_tags} 个）；来源记录保留。"
            )
        return (message, *self.refresh(filters, sort_by, page, page_size))


def build_library_tab(settings: AppSettings, runner: TaskRunner) -> None:
    """Render the 素材库 tab (call inside a ``gr.Tabs()`` context)."""

    service = LibraryService(runner.library, settings)
    tab = LibraryTab(service, settings)
    with gr.Tab("素材库"):
        header = gr.Markdown(
            library_header_markdown(
                service.overview(tab.default_filters()),
                service.fetch_page(
                    tab.default_filters(),
                    page_size=settings.library.default_page_size,
                ),
            )
        )
        categories = gr.Markdown(category_markdown(service.category_counts()))

        with gr.Accordion("筛选条件（留空表示不限制）", open=True):
            with gr.Row():
                category_box = gr.Dropdown(
                    label="素材分类 (library_category)",
                    choices=[""] + [name for name, _count in service.category_counts()],
                    value="",
                    allow_custom_value=True,
                )
                material_box = gr.Textbox(label="识别物料 (material)", value="")
                form_box = gr.Dropdown(
                    label="物料形态", choices=_enum_choices(MaterialForm), value=""
                )
                state_box = gr.Dropdown(
                    label="物料状态", choices=_enum_choices(MaterialState), value=""
                )
                stage_box = gr.Dropdown(
                    label="工序阶段", choices=_enum_choices(ProcessStage), value=""
                )
            with gr.Row():
                shot_box = gr.Dropdown(
                    label="景别", choices=_enum_choices(ShotType), value=""
                )
                motion_box = gr.Dropdown(
                    label="运镜", choices=_enum_choices(CameraMotion), value=""
                )
                role_box = gr.Dropdown(
                    label="人物角色", choices=_enum_choices(PersonRole), value=""
                )
                edit_box = gr.Dropdown(
                    label="剪辑角色 (edit_role)", choices=_enum_choices(EditRole), value=""
                )
                equipment_box = gr.Dropdown(
                    label="设备可见", choices=[("全部", ""), ("是", "true"), ("否", "false")], value=""
                )
            with gr.Row():
                people_box = gr.Dropdown(
                    label="有人物", choices=[("全部", ""), ("是", "true"), ("否", "false")], value="false"
                )
                subtitle_box = gr.CheckboxGroup(
                    label="字幕类型",
                    choices=[item.value for item in SubtitleType],
                    value=[],
                )
                provenance_box = gr.Dropdown(
                    label="来源类型 (provenance)",
                    choices=[(label, value) for label, value in PROVENANCE_CHOICES],
                    value=settings.library.default_provenance_filter,
                )
                prompt_box = gr.Textbox(label="打标 prompt 版本", value="")
            with gr.Row():
                review_box = gr.CheckboxGroup(
                    label="审核状态",
                    choices=[(label, value) for label, value in REVIEW_STATUS_CHOICES],
                    value=[],
                )
                favorite_box = gr.Dropdown(
                    label="收藏", choices=[("全部", ""), ("已收藏", "true"), ("未收藏", "false")], value=""
                )
                free_text_box = gr.Textbox(
                    label="关键词搜索 (描述/场景/标题/作者)", value=""
                )
            with gr.Row():
                min_score_box = gr.Number(label="最低总分", value=None)
                min_duration_box = gr.Number(label="最短时长(s)", value=None)
                max_duration_box = gr.Number(label="最长时长(s)", value=None)
                created_after_box = gr.Textbox(label="入库时间 >= (ISO)", value="")
                created_before_box = gr.Textbox(label="入库时间 <= (ISO)", value="")
            with gr.Row():
                apply_button = gr.Button("应用筛选 / 刷新", variant="primary")
                reset_button = gr.Button("重置筛选")
                page_box = gr.Number(label="页码", value=1, precision=0)
                prev_button = gr.Button("上一页")
                next_button = gr.Button("下一页")
                page_size_box = gr.Dropdown(
                    label="每页",
                    choices=settings.library.page_sizes(),
                    value=settings.library.clamp_page_size(None),
                )
                sort_box = gr.Dropdown(
                    label="排序",
                    choices=list(tab.service.sort_choices()),
                    value="newest",
                )
            with gr.Row():
                preset_box = gr.Dropdown(
                    label="筛选预设",
                    choices=[preset["name"] for preset in service.library.list_filter_presets()],
                    value=None,
                    allow_custom_value=True,
                )
                preset_name_box = gr.Textbox(
                    label="预设名称", value="", placeholder="例如: 苹果干-无人物高分"
                )
                preset_save_button = gr.Button("保存为预设")
                preset_load_button = gr.Button("载入预设")
                preset_delete_button = gr.Button("删除预设")
        page_summary = gr.Markdown("")

        filter_inputs = [
            category_box,
            material_box,
            form_box,
            state_box,
            stage_box,
            shot_box,
            motion_box,
            role_box,
            equipment_box,
            people_box,
            edit_box,
            provenance_box,
            prompt_box,
            review_box,
            favorite_box,
            subtitle_box,
            free_text_box,
            min_score_box,
            min_duration_box,
            max_duration_box,
            created_after_box,
            created_before_box,
        ]

        def _filters(*values: Any) -> ClipFilters:
            return tab.filters_from_values(*values)

        def _refresh(*values: Any) -> tuple[Any, ...]:
            args = list(values)
            sort_by, page, page_size = args[-3:]
            return tab.refresh(_filters(*args[:-3]), sort_by, page, page_size)

        with gr.Row():
            with gr.Column(scale=3):
                gallery = gr.Gallery(
                    label="缩略图（仅缩略图，不预载视频）", columns=4, height=360
                )
                clip_rows = gr.Dataframe(
                    headers=CLIP_TABLE_HEADERS, label="素材明细", wrap=True, interactive=False
                )
            with gr.Column(scale=2):
                selection_box_holder = gr.Dropdown(
                    label="本页素材（可多选，用于批量操作/导出）",
                    choices=[],
                    value=[],
                    multiselect=True,
                )
                focus_box_holder = gr.Dropdown(
                    label="查看/操作素材（单选）", choices=[], value=None
                )
                page_lookup = gr.Dropdown(
                    label="跳转到素材 ID（含其它页）", choices=[], value=None, allow_custom_value=True
                )
                detail_box = gr.Markdown("请选择一条素材。")
                video_box = gr.Video(label="原始素材（永远保留）", interactive=False)
                cleaned_video_box = gr.Video(
                    label="清理派生（未批准时仅供复核）", interactive=False
                )
                thumb_box = gr.Image(label="缩略图", height=180)
                warning_box = gr.Markdown("")
                with gr.Row():
                    approve_button = gr.Button("批准")
                    reject_button = gr.Button("拒绝")
                    needs_review_button = gr.Button("需要复核")
                    clear_review_button = gr.Button("清除审核状态")
                note_box = gr.Textbox(label="审核备注", lines=2)
                with gr.Row():
                    favorite_button = gr.Button("收藏")
                    unfavorite_button = gr.Button("取消收藏")
                with gr.Row():
                    retag_box = gr.Checkbox(
                        label="我确认重新识别会调用真实模型（可能产生费用）", value=False
                    )
                    retag_button = gr.Button("重新识别标签", variant="secondary")
                with gr.Row():
                    cleanup_force_box = gr.Checkbox(
                        label="强制重试字幕清理（覆盖失败/已完成的同版本结果）",
                        value=False,
                    )
                    cleanup_button = gr.Button("运行字幕清理（本地，无云端调用）")
                with gr.Row():
                    cleanup_approve_button = gr.Button("批准清理派生")
                    cleanup_reject_button = gr.Button("拒绝清理派生")
                    cleanup_reset_button = gr.Button("重置清理复核")
                    cleanup_delete_button = gr.Button(
                        "删除清理派生", variant="stop"
                    )
                cleanup_review_note_box = gr.Textbox(
                    label="清理复核备注 / 失败分类", lines=2, value=""
                )
                cleanup_failure_class_box = gr.Dropdown(
                    label="拒绝时选择 failure_class",
                    choices=[
                        "residual_subtitle",
                        "visible_blur_patch",
                        "content_removed",
                        "flicker",
                        "mask_too_large",
                        "wrong_region",
                        "timing_mismatch",
                        "other",
                    ],
                    value="other",
                )
                with gr.Row():
                    delete_confirm_box = gr.Textbox(
                        label="删除确认（请输入：删除素材 #ID）", value=""
                    )
                    delete_button = gr.Button("安全删除", variant="stop")
                with gr.Row():
                    export_json_button = gr.Button("导出 JSON（当前筛选）")
                    export_csv_button = gr.Button("导出 CSV（当前筛选）")
                    export_selected_json = gr.Button("导出选中 JSON")
                    export_selected_csv = gr.Button("导出选中 CSV")
                action_box = gr.Markdown("")
                ai_table = gr.Dataframe(
                    headers=AI_TABLE_HEADERS, label="AI 审计（最新优先）", wrap=True, interactive=False
                )
                with gr.Accordion("最近一次打标原始结果", open=False):
                    ai_raw_box = gr.Code(label="result_json", language="json", lines=10)

        refresh_outputs = [
            header,
            categories,
            clip_rows,
            gallery,
            selection_box_holder,
            focus_box_holder,
            page_summary,
        ]

        # -- wiring --------------------------------------------------------
        apply_button.click(fn=_refresh, inputs=[*filter_inputs, sort_box, page_box, page_size_box], outputs=refresh_outputs)
        for component in (category_box, form_box, state_box, stage_box, provenance_box, sort_box, page_size_box, review_box, subtitle_box, favorite_box):
            component.change(fn=_refresh, inputs=[*filter_inputs, sort_box, page_box, page_size_box], outputs=refresh_outputs)
        material_box.submit(fn=_refresh, inputs=[*filter_inputs, sort_box, page_box, page_size_box], outputs=refresh_outputs)
        free_text_box.submit(fn=_refresh, inputs=[*filter_inputs, sort_box, page_box, page_size_box], outputs=refresh_outputs)
        min_score_box.submit(fn=_refresh, inputs=[*filter_inputs, sort_box, page_box, page_size_box], outputs=refresh_outputs)

        def _step(delta: int, page_value: Any):
            try:
                current = int(page_value or 1)
            except (TypeError, ValueError):
                current = 1
            return max(1, current + delta)

        page_box.change(fn=_refresh, inputs=[*filter_inputs, sort_box, page_box, page_size_box], outputs=refresh_outputs)
        prev_button.click(fn=lambda page: _step(-1, page), inputs=[page_box], outputs=[page_box])
        next_button.click(fn=lambda page: _step(1, page), inputs=[page_box], outputs=[page_box])

        reset_button.click(
            fn=lambda: (
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "false",
                "",
                settings.library.default_provenance_filter,
                "",
                [],
                "",
                [],
                "",
                None,
                None,
                None,
                "",
                "",
            ),
            outputs=filter_inputs,
        ).then(fn=_refresh, inputs=[*filter_inputs, sort_box, page_box, page_size_box], outputs=refresh_outputs)

        # -- saved filter presets (Milestone 5, sections 21/22) -----------
        def _preset_updates(preset: dict[str, Any]) -> list[Any]:
            values = dict(preset.get("filters") or {})
            defaults = ClipFilters()
            updates: list[Any] = []
            for field in FILTER_FIELD_ORDER:
                fallback = getattr(defaults, field)
                updates.append(gr.update(value=values.get(field, fallback)))
            return updates

        def _save_preset(name: str, *values: Any) -> tuple[Any, ...]:
            from core.library_service import preset_from_filters

            clean = (name or "").strip()
            if not clean:
                return ("请填写预设名称。", gr.update())
            payload = preset_from_filters(_filters(*values[:-3]))
            try:
                service.library.save_filter_preset(clean, payload)
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.exception("saving the preset failed")
                return (f"保存预设失败: {exc}", gr.update())
            names = [preset["name"] for preset in service.library.list_filter_presets()]
            return (f"已保存预设 {clean!r}: {payload}", gr.update(choices=names, value=clean))

        def _load_preset(name: str) -> tuple[Any, ...]:
            from core.library_service import preset_to_filters

            preset = service.library.get_filter_preset(name or "")
            if preset is None:
                return ("没有这个预设。", *[gr.update() for _ in FILTER_FIELD_ORDER])
            try:
                preset_to_filters(preset)
            except ValueError as exc:
                return (f"预设无效: {exc}", *[gr.update() for _ in FILTER_FIELD_ORDER])
            return (f"已载入预设 {name!r}", *_preset_updates(preset))

        def _delete_preset(name: str) -> tuple[str, Any]:
            clean = (name or "").strip()
            removed = service.library.delete_filter_preset(clean)
            names = [preset["name"] for preset in service.library.list_filter_presets()]
            return (f"删除预设 {clean!r}: {removed} 行", gr.update(choices=names, value=None))

        preset_save_button.click(
            fn=_save_preset,
            inputs=[preset_name_box, *filter_inputs, sort_box, page_box, page_size_box],
            outputs=[action_box, preset_box],
        )
        preset_load_button.click(
            fn=_load_preset, inputs=[preset_box], outputs=[action_box, *filter_inputs]
        ).then(
            fn=_refresh,
            inputs=[*filter_inputs, sort_box, page_box, page_size_box],
            outputs=refresh_outputs,
        )
        preset_delete_button.click(
            fn=_delete_preset, inputs=[preset_box], outputs=[action_box, preset_box]
        )

        detail_outputs = [
            detail_box,
            video_box,
            cleaned_video_box,
            thumb_box,
            warning_box,
            ai_table,
            ai_raw_box,
        ]
        focus_box_holder.change(
            fn=tab.load_detail, inputs=[focus_box_holder], outputs=detail_outputs
        )
        page_lookup.change(
            fn=tab.load_detail, inputs=[page_lookup], outputs=detail_outputs
        )

        approve_button.click(fn=lambda ids, note: tab.review(ids, ReviewStatus.APPROVED.value, note), inputs=[selection_box_holder, note_box], outputs=[action_box])
        reject_button.click(fn=lambda ids, note: tab.review(ids, ReviewStatus.REJECTED.value, note), inputs=[selection_box_holder, note_box], outputs=[action_box])
        needs_review_button.click(fn=lambda ids, note: tab.review(ids, ReviewStatus.NEEDS_REVIEW.value, note), inputs=[selection_box_holder, note_box], outputs=[action_box])
        clear_review_button.click(fn=tab.clear_review, inputs=[selection_box_holder], outputs=[action_box])
        favorite_button.click(fn=lambda ids: tab.favorite(ids, True), inputs=[selection_box_holder], outputs=[action_box])
        unfavorite_button.click(fn=lambda ids: tab.favorite(ids, False), inputs=[selection_box_holder], outputs=[action_box])

        export_json_button.click(fn=lambda *values: tab.export(_filters(*values[:-3]), values[-3], "json"), inputs=[*filter_inputs, sort_box, page_box, page_size_box], outputs=[action_box])
        export_csv_button.click(fn=lambda *values: tab.export(_filters(*values[:-3]), values[-3], "csv"), inputs=[*filter_inputs, sort_box, page_box, page_size_box], outputs=[action_box])
        export_selected_json.click(fn=lambda ids: tab.export_selected(ids, "json"), inputs=[selection_box_holder], outputs=[action_box])
        export_selected_csv.click(fn=lambda ids: tab.export_selected(ids, "csv"), inputs=[selection_box_holder], outputs=[action_box])

        def _retag(clip_id: Any, confirmed: bool) -> str:
            from core.ui_actions import retag_clip_for_ui

            return retag_clip_for_ui(settings, runner, clip_id, confirmed=confirmed)

        retag_button.click(fn=_retag, inputs=[focus_box_holder, retag_box], outputs=[action_box]).then(
            fn=tab.load_detail, inputs=[focus_box_holder], outputs=detail_outputs
        )

        def _cleanup(clip_id: Any, force: bool) -> str:
            from core.ui_actions import cleanup_clip_for_ui

            return cleanup_clip_for_ui(settings, runner, clip_id, force=force)

        cleanup_button.click(
            fn=_cleanup,
            inputs=[focus_box_holder, cleanup_force_box],
            outputs=[action_box],
        ).then(
            fn=tab.load_detail,
            inputs=[focus_box_holder],
            outputs=detail_outputs,
        )

        def _cleanup_review(
            clip_id: Any, note: str, failure_class: str, status: str
        ) -> str:
            from core.ui_actions import review_cleanup_for_ui

            return review_cleanup_for_ui(
                settings,
                runner,
                clip_id,
                status=status,
                note=note,
                failure_class=failure_class,
            )

        def _cleanup_delete(clip_id: Any, note: str) -> str:
            from core.ui_actions import delete_cleanup_derivative_for_ui

            return delete_cleanup_derivative_for_ui(
                settings, runner, clip_id, note=note
            )

        cleanup_approve_button.click(
            fn=lambda clip_id, note, failure: _cleanup_review(
                clip_id, note, failure, "approved"
            ),
            inputs=[focus_box_holder, cleanup_review_note_box, cleanup_failure_class_box],
            outputs=[action_box],
        ).then(fn=tab.load_detail, inputs=[focus_box_holder], outputs=detail_outputs)
        cleanup_reject_button.click(
            fn=lambda clip_id, note, failure: _cleanup_review(
                clip_id, note, failure, "rejected"
            ),
            inputs=[focus_box_holder, cleanup_review_note_box, cleanup_failure_class_box],
            outputs=[action_box],
        ).then(fn=tab.load_detail, inputs=[focus_box_holder], outputs=detail_outputs)
        cleanup_reset_button.click(
            fn=lambda clip_id, note, failure: _cleanup_review(
                clip_id, note, failure, "pending"
            ),
            inputs=[focus_box_holder, cleanup_review_note_box, cleanup_failure_class_box],
            outputs=[action_box],
        ).then(fn=tab.load_detail, inputs=[focus_box_holder], outputs=detail_outputs)
        cleanup_delete_button.click(
            fn=_cleanup_delete,
            inputs=[focus_box_holder, cleanup_review_note_box],
            outputs=[action_box],
        ).then(fn=tab.load_detail, inputs=[focus_box_holder], outputs=detail_outputs)

        delete_button.click(
            fn=lambda clip_id, confirmation, *values: tab.delete(
                clip_id, confirmation, _filters(*values[:-3]), values[-3], values[-2], values[-1]
            ),
            inputs=[focus_box_holder, delete_confirm_box, *filter_inputs, sort_box, page_box, page_size_box],
            outputs=[action_box, *refresh_outputs],
        )

        gr.Markdown(
            "说明：素材库只读取 SQLite 与已存文件；缩略图来自入库时保存的文件，"
            "视频仅在选中时加载。删除会同时移除数据库记录与文件，并要求输入确认语；"
            "导出清单写入 `exports/`，不会写入 `D:/素材库2`。"
        )


def build_tasks_tab(settings: AppSettings, runner: TaskRunner) -> None:
    """Render the read-only 任务记录 tab."""

    service = LibraryService(runner.library, settings)
    with gr.Tab("任务记录"):
        gr.Markdown("### 采集任务历史（只读）")
        refresh_button = gr.Button("刷新任务列表", variant="primary")
        task_table = gr.Dataframe(
            headers=TASK_TABLE_HEADERS, label="任务", wrap=True, interactive=False
        )
        task_box = gr.Dropdown(label="选择任务查看详情", choices=[], value=None)
        summary_box = gr.Markdown("")
        yields_table = gr.Dataframe(
            headers=YIELD_TABLE_HEADERS, label="搜索词产出 (search_yields)", wrap=True, interactive=False
        )
        sources_table = gr.Dataframe(
            headers=SOURCE_TABLE_HEADERS, label="来源视频", wrap=True, interactive=False
        )
        clips_table = gr.Dataframe(
            headers=["clip_id", "素材分类", "识别物料", "状态", "工序", "时长(s)", "总分"],
            label="该任务产出的片段",
            wrap=True,
            interactive=False,
        )

        def _rows() -> tuple[Any, ...]:
            try:
                rows = service.task_rows(limit=50)
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.exception("task list failed")
                return ([], gr.update(choices=[]), f"⚠ 任务列表读取失败: {exc}", [], [], [])
            table = [
                [
                    row["task_id"],
                    row["material"],
                    row["library_category"],
                    row["target"],
                    row["status"],
                    row["created_at"],
                    row["clips"],
                    row["ai_calls"],
                    row["tokens"],
                    row["queries"],
                ]
                for row in rows
            ]
            choices = [
                (f"#{row['task_id']} {row['material']} ({row['status']})", row["task_id"])
                for row in rows
            ]
            return (table, gr.update(choices=choices, value=choices[0][1] if choices else None), "任务列表已刷新。", [], [], [])

        def _detail(task_id: Any) -> tuple[Any, ...]:
            if task_id in (None, ""):
                return ("请选择一个任务。", [], [], [])
            try:
                detail = service.task_detail(int(task_id))
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.exception("task detail failed")
                return (f"⚠ 任务详情读取失败: {exc}", [], [], [])
            yields = [
                [
                    row.get("query"),
                    row.get("candidate_count"),
                    row.get("unique_candidate_count"),
                    row.get("preview_accept_count"),
                    row.get("download_count"),
                    row.get("final_clip_count"),
                ]
                for row in detail["yields"]
            ]
            sources = [
                [
                    row.get("id"),
                    row.get("platform_video_id"),
                    row.get("status"),
                    row.get("reject_reason"),
                    (row.get("title") or "")[:40],
                    row.get("author"),
                    "、".join(row.get("matched_queries") or [])[:40],
                    row.get("clips"),
                ]
                for row in detail["sources"]
            ]
            clips = [
                [
                    row.get("clip_id"),
                    row.get("library_category"),
                    row.get("material"),
                    row.get("material_state"),
                    row.get("process_stage"),
                    row.get("duration"),
                    row.get("overall_score"),
                ]
                for row in detail["clips"]
            ]
            return (task_detail_markdown(service, int(task_id)), yields, sources, clips)

        refresh_button.click(fn=_rows, outputs=[task_table, task_box, summary_box, yields_table, sources_table, clips_table])
        task_box.change(fn=_detail, inputs=[task_box], outputs=[summary_box, yields_table, sources_table, clips_table])
        gr.Markdown("任务记录为只读视图：本里程碑不包含任务队列或调度器。")


STAGE_TABLE_HEADERS = ["工序", "当前", "已批准", "收藏", "目标", "缺口", "达成率", "优先级"]
YIELD_TABLE_HEADERS_COVERAGE = [
    "搜索词",
    "次数",
    "候选",
    "唯一",
    "通过预筛",
    "下载",
    "最终片段",
    "候选→片段",
    "有效分",
]
COST_TABLE_HEADERS = ["搜索词", "来源视频", "AI 调用", "tokens", "片段", "tokens/片段", "归属"]


def build_coverage_tab(settings: AppSettings, runner: TaskRunner) -> None:
    """Render 素材覆盖 (Milestone 5, sections 16/17) - tables only, no dashboard."""

    from core.coverage import PRIORITY_LABELS, CoverageAnalyzer
    from core.library_service import LibraryService

    analyzer = CoverageAnalyzer(runner.library, settings.coverage)
    service = LibraryService(runner.library, settings)
    with gr.Tab("素材覆盖"):
        gr.Markdown("### 素材覆盖与采集策略（只读，基于 SQLite 标签）")
        with gr.Row():
            category_box = gr.Dropdown(
                label="素材分类",
                choices=[
                    ("(全部)", ""),
                    *[
                        (f"{name} ({count})", name)
                        for name, count in analyzer.categories()
                    ],
                ],
                value=(analyzer.categories()[0][0] if analyzer.categories() else ""),
            )
            refresh_button = gr.Button("刷新覆盖分析", variant="primary")
        overview_box = gr.Markdown("")
        gr.Markdown("#### 工序覆盖 / 素材缺口")
        stage_table = gr.Dataframe(
            headers=STAGE_TABLE_HEADERS, label="工序覆盖", wrap=True, interactive=False
        )
        gap_table = gr.Dataframe(
            headers=STAGE_TABLE_HEADERS, label="素材缺口（按优先级）", wrap=True, interactive=False
        )
        recommendations_box = gr.Markdown("")
        with gr.Row():
            with gr.Column():
                shot_table = gr.Dataframe(
                    headers=["景别", "数量"], label="镜头覆盖", wrap=True, interactive=False
                )
                state_table = gr.Dataframe(
                    headers=["物料状态", "数量"], label="物料状态", wrap=True, interactive=False
                )
            with gr.Column():
                role_table = gr.Dataframe(
                    headers=["剪辑角色", "数量"], label="剪辑角色覆盖", wrap=True, interactive=False
                )
                quality_table = gr.Dataframe(
                    headers=["质量区间", "数量"], label="质量分布", wrap=True, interactive=False
                )
        review_box = gr.Markdown("")
        gr.Markdown("#### 搜索词产出率与成本")
        yield_table = gr.Dataframe(
            headers=YIELD_TABLE_HEADERS_COVERAGE,
            label="搜索词产出排名（有效产出，非候选量）",
            wrap=True,
            interactive=False,
        )
        cost_table = gr.Dataframe(
            headers=COST_TABLE_HEADERS,
            label="AI 成本归属（无法可靠归属记为 unavailable）",
            wrap=True,
            interactive=False,
        )
        source_table = gr.Dataframe(
            headers=["来源视频ID", "标题", "作者", "状态", "片段", "已批准", "平均分"],
            label="来源产出",
            wrap=True,
            interactive=False,
        )
        author_table = gr.Dataframe(
            headers=["作者", "处理视频", "片段", "已批准", "平均分"],
            label="作者产出（仅统计，不自动关注/爬取）",
            wrap=True,
            interactive=False,
        )
        task_table = gr.Dataframe(
            headers=[
                "任务",
                "物料",
                "目标",
                "状态",
                "片段",
                "候选",
                "下载",
                "AI 调用",
                "tokens",
                "tokens/片段",
            ],
            label="任务产出与成本",
            wrap=True,
            interactive=False,
        )
        gr.Markdown("#### 字幕质量（Milestone 6 测量）")
        subtitle_table = gr.Dataframe(
            headers=["分类", "clean", "simple", "complex", "unknown"],
            label="按素材分类的字幕质量分布",
            wrap=True,
            interactive=False,
        )
        subtitle_insight_table = gr.Dataframe(
            headers=["搜索词", "看过候选", "预览淘汰", "字幕淘汰", "字幕淘汰率"],
            label="搜索词字幕淘汰率（可归属时）",
            wrap=True,
            interactive=False,
        )

        def _stage_rows(stages: list[Any]) -> list[list[Any]]:
            return [
                [
                    stage.stage,
                    stage.total,
                    stage.approved,
                    stage.favorite,
                    stage.target,
                    stage.missing,
                    f"{stage.ratio:.0%}",
                    PRIORITY_LABELS.get(stage.priority, stage.priority),
                ]
                for stage in stages
            ]

        def _pairs(mapping: dict[str, int]) -> list[list[Any]]:
            return [[key, value] for key, value in mapping.items()]

        def _reload(category: str) -> tuple[Any, ...]:
            resolved = (category or "").strip() or None
            try:
                report = analyzer.report(resolved)
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.exception("coverage report failed")
                empty: list[Any] = []
                return (
                    f"⚠ 覆盖分析失败: {exc}",
                    empty,  # 工序覆盖
                    empty,  # 缺口
                    "",  # 推荐
                    empty,  # 景别
                    empty,  # 状态
                    empty,  # 剪辑角色
                    empty,  # 质量
                    "",  # 审核
                    empty,  # 产出排名
                    empty,  # 成本
                    empty,  # 来源
                    empty,  # 作者
                    empty,  # 任务
                    empty,  # 字幕质量
                    empty,  # 字幕淘汰率
                )
            overview = (
                f"**分类**: {report.library_category} · 素材总数 **{report.total}** · "
                f"真实抖音 **{report.real}** · 已批准 **{report.approved}** · "
                f"收藏 **{report.favorite}**\n\n"
                f"平均分: 物料 {report.score_averages.get('material_score', 0):.2f} · "
                f"画质 {report.score_averages.get('visual_quality_score', 0):.2f} · "
                f"字幕洁净 {report.score_averages.get('subtitle_cleanliness_score', 0):.2f} · "
                f"稳定 {report.score_averages.get('stability_score', 0):.2f} · "
                f"构图 {report.score_averages.get('composition_score', 0):.2f} · "
                f"总分 {report.score_averages.get('overall_score', 0):.2f}"
            )
            review = report.review
            review_text = (
                f"**审核覆盖**: 总数 {report.total} · 已批准 {review.get('approved', 0)} · "
                f"待审核 {review.get('unreviewed', 0)} · 需复核 {review.get('needs_review', 0)} · "
                f"已拒绝 {review.get('rejected', 0)} · 收藏 {review.get('favorite', 0)}"
            )
            gaps = report.gaps
            if gaps:
                lines = ["**优先补采建议（确定性模板，不会自动执行）**", ""]
                for index, gap in enumerate(gaps[:5], start=1):
                    queries = analyzer.recommended_queries(resolved, gap.stage)
                    lines.append(
                        f"{index}. `{gap.stage}` current={gap.total} target={gap.target} "
                        f"missing={gap.missing}（{PRIORITY_LABELS.get(gap.priority, gap.priority)}）"
                    )
                    lines.append("   - " + " / ".join(queries))
                recommendations = "\n".join(lines)
            else:
                recommendations = "当前分类没有缺口：所有已配置工序都达到目标。"
            yields = analyzer.ranked_queries(limit=30)
            yield_rows = [
                [
                    row["query"],
                    row["runs"],
                    row["candidates"],
                    row["unique_candidates"],
                    row["preview_accepted"],
                    row["downloads"],
                    row["clips"],
                    f"{(row['candidate_to_clip_rate'] or 0):.1%}",
                    row["usefulness"],
                ]
                for row in yields
            ]
            cost_rows = [
                [
                    row["query"],
                    row["videos"],
                    row["ai_calls"],
                    row["tokens"],
                    row["clips"],
                    row["tokens_per_clip"] if row["tokens_per_clip"] else "n/a",
                    row["attribution"],
                ]
                for row in analyzer.query_cost_analysis()
            ]
            source_rows = [
                [
                    row["platform_video_id"],
                    row["title"][:40],
                    row["author"],
                    row["status"],
                    row["clips"],
                    row["approved"],
                    f"{row['average_score']:.2f}",
                ]
                for row in analyzer.source_yield(limit=30)
            ]
            author_rows = [
                [
                    row["author"],
                    row["videos_processed"],
                    row["clips"],
                    row["approved"],
                    f"{row['average_score']:.2f}",
                ]
                for row in analyzer.author_yield(limit=30)
            ]
            task_rows = [
                [
                    row["task_id"],
                    row["material"],
                    row["target"],
                    row["status"],
                    row["clips"],
                    row["candidates"],
                    row["downloads"],
                    row["ai_calls"],
                    row["tokens"],
                    row["tokens_per_clip"] if row["tokens_per_clip"] else "n/a",
                ]
                for row in analyzer.task_performance(limit=30)
            ]
            from core.subtitle_ops import SubtitleOps

            subtitle_ops = SubtitleOps(runner.library, settings)
            subtitle_report = subtitle_ops.report(library_category=resolved)
            subtitle_rows = [
                [
                    row["library_category"],
                    row.get("clean", 0),
                    row.get("simple", 0),
                    row.get("complex", 0),
                    row.get("unknown", 0),
                ]
                for row in subtitle_report.per_category
            ]
            subtitle_insight_rows = [
                [
                    row["query"],
                    row["candidates_seen"],
                    row["preview_rejected"],
                    row["subtitle_rejected"],
                    f"{(row['subtitle_rejection_rate'] or 0):.0%}",
                ]
                for row in subtitle_ops.search_yield_subtitle_insight(limit=15)
            ]
            return (
                overview,
                _stage_rows(report.stages),
                _stage_rows(gaps),
                recommendations,
                _pairs(report.shots),
                _pairs(report.states),
                _pairs(report.edit_roles),
                _pairs(report.quality),
                review_text,
                yield_rows,
                cost_rows,
                source_rows,
                author_rows,
                task_rows,
                subtitle_rows,
                subtitle_insight_rows,
            )

        coverage_outputs = [
            overview_box,
            stage_table,
            gap_table,
            recommendations_box,
            shot_table,
            state_table,
            role_table,
            quality_table,
            review_box,
            yield_table,
            cost_table,
            source_table,
            author_table,
            task_table,
            subtitle_table,
            subtitle_insight_table,
        ]
        refresh_button.click(fn=_reload, inputs=[category_box], outputs=coverage_outputs)
        category_box.change(fn=_reload, inputs=[category_box], outputs=coverage_outputs)
        gr.Markdown(
            "覆盖分析全部来自 SQLite 标签，不重新解码视频、不调用 AI；"
            "这里只给出建议，不会自动开始采集。"
        )


def build_system_tab(settings: AppSettings, runner: TaskRunner) -> None:
    """Render the 系统检查 tab (environment + library health)."""

    service = LibraryService(runner.library, settings)
    with gr.Tab("系统检查"):
        gr.Markdown("### 环境与素材库健康检查（只读）")
        gr.Markdown("### 火山引擎付费保护")
        with gr.Row():
            paid_run_limit = gr.Number(
                label="单次付费任务上限",
                value=settings.cloud_cleanup.max_paid_tasks_per_run,
                precision=0,
            )
            paid_day_limit = gr.Number(
                label="每日付费任务上限（UTC）",
                value=settings.cloud_cleanup.max_paid_tasks_per_day,
                precision=0,
            )
            paid_limit_button = gr.Button("应用运行时上限")
            paid_limit_save_button = gr.Button("保存为默认配置")
        paid_limit_status = gr.Markdown("")

        def _update_paid_limits(per_run: float, per_day: float) -> str:
            run_value = max(1, min(100, int(per_run)))
            day_value = max(1, min(1000, int(per_day)))
            settings.cloud_cleanup.max_paid_tasks_per_run = run_value
            settings.cloud_cleanup.max_paid_tasks_per_day = day_value
            from core.cloud_cleanup import CloudCleanupService

            used = CloudCleanupService(runner.library, settings).paid_tasks_today()
            return (
                f"已应用：单次 **{run_value}**，每日 **{day_value}**，"
                f"今日已提交 **{used}** 个付费任务。"
            )

        paid_limit_button.click(
            fn=_update_paid_limits,
            inputs=[paid_run_limit, paid_day_limit],
            outputs=[paid_limit_status],
        )

        def _save_paid_limits(per_run: float, per_day: float) -> str:
            message = _update_paid_limits(per_run, per_day)
            save_cloud_cleanup_limits(
                settings.project_root / "config.yaml",
                per_run=settings.cloud_cleanup.max_paid_tasks_per_run,
                per_day=settings.cloud_cleanup.max_paid_tasks_per_day,
            )
            return message + " 已保存到 config.yaml，重启后继续生效。"

        paid_limit_save_button.click(
            fn=_save_paid_limits,
            inputs=[paid_run_limit, paid_day_limit],
            outputs=[paid_limit_status],
        )
        check_button = gr.Button("重新检查", variant="primary")
        env_box = gr.Code(label="环境检查", language="markdown", lines=16)
        library_box = gr.Code(label="素材库检查", language="markdown", lines=18)

        def _lines(kind: str) -> str:
            try:
                if kind == "library":
                    report = service.health()
                    overview = service.overview()
                    lines = [
                        f"素材库根目录: {report['library_root']}",
                        f"数据库片段记录: {report['clips_in_db']}",
                        f"视频文件存在: {report['videos_present']}",
                        f"缺失视频: {len(report['missing_videos'])}",
                        f"缺失缩略图: {len(report['missing_thumbnails'])}",
                        f"未引用视频: {len(report['orphan_media'])}",
                        f"未引用缩略图: {len(report['orphan_thumbnails'])}",
                        f"素材总数: {overview['total']} / 真实抖音: {overview['real']} / "
                        f"待审核: {overview['unreviewed']} / 收藏: {overview['favorite']}",
                        f"导出目录: {settings.library.exports_dir}",
                    ]
                    return "\n".join(lines)
                from app import doctor

                ok, lines = doctor(settings)
                return "\n".join([*lines, "", f"环境检查: {'通过' if ok else '存在问题'}"])
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.exception("system check failed")
                return f"检查失败: {exc}"

        def _run() -> tuple[str, str]:
            return _lines("env"), _lines("library")

        check_button.click(fn=_run, outputs=[env_box, library_box])
        gr.Markdown(
            "系统检查不会修改任何文件：缺失/孤立文件只报告，不自动删除或重建。"
        )
