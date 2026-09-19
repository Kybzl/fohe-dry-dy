"""Milestone 8 acceptance helpers: media validation and plan→clip linkage.

Two read-only checks an operator (or an automated acceptance run) can use:

* :func:`validate_clip` - does this real clip really satisfy the acceptance
  contract (file inside the library, FFprobe readable, sane duration, video
  stream, thumbnail, ``provenance=douyin_real``)?
* :func:`plan_linkage` / :func:`acceptance_report` - walk the real relation
  ``collection_plan → collection_plan_item → collection_plan_tasks → tasks →
  source_videos → clips`` and report the actual ids.

Nothing here writes to the database or the filesystem.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from core.config import AppSettings
from core.models import ClipRecord
from storage.library import MaterialLibrary

LOGGER = logging.getLogger(__name__)

#: acceptance contract for a newly saved real clip (Milestone 8 section 34)
MIN_ACCEPTANCE_DURATION = 3.0
MAX_ACCEPTANCE_DURATION = 15.0
REAL_PROVENANCE = "douyin_real"


@dataclass
class ClipValidation:
    """Result of validating one clip against the acceptance contract."""

    clip_id: int | None
    file_path: str
    checks: dict[str, bool] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(self.checks.values())

    @property
    def failures(self) -> list[str]:
        return [name for name, passed in self.checks.items() if not passed]

    def lines(self) -> list[str]:
        lines = [f"素材片段 #{self.clip_id}: {self.file_path}"]
        for name, passed in self.checks.items():
            lines.append(f"  [{'ok' if passed else 'warn'}] {name}")
        if self.details:
            lines.append(f"  详情: {self.details}")
        return lines


@dataclass
class PlanLinkage:
    """The real id chain behind one plan."""

    plan_id: int
    item_ids: list[int] = field(default_factory=list)
    task_ids: list[int] = field(default_factory=list)
    source_video_ids: list[int] = field(default_factory=list)
    clip_ids: list[int] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)

    def lines(self) -> list[str]:
        return [
            f"计划 #{self.plan_id}",
            f"  → collection_plan_item: {self.item_ids or '（无）'}",
            f"  → collection_plan_tasks/tasks: {self.task_ids or '（无）'}",
            f"  → source_videos: {self.source_video_ids or '（无）'}",
            f"  → clips: {self.clip_ids or '（无）'}",
        ]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


async def validate_clip_async(
    library: MaterialLibrary,
    settings: AppSettings,
    clip_id: int,
    *,
    probe: Callable[[Path], Awaitable[Any]] | None = None,
    min_duration: float = MIN_ACCEPTANCE_DURATION,
    max_duration: float = MAX_ACCEPTANCE_DURATION,
    require_real_provenance: bool = True,
) -> ClipValidation:
    """Validate one clip; ``probe`` is injectable so tests need no FFmpeg."""

    record = library.get_clip(clip_id)
    if record is None:
        return ClipValidation(
            clip_id=clip_id, file_path="", checks={"clip_record_exists": False}
        )
    path = Path(record.file_path or "")
    library_root = Path(settings.paths.library_root)
    checks: dict[str, bool] = {
        "clip_record_exists": True,
        "file_exists": path.exists(),
        "inside_library_root": _inside(path, library_root) if path.parts else False,
    }
    details: dict[str, Any] = {
        "library_root": str(library_root),
        "provenance": record.provenance,
        "duration_recorded": record.duration,
    }
    if require_real_provenance:
        checks["provenance_is_real"] = record.provenance == REAL_PROVENANCE

    thumbnail = Path(record.thumbnail_path) if record.thumbnail_path else None
    checks["thumbnail_exists"] = bool(thumbnail and thumbnail.exists())

    info = None
    if path.exists():
        if probe is None:
            from core.dependencies import build_toolkit

            probe = build_toolkit(settings).probe
        try:
            info = await probe(path)
        except Exception as exc:  # pragma: no cover - depends on the environment
            LOGGER.warning("ffprobe failed for clip %s: %s", clip_id, exc)
            info = None
    checks["ffprobe_readable"] = info is not None
    if info is not None:
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        has_video = getattr(info, "has_video", False)
        if callable(has_video):
            has_video = has_video()
        details["duration"] = round(duration, 3)
        details["width"] = getattr(info, "width", None)
        details["height"] = getattr(info, "height", None)
        checks["duration_in_range"] = min_duration <= duration <= max_duration
        checks["video_stream"] = bool(has_video)
    else:
        checks["duration_in_range"] = False
        checks["video_stream"] = False
    return ClipValidation(
        clip_id=clip_id, file_path=str(path), checks=checks, details=details
    )


def validate_clip(
    library: MaterialLibrary,
    settings: AppSettings,
    clip_id: int,
    **kwargs: Any,
) -> ClipValidation:
    """Synchronous wrapper around :func:`validate_clip_async`."""

    import asyncio

    return asyncio.run(validate_clip_async(library, settings, clip_id, **kwargs))


def plan_linkage(library: MaterialLibrary, plan_id: int) -> PlanLinkage:
    """Walk ``plan → item → task → source → clip`` for one plan (section 37)."""

    rows = library.database.query(
        "SELECT cpt.plan_id, cpt.plan_item_id, cpt.task_id, cpt.query, "
        "       sv.id AS source_video_id, sv.status AS source_status, "
        "       c.id AS clip_id, c.process_stage, c.file_path, c.provenance, "
        "       c.review_status "
        "FROM collection_plan_tasks cpt "
        "LEFT JOIN source_videos sv ON sv.task_id = cpt.task_id "
        "LEFT JOIN clips c ON c.source_video_id = sv.id "
        "WHERE cpt.plan_id = ? "
        "ORDER BY cpt.id, c.id",
        (int(plan_id),),
    )
    linkage = PlanLinkage(plan_id=int(plan_id))
    for row in rows:
        entry = dict(row)
        linkage.rows.append(entry)
        for key, bucket in (
            ("plan_item_id", linkage.item_ids),
            ("task_id", linkage.task_ids),
            ("source_video_id", linkage.source_video_ids),
            ("clip_id", linkage.clip_ids),
        ):
            value = entry.get(key)
            if value is not None and int(value) not in bucket:
                bucket.append(int(value))
    return linkage


def acceptance_report(
    library: MaterialLibrary,
    settings: AppSettings,
    plan_id: int,
    *,
    probe: Callable[[Path], Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """Linkage + clip validation for one plan (sections 34/37)."""

    import asyncio

    linkage = plan_linkage(library, plan_id)
    validations = [
        asyncio.run(validate_clip_async(library, settings, clip_id, probe=probe))
        for clip_id in linkage.clip_ids
    ]
    return {
        "plan_id": plan_id,
        "linkage": linkage,
        "clips": validations,
        "clips_ok": sum(1 for entry in validations if entry.ok),
        "clips_total": len(validations),
    }
