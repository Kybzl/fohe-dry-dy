"""Real FFmpeg integration: probing, frames, cutting and thumbnails."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from media.ffmpeg import EncodeSettings, FFmpegToolkit, MediaInfo, pick_best_frame_index
from media.frame_sampler import FrameSampler


def run(coro):
    return asyncio.run(coro)


def toolkit_from(pair: tuple[str, str]) -> FFmpegToolkit:
    ffmpeg, ffprobe = pair
    return FFmpegToolkit(ffmpeg_bin=ffmpeg, ffprobe_bin=ffprobe, timeout=120.0)


def test_real_probe_reads_metadata(real_ffmpeg, sample_video: Path) -> None:
    info = run(toolkit_from(real_ffmpeg).probe(sample_video))
    assert info.has_video
    assert info.width == 640 and info.height == 360
    assert info.fps and info.fps > 10
    assert info.duration == pytest.approx(30.0, abs=1.5)
    assert info.codec == "h264"
    assert info.size_bytes > 0
    # the silent sample has no audio stream
    assert info.has_audio is False


def test_real_probe_detects_audio_track(real_ffmpeg, sample_video_with_audio: Path) -> None:
    info = run(toolkit_from(real_ffmpeg).probe(sample_video_with_audio))
    assert info.has_audio is True
    assert info.audio_codec == "aac"


def test_ffmpeg_stderr_fallback_parses_the_same_fields(real_ffmpeg, sample_video: Path) -> None:
    """When ffprobe is unavailable, ``ffmpeg -i`` output is parsed instead."""

    ffmpeg, _ = real_ffmpeg
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(sample_video)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    info = FFmpegToolkit.parse_ffmpeg_stderr_info(result.stderr)
    assert info.has_video
    assert (info.width, info.height) == (640, 360)
    assert info.duration == pytest.approx(30.0, abs=1.5)

    toolkit = FFmpegToolkit(ffmpeg_bin=ffmpeg, ffprobe_bin="definitely-missing-ffprobe")
    detected = run(toolkit.probe(sample_video))
    assert detected.has_video
    assert detected.duration == pytest.approx(30.0, abs=1.5)


def test_real_probe_json_shape_is_stable(real_ffmpeg, sample_video: Path) -> None:
    ffprobe = real_ffmpeg[1]
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(sample_video),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    info: MediaInfo = FFmpegToolkit.parse_probe_json(json.loads(result.stdout))
    assert info.duration == pytest.approx(30.0, abs=1.5)
    assert info.width == 640


def test_real_frame_extraction_scales_and_keeps_timestamps(
    real_ffmpeg, sample_video: Path, tmp_path: Path
) -> None:
    toolkit = toolkit_from(real_ffmpeg)
    sampler = FrameSampler(toolkit, max_frames=8, max_width=320)
    frames = run(sampler.sample(sample_video, 30.0, 8, "real", tmp_path / "frames"))
    assert len(frames) == 8
    for frame in frames:
        assert frame.image_path and Path(frame.image_path).exists()
    stamps = [frame.timestamp for frame in frames]
    assert stamps == sorted(stamps)
    assert stamps[0] < 5.0 and stamps[-1] > 25.0

    from PIL import Image

    with Image.open(frames[0].image_path) as image:
        assert image.width == 320, "preview_max_width must shrink sampled frames"


def test_real_clip_cutting_and_thumbnail(real_ffmpeg, sample_video: Path, tmp_path: Path) -> None:
    toolkit = toolkit_from(real_ffmpeg)
    clip = tmp_path / "clip.mp4"
    run(toolkit.cut_clip(sample_video, 4.8, 11.4, clip, reencode=True, has_audio=False))
    assert clip.exists() and clip.stat().st_size > 1000
    info = run(toolkit.probe(clip))
    assert info.duration == pytest.approx(6.6, abs=0.6)
    assert (info.width, info.height) == (640, 360)
    assert info.has_audio is False

    thumbnail = tmp_path / "thumb.jpg"
    run(toolkit.make_thumbnail(sample_video, 8.0, thumbnail))
    assert thumbnail.exists() and thumbnail.stat().st_size > 0


def test_stream_copy_cut_still_produces_a_file(
    real_ffmpeg, sample_video: Path, tmp_path: Path
) -> None:
    toolkit = toolkit_from(real_ffmpeg)
    clip = tmp_path / "copy.mp4"
    run(toolkit.cut_clip(sample_video, 6.0, 12.0, clip, reencode=False))
    assert clip.exists() and clip.stat().st_size > 1000
    assert run(toolkit.probe(clip)).duration > 1.0


def test_real_perceptual_hash_is_stable(real_ffmpeg, sample_video: Path, tmp_path: Path) -> None:
    toolkit = toolkit_from(real_ffmpeg)
    first = tmp_path / "f1.jpg"
    second = tmp_path / "f2.jpg"
    other = tmp_path / "f3.jpg"
    run(toolkit.extract_representative_frame(sample_video, 2.0, first))
    run(toolkit.extract_representative_frame(sample_video, 2.5, second))
    run(toolkit.extract_representative_frame(sample_video, 20.0, other))
    hash_first = toolkit.perceptual_hash(first)
    hash_second = toolkit.perceptual_hash(second)
    hash_other = toolkit.perceptual_hash(other)
    assert hash_first and len(hash_first) == 16
    assert hash_first == hash_second or hash_first != hash_other


def test_pick_best_frame_avoids_black_frames(
    real_ffmpeg, sample_video: Path, tmp_path: Path
) -> None:
    from PIL import Image

    toolkit = toolkit_from(real_ffmpeg)
    good = tmp_path / "good.jpg"
    run(toolkit.make_thumbnail(sample_video, 3.0, good))
    black = tmp_path / "black.jpg"
    Image.new("RGB", (160, 160), (0, 0, 0)).save(black)
    assert pick_best_frame_index([black, good]) == 1
    assert pick_best_frame_index([good, black]) == 0


def test_cut_args_never_resize_and_keep_codec_settings(tmp_path: Path) -> None:
    settings = EncodeSettings(video_codec="libx264", crf=18, preset="medium")
    args = FFmpegToolkit.build_cut_args(
        tmp_path / "in.mp4",
        0.0,
        5.0,
        tmp_path / "out.mp4",
        reencode=True,
        encode_settings=settings,
    )
    assert "-vf" not in args and "-s" not in args, "clips must keep the source resolution"
    assert "libx264" in args and "18" in args and "medium" in args
    assert args[-1] == str(tmp_path / "out.mp4")


def test_silent_source_cut_drops_audio_stream(tmp_path: Path) -> None:
    args = FFmpegToolkit.build_cut_args(
        tmp_path / "in.mp4", 0.0, 5.0, tmp_path / "out.mp4", reencode=True, has_audio=False
    )
    assert "-an" in args
    with_audio = FFmpegToolkit.build_cut_args(
        tmp_path / "in.mp4", 0.0, 5.0, tmp_path / "out.mp4", reencode=True, has_audio=True
    )
    assert "-an" not in with_audio and "aac" in with_audio
