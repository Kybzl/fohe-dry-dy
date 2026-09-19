"""``MaterialLibrary``: the write/read service for clips and their metadata."""

from __future__ import annotations

import logging
import os
import re
import uuid
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from core.models import (
    CLIP_SORT_COLUMNS,
    ClipArtifact,
    ClipQuery,
    ClipRecord,
    ClipTagging,
    RejectReason,
    ReviewStatus,
    SegmentTiming,
    SortDirection,
    SourceVideoRecord,
    SourceVideoStatus,
    TaskRecord,
    TaskRequest,
    TaskStatus,
    utc_now,
)
from core.paths import resolve_candidate, resolve_within
from core.models import sort_option as resolve_sort_option
from core.provenance import ClipIntegrity, classify_provenance, demo_removal_verdict
from core.subtitle_cleanup_models import (
    CLEANUP_VERSION,
    CleanupReviewStatus,
    CleanupStatus,
)
from storage.database import Database
from storage.models import clip_row_values, row_to_clip, row_to_source_video, row_to_task, tags_from_tagging

LOGGER = logging.getLogger(__name__)

# A small transliteration table keeps generated file names readable
# (``苹果`` -> ``apple``) without pulling in a pinyin dependency.
MATERIAL_SLUGS: dict[str, str] = {
    "苹果": "apple",
    "香蕉": "banana",
    "辣椒": "chili",
    "山药": "yam",
    "枸杞": "goji",
    "鱼": "fish",
    "虾": "shrimp",
    "药材": "herb",
    "香菇": "shiitake",
    "红枣": "jujube",
    "萝卜": "radish",
    "姜": "ginger",
    "蒜": "garlic",
    "海带": "kelp",
}

_UNSAFE = re.compile(r"[^0-9a-zA-Z_\-]+")


def material_slug(material: str) -> str:
    """Readable, filesystem safe folder/prefix name for a material."""

    material = (material or "").strip()
    if not material:
        return "unknown"
    if material in MATERIAL_SLUGS:
        return MATERIAL_SLUGS[material]
    for name, slug in MATERIAL_SLUGS.items():
        if name and name in material:
            return slug
    ascii_only = _UNSAFE.sub("_", material).strip("_")
    if ascii_only and any(character.isalnum() for character in ascii_only):
        return ascii_only.lower()[:32]
    digest = uuid.uuid5(uuid.NAMESPACE_URL, material).hex[:8]
    return f"material_{digest}"


def _iso(moment: datetime | None = None) -> str:
    return (moment or utc_now()).isoformat()


def _json_text(value: Any) -> str | None:
    """Serialize an optional JSON payload for SQLite (``None`` stays NULL)."""

    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_value(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):  # pragma: no cover - defensive
        return None


#: statuses that represent a settled outcome for a source video
TERMINAL_STATUSES: frozenset[SourceVideoStatus] = frozenset(
    {
        SourceVideoStatus.PROCESSED,
        SourceVideoStatus.SKIPPED_DUPLICATE,
        SourceVideoStatus.REJECTED,
        SourceVideoStatus.REJECTED_PREVIEW,
        SourceVideoStatus.NO_USABLE_SEGMENT,
        SourceVideoStatus.FAILED,
        SourceVideoStatus.FAILED_SEARCH,
        SourceVideoStatus.FAILED_PREVIEW,
        SourceVideoStatus.FAILED_DOWNLOAD,
        SourceVideoStatus.FAILED_AI,
        SourceVideoStatus.FAILED_MEDIA,
    }
)


@dataclass
class ClipRemovalReport:
    """What ``MaterialLibrary.remove_clip`` actually did (section 21/22)."""

    clip_id: int
    found: bool = False
    record: ClipRecord | None = None
    removed_files: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    #: stored paths that were refused because they live outside the safe roots
    refused_files: list[str] = field(default_factory=list)
    orphaned_tags: int = 0
    unlinked_ai_runs: int = 0

    @property
    def removed_file_count(self) -> int:
        return len(self.removed_files)


