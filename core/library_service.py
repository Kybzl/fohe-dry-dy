"""Operator-facing material library service (Milestone 4).

Everything the 素材库 tab needs, without putting UI code into storage:

* build a :class:`ClipQuery` from simple widget values (empty = no restriction)
* page / sort / count in SQLite (never load the whole library into memory)
* library + category statistics
* JSON / CSV manifest export under ``E:/Codex/fohe-dy/exports``
* task history rows and per-clip AI audit information
* safe media paths for the video player and thumbnails

The service is deliberately free of Gradio imports so it can be tested and
reused from the CLI.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.config import AppSettings
from core.models import (
    CameraMotion,
    ClipQuery,
    ClipRecord,
    EditRole,
    MaterialForm,
    MaterialState,
    PersonRole,
    ProcessStage,
    ReviewStatus,
    ShotType,
    SubtitleType,
    clip_sort_choices,
)
from core.provenance import DOUYIN_REAL, classify_provenance
from storage.library import MaterialLibrary

LOGGER = logging.getLogger(__name__)

#: fields written to an export manifest (section 17)
EXPORT_FIELDS: tuple[str, ...] = (
    "clip_id",
    "file_path",
    "thumbnail_path",
    "library_category",
    "material",
    "material_form",
    "material_state",
    "process_stage",
    "equipment_type",
    "equipment_visible",
    "shot_type",
    "camera_motion",
    "people",
    "subtitle_type",
    "edit_roles",
    "duration",
    "overall_score",
    "review_status",
    "favorite",
    "provenance",
    "source_platform",
    "source_video_id",
    "source_url",
)


@dataclass
class ClipFilters:
    """Raw widget values; an empty value means "no restriction"."""

    library_category: str = ""
    material: str = ""
    material_form: str = ""
    material_state: str = ""
    process_stage: str = ""
    equipment_type: str = ""
    equipment_visible: str = ""
    shot_type: str = ""
    camera_motion: str = ""
    people: str = ""
    person_role: str = ""
    subtitle_type: list[str] = field(default_factory=list)
    edit_role: str = ""
    provenance: str = ""
    tag_prompt_version: str = ""
    review_status: list[str] = field(default_factory=list)
    favorite: str = ""
    min_overall_score: float | None = None
    max_overall_score: float | None = None
    min_duration: float | None = None
    max_duration: float | None = None
    source_platform: str = ""
    created_after: str = ""
    created_before: str = ""
    free_text: str = ""


@dataclass
class ClipPage:
    """One server-side page of clips plus everything the header shows."""

    clips: list[ClipRecord] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    total_seconds: float = 0.0

    @property
    def page_count(self) -> int:
        if self.page_size <= 0:
            return 1
        return max(1, (self.total + self.page_size - 1) // self.page_size)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.page_count

    def summary(self) -> str:
        if not self.total:
            return "没有符合条件的素材。"
        first = self.offset + 1
        last = self.offset + len(self.clips)
        return (
            f"第 {self.page}/{self.page_count} 页 · {first}-{last} / 共 {self.total} 条 · "
            f"筛选结果总时长 {self.total_seconds:.1f}s"
        )


def _parse_choice(value: Any, enum_type: Any) -> Any:
    """Tolerant enum parsing for widget values ('' / None / unknown -> None)."""

    if value in (None, "", "全部", "不限"):
        return None
    try:
        return enum_type(value)
    except ValueError:
        return None


def _parse_bool(value: Any) -> bool | None:
    if value in (None, "", "全部", "不限"):
        return None
    lowered = str(value).strip().lower()
    if lowered in ("true", "yes", "1", "是", "有"):
        return True
    if lowered in ("false", "no", "0", "否", "无"):
        return False
    return None


#: the only keys a filter preset may contain - a preset never stores SQL
PRESET_FIELDS: frozenset[str] = frozenset(ClipFilters.__dataclass_fields__)


def preset_from_filters(filters: ClipFilters) -> dict[str, Any]:
    """Serialise a filter set, dropping empty values (section 21)."""

    payload: dict[str, Any] = {}
    for key, value in vars(filters).items():
        if key not in PRESET_FIELDS:  # pragma: no cover - defensive
            continue
        if value in (None, "", [], {}):
            continue
        payload[key] = value
    return payload


def preset_to_filters(preset: Mapping[str, Any]) -> ClipFilters:
    """Validate a stored preset and rebuild the filter set.

    Unknown keys are rejected instead of being passed through, so a corrupted
    or hand-edited preset can never inject anything into a query.
    """

    payload = dict(preset.get("filters") or {}) if isinstance(preset, Mapping) else {}
    unknown = set(payload) - PRESET_FIELDS
    if unknown:
        raise ValueError(f"preset contains unsupported keys: {sorted(unknown)}")
    return ClipFilters(**payload)


class LibraryService:
    """Read/write facade for the material library UI and CLI."""

    def __init__(self, library: MaterialLibrary, settings: AppSettings) -> None:
        self.library = library
        self.settings = settings

    # -- query building ----------------------------------------------------
    def build_query(
        self,
        filters: ClipFilters | None = None,
        *,
        sort_by: str = "newest",
        sort_direction: str | None = None,
        page: int = 1,
        page_size: int | str | None = None,
    ) -> ClipQuery:
        f = filters or ClipFilters()
        size = self.settings.library.clamp_page_size(page_size)
        page_number = max(1, int(page or 1))
        payload: dict[str, Any] = {
            "library_category": (f.library_category or "").strip() or None,
            "provenance": (f.provenance or "").strip() or None,
            "tag_prompt_version": (f.tag_prompt_version or "").strip() or None,
            "source_platform": (f.source_platform or "").strip() or None,
            "material": (f.material or "").strip() or None,
            "material_form": _parse_choice(f.material_form, MaterialForm),
            "material_state": _parse_choice(f.material_state, MaterialState),
            "process_stage": _parse_choice(f.process_stage, ProcessStage),
            "equipment_type": (f.equipment_type or "").strip() or None,
            "equipment_visible": _parse_bool(f.equipment_visible),
            "shot_type": _parse_choice(f.shot_type, ShotType),
            "camera_motion": _parse_choice(f.camera_motion, CameraMotion),
            "people": _parse_bool(f.people),
            "person_role": _parse_choice(f.person_role, PersonRole),
            "subtitle_type": [
                SubtitleType(value)
                for value in (f.subtitle_type or [])
                if value not in (None, "", "全部", "不限")
            ]
            or None,
            "edit_role": _parse_choice(f.edit_role, EditRole),
            "review_status": [
                ReviewStatus(value)
                for value in (f.review_status or [])
                if value not in (None, "", "全部", "不限")
            ]
            or None,
            "favorite": _parse_bool(f.favorite),
            "free_text": (f.free_text or "").strip() or None,
            "min_overall_score": f.min_overall_score,
            "max_overall_score": f.max_overall_score,
            "min_duration": f.min_duration,
            "max_duration": f.max_duration,
            "created_after": (f.created_after or "").strip() or None,
            "created_before": (f.created_before or "").strip() or None,
            "sort_by": sort_by or "newest",
            "offset": (page_number - 1) * size,
            "limit": size,
        }
        if sort_direction:
            payload["sort_direction"] = sort_direction
        return ClipQuery(**payload)

    def fetch_page(
        self,
        filters: ClipFilters | None = None,
        *,
        sort_by: str = "newest",
        sort_direction: str | None = None,
        page: int = 1,
        page_size: int | str | None = None,
    ) -> ClipPage:
        """One page of results, counted and sliced in SQLite (section 4)."""

        query = self.build_query(
            filters,
            sort_by=sort_by,
            sort_direction=sort_direction,
            page=page,
            page_size=page_size,
        )
        stats = self.library.clip_query_stats(query)
        clips = self.library.query_clips(query)
        size = int(query.limit)
        page_count = max(1, (int(stats["count"]) + size - 1) // size)
        page_number = min(max(1, int(page or 1)), page_count)
        if page_number != int(page or 1):
            # the requested page was past the end: fall back to the last real page
            query = self.build_query(
                filters,
                sort_by=sort_by,
                sort_direction=sort_direction,
                page=page_number,
                page_size=size,
            )
            clips = self.library.query_clips(query)
        return ClipPage(
            clips=clips,
            total=int(stats["count"]),
            page=page_number,
            page_size=size,
            total_seconds=float(stats["duration_seconds"]),
        )

    # -- statistics --------------------------------------------------------
    def overview(self, filters: ClipFilters | None = None) -> dict[str, Any]:
        """Header numbers (section 22) - a handful of cheap COUNT queries."""

        f = filters or ClipFilters()
        filtered = self.library.clip_query_stats(self.build_query(f))
        real = self.library.clip_query_stats(
            self.build_query(ClipFilters(provenance=DOUYIN_REAL))
        )
        counts = self.library.review_counts()
        return {
            "total": self.library.count_clips(),
            "real": int(real["count"]),
            "unreviewed": int(counts.get(ReviewStatus.UNREVIEWED.value, 0)),
            "approved": int(counts.get(ReviewStatus.APPROVED.value, 0)),
            "favorite": int(counts.get("favorite", 0)),
            "filtered": int(filtered["count"]),
            "filtered_seconds": float(filtered["duration_seconds"]),
        }

    def category_counts(self, filters: ClipFilters | None = None) -> list[tuple[str, int]]:
        f = filters or ClipFilters()
        return self.library.category_counts(provenance=(f.provenance or None))

    def sort_choices(self) -> tuple[tuple[str, str], ...]:
        return clip_sort_choices()

    def default_filters(self) -> ClipFilters:
        """The operator's default view: real material, everything else open."""

        return ClipFilters(
            provenance=self.settings.library.default_provenance_filter or ""
        )

    def config_choices(self) -> dict[str, Any]:
        """Small value lists for the filter widgets."""

        return {
            "page_sizes": self.settings.library.page_sizes(),
            "default_page_size": self.settings.library.clamp_page_size(None),
            "default_provenance": self.settings.library.default_provenance_filter,
        }

    # -- media access ------------------------------------------------------
    def thumbnail_path(self, clip: ClipRecord) -> Path | None:
        """Stored thumbnail inside the library root, else ``None``."""

        path = self.library.safe_media_path(clip.thumbnail_path)
        if path is None or not path.exists():
            return None
        return path

    def video_path(self, clip: ClipRecord) -> Path | None:
        """Playable file inside the library root, else ``None`` (section 7)."""

        path = self.library.safe_media_path(clip.file_path)
        if path is None or not path.exists():
            return None
        return path

    def preferred_video_path(self, clip: ClipRecord) -> Path | None:
        """Successful subtitle-clean derivative when healthy, else original.

        Milestone 9.2: a derivative is a playback/edit preference only; the
        original clip row and its semantic metadata stay authoritative.
        """

        path = self.library.safe_media_path(
            self.library.preferred_media_path(
                clip,
                require_review=bool(
                    self.settings.subtitle_cleanup.require_review_before_preferred
                ),
            )
        )
        if path is None or not path.exists():
            return self.video_path(clip)
        return path

    def missing_files(self, clip: ClipRecord) -> list[str]:
        problems: list[str] = []
        if self.video_path(clip) is None:
            problems.append("missing_file")
        if clip.thumbnail_path and self.thumbnail_path(clip) is None:
            problems.append("missing_thumbnail")
        return problems

    # -- export (sections 17/18) -------------------------------------------
    def record(self, clip: ClipRecord) -> dict[str, Any]:
        """One export/table row (generic manifest, not an edit decision list)."""

        provenance = clip.provenance or classify_provenance(
            clip.platform, clip.platform_video_id, source_url=clip.source_url
        )
        preferred = self.library.safe_media_path(
            self.library.preferred_media_path(
                clip,
                require_review=bool(
                    self.settings.subtitle_cleanup.require_review_before_preferred
                ),
            )
        )
        original = self.library.safe_media_path(clip.file_path)
        preferred_kind = "original"
        if preferred is not None and original is not None:
            try:
                if preferred.resolve() != original.resolve():
                    preferred_kind = "cleaned"
            except OSError:  # pragma: no cover - defensive
                preferred_kind = "original"
        return {
            "clip_id": clip.id,
            # a human-rejected clip is never treated as usable production media
            "usable_for_production": str(clip.review_status) != "rejected",
            "file_path": str(clip.file_path),
            "preferred_media_path": str(preferred) if preferred else str(clip.file_path),
            "preferred_media_kind": preferred_kind,
            "thumbnail_path": str(clip.thumbnail_path) if clip.thumbnail_path else "",
            "library_category": clip.library_category or "未记录",
            "material": clip.material,
            "material_form": str(clip.material_form),
            "material_state": str(clip.material_state),
            "process_stage": str(clip.process_stage),
            "equipment_type": clip.equipment_type or "",
            "equipment_visible": bool(clip.equipment_visible),
            "shot_type": str(clip.shot_type),
            "camera_motion": str(clip.camera_motion),
            "people": bool(clip.people),
            "subtitle_type": str(clip.subtitle_type),
            "edit_roles": [str(role) for role in clip.edit_roles],
            "duration": round(float(clip.duration or 0.0), 3),
            "overall_score": round(float(clip.overall_score or 0.0), 3),
            "review_status": str(clip.review_status),
            "favorite": bool(clip.favorite),
            "provenance": provenance,
            "source_platform": clip.platform,
            "source_video_id": clip.platform_video_id,
            "source_url": clip.source_url,
        }

    def export_records(
        self,
        clips: Sequence[ClipRecord],
        *,
        fmt: str,
        filters: ClipFilters | None = None,
        tag: str = "",
    ) -> dict[str, Any]:
        """Write a JSON/CSV manifest under the project ``exports/`` folder."""

        rows = [self.record(clip) for clip in clips]
        limit = int(self.settings.library.max_export_rows)
        if len(rows) > limit:
            return {
                "ok": False,
                "error": f"导出结果 {len(rows)} 行超过上限 {limit}，请缩小筛选范围后重试。",
                "rows": len(rows),
                "limit": limit,
            }
        directory = Path(self.settings.library.exports_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        suffix = "csv" if str(fmt).lower() == "csv" else "json"
        name = f"material-manifest-{stamp}{('-' + tag) if tag else ''}.{suffix}"
        path = directory / name
        if suffix == "json":
            payload = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "library_root": str(self.settings.paths.library_root),
                "filters": vars(filters) if filters else {},
                "count": len(rows),
                "clips": rows,
            }
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        else:
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(EXPORT_FIELDS))
                writer.writeheader()
                for row in rows:
                    writer.writerow({key: row.get(key) for key in EXPORT_FIELDS})
        LOGGER.info("exported %s clip(s) to %s", len(rows), path)
        return {"ok": True, "path": str(path), "rows": len(rows), "format": suffix}

    def export_filtered(
        self,
        filters: ClipFilters,
        *,
        fmt: str,
        sort_by: str = "newest",
        max_rows: int | None = None,
    ) -> dict[str, Any]:
        """Export every clip matching the current filters (bounded)."""

        limit = int(max_rows or self.settings.library.max_export_rows)
        query = self.build_query(filters, sort_by=sort_by, page=1, page_size=1)
        total = self.library.count_clips_matching(query)
        if total > limit:
            return {
                "ok": False,
                "error": f"筛选结果 {total} 条超过导出上限 {limit}，请缩小筛选范围。",
                "rows": total,
                "limit": limit,
            }
        collected: list[ClipRecord] = []
        page_size = max(1, min(self.settings.library.max_page_size, limit))
        while len(collected) < total:
            batch_query = self.build_query(filters, sort_by=sort_by, page=1, page_size=page_size)
            batch_query.offset = len(collected)
            batch_query.limit = page_size
            batch = self.library.query_clips(batch_query)
            if not batch:
                break
            collected.extend(batch)
        return self.export_records(collected, fmt=fmt, filters=filters, tag="filtered")

    def export_by_ids(self, clip_ids: Sequence[int], *, fmt: str) -> dict[str, Any]:
        """Export explicitly selected clips (section 16/17)."""

        clips = [clip for clip in (self.library.get_clip(int(i)) for i in clip_ids) if clip]
        if not clips:
            return {"ok": False, "error": "没有可导出的素材（请先选择素材）。", "rows": 0}
        return self.export_records(clips, fmt=fmt, tag="selected")

    # -- task history (section 27) -----------------------------------------
    def task_rows(self, limit: int = 50) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for task in self.library.list_tasks(limit=limit):
            clips = self.library.clips_for_task(int(task.id or 0), limit=1000)
            usage = self.library.ai_usage_summary(task_id=task.id)
            yields = self.library.list_search_yields(task.id, limit=1000)
            categories = sorted(
                {clip.library_category for clip in clips if clip.library_category}
            )
            rows.append(
                {
                    "task_id": task.id,
                    "material": task.material,
                    "library_category": " / ".join(categories) or task.material,
                    "target": task.target_clip_count,
                    "status": str(task.status),
                    "created_at": task.created_at.strftime("%Y-%m-%d %H:%M")
                    if task.created_at
                    else "",
                    "clips": len(clips),
                    "ai_calls": int(usage.get("ai_calls") or 0),
                    "tokens": int(usage.get("total_tokens") or 0),
                    "queries": len(yields),
                    "error": task.error or "",
                }
            )
        return rows

    def task_detail(self, task_id: int) -> dict[str, Any]:
        """Read-only detail: search yields, source videos and produced clips."""

        clips = self.library.clips_for_task(task_id, limit=1000)
        yields = self.library.list_search_yields(task_id, limit=1000)
        sources = self.library.list_source_videos(task_id=task_id, limit=500)
        return {
            "clips": [self.record(clip) for clip in clips],
            "yields": yields,
            "sources": [
                {
                    "id": source.id,
                    "platform_video_id": source.platform_video_id,
                    "status": str(source.status),
                    "reject_reason": str(source.reject_reason)
                    if source.reject_reason
                    else "",
                    "title": source.title,
                    "author": source.author,
                    "matched_queries": list(source.matched_queries),
                    "clips": len(
                        self.library.clips_for_source_video(int(source.id or 0))
                    ),
                }
                for source in sources
            ],
        }

    def source_detail(self, clip: ClipRecord) -> dict[str, Any]:
        """Source provenance panel for one clip (sections 9/35)."""

        if clip.source_video_id is None:
            return {}
        row = self.library.get_source_video_by_id(int(clip.source_video_id))
        if row is None:
            return {}
        return {
            "id": row.id,
            "platform_video_id": row.platform_video_id,
            "title": row.title,
            "author": row.author,
            "author_id": row.author_id,
            "publish_time": row.publish_time.isoformat() if row.publish_time else "",
            "source_url": row.source_url,
            "status": str(row.status),
            "reject_reason": str(row.reject_reason) if row.reject_reason else "",
            "matched_queries": list(row.matched_queries),
            "clips": len(self.library.clips_for_source_video(int(row.id or 0))),
        }

    def ai_audit(self, clip_id: int) -> list[dict[str, Any]]:
        """Compact AI rows for one clip, newest first, no secrets (section 28)."""

        runs = self.library.ai_runs_for_clip(clip_id)
        runs.reverse()
        return [
            {
                "id": run.get("id"),
                "operation": run.get("operation"),
                "provider": run.get("provider"),
                "model": run.get("model"),
                "prompt_version": run.get("prompt_version"),
                "latency_ms": run.get("latency_ms"),
                "total_tokens": run.get("total_tokens"),
                "origin": run.get("origin"),
                "status": run.get("status"),
                "created_at": run.get("created_at"),
                "result_json": run.get("result_json"),
            }
            for run in runs
        ]

    # -- health ------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        return self.library.health_report()

    # -- review ------------------------------------------------------------
    def set_review(
        self,
        clip_ids: Sequence[int],
        *,
        status: ReviewStatus | str | None = None,
        note: str | None = None,
    ) -> int:
        return self.library.set_review(clip_ids, status=status, note=note)

    def set_favorite(self, clip_ids: Sequence[int], favorite: bool) -> int:
        return self.library.set_favorite(clip_ids, favorite)
