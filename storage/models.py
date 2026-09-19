"""Mapping helpers between SQLite rows and the Pydantic domain models."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from core.models import (
    CameraMotion,
    ClipRecord,
    ClipTagging,
    EditRole,
    MaterialForm,
    MaterialState,
    PersonRole,
    ProcessStage,
    RejectReason,
    ReviewStatus,
    ShotType,
    SourceVideoRecord,
    SourceVideoStatus,
    SubtitlePolicy,
    SubtitleType,
    TaskRecord,
    TaskStatus,
)


def _enum(enum_type: Any, value: Any, default: Any) -> Any:
    """Parse an enum value, falling back to ``default`` on unknown input."""

    if value is None:
        return default
    try:
        return enum_type(value)
    except ValueError:
        return default


def _field(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """Read a column that may not exist yet in an older database file."""

    try:
        keys = row.keys()
    except AttributeError:  # pragma: no cover - defensive
        return default
    return row[key] if key in keys else default


def _clip_subtitle_analysis(row: sqlite3.Row) -> dict[str, Any] | None:
    """Parse ``clips.subtitle_analysis_json`` (Milestone 6) defensively."""

    raw = _field(row, "subtitle_analysis_json")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):  # pragma: no cover - defensive
        return None
    return payload if isinstance(payload, dict) else None


def clip_row_values(
    *,
    task_id: int | None,
    source_video_id: int | None,
    platform: str,
    platform_video_id: str,
    source_url: str,
    tagging: ClipTagging,
    duration: float,
    width: int | None,
    height: int | None,
    fps: float | None,
    source_start: float,
    source_end: float,
    file_path: str,
    thumbnail_path: str | None,
    phash: str | None,
    sha256: str | None,
    content_key: str | None,
    created_at: str,
    source_title: str = "",
    source_author: str = "",
    source_author_id: str | None = None,
    source_publish_time: str | None = None,
    library_category: str = "",
    provenance: str = "",
    tag_prompt_version: str = "",
    subtitle_analysis_json: str | None = None,
) -> dict[str, Any]:
    """Build one ``clips`` insert payload from tagging plus artifact data."""

    return {
        "task_id": task_id,
        "source_video_id": source_video_id,
        "platform": platform,
        "platform_video_id": platform_video_id,
        "source_url": source_url,
        "material": tagging.material,
        "material_form": str(tagging.material_form),
        "material_state": str(tagging.material_state),
        "process_stage": str(tagging.process_stage),
        "equipment_type": tagging.equipment_type,
        "equipment_visible": int(tagging.equipment_visible),
        "scene": tagging.scene,
        "shot_type": str(tagging.shot_type),
        "camera_motion": str(tagging.camera_motion),
        "people": int(tagging.people),
        "people_count": tagging.people_count,
        "person_role": str(tagging.person_role),
        "subtitle_type": str(tagging.subtitle_type),
        "subtitle_score": tagging.subtitle_score,
        "edit_roles": json.dumps([str(role) for role in tagging.edit_roles], ensure_ascii=False),
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "material_score": tagging.scores.material_relevance,
        "visual_quality_score": tagging.scores.visual_quality,
        "subtitle_cleanliness_score": tagging.scores.subtitle_cleanliness,
        "stability_score": tagging.scores.stability,
        "composition_score": tagging.scores.composition,
        "overall_score": tagging.scores.overall,
        "description": tagging.description,
        "source_title": source_title,
        "source_author": source_author,
        "source_author_id": source_author_id,
        "source_publish_time": source_publish_time,
        # Milestone 3.7: physical category, asset provenance and the prompt
        # version that produced the semantic tags (audit + safe cleanup)
        "library_category": library_category,
        "provenance": provenance,
        "tag_prompt_version": tag_prompt_version,
        "subtitle_analysis_json": subtitle_analysis_json,
        "source_start": source_start,
        "source_end": source_end,
        "file_path": file_path,
        "thumbnail_path": thumbnail_path,
        "phash": phash,
        "sha256": sha256,
        "content_key": content_key,
        "created_at": created_at,
    }


def tags_from_tagging(tagging: ClipTagging) -> list[tuple[str, str, float, str]]:
    """Flatten a ``ClipTagging`` into ``(name, category, confidence, source)``."""

    rows: list[tuple[str, str, float, str]] = [
        (tagging.material, "material", 1.0, "ai"),
        (str(tagging.material_form), "material_form", 1.0, "ai"),
        (str(tagging.material_state), "material_state", 1.0, "ai"),
        (str(tagging.process_stage), "process_stage", 1.0, "ai"),
        (str(tagging.shot_type), "shot_type", 1.0, "ai"),
        (str(tagging.camera_motion), "camera_motion", 1.0, "ai"),
        (str(tagging.subtitle_type), "subtitle_type", 1.0, "ai"),
    ]
    if tagging.equipment_type:
        rows.append((tagging.equipment_type, "equipment_type", 1.0, "ai"))
    if tagging.scene:
        rows.append((tagging.scene, "scene", 1.0, "ai"))
    if tagging.person_role is not PersonRole.NONE:
        rows.append((str(tagging.person_role), "person_role", 1.0, "ai"))
    if tagging.people and tagging.people_count > 0:
        rows.append(("people_present", "people", 1.0, "ai"))
    for role in tagging.edit_roles:
        rows.append((str(role), "edit_role", 1.0, "ai"))
    return rows


def _parse_edit_roles(value: Any) -> list[EditRole]:
    """Parse the stored ``edit_roles`` JSON/CSV value."""

    if not value:
        return []
    raw: list[Any]
    if isinstance(value, str):
        try:
            raw = json.loads(value)
        except json.JSONDecodeError:
            raw = [item for item in value.split(",") if item]
    else:
        raw = list(value)
    roles: list[EditRole] = []
    for item in raw:
        try:
            roles.append(EditRole(item))
        except ValueError:
            continue
    return roles


def row_to_clip(row: sqlite3.Row, tags: Iterable[str] = ()) -> ClipRecord:
    """Map a ``clips`` row (plus its tag names) to a ``ClipRecord``."""

    return ClipRecord(
        id=row["id"],
        task_id=row["task_id"],
        source_video_id=row["source_video_id"],
        platform=row["platform"] or "",
        platform_video_id=row["platform_video_id"] or "",
        source_url=row["source_url"] or "",
        source_title=_field(row, "source_title", "") or "",
        source_author=_field(row, "source_author", "") or "",
        source_author_id=_field(row, "source_author_id"),
        source_publish_time=_field(row, "source_publish_time"),
        source_start=row["source_start"],
        source_end=row["source_end"],
        library_category=_field(row, "library_category", "") or "",
        provenance=_field(row, "provenance", "") or "",
        tag_prompt_version=_field(row, "tag_prompt_version", "") or "",
        subtitle_analysis=_clip_subtitle_analysis(row),
        review_status=_enum(
            ReviewStatus, _field(row, "review_status", ""), ReviewStatus.UNREVIEWED
        ),
        review_note=_field(row, "review_note", "") or "",
        favorite=bool(_field(row, "favorite", 0) or 0),
        material=row["material"],
        material_form=_enum(MaterialForm, row["material_form"], MaterialForm.UNKNOWN),
        material_state=_enum(MaterialState, row["material_state"], MaterialState.UNKNOWN),
        process_stage=_enum(ProcessStage, row["process_stage"], ProcessStage.OTHER),
        equipment_type=row["equipment_type"],
        equipment_visible=bool(row["equipment_visible"]),
        scene=row["scene"] or "",
        shot_type=_enum(ShotType, row["shot_type"], ShotType.UNKNOWN),
        camera_motion=_enum(CameraMotion, row["camera_motion"], CameraMotion.UNKNOWN),
        people=bool(row["people"]),
        people_count=int(row["people_count"] or 0),
        person_role=_enum(PersonRole, row["person_role"], PersonRole.NONE),
        subtitle_type=_enum(SubtitleType, row["subtitle_type"], SubtitleType.UNKNOWN),
        subtitle_score=float(row["subtitle_score"] or 0.0),
        edit_roles=_parse_edit_roles(row["edit_roles"]),
        description=row["description"] or "",
        duration=float(row["duration"] or 0.0),
        width=row["width"],
        height=row["height"],
        fps=row["fps"],
        material_score=float(row["material_score"] or 0.0),
        visual_quality_score=float(row["visual_quality_score"] or 0.0),
        subtitle_cleanliness_score=float(_field(row, "subtitle_cleanliness_score", 0.0) or 0.0),
        stability_score=float(_field(row, "stability_score", 0.0) or 0.0),
        composition_score=float(_field(row, "composition_score", 0.0) or 0.0),
        overall_score=float(row["overall_score"] or 0.0),
        file_path=row["file_path"],
        thumbnail_path=row["thumbnail_path"],
        phash=row["phash"],
        sha256=row["sha256"],
        content_key=_field(row, "content_key"),
        tags=list(tags),
        created_at=row["created_at"],
    )


def row_to_task(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        id=row["id"],
        material=row["material"],
        target_clip_count=row["target_clip_count"],
        min_clip_duration=row["min_clip_duration"],
        max_clip_duration=row["max_clip_duration"],
        subtitle_policy=_enum(SubtitlePolicy, row["subtitle_policy"], SubtitlePolicy.STRICT),
        status=_enum(TaskStatus, row["status"], TaskStatus.PENDING),
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def row_to_source_video(row: sqlite3.Row) -> SourceVideoRecord:
    reason = None
    if row["reject_reason"]:
        reason = _enum(RejectReason, row["reject_reason"], None)
    matched: list[str] = []
    raw_matched = _field(row, "matched_queries")
    if raw_matched:
        try:
            matched = [str(item) for item in json.loads(raw_matched)]
        except (json.JSONDecodeError, TypeError):
            matched = [item for item in str(raw_matched).split(",") if item]
    stats: dict[str, Any] = {}
    raw_stats = _field(row, "source_stats_json")
    if raw_stats:
        try:
            parsed = json.loads(raw_stats)
            stats = parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            stats = {}
    return SourceVideoRecord(
        id=row["id"],
        task_id=row["task_id"],
        platform=row["platform"],
        platform_video_id=row["platform_video_id"],
        source_url=row["source_url"],
        title=row["title"] or "",
        author=row["author"] or "",
        author_id=_field(row, "author_id"),
        cover_url=_field(row, "cover_url"),
        publish_time=_field(row, "publish_time"),
        duration=row["duration"],
        status=_enum(SourceVideoStatus, row["status"], SourceVideoStatus.CANDIDATE),
        reject_reason=reason,
        preview_material_score=row["preview_material_score"],
        preview_subtitle_score=row["preview_subtitle_score"],
        preview_quality_score=row["preview_quality_score"],
        media_url=_field(row, "media_url"),
        matched_queries=matched,
        statistics=stats,
        attempt_count=int(_field(row, "attempt_count", 0) or 0),
        last_attempt_at=_field(row, "last_attempt_at"),
        created_at=row["created_at"],
    )
