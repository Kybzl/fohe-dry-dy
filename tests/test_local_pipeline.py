"""End-to-end Milestone 2 workflow on a real local video (real FFmpeg)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.config import load_settings
from core.models import SourceVideoStatus, TaskRequest, TaskStatus
from core.task_runner import TaskRunner
from media.ffmpeg import FFmpegToolkit
from storage.database import Database
from storage.library import MaterialLibrary


@pytest.fixture
def real_settings(tmp_path: Path, settings):
    """Settings that write into a Chinese named library folder."""

    library_root = tmp_path / "素材库2"
    return load_settings(
        overrides={
            "storage": {
                "library_root": str(library_root),
                "database_path": str(tmp_path / "data" / "library.db"),
                "cache_root": str(tmp_path / "cache"),
            },
            "media": {"thumbnail_candidates": 2, "backend": "ffmpeg"},
            # The synthetic ffmpeg test patterns (testsrc/smptebars/...) contain
            # literal text overlays, so the measured subtitle analyzer would
            # correctly flag them as text-heavy.  These tests exercise the local
            # *pipeline*, so the Milestone 6 feature stays off here.
            "subtitle_analysis": {"enabled": False},
        }
    )


def make_request(settings, files, *, target: int = 3, material: str = "苹果干") -> TaskRequest:
    return TaskRequest(
        material=material,
        target_clip_count=target,
        min_clip_duration=3.0,
        max_clip_duration=15.0,
        library_root=settings.paths.library_root,
        source="local",
        local_files=[Path(item) for item in files],
        provider="mock",
        media_backend="ffmpeg",
    )


def test_real_local_video_end_to_end(real_settings, sample_video: Path, real_ffmpeg) -> None:
    runner = TaskRunner(real_settings)
    result = runner.run(make_request(real_settings, [sample_video], target=3))

    assert result.status in (TaskStatus.SUCCEEDED, TaskStatus.PARTIAL)
    assert result.clips, "the real pipeline must produce clips"
    assert result.stats.downloads == 1
    assert result.stats.errors == 0

    toolkit = FFmpegToolkit(ffmpeg_bin=real_ffmpeg[0], ffprobe_bin=real_ffmpeg[1], timeout=120)
    for clip in result.clips:
        path = Path(clip.file_path)
        assert path.exists() and path.stat().st_size > 1000
        assert path.parent.parent.parent == real_settings.paths.library_root
        assert path.parent.name == "clips"
        assert clip.thumbnail_path and Path(clip.thumbnail_path).exists()
        info = asyncio.run(toolkit.probe(path))
        assert 3.0 - 0.4 <= info.duration <= 15.0 + 0.4
        assert (info.width, info.height) == (640, 360), "clips must keep the source resolution"
        assert clip.duration == pytest.approx(info.duration, abs=0.5)
        assert clip.sha256 and len(clip.sha256) == 64
        assert clip.phash and len(clip.phash) == 16
        assert clip.tags, "every clip needs structured tags"
        assert clip.subtitle_cleanliness_score > 0
        assert clip.stability_score > 0
        assert clip.composition_score > 0
        assert clip.source_start >= 0 and clip.source_end > clip.source_start

    # one source video, several clips: explicitly allowed in Milestone 2
    if len(result.clips) > 1:
        assert len({clip.source_video_id for clip in result.clips}) == 1


def test_original_video_is_never_modified(real_settings, sample_video: Path) -> None:
    before = sample_video.stat()
    runner = TaskRunner(real_settings)
    runner.run(make_request(real_settings, [sample_video], target=2))
    after = sample_video.stat()
    assert sample_video.exists()
    assert before.st_size == after.st_size
    # the staged copy inside the cache is gone
    leftovers = [path for path in real_settings.paths.cache_dir.rglob("*") if path.is_file()]
    assert all(path.suffix != ".mp4" for path in leftovers), leftovers


def test_real_run_persists_clips_tags_and_ai_runs(real_settings, sample_video: Path) -> None:
    runner = TaskRunner(real_settings)
    result = runner.run(make_request(real_settings, [sample_video], target=2))
    library = runner.library

    assert library.count_clips(material="苹果") == len(result.clips)
    assert library.count_clips() >= 1
    runs = library.list_ai_runs(task_id=result.task_id)
    assert runs, "every AI call must be audited"
    assert all(run["prompt_version"] for run in runs)
    assert {run["operation"] for run in runs} <= {
        "preview_filter",
        "segment_detection",
        "clip_tagging",
    }
    summary = library.ai_usage_summary(task_id=result.task_id)
    assert summary["ai_calls"] == len(runs)
    assert result.ai_usage.get("ai_calls") == len(runs)

    clip = library.list_clips(material="苹果")[0]
    assert clip.process_stage is not None
    assert clip.material_form is not None
    assert clip.description
    assert clip.edit_roles


def test_source_report_carries_preview_and_segments(real_settings, sample_video: Path) -> None:
    runner = TaskRunner(real_settings)
    result = runner.run(make_request(real_settings, [sample_video], target=2))
    assert len(result.source_videos) == 1
    report = result.source_videos[0]
    assert report.preview_accept is True
    assert report.status is SourceVideoStatus.PROCESSED
    assert report.segments, "the debug panel needs the detected segments"
    assert any(segment.saved for segment in report.segments)
    assert report.ai_calls >= 1
    assert report.duration and report.duration > 25.0


def test_two_sources_in_one_run(real_settings, sample_video: Path, sample_video_with_audio: Path) -> None:
    runner = TaskRunner(real_settings)
    result = runner.run(
        make_request(real_settings, [sample_video, sample_video_with_audio], target=4)
    )
    assert result.stats.downloads == 2
    assert len({report.source_path for report in result.source_videos}) == 2
    assert result.clips


def test_corrupt_file_does_not_break_the_run(real_settings, sample_video: Path, tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video at all")
    runner = TaskRunner(real_settings)
    result = runner.run(make_request(real_settings, [broken, sample_video], target=2))

    assert result.clips, "the healthy video must still be processed"
    statuses = {Path(report.source_path).name: report for report in result.source_videos}
    assert statuses["broken.mp4"].status is SourceVideoStatus.REJECTED
    assert statuses["broken.mp4"].reject_reason is not None
    assert statuses[sample_video.name].status is SourceVideoStatus.PROCESSED


def test_unicode_library_path_layout(real_settings, sample_video: Path) -> None:
    runner = TaskRunner(real_settings)
    result = runner.run(make_request(real_settings, [sample_video], target=1))
    assert result.clips
    clip = result.clips[0]
    assert "素材库2" in str(clip.file_path)
    assert Path(clip.file_path).parent.parent.name == "苹果干"
    assert Path(clip.file_path).parent.parent.parent.name == "素材库2"


def test_shipped_default_library_root_is_applied_without_touching_disk() -> None:
    """The default output path is D:/素材库2 and is used as-is."""

    settings = load_settings()
    assert settings.paths.library_root == Path("D:/素材库2")
    library = MaterialLibrary(Database(settings.paths.database), settings.paths.library_root)
    assert library.clip_dir_for("苹果干") == Path("D:/素材库2") / "苹果干" / "clips"
    assert library.thumbnail_dir_for("苹果干") == Path("D:/素材库2") / "苹果干" / "thumbnails"
    assert str(library.clip_dir_for("苹果干")).replace("\\", "/") == "D:/素材库2/苹果干/clips"


def test_local_source_without_files_reports_a_clear_error(real_settings) -> None:
    runner = TaskRunner(real_settings)
    result = runner.run(
        TaskRequest(material="苹果干", source="local", local_files=[], provider="mock")
    )
    assert result.status is TaskStatus.FAILED
    assert "本地测试视频" in " ".join(result.messages)
