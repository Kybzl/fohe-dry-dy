"""Safe library maintenance operations (Milestone 5, sections 23-29).

Every operation here follows the same contract:

* **dry-run by default** - nothing changes until the caller confirms
* the exact changes are reported back (old -> new / original -> quarantine path)
* the operation writes one row to ``maintenance_log`` (never ``ai_runs``)
* AI tags and scores are never modified by maintenance

Nothing in this module deletes an orphan asset: orphans are moved to
``quarantine/`` so the operator can inspect them.
"""

from __future__ import annotations

import csv
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from core.config import AppSettings
from core.models import ClipRecord, ReviewStatus
from core.paths import is_within, resolve_candidate, resolve_within
from media.ffmpeg import MediaToolkit
from storage.library import MaterialLibrary

LOGGER = logging.getLogger(__name__)

#: columns of the review CSV (section 23)
REVIEW_CSV_FIELDS: tuple[str, ...] = (
    "clip_id",
    "review_status",
    "review_note",
    "favorite",
)

TRUE_WORDS = {"1", "true", "yes", "y", "是", "✓", "★"}
FALSE_WORDS = {"0", "false", "no", "n", "否", ""}


@dataclass
class MaintenanceReport:
    """Outcome of one maintenance operation."""

    operation: str
    dry_run: bool = True
    items: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    log_id: int | None = None

    @property
    def changed(self) -> int:
        return len(self.items)

    def summary(self) -> str:
        verb = "将执行" if self.dry_run else "已执行"
        text = f"{verb} {self.operation}: {self.changed} 项"
        if self.skipped:
            text += f"，跳过 {len(self.skipped)} 项"
        if self.errors:
            text += f"，错误 {len(self.errors)} 项"
        return text


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