class MaterialLibrary:
    """Stores clips, source videos and tasks; owns the ``library/`` layout."""

    def __init__(self, database: Database, library_root: Path) -> None:
        self.database = database
        self.root = Path(library_root)
        #: paths the library may delete: the material library itself, plus the
        #: project directory (mock/demo runs write placeholders into scratch
        #: folders there).  Anything else is refused.
        self.safe_roots: tuple[Path, ...] = (
            Path(library_root),
            Path(__file__).resolve().parent.parent,
        )

    # -- lifecycle ---------------------------------------------------------
    def initialize(self) -> None:
        self.database.initialize()
        self.root.mkdir(parents=True, exist_ok=True)

    def clip_dir_for(self, material: str) -> Path:
        """``<library_root>/<material>/clips`` without touching the disk."""

        return self.root / (material or "unknown") / "clips"

    def thumbnail_dir_for(self, material: str) -> Path:
        """``<library_root>/<material>/thumbnails`` without touching the disk."""

        return self.root / (material or "unknown") / "thumbnails"

    def clip_dir(self, material: str) -> Path:
        path = self.clip_dir_for(material)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def thumbnail_dir(self, material: str) -> Path:
        path = self.thumbnail_dir_for(material)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def next_clip_paths(self, material: str) -> tuple[Path, Path]:
        """``library/苹果/clips/apple_ab12cd34.mp4`` + matching thumbnail."""

        slug = material_slug(material)
        token = uuid.uuid4().hex[:8]
        clip_path = self.clip_dir(material) / f"{slug}_{token}.mp4"
        thumbnail_path = self.thumbnail_dir(material) / f"{slug}_{token}.jpg"
        return clip_path, thumbnail_path

    # -- tasks -------------------------------------------------------------
    def create_task(
        self,
        request: TaskRequest,
        *,
        status: TaskStatus = TaskStatus.PENDING,
        error: str | None = None,
    ) -> int:
        now = _iso()
        request_payload = request.model_dump(mode="json")
        request_payload["resume_task_id"] = None
        task_id = self.database.insert(
            "tasks",
            {
                "material": request.material,
                "target_clip_count": request.target_clip_count,
                "min_clip_duration": request.min_clip_duration,
                "max_clip_duration": request.max_clip_duration,
                "subtitle_policy": str(request.subtitle_policy),
                "status": str(status),
                "error": error,
                "request_json": _json_text(request_payload),
                "created_at": now,
                "updated_at": now,
            },
        )
        LOGGER.info("task #%s created for material %r", task_id, request.material)
        return task_id

    def get_task_request(self, task_id: int) -> TaskRequest | None:
        """Load the non-secret request checkpoint for an exact resume."""

        row = self.database.query_one(
            "SELECT request_json FROM tasks WHERE id = ?", (int(task_id),)
        )
        if row is None or not row["request_json"]:
            return None
        try:
            return TaskRequest.model_validate_json(row["request_json"])
        except Exception as exc:
            LOGGER.warning("task #%s has an invalid request checkpoint: %s", task_id, exc)
            return None

    def update_task_request(self, task_id: int, request: TaskRequest) -> None:
        """Checkpoint intentional resume changes such as a higher target."""

        payload = request.model_dump(mode="json")
        payload["resume_task_id"] = None
        self.database.execute(
            "UPDATE tasks SET target_clip_count = ?, request_json = ?, updated_at = ? "
            "WHERE id = ?",
            (
                int(request.target_clip_count),
                _json_text(payload),
                _iso(),
                int(task_id),
            ),
        )

    def update_task_status(
        self,
        task_id: int,
        status: TaskStatus,
        *,
        error: str | None = None,
    ) -> None:
        self.database.execute(
            "UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (str(status), error, _iso(), task_id),
        )

    def get_task(self, task_id: int) -> TaskRecord | None:
        row = self.database.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return row_to_task(row) if row else None

    def list_tasks(self, limit: int = 20) -> list[TaskRecord]:
        rows = self.database.query("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))
        return [row_to_task(row) for row in rows]

    def normalize_interrupted_tasks(self) -> dict[int, int]:
        """Make tasks abandoned by a previous process safely resumable.

        A live :class:`TaskRunner` owns at most one task.  Therefore any rows
        still marked ``running`` when a new runner is constructed came from an
        unclean process exit.  The task becomes ``partial`` (not failed), and
        transient source states are released so ``--resume-task`` can retry
        them immediately.  The return value maps task id to released sources.
        """

        in_progress = (
            SourceVideoStatus.PREVIEWING,
            SourceVideoStatus.DOWNLOADING,
            SourceVideoStatus.ANALYZING,
            SourceVideoStatus.QUALIFIED,
        )
        source_placeholders = ", ".join("?" for _ in in_progress)
        now = _iso()
        recovered: dict[int, int] = {}
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT id, error FROM tasks WHERE status = ? ORDER BY id",
                (str(TaskStatus.RUNNING),),
            ).fetchall()
            for row in rows:
                task_id = int(row["id"])
                owner_pid = self._runner_pid(row["error"])
                if owner_pid is not None and self._process_is_alive(owner_pid):
                    continue
                connection.execute(
                    "UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                    (
                        str(TaskStatus.PARTIAL),
                        "interrupted_by_process_restart",
                        now,
                        task_id,
                    ),
                )
                cursor = connection.execute(
                    f"UPDATE source_videos SET status = ?, reject_reason = NULL, "
                    f"updated_at = ? WHERE task_id = ? AND status IN ({source_placeholders})",
                    (
                        str(SourceVideoStatus.DISCOVERED),
                        now,
                        task_id,
                        *(str(status) for status in in_progress),
                    ),
                )
                recovered[task_id] = int(cursor.rowcount)
        if recovered:
            LOGGER.warning(
                "normalized interrupted collection task(s): %s",
                ", ".join(f"#{task_id} ({count} sources released)" for task_id, count in recovered.items()),
            )
        return recovered

    @staticmethod
    def _runner_pid(value: Any) -> int | None:
        text = str(value or "")
        if not text.startswith("runner_pid:"):
            return None
        try:
            return int(text.partition(":")[2])
        except ValueError:
            return None

    @staticmethod
    def _process_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    # -- source videos -----------------------------------------------------
    def upsert_source_video(
        self,
        *,
        task_id: int | None,
        platform: str,
        platform_video_id: str,
        source_url: str,
        title: str = "",
        author: str = "",
        author_id: str | None = None,
        cover_url: str | None = None,
        publish_time: str | None = None,
        duration: float | None = None,
        status: SourceVideoStatus = SourceVideoStatus.CANDIDATE,
        reject_reason: RejectReason | None = None,
        preview_material_score: float | None = None,
        preview_subtitle_score: float | None = None,
        preview_quality_score: float | None = None,
        media_url: str | None = None,
        matched_queries: list[str] | None = None,
        statistics: dict[str, Any] | None = None,
        touch_attempt: bool = False,
    ) -> int:
        """Insert or update one source video row.

        Reason precedence (Milestone 3.6): a **terminal** reason such as
        ``no_material`` describes what the video actually is, so a later
        *bookkeeping* event (``duplicate_video`` / ``duplicate_url`` /
        ``already_processed``) only merges discovery metadata and never
        overwrites the stored status/reason.
        """

        now = _iso()
        effective_status = status
        effective_reason = reject_reason
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT status, reject_reason FROM source_videos "
                "WHERE platform = ? AND platform_video_id = ?",
                (platform, platform_video_id),
            ).fetchone()
            if existing is not None and self._preserve_terminal_outcome(
                existing["status"], existing["reject_reason"], reject_reason
            ):
                effective_status = SourceVideoStatus(existing["status"])
                effective_reason = (
                    RejectReason(existing["reject_reason"])
                    if existing["reject_reason"]
                    else None
                )
                LOGGER.debug(
                    "keeping terminal outcome %s/%s for %s (ignoring bookkeeping %s)",
                    effective_status,
                    effective_reason,
                    platform_video_id,
                    reject_reason,
                )
            connection.execute(
                """
                INSERT INTO source_videos (
                    task_id, platform, platform_video_id, source_url, title, author, duration,
                    status, reject_reason, preview_material_score, preview_subtitle_score,
                    preview_quality_score, author_id, cover_url, publish_time,
                    source_stats_json, matched_queries, media_url, attempt_count,
                    last_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (platform, platform_video_id) DO UPDATE SET
                    task_id = COALESCE(excluded.task_id, source_videos.task_id),
                    source_url = excluded.source_url,
                    title = excluded.title,
                    author = excluded.author,
                    author_id = COALESCE(excluded.author_id, source_videos.author_id),
                    cover_url = COALESCE(excluded.cover_url, source_videos.cover_url),
                    publish_time = COALESCE(excluded.publish_time, source_videos.publish_time),
                    duration = COALESCE(excluded.duration, source_videos.duration),
                    status = excluded.status,
                    reject_reason = excluded.reject_reason,
                    preview_material_score = COALESCE(
                        excluded.preview_material_score, source_videos.preview_material_score
                    ),
                    preview_subtitle_score = COALESCE(
                        excluded.preview_subtitle_score, source_videos.preview_subtitle_score
                    ),
                    preview_quality_score = COALESCE(
                        excluded.preview_quality_score, source_videos.preview_quality_score
                    ),
                    source_stats_json = COALESCE(
                        excluded.source_stats_json, source_videos.source_stats_json
                    ),
                    matched_queries = COALESCE(
                        excluded.matched_queries, source_videos.matched_queries
                    ),
                    media_url = COALESCE(excluded.media_url, source_videos.media_url),
                    attempt_count = CASE
                        WHEN ? THEN source_videos.attempt_count + 1
                        ELSE source_videos.attempt_count
                    END,
                    last_attempt_at = CASE
                        WHEN ? THEN excluded.last_attempt_at
                        ELSE source_videos.last_attempt_at
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    platform,
                    platform_video_id,
                    source_url,
                    title,
                    author,
                    duration,
                    str(effective_status),
                    str(effective_reason) if effective_reason else None,
                    preview_material_score,
                    preview_subtitle_score,
                    preview_quality_score,
                    author_id,
                    cover_url,
                    publish_time,
                    json.dumps(statistics, ensure_ascii=False) if statistics else None,
                    json.dumps(matched_queries, ensure_ascii=False) if matched_queries else None,
                    media_url,
                    1 if touch_attempt else 0,
                    now if touch_attempt else None,
                    now,
                    now,
                    int(bool(touch_attempt)),
                    int(bool(touch_attempt)),
                ),
            )
            row = connection.execute(
                "SELECT id FROM source_videos WHERE platform = ? AND platform_video_id = ?",
                (platform, platform_video_id),
            ).fetchone()
            return int(row["id"])

    @staticmethod
    @staticmethod
    def _preserve_terminal_outcome(
        existing_status: str | None,
        existing_reason: str | None,
        incoming_reason: RejectReason | None,
    ) -> bool:
        """True when a bookkeeping event must not overwrite a settled outcome.

        * a terminal stored outcome (processed, rejected with a content reason,
          failed_*) is kept
        * a real verdict (``no_material`` ...) or a progress write still wins,
          so re-processing after the retry window keeps working
        """

        if existing_status is None:
            return False
        try:
            stored_status = SourceVideoStatus(existing_status)
        except ValueError:
            return False
        if stored_status not in TERMINAL_STATUSES:
            return False
        if incoming_reason is None:
            return False
        return incoming_reason in RejectReason.bookkeeping_reasons()

    def update_source_video_status(
        self,
        source_video_id: int,
        status: SourceVideoStatus,
        *,
        reject_reason: RejectReason | None = None,
    ) -> None:
        self.database.execute(
            "UPDATE source_videos SET status = ?, reject_reason = ?, updated_at = ? WHERE id = ?",
            (str(status), str(reject_reason) if reject_reason else None, _iso(), source_video_id),
        )

    def release_in_progress_sources(self, task_id: int) -> int:
        """Make sources abandoned by a cancelled task immediately retryable."""

        in_progress = (
            SourceVideoStatus.PREVIEWING,
            SourceVideoStatus.DOWNLOADING,
            SourceVideoStatus.ANALYZING,
            SourceVideoStatus.QUALIFIED,
        )
        placeholders = ", ".join("?" for _ in in_progress)
        return self.database.execute(
            f"UPDATE source_videos SET status = ?, reject_reason = NULL, updated_at = ? "
            f"WHERE task_id = ? AND status IN ({placeholders})",
            (
                str(SourceVideoStatus.DISCOVERED),
                _iso(),
                int(task_id),
                *(str(status) for status in in_progress),
            ),
        )

    def get_source_video(self, platform: str, platform_video_id: str) -> SourceVideoRecord | None:
        row = self.database.query_one(
            "SELECT * FROM source_videos WHERE platform = ? AND platform_video_id = ?",
            (platform, platform_video_id),
        )
        return row_to_source_video(row) if row else None

    def get_source_video_by_id(self, source_video_id: int) -> SourceVideoRecord | None:
        """Source row by primary key (used by the clip provenance panel)."""

        row = self.database.query_one(
            "SELECT * FROM source_videos WHERE id = ?", (int(source_video_id),)
        )
        return row_to_source_video(row) if row else None

    def list_source_videos(self, task_id: int | None = None, limit: int = 200) -> list[SourceVideoRecord]:
        if task_id is None:
            rows = self.database.query(
                "SELECT * FROM source_videos ORDER BY id DESC LIMIT ?", (limit,)
            )
        else:
            rows = self.database.query(
                "SELECT * FROM source_videos WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                (task_id, limit),
            )
        return [row_to_source_video(row) for row in rows]

    # -- clips -------------------------------------------------------------
    def insert_clip(
        self,
        *,
        task_id: int | None,
        source_video_id: int | None,
        platform: str,
        platform_video_id: str,
        source_url: str,
        tagging: ClipTagging,
        timing: SegmentTiming,
        artifact: ClipArtifact,
        content_key: str | None = None,
        source_title: str = "",
        source_author: str = "",
        source_author_id: str | None = None,
        source_publish_time: str | None = None,
        library_category: str = "",
        provenance: str = "",
        tag_prompt_version: str = "",
        subtitle_analysis_json: str = "",
    ) -> int:
        """Insert a clip, its tag rows and its ``clip_tags`` links atomically."""

        values = clip_row_values(
            task_id=task_id,
            source_video_id=source_video_id,
            platform=platform,
            platform_video_id=platform_video_id,
            source_url=source_url,
            tagging=tagging,
            duration=artifact.duration,
            width=artifact.width,
            height=artifact.height,
            fps=artifact.fps,
            source_start=timing.start,
            source_end=timing.end,
            file_path=str(artifact.file_path),
            thumbnail_path=str(artifact.thumbnail_path) if artifact.thumbnail_path else None,
            phash=artifact.phash,
            sha256=artifact.sha256,
            content_key=content_key,
            created_at=_iso(),
            source_title=source_title,
            source_author=source_author,
            source_author_id=source_author_id,
            source_publish_time=source_publish_time,
            library_category=library_category,
            provenance=provenance,
            tag_prompt_version=tag_prompt_version,
            subtitle_analysis_json=subtitle_analysis_json or None,
        )
        tag_rows = tags_from_tagging(tagging)

        with self.database.transaction() as connection:
            columns = ", ".join(values)
            placeholders = ", ".join("?" for _ in values)
            cursor = connection.execute(
                f"INSERT INTO clips ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
            clip_id = int(cursor.lastrowid or 0)
            for name, category, confidence, source in tag_rows:
                connection.execute(
                    "INSERT INTO tags (name, category) VALUES (?, ?) "
                    "ON CONFLICT (name, category) DO NOTHING",
                    (name, category),
                )
                tag_row = connection.execute(
                    "SELECT id FROM tags WHERE name = ? AND category = ?", (name, category)
                ).fetchone()
                if tag_row is None:  # pragma: no cover - defensive
                    continue
                connection.execute(
                    "INSERT INTO clip_tags (clip_id, tag_id, confidence, source) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (clip_id, tag_id) DO NOTHING",
                    (clip_id, int(tag_row["id"]), confidence, source),
                )
        return clip_id

    def clip_tags(self, clip_id: int) -> list[str]:
        rows = self.database.query(
            """
            SELECT tags.name AS name
            FROM clip_tags
            JOIN tags ON tags.id = clip_tags.tag_id
            WHERE clip_tags.clip_id = ?
            ORDER BY tags.category, tags.name
            """,
            (clip_id,),
        )
        return [row["name"] for row in rows]

    def get_clip(self, clip_id: int) -> ClipRecord | None:
        row = self.database.query_one("SELECT * FROM clips WHERE id = ?", (clip_id,))
        if row is None:
            return None
        return row_to_clip(row, self.clip_tags(clip_id))

    def list_clips(
        self,
        *,
        material: str | None = None,
        task_id: int | None = None,
        limit: int = 100,
    ) -> list[ClipRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if material:
            clauses.append("material = ?")
            params.append(material)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.database.query(
            f"SELECT * FROM clips {where} ORDER BY id DESC LIMIT ?", params
        )
        return [row_to_clip(row, self.clip_tags(int(row["id"]))) for row in rows]

    def count_clips(self, material: str | None = None, task_id: int | None = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if material:
            clauses.append("material = ?")
            params.append(material)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self.database.query_one(f"SELECT COUNT(*) AS n FROM clips {where}", params)
        return int(row["n"]) if row else 0

    def clips_for_task(self, task_id: int, limit: int = 1000) -> list[ClipRecord]:
        """Clips already produced by a task (used when resuming it)."""

        return self.list_clips(task_id=task_id, limit=limit)

    # -- audit linkage (section 16/17) --------------------------------------
    def update_ai_run_clip(self, run_id: int, clip_id: int) -> int:
        """Back-patch ``ai_runs.clip_id`` once the clip row exists.

        ``clip_tagging`` runs before the clip is inserted (the tags are part of
        the insert), so the audit row is linked right after persistence.  Only
        rows that do not have a clip yet are touched.
        """

        return self.database.execute(
            "UPDATE ai_runs SET clip_id = ? WHERE id = ? AND clip_id IS NULL",
            (clip_id, run_id),
        )

    def ai_runs_for_clip(self, clip_id: int) -> list[dict[str, Any]]:
        rows = self.database.query(
            "SELECT * FROM ai_runs WHERE clip_id = ? ORDER BY id", (clip_id,)
        )
        return [dict(row) for row in rows]

    # -- physical removal (sections 21/22) ---------------------------------
    def remove_clip(
        self,
        clip_id: int,
        *,
        delete_file: bool = True,
        delete_thumbnail: bool = True,
        prune_orphan_tags: bool = True,
        audit_runs: int = 0,
    ) -> ClipRemovalReport:
        """Safely remove one clip: row, tag links, thumbnail and video file.

        Source provenance rows are **never** deleted here - they describe the
        source video, not this clip.  ``ai_runs`` rows are kept by default
        (only the link is cleared) so the audit trail stays intact.  A stored
        path that resolves outside every safe root is refused rather than
        deleted (section 31).
        """

        clip = self.get_clip(clip_id)
        if clip is None:
            return ClipRemovalReport(clip_id=clip_id, found=False)

        removed_files: list[str] = []
        missing_files: list[str] = []
        refused_files: list[str] = []
        for path, enabled in (
            (clip.file_path, delete_file),
            (clip.thumbnail_path, delete_thumbnail),
        ):
            if not enabled or path is None:
                continue
            target = Path(path)
            safe = resolve_within(target, self.safe_roots)
            if safe is None:
                refused_files.append(str(target))
                LOGGER.warning(
                    "refusing to delete %s: outside the configured library roots", target
                )
                continue
            try:
                if safe.exists():
                    safe.unlink()
                    removed_files.append(str(safe))
                else:
                    missing_files.append(str(safe))
            except OSError as exc:  # pragma: no cover - defensive
                LOGGER.warning("could not delete clip file %s: %s", safe, exc)
                missing_files.append(str(safe))

        orphaned_tags = 0
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE ai_runs SET clip_id = NULL WHERE clip_id = ?", (clip_id,)
            )
            connection.execute("DELETE FROM clip_tags WHERE clip_id = ?", (clip_id,))
            connection.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
            if prune_orphan_tags:
                cursor = connection.execute(
                    "DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM clip_tags)"
                )
                orphaned_tags = int(cursor.rowcount or 0)

        LOGGER.info(
            "removed clip #%s (files=%s orphan_tags=%s)",
            clip_id,
            len(removed_files),
            orphaned_tags,
        )
        return ClipRemovalReport(
            clip_id=clip_id,
            found=True,
            record=clip,
            removed_files=removed_files,
            missing_files=missing_files,
            refused_files=refused_files,
            orphaned_tags=orphaned_tags,
            unlinked_ai_runs=audit_runs,
        )

    def inventory_clips(self, *, limit: int = 2000) -> list[ClipRecord]:
        """Every clip with its persisted provenance, newest first."""

        rows = self.database.query(
            "SELECT * FROM clips ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [row_to_clip(row, self.clip_tags(int(row["id"]))) for row in rows]

    def backfill_clip_metadata(self, *, apply: bool = False) -> list[dict[str, Any]]:
        """Fill empty ``library_category`` / ``provenance`` on historical rows.

        Only *empty* values are filled, from evidence that already exists:
        the physical folder the clip lives in (which by construction is the
        library category) and the provenance classifier.  Nothing is overwritten
        and nothing is deleted, so this is a safe, explicit maintenance step
        (sections 17/29/31).
        """

        changes: list[dict[str, Any]] = []
        for clip in self.inventory_clips():
            category = clip.library_category
            provenance = clip.provenance
            if not category:
                parent = Path(clip.file_path).parent
                candidate = parent.parent.name if parent.name == "clips" else parent.name
                if candidate:
                    changes.append(
                        {
                            "clip_id": clip.id,
                            "field": "library_category",
                            "old": "",
                            "new": candidate,
                        }
                    )
                    category = candidate
            if not provenance:
                derived = classify_provenance(
                    clip.platform, clip.platform_video_id, source_url=clip.source_url
                )
                if derived:
                    changes.append(
                        {
                            "clip_id": clip.id,
                            "field": "provenance",
                            "old": "",
                            "new": derived,
                        }
                    )
                    provenance = derived
            prompt_version = clip.tag_prompt_version
            if not prompt_version:
                rows = self.database.query(
                    "SELECT prompt_version FROM ai_runs WHERE clip_id = ? "
                    "AND operation = 'clip_tagging' AND prompt_version IS NOT NULL "
                    "ORDER BY id",
                    (clip.id,),
                )
                versions = {
                    str(row["prompt_version"]) for row in rows if row["prompt_version"]
                }
                if len(versions) == 1:
                    # exactly one version is on record: the mapping is reliable
                    prompt_version = next(iter(versions))
                    changes.append(
                        {
                            "clip_id": clip.id,
                            "field": "tag_prompt_version",
                            "old": "",
                            "new": prompt_version,
                        }
                    )
                elif len(versions) > 1:
                    # several prompts were used on this clip (e.g. an A/B run):
                    # guessing would be dishonest, so it stays empty for a human
                    changes.append(
                        {
                            "clip_id": clip.id,
                            "field": "tag_prompt_version",
                            "old": "",
                            "new": "",
                            "reason": f"ambiguous: {sorted(versions)}",
                        }
                    )
            if apply and (category or provenance):
                self.database.execute(
                    "UPDATE clips SET library_category = ?, provenance = ?, "
                    "tag_prompt_version = COALESCE(NULLIF(tag_prompt_version, ''), ?) "
                    "WHERE id = ? AND (library_category IS NULL OR library_category = '' "
                    "OR provenance IS NULL OR provenance = '' "
                    "OR tag_prompt_version IS NULL OR tag_prompt_version = '')",
                    (category, provenance, prompt_version or "", clip.id),
                )
        return changes

    def classify_clip(self, clip: ClipRecord) -> tuple[str, bool, str]:
        """``(provenance, removable, reason)`` for one stored clip."""

        provenance = clip.provenance or classify_provenance(
            clip.platform, clip.platform_video_id, source_url=clip.source_url
        )
        size_bytes: int | None = None
        try:
            if clip.file_path is not None and Path(clip.file_path).exists():
                size_bytes = Path(clip.file_path).stat().st_size
        except OSError:  # pragma: no cover - defensive
            size_bytes = None
        integrity = ClipIntegrity(
            width=clip.width, height=clip.height, size_bytes=size_bytes
        )
        removable, reason = demo_removal_verdict(
            provenance=provenance, integrity=integrity
        )
        return provenance, removable, reason

    # -- retagging (section 28) --------------------------------------------
    def replace_clip_tags(
        self,
        clip_id: int,
        tagging: ClipTagging,
        *,
        prompt_version: str = "",
    ) -> ClipRecord | None:
        """Replace the semantic tags/scores of one clip, keeping its media.

        The video file, thumbnail, hashes, provenance and ``created_at`` are
        untouched: only the tag columns, the ``clip_tags`` links and the prompt
        version change.  Used by ``--retag-clip`` and by the A/B evaluation.
        """

        clip = self.get_clip(clip_id)
        if clip is None:
            return None

        values = {
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
            "edit_roles": json.dumps(
                [str(role) for role in tagging.edit_roles], ensure_ascii=False
            ),
            "description": tagging.description,
            "material_score": tagging.scores.material_relevance,
            "visual_quality_score": tagging.scores.visual_quality,
            "subtitle_cleanliness_score": tagging.scores.subtitle_cleanliness,
            "stability_score": tagging.scores.stability,
            "composition_score": tagging.scores.composition,
            "overall_score": tagging.scores.overall,
            "tag_prompt_version": prompt_version,
        }
        assignments = ", ".join(f"{column} = ?" for column in values)
        tag_rows = tags_from_tagging(tagging)

        with self.database.transaction() as connection:
            connection.execute(
                f"UPDATE clips SET {assignments} WHERE id = ?",
                (*values.values(), clip_id),
            )
            connection.execute("DELETE FROM clip_tags WHERE clip_id = ?", (clip_id,))
            for name, category, confidence, source in tag_rows:
                connection.execute(
                    "INSERT INTO tags (name, category) VALUES (?, ?) "
                    "ON CONFLICT (name, category) DO NOTHING",
                    (name, category),
                )
                tag_row = connection.execute(
                    "SELECT id FROM tags WHERE name = ? AND category = ?", (name, category)
                ).fetchone()
                if tag_row is None:  # pragma: no cover - defensive
                    continue
                connection.execute(
                    "INSERT INTO clip_tags (clip_id, tag_id, confidence, source) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (clip_id, tag_id) DO NOTHING",
                    (clip_id, int(tag_row["id"]), confidence, source),
                )
        return self.get_clip(clip_id)
    # -- dedup lookups -----------------------------------------------------
    def find_clip_by_sha256(self, sha256: str) -> int | None:
        row = self.database.query_one("SELECT id FROM clips WHERE sha256 = ? LIMIT 1", (sha256,))
        return int(row["id"]) if row else None

    def find_clip_by_content_key(self, content_key: str) -> int | None:
        row = self.database.query_one(
            "SELECT id FROM clips WHERE content_key = ? LIMIT 1", (content_key,)
        )
        return int(row["id"]) if row else None

    def all_phashes(self, material: str | None = None) -> list[tuple[int, str]]:
        if material:
            rows = self.database.query(
                "SELECT id, phash FROM clips WHERE phash IS NOT NULL AND material = ?", (material,)
            )
        else:
            rows = self.database.query("SELECT id, phash FROM clips WHERE phash IS NOT NULL")
        return [(int(row["id"]), str(row["phash"])) for row in rows]

    def url_seen(self, source_url: str) -> bool:
        row = self.database.query_one(
            "SELECT id FROM source_videos WHERE source_url = ? LIMIT 1", (source_url,)
        )
        return row is not None

    def add_matched_query(self, platform: str, platform_video_id: str, query: str) -> None:
        """Record that another search term surfaced this source video (section 12)."""

        if not query:
            return
        record = self.get_source_video(platform, platform_video_id)
        if record is None or query in record.matched_queries:
            return
        queries = [*record.matched_queries, query]
        self.database.execute(
            "UPDATE source_videos SET matched_queries = ?, updated_at = ? "
            "WHERE platform = ? AND platform_video_id = ?",
            (json.dumps(queries, ensure_ascii=False), _iso(), platform, platform_video_id),
        )

    def merge_discovery(
        self,
        *,
        platform: str,
        platform_video_id: str,
        query: str,
        source_url: str = "",
        title: str = "",
        author: str = "",
    ) -> SourceVideoRecord | None:
        """Pure discovery bookkeeping: merge the query, never touch the verdict.

        Used when the same video shows up again (another keyword, another URL
        form, an earlier task).  ``status`` / ``reject_reason`` /
        ``last_attempt_at`` all stay exactly as they were; only
        ``matched_queries`` (and a still empty title/author/url) are filled in.
        """

        record = self.get_source_video(platform, platform_video_id)
        if record is None:
            # first time we see it and it was rejected before processing started
            self.upsert_source_video(
                task_id=None,
                platform=platform,
                platform_video_id=platform_video_id,
                source_url=source_url,
                title=title,
                author=author,
                status=SourceVideoStatus.DISCOVERED,
                matched_queries=[query] if query else None,
            )
            return self.get_source_video(platform, platform_video_id)

        self.add_matched_query(platform, platform_video_id, query)
        patch: list[str] = []
        params: list[Any] = []
        if source_url and not record.source_url:
            patch.append("source_url = ?")
            params.append(source_url)
        if title and not record.title:
            patch.append("title = ?")
            params.append(title)
        if author and not record.author:
            patch.append("author = ?")
            params.append(author)
        if patch:
            params.extend([platform, platform_video_id])
            self.database.execute(
                f"UPDATE source_videos SET {', '.join(patch)} "
                "WHERE platform = ? AND platform_video_id = ?",
                params,
            )
        return self.get_source_video(platform, platform_video_id)

    # -- search yield metrics (section 31) ---------------------------------
    def add_search_yield(
        self,
        *,
        task_id: int | None,
        platform: str,
        query: str,
        candidate_count: int = 0,
        unique_candidate_count: int = 0,
        preview_accept_count: int = 0,
        download_count: int = 0,
        final_clip_count: int = 0,
        new_to_system_count: int = 0,
        known_source_count: int = 0,
        current_run_duplicate_count: int = 0,
        already_processed_count: int = 0,
        already_represented_count: int = 0,
        query_family: str = "",
        plan_id: int | None = None,
        plan_item_id: int | None = None,
        planned_order: int = 0,
        actual_order: int = 0,
        candidate_cap: int = 0,
        stop_reason: str = "",
        reserve_activation_reason: str = "",
        was_reserve: bool = False,
    ) -> int:
        return self.database.insert(
            "search_yields",
            {
                "task_id": task_id,
                "platform": platform,
                "query": query,
                "candidate_count": candidate_count,
                "unique_candidate_count": unique_candidate_count,
                "preview_accept_count": preview_accept_count,
                "download_count": download_count,
                "final_clip_count": final_clip_count,
                "new_to_system_count": int(new_to_system_count),
                "known_source_count": int(known_source_count),
                "current_run_duplicate_count": int(current_run_duplicate_count),
                "already_processed_count": int(already_processed_count),
                "already_represented_count": int(already_represented_count),
                "query_family": str(query_family or ""),
                "plan_id": int(plan_id) if plan_id is not None else None,
                "plan_item_id": int(plan_item_id) if plan_item_id is not None else None,
                "planned_order": int(planned_order),
                "actual_order": int(actual_order),
                "candidate_cap": int(candidate_cap),
                "stop_reason": str(stop_reason or ""),
                "reserve_activation_reason": str(reserve_activation_reason or ""),
                "was_reserve": 1 if was_reserve else 0,
                "created_at": _iso(),
            },
        )

    def list_search_yields(self, task_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if task_id is None:
            rows = self.database.query(
                "SELECT * FROM search_yields ORDER BY id DESC LIMIT ?", (limit,)
            )
        else:
            rows = self.database.query(
                "SELECT * FROM search_yields WHERE task_id = ? ORDER BY id LIMIT ?",
                (task_id, limit),
            )
        return [dict(row) for row in rows]

    def query_yield_stats(self, limit: int = 20) -> list[dict[str, Any]]:
        """Which search terms actually produce material (per query aggregate)."""

        rows = self.database.query(
            """
            SELECT query,
                   SUM(candidate_count) AS candidates,
                   SUM(unique_candidate_count) AS unique_candidates,
                   SUM(preview_accept_count) AS accepted,
                   SUM(download_count) AS downloads,
                   SUM(final_clip_count) AS clips
            FROM search_yields
            GROUP BY query
            ORDER BY clips DESC, candidates DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in rows]

    def clips_with_content_key(self, content_key: str, limit: int = 5) -> list[int]:
        rows = self.database.query(
            "SELECT id FROM clips WHERE content_key = ? ORDER BY id LIMIT ?",
            (content_key, limit),
        )
        return [int(row["id"]) for row in rows]

    def content_key_groups(
        self, *, material: str | None = None, limit: int = 20
    ) -> list[tuple[str, int]]:
        """``(content_key, clip_count)`` groups, largest first (similarity only)."""

        if material:
            rows = self.database.query(
                """
                SELECT content_key, COUNT(*) AS n FROM clips
                WHERE content_key IS NOT NULL AND material = ?
                GROUP BY content_key ORDER BY n DESC, content_key LIMIT ?
                """,
                (material, limit),
            )
        else:
            rows = self.database.query(
                """
                SELECT content_key, COUNT(*) AS n FROM clips
                WHERE content_key IS NOT NULL
                GROUP BY content_key ORDER BY n DESC, content_key LIMIT ?
                """,
                (limit,),
            )
        return [(str(row["content_key"]), int(row["n"])) for row in rows]

    # -- AI audit ----------------------------------------------------------
    def add_ai_run(self, record: Any) -> int:
        """Persist one ``AICallRecord`` (see ``ai/audit.py``)."""

        row = record.to_row() if hasattr(record, "to_row") else dict(record)
        return self.database.insert("ai_runs", row)

    def list_ai_runs(
        self,
        *,
        task_id: int | None = None,
        source_video_id: int | None = None,
        clip_id: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if source_video_id is not None:
            clauses.append("source_video_id = ?")
            params.append(source_video_id)
        if clip_id is not None:
            clauses.append("clip_id = ?")
            params.append(clip_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.database.query(
            f"SELECT * FROM ai_runs {where} ORDER BY id DESC LIMIT ?", params
        )
        return [dict(row) for row in rows]

    def ai_usage_summary(self, *, task_id: int | None = None) -> dict[str, Any]:
        """Task level AI usage: calls, tokens, cost, latency (section 30)."""

        where = "WHERE task_id = ?" if task_id is not None else ""
        params: list[Any] = [task_id] if task_id is not None else []
        row = self.database.query_one(
            f"""
            SELECT
                COUNT(*) AS ai_calls,
                SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS successful_calls,
                SUM(CASE WHEN status <> 'ok' THEN 1 ELSE 0 END) AS failed_calls,
                SUM(COALESCE(prompt_tokens, 0)) AS prompt_tokens,
                SUM(COALESCE(completion_tokens, 0)) AS completion_tokens,
                SUM(COALESCE(total_tokens, 0)) AS total_tokens,
                SUM(estimated_cost) AS estimated_cost,
                AVG(latency_ms) AS average_latency_ms
            FROM ai_runs {where}
            """,
            params,
        )
        summary = dict(row) if row else {}
        providers = self.database.query(
            f"SELECT provider, model, COUNT(*) AS n FROM ai_runs {where} "
            "GROUP BY provider, model ORDER BY n DESC",
            params,
        )
        summary["providers"] = [
            {"provider": item["provider"], "model": item["model"], "calls": int(item["n"])}
            for item in providers
        ]
        if summary.get("estimated_cost") is None:
            summary["estimated_cost"] = None
        return summary

    # -- querying ----------------------------------------------------------
    #: columns the query builder is allowed to interpolate (defence in depth
    #: next to ``ClipQuery``'s own contract)
    _QUERY_COLUMNS: frozenset[str] = frozenset(
        {
            "library_category",
            "provenance",
            "tag_prompt_version",
            "platform",
            "material",
            "material_form",
            "material_state",
            "process_stage",
            "equipment_type",
            "equipment_visible",
            "scene",
            "people",
            "person_role",
            "shot_type",
            "camera_motion",
            "task_id",
            "favorite",
        }
    )

    #: free-text search columns (section 19)
    _FREE_TEXT_COLUMNS: tuple[str, ...] = (
        "description",
        "scene",
        "source_title",
        "source_author",
    )

    def _clip_where(self, query: ClipQuery) -> tuple[list[str], list[Any]]:
        """Build the WHERE clauses and parameters for one query."""

        clauses: list[str] = []
        params: list[Any] = []
        for column, value in query.as_filters():
            if column not in self._QUERY_COLUMNS:  # pragma: no cover - defensive
                raise ValueError(f"unsupported clip filter column: {column}")
            clauses.append(f"c.{column} = ?")
            params.append(value)
        if query.subtitle_type:
            placeholders = ", ".join("?" for _ in query.subtitle_type)
            clauses.append(f"c.subtitle_type IN ({placeholders})")
            params.extend(str(item) for item in query.subtitle_type)
        if query.review_status:
            placeholders = ", ".join("?" for _ in query.review_status)
            clauses.append(f"c.review_status IN ({placeholders})")
            params.extend(str(item) for item in query.review_status)
        if query.min_duration is not None:
            clauses.append("c.duration >= ?")
            params.append(float(query.min_duration))
        if query.max_duration is not None:
            clauses.append("c.duration <= ?")
            params.append(float(query.max_duration))
        if query.min_overall_score is not None:
            clauses.append("c.overall_score >= ?")
            params.append(float(query.min_overall_score))
        if query.max_overall_score is not None:
            clauses.append("c.overall_score <= ?")
            params.append(float(query.max_overall_score))
        if query.min_material_score is not None:
            clauses.append("c.material_score >= ?")
            params.append(float(query.min_material_score))
        if query.created_after:
            clauses.append("c.created_at >= ?")
            params.append(str(query.created_after))
        if query.created_before:
            clauses.append("c.created_at <= ?")
            params.append(str(query.created_before))
        if query.edit_role:
            # EXISTS keeps one row per clip even when several tags match
            clauses.append(
                "EXISTS (SELECT 1 FROM clip_tags ct JOIN tags t ON t.id = ct.tag_id "
                "WHERE ct.clip_id = c.id AND t.category = 'edit_role' AND t.name = ?)"
            )
            params.append(str(query.edit_role))
        text = (query.free_text or "").strip()
        if text:
            like = f"%{text}%"
            searchable = " OR ".join(
                f"COALESCE(c.{column}, '') LIKE ?" for column in self._FREE_TEXT_COLUMNS
            )
            clauses.append(f"({searchable})")
            params.extend([like] * len(self._FREE_TEXT_COLUMNS))
        return clauses, params

    def _clip_order(self, query: ClipQuery) -> str:
        """Allowlisted ORDER BY clause (never interpolate arbitrary input)."""

        option = resolve_sort_option(query.sort_by)
        column = option.column if option.column in CLIP_SORT_COLUMNS else "created_at"
        direction = query.sort_direction or option.direction
        arrow = "ASC" if SortDirection(direction) is SortDirection.ASC else "DESC"
        return f"c.{column} {arrow}, c.id DESC"

    def query_clips(self, query: ClipQuery) -> list[ClipRecord]:
        """Filter + sort + paginate the library (sections 4/5/6)."""

        clauses, params = self._clip_where(query)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT c.* FROM clips c {where} ORDER BY {self._clip_order(query)} "
            "LIMIT ? OFFSET ?"
        )
        rows = self.database.query(
            sql, [*params, int(query.limit), int(query.offset)]
        )
        return [row_to_clip(row, self.clip_tags(int(row["id"]))) for row in rows]

    def count_clips_matching(self, query: ClipQuery) -> int:
        """Total rows matching the filters (independent of pagination)."""

        clauses, params = self._clip_where(query)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self.database.query_one(f"SELECT COUNT(*) AS n FROM clips c {where}", params)
        return int(row["n"]) if row else 0

    def clip_query_stats(self, query: ClipQuery) -> dict[str, float | int]:
        """Count and total duration of the current filter (section 22)."""

        clauses, params = self._clip_where(query)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self.database.query_one(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(c.duration), 0) AS seconds "
            f"FROM clips c {where}",
            params,
        )
        return {
            "count": int(row["n"]) if row else 0,
            "duration_seconds": round(float(row["seconds"]), 2) if row else 0.0,
        }

    # -- human review (sections 10/12/13) ----------------------------------
    def set_review(
        self,
        clip_ids: Iterable[int],
        *,
        status: ReviewStatus | str | None = None,
        note: str | None = None,
    ) -> int:
        """Set the review state of one or many clips (never touches media)."""

        ids = [int(clip_id) for clip_id in clip_ids]
        if not ids:
            return 0
        assignments: list[str] = []
        params: list[Any] = []
        if status is not None:
            assignments.append("review_status = ?")
            params.append(str(status))
        if note is not None:
            assignments.append("review_note = ?")
            params.append(str(note))
        if not assignments:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        params.extend(ids)
        return self.database.execute(
            f"UPDATE clips SET {', '.join(assignments)} WHERE id IN ({placeholders})",
            params,
        )

    def set_favorite(self, clip_ids: Iterable[int], favorite: bool) -> int:
        ids = [int(clip_id) for clip_id in clip_ids]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        return self.database.execute(
            f"UPDATE clips SET favorite = ? WHERE id IN ({placeholders})",
            [int(bool(favorite)), *ids],
        )

    def review_counts(self, *, provenance: str | None = None) -> dict[str, int]:
        """``{review_status: n}`` plus ``favorite`` (section 22)."""

        where = "WHERE provenance = ?" if provenance else ""
        params: list[Any] = [provenance] if provenance else []
        rows = self.database.query(
            f"SELECT COALESCE(NULLIF(review_status, ''), 'unreviewed') AS status, "
            f"COUNT(*) AS n FROM clips {where} GROUP BY status",
            params,
        )
        counts = {row["status"]: int(row["n"]) for row in rows}
        favourite_row = self.database.query_one(
            "SELECT COUNT(*) AS n FROM clips WHERE favorite = 1 "
            + ("AND provenance = ?" if provenance else ""),
            params,
        )
        counts["favorite"] = int(favourite_row["n"]) if favourite_row else 0
        counts["total"] = sum(value for key, value in counts.items() if key != "favorite")
        return counts

    def category_counts(
        self, *, provenance: str | None = None, limit: int = 50
    ) -> list[tuple[str, int]]:
        """``(library_category, count)`` for the sidebar (section 23)."""

        clauses: list[str] = []
        params: list[Any] = []
        if provenance:
            clauses.append("provenance = ?")
            params.append(provenance)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        rows = self.database.query(
            f"SELECT COALESCE(NULLIF(library_category, ''), '(legacy)') AS category, "
            f"COUNT(*) AS n FROM clips {where} GROUP BY category "
            "ORDER BY n DESC, category LIMIT ?",
            params,
        )
        return [(row["category"], int(row["n"])) for row in rows]

    # -- AI audit for one clip (section 28) --------------------------------
    def latest_clip_tagging_run(self, clip_id: int) -> dict[str, Any] | None:
        row = self.database.query_one(
            "SELECT * FROM ai_runs WHERE clip_id = ? AND operation = 'clip_tagging' "
            "ORDER BY id DESC LIMIT 1",
            (clip_id,),
        )
        return dict(row) if row else None

    # -- filter presets (Milestone 5, sections 21/22) ----------------------
    def save_filter_preset(self, name: str, filters: dict[str, Any]) -> int:
        """Create or update a named filter preset (upsert by name)."""

        clean = (name or "").strip()
        if not clean:
            raise ValueError("preset name must not be empty")
        payload = json.dumps(filters or {}, ensure_ascii=False, sort_keys=True)
        now = _iso()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO filter_presets (name, filters_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET filters_json = excluded.filters_json, "
                "updated_at = excluded.updated_at",
                (clean, payload, now, now),
            )
            row = connection.execute(
                "SELECT id FROM filter_presets WHERE name = ?", (clean,)
            ).fetchone()
        preset_id = int(row["id"]) if row else 0
        LOGGER.info("saved filter preset %r (#%s)", clean, preset_id)
        return preset_id

    def list_filter_presets(self) -> list[dict[str, Any]]:
        rows = self.database.query(
            "SELECT id, name, filters_json, created_at, updated_at "
            "FROM filter_presets ORDER BY name"
        )
        presets: list[dict[str, Any]] = []
        for row in rows:
            try:
                filters = json.loads(row["filters_json"])
            except (json.JSONDecodeError, TypeError):  # pragma: no cover - defensive
                filters = {}
            presets.append(
                {
                    "id": int(row["id"]),
                    "name": row["name"],
                    "filters": filters if isinstance(filters, dict) else {},
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return presets

    def get_filter_preset(self, name: str) -> dict[str, Any] | None:
        for preset in self.list_filter_presets():
            if preset["name"] == name:
                return preset
        return None

    def delete_filter_preset(self, name: str) -> int:
        return self.database.execute(
            "DELETE FROM filter_presets WHERE name = ?", ((name or "").strip(),)
        )

    # -- maintenance audit (Milestone 5, sections 28/29) -------------------
    def log_maintenance(
        self,
        operation: str,
        *,
        target_type: str,
        target_id: str | int | None = None,
        details: dict[str, Any] | None = None,
    ) -> int:
        """Record one operator maintenance action (kept apart from ``ai_runs``)."""

        return self.database.insert(
            "maintenance_log",
            {
                "operation": str(operation),
                "target_type": str(target_type),
                "target_id": None if target_id is None else str(target_id),
                "details_json": json.dumps(details or {}, ensure_ascii=False),
                "created_at": _iso(),
            },
        )

    def list_maintenance_log(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.database.query(
            "SELECT * FROM maintenance_log ORDER BY id DESC LIMIT ?", (int(limit),)
        )
        entries: list[dict[str, Any]] = []
        for row in rows:
            try:
                details = json.loads(row["details_json"] or "{}")
            except (json.JSONDecodeError, TypeError):  # pragma: no cover - defensive
                details = {}
            entries.append(
                {
                    "id": int(row["id"]),
                    "operation": row["operation"],
                    "target_type": row["target_type"],
                    "target_id": row["target_id"],
                    "details": details,
                    "created_at": row["created_at"],
                }
            )
        return entries

    # -- measured subtitle analysis (Milestone 6, sections 28/34) ----------
    def save_clip_subtitle_analysis(self, clip_id: int, result: Any) -> None:
        """Persist the measured subtitle evidence for one clip."""

        payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
        self.database.execute(
            "UPDATE clips SET subtitle_analysis_json = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), int(clip_id)),
        )

    def clip_subtitle_analysis(self, clip_id: int) -> dict[str, Any] | None:
        row = self.database.query_one(
            "SELECT subtitle_analysis_json FROM clips WHERE id = ?", (int(clip_id),)
        )
        if row is None or not row["subtitle_analysis_json"]:
            return None
        try:
            payload = json.loads(row["subtitle_analysis_json"])
        except (json.JSONDecodeError, TypeError):  # pragma: no cover - defensive
            return None
        return payload if isinstance(payload, dict) else None

    def save_source_subtitle_analysis(
        self, platform: str, platform_video_id: str, result: Any
    ) -> None:
        """Persist the measured subtitle evidence for a source video preview."""

        payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
        self.database.execute(
            "UPDATE source_videos SET subtitle_analysis_json = ? "
            "WHERE platform = ? AND platform_video_id = ?",
            (json.dumps(payload, ensure_ascii=False), platform, platform_video_id),
        )

    def source_subtitle_analysis(
        self, platform: str, platform_video_id: str
    ) -> dict[str, Any] | None:
        row = self.database.query_one(
            "SELECT subtitle_analysis_json FROM source_videos "
            "WHERE platform = ? AND platform_video_id = ?",
            (platform, platform_video_id),
        )
        if row is None or not row["subtitle_analysis_json"]:
            return None
        try:
            payload = json.loads(row["subtitle_analysis_json"])
        except (json.JSONDecodeError, TypeError):  # pragma: no cover - defensive
            return None
        return payload if isinstance(payload, dict) else None

    def subtitle_cache_get(self, cache_key: str) -> dict[str, Any] | None:
        """Cached measurement for ``(clip hash, analysis version, thresholds)``."""

        if not cache_key:
            return None
        row = self.database.query_one(
            "SELECT result_json FROM subtitle_cache WHERE cache_key = ?", (cache_key,)
        )
        if row is None or not row["result_json"]:
            return None
        try:
            payload = json.loads(row["result_json"])
        except (json.JSONDecodeError, TypeError):  # pragma: no cover - defensive
            return None
        return payload if isinstance(payload, dict) else None

    def subtitle_cache_put(self, cache_key: str, payload: Mapping[str, Any]) -> None:
        if not cache_key:
            return
        version = str(payload.get("analysis_version") or "")
        self.database.execute(
            "INSERT INTO subtitle_cache (cache_key, analysis_version, result_json, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (cache_key) DO UPDATE SET analysis_version = excluded.analysis_version, "
            "result_json = excluded.result_json, created_at = excluded.created_at",
            (
                cache_key,
                version,
                json.dumps(dict(payload), ensure_ascii=False),
                _iso(),
            ),
        )

    def clear_subtitle_cache(self) -> int:
        return self.database.execute("DELETE FROM subtitle_cache")

    # -- conservative local subtitle cleanup (Milestone 9.2) ---------------
    def save_subtitle_cleanup(
        self,
        *,
        clip_id: int,
        status: str | CleanupStatus,
        version: str = CLEANUP_VERSION,
        engine: str = "",
        source_analysis_version: str = "",
        output_path: Path | str | None = None,
        eligible: bool = False,
        skip_reason: str = "",
        regions: Any = None,
        before_metrics: Any = None,
        after_metrics: Any = None,
        settings: Any = None,
        quality: Any = None,
        reduction: Any = None,
        processing_ms: int = 0,
        review_status: str = "pending",
        review_note: str = "",
        review_failure_class: str | None = None,
        reviewed_at: str | None = None,
        provider: str = "",
        input_kind: str = "",
        input_vid: str = "",
        run_id: str = "",
        submitted_at: str | None = None,
        completed_at: str | None = None,
        cloud_status: str = "",
        cloud_error_class: str = "",
        cloud_output_vid: str = "",
        cloud_output_file_name: str = "",
        error: str = "",
    ) -> int:
        """Insert or update the cleanup record for ``(clip_id, version)``."""

        now = _iso()
        payload = (
            str(status),
            str(engine or ""),
            str(source_analysis_version or ""),
            str(output_path) if output_path else None,
            1 if eligible else 0,
            str(skip_reason or ""),
            _json_text(regions),
            _json_text(before_metrics),
            _json_text(after_metrics),
            _json_text(settings),
            _json_text(quality),
            _json_text(reduction),
            int(processing_ms),
            str(review_status or "pending"),
            str(review_note or ""),
            str(review_failure_class) if review_failure_class else None,
            reviewed_at,
            str(provider or ""),
            str(input_kind or ""),
            str(input_vid or ""),
            str(run_id or ""),
            submitted_at,
            completed_at,
            str(cloud_status or ""),
            str(cloud_error_class or ""),
            str(cloud_output_vid or ""),
            str(cloud_output_file_name or ""),
            str(error or "")[:1000] or None,
            now,
            now,
            int(clip_id),
            str(version),
        )
        self.database.execute(
            """
            INSERT INTO subtitle_cleanups (
                status, engine, source_analysis_version, output_path, eligible,
                skip_reason, regions_json, before_metrics_json, after_metrics_json,
                settings_json, quality_json, reduction_json, processing_ms,
                review_status, review_note, review_failure_class, reviewed_at,
                provider, input_kind, input_vid, run_id, submitted_at, completed_at,
                cloud_status, cloud_error_class, cloud_output_vid, cloud_output_file_name,
                error, created_at, updated_at, clip_id, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (clip_id, version) DO UPDATE SET
                status = excluded.status,
                engine = excluded.engine,
                source_analysis_version = excluded.source_analysis_version,
                output_path = excluded.output_path,
                eligible = excluded.eligible,
                skip_reason = excluded.skip_reason,
                regions_json = excluded.regions_json,
                before_metrics_json = excluded.before_metrics_json,
                after_metrics_json = excluded.after_metrics_json,
                settings_json = excluded.settings_json,
                quality_json = excluded.quality_json,
                reduction_json = excluded.reduction_json,
                processing_ms = excluded.processing_ms,
                review_status = excluded.review_status,
                review_note = excluded.review_note,
                review_failure_class = excluded.review_failure_class,
                reviewed_at = excluded.reviewed_at,
                provider = excluded.provider,
                input_kind = excluded.input_kind,
                input_vid = excluded.input_vid,
                run_id = excluded.run_id,
                submitted_at = excluded.submitted_at,
                completed_at = excluded.completed_at,
                cloud_status = excluded.cloud_status,
                cloud_error_class = excluded.cloud_error_class,
                cloud_output_vid = excluded.cloud_output_vid,
                cloud_output_file_name = excluded.cloud_output_file_name,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            payload,
        )
        row = self.database.query_one(
            "SELECT id FROM subtitle_cleanups WHERE clip_id = ? AND version = ?",
            (int(clip_id), str(version)),
        )
        return int(row["id"]) if row else 0

    @staticmethod
    def _subtitle_cleanup_row(row: Any) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "clip_id": int(row["clip_id"]),
            "version": row["version"],
            "status": row["status"],
            "engine": row["engine"] or "",
            "source_analysis_version": row["source_analysis_version"] or "",
            "output_path": row["output_path"] or "",
            "eligible": bool(row["eligible"]),
            "skip_reason": row["skip_reason"] or "",
            "regions": _json_value(row["regions_json"]),
            "before_metrics": _json_value(row["before_metrics_json"]),
            "after_metrics": _json_value(row["after_metrics_json"]),
            "settings": _json_value(row["settings_json"]),
            "quality": _json_value(row["quality_json"]),
            "reduction": _json_value(row["reduction_json"]),
            "processing_ms": int(row["processing_ms"] or 0),
            "review_status": row["review_status"] or "pending",
            "review_note": row["review_note"] or "",
            "review_failure_class": row["review_failure_class"] or "",
            "reviewed_at": row["reviewed_at"],
            "provider": row["provider"] or "",
            "input_kind": row["input_kind"] or "",
            "input_vid": row["input_vid"] or "",
            "run_id": row["run_id"] or "",
            "submitted_at": row["submitted_at"],
            "completed_at": row["completed_at"],
            "cloud_status": row["cloud_status"] or "",
            "cloud_error_class": row["cloud_error_class"] or "",
            "cloud_output_vid": row["cloud_output_vid"] or "",
            "cloud_output_file_name": row["cloud_output_file_name"] or "",
            "error": row["error"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def subtitle_cleanup(
        self, clip_id: int, version: str = CLEANUP_VERSION
    ) -> dict[str, Any] | None:
        """Latest cleanup record for one clip/version, or ``None``."""

        row = self.database.query_one(
            "SELECT * FROM subtitle_cleanups WHERE clip_id = ? AND version = ?",
            (int(clip_id), str(version)),
        )
        return self._subtitle_cleanup_row(row) if row is not None else None

    def latest_subtitle_cleanup(self, clip_id: int) -> dict[str, Any] | None:
        """Newest cleanup record for one clip across versions."""

        row = self.database.query_one(
            "SELECT * FROM subtitle_cleanups WHERE clip_id = ? ORDER BY id DESC LIMIT 1",
            (int(clip_id),),
        )
        return self._subtitle_cleanup_row(row) if row is not None else None

    def list_subtitle_cleanups(
        self,
        *,
        limit: int = 500,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        if status:
            rows = self.database.query(
                "SELECT * FROM subtitle_cleanups WHERE status = ? ORDER BY id DESC LIMIT ?",
                (str(status), int(limit)),
            )
        else:
            rows = self.database.query(
                "SELECT * FROM subtitle_cleanups ORDER BY id DESC LIMIT ?",
                (int(limit),),
            )
        return [self._subtitle_cleanup_row(row) for row in rows]

    def set_subtitle_cleanup_review(
        self,
        clip_id: int,
        *,
        review_status: str,
        note: str = "",
        failure_class: str | None = None,
        version: str = CLEANUP_VERSION,
        reviewed_at: str | None = None,
    ) -> int:
        """Update only the human review fields of one cleanup record."""

        return self.database.execute(
            "UPDATE subtitle_cleanups SET review_status = ?, review_note = ?, "
            "review_failure_class = ?, reviewed_at = ?, updated_at = ? "
            "WHERE clip_id = ? AND version = ?",
            (
                str(review_status),
                str(note or ""),
                str(failure_class) if failure_class else None,
                reviewed_at or _iso(),
                _iso(),
                int(clip_id),
                str(version),
            ),
        )

    def mark_subtitle_cleanup_derivative_deleted(
        self,
        clip_id: int,
        *,
        note: str = "",
        version: str = CLEANUP_VERSION,
    ) -> int:
        """Audited derivative deletion: the original clip is never touched."""

        return self.database.execute(
            "UPDATE subtitle_cleanups SET status = ?, output_path = NULL, "
            "review_status = 'rejected', review_note = ?, reviewed_at = ?, "
            "updated_at = ? WHERE clip_id = ? AND version = ?",
            (
                str(CleanupStatus.DERIVATIVE_DELETED),
                str(note or "derivative deleted by operator"),
                _iso(),
                _iso(),
                int(clip_id),
                str(version),
            ),
        )

    def preferred_media_path(
        self,
        clip: ClipRecord | int,
        *,
        require_review: bool = True,
    ) -> Path:
        """Approved healthy derivative when available, else the original.

        Milestone 9.3 production policy: an automatically generated derivative
        with ``review_status=pending`` (or ``rejected``) never silently replaces
        the original.  One original clip plus one derivative still counts as
        **one** semantic clip.
        """

        record = self.get_clip(clip) if isinstance(clip, int) else clip
        if record is None:
            return Path("")
        original = self.safe_media_path(record.file_path) or Path(record.file_path)
        # choose the newest approved healthy derivative across cleanup versions
        rows = self.database.query(
            "SELECT * FROM subtitle_cleanups WHERE clip_id = ? ORDER BY id DESC",
            (int(record.id or 0),),
        )
        for row in rows:
            cleanup = self._subtitle_cleanup_row(row)
            if str(cleanup.get("status")) != str(CleanupStatus.SUCCEEDED):
                continue
            if require_review and str(cleanup.get("review_status") or "pending") != str(
                CleanupReviewStatus.APPROVED
            ):
                continue
            derivative = self.safe_media_path(cleanup.get("output_path"))
            if derivative is not None and derivative.exists():
                return derivative
        return original

    # -- query approval stats (Milestone 7 query ranking) ------------------
    def query_approved_counts(self) -> dict[str, dict[str, int]]:
        """``{query: {approved, clips}}`` from the source→clip provenance.

        A source discovered by several queries attributes its clips to each of
        them; this is an *indication* used for ranking, not exact attribution.
        """

        rows = self.database.query(
            """
            SELECT sv.matched_queries AS matched, c.id AS clip_id, c.review_status AS review
            FROM source_videos sv
            JOIN clips c ON c.source_video_id = sv.id
            WHERE sv.matched_queries IS NOT NULL AND sv.matched_queries != ''
            """
        )
        stats: dict[str, dict[str, int]] = {}
        for row in rows:
            try:
                queries = json.loads(row["matched"])
            except (json.JSONDecodeError, TypeError):  # pragma: no cover - defensive
                continue
            if not isinstance(queries, list):
                continue
            for query in queries:
                bucket = stats.setdefault(str(query), {"clips": 0, "approved": 0})
                bucket["clips"] += 1
                if str(row["review"] or "") == "approved":
                    bucket["approved"] += 1
        return stats

    # -- source video detail (section 35) ---------------------------------
    def clips_for_source_video(self, source_video_id: int) -> list[ClipRecord]:
        rows = self.database.query(
            "SELECT * FROM clips WHERE source_video_id = ? ORDER BY id", (source_video_id,)
        )
        return [row_to_clip(row, self.clip_tags(int(row["id"]))) for row in rows]

    def safe_media_path(self, path: Path | str | None) -> Path | None:
        """Resolved path for serving/playing, only when inside the library."""

        if path is None:
            return None
        return resolve_within(path, (self.root,))

    # -- physical library health (sections 24/25/26) -----------------------
    _MEDIA_SUFFIXES: tuple[str, ...] = (".mp4", ".mov", ".m4v")
    _THUMB_SUFFIXES: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp")

    def health_report(self, *, limit: int = 200) -> dict[str, Any]:
        """Read-only inventory of the library: DB rows vs files on disk.

        Nothing is deleted or regenerated here.
        """

        clips = self.inventory_clips(limit=limit)
        referenced_media: set[Path] = set()
        referenced_thumbs: set[Path] = set()
        missing_videos: list[dict[str, Any]] = []
        missing_thumbs: list[dict[str, Any]] = []
        present_videos = 0
        for clip in clips:
            video = self.safe_media_path(clip.file_path)
            if video is None:
                missing_videos.append(
                    {"clip_id": clip.id, "path": str(clip.file_path), "reason": "outside_root"}
                )
            elif video.exists():
                present_videos += 1
                referenced_media.add(video)
            else:
                missing_videos.append(
                    {"clip_id": clip.id, "path": str(video), "reason": "missing_file"}
                )
            thumb = self.safe_media_path(clip.thumbnail_path) if clip.thumbnail_path else None
            if clip.thumbnail_path and thumb is None:
                missing_thumbs.append(
                    {
                        "clip_id": clip.id,
                        "path": str(clip.thumbnail_path),
                        "reason": "outside_root",
                    }
                )
            elif thumb is not None:
                if thumb.exists():
                    referenced_thumbs.add(thumb)
                else:
                    missing_thumbs.append(
                        {"clip_id": clip.id, "path": str(thumb), "reason": "missing_file"}
                    )

        orphan_media: list[str] = []
        orphan_thumbs: list[str] = []
        missing_cleanups: list[dict[str, Any]] = []
        missing_approved_derivatives = 0
        cleanup_outputs_present = 0
        cleanup_total_row = self.database.query_one(
            "SELECT COUNT(*) AS n FROM subtitle_cleanups"
        )
        cleanup_total = int(cleanup_total_row["n"]) if cleanup_total_row else 0
        # Derivatives are part of the library inventory: a healthy successful
        # cleanup output must never be reported as an orphan file.
        cleanup_rows = self.database.query(
            "SELECT id, clip_id, status, output_path, review_status FROM subtitle_cleanups "
            "WHERE output_path IS NOT NULL AND output_path != ''"
        )
        for row in cleanup_rows:
            resolved = resolve_candidate(row["output_path"])
            if resolved is None:  # pragma: no cover - defensive
                missing_cleanups.append(
                    {
                        "cleanup_id": int(row["id"]),
                        "clip_id": int(row["clip_id"]),
                        "path": str(row["output_path"]),
                        "reason": "unresolvable",
                    }
                )
            elif resolved.exists():
                cleanup_outputs_present += 1
                referenced_media.add(resolved)
            else:
                if (
                    str(row["status"]) == str(CleanupStatus.SUCCEEDED)
                    and str(row["review_status"] or "") == "approved"
                ):
                    missing_approved_derivatives += 1
                missing_cleanups.append(
                    {
                        "cleanup_id": int(row["id"]),
                        "clip_id": int(row["clip_id"]),
                        "path": str(resolved),
                        "reason": "missing_file",
                        "status": row["status"],
                    }
                )
        root = self.root
        if root.exists():
            for path in root.rglob("*"):
                try:
                    if not path.is_file():
                        continue
                except OSError:  # pragma: no cover - defensive
                    continue
                suffix = path.suffix.lower()
                resolved = resolve_candidate(path)
                if resolved is None:  # pragma: no cover - defensive
                    continue
                if suffix in self._MEDIA_SUFFIXES and resolved not in referenced_media:
                    orphan_media.append(str(resolved))
                elif suffix in self._THUMB_SUFFIXES and resolved not in referenced_thumbs:
                    orphan_thumbs.append(str(resolved))

        return {
            "library_root": str(root),
            "clips_in_db": len(clips),
            "videos_present": present_videos,
            "missing_videos": missing_videos,
            "missing_thumbnails": missing_thumbs,
            "orphan_media": sorted(orphan_media),
            "orphan_thumbnails": sorted(orphan_thumbs),
            "cleanup_records": cleanup_total,
            "cleanup_outputs_present": cleanup_outputs_present,
            "missing_cleanup_outputs": missing_cleanups,
            "missing_approved_derivatives": missing_approved_derivatives,
        }

    # -- reporting ---------------------------------------------------------
    def stats(self) -> dict[str, int]:
        return self.database.stats()

    def tag_counts(self, category: str | None = None) -> list[tuple[str, str, int]]:
        if category:
            rows = self.database.query(
                """
                SELECT tags.name AS name, tags.category AS category, COUNT(*) AS n
                FROM clip_tags JOIN tags ON tags.id = clip_tags.tag_id
                WHERE tags.category = ?
                GROUP BY tags.name, tags.category
                ORDER BY n DESC, tags.name
                """,
                (category,),
            )
        else:
            rows = self.database.query(
                """
                SELECT tags.name AS name, tags.category AS category, COUNT(*) AS n
                FROM clip_tags JOIN tags ON tags.id = clip_tags.tag_id
                GROUP BY tags.name, tags.category
                ORDER BY n DESC, tags.name
                """
            )
        return [(row["name"], row["category"], int(row["n"])) for row in rows]

    def gallery_items(self, clips: Iterable[ClipRecord]) -> list[tuple[str, str]]:
        """``(image_path, caption)`` pairs for the Gradio gallery."""

        items: list[tuple[str, str]] = []
        for clip in clips:
            if clip.thumbnail_path and Path(clip.thumbnail_path).exists():
                image = str(clip.thumbnail_path)
            elif Path(clip.file_path).exists():
                image = str(clip.file_path)
            else:
                continue
            caption = (
                f"#{clip.id} {clip.material} {clip.material_form}/{clip.material_state} "
                f"{clip.process_stage}\n{clip.source_start:.1f}-{clip.source_end:.1f}s "
                f"({clip.duration:.1f}s) overall={clip.overall_score:.2f}"
            )
            items.append((image, caption))
        return items
