"""Material library querying and the corrected dedup behaviour."""

from __future__ import annotations

from pathlib import Path

from core.models import (
    CameraMotion,
    ClipArtifact,
    ClipQuery,
    ClipScores,
    ClipTagging,
    EditRole,
    MaterialForm,
    MaterialState,
    ProcessStage,
    SegmentTiming,
    ShotType,
    SubtitleType,
    TaskRequest,
)
from storage.database import Database
from storage.dedup import DeduplicationService
from storage.library import MaterialLibrary


def _tagging(
    *,
    material: str = "苹果",
    stage: ProcessStage = ProcessStage.DRYING,
    shot: ShotType = ShotType.CLOSE_UP,
    subtitle: SubtitleType = SubtitleType.BOTTOM_SIMPLE,
    people: bool = False,
    overall: float = 0.9,
) -> ClipTagging:
    scores = ClipScores(
        material_relevance=0.95,
        visual_quality=0.9,
        subtitle_cleanliness=0.88,
        stability=0.9,
        composition=0.85,
        overall=overall,
    )
    return ClipTagging(
        material=material,
        material_form=MaterialForm.SLICE,
        material_state=MaterialState.DRYING,
        process_stage=stage,
        equipment_type="heat_pump_dryer",
        equipment_visible=True,
        scene="烘干房内部",
        shot_type=shot,
        camera_motion=CameraMotion.STATIC,
        people=people,
        people_count=1 if people else 0,
        subtitle_type=subtitle,
        subtitle_score=0.1,
        edit_roles=[EditRole.PROCESS, EditRole.DETAIL],
        description="苹果片铺盘",
        scores=scores,
    )


def _artifact(tmp_path: Path, name: str, sha: str | None = None) -> ClipArtifact:
    clip = tmp_path / name
    clip.write_bytes(b"payload-" + name.encode())
    thumb = tmp_path / f"{name}.jpg"
    thumb.write_bytes(b"thumb-" + name.encode())
    return ClipArtifact(
        file_path=clip,
        thumbnail_path=thumb,
        duration=8.0,
        width=1080,
        height=1920,
        fps=30.0,
        size_bytes=clip.stat().st_size,
        sha256=sha or ("a" * 60 + name[-3:].rjust(4, "0")),
        phash="0123456789abcdef",
    )


def _library(settings) -> MaterialLibrary:
    library = MaterialLibrary(Database(settings.paths.database), settings.paths.library_root)
    library.initialize()
    # foreign keys are enforced: seed the task and source video rows first
    library.create_task(TaskRequest(material="苹果干"))
    library.upsert_source_video(
        task_id=1, platform="local", platform_video_id="local_1", source_url="file:///x.mp4"
    )
    library.upsert_source_video(
        task_id=1, platform="local", platform_video_id="local_2", source_url="file:///y.mp4"
    )
    return library


def _add(library: MaterialLibrary, tmp_path: Path, index: int, **tagging_kwargs) -> int:
    return library.insert_clip(
        task_id=1,
        source_video_id=1,
        platform="local",
        platform_video_id="local_1",
        source_url="file:///x.mp4",
        tagging=_tagging(**tagging_kwargs),
        timing=SegmentTiming(start=float(index), end=float(index) + 8.0),
        artifact=_artifact(tmp_path, f"clip_{index}.mp4", sha=f"{index:064d}"),
        content_key=f"key{index}",
    )


def test_query_by_material_and_stage(settings, tmp_path) -> None:
    library = _library(settings)
    _add(library, tmp_path, 1, stage=ProcessStage.DRYING)
    _add(library, tmp_path, 2, stage=ProcessStage.CUTTING)
    _add(library, tmp_path, 3, material="香蕉", stage=ProcessStage.DRYING)

    drying = library.query_clips(ClipQuery(material="苹果", process_stage=ProcessStage.DRYING))
    assert [clip.process_stage for clip in drying] == [ProcessStage.DRYING]
    assert all(clip.material == "苹果" for clip in drying)


def test_query_by_subtitle_shot_and_people(settings, tmp_path) -> None:
    library = _library(settings)
    _add(library, tmp_path, 1, subtitle=SubtitleType.NONE, shot=ShotType.CLOSE_UP)
    _add(library, tmp_path, 2, subtitle=SubtitleType.COLORED_BLOCK, shot=ShotType.WIDE)
    _add(library, tmp_path, 3, people=True, shot=ShotType.WIDE)

    clean = library.query_clips(
        ClipQuery(
            material="苹果",
            subtitle_type=[SubtitleType.NONE, SubtitleType.BOTTOM_SIMPLE],
            shot_type=ShotType.CLOSE_UP,
        )
    )
    assert len(clean) == 1 and clean[0].id == 1
    nobody = library.query_clips(ClipQuery(people=False))
    assert all(clip.people is False for clip in nobody)
    with_people = library.query_clips(ClipQuery(people=True))
    assert [clip.id for clip in with_people] == [3]


