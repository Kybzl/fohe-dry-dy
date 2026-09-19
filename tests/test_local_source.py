"""``LocalFileSource``: local files exposed through the VideoSource contract."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.models import RejectReason
from media.downloader import LocalFileDownloader, local_path_from_url
from media.ffmpeg import FFmpegToolkit, MockMediaToolkit
from sources.base import SourceError
from sources.local import LocalFileSource


def run(coro):
    return asyncio.run(coro)


def toolkit_from(pair) -> FFmpegToolkit:
    return FFmpegToolkit(ffmpeg_bin=pair[0], ffprobe_bin=pair[1], timeout=120.0)


def test_search_returns_one_candidate_per_file(sample_video: Path, real_ffmpeg) -> None:
    source = LocalFileSource([sample_video], toolkit=toolkit_from(real_ffmpeg))
    candidates = run(source.search("苹果干", 10))
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.platform == "local"
    assert candidate.platform_video_id.startswith("local_")
    assert candidate.source_url.startswith("file://")
    assert candidate.duration == pytest.approx(30.0, abs=1.5)
    assert candidate.metadata["file_name"].endswith(".mp4")
    assert candidate.metadata.get("unreadable") is not True
    assert source.search_mode == "collection"


def test_search_handles_multiple_files(sample_video: Path, sample_video_with_audio: Path, real_ffmpeg) -> None:
    source = LocalFileSource(
        [sample_video, sample_video_with_audio], toolkit=toolkit_from(real_ffmpeg)
    )
    candidates = run(source.search("苹果干", 10))
    assert len(candidates) == 2
    assert len({item.platform_video_id for item in candidates}) == 2
    assert run(source.search("苹果干", 1)) == run(source.search("苹果干", 1))[:1]


def test_expand_directory_filters_by_extension(tmp_path: Path) -> None:
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "b.MOV").write_bytes(b"x")
    (tmp_path / "c.txt").write_bytes(b"x")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "d.m4v").write_bytes(b"x")

    source = LocalFileSource(toolkit=MockMediaToolkit())
    flat = source.expand(tmp_path)
    assert [path.name for path in flat] == ["a.mp4", "b.MOV"]

    recursive = LocalFileSource(recursive=True, toolkit=MockMediaToolkit())
    assert [path.name for path in recursive.expand(tmp_path)] == ["a.mp4", "b.MOV", "d.m4v"]


def test_missing_file_is_marked_unreadable(tmp_path: Path) -> None:
    from analyzers.candidate_filter import CandidateFilter
    from storage.database import Database
    from storage.dedup import DeduplicationService
    from storage.library import MaterialLibrary

    source = LocalFileSource(
        [tmp_path / "missing.mp4"], toolkit=MockMediaToolkit()
    )
    candidates = run(source.search("苹果干", 5))
    assert len(candidates) == 1
    assert candidates[0].metadata["unreadable"] is True

    library = MaterialLibrary(Database(tmp_path / "data" / "library.db"), tmp_path / "library")
    library.initialize()
    decision = CandidateFilter(DeduplicationService(library)).evaluate(
        candidates[0], material="苹果干"
    )
    assert decision.accepted is False
    assert decision.reason is RejectReason.CORRUPT_MEDIA


def test_corrupt_media_is_reported_as_corrupt(tmp_path: Path, real_ffmpeg) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"this is definitely not a video")
    source = LocalFileSource([broken], toolkit=toolkit_from(real_ffmpeg))
    candidates = run(source.search("苹果干", 5))
    assert candidates[0].metadata["unreadable"] is True
    assert "probe_error" in candidates[0].metadata


def test_get_video_info_and_preview(sample_video: Path, real_ffmpeg, tmp_path: Path) -> None:
    source = LocalFileSource(
        [sample_video],
        toolkit=toolkit_from(real_ffmpeg),
        preview_dir=tmp_path / "previews",
        preview_frame_count=4,
        preview_max_width=320,
    )
    candidate = run(source.search("苹果干", 1))[0]
    info = run(source.get_video_info(candidate.platform_video_id))
    assert info.duration == pytest.approx(30.0, abs=1.5)
    assert (info.width, info.height) == (640, 360)

    preview = run(source.get_preview(candidate.platform_video_id))
    assert len(preview.frames) == 4
    assert all(frame.image_path and Path(frame.image_path).exists() for frame in preview.frames)
    timestamps = [frame.timestamp for frame in preview.frames]
    assert timestamps == sorted(timestamps)
    assert preview.video_path == sample_video


def test_get_download_url_round_trips_unicode_paths(sample_video: Path) -> None:
    source = LocalFileSource([sample_video], toolkit=MockMediaToolkit())
    candidate = run(source.search("苹果干", 1))[0]
    url = run(source.get_download_url(candidate.platform_video_id))
    assert url.startswith("file://")
    assert local_path_from_url(url) == sample_video
    assert "素材" not in url or "%" in url or "素材" in url  # url may stay readable


def test_unknown_video_id_raises() -> None:
    source = LocalFileSource(toolkit=MockMediaToolkit())
    with pytest.raises(SourceError):
        run(source.get_video_info("local_nope"))


def test_local_downloader_copies_into_cache(sample_video: Path, tmp_path: Path) -> None:
    downloader = LocalFileDownloader()
    dest = tmp_path / "cache" / "staged.mp4"
    staged = run(downloader.download(sample_video.as_uri(), dest, metadata={"duration": 30.0}))
    assert staged == dest
    assert dest.exists() and dest.stat().st_size == sample_video.stat().st_size
    # the original file is untouched and still present
    assert sample_video.exists()
    sidecar = dest.with_suffix(".mp4.meta.json")
    assert sidecar.exists()


def test_local_downloader_rejects_missing_source(tmp_path: Path) -> None:
    from media.downloader import DownloadError

    downloader = LocalFileDownloader()
    with pytest.raises(DownloadError):
        run(downloader.download((tmp_path / "nope.mp4").as_uri(), tmp_path / "out.mp4"))


def test_local_path_from_url_accepts_plain_paths(tmp_path: Path) -> None:
    assert local_path_from_url(str(tmp_path / "a.mp4")) == tmp_path / "a.mp4"
    assert local_path_from_url((tmp_path / "a.mp4").as_uri()) == tmp_path / "a.mp4"
