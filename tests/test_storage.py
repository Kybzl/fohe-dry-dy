"""SQLite schema, material library service and deduplication."""

from __future__ import annotations

from pathlib import Path

from core.models import (
    ClipArtifact,
    ClipScores,
    ClipTagging,
    EditRole,
    MaterialForm,
    MaterialState,
    ProcessStage,
    RejectReason,
    SegmentTiming,
    SourceVideoStatus,
    SubtitlePolicy,
    SubtitleType,
    TaskRequest,
    TaskStatus,
)
from storage.database import Database
from storage.dedup import DeduplicationService, hamming_distance
from storage.library import MaterialLibrary, material_slug
from storage.schema import TABLE_NAMES


def _tagging(material: str = "苹果", description: str = "苹果片铺盘") -> ClipTagging:
    scores = ClipScores(
        material_relevance=0.95,
        visual_quality=0.9,
        subtitle_cleanliness=0.88,
        stability=0.9,
        composition=0.85,
    )
    scores.overall = scores.recompute_overall()
    return ClipTagging(
        material=material,
        material_form=MaterialForm.SLICE,
        material_state=MaterialState.DRYING,
        process_stage=ProcessStage.TRAY_ARRANGEMENT,
        equipment_type="heat_pump_dryer",
        equipment_visible=True,
        scene="烘干房内部",
        subtitle_type=SubtitleType.BOTTOM_SIMPLE,
        subtitle_score=0.1,
        edit_roles=[EditRole.PROCESS, EditRole.DETAIL],
        description=description,
        scores=scores,
    )


def _artifact(tmp_path: Path, name: str = "apple_test.mp4", sha: str = "a" * 64) -> ClipArtifact:
    clip_file = tmp_path / name
    clip_file.write_bytes(b"payload-" + name.encode())
    thumb = tmp_path / (name + ".jpg")
    thumb.write_bytes(b"thumb")
    return ClipArtifact(
        file_path=clip_file,
        thumbnail_path=thumb,
        duration=8.0,
        width=1080,
        height=1920,
        fps=30.0,
        size_bytes=clip_file.stat().st_size,
        sha256=sha,
        phash="0123456789abcdef",
    )


def test_schema_creates_the_five_required_tables(settings) -> None:
    database = Database(settings.paths.database)
    assert database.missing_tables() == list(TABLE_NAMES)
    database.initialize()
    assert database.missing_tables() == []
    assert "clips" in database.table_names()
    assert "ai_runs" in database.table_names()
    assert database.count("clips") == 0


def test_library_task_lifecycle(settings) -> None:
    library = MaterialLibrary(Database(settings.paths.database), settings.paths.library_root)
    library.initialize()
    request = TaskRequest(material="苹果干", target_clip_count=5, subtitle_policy=SubtitlePolicy.STRICT)
    task_id = library.create_task(request)
    assert task_id > 0
    assert library.get_task(task_id).status is TaskStatus.PENDING
    library.update_task_status(task_id, TaskStatus.SUCCEEDED)
    assert library.get_task(task_id).status is TaskStatus.SUCCEEDED
    assert [task.id for task in library.list_tasks()] == [task_id]


def test_task_request_checkpoint_round_trips_and_updates(settings) -> None:
    library = MaterialLibrary(Database(settings.paths.database), settings.paths.library_root)
    library.initialize()
    request = TaskRequest(
        material="香菇干",
        query_seed="香菇烘干实拍",
        explicit_queries=["香菇烘干实拍", "香菇烘干房"],
        target_clip_count=7,
        library_category="香菇干",
        source="douyin",
        provider="qwen",
        media_backend="ffmpeg",
    )
    task_id = library.create_task(request)

    restored = library.get_task_request(task_id)

    assert restored is not None
    assert restored.model_dump(mode="json") == request.model_dump(mode="json")
    restored.target_clip_count = 12
    restored.resume_task_id = task_id
    library.update_task_request(task_id, restored)
    checkpoint = library.get_task_request(task_id)
    assert checkpoint is not None
    assert checkpoint.target_clip_count == 12
    assert checkpoint.resume_task_id is None
    assert library.get_task(task_id).target_clip_count == 12


def test_source_video_upsert_is_idempotent(settings) -> None:
    library = MaterialLibrary(Database(settings.paths.database), settings.paths.library_root)
    library.initialize()
    first = library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="dy1",
        source_url="https://example.test/1",
        title="苹果烘干",
        duration=42.0,
        status=SourceVideoStatus.CANDIDATE,
    )
    second = library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="dy1",
        source_url="https://example.test/1",
        title="苹果烘干（更新）",
        duration=42.0,
        status=SourceVideoStatus.PROCESSED,
    )
    assert first == second
    record = library.get_source_video("douyin", "dy1")
    assert record.status is SourceVideoStatus.PROCESSED
    assert record.title == "苹果烘干（更新）"
    assert library.url_seen("https://example.test/1") is True


def test_cancelled_task_releases_only_its_in_progress_sources(settings) -> None:
    library = MaterialLibrary(Database(settings.paths.database), settings.paths.library_root)
    library.initialize()
    task_id = library.create_task(TaskRequest(material="香菇干"))
    source_id = library.upsert_source_video(
        task_id=task_id,
        platform="douyin",
        platform_video_id="interrupted",
        source_url="https://example.test/interrupted",
        status=SourceVideoStatus.ANALYZING,
    )
    finished_id = library.upsert_source_video(
        task_id=task_id,
        platform="douyin",
        platform_video_id="finished",
        source_url="https://example.test/finished",
        status=SourceVideoStatus.PROCESSED,
    )

    assert library.release_in_progress_sources(task_id) == 1
    assert library.get_source_video_by_id(source_id).status is SourceVideoStatus.DISCOVERED
    assert library.get_source_video_by_id(finished_id).status is SourceVideoStatus.PROCESSED