def test_query_by_edit_role_uses_the_tag_join(settings, tmp_path) -> None:
    library = _library(settings)
    _add(library, tmp_path, 1)
    result = library.query_clips(ClipQuery(material="苹果", edit_role=EditRole.DETAIL))
    assert len(result) == 1
    empty = library.query_clips(ClipQuery(edit_role=EditRole.HOOK))
    assert empty == []


def test_query_by_duration_and_score(settings, tmp_path) -> None:
    library = _library(settings)
    _add(library, tmp_path, 1, overall=0.95)
    _add(library, tmp_path, 2, overall=0.6)
    high = library.query_clips(ClipQuery(min_overall_score=0.8))
    assert [clip.id for clip in high] == [1]
    none = library.query_clips(ClipQuery(min_duration=20.0))
    assert none == []
    assert len(library.query_clips(ClipQuery(min_duration=5.0, max_duration=10.0))) == 2


def test_query_respects_limit_and_order(settings, tmp_path) -> None:
    library = _library(settings)
    for index in range(5):
        _add(library, tmp_path, index, overall=0.5 + index * 0.05)
    limited = library.query_clips(ClipQuery(material="苹果", limit=2))
    assert len(limited) == 2
    assert limited[0].overall_score >= limited[1].overall_score


def test_multiple_clips_from_one_source_video_are_allowed(settings, tmp_path) -> None:
    library = _library(settings)
    identifiers = [_add(library, tmp_path, index) for index in range(4)]
    assert identifiers == [1, 2, 3, 4]
    clips = library.list_clips(material="苹果")
    assert len(clips) == 4
    assert {clip.source_video_id for clip in clips} == {1}
    assert len({clip.file_path for clip in clips}) == 4


def test_similar_content_is_grouped_but_not_rejected(settings, tmp_path) -> None:
    library = _library(settings)
    library.insert_clip(
        task_id=1,
        source_video_id=1,
        platform="local",
        platform_video_id="local_1",
        source_url="file:///x.mp4",
        tagging=_tagging(),
        timing=SegmentTiming(start=0.0, end=8.0),
        artifact=_artifact(tmp_path, "a.mp4", sha="1" * 64),
        content_key="same-key",
    )
    library.insert_clip(
        task_id=1,
        source_video_id=2,
        platform="local",
        platform_video_id="local_2",
        source_url="file:///y.mp4",
        tagging=_tagging(),
        timing=SegmentTiming(start=3.0, end=11.0),
        artifact=_artifact(tmp_path, "b.mp4", sha="2" * 64),
        content_key="same-key",
    )
    dedup = DeduplicationService(library)
    # both clips are stored: same description is *not* a duplicate
    assert library.count_clips() == 2
    assert dedup.find_duplicate(content_key="same-key") is None
    similar = dedup.find_similar(content_key="same-key")
    assert [item.clip_id for item in similar] == [1, 2]
    groups = dedup.similarity_groups(material="苹果")
    assert groups[0] == ("same-key", 2)


def test_identical_clip_files_are_still_rejected(settings, tmp_path) -> None:
    library = _library(settings)
    artifact = _artifact(tmp_path, "dup.mp4", sha="9" * 64)
    library.insert_clip(
        task_id=1,
        source_video_id=1,
        platform="local",
        platform_video_id="local_1",
        source_url="file:///x.mp4",
        tagging=_tagging(),
        timing=SegmentTiming(start=0.0, end=8.0),
        artifact=artifact,
        content_key="key-a",
    )
    dedup = DeduplicationService(library)
    hit = dedup.find_duplicate(sha256="9" * 64)
    assert hit is not None and hit.detector == "sha256"


def test_phash_distance_threshold_is_configurable(settings, tmp_path) -> None:
    library = _library(settings)
    library.insert_clip(
        task_id=1,
        source_video_id=1,
        platform="local",
        platform_video_id="local_1",
        source_url="file:///x.mp4",
        tagging=_tagging(),
        timing=SegmentTiming(start=0.0, end=8.0),
        artifact=_artifact(tmp_path, "p.mp4", sha="3" * 64),
        content_key="k",
    )
    # phash of the stored clip is 0123456789abcdef
    near = "0123456789abcdee"  # distance 1
    far = "ffffffffffffffff"
    assert DeduplicationService(library, phash_max_distance=2).find_duplicate(phash=near)
    assert DeduplicationService(library, phash_max_distance=2).find_duplicate(phash=far) is None
