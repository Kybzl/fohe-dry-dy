"""Milestone 9.3 regression tests: production subtitle-cleanup workflow.

No cloud, Douyin, browser or real production library is touched.  The tests
reuse the deterministic M9.2 fakes and add review/lifecycle/report coverage.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path

import pytest

from core.coverage import CoverageAnalyzer
from core.models import SubtitleType
from core.subtitle_cleanup_models import (
    CleanupReviewStatus,
    CleanupStatus,
    REVIEW_FAILURE_CLASSES,
)
from core.subtitle_models import SubtitleAnalysisResult
from storage.database import Database
from tests.test_m9_2_subtitle_cleanup import (
    FakeEngine,
    ScriptedDetector,
    _add_clip,
    _region,
    _service,
    run,
)


def _success(settings, *, index: int = 1, review_failure_pack: bool = False):
    settings.subtitle_cleanup.review_pack = review_failure_pack
    service, detector, toolkit, engine = _service(settings)
    clip_id = _add_clip(service.library, settings.paths.library_root, index=index)
    outcome = run(service.cleanup_clip(clip_id))
    assert outcome.status is CleanupStatus.SUCCEEDED, outcome.lines()
    return service, clip_id, outcome, engine


# ---------------------------------------------------------------------------
# Preferred media policy
# ---------------------------------------------------------------------------
def test_succeeded_pending_review_keeps_original_preferred(settings) -> None:
    service, clip_id, outcome, _engine = _success(settings)
    clip = service.library.get_clip(clip_id)
    assert service.library.preferred_media_path(clip) == Path(clip.file_path)
    record = service.library.subtitle_cleanup(clip_id)
    assert record is not None and record["review_status"] == "pending"


def test_succeeded_approved_prefers_derivative(settings) -> None:
    service, clip_id, outcome, _engine = _success(settings)
    ok, _message = service.review_cleanup(clip_id, status="approved", note="画面自然")
    assert ok
    clip = service.library.get_clip(clip_id)
    assert service.library.preferred_media_path(clip) == Path(outcome.output_path)


def test_succeeded_rejected_prefers_original(settings) -> None:
    service, clip_id, _outcome, _engine = _success(settings)
    ok, _message = service.review_cleanup(
        clip_id, status="rejected", note="模糊", failure_class="visible_blur_patch"
    )
    assert ok
    clip = service.library.get_clip(clip_id)
    assert service.library.preferred_media_path(clip) == Path(clip.file_path)


def test_missing_approved_derivative_prefers_original(settings) -> None:
    service, clip_id, outcome, _engine = _success(settings)
    ok, _message = service.review_cleanup(clip_id, status="approved")
    assert ok
    Path(outcome.output_path).unlink()
    clip = service.library.get_clip(clip_id)
    assert service.library.preferred_media_path(clip) == Path(clip.file_path)


def test_require_review_can_be_disabled_explicitly(settings) -> None:
    settings.subtitle_cleanup.require_review_before_preferred = False
    service, clip_id, outcome, _engine = _success(settings)
    clip = service.library.get_clip(clip_id)
    assert service.library.preferred_media_path(
        clip, require_review=False
    ) == Path(outcome.output_path)


# ---------------------------------------------------------------------------
# Review actions
# ---------------------------------------------------------------------------
def test_approval_action_records_note(settings) -> None:
    service, clip_id, _outcome, _engine = _success(settings)
    ok, message = service.review_cleanup(
        clip_id, status="approved", note="20/50/80% 画面均自然"
    )
    assert ok and "approved" in message
    record = service.library.subtitle_cleanup(clip_id)
    assert record is not None
    assert record["review_status"] == "approved"
    assert "20/50/80%" in record["review_note"]
    assert record["reviewed_at"]


def test_rejection_action_requires_and_records_failure_class(settings) -> None:
    service, clip_id, _outcome, _engine = _success(settings)
    ok, message = service.review_cleanup(clip_id, status="rejected", note="no class")
    assert not ok and "failure-class" in message
    ok, _message = service.review_cleanup(
        clip_id, status="rejected", note="残留字幕", failure_class="residual_subtitle"
    )
    assert ok
    record = service.library.subtitle_cleanup(clip_id)
    assert record is not None
    assert record["review_status"] == "rejected"
    assert record["review_failure_class"] == "residual_subtitle"
    assert record["review_note"] == "残留字幕"


def test_review_reset_to_pending(settings) -> None:
    service, clip_id, _outcome, _engine = _success(settings)
    assert service.review_cleanup(clip_id, status="approved")[0]
    assert service.review_cleanup(
        clip_id, status="pending", note="重新人工复核"
    )[0]
    record = service.library.subtitle_cleanup(clip_id)
    assert record is not None and record["review_status"] == "pending"


def test_failure_class_vocabulary_is_controlled(settings) -> None:
    service, clip_id, _outcome, _engine = _success(settings)
    ok, message = service.review_cleanup(
        clip_id, status="rejected", failure_class="made_up_class"
    )
    assert not ok and "unknown failure class" in message
    assert "residual_subtitle" in REVIEW_FAILURE_CLASSES


# ---------------------------------------------------------------------------
# Derivative lifecycle / audit
# ---------------------------------------------------------------------------
def test_delete_derivative_never_deletes_original(settings) -> None:
    service, clip_id, outcome, _engine = _success(settings)
    clip = service.library.get_clip(clip_id)
    original = Path(clip.file_path)
    before_sha = hashlib.sha256(original.read_bytes()).hexdigest()
    ok, message = service.delete_derivative(clip_id, note="人工拒绝：模糊")
    assert ok, message
    assert original.exists()
    assert hashlib.sha256(original.read_bytes()).hexdigest() == before_sha
    assert not Path(outcome.output_path).exists()
    record = service.library.subtitle_cleanup(clip_id)
    assert record is not None
    assert record["status"] == str(CleanupStatus.DERIVATIVE_DELETED)
    assert record["output_path"] == ""
    assert record["review_status"] == "rejected"
    assert service.library.preferred_media_path(clip) == original


def test_delete_derivative_is_idempotent_and_audited(settings) -> None:
    service, clip_id, _outcome, _engine = _success(settings)
    assert service.delete_derivative(clip_id, note="first")[0]
    ok, _message = service.delete_derivative(clip_id, note="second")
    assert ok
    entries = service.library.list_maintenance_log(limit=50)
    operations = [entry["operation"] for entry in entries]
    assert "cleanup_derivative_deleted" in operations
    clip = service.library.get_clip(clip_id)
    assert Path(clip.file_path).exists()


def test_force_regeneration_resets_review_and_audits(settings) -> None:
    service, clip_id, _outcome, engine = _success(settings)
    assert service.review_cleanup(clip_id, status="approved", note="v1 ok")[0]
    record = service.library.subtitle_cleanup(clip_id)
    assert record is not None and record["review_status"] == "approved"
    second = run(service.cleanup_clip(clip_id, force=True))
    assert second.status is CleanupStatus.SUCCEEDED
    assert len(engine.calls) == 2
    record = service.library.subtitle_cleanup(clip_id)
    assert record is not None and record["review_status"] == "pending"
    operations = [entry["operation"] for entry in service.library.list_maintenance_log(limit=50)]
    assert "cleanup_generated" in operations
    assert "cleanup_regenerated" in operations


def test_review_actions_are_audited(settings) -> None:
    service, clip_id, _outcome, _engine = _success(settings)
    service.review_cleanup(clip_id, status="approved", note="ok")
    service.review_cleanup(
        clip_id, status="rejected", note="blur", failure_class="visible_blur_patch"
    )
    service.review_cleanup(clip_id, status="pending", note="reset")
    operations = [entry["operation"] for entry in service.library.list_maintenance_log(limit=50)]
    assert "cleanup_approved" in operations
    assert "cleanup_rejected" in operations
    assert "cleanup_review_reset" in operations


# ---------------------------------------------------------------------------
# Candidate scan / bounded batch
# ---------------------------------------------------------------------------
def test_candidate_dry_run_uses_stored_metadata_only(settings) -> None:
    service, _detector, _toolkit, engine = _service(settings)
    root = settings.paths.library_root
    eligible_id = _add_clip(service.library, root, index=1)
    complex_id = _add_clip(service.library, root, index=2)
    control_id = _add_clip(service.library, root, index=3)
    service.library.database.execute(
        "UPDATE clips SET subtitle_type = ? WHERE id = ?",
        (str(SubtitleType.COMPLEX), complex_id),
    )
    service.library.database.execute(
        "UPDATE clips SET subtitle_type = ? WHERE id = ?",
        (str(SubtitleType.WATERMARK_ONLY), control_id),
    )
    candidates = service.candidate_scan()
    by_id = {item.clip_id: item for item in candidates}
    assert by_id[eligible_id].eligibility == "eligible_unprocessed"
    assert by_id[complex_id].eligibility == "ineligible"
    assert by_id[control_id].eligibility == "not_needed"
    assert engine.calls == []
    assert _detector.calls == 0


def test_bounded_batch_obeys_limit_and_skips_processed(settings) -> None:
    service, _detector, _toolkit, engine = _service(settings)
    root = settings.paths.library_root
    ids = [_add_clip(service.library, root, index=index) for index in range(1, 5)]
    outcomes = run(service.cleanup_batch(limit=2))
    assert len(outcomes) == 2
    assert len(engine.calls) == 2
    records = [service.library.subtitle_cleanup(clip_id) for clip_id in ids]
    assert sum(1 for record in records if record is not None) == 2


def test_batch_is_idempotent(settings) -> None:
    service, _detector, _toolkit, engine = _service(settings)
    root = settings.paths.library_root
    for index in range(1, 4):
        _add_clip(service.library, root, index=index)
    first = run(service.cleanup_batch(limit=5))
    calls_after_first = len(engine.calls)
    second = run(service.cleanup_batch(limit=5))
    assert len(first) == 3
    assert second == []
    assert len(engine.calls) == calls_after_first


def test_batch_does_not_retry_failed_without_force(settings) -> None:
    engine = FakeEngine(fail=True)
    service, _detector, _toolkit, _engine = _service(settings, engine=engine)
    root = settings.paths.library_root
    _add_clip(service.library, root, index=1)
    first = run(service.cleanup_batch(limit=5))
    assert len(first) == 1 and first[0].status is CleanupStatus.FAILED_PROCESSING
    calls = engine.attempts
    second = run(service.cleanup_batch(limit=5))
    assert second == []
    assert engine.attempts == calls
    forced = run(service.cleanup_batch(limit=5, force=True))
    assert len(forced) == 1
    assert engine.attempts == calls + 1


def test_keyboard_interrupt_cleans_temp_and_propagates(settings) -> None:
    class InterruptEngine(FakeEngine):
        async def apply(self, *args, **kwargs):  # type: ignore[override]
            raise KeyboardInterrupt()

    service, _detector, _toolkit, _engine = _service(settings, engine=InterruptEngine())
    clip_id = _add_clip(service.library, settings.paths.library_root, index=1)
    with pytest.raises(KeyboardInterrupt):
        run(service.cleanup_clip(clip_id))
    temp_root = settings.paths.cache_dir / "subtitle_cleanup"
    assert not temp_root.exists() or not any(temp_root.rglob("*"))


# ---------------------------------------------------------------------------
# Semantic duplication / coverage / report
# ---------------------------------------------------------------------------
def test_cleanup_does_not_create_clip_or_change_coverage(settings) -> None:
    service, _detector, _toolkit, _engine = _service(settings)
    root = settings.paths.library_root
    for index in range(1, 4):
        _add_clip(service.library, root, index=index)
    analyzer = CoverageAnalyzer(service.library, settings.coverage)
    before_report = analyzer.report()
    before = (
        service.library.count_clips(),
        before_report.total,
        [(stage.stage, stage.total) for stage in before_report.stages],
    )
    run(service.cleanup_batch(limit=5))
    after_report = analyzer.report()
    after = (
        service.library.count_clips(),
        after_report.total,
        [(stage.stage, stage.total) for stage in after_report.stages],
    )
    assert before == after


def test_report_aggregation(settings) -> None:
    service, clip_id, _outcome, _engine = _success(settings)
    service.review_cleanup(clip_id, status="approved", note="ok")
    report = service.report()
    assert report["counts"]["total_library_clips"] == 1
    assert report["counts"]["approved"] == 1
    assert report["successes"]
    row = report["successes"][0]
    for key in (
        "before_cleanliness",
        "after_cleanliness",
        "cleanliness_delta",
        "before_regions",
        "after_regions",
        "masked_area_ratio",
        "outside_mask_mean_diff",
        "duration_delta",
        "processing_seconds",
    ):
        assert key in row
    assert report["class_metrics"]


# ---------------------------------------------------------------------------
# Review pack
# ---------------------------------------------------------------------------
def test_review_pack_generation_and_text_escaping(settings, tmp_path: Path) -> None:
    settings.subtitle_cleanup.reports_dir = tmp_path / "reports"
    service, clip_id, _outcome, _engine = _success(
        settings, review_failure_pack=True
    )
    ok, _message = service.review_cleanup(
        clip_id, status="approved", note="<script>alert(1)</script> 画面自然"
    )
    assert ok
    packs = run(service.build_review_pack(clip_ids=[clip_id]))
    assert packs
    index_path = packs[0]
    assert index_path.exists() and index_path.name == "index.html"
    html_text = index_path.read_text(encoding="utf-8")
    assert "clip #" in html_text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_text
    assert "<script>alert(1)</script>" not in html_text
    assert (index_path.parent / "frames").exists()


def test_complex_clip_remains_ineligible(settings) -> None:
    detector = ScriptedDetector(
        before=[
            _region(y1=0.72, y2=0.80, text="底部"),
            _region(y1=0.20, y2=0.32, text="顶部"),
        ]
    )
    service, _detector, _toolkit, engine = _service(settings, detector=detector)
    clip_id = _add_clip(service.library, settings.paths.library_root, index=1)
    outcome = run(service.cleanup_clip(clip_id))
    assert outcome.status is CleanupStatus.INELIGIBLE
    assert engine.calls == []
    service.library.database.execute(
        "UPDATE clips SET subtitle_type = ? WHERE id = ?",
        (str(SubtitleType.COMPLEX), clip_id),
    )
    candidates = service.candidate_scan()
    assert candidates[0].eligibility == "ineligible"


def test_schema_v11_migrates_existing_cleanup_table(tmp_path: Path) -> None:
    """The v10 subtitle_cleanups table upgrades in place before the new index."""

    db_path = tmp_path / "library.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE subtitle_cleanups (
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
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (clip_id, version)
        )
        """
    )
    connection.commit()
    connection.close()
    database = Database(db_path)
    database.initialize()
    columns = {
        row["name"]
        for row in database.query("PRAGMA table_info(subtitle_cleanups)")
    }
    assert {"review_status", "review_note", "reviewed_at", "quality_json", "reduction_json"} <= columns
    indexes = {
        row["name"] for row in database.query("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "idx_subtitle_cleanups_review" in indexes
