"""SQLite DDL for the material library.

Five tables are mandated by the specification: ``tasks``, ``source_videos``,
``clips``, ``tags`` and ``clip_tags``.  A few extra columns are added for
traceability and deduplication (``task_id``, ``sha256``, ``content_key``),
which the specification explicitly allows ("at least these tables/fields").
"""

from __future__ import annotations

SCHEMA_VERSION = 16

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        material TEXT NOT NULL,
        target_clip_count INTEGER NOT NULL,
        min_clip_duration REAL NOT NULL,
        max_clip_duration REAL NOT NULL,
        subtitle_policy TEXT NOT NULL,
        status TEXT NOT NULL,
        error TEXT,
        request_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_videos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER,
        platform TEXT NOT NULL,
        platform_video_id TEXT NOT NULL,
        source_url TEXT NOT NULL,
        title TEXT DEFAULT '',
        author TEXT DEFAULT '',
        duration REAL,
        status TEXT NOT NULL,
        reject_reason TEXT,
        preview_material_score REAL,
        preview_subtitle_score REAL,
        preview_quality_score REAL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE SET NULL,
        UNIQUE (platform, platform_video_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS clips (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER,
        source_video_id INTEGER,
        platform TEXT,
        platform_video_id TEXT,
        source_url TEXT,
        material TEXT NOT NULL,
        material_form TEXT NOT NULL,
        material_state TEXT NOT NULL,
        process_stage TEXT NOT NULL,
        equipment_type TEXT,
        equipment_visible INTEGER NOT NULL DEFAULT 0,
        scene TEXT DEFAULT '',
        shot_type TEXT NOT NULL,
        camera_motion TEXT NOT NULL,
        people INTEGER NOT NULL DEFAULT 0,
        people_count INTEGER NOT NULL DEFAULT 0,
        person_role TEXT NOT NULL,
        subtitle_type TEXT NOT NULL,
        subtitle_score REAL NOT NULL DEFAULT 0,
        edit_roles TEXT DEFAULT '[]',
        duration REAL NOT NULL,
        width INTEGER,
        height INTEGER,
        fps REAL,
        material_score REAL NOT NULL DEFAULT 0,
        visual_quality_score REAL NOT NULL DEFAULT 0,
        overall_score REAL NOT NULL DEFAULT 0,
        description TEXT DEFAULT '',
        source_start REAL NOT NULL,
        source_end REAL NOT NULL,
        file_path TEXT NOT NULL,
        thumbnail_path TEXT,
        phash TEXT,
        sha256 TEXT,
        content_key TEXT,
        -- Milestone 3.7 / 4: physical category, provenance, AI prompt version
        -- and human review state.  Older databases get these through
        -- COLUMN_MIGRATIONS below, so both paths end up identical.
        library_category TEXT DEFAULT '',
        provenance TEXT DEFAULT '',
        tag_prompt_version TEXT DEFAULT '',
        review_status TEXT DEFAULT 'unreviewed',
        review_note TEXT DEFAULT '',
        favorite INTEGER NOT NULL DEFAULT 0,
        -- Milestone 6: measured subtitle evidence (versioned JSON)
        subtitle_analysis_json TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (source_video_id) REFERENCES source_videos (id) ON DELETE SET NULL,
        FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        UNIQUE (name, category)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS clip_tags (
        clip_id INTEGER NOT NULL,
        tag_id INTEGER NOT NULL,
        confidence REAL NOT NULL DEFAULT 1.0,
        source TEXT NOT NULL DEFAULT 'ai',
        PRIMARY KEY (clip_id, tag_id),
        FOREIGN KEY (clip_id) REFERENCES clips (id) ON DELETE CASCADE,
        FOREIGN KEY (tag_id) REFERENCES tags (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER,
        source_video_id INTEGER,
        clip_id INTEGER,
        provider TEXT NOT NULL,
        model TEXT,
        operation TEXT NOT NULL,
        prompt_version TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        latency_ms INTEGER,
        input_frame_count INTEGER DEFAULT 0,
        input_video_duration REAL,
        status TEXT NOT NULL,
        error_type TEXT,
        error_message TEXT,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        total_tokens INTEGER,
        estimated_cost REAL,
        result_json TEXT,
        -- pipeline / retag / evaluation (Milestone 3.7)
        origin TEXT DEFAULT 'pipeline',
        created_at TEXT NOT NULL,
        FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE SET NULL,
        FOREIGN KEY (source_video_id) REFERENCES source_videos (id) ON DELETE SET NULL,
        FOREIGN KEY (clip_id) REFERENCES clips (id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS search_yields (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER,
        platform TEXT NOT NULL,
        query TEXT NOT NULL,
        candidate_count INTEGER NOT NULL DEFAULT 0,
        unique_candidate_count INTEGER NOT NULL DEFAULT 0,
        preview_accept_count INTEGER NOT NULL DEFAULT 0,
        download_count INTEGER NOT NULL DEFAULT 0,
        final_clip_count INTEGER NOT NULL DEFAULT 0,
        new_to_system_count INTEGER NOT NULL DEFAULT 0,
        known_source_count INTEGER NOT NULL DEFAULT 0,
        current_run_duplicate_count INTEGER NOT NULL DEFAULT 0,
        already_processed_count INTEGER NOT NULL DEFAULT 0,
        already_represented_count INTEGER NOT NULL DEFAULT 0,
        query_family TEXT DEFAULT '',
        plan_id INTEGER,
        plan_item_id INTEGER,
        planned_order INTEGER NOT NULL DEFAULT 0,
        actual_order INTEGER NOT NULL DEFAULT 0,
        candidate_cap INTEGER NOT NULL DEFAULT 0,
        stop_reason TEXT DEFAULT '',
        reserve_activation_reason TEXT DEFAULT '',
        was_reserve INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS filter_presets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        filters_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS maintenance_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id TEXT,
        details_json TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subtitle_cache (
        cache_key TEXT PRIMARY KEY,
        analysis_version TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    # -- Milestone 9.2: conservative local subtitle-cleanup derivatives -----
    """
    CREATE TABLE IF NOT EXISTS subtitle_cleanups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        clip_id INTEGER NOT NULL,
        version TEXT NOT NULL,
        status TEXT NOT NULL,
        engine TEXT DEFAULT '',
        source_analysis_version TEXT DEFAULT '',
        output_path TEXT,
        eligible INTEGER NOT NULL DEFAULT 0,
        skip_reason TEXT DEFAULT '',
        regions_json TEXT,
        before_metrics_json TEXT,
        after_metrics_json TEXT,
        settings_json TEXT,
        quality_json TEXT,
        reduction_json TEXT,
        processing_ms INTEGER NOT NULL DEFAULT 0,
        review_status TEXT NOT NULL DEFAULT 'pending',
        review_note TEXT DEFAULT '',
        review_failure_class TEXT,
        reviewed_at TEXT,
        -- Milestone 9.8: Volcano Engine VOD refined subtitle erase audit
        provider TEXT DEFAULT '',
        input_kind TEXT DEFAULT '',
        input_vid TEXT DEFAULT '',
        run_id TEXT DEFAULT '',
        submitted_at TEXT,
        completed_at TEXT,
        cloud_status TEXT DEFAULT '',
        cloud_error_class TEXT DEFAULT '',
        cloud_output_vid TEXT DEFAULT '',
        cloud_output_file_name TEXT DEFAULT '',
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (clip_id) REFERENCES clips (id) ON DELETE CASCADE,
        UNIQUE (clip_id, version)
    )
    """,
    # -- Milestone 7: operator controlled collection planning --------------
    """
    CREATE TABLE IF NOT EXISTS collection_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        status TEXT NOT NULL,
        library_category TEXT NOT NULL,
        count_mode TEXT NOT NULL DEFAULT 'all',
        created_from TEXT DEFAULT '',
        target_final_clips INTEGER NOT NULL DEFAULT 0,
        max_preview_candidates INTEGER NOT NULL DEFAULT 0,
        max_downloads INTEGER NOT NULL DEFAULT 0,
        max_ai_tokens INTEGER NOT NULL DEFAULT 0,
        max_runtime_minutes REAL DEFAULT 0,
        coverage_before_json TEXT,
        coverage_after_json TEXT,
        progress_json TEXT,
        pause_reason TEXT DEFAULT '',
        provider_failure_class TEXT DEFAULT '',
        provider_failure_subtype TEXT DEFAULT '',
        provider_ready INTEGER NOT NULL DEFAULT 0,
        provider_name TEXT DEFAULT '',
        provider_model TEXT DEFAULT '',
        provider_operation TEXT DEFAULT '',
        provider_checked_at TEXT DEFAULT '',
        approval_note TEXT DEFAULT '',
        -- Milestone 8 (schema v9): operator archive / acceptance-test markers
        archived INTEGER NOT NULL DEFAULT 0,
        test_plan INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        approved_at TEXT,
        started_at TEXT,
        finished_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS collection_plan_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER NOT NULL,
        process_stage TEXT NOT NULL,
        current_count INTEGER NOT NULL DEFAULT 0,
        target_count INTEGER NOT NULL DEFAULT 0,
        gap INTEGER NOT NULL DEFAULT 0,
        requested_clips INTEGER NOT NULL DEFAULT 1,
        priority TEXT NOT NULL DEFAULT 'medium',
        queries_json TEXT NOT NULL DEFAULT '[]',
        max_candidates INTEGER NOT NULL DEFAULT 0,
        max_downloads INTEGER NOT NULL DEFAULT 0,
        max_tokens INTEGER NOT NULL DEFAULT 0,
        progress_json TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (plan_id) REFERENCES collection_plans (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS collection_plan_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER NOT NULL,
        plan_item_id INTEGER NOT NULL,
        task_id INTEGER NOT NULL,
        query TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY (plan_id) REFERENCES collection_plans (id) ON DELETE CASCADE,
        FOREIGN KEY (plan_item_id) REFERENCES collection_plan_items (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS collection_plan_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER NOT NULL,
        plan_item_id INTEGER,
        event TEXT NOT NULL,
        details_json TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (plan_id) REFERENCES collection_plans (id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_clips_material ON clips (material)",
    "CREATE INDEX IF NOT EXISTS idx_clips_phash ON clips (phash)",
    "CREATE INDEX IF NOT EXISTS idx_clips_sha256 ON clips (sha256)",
    "CREATE INDEX IF NOT EXISTS idx_clips_content_key ON clips (content_key)",
    "CREATE INDEX IF NOT EXISTS idx_clips_task ON clips (task_id)",
    "CREATE INDEX IF NOT EXISTS idx_source_videos_status ON source_videos (status)",
    "CREATE INDEX IF NOT EXISTS idx_ai_runs_task ON ai_runs (task_id)",
    "CREATE INDEX IF NOT EXISTS idx_ai_runs_clip ON ai_runs (clip_id)",
    "CREATE INDEX IF NOT EXISTS idx_ai_runs_operation ON ai_runs (operation)",
    "CREATE INDEX IF NOT EXISTS idx_ai_runs_status ON ai_runs (status)",
    "CREATE INDEX IF NOT EXISTS idx_search_yields_task ON search_yields (task_id)",
    "CREATE INDEX IF NOT EXISTS idx_search_yields_query ON search_yields (query)",
    # Milestone 5 (section 30): only the combinations the library page and the
    # coverage reports actually ask for, verified with EXPLAIN QUERY PLAN.
    "CREATE INDEX IF NOT EXISTS idx_clips_category_stage ON clips (library_category, process_stage)",
    "CREATE INDEX IF NOT EXISTS idx_clips_category_review ON clips (library_category, review_status)",
    "CREATE INDEX IF NOT EXISTS idx_clips_material_state ON clips (material, material_state)",
    "CREATE INDEX IF NOT EXISTS idx_clips_created_at ON clips (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_maintenance_log_created ON maintenance_log (created_at)",
    # Milestone 9.2: cleanup rows are looked up per clip / per status
    "CREATE INDEX IF NOT EXISTS idx_subtitle_cleanups_clip ON subtitle_cleanups (clip_id)",
    "CREATE INDEX IF NOT EXISTS idx_subtitle_cleanups_status ON subtitle_cleanups (status)",
    # Milestone 7: plan lookups by status/category and the plan→task linkage
    "CREATE INDEX IF NOT EXISTS idx_plans_status ON collection_plans (status)",
    "CREATE INDEX IF NOT EXISTS idx_plan_items_plan ON collection_plan_items (plan_id)",
    "CREATE INDEX IF NOT EXISTS idx_plan_tasks_plan ON collection_plan_tasks (plan_id)",
    "CREATE INDEX IF NOT EXISTS idx_plan_tasks_item ON collection_plan_tasks (plan_item_id)",
    "CREATE INDEX IF NOT EXISTS idx_plan_events_plan ON collection_plan_events (plan_id)",
)

#: DDL that depends on columns added by ``COLUMN_MIGRATIONS``.  It must run
#: *after* the in-place column upgrades on existing databases.
POST_MIGRATION_STATEMENTS: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_subtitle_cleanups_review "
    "ON subtitle_cleanups (review_status)",
    "CREATE INDEX IF NOT EXISTS idx_search_yields_plan ON search_yields (plan_id)",
)

TABLE_NAMES: tuple[str, ...] = (
    "tasks",
    "source_videos",
    "clips",
    "tags",
    "clip_tags",
    "ai_runs",
    "search_yields",
    "filter_presets",
    "maintenance_log",
    "subtitle_cache",
    "subtitle_cleanups",
    "collection_plans",
    "collection_plan_items",
    "collection_plan_tasks",
    "collection_plan_events",
)

#: Columns added after the initial release.  ``Database.initialize`` adds any
#: that are missing, which is how Milestone 1 databases are upgraded in place.
COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "tasks": {
        # Milestone 9.9 (schema v16): full non-secret request for exact resume
        "request_json": "TEXT",
    },
    "clips": {
        "subtitle_cleanliness_score": "REAL NOT NULL DEFAULT 0",
        "stability_score": "REAL NOT NULL DEFAULT 0",
        "composition_score": "REAL NOT NULL DEFAULT 0",
        "source_title": "TEXT DEFAULT ''",
        "source_author": "TEXT DEFAULT ''",
        "source_author_id": "TEXT",
        "source_publish_time": "TEXT",
        # Milestone 3.7 (schema v4): physical category vs semantic observation
        "library_category": "TEXT DEFAULT ''",
        "provenance": "TEXT DEFAULT ''",
        "tag_prompt_version": "TEXT DEFAULT ''",
        # Milestone 4 (schema v5): human review state, separate from AI tags
        "review_status": "TEXT DEFAULT 'unreviewed'",
        "review_note": "TEXT DEFAULT ''",
        "favorite": "INTEGER NOT NULL DEFAULT 0",
        # Milestone 6 (schema v7)
        "subtitle_analysis_json": "TEXT",
    },
    "source_videos": {
        "author_id": "TEXT",
        "cover_url": "TEXT",
        "publish_time": "TEXT",
        "source_stats_json": "TEXT",
        "matched_queries": "TEXT",
        "media_url": "TEXT",
        "last_attempt_at": "TEXT",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        # Milestone 6: measured subtitle evidence for the preview frames
        "subtitle_analysis_json": "TEXT",
    },
    "ai_runs": {
        # Milestone 3.7 (schema v4): pipeline / retag / evaluation
        "origin": "TEXT DEFAULT 'pipeline'",
    },
    "collection_plans": {
        # Milestone 8 (schema v9): operator archive + acceptance-test markers
        "archived": "INTEGER NOT NULL DEFAULT 0",
        "test_plan": "INTEGER NOT NULL DEFAULT 0",
        # Milestone 9.7 (schema v13): provider-unavailable pause evidence
        "provider_failure_class": "TEXT DEFAULT ''",
        "provider_failure_subtype": "TEXT DEFAULT ''",
        "provider_ready": "INTEGER NOT NULL DEFAULT 0",
        "provider_name": "TEXT DEFAULT ''",
        "provider_model": "TEXT DEFAULT ''",
        "provider_operation": "TEXT DEFAULT ''",
        "provider_checked_at": "TEXT DEFAULT ''",
    },
    "subtitle_cleanups": {
        # Milestone 9.3 (schema v11): human review + production observability
        "quality_json": "TEXT",
        "reduction_json": "TEXT",
        "processing_ms": "INTEGER NOT NULL DEFAULT 0",
        "review_status": "TEXT NOT NULL DEFAULT 'pending'",
        "review_note": "TEXT DEFAULT ''",
        "review_failure_class": "TEXT",
        "reviewed_at": "TEXT",
        # Milestone 9.8 (schema v15): Volcano Engine VOD cloud audit
        "provider": "TEXT DEFAULT ''",
        "input_kind": "TEXT DEFAULT ''",
        "input_vid": "TEXT DEFAULT ''",
        "run_id": "TEXT DEFAULT ''",
        "submitted_at": "TEXT",
        "completed_at": "TEXT",
        "cloud_status": "TEXT DEFAULT ''",
        "cloud_error_class": "TEXT DEFAULT ''",
        "cloud_output_vid": "TEXT DEFAULT ''",
        "cloud_output_file_name": "TEXT DEFAULT ''",
    },
    "search_yields": {
        # Milestone 9.5 (schema v12): novelty / saturation evidence
        "new_to_system_count": "INTEGER NOT NULL DEFAULT 0",
        "known_source_count": "INTEGER NOT NULL DEFAULT 0",
        "current_run_duplicate_count": "INTEGER NOT NULL DEFAULT 0",
        "already_processed_count": "INTEGER NOT NULL DEFAULT 0",
        "already_represented_count": "INTEGER NOT NULL DEFAULT 0",
        "query_family": "TEXT DEFAULT ''",
        "plan_id": "INTEGER",
        "plan_item_id": "INTEGER",
        "planned_order": "INTEGER NOT NULL DEFAULT 0",
        "actual_order": "INTEGER NOT NULL DEFAULT 0",
        "candidate_cap": "INTEGER NOT NULL DEFAULT 0",
        "stop_reason": "TEXT DEFAULT ''",
        "reserve_activation_reason": "TEXT DEFAULT ''",
        "was_reserve": "INTEGER NOT NULL DEFAULT 0",
    },
}

CLIP_COLUMNS: tuple[str, ...] = (
    "task_id",
    "source_video_id",
    "platform",
    "platform_video_id",
    "source_url",
    "material",
    "material_form",
    "material_state",
    "process_stage",
    "equipment_type",
    "equipment_visible",
    "scene",
    "shot_type",
    "camera_motion",
    "people",
    "people_count",
    "person_role",
    "subtitle_type",
    "subtitle_score",
    "edit_roles",
    "duration",
    "width",
    "height",
    "fps",
    "material_score",
    "visual_quality_score",
    "subtitle_cleanliness_score",
    "stability_score",
    "composition_score",
    "overall_score",
    "description",
    "source_title",
    "source_author",
    "source_author_id",
    "source_publish_time",
    "library_category",
    "provenance",
    "tag_prompt_version",
    "subtitle_analysis_json",
    "source_start",
    "source_end",
    "file_path",
    "thumbnail_path",
    "phash",
    "sha256",
    "content_key",
    "created_at",
)
