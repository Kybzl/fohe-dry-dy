"""Downloader, FFmpeg wrapper and frame sampler."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from media.clipper import ClipCutter
from media.downloader import DownloadError, MockDownloader
from media.ffmpeg import (
    FFmpegError,
    FFmpegNotFoundError,
    FFmpegToolkit,
    MockMediaToolkit,
)
from media.frame_sampler import FrameSampler

PROBE_PAYLOAD = {
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1080,
            "height": 1920,
            "avg_frame_rate": "30000/1001",
        }
    ],
    "format": {"duration": "42.512", "size": "1048576"},
}


def test_cut_args_use_stream_copy_by_default(tmp_path: Path) -> None:
    args = FFmpegToolkit.build_cut_args(
        tmp_path / "in.mp4", 5.2, 11.8, tmp_path / "out.mp4"
    )
    assert args[:4] == ["-ss", "5.200", "-i", str(tmp_path / "in.mp4")]
    assert args[4:6] == ["-t", "6.600"]
    assert "-c" in args and "copy" in args
    assert args[-1] == str(tmp_path / "out.mp4")


def test_cut_args_support_frame_accurate_reencode(tmp_path: Path) -> None:
    args = FFmpegToolkit.build_cut_args(
        tmp_path / "in.mp4", 0.0, 4.0, tmp_path / "out.mp4", reencode=True
    )
    assert "libx264" in args
    assert "copy" not in args
    assert "+faststart" in args


def test_frame_args_include_scale_filter_when_sized(tmp_path: Path) -> None:
    args = FFmpegToolkit.build_frame_args(
        tmp_path / "in.mp4", 3.0, tmp_path / "f.jpg", size=320
    )
    assert "-frames:v" in args
    assert "scale=320:-2" in args
    assert args[-1] == str(tmp_path / "f.jpg")


def test_parse_probe_json_reads_duration_and_fps() -> None:
    info = FFmpegToolkit.parse_probe_json(json.dumps(PROBE_PAYLOAD))
    assert info.duration == 42.512
    assert (info.width, info.height) == (1080, 1920)
    assert info.fps == 29.97
    assert info.codec == "h264"
    assert info.size_bytes == 1048576
    assert info.has_video is True


def test_parse_probe_json_tolerates_missing_streams() -> None:
    info = FFmpegToolkit.parse_probe_json({"format": {}})
    assert info.duration == 0.0
    assert info.width is None
    assert info.has_video is False


def test_parse_frame_rate_handles_fractions_and_garbage() -> None:
    assert FFmpegToolkit.parse_frame_rate("25/1") == 25.0
    assert FFmpegToolkit.parse_frame_rate("30") == 30.0
    assert FFmpegToolkit.parse_frame_rate(None) is None
    assert FFmpegToolkit.parse_frame_rate("0/0") is None
    assert FFmpegToolkit.parse_frame_rate("abc") is None


def test_missing_ffmpeg_binary_raises_clear_error(tmp_path: Path) -> None:
    toolkit = FFmpegToolkit(ffmpeg_bin="definitely-not-ffmpeg", ffprobe_bin="nope-ffprobe")
    assert toolkit.is_available is False
    existing = tmp_path / "in.mp4"
    existing.write_bytes(b"not really a video")
    with pytest.raises(FFmpegNotFoundError):
        asyncio.run(toolkit.probe(existing))


def test_probe_rejects_missing_files(tmp_path: Path) -> None:
    toolkit = FFmpegToolkit(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
    with pytest.raises(FFmpegError):
        asyncio.run(toolkit.probe(tmp_path / "does-not-exist.mp4"))


def test_mock_downloader_writes_file_and_sidecar(tmp_path: Path) -> None:
    downloader = MockDownloader()
    dest = tmp_path / "cache" / "video.mp4"
    written = asyncio.run(
        downloader.download("https://mock.test/v/1", dest, metadata={"duration": 33.5})
    )
    assert written == dest
    assert dest.exists() and dest.stat().st_size > 0
    sidecar = dest.with_suffix(".mp4.meta.json")
    assert json.loads(sidecar.read_text(encoding="utf-8"))["duration"] == 33.5
    assert downloader.downloaded == [dest]


def test_mock_downloader_can_simulate_failures(tmp_path: Path) -> None:
    downloader = MockDownloader(fail_on=("broken",))
    with pytest.raises(DownloadError):
        asyncio.run(downloader.download("https://mock.test/broken", tmp_path / "x.mp4"))


def test_mock_toolkit_reports_cut_duration_and_hashes(tmp_path: Path) -> None:
    toolkit = MockMediaToolkit()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    dest = tmp_path / "clip.mp4"
    asyncio.run(toolkit.cut_clip(source, 2.0, 9.5, dest, content_key="ck1"))
    info = asyncio.run(toolkit.probe(dest))
    assert info.duration == 7.5
    assert info.width == 1080 and info.height == 1920
    thumb = tmp_path / "clip.jpg"
    asyncio.run(toolkit.make_thumbnail(dest, 1.0, thumb))
    assert thumb.exists()
    hash_a = toolkit.perceptual_hash(thumb, signature=b"ck1")
    hash_b = toolkit.perceptual_hash(thumb, signature=b"ck2")
    assert hash_a != hash_b
    assert len(hash_a) == 16
    assert len(toolkit.sha256_file(dest)) == 64


def test_frame_sampler_timestamps_are_evenly_spaced() -> None:
    stamps = FrameSampler.timestamps(10.0, 4)
    assert stamps == [1.25, 3.75, 6.25, 8.75]
    windowed = FrameSampler.timestamps(100.0, 2, start=10.0, end=20.0)
    assert windowed == [12.5, 17.5]
    assert FrameSampler.timestamps(0.0, 4) == []
    assert FrameSampler.timestamps(10.0, 0) == []


def test_frame_sampler_extracts_placeholder_frames(tmp_path: Path) -> None:
    toolkit = MockMediaToolkit()
    sampler = FrameSampler(toolkit, max_frames=4)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    frames = asyncio.run(
        sampler.sample(video, 20.0, 8, "vid", tmp_path / "frames")
    )
    assert len(frames) == 4
    assert all(frame.image_path and Path(frame.image_path).exists() for frame in frames)
    assert [frame.timestamp for frame in frames] == sorted(f.timestamp for f in frames)


def test_clip_cutter_produces_artifact_with_content_hash(tmp_path: Path) -> None:
    toolkit = MockMediaToolkit()
    cutter = ClipCutter(toolkit)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    from core.models import SegmentTiming

    timing = SegmentTiming(start=4.0, end=12.0)
    key = cutter.compute_content_key("苹果干", "苹果片铺盘", timing.duration)
    artifact = asyncio.run(
        cutter.cut(
            source_video=source,
            timing=timing,
            dest=tmp_path / "clip.mp4",
            thumbnail_dest=tmp_path / "clip.jpg",
            content_key=key,
        )
    )
    assert artifact.duration == 8.0
    assert artifact.file_path.exists()
    assert artifact.thumbnail_path.exists()
    assert artifact.sha256 and len(artifact.sha256) == 64
    assert artifact.phash and len(artifact.phash) == 16
    same = cutter.compute_content_key("苹果干", "苹果片铺盘", 8.0)
    other = cutter.compute_content_key("苹果干", "苹果片铺盘", 9.0)
    assert key == same and key != other
