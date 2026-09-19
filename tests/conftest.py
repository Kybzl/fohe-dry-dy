"""Shared fixtures: every test runs against an isolated temporary library."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from core.config import AppSettings, load_settings
from core.models import SubtitlePolicy, TaskRequest
from core.task_runner import TaskRunner


def _available_ffmpeg(settings: AppSettings) -> tuple[str, str] | None:
    """Return the configured ffmpeg/ffprobe pair when both exist."""

    from media.ffmpeg import FFmpegToolkit

    toolkit = FFmpegToolkit(
        ffmpeg_bin=settings.media.ffmpeg_path or "ffmpeg",
        ffprobe_bin=settings.media.ffprobe_path or "ffprobe",
        timeout=120.0,
    )
    if not toolkit.is_available:
        return None
    ffmpeg = FFmpegToolkit._resolve(toolkit.ffmpeg_bin)
    ffprobe = FFmpegToolkit._resolve(toolkit.ffprobe_bin)
    if ffmpeg is None or ffprobe is None:
        return None
    return ffmpeg, ffprobe


@pytest.fixture(scope="session")
def real_ffmpeg() -> tuple[str, str]:
    """Real ffmpeg/ffprobe pair, or skip the test module."""

    settings = load_settings()
    pair = _available_ffmpeg(settings)
    if pair is None:
        pytest.skip("ffmpeg/ffprobe are not available in this environment")
    return pair


def _render_scene_video(
    ffmpeg: str,
    dest: Path,
    *,
    scenes: int = 5,
    scene_seconds: float = 6.0,
    with_audio: bool = False,
) -> Path:
    """Render a multi-scene test video with unambiguous shot cuts.

    Each scene uses a different generator (moving test pattern, colour bars,
    solid colour, ...) so ``PySceneDetect`` sees a real content change at every
    boundary, exactly like a cut between shots in real footage.
    """

    generators = [
        "testsrc2=s=640x360:d=6",
        "smptebars=s=640x360:d=6",
        "testsrc=s=640x360:d=6",
        "rgbtestsrc=s=640x360:d=6",
        "color=c=orange:s=640x360:d=6",
        "gradients=s=640x360:d=6",
    ]
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for index in range(scenes):
        generator = generators[index % len(generators)]
        args += ["-f", "lavfi", "-i", generator]
    if with_audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={scenes * scene_seconds}"]

    streams = "".join(f"[{index}:v]" for index in range(scenes))
    filter_complex = f"{streams}concat=n={scenes}:v=1:a=0[v]"
    args += ["-filter_complex", filter_complex, "-map", "[v]"]
    if with_audio:
        args += ["-map", f"{scenes}:a", "-c:a", "aac", "-shortest"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(dest)]

    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if result.returncode != 0 or not dest.exists():
        pytest.skip(f"could not render a sample video: {result.stderr[-300:]}")
    return dest


@pytest.fixture(scope="session")
def sample_video(real_ffmpeg, tmp_path_factory) -> Path:
    """30 second, five scene, **silent** test video (Unicode file name)."""

    ffmpeg, _ = real_ffmpeg
    target = tmp_path_factory.mktemp("media") / "苹果烘干_测试片段.mp4"
    return _render_scene_video(ffmpeg, target, with_audio=False)


@pytest.fixture(scope="session")
def sample_video_with_audio(real_ffmpeg, tmp_path_factory) -> Path:
    """Same shape, but with an audio track."""

    ffmpeg, _ = real_ffmpeg
    target = tmp_path_factory.mktemp("media-audio") / "sample_with_audio.mp4"
    return _render_scene_video(ffmpeg, target, with_audio=True)


@pytest.fixture(autouse=True)
def quiet_logging() -> None:
    logging.getLogger().setLevel(logging.WARNING)


@pytest.fixture
def settings(tmp_path: Path) -> AppSettings:
    """Mock-mode settings whose data/cache/library live in ``tmp_path``."""

    return load_settings(
        overrides={
            "storage": {
                "library_root": str(tmp_path / "library"),
                "database_path": str(tmp_path / "data" / "library.db"),
                "cache_root": str(tmp_path / "cache"),
            },
            "paths": {
                "data_dir": str(tmp_path / "data"),
                "log_dir": str(tmp_path / "logs"),
            },
            "pipeline": {"default_subtitle_policy": "strict"},
            "subtitle_cleanup": {
                # tests never write review artifacts into the real project
                "review_pack": False,
                "reports_dir": str(tmp_path / "reports"),
            },
            "ai": {
                # unit tests must never touch a real AI provider; readiness is
                # exercised explicitly by the M9.7 tests
                "provider_readiness": {"enabled": False},
            },
        }
    )


@pytest.fixture
def runner(settings: AppSettings) -> TaskRunner:
    return TaskRunner(settings)


@pytest.fixture
def task_request(settings: AppSettings):
    def _factory(
        material: str = "苹果干",
        target: int = 5,
        *,
        min_duration: float = 3.0,
        max_duration: float = 15.0,
        policy: SubtitlePolicy = SubtitlePolicy.STRICT,
        **overrides,
    ) -> TaskRequest:
        payload = dict(
            material=material,
            target_clip_count=target,
            min_clip_duration=min_duration,
            max_clip_duration=max_duration,
            subtitle_policy=policy,
            library_root=settings.paths.library_root,
            # Milestone 1 workflow tests always run the offline stack; tests
            # that exercise real media override these explicitly.
            source="mock",
            provider="mock",
            media_backend="mock",
        )
        payload.update(overrides)
        return TaskRequest(**payload)

    return _factory


@pytest.fixture
def local_request(settings: AppSettings):
    """Build a ``TaskRequest`` for real local files."""

    def _factory(
        files,
        material: str = "苹果干",
        target: int = 5,
        *,
        min_duration: float = 3.0,
        max_duration: float = 15.0,
        provider: str = "mock",
        media_backend: str = "ffmpeg",
    ) -> TaskRequest:
        return TaskRequest(
            material=material,
            target_clip_count=target,
            min_clip_duration=min_duration,
            max_clip_duration=max_duration,
            library_root=settings.paths.library_root,
            source="local",
            local_files=[Path(item) for item in ([files] if isinstance(files, (str, Path)) else files)],
            provider=provider,
            media_backend=media_backend,
        )

    return _factory
