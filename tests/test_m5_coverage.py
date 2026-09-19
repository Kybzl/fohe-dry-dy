"""Milestone 5 regressions: coverage analytics, presets, maintenance, reports.

Everything runs on temporary libraries with the mock media toolkit; no test
talks to Qwen, Douyin or the production database.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from core.coverage import (
    PRIORITY_CRITICAL,
    PRIORITY_HEALTHY,
    PRIORITY_HIGH,
    PRIORITY_MEDIUM,
    CoverageAnalyzer,
    priority_for,
)
from core.dependencies import build_library, build_toolkit
from core.library_ops import REVIEW_CSV_FIELDS, LibraryOps
from core.library_service import (
    ClipFilters,
    LibraryService,
    preset_from_filters,
    preset_to_filters,
)
from core.models import (
    ClipArtifact,
    ClipScores,
    ClipTagging,
    EditRole,
    MaterialForm,
    MaterialState,
    ProcessStage,
    ReviewStatus,
    SegmentTiming,
    ShotType,
    SubtitleType,
)
from core.provenance import DOUYIN_REAL, LOCAL_TEST


def _add_clip(
    library,
    root: Path,
    *,
    index: int,
    category: str = "苹果干",
    material: str = "苹果",
    stage: ProcessStage = ProcessStage.DRYING,
    state: MaterialState = MaterialState.DRYING,
    shot: ShotType = ShotType.CLOSE_UP,
    edit_roles: tuple[EditRole, ...] = (EditRole.PROCESS,),
    overall: float = 0.85,
    duration: float = 6.0,
    provenance: str = DOUYIN_REAL,
    review: ReviewStatus = ReviewStatus.UNREVIEWED,
    favorite: bool = False,
    with_thumbnail: bool = True,
    with_video: bool = True,
):
    clip_dir = root / category / "clips"
    thumb_dir = root / category / "thumbnails"
    clip_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)
    video = clip_dir / f"clip_{index:03d}.mp4"
    thumb = thumb_dir / f"clip_{index:03d}.jpg"
    if with_video:
        video.write_bytes(b"video")
    if with_thumbnail:
        thumb.write_bytes(b"thumb")
    tagging = ClipTagging(
        material=material,
        material_form=MaterialForm.SLICE,
        material_state=state,
        process_stage=stage,
        shot_type=shot,
        subtitle_type=SubtitleType.BOTTOM_SIMPLE,
        edit_roles=list(edit_roles),
        description=f"{category} 第 {index} 段",
        scores=ClipScores(
            material_relevance=overall,
            visual_quality=overall,
            subtitle_cleanliness=0.9,
            stability=overall,
            composition=overall,
            overall=overall,
        ),
    )
    artifact = ClipArtifact(
        file_path=video,
        thumbnail_path=thumb,
        duration=duration,
        width=1080,
        height=1920,
        sha256=f"sha{index:05d}",
        phash=f"ph{index:06d}",
    )
    clip_id = library.insert_clip(
        task_id=None,
        source_video_id=None,
        platform="douyin",
        platform_video_id=f"7652321152866{index:05d}",
        source_url=f"https://www.douyin.com/video/7652321152866{index:05d}",
        tagging=tagging,
        timing=SegmentTiming(start=0.0, end=duration),
        artifact=artifact,
        content_key=f"ck-{index}",
        source_title=f"{category} 来源 {index}",
        source_author=f"作者{index % 3}",
        library_category=category,
        provenance=provenance,
        tag_prompt_version="clip_tagging_v2",
    )
    if review is not ReviewStatus.UNREVIEWED or favorite:
        library.set_review([clip_id], status=review, note="")
    if favorite:
        library.set_favorite([clip_id], True)
    return clip_id


@pytest.fixture
def seeded(settings):
    """A small library with known coverage numbers."""

    library = build_library(settings)
    root = settings.paths.library_root
    ids: list[int] = []
    # 苹果干: 3 drying (1 approved+favorite), 1 tray_arrangement, 1 finished
    for index in range(1, 4):
        ids.append(
            _add_clip(
                library,
                root,
                index=index,
                stage=ProcessStage.DRYING,
                state=MaterialState.DRYING,
                shot=ShotType.CLOSE_UP if index < 3 else ShotType.WIDE,
                overall=0.92 if index == 1 else 0.82,
                review=ReviewStatus.APPROVED if index == 1 else ReviewStatus.UNREVIEWED,
                favorite=index == 1,
                edit_roles=(EditRole.PROCESS, EditRole.DETAIL),
            )
        )
    ids.append(
        _add_clip(
            library,
            root,
            index=4,
            stage=ProcessStage.TRAY_ARRANGEMENT,
            state=MaterialState.PREPARED,
            shot=ShotType.MEDIUM,
            overall=0.70,
            edit_roles=(EditRole.DETAIL,),
        )
    )
    ids.append(
        _add_clip(
            library,
            root,
            index=5,
            stage=ProcessStage.FINISHED_PRODUCT,
            state=MaterialState.DRIED,
            shot=ShotType.DETAIL,
            overall=0.65,
            edit_roles=(EditRole.RESULT,),
        )
    )
    # 香蕉干: one local test clip
    ids.append(
        _add_clip(
            library,
            root,
            index=6,
            category="香蕉干",
            material="香蕉",
            stage=ProcessStage.UNLOADING,
            state=MaterialState.FINISHED,
            provenance=LOCAL_TEST,
        )
    )
    analyzer = CoverageAnalyzer(library, settings.coverage)
    service = LibraryService(library, settings)
    ops = LibraryOps(library, settings, toolkit=build_toolkit(settings, backend="mock"))
    return library, analyzer, service, ops, ids


# ---------------------------------------------------------------------------
# 1-6. coverage dimensions
# ---------------------------------------------------------------------------
def test_process_stage_coverage(seeded) -> None:
    _library, analyzer, _service, _ops, _ids = seeded
    stages = {stage.stage: stage for stage in analyzer.process_stage_coverage("苹果干")}
    assert stages["drying"].total == 3
    assert stages["drying"].approved == 1
    assert stages["drying"].favorite == 1
    assert stages["tray_arrangement"].total == 1
    assert stages["finished_product"].total == 1
    assert stages["packaging"].total == 0
    assert stages["drying"].target == 8
    assert stages["drying"].missing == 5


def test_shot_type_coverage(seeded) -> None:
    _library, analyzer, _service, _ops, _ids = seeded
    shots = analyzer.shot_type_coverage("苹果干")
    assert shots == {"close_up": 2, "medium": 1, "wide": 1, "detail": 1}


def test_material_state_coverage(seeded) -> None:
    _library, analyzer, _service, _ops, _ids = seeded
    states = analyzer.state_coverage("苹果干")
    assert states["drying"] == 3
    assert states["prepared"] == 1
    assert states["dried"] == 1


def test_edit_role_coverage_counts_clips_once(seeded) -> None:
    _library, analyzer, _service, _ops, _ids = seeded
    roles = analyzer.edit_role_coverage("苹果干")
    # two roles on the first three clips must not inflate the counts
    assert roles["process"] == 3
    assert roles["detail"] == 4
    assert roles["result"] == 1


def test_quality_distribution_and_averages(seeded) -> None:
    _library, analyzer, _service, _ops, _ids = seeded
    quality = analyzer.quality_distribution("苹果干")
    assert quality[">=0.90"] == 1
    assert quality["0.80-0.89"] == 2
    assert quality["0.70-0.79"] == 1
    assert quality["<0.70"] == 1
    report = analyzer.report("苹果干")
    assert report.total == 5
    assert 0.6 < report.score_averages["overall_score"] < 0.9


def test_review_coverage_and_adjusted_counting(seeded) -> None:
    library, analyzer, _service, _ops, _ids = seeded
    review = analyzer.review_coverage("苹果干")
    assert review["approved"] == 1
    assert review["unreviewed"] == 4
    assert review["favorite"] == 1

    # the default mode counts every clip
    all_mode = {stage.stage: stage for stage in analyzer.process_stage_coverage("苹果干")}
    assert all_mode["drying"].total == 3

    analyzer.settings.count_mode = "approved"
    approved_mode = {
        stage.stage: stage for stage in analyzer.process_stage_coverage("苹果干")
    }
    assert approved_mode["drying"].total == 3
    assert approved_mode["drying"].approved == 1
    # approved-only counting marks drying as a gap again
    assert approved_mode["drying"].priority in (PRIORITY_CRITICAL, PRIORITY_HIGH)


def test_gap_priority_is_deterministic(settings) -> None:
    cfg = settings.coverage
    assert priority_for(0, 8, cfg) == PRIORITY_CRITICAL
    assert priority_for(2, 8, cfg) == PRIORITY_HIGH
    assert priority_for(6, 8, cfg) == PRIORITY_MEDIUM
    assert priority_for(8, 8, cfg) == PRIORITY_HEALTHY
    assert priority_for(12, 8, cfg) == PRIORITY_HEALTHY
    assert priority_for(3, 0, cfg) == PRIORITY_HEALTHY


def test_gap_report_orders_by_urgency(seeded) -> None:
    _library, analyzer, _service, _ops, _ids = seeded
    gaps = analyzer.gap_report("苹果干")
    assert gaps, "the small library must show gaps"
    stages = [gap.stage for gap in gaps]
    assert "packaging" in stages and "drying" in stages
    priorities = [gap.priority for gap in gaps]
    order = {PRIORITY_CRITICAL: 0, PRIORITY_HIGH: 1, PRIORITY_MEDIUM: 2}
    assert [order.get(item, 3) for item in priorities] == sorted(
        order.get(item, 3) for item in priorities
    )
    assert all(gap.missing > 0 for gap in gaps)


def test_recommended_queries_are_deterministic(seeded) -> None:
    _library, analyzer, _service, _ops, _ids = seeded
    first = analyzer.recommended_queries("苹果干", "inside_dryer")
    second = analyzer.recommended_queries("苹果干", "inside_dryer")
    assert first == second
    assert first[0] == "苹果干 烘干机内部"
    assert any("烘干房内部" in query for query in first)
    assert any("苹果" in query for query in first)
    assert analyzer.recommended_queries("苹果干", "unknown_stage")


# ---------------------------------------------------------------------------
# 10-15. yield / cost / source / author / task analysis
# ---------------------------------------------------------------------------
def _seed_yields(library, task_id: int) -> None:
    library.add_search_yield(
        task_id=task_id, platform="douyin", query="苹果干烘干",
        candidate_count=40, unique_candidate_count=31, preview_accept_count=12,
        download_count=7, final_clip_count=9,
    )
    library.add_search_yield(
        task_id=task_id, platform="douyin", query="苹果烘干机",
        candidate_count=25, unique_candidate_count=4, preview_accept_count=0,
        download_count=0, final_clip_count=0,
    )
    library.add_search_yield(
        task_id=task_id, platform="douyin", query="苹果干",
        candidate_count=10, unique_candidate_count=5, preview_accept_count=2,
        download_count=1, final_clip_count=3,
    )


def test_search_yield_ratios(seeded) -> None:
    library, analyzer, _service, _ops, _ids = seeded
    from core.models import TaskRequest

    task_id = library.create_task(TaskRequest(material="苹果干", target_clip_count=2))
    _seed_yields(library, task_id)
    rows = {row["query"]: row for row in analyzer.search_yield_report()}
    assert rows["苹果干烘干"]["unique_rate"] == round(31 / 40, 3)
    assert rows["苹果干烘干"]["preview_accept_rate"] == round(12 / 31, 3)
    assert rows["苹果干烘干"]["download_to_clip_rate"] == round(9 / 7, 3)
    assert rows["苹果干烘干"]["candidate_to_clip_rate"] == round(9 / 40, 3)
    # a real zero (4 unique, 0 accepted) is 0.0; ``None`` means "no denominator"
    assert rows["苹果烘干机"]["preview_accept_rate"] == 0.0
    assert rows["苹果烘干机"]["download_to_clip_rate"] is None


def test_query_ranking_uses_useful_output_and_keeps_zero_yield(seeded) -> None:
    library, analyzer, _service, _ops, _ids = seeded
    from core.models import TaskRequest

    task_id = library.create_task(TaskRequest(material="苹果干", target_clip_count=2))
    _seed_yields(library, task_id)
    ranked = analyzer.ranked_queries(limit=10)
    by_query = {row["query"]: row for row in ranked}
    assert by_query["苹果干烘干"]["usefulness"] > by_query["苹果干"]["usefulness"]
    # a query with the most candidates but no clips must not win
    assert by_query["苹果烘干机"]["usefulness"] == 0.0
    assert "苹果烘干机" in by_query, "zero-yield queries stay visible"
    assert ranked[0]["query"] in {"苹果干烘干", "苹果干"}


def test_query_cost_analysis_reports_unattributable(seeded) -> None:
    library, analyzer, _service, _ops, _ids = seeded
    from core.models import TaskRequest

    task_id = library.create_task(TaskRequest(material="苹果干", target_clip_count=2))
    _seed_yields(library, task_id)
    # a single-query source is attributable, a multi-query source is not
    library.upsert_source_video(
        task_id=task_id, platform="douyin", platform_video_id="7652321152866000001",
        source_url="https://www.douyin.com/video/7652321152866000001",
        matched_queries=["苹果干烘干"],
    )
    library.upsert_source_video(
        task_id=task_id, platform="douyin", platform_video_id="7652321152866000002",
        source_url="https://www.douyin.com/video/7652321152866000002",
        matched_queries=["苹果干烘干", "苹果干"],
    )
    single = library.get_source_video("douyin", "7652321152866000001")
    multi = library.get_source_video("douyin", "7652321152866000002")
    for source, tokens in ((single, 1000), (multi, 2000)):
        library.add_ai_run(
            {
                "task_id": task_id,
                "source_video_id": source.id,
                "clip_id": None,
                "provider": "qwen",
                "model": "m",
                "operation": "preview_filter",
                "prompt_version": "preview_filter_v1",
                "status": "ok",
                "total_tokens": tokens,
                "origin": "pipeline",
                "started_at": "2026-01-01T00:00:00+00:00",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
    rows = {row["query"]: row for row in analyzer.query_cost_analysis()}
    assert rows["苹果干烘干"]["tokens"] == 1000
    assert rows["苹果干烘干"]["attribution"] == "source_matched_single_query"
    unavailable = [row for row in rows.values() if row["attribution"] == "unavailable"]
    assert unavailable, "multi-query sources must be reported as unattributable"
    assert sum(row["tokens"] for row in unavailable) >= 2000


def test_source_author_and_task_yield(seeded) -> None:
    library, analyzer, _service, _ops, ids = seeded
    from core.models import TaskRequest

    task_id = library.create_task(TaskRequest(material="苹果干", target_clip_count=2))
    _seed_yields(library, task_id)
    library.upsert_source_video(
        task_id=task_id, platform="douyin", platform_video_id="7652321152866000003",
        source_url="https://www.douyin.com/video/7652321152866000003",
        title="苹果干烘干实拍", author="某厂", matched_queries=["苹果干烘干"],
    )
    source = library.get_source_video("douyin", "7652321152866000003")
    library.database.execute(
        "UPDATE clips SET source_video_id = ? WHERE id IN (%s)"
        % ", ".join("?" for _ in ids[:2]),
        (source.id, *ids[:2]),
    )
    library.database.execute(
        "UPDATE clips SET task_id = ? WHERE id IN (%s)"
        % ", ".join("?" for _ in ids[:2]),
        (task_id, *ids[:2]),
    )
    sources = analyzer.source_yield()
    assert sources and sources[0]["clips"] == 2
    assert sources[0]["clips_per_source_video"] == 2.0

    authors = {row["author"]: row for row in analyzer.author_yield()}
    assert authors["某厂"]["clips"] == 2

    tasks = {row["task_id"]: row for row in analyzer.task_performance()}
    assert tasks[task_id]["clips"] == 2
    assert tasks[task_id]["candidates"] == 75


# ---------------------------------------------------------------------------
# 21/22. filter presets
# ---------------------------------------------------------------------------
def test_filter_preset_crud(seeded) -> None:
    library, _analyzer, service, _ops, _ids = seeded
    filters = ClipFilters(
        library_category="苹果干",
        process_stage=ProcessStage.DRYING.value,
        people="false",
        min_overall_score=0.85,
    )
    payload = preset_from_filters(filters)
    assert payload["library_category"] == "苹果干"
    assert "material" not in payload, "empty filters are not stored"

    preset_id = library.save_filter_preset("苹果干-高质量过程", payload)
    assert preset_id > 0
    listed = library.list_filter_presets()
    assert [preset["name"] for preset in listed] == ["苹果干-高质量过程"]

    # update by name
    library.save_filter_preset("苹果干-高质量过程", {"library_category": "香蕉干"})
    stored = library.get_filter_preset("苹果干-高质量过程")
    assert stored is not None and stored["filters"]["library_category"] == "香蕉干"

    rebuilt = preset_to_filters(stored)
    assert rebuilt.library_category == "香蕉干"
    assert library.delete_filter_preset("苹果干-高质量过程") == 1
    assert library.list_filter_presets() == []


def test_preset_validation_rejects_unknown_keys(seeded) -> None:
    _library, _analyzer, service, _ops, _ids = seeded
    with pytest.raises(ValueError):
        preset_to_filters({"name": "x", "filters": {"sql": "DROP TABLE clips"}})
    with pytest.raises(ValueError):
        preset_to_filters({"name": "x", "filters": {"library_category": "苹果干", "bogus": 1}})
    assert preset_to_filters({"name": "x", "filters": {}}) == ClipFilters()
    # a preset built from the UI values is always accepted
    assert preset_to_filters({"filters": preset_from_filters(ClipFilters(material="苹果"))})


# ---------------------------------------------------------------------------
# 23. review import / export
# ---------------------------------------------------------------------------
def test_review_csv_export_and_roundtrip(seeded, tmp_path) -> None:
    library, _analyzer, _service, ops, ids = seeded
    ops.settings.library.review_dir = tmp_path / "exports"
    path = ops.export_review_csv()
    assert path.exists()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == len(ids)
    assert set(rows[0].keys()) == set(REVIEW_CSV_FIELDS)

    # importing the file back changes nothing (dry run reports no diffs)
    report = ops.import_review_csv(path, dry_run=True)
    assert report.changed == 0
    assert all(item["reason"] == "unchanged" for item in report.skipped)


def test_review_import_dry_run_then_apply(seeded, tmp_path) -> None:
    library, _analyzer, _service, ops, ids = seeded
    target = ids[3]
    csv_path = tmp_path / "review.csv"
    csv_path.write_text(
        "clip_id,review_status,review_note,favorite\n"
        f"{target},approved,导入批准,1\n",
        encoding="utf-8",
    )
    dry = ops.import_review_csv(csv_path, dry_run=True)
    assert dry.changed == 1
    before = library.get_clip(target)
    assert before is not None and before.review_status is ReviewStatus.UNREVIEWED
    assert before.favorite is False

    applied = ops.import_review_csv(csv_path, dry_run=False)
    assert applied.changed == 1
    after = library.get_clip(target)
    assert after is not None
    assert after.review_status is ReviewStatus.APPROVED
    assert after.review_note == "导入批准"
    assert after.favorite is True
    # AI tags untouched by the review import
    assert after.material == before.material
    assert after.overall_score == before.overall_score


def test_review_import_rejects_invalid_rows(seeded, tmp_path) -> None:
    library, _analyzer, _service, ops, ids = seeded
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text(
        "clip_id,review_status,review_note,favorite\n"
        "abc,approved,x,1\n"
        "999999,approved,x,1\n"
        f"{ids[0]},nonsense,x,1\n"
        f"{ids[1]},approved,x,maybe\n"
        f"{ids[2]},needs_review,ok,0\n",
        encoding="utf-8",
    )
    report = ops.import_review_csv(csv_path, dry_run=True)
    assert report.changed == 1
    reasons = " ".join(str(item.get("reason")) for item in report.skipped)
    assert "invalid clip_id" in reasons
    assert "not found" in reasons
    assert "invalid status" in reasons
    assert "invalid favorite" in reasons


# ---------------------------------------------------------------------------
# 24-29. maintenance
# ---------------------------------------------------------------------------
def test_thumbnail_repair_dry_run_then_apply(seeded) -> None:
    library, _analyzer, _service, ops, ids = seeded
    target = ids[0]
    clip = library.get_clip(target)
    assert clip is not None and clip.thumbnail_path is not None
    Path(clip.thumbnail_path).unlink()

    dry = ops.repair_thumbnails(dry_run=True)
    assert [item["clip_id"] for item in dry.items] == [target]
    assert not Path(clip.thumbnail_path).exists(), "dry run must not write"

    applied = ops.repair_thumbnails(dry_run=False)
    assert applied.changed == 1
    updated = library.get_clip(target)
    assert updated is not None and updated.thumbnail_path is not None
    assert Path(updated.thumbnail_path).exists()
    # the video is never touched
    assert Path(updated.file_path).exists()


def test_quarantine_moves_orphans_but_never_deletes(seeded, tmp_path) -> None:
    library, _analyzer, _service, ops, _ids = seeded
    ops.settings.library.quarantine_dir = tmp_path / "quarantine"
    orphan = library.root / "苹果干" / "clips" / "orphan.mp4"
    orphan.write_bytes(b"orphan")
    orphan_thumb = library.root / "苹果干" / "thumbnails" / "orphan.jpg"
    orphan_thumb.write_bytes(b"orphan")

    dry = ops.quarantine_orphans(dry_run=True)
    assert dry.changed == 2
    assert orphan.exists(), "dry run must not move anything"

    applied = ops.quarantine_orphans(dry_run=False)
    assert applied.changed == 2
    assert not orphan.exists() and not orphan_thumb.exists()
    moved = [Path(item["quarantine_path"]) for item in applied.items]
    assert all(path.exists() for path in moved)
    # the relative path is preserved inside the quarantine folder
    assert any(path.parts[-3:] == ("苹果干", "clips", "orphan.mp4") for path in moved)
    assert all(str(path).startswith(str(tmp_path / "quarantine")) for path in moved)


def test_orphans_outside_the_library_are_skipped(seeded, tmp_path) -> None:
    library, _analyzer, _service, ops, _ids = seeded
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    # pretend the health report listed a file outside the library root
    ops.orphans = lambda include_thumbnails=True: [outside]  # type: ignore[assignment]
    report = ops.quarantine_orphans(dry_run=False)
    assert report.changed == 0
    assert outside.exists()
    assert report.skipped and report.skipped[0]["reason"] == "outside_library"


def test_maintenance_actions_are_audited_separately(seeded) -> None:
    library, _analyzer, _service, ops, _ids = seeded
    before_runs = library.stats()["ai_runs"]
    ops.repair_thumbnails(dry_run=False)
    ops.quarantine_orphans(dry_run=False)
    log = library.list_maintenance_log()
    assert {entry["operation"] for entry in log} >= {"repair_thumbnails", "quarantine_orphans"}
    assert all(entry["target_type"] == "library" for entry in log)
    assert library.stats()["ai_runs"] == before_runs, "maintenance must not write ai_runs"
    # dry runs are not logged
    count = len(log)
    ops.repair_thumbnails(dry_run=True)
    assert len(library.list_maintenance_log()) == count


# ---------------------------------------------------------------------------
# 31-33. CLI reports
# ---------------------------------------------------------------------------
def test_coverage_cli_reports(seeded, capsys) -> None:
    import app as app_module

    library, _analyzer, _service, _ops, _ids = seeded
    settings = library.database.path  # not used; keep signature simple
    settings = _ops.settings
    assert app_module.run_coverage_report(settings, "苹果干") == 0
    out = capsys.readouterr().out
    assert "# 苹果干" in out and "工序覆盖" in out and "缺口" in out

    assert app_module.run_coverage_gaps(settings, "苹果干") == 0
    out = capsys.readouterr().out
    assert "优先补采" in out and "苹果干 烘干机内部" in out

    assert app_module.run_search_yield_report(settings) == 0
    out = capsys.readouterr().out
    assert "搜索词产出与排名" in out and "AI 成本归属" in out


def test_coverage_cli_rejects_unknown_category(seeded, capsys) -> None:
    import app as app_module

    _library, _analyzer, _service, ops, _ids = seeded
    assert app_module.run_coverage_report(ops.settings, "不存在的分类") == 1
    assert "没有分类" in capsys.readouterr().out


@pytest.mark.skipif(
    not __import__("ui.gradio_app", fromlist=["GRADIO_AVAILABLE"]).GRADIO_AVAILABLE,
    reason="gradio is not installed",
)
def test_gradio_coverage_tab_builds(settings, runner) -> None:
    from ui.gradio_app import build_ui

    demo = build_ui(settings, runner)
    labels = [getattr(block, "label", None) for block in demo.blocks.values()]
    for tab in ("素材采集", "素材库", "素材覆盖", "任务记录", "系统检查"):
        assert tab in labels, f"missing tab: {tab}"