def test_insert_clip_writes_tags_and_links(settings, tmp_path) -> None:
    library = MaterialLibrary(Database(settings.paths.database), settings.paths.library_root)
    library.initialize()
    clip_id = library.insert_clip(
        task_id=None,
        source_video_id=None,
        platform="douyin",
        platform_video_id="dy1",
        source_url="https://example.test/1",
        tagging=_tagging(),
        timing=SegmentTiming(start=5.0, end=13.0),
        artifact=_artifact(tmp_path),
        content_key="ck1",
    )
    clip = library.get_clip(clip_id)
    assert clip is not None
    assert clip.material == "苹果"
    assert clip.source_start == 5.0 and clip.source_end == 13.0
    assert clip.duration == 8.0
    assert clip.edit_roles == [EditRole.PROCESS, EditRole.DETAIL]
    assert "heat_pump_dryer" in clip.tags
    assert "process" in clip.tags
    assert library.count_clips() == 1
    assert library.count_clips(material="苹果") == 1
    assert library.count_clips(material="香蕉") == 0
    assert library.find_clip_by_sha256("a" * 64) == clip_id
    assert library.find_clip_by_content_key("ck1") == clip_id
    assert library.find_clip_by_content_key("missing") is None
    assert library.all_phashes() == [(clip_id, "0123456789abcdef")]
    assert library.list_clips(material="苹果")[0].id == clip_id
    categories = {category for _, category, _ in library.tag_counts()}
    assert {"material", "edit_role", "process_stage"} <= categories


def test_library_file_layout_uses_material_folder(settings) -> None:
    library = MaterialLibrary(Database(settings.paths.database), settings.paths.library_root)
    library.initialize()
    clip_path, thumb_path = library.next_clip_paths("苹果干")
    assert clip_path.parent.name == "clips"
    assert thumb_path.parent.name == "thumbnails"
    assert clip_path.parent.parent.name == "苹果干"
    assert clip_path.suffix == ".mp4" and thumb_path.suffix == ".jpg"
    assert clip_path.parent.exists() and thumb_path.parent.exists()
    assert clip_path.stem == thumb_path.stem


def test_material_slug_prefers_readable_names() -> None:
    assert material_slug("苹果干") == "apple"
    assert material_slug("香蕉") == "banana"
    assert material_slug("辣椒") == "chili"
    assert material_slug("").startswith("unknown")
    assert material_slug("未收录物料").startswith("material_")


def test_dedup_service_detects_all_three_layers(settings, tmp_path) -> None:
    library = MaterialLibrary(Database(settings.paths.database), settings.paths.library_root)
    library.initialize()
    library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="dy1",
        source_url="https://example.test/1",
        status=SourceVideoStatus.PROCESSED,
    )
    library.insert_clip(
        task_id=None,
        source_video_id=None,
        platform="douyin",
        platform_video_id="dy1",
        source_url="https://example.test/1",
        tagging=_tagging(),
        timing=SegmentTiming(start=1.0, end=9.0),
        artifact=_artifact(tmp_path),
        content_key="ck1",
    )
    dedup = DeduplicationService(library, phash_max_distance=6)
    assert dedup.source_video_processed("douyin", "dy1") is True
    assert dedup.source_video_processed("douyin", "dy2") is False
    assert dedup.url_seen("https://example.test/1") is True
    assert dedup.find_duplicate(sha256="a" * 64).detector == "sha256"
    # In Milestone 2 content_key is similarity metadata, never a hard duplicate.
    assert dedup.find_duplicate(content_key="ck1") is None
    assert dedup.find_similar(content_key="ck1")[0].clip_id == 1
    hit = dedup.find_duplicate(phash="0123456789abcdee")
    assert hit is not None and hit.reason is RejectReason.DUPLICATE_CLIP
    assert hit.distance == 1
    assert dedup.find_duplicate(phash="ffffffffffffffff") is None
    disabled = DeduplicationService(library, enabled=False)
    assert disabled.find_duplicate(sha256="a" * 64) is None


def test_content_key_duplicate_check_is_opt_in(settings, tmp_path) -> None:
    library = MaterialLibrary(Database(settings.paths.database), settings.paths.library_root)
    library.initialize()
    library.insert_clip(
        task_id=None,
        source_video_id=None,
        platform="douyin",
        platform_video_id="dy1",
        source_url="https://example.test/1",
        tagging=_tagging(),
        timing=SegmentTiming(start=1.0, end=9.0),
        artifact=_artifact(tmp_path),
        content_key="ck1",
    )
    strict = DeduplicationService(library, use_content_key_as_duplicate=True)
    assert strict.find_duplicate(content_key="ck1").detector == "content_key"


def test_hamming_distance_is_a_bit_distance() -> None:
    assert hamming_distance("0123456789abcdef", "0123456789abcdef") == 0
    assert hamming_distance("0000000000000000", "0000000000000001") == 1
    assert hamming_distance("", "abc") == 64
    assert hamming_distance("0" * 16, "f" * 16) == 64
