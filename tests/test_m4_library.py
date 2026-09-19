"""Milestone 4 regressions: library query, review, export, health, tabs.

Every test runs against a temporary library; nothing here touches real Qwen,
Douyin or the production database.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from core.dependencies import build_library
from core.library_service import ClipFilters, LibraryService
from core.models import (
    CLIP_SORT_OPTIONS,
    ClipArtifact,
    ClipQuery,
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
from core.provenance import DOUYIN_REAL, LOCAL_TEST, MOCK


def _add_clip(
    library,
    root: Path,
    *,
    index: int,
    material: str = "苹果",
    material_form: MaterialForm = MaterialForm.SLICE,
    material_state: MaterialState = MaterialState.DRYING,
    process_stage: ProcessStage = ProcessStage.DRYING,
    subtitle_type: SubtitleType = SubtitleType.BOTTOM_SIMPLE,
    edit_roles: tuple[EditRole, ...] = (EditRole.PROCESS,),
    description: str = "苹果片铺在托盘上",
    scene: str = "烘干房内部",
    overall: float = 0.80,
    duration: float = 6.0,
    category: str = "苹果干",
    provenance: str = DOUYIN_REAL,
    platform: str = "douyin",
    platform_video_id: str | None = None,
    create_files: bool = True,
):
    """Insert one fully controlled clip row (with real files by default)."""

    clip_dir = root / category / "clips"
    thumb_dir = root / category / "thumbnails"
    clip_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)
    video = clip_dir / f"clip_{index:03d}.mp4"
    thumb = thumb_dir / f"clip_{index:03d}.jpg"
    if create_files:
        video.write_bytes(b"video")
        thumb.write_bytes(b"thumb")
    tagging = ClipTagging(
        material=material,
        material_form=material_form,
        material_state=material_state,
        process_stage=process_stage,
        shot_type=ShotType.CLOSE_UP,
        scene=scene,
        subtitle_type=subtitle_type,
        subtitle_score=0.1,
        edit_roles=list(edit_roles),
        description=description,
        scores=ClipScores(
            material_relevance=min(1.0, overall + 0.05),
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
        sha256=f"sha{index:04d}",
        phash=f"ph{index:06d}",
    )
    return library.insert_clip(
        task_id=None,
        source_video_id=None,
        platform=platform,
        platform_video_id=platform_video_id or f"76523211528660{index:05d}",
        source_url=f"https://www.douyin.com/video/76523211528660{index:05d}",
        tagging=tagging,
        timing=SegmentTiming(start=0.0, end=duration),
        artifact=artifact,
        content_key=f"ck-{index}",
        source_title=f"苹果烘干样例 {index}",
        source_author="测试作者",
        library_category=category,
        provenance=provenance,
        tag_prompt_version="clip_tagging_v2",
    )


@pytest.fixture
def seeded(settings):
    """25 clips across categories/provenance for the query tests."""

    library = build_library(settings)
    root = settings.paths.library_root
    ids: list[int] = []
    for index in range(1, 21):
        ids.append(
            _add_clip(
                library,
                root,
                index=index,
                category="苹果干",
                material_state=MaterialState.DRYING,
                process_stage=ProcessStage.DRYING,
                overall=0.60 + index * 0.01,
                duration=4.0 + index * 0.25,
                description=f"苹果片烘干过程 第{index}段",
                edit_roles=(EditRole.PROCESS, EditRole.DETAIL),
            )
        )
    for index in range(21, 26):
        ids.append(
            _add_clip(
                library,
                root,
                index=index,
                category="香蕉干",
                material="香蕉",
                material_state=MaterialState.DRIED,
                process_stage=ProcessStage.UNLOADING,
                subtitle_type=SubtitleType.NONE,
                scene="包装车间",
                edit_roles=(EditRole.RESULT,),
                overall=0.90,
                duration=12.0,
                description="香蕉干出料",
                platform="local",
                platform_video_id=f"local_seed_{index}",
                provenance=LOCAL_TEST,
            )
        )
    service = LibraryService(library, settings)
    return library, service, ids


# ---------------------------------------------------------------------------
# 4/5. pagination and sorting
# ---------------------------------------------------------------------------
def test_pagination_slices_in_sql(seeded) -> None:
    _library, service, _ids = seeded
    filters = ClipFilters(provenance="")
    first = service.fetch_page(filters, page=1, page_size=20)
    assert first.total == 25
    assert first.page_count == 2
    assert len(first.clips) == 20
    assert first.has_next and not first.has_previous

    second = service.fetch_page(filters, page=2, page_size=20)
    assert len(second.clips) == 5
    assert second.has_previous and not second.has_next
    ids_first = {clip.id for clip in first.clips}
    assert ids_first.isdisjoint({clip.id for clip in second.clips})


def test_page_size_is_clamped(seeded) -> None:
    _library, service, _ids = seeded
    page = service.fetch_page(ClipFilters(provenance=""), page=1, page_size=5000)
    assert page.page_size == service.settings.library.max_page_size == 100
    page = service.fetch_page(ClipFilters(provenance=""), page=1, page_size="abc")
    assert page.page_size == service.settings.library.default_page_size


def test_page_past_the_end_clamps(seeded) -> None:
    _library, service, _ids = seeded
    page = service.fetch_page(ClipFilters(provenance=""), page=99, page_size=10)
    assert page.page == 3
    assert page.clips, "the last real page is returned instead of nothing"


def test_sorting_options(seeded) -> None:
    _library, service, _ids = seeded
    filters = ClipFilters(provenance="")

    newest = service.fetch_page(filters, sort_by="newest", page_size=5).clips
    oldest = service.fetch_page(filters, sort_by="oldest", page_size=5).clips
    assert newest[0].id != oldest[0].id

    desc = service.fetch_page(filters, sort_by="overall_desc", page_size=5).clips
    asc = service.fetch_page(filters, sort_by="overall_asc", page_size=5).clips
    assert desc[0].overall_score >= desc[-1].overall_score
    assert asc[0].overall_score <= asc[-1].overall_score

    short = service.fetch_page(filters, sort_by="duration_asc", page_size=5).clips
    long_ = service.fetch_page(filters, sort_by="duration_desc", page_size=5).clips
    assert short[0].duration <= short[-1].duration
    assert long_[0].duration >= long_[-1].duration

    material = service.fetch_page(filters, sort_by="material_desc", page_size=5).clips
    assert material[0].material_score >= material[-1].material_score
    clean = service.fetch_page(filters, sort_by="subtitle_clean_desc", page_size=5).clips
    assert clean[0].subtitle_cleanliness_score >= clean[-1].subtitle_cleanliness_score


def test_sort_allowlist_rejects_arbitrary_sql(seeded) -> None:
    _library, service, _ids = seeded
    # an unknown code falls back to the default instead of reaching SQL
    query = service.build_query(ClipFilters(provenance=""), sort_by="id; DROP TABLE clips")
    assert query.sort_option().code == "newest"
    assert service.library.query_clips(query)
    # and the SQL builder only ever emits allowlisted columns
    assert all(option.column in {"created_at", "overall_score", "duration", "material_score", "subtitle_cleanliness_score"} for option in CLIP_SORT_OPTIONS)


# ---------------------------------------------------------------------------
# 2/3. filters
# ---------------------------------------------------------------------------
def test_category_and_material_filters(seeded) -> None:
    _library, service, _ids = seeded
    by_category = service.fetch_page(ClipFilters(library_category="苹果干"), page_size=100)
    assert by_category.total == 20
    assert all(clip.library_category == "苹果干" for clip in by_category.clips)

    by_material = service.fetch_page(ClipFilters(material="苹果"), page_size=100)
    assert by_material.total == 20
    assert all(clip.material == "苹果" for clip in by_material.clips)

    by_state = service.fetch_page(
        ClipFilters(material="苹果", material_state=MaterialState.DRYING), page_size=100
    )
    assert by_state.total == 20
    by_stage = service.fetch_page(
        ClipFilters(process_stage=ProcessStage.UNLOADING), page_size=100
    )
    assert by_stage.total == 5


def test_multi_value_subtitle_filter(seeded) -> None:
    _library, service, _ids = seeded
    page = service.fetch_page(
        ClipFilters(subtitle_type=[SubtitleType.NONE.value, SubtitleType.BOTTOM_SIMPLE.value]),
        page_size=100,
    )
    assert page.total == 25
    only_none = service.fetch_page(
        ClipFilters(subtitle_type=[SubtitleType.NONE.value]), page_size=100
    )
    assert only_none.total == 5


def test_edit_role_filter_has_no_duplicate_rows(seeded) -> None:
    _library, service, _ids = seeded
    page = service.fetch_page(ClipFilters(edit_role=EditRole.PROCESS.value), page_size=100)
    ids = [clip.id for clip in page.clips]
    assert len(ids) == len(set(ids)), "a tag join must not duplicate rows"
    assert page.total == page.total == 20


def test_bool_filters(seeded) -> None:
    _library, service, _ids = seeded
    no_people = service.fetch_page(ClipFilters(people="false"), page_size=100)
    assert no_people.total == 25
    with_people = service.fetch_page(ClipFilters(people="true"), page_size=100)
    assert with_people.total == 0


def test_free_text_search(seeded) -> None:
    _library, service, _ids = seeded
    hit = service.fetch_page(ClipFilters(free_text="第7段"), page_size=100)
    assert hit.total == 1
    by_scene = service.fetch_page(ClipFilters(free_text="烘干房"), page_size=100)
    assert by_scene.total == 20
    by_author = service.fetch_page(ClipFilters(free_text="测试作者"), page_size=100)
    assert by_author.total == 25
    assert service.fetch_page(ClipFilters(free_text="不存在的词"), page_size=100).total == 0


def test_score_and_duration_filters(seeded) -> None:
    _library, service, _ids = seeded
    high = service.fetch_page(ClipFilters(min_overall_score=0.85), page_size=100)
    assert high.total == 5
    bounded = service.fetch_page(
        ClipFilters(min_duration=10.0, max_duration=13.0), page_size=100
    )
    assert bounded.total == 5


def test_default_provenance_filter_is_real_material(seeded) -> None:
    _library, service, _ids = seeded
    default_filters = service.default_filters()
    page = service.fetch_page(default_filters, page_size=100)
    assert page.total == 20, "the operator view favors real Douyin clips"
    assert all(clip.provenance == DOUYIN_REAL for clip in page.clips)
    everything = service.fetch_page(ClipFilters(provenance=""), page_size=100)
    assert everything.total == 25


def test_prompt_version_filter(seeded) -> None:
    _library, service, _ids = seeded
    page = service.fetch_page(
        ClipFilters(provenance="", tag_prompt_version="clip_tagging_v2"), page_size=100
    )
    assert page.total == 25
    assert (
        service.fetch_page(
            ClipFilters(provenance="", tag_prompt_version="clip_tagging_v1"), page_size=100
        ).total
        == 0
    )


# ---------------------------------------------------------------------------
# 10/12/13. review, note, favorite
# ---------------------------------------------------------------------------
def test_review_and_favorite_persistence(seeded) -> None:
    library, service, ids = seeded
    target = ids[0]
    service.set_review([target], status=ReviewStatus.APPROVED, note="Milestone 4 acceptance")
    service.set_favorite([target], True)

    clip = library.get_clip(target)
    assert clip is not None
    assert clip.review_status is ReviewStatus.APPROVED
    assert clip.review_note == "Milestone 4 acceptance"
    assert clip.favorite is True
    # the AI tags are untouched by a human review decision
    assert clip.material == "苹果" and clip.overall_score > 0

    approved = service.fetch_page(
        ClipFilters(provenance="", review_status=[ReviewStatus.APPROVED.value]), page_size=100
    )
    assert [item.id for item in approved.clips] == [target]
    favorites = service.fetch_page(ClipFilters(provenance="", favorite="true"), page_size=100)
    assert [item.id for item in favorites.clips] == [target]

    overview = service.overview(ClipFilters(provenance=""))
    assert overview["approved"] == 1
    assert overview["favorite"] == 1
    assert overview["unreviewed"] == len(ids) - 1


def test_batch_review_and_rejected_keeps_files(seeded) -> None:
    library, service, ids = seeded
    batch = ids[:3]
    service.set_review(batch, status=ReviewStatus.NEEDS_REVIEW)
    service.set_review(batch[:1], status=ReviewStatus.REJECTED, note="构图差")
    clip = library.get_clip(batch[0])
    assert clip is not None
    assert clip.review_status is ReviewStatus.REJECTED
    assert Path(clip.file_path).exists(), "rejecting never deletes the file"


def test_clear_review_state(seeded) -> None:
    library, service, ids = seeded
    service.set_review([ids[0]], status=ReviewStatus.APPROVED, note="x")
    service.set_review([ids[0]], status=ReviewStatus.UNREVIEWED, note="")
    clip = library.get_clip(ids[0])
    assert clip is not None
    assert clip.review_status is ReviewStatus.UNREVIEWED
    assert clip.review_note == ""


# ---------------------------------------------------------------------------
# 22/23. stats
# ---------------------------------------------------------------------------
def test_library_and_category_stats(seeded) -> None:
    _library, service, _ids = seeded
    overview = service.overview(ClipFilters(provenance=""))
    assert overview["total"] == 25
    assert overview["real"] == 20
    assert overview["filtered"] == 25
    assert overview["filtered_seconds"] > 100
    counts = service.category_counts(ClipFilters(provenance=""))
    assert dict(counts) == {"苹果干": 20, "香蕉干": 5}
    real_only = service.category_counts(service.default_filters())
    assert dict(real_only) == {"苹果干": 20}


# ---------------------------------------------------------------------------
# 17/18. export
# ---------------------------------------------------------------------------
def test_json_and_csv_export(seeded, tmp_path) -> None:
    _library, service, ids = seeded
    settings = service.settings
    settings.library.exports_dir = tmp_path / "exports"

    json_result = service.export_by_ids(ids[:2], fmt="json")
    assert json_result["ok"] and json_result["rows"] == 2
    payload = json.loads(Path(json_result["path"]).read_text(encoding="utf-8"))
    assert payload["count"] == 2
    first = payload["clips"][0]
    for field in ("clip_id", "file_path", "library_category", "material", "review_status", "provenance"):
        assert field in first

    csv_result = service.export_by_ids(ids[:2], fmt="csv")
    assert csv_result["ok"]
    with Path(csv_result["path"]).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert rows[0]["library_category"] == "苹果干"
    assert Path(csv_result["path"]).parent == tmp_path / "exports"


def test_export_limit_is_enforced(seeded, tmp_path) -> None:
    _library, service, _ids = seeded
    service.settings.library.exports_dir = tmp_path / "exports"
    service.settings.library.max_export_rows = 5
    filtered = service.export_filtered(ClipFilters(provenance=""), fmt="json")
    assert filtered["ok"] is False
    assert "上限" in filtered["error"]
    assert not list((tmp_path / "exports").glob("*")) if (tmp_path / "exports").exists() else True


def test_export_filtered_paginates_through_results(seeded, tmp_path) -> None:
    _library, service, _ids = seeded
    service.settings.library.exports_dir = tmp_path / "exports"
    service.settings.library.max_export_rows = 100
    service.settings.library.max_page_size = 10
    result = service.export_filtered(ClipFilters(provenance=""), fmt="csv")
    assert result["ok"] and result["rows"] == 25
    with Path(result["path"]).open(encoding="utf-8-sig", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 25


# ---------------------------------------------------------------------------
# 7/24/31. paths, missing files, deletion safety
# ---------------------------------------------------------------------------
def test_video_and_thumbnail_paths_are_contained(seeded, tmp_path) -> None:
    library, service, ids = seeded
    clip = library.get_clip(ids[0])
    assert clip is not None
    assert service.video_path(clip) is not None
    assert service.thumbnail_path(clip) is not None

    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    compromised = clip.model_copy(update={"file_path": outside})
    assert service.video_path(compromised) is None, "paths outside the library are refused"
    assert "missing_file" in service.missing_files(compromised)


def test_missing_file_and_thumbnail_are_reported(settings, tmp_path) -> None:
    library = build_library(settings)
    clip_id = _add_clip(
        library, settings.paths.library_root, index=99, create_files=False
    )
    service = LibraryService(library, settings)
    clip = library.get_clip(clip_id)
    assert clip is not None
    problems = service.missing_files(clip)
    assert set(problems) == {"missing_file", "missing_thumbnail"}
    report = service.health()
    assert [item["clip_id"] for item in report["missing_videos"]] == [clip_id]
    assert report["clips_in_db"] == 1
    assert report["videos_present"] == 0


def test_remove_clip_refuses_paths_outside_the_roots(settings, tmp_path) -> None:
    import tempfile

    library = build_library(settings)
    clip_id = _add_clip(library, settings.paths.library_root, index=100)
    # genuinely outside both safe roots (the project dir and the library root)
    outside = Path(tempfile.mkdtemp(prefix="m4-outside-")) / "outside-root.mp4"
    outside.write_bytes(b"keep me")
    library.database.execute(
        "UPDATE clips SET file_path = ? WHERE id = ?", (str(outside), clip_id)
    )
    report = library.remove_clip(clip_id)
    assert report.found
    assert outside.exists(), "a file outside the library roots is never deleted"
    assert str(outside) in report.refused_files
    assert library.get_clip(clip_id) is None


def test_safe_delete_removes_files_and_keeps_source(settings) -> None:
    library = build_library(settings)
    library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="7652321152866089979",
        source_url="https://www.douyin.com/video/7652321152866089979",
        title="来源",
    )
    source = library.get_source_video("douyin", "7652321152866089979")
    clip_id = _add_clip(library, settings.paths.library_root, index=101)
    clip = library.get_clip(clip_id)
    assert clip is not None
    report = library.remove_clip(clip_id)
    assert report.found and len(report.removed_files) == 2
    assert not Path(clip.file_path).exists()
    assert library.get_source_video("douyin", "7652321152866089979") is not None
    assert source is not None


# ---------------------------------------------------------------------------
# 25/26. health and orphan detection
# ---------------------------------------------------------------------------
def test_health_report_detects_orphans(seeded) -> None:
    library, service, _ids = seeded
    root = library.root
    orphan_video = root / "苹果干" / "clips" / "orphan.mp4"
    orphan_video.write_bytes(b"orphan")
    orphan_thumb = root / "苹果干" / "thumbnails" / "orphan.jpg"
    orphan_thumb.write_bytes(b"orphan")

    report = service.health()
    assert report["clips_in_db"] == 25
    assert report["videos_present"] == 25
    assert str(orphan_video.resolve()) in report["orphan_media"]
    assert str(orphan_thumb.resolve()) in report["orphan_thumbnails"]
    # reporting is read-only
    assert orphan_video.exists() and orphan_thumb.exists()


# ---------------------------------------------------------------------------
# 27/28/35. task history and audit lookups
# ---------------------------------------------------------------------------
def test_task_history_rows_and_detail(settings) -> None:
    from core.models import TaskRequest, TaskStatus

    library = build_library(settings)
    task_id = library.create_task(
        TaskRequest(material="苹果干", target_clip_count=2), status=TaskStatus.SUCCEEDED
    )
    library.add_search_yield(
        task_id=task_id,
        platform="douyin",
        query="苹果干烘干",
        candidate_count=8,
        unique_candidate_count=5,
        preview_accept_count=2,
        download_count=1,
        final_clip_count=1,
    )
    library.upsert_source_video(
        task_id=task_id,
        platform="douyin",
        platform_video_id="7652321152866089979",
        source_url="https://www.douyin.com/video/7652321152866089979",
        title="苹果干烘干实拍",
        author="作者",
        matched_queries=["苹果干烘干"],
    )
    _add_clip(library, settings.paths.library_root, index=200)
    service = LibraryService(library, settings)

    rows = service.task_rows()
    assert rows and rows[0]["task_id"] == task_id
    assert rows[0]["queries"] == 1
    detail = service.task_detail(task_id)
    assert detail["yields"][0]["query"] == "苹果干烘干"
    assert detail["sources"][0]["title"] == "苹果干烘干实拍"
    assert "matched_queries" in detail["sources"][0]


def test_ai_audit_lookup_for_clip(settings) -> None:
    library = build_library(settings)
    clip_id = _add_clip(library, settings.paths.library_root, index=201)
    library.add_ai_run(
        {
            "task_id": None,
            "source_video_id": None,
            "clip_id": clip_id,
            "provider": "qwen",
            "model": "qwen3-vl-flash",
            "operation": "clip_tagging",
            "prompt_version": "clip_tagging_v2",
            "latency_ms": 1234,
            "total_tokens": 4321,
            "origin": "pipeline",
            "status": "ok",
            "result_json": '{"description": "x"}',
            "started_at": "2026-01-01T00:00:00+00:00",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    service = LibraryService(library, settings)
    runs = service.ai_audit(clip_id)
    assert runs and runs[0]["prompt_version"] == "clip_tagging_v2"
    assert runs[0]["latency_ms"] == 1234
    assert service.library.latest_clip_tagging_run(clip_id)["total_tokens"] == 4321


def test_source_detail_panel(settings) -> None:
    library = build_library(settings)
    library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="7652321152866089979",
        source_url="https://www.douyin.com/video/7652321152866089979",
        title="苹果干烘干实拍",
        author="某厂",
        matched_queries=["苹果干烘干"],
    )
    source = library.get_source_video("douyin", "7652321152866089979")
    assert source is not None
    clip_id = _add_clip(library, settings.paths.library_root, index=202)
    library.database.execute(
        "UPDATE clips SET source_video_id = ? WHERE id = ?", (source.id, clip_id)
    )
    service = LibraryService(library, settings)
    clip = library.get_clip(clip_id)
    assert clip is not None
    detail = service.source_detail(clip)
    assert detail["title"] == "苹果干烘干实拍"
    assert detail["matched_queries"] == ["苹果干烘干"]


# ---------------------------------------------------------------------------
# 36. CLI + Gradio surfaces
# ---------------------------------------------------------------------------
def test_check_library_cli_is_read_only(settings, capsys) -> None:
    import app as app_module

    library = build_library(settings)
    _add_clip(library, settings.paths.library_root, index=300)
    code = app_module.run_check_library(settings)
    output = capsys.readouterr().out
    assert code == 0
    assert "数据库片段记录: 1" in output
    assert "本命令只读" in output


def test_list_and_export_cli(settings, tmp_path, capsys) -> None:
    import app as app_module

    settings.library.exports_dir = tmp_path / "exports"
    library = build_library(settings)
    clip_id = _add_clip(library, settings.paths.library_root, index=301)

    args = app_module.parse_args(["--list-clips", "--limit", "10"])
    assert app_module.run_list_clips(args, settings) == 0
    assert f"{clip_id}" in capsys.readouterr().out

    args = app_module.parse_args(
        ["--export-clips", "--clips", str(clip_id), "--export-format", "both"]
    )
    assert app_module.run_export_clips(args, settings) == 0
    output = capsys.readouterr().out
    assert "JSON" in output and "CSV" in output
    assert len(list((tmp_path / "exports").glob("*.json"))) == 1
    assert len(list((tmp_path / "exports").glob("*.csv"))) == 1


@pytest.mark.skipif(
    not __import__("ui.gradio_app", fromlist=["GRADIO_AVAILABLE"]).GRADIO_AVAILABLE,
    reason="gradio is not installed",
)
def test_gradio_library_tabs_build(settings, runner) -> None:
    from ui.gradio_app import build_ui

    demo = build_ui(settings, runner)
    labels = [getattr(block, "label", None) for block in demo.blocks.values()]
    for tab in ("素材采集", "素材库", "任务记录", "系统检查"):
        assert tab in labels, f"missing tab: {tab}"


def test_library_tab_helpers_render_rows(seeded) -> None:
    from ui.library_tab import clip_table_rows, gallery_items, library_header_markdown

    library, service, _ids = seeded
    page = service.fetch_page(ClipFilters(provenance=""), page_size=5)
    rows = clip_table_rows(page.clips, service)
    assert len(rows) == 5
    assert len(rows[0]) == 13
    assert gallery_items(page.clips, service), "thumbnails are served from disk"
    header = library_header_markdown(service.overview(ClipFilters(provenance="")), page)
    assert "素材总数" in header and "第 1/5 页" in header