class LibraryOps:
    """Operator maintenance on top of :class:`MaterialLibrary`."""

    def __init__(
        self,
        library: MaterialLibrary,
        settings: AppSettings,
        *,
        toolkit: MediaToolkit | None = None,
    ) -> None:
        self.library = library
        self.settings = settings
        self.toolkit = toolkit

    # -- helpers -----------------------------------------------------------
    def _toolkit(self) -> MediaToolkit:
        if self.toolkit is None:
            from core.dependencies import build_toolkit

            self.toolkit = build_toolkit(
                self.settings, backend=self.settings.media.backend
            )
        return self.toolkit

    def _log(self, report: MaintenanceReport) -> None:
        if report.dry_run:
            return
        try:
            report.log_id = self.library.log_maintenance(
                report.operation,
                target_type="library",
                target_id=None,
                details={
                    "changed": report.changed,
                    "skipped": len(report.skipped),
                    "errors": report.errors[:5],
                    "items": report.items[:50],
                },
            )
        except Exception as exc:  # pragma: no cover - audit must not fail the op
            LOGGER.warning("could not write the maintenance log: %s", exc)

    # -- thumbnail repair (section 25) ------------------------------------
    def missing_thumbnails(self, *, limit: int = 500) -> list[ClipRecord]:
        """Clips whose thumbnail is missing but whose video file is present."""

        candidates: list[ClipRecord] = []
        for clip in self.library.inventory_clips(limit=limit):
            video = self.library.safe_media_path(clip.file_path)
            if video is None or not video.exists():
                continue
            thumb = (
                self.library.safe_media_path(clip.thumbnail_path)
                if clip.thumbnail_path
                else None
            )
            if thumb is None or not thumb.exists():
                candidates.append(clip)
        return candidates

    async def _rebuild_thumbnail(self, clip: ClipRecord) -> Path:
        video = self.library.safe_media_path(clip.file_path)
        if video is None:
            raise ValueError("clip file is outside the library root")
        category = clip.library_category or "unknown"
        thumb_dir = self.library.root / category / "thumbnails"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        target = Path(clip.thumbnail_path) if clip.thumbnail_path else None
        if target is None or not is_within(target, self.library.root):
            target = thumb_dir / f"{video.stem}.jpg"
        timestamp = max(0.0, float(clip.duration or 0.0) / 2)
        await self._toolkit().make_thumbnail(video, timestamp, target)
        if not target.exists():  # pragma: no cover - defensive
            raise RuntimeError(f"ffmpeg did not produce {target}")
        return target

    def repair_thumbnails(self, *, dry_run: bool = True, limit: int = 500) -> MaintenanceReport:
        """Rebuild missing thumbnails from the stored MP4 (never touches the MP4)."""

        report = MaintenanceReport(operation="repair_thumbnails", dry_run=dry_run)
        for clip in self.missing_thumbnails(limit=limit):
            entry = {
                "clip_id": clip.id,
                "video": str(clip.file_path),
                "thumbnail": str(clip.thumbnail_path or ""),
            }
            if dry_run:
                report.items.append(entry)
                continue
            try:
                import asyncio

                new_path = asyncio.run(self._rebuild_thumbnail(clip))
            except Exception as exc:
                LOGGER.warning("thumbnail repair failed for clip %s: %s", clip.id, exc)
                report.errors.append(f"clip {clip.id}: {exc}")
                continue
            self.library.database.execute(
                "UPDATE clips SET thumbnail_path = ? WHERE id = ?",
                (str(new_path), clip.id),
            )
            entry["new_thumbnail"] = str(new_path)
            report.items.append(entry)
        self._log(report)
        return report

    # -- orphan quarantine (sections 26/27) -------------------------------
    def orphans(self, *, include_thumbnails: bool = True) -> list[Path]:
        report = self.library.health_report()
        paths = [Path(item) for item in report["orphan_media"]]
        if include_thumbnails:
            paths.extend(Path(item) for item in report["orphan_thumbnails"])
        return paths

    def quarantine_orphans(
        self,
        *,
        dry_run: bool = True,
        include_thumbnails: bool = True,
        limit: int = 500,
    ) -> MaintenanceReport:
        """Move orphan files into the quarantine folder - never delete them."""

        report = MaintenanceReport(operation="quarantine_orphans", dry_run=dry_run)
        quarantine_root = Path(self.settings.library.quarantine_dir)
        stamp = _timestamp()
        for path in self.orphans(include_thumbnails=include_thumbnails)[:limit]:
            source = resolve_candidate(path)
            if source is None or not source.exists():
                report.skipped.append({"path": str(path), "reason": "not_found"})
                continue
            if not is_within(source, self.library.root):
                report.skipped.append({"path": str(source), "reason": "outside_library"})
                continue
            try:
                relative = source.relative_to(Path(self.library.root).resolve())
            except ValueError:
                relative = Path(source.name)
            destination = quarantine_root / stamp / relative
            entry = {
                "original_path": str(source),
                "quarantine_path": str(destination),
                "timestamp": stamp,
                "reason": "orphan_file",
            }
            if dry_run:
                report.items.append(entry)
                continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
            except OSError as exc:
                LOGGER.warning("could not quarantine %s: %s", source, exc)
                report.errors.append(f"{source}: {exc}")
                continue
            report.items.append(entry)
        self._log(report)
        return report

    # -- review import / export (section 23) ------------------------------
    def export_review_csv(self, path: Path | None = None) -> Path:
        """Write ``clip_id, review_status, review_note, favorite`` for every clip."""

        directory = Path(self.settings.library.review_dir)
        directory.mkdir(parents=True, exist_ok=True)
        target = path or directory / f"review-{_timestamp()}.csv"
        rows = [
            {
                "clip_id": clip.id,
                "review_status": str(clip.review_status),
                "review_note": clip.review_note,
                "favorite": 1 if clip.favorite else 0,
            }
            for clip in self.library.inventory_clips(limit=100000)
        ]
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(REVIEW_CSV_FIELDS))
            writer.writeheader()
            writer.writerows(rows)
        LOGGER.info("exported review state for %s clip(s) to %s", len(rows), target)
        return target

    @staticmethod
    def _parse_bool(value: Any) -> bool | None:
        text = str(value or "").strip().lower()
        if text in TRUE_WORDS:
            return True
        if text in FALSE_WORDS:
            return False
        return None

    def import_review_csv(
        self, path: Path, *, dry_run: bool = True, limit: int = 100000
    ) -> MaintenanceReport:
        """Validate and (optionally) apply a review CSV.

        Invalid ids, unknown statuses or unparseable favourite values are
        reported and skipped - the import never touches AI tags.
        """

        report = MaintenanceReport(operation="review_import", dry_run=dry_run)
        source_path = resolve_candidate(path)
        if source_path is None or not source_path.exists():
            report.errors.append(f"文件不存在: {path}")
            return report
        with source_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for index, row in enumerate(reader, start=2):
                if len(report.items) + len(report.skipped) >= limit:
                    report.skipped.append({"row": index, "reason": "limit_reached"})
                    break
                raw_id = str(row.get("clip_id") or "").strip()
                if not raw_id.isdigit():
                    report.skipped.append({"row": index, "reason": f"invalid clip_id {raw_id!r}"})
                    continue
                clip_id = int(raw_id)
                clip = self.library.get_clip(clip_id)
                if clip is None:
                    report.skipped.append({"row": index, "reason": f"clip {clip_id} not found"})
                    continue
                raw_status = str(row.get("review_status") or "").strip()
                try:
                    status = ReviewStatus(raw_status)
                except ValueError:
                    report.skipped.append(
                        {"row": index, "reason": f"invalid status {raw_status!r}"}
                    )
                    continue
                favourite = self._parse_bool(row.get("favorite"))
                if favourite is None:
                    report.skipped.append(
                        {"row": index, "reason": f"invalid favorite {row.get('favorite')!r}"}
                    )
                    continue
                note = str(row.get("review_note") or "")
                entry = {
                    "clip_id": clip_id,
                    "old_status": str(clip.review_status),
                    "new_status": status.value,
                    "old_note": clip.review_note,
                    "new_note": note,
                    "old_favorite": bool(clip.favorite),
                    "new_favorite": favourite,
                }
                unchanged = (
                    str(clip.review_status) == status.value
                    and clip.review_note == note
                    and bool(clip.favorite) == favourite
                )
                if unchanged:
                    report.skipped.append({"row": index, "reason": "unchanged"})
                    continue
                if not dry_run:
                    self.library.set_review([clip_id], status=status, note=note)
                    self.library.set_favorite([clip_id], favourite)
                report.items.append(entry)
        if not dry_run:
            self._log(report)
        return report

    # -- audit helper for the pre-existing operations ---------------------
    def log_existing_operation(
        self, operation: str, *, details: dict[str, Any] | None = None
    ) -> int:
        return self.library.log_maintenance(
            operation, target_type="library", target_id=None, details=details or {}
        )
