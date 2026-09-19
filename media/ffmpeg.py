"""FFmpeg / ffprobe wrapper (real implementation) and its mock twin.

All shell interaction is centralised here: probing, frame extraction, clip
cutting, thumbnail generation and perceptual hashing.  Commands are always
passed as argument arrays to ``asyncio.create_subprocess_exec`` -- no shell
string is ever built.  MoviePy is deliberately not used.

The pure helpers (``build_*_args``, ``parse_probe_json``,
``parse_ffmpeg_stderr_info``) are unit tested without needing a real binary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

from media.placeholder import write_placeholder_jpeg, write_placeholder_mp4

LOGGER = logging.getLogger(__name__)


class FFmpegError(RuntimeError):
    """ffmpeg / ffprobe exited non zero, timed out or produced no output."""


class FFmpegNotFoundError(FFmpegError):
    """Neither the configured path nor PATH provides the required binary."""


class RemoteMediaError(FFmpegError):
    """A remote media URL was missing, malformed or used a refused scheme."""


#: Remote media may only be fetched over the network (never file://, never a
#: shell).  See section 18 of the Milestone 3 specification.
ALLOWED_REMOTE_SCHEMES: tuple[str, ...] = ("http", "https")

DEFAULT_REMOTE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def validate_remote_media_url(
    url: str,
    *,
    allowed_schemes: Sequence[str] = ALLOWED_REMOTE_SCHEMES,
) -> str:
    """Validate and return a remote media URL.

    Remote media URLs are untrusted input: only ``http``/``https`` are accepted,
    the host must be present, and control characters (which could smuggle
    arguments into a command line) are refused outright.
    """

    text = str(url or "").strip()
    if not text:
        raise RemoteMediaError("empty remote media url")
    if any(character in text for character in ("\n", "\r", "\x00", "\t")):
        raise RemoteMediaError("remote media url contains control characters")
    parsed = urlparse(text)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {item.lower() for item in allowed_schemes}:
        raise RemoteMediaError(f"refused media url scheme: {scheme or '(none)'!r}")
    if not parsed.netloc:
        raise RemoteMediaError("remote media url has no host")
    return text


def close_transport(process: Any) -> None:
    """Close an asyncio subprocess transport (avoids shutdown warnings)."""

    transport = getattr(process, "_transport", None)
    if transport is None:
        return
    try:
        if not transport.is_closing():
            transport.close()
    except Exception:  # pragma: no cover - best effort cleanup
        return


@dataclass(frozen=True)
class EncodeSettings:
    """Encoder options used when a clip is cut with re-encoding."""

    video_codec: str = "libx264"
    crf: int = 18
    preset: str = "medium"
    audio_codec: str = "aac"
    audio_bitrate: str = "128k"

    def video_args(self) -> list[str]:
        return [
            "-c:v",
            self.video_codec,
            "-preset",
            self.preset,
            "-crf",
            str(int(self.crf)),
            "-pix_fmt",
            "yuv420p",
        ]

    def audio_args(self, *, has_audio: bool) -> list[str]:
        if not has_audio:
            # A silent source must still produce a usable clip.
            return ["-an"]
        return ["-c:a", self.audio_codec, "-b:a", self.audio_bitrate]

    @classmethod
    def from_mapping(cls, payload: dict | None) -> "EncodeSettings":
        payload = payload or {}
        return cls(
            video_codec=str(payload.get("video_codec", cls.video_codec)),
            crf=int(payload.get("crf", cls.crf)),
            preset=str(payload.get("preset", cls.preset)),
            audio_codec=str(payload.get("audio_codec", cls.audio_codec)),
            audio_bitrate=str(payload.get("audio_bitrate", cls.audio_bitrate)),
        )


@dataclass(frozen=True)
class MediaInfo:
    """Result of ``ffprobe`` (or of the mock metadata sidecar)."""

    duration: float = 0.0
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    codec: str | None = None
    audio_codec: str | None = None
    has_audio: bool = False
    bit_rate: int | None = None
    size_bytes: int = 0
    #: which deterministic signal produced ``duration`` (Milestone 9.6)
    duration_source: str = ""

    @property
    def has_video(self) -> bool:
        return bool(self.width and self.height)

    @property
    def is_vertical(self) -> bool:
        return bool(self.width and self.height and self.height > self.width)


def discover_binary(configured: str, default_name: str) -> str:
    """Resolve an executable: explicit config path first, then PATH."""

    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return str(candidate)
        LOGGER.warning("configured binary %s does not exist; falling back to PATH", configured)
    return shutil.which(default_name) or default_name


class MediaToolkit(ABC):
    """Everything the pipeline needs from a media backend."""

    @property
    @abstractmethod
    def is_available(self) -> bool:
        """True when the backend can actually be used."""

    @abstractmethod
    async def probe(self, path: Path) -> MediaInfo:
        """Return duration / resolution / fps / audio presence."""

    @abstractmethod
    async def extract_frames(
        self,
        video: Path,
        timestamps: Sequence[float],
        out_dir: Path,
        prefix: str,
        *,
        size: int | None = None,
    ) -> list[Path]:
        """Write one still per timestamp and return the paths, in order."""

    @abstractmethod
    async def cut_clip(
        self,
        source: Path,
        start: float,
        end: float,
        dest: Path,
        *,
        reencode: bool = False,
        content_key: str | None = None,
        encode_settings: EncodeSettings | None = None,
        has_audio: bool | None = None,
    ) -> Path:
        """Cut ``[start, end)`` out of ``source`` into ``dest``."""

    @abstractmethod
    async def make_thumbnail(self, source: Path, timestamp: float, dest: Path) -> Path:
        """Write a single thumbnail frame."""

    @abstractmethod
    async def extract_representative_frame(
        self,
        video: Path,
        timestamp: float,
        dest: Path,
    ) -> Path:
        """Extract the frame used for the perceptual hash."""

    @abstractmethod
    def perceptual_hash(self, image_path: Path, *, signature: bytes | None = None) -> str | None:
        """Return a 16 character hex perceptual hash of an image."""

    @abstractmethod
    async def probe_remote(self, url: str) -> MediaInfo:
        """Probe a remote HTTP(S) media URL without downloading it fully."""

    @abstractmethod
    async def sample_remote_frames(
        self,
        url: str,
        timestamps: Sequence[float],
        out_dir: Path,
        prefix: str,
        *,
        size: int | None = None,
    ) -> list[Path]:
        """Sample frames straight from a remote media URL.

        Only the ranges FFmpeg needs are read; the whole video is never saved.
        """

    async def measure_bounded_duration(
        self, path: Path, *, max_seconds: float, timeout: float | None = None
    ) -> tuple[float | None, str]:
        """Bounded decode fallback when normal duration fields are absent."""

        return None, "unsupported"

    @staticmethod
    def sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()


# ---------------------------------------------------------------------------
# Thumbnail frame selection
# ---------------------------------------------------------------------------
_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_BITRATE = re.compile(r"bitrate:\s*(\d+)\s*kb/s")
_VIDEO_LINE = re.compile(
    r"Stream #\d+:\d+.*?: Video: ([A-Za-z0-9_\-]+).*?, (\d{2,5})x(\d{2,5})"
)
_FPS = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
_AUDIO_LINE = re.compile(r"Stream #\d+:\d+.*?: Audio: ([A-Za-z0-9_\-]+)")


def frame_quality_score(image_path: Path) -> float | None:
    """Heuristic score for choosing a thumbnail frame.

    Rejects black / blown out frames (a common side effect of transitions) and
    prefers sharp frames: the score combines edge energy with a brightness
    sanity check.  Returns ``None`` when Pillow is unavailable.
    """

    try:
        from PIL import Image, ImageFilter, ImageStat

        with Image.open(image_path) as image:
            gray = image.convert("L").resize((160, 160))
            mean = ImageStat.Stat(gray).mean[0]
            if mean < 12.0 or mean > 245.0:
                return 0.0
            edges = gray.filter(ImageFilter.FIND_EDGES)
            sharpness = ImageStat.Stat(edges).stddev[0]
            brightness_weight = 1.0 - (abs(mean - 128.0) / 128.0)
            return float(sharpness * (0.5 + brightness_weight))
    except Exception as exc:  # pragma: no cover - Pillow is optional
        LOGGER.debug("frame scoring unavailable for %s: %s", image_path, exc)
        return None


def pick_best_frame_index(candidates: Sequence[Path]) -> int:
    """Index of the most usable candidate frame (middle frame as fallback)."""

    if not candidates:
        return 0
    best_index = len(candidates) // 2
    best_score = -1.0
    for index, path in enumerate(candidates):
        score = frame_quality_score(path)
        if score is None:  # pragma: no cover - Pillow missing
            return len(candidates) // 2
        if score > best_score:
            best_score = score
            best_index = index
    return best_index


# ---------------------------------------------------------------------------
# Real backend
# ---------------------------------------------------------------------------
class FFmpegToolkit(MediaToolkit):
    """Backend built on the ``ffmpeg`` and ``ffprobe`` command line tools."""

    def __init__(
        self,
        *,
        ffmpeg_bin: str = "ffmpeg",
        ffprobe_bin: str = "ffprobe",
        timeout: float = 300.0,
        encode_settings: EncodeSettings | None = None,
        precise_cut: bool = True,
        remote_user_agent: str = DEFAULT_REMOTE_USER_AGENT,
        allowed_remote_schemes: Sequence[str] = ALLOWED_REMOTE_SCHEMES,
    ) -> None:
        # An explicit path in config wins; otherwise PATH discovery is used.
        self.ffmpeg_bin = discover_binary(ffmpeg_bin, "ffmpeg")
        self.ffprobe_bin = discover_binary(ffprobe_bin, "ffprobe")
        self.timeout = timeout
        self.encode_settings = encode_settings or EncodeSettings()
        self.precise_cut = precise_cut
        self.remote_user_agent = remote_user_agent or DEFAULT_REMOTE_USER_AGENT
        self.allowed_remote_schemes = tuple(allowed_remote_schemes)

    @property
    def is_available(self) -> bool:
        return bool(
            self._resolve(self.ffmpeg_bin) is not None
            and self._resolve(self.ffprobe_bin) is not None
        )

    @property
    def ffmpeg_available(self) -> bool:
        return self._resolve(self.ffmpeg_bin) is not None

    @staticmethod
    def _resolve(binary: str) -> str | None:
        if not binary:
            return None
        candidate = Path(binary)
        if candidate.is_file():
            return str(candidate)
        return shutil.which(binary)

    def describe(self) -> str:
        """Human readable backend summary used by ``app.py --check``."""

        ffmpeg = self._resolve(self.ffmpeg_bin) or f"{self.ffmpeg_bin} (missing)"
        ffprobe = self._resolve(self.ffprobe_bin) or f"{self.ffprobe_bin} (missing)"
        return f"ffmpeg={ffmpeg} ffprobe={ffprobe}"

    # -- pure helpers (unit tested) ----------------------------------------
    @staticmethod
    def build_probe_args(path: Path) -> list[str]:
        return [
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]

    @staticmethod
    def build_info_args(path: Path) -> list[str]:
        """``ffmpeg -i file`` is used when ffprobe is unavailable."""

        return ["-hide_banner", "-i", str(path)]

    def _remote_options(self) -> list[str]:
        """Network input options used for every remote read.

        ``-rw_timeout`` bounds a stalled transfer in microseconds and
        ``-user_agent`` is required by most CDNs (including Douyin's).
        """

        return [
            "-rw_timeout",
            str(int(max(1.0, self.timeout) * 1_000_000)),
            "-user_agent",
            self.remote_user_agent,
        ]

    @staticmethod
    def build_remote_probe_args(
        url: str,
        *,
        remote_options: Sequence[str] = (),
    ) -> list[str]:
        return [
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            *remote_options,
            url,
        ]

    @classmethod
    def build_remote_frame_args(
        cls,
        url: str,
        timestamp: float,
        dest: Path,
        *,
        size: int | None = None,
        remote_options: Sequence[str] = (),
    ) -> list[str]:
        args = [
            "-ss",
            f"{max(0.0, timestamp):.3f}",
            *remote_options,
            "-i",
            url,
            "-frames:v",
            "1",
        ]
        if size:
            args += ["-vf", f"scale={int(size)}:-2"]
        args += ["-q:v", "2", "-y", str(dest)]
        return args

    @staticmethod
    def build_frame_args(
        video: Path,
        timestamp: float,
        dest: Path,
        *,
        size: int | None = None,
    ) -> list[str]:
        args = ["-ss", f"{max(0.0, timestamp):.3f}", "-i", str(video), "-frames:v", "1"]
        if size:
            args += ["-vf", f"scale={int(size)}:-2"]
        args += ["-q:v", "2", "-y", str(dest)]
        return args

    @staticmethod
    def build_bounded_duration_args(path: Path, max_seconds: float) -> list[str]:
        return [
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostats",
            "-progress",
            "pipe:1",
            "-i",
            str(path),
            "-t",
            f"{max(0.5, float(max_seconds)):.3f}",
            "-f",
            "null",
            "-",
        ]

    async def measure_bounded_duration(
        self, path: Path, *, max_seconds: float, timeout: float | None = None
    ) -> tuple[float | None, str]:
        """Decode at most ``max_seconds`` and derive the observed timestamp."""

        binary = self._resolve(self.ffmpeg_bin)
        if binary is None:
            raise FFmpegNotFoundError(
                f"{self.ffmpeg_bin!r} was not found; cannot run the bounded decode fallback"
            )
        process = await asyncio.create_subprocess_exec(
            binary,
            *self.build_bounded_duration_args(path, max_seconds),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        observed = 0.0
        try:
            stdout, _stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout or self.timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return None, "bounded_decode_timeout"
        finally:
            close_transport(process)
        for line in (stdout or b"").decode("utf-8", errors="replace").splitlines():
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() not in ("out_time_us", "out_time_ms"):
                continue
            try:
                candidate = float(value.strip()) / 1_000_000.0
            except ValueError:
                continue
            observed = max(observed, candidate)
        if process.returncode != 0:
            return None, "bounded_decode_failed"
        if observed <= 0:
            return None, "bounded_decode_no_timestamps"
        if observed >= max_seconds - 0.25:
            return float(max_seconds), "bounded_decode_bound_reached"
        return round(observed, 3), "bounded_decode"

    @staticmethod
    def build_cut_args(
        source: Path,
        start: float,
        end: float,
        dest: Path,
        *,
        reencode: bool = False,
        encode_settings: EncodeSettings | None = None,
        has_audio: bool | None = None,
    ) -> list[str]:
        """Build the ffmpeg argument array for one clip.

        Stream copy keeps the original quality but snaps to keyframes;
        re-encoding with the configured codec gives frame accurate cuts.
        """

        settings = encode_settings or EncodeSettings()
        duration = max(0.05, end - start)
        args = ["-ss", f"{max(0.0, start):.3f}", "-i", str(source), "-t", f"{duration:.3f}"]
        if reencode:
            args += settings.video_args()
            args += settings.audio_args(has_audio=bool(has_audio))
            args += ["-movflags", "+faststart"]
        else:
            args += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        args += ["-y", str(dest)]
        return args

    @staticmethod
    def parse_frame_rate(value: str | None) -> float | None:
        """``"30000/1001"`` -> ``29.97``."""

        if not value:
            return None
        if "/" in value:
            numerator, _, denominator = value.partition("/")
            try:
                den = float(denominator)
                return round(float(numerator) / den, 3) if den else None
            except ValueError:
                return None
        try:
            return float(value)
        except ValueError:
            return None

    @staticmethod
    def parse_probe_json(payload: str | dict) -> MediaInfo:
        """Parse ``ffprobe -print_format json`` output."""

        data = json.loads(payload) if isinstance(payload, str) else payload
        fmt = data.get("format") or {}
        streams = data.get("streams") or []
        video_stream = next(
            (stream for stream in streams if stream.get("codec_type") == "video"), {}
        )
        audio_stream = next(
            (stream for stream in streams if stream.get("codec_type") == "audio"), {}
        )
        duration = 0.0
        duration_source = ""
        for source_name, raw in (
            ("format", fmt.get("duration")),
            ("video_stream", video_stream.get("duration")),
            ("audio_stream", audio_stream.get("duration")),
        ):
            try:
                value = float(raw or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                duration = value
                duration_source = source_name
                break
        if duration <= 0:
            try:
                frames = int(video_stream.get("nb_frames") or 0)
                fps = FFmpegToolkit.parse_frame_rate(
                    video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
                )
                if frames > 0 and fps and 1.0 <= fps <= 240.0 and frames <= 10_000_000:
                    duration = frames / fps
                    duration_source = "frame_derived"
            except (TypeError, ValueError):
                pass
        try:
            size_bytes = int(float(fmt.get("size") or 0))
        except (TypeError, ValueError):
            size_bytes = 0
        try:
            bit_rate = int(float(fmt["bit_rate"])) if fmt.get("bit_rate") else None
        except (TypeError, ValueError):
            bit_rate = None
        return MediaInfo(
            duration=round(duration, 3),
            width=int(video_stream["width"]) if video_stream.get("width") else None,
            height=int(video_stream["height"]) if video_stream.get("height") else None,
            fps=FFmpegToolkit.parse_frame_rate(
                video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
            ),
            codec=video_stream.get("codec_name"),
            audio_codec=audio_stream.get("codec_name"),
            has_audio=bool(audio_stream),
            bit_rate=bit_rate,
            size_bytes=size_bytes,
            duration_source=duration_source,
        )

    @staticmethod
    def parse_ffmpeg_stderr_info(text: str) -> MediaInfo:
        """Fallback probe: parse the ``ffmpeg -i`` banner when ffprobe is absent."""

        duration = 0.0
        match = _DURATION.search(text)
        if match:
            hours, minutes, seconds = match.groups()
            duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

        bit_rate = None
        bitrate_match = _BITRATE.search(text)
        if bitrate_match:
            bit_rate = int(bitrate_match.group(1)) * 1000

        width = height = None
        fps = None
        codec = None
        video_match = _VIDEO_LINE.search(text)
        if video_match:
            codec = video_match.group(1)
            width = int(video_match.group(2))
            height = int(video_match.group(3))
            fps_match = _FPS.search(text[video_match.start() : video_match.start() + 400])
            if fps_match:
                fps = round(float(fps_match.group(1)), 3)

        audio_match = _AUDIO_LINE.search(text)
        return MediaInfo(
            duration=round(duration, 3),
            width=width,
            height=height,
            fps=fps,
            codec=codec,
            audio_codec=audio_match.group(1) if audio_match else None,
            has_audio=bool(audio_match),
            bit_rate=bit_rate,
            duration_source="ffmpeg_banner" if duration > 0 else "",
        )

    # -- async operations --------------------------------------------------
    async def probe(self, path: Path) -> MediaInfo:
        """Real metadata; falls back to parsing ``ffmpeg -i`` when needed."""

        if not path.exists():
            raise FFmpegError(f"media file does not exist: {path}")
        try:
            stdout, _ = await self._run(self.ffprobe_bin, self.build_probe_args(path))
            return self.parse_probe_json(stdout)
        except FFmpegNotFoundError:
            LOGGER.warning("ffprobe unavailable; probing %s via ffmpeg instead", path.name)
            _, stderr = await self._run_ffmpeg_info(path)
            info = self.parse_ffmpeg_stderr_info(stderr)
            if not info.has_video:
                raise FFmpegError(f"could not read media information from {path}")
            return MediaInfo(**{**info.__dict__, "size_bytes": path.stat().st_size})

    async def _run_ffmpeg_info(self, path: Path) -> tuple[str, str]:
        """Run ``ffmpeg -i`` and return its streams (exit code 1 is expected)."""

        binary = self._resolve(self.ffmpeg_bin)
        if binary is None:
            raise FFmpegNotFoundError(
                f"{self.ffmpeg_bin!r} was not found. Install FFmpeg, set "
                "media.ffmpeg_path in config.yaml, or use media.backend=mock."
            )
        process = await asyncio.create_subprocess_exec(
            binary,
            *self.build_info_args(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await process.communicate()
        finally:
            close_transport(process)
        return (
            (stdout or b"").decode("utf-8", errors="replace"),
            (stderr or b"").decode("utf-8", errors="replace"),
        )

    async def extract_frames(
        self,
        video: Path,
        timestamps: Sequence[float],
        out_dir: Path,
        prefix: str,
        *,
        size: int | None = None,
    ) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for index, timestamp in enumerate(timestamps):
            dest = out_dir / f"{prefix}_{index:03d}.jpg"
            await self._run(
                self.ffmpeg_bin, self.build_frame_args(video, timestamp, dest, size=size)
            )
            if dest.exists():
                written.append(dest)
            else:  # pragma: no cover - defensive
                LOGGER.warning("frame extraction produced no file for t=%s", timestamp)
        return written

    async def cut_clip(
        self,
        source: Path,
        start: float,
        end: float,
        dest: Path,
        *,
        reencode: bool = False,
        content_key: str | None = None,
        encode_settings: EncodeSettings | None = None,
        has_audio: bool | None = None,
    ) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        await self._run(
            self.ffmpeg_bin,
            self.build_cut_args(
                source,
                start,
                end,
                dest,
                reencode=reencode,
                encode_settings=encode_settings or self.encode_settings,
                has_audio=has_audio,
            ),
        )
        if not dest.exists() or dest.stat().st_size == 0:
            raise FFmpegError(f"clip cutting produced no output: {dest}")
        return dest

    async def make_thumbnail(self, source: Path, timestamp: float, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        await self._run(self.ffmpeg_bin, self.build_frame_args(source, timestamp, dest))
        if not dest.exists():
            raise FFmpegError(f"thumbnail generation failed: {dest}")
        return dest

    async def extract_representative_frame(
        self,
        video: Path,
        timestamp: float,
        dest: Path,
    ) -> Path:
        """Frame used for the perceptual hash (same extraction, explicit name)."""

        return await self.make_thumbnail(video, timestamp, dest)

    async def probe_remote(self, url: str) -> MediaInfo:
        """Probe a remote stream with ffprobe (falling back to ``ffmpeg -i``)."""

        remote = validate_remote_media_url(url, allowed_schemes=self.allowed_remote_schemes)
        options = self._remote_options()
        try:
            stdout, _ = await self._run(
                self.ffprobe_bin,
                self.build_remote_probe_args(remote, remote_options=options),
            )
            info = self.parse_probe_json(stdout)
            if not info.has_video:
                raise RemoteMediaError(f"remote media has no video stream: {remote[:80]}")
            return info
        except FFmpegNotFoundError:
            binary = self._resolve(self.ffmpeg_bin)
            if binary is None:
                raise
            process = await asyncio.create_subprocess_exec(
                binary,
                "-hide_banner",
                *options,
                "-i",
                remote,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
            close_transport(process)
            info = self.parse_ffmpeg_stderr_info((stderr or b"").decode("utf-8", "replace"))
            if not info.has_video:
                raise RemoteMediaError(f"could not read remote media metadata: {remote[:80]}")
            return info

    async def sample_remote_frames(
        self,
        url: str,
        timestamps: Sequence[float],
        out_dir: Path,
        prefix: str,
        *,
        size: int | None = None,
    ) -> list[Path]:
        """Sample frames directly from a remote URL (no full download)."""

        remote = validate_remote_media_url(url, allowed_schemes=self.allowed_remote_schemes)
        out_dir.mkdir(parents=True, exist_ok=True)
        options = self._remote_options()
        written: list[Path] = []
        for index, timestamp in enumerate(timestamps):
            dest = out_dir / f"{prefix}_{index:03d}.jpg"
            try:
                await self._run(
                    self.ffmpeg_bin,
                    self.build_remote_frame_args(
                        remote, timestamp, dest, size=size, remote_options=options
                    ),
                )
            except FFmpegError as exc:
                # A frame that cannot be read is not fatal: the sampler reports
                # what it got and the preview filter decides on the rest.
                LOGGER.warning("remote frame at %.2fs failed: %s", timestamp, exc)
                continue
            if dest.exists():
                written.append(dest)
        return written

    def perceptual_hash(self, image_path: Path, *, signature: bytes | None = None) -> str | None:
        """Real pHash via ``imagehash``; content hash fallback when unavailable."""

        try:
            import imagehash
            from PIL import Image

            with Image.open(image_path) as image:
                return str(imagehash.phash(image.convert("RGB")))
        except Exception as exc:
            LOGGER.debug("phash unavailable for %s (%s); using content hash", image_path, exc)
            if signature is None:
                signature = image_path.read_bytes()[:256] if image_path.exists() else b""
            return hashlib.sha256(signature).hexdigest()[:16]

    async def _run(self, binary: str, args: Sequence[str]) -> tuple[str, str]:
        resolved = self._resolve(binary)
        if resolved is None:
            raise FFmpegNotFoundError(
                f"{binary!r} was not found on PATH. Install FFmpeg, set "
                "media.ffmpeg_path / media.ffprobe_path in config.yaml, or use "
                "media.backend=mock."
            )
        command = [resolved, *args]
        LOGGER.debug("running: %s", " ".join(command))
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:  # pragma: no cover - race with PATH
            raise FFmpegNotFoundError(str(exc)) from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise FFmpegError(f"{binary} timed out after {self.timeout}s") from exc
        finally:
            # Windows' proactor loop warns when a subprocess transport is still
            # open at interpreter shutdown; close it explicitly.
            close_transport(process)
        if process.returncode != 0:
            tail = (stderr or b"").decode("utf-8", errors="replace")[-400:]
            raise FFmpegError(f"{binary} exited with {process.returncode}: {tail}")
        return (stdout or b"").decode("utf-8", errors="replace"), (
            stderr or b""
        ).decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Mock backend (offline tests, demo mode)
# ---------------------------------------------------------------------------
def clip_signature(
    source: Path,
    start: float,
    end: float,
    *,
    content_key: str | None = None,
) -> bytes:
    """Deterministic content signature of a cut range.

    Identical ranges of the same source produce identical signatures.  When a
    semantic ``content_key`` is supplied the range is ignored, so the *same*
    picture content found in two different source videos hashes identically -
    exactly the case SHA256 / pHash deduplication is meant to catch.
    """

    if content_key:
        return f"content|{content_key}".encode("utf-8")
    return f"{source.name}|{start:.2f}|{end:.2f}".encode("utf-8")


class MockMediaToolkit(MediaToolkit):
    """Backend that writes placeholder files; needs no ffmpeg installation.

    Durations come from the ``.meta.json`` sidecar written by
    ``MockDownloader`` (falling back to a deterministic value derived from the
    file path), so clip timings stay consistent with the mock source.
    """

    def __init__(self, *, default_duration: float = 45.0) -> None:
        self.default_duration = default_duration
        self.calls: list[tuple[str, tuple]] = []
        # Cut clips remember their own metadata, exactly like the real backend
        # would report it back from ffprobe.
        self._produced: dict[str, MediaInfo] = {}

    @property
    def is_available(self) -> bool:
        return True

    def describe(self) -> str:
        return "mock media toolkit (placeholder files, no ffmpeg required)"

    async def measure_bounded_duration(
        self, path: Path, *, max_seconds: float, timeout: float | None = None
    ) -> tuple[float | None, str]:
        info = await self.probe(path)
        if info.duration and info.duration < max_seconds - 0.25:
            return float(info.duration), "bounded_decode"
        return float(max(0.5, max_seconds)), "bounded_decode_bound_reached"

    async def probe(self, path: Path) -> MediaInfo:
        self.calls.append(("probe", (path,)))
        produced = self._produced.get(str(path))
        if produced is not None:
            size = path.stat().st_size if path.exists() else produced.size_bytes
            return MediaInfo(**{**produced.__dict__, "size_bytes": size})
        sidecar = path.with_suffix(path.suffix + ".meta.json")
        payload: dict = {}
        if sidecar.exists():
            try:
                payload = json.loads(sidecar.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
        rng = int(hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:8], 16)
        duration = payload.get("duration")
        if not duration:
            duration = round(self.default_duration + (rng % 300) / 10.0, 1)
        return MediaInfo(
            duration=float(duration),
            width=int(payload.get("width") or 1080),
            height=int(payload.get("height") or 1920),
            fps=float(payload.get("fps") or 30.0),
            codec="mock",
            audio_codec="mock",
            has_audio=True,
            size_bytes=path.stat().st_size if path.exists() else 0,
            duration_source="mock",
        )

    async def extract_frames(
        self,
        video: Path,
        timestamps: Sequence[float],
        out_dir: Path,
        prefix: str,
        *,
        size: int | None = None,
    ) -> list[Path]:
        self.calls.append(("extract_frames", (video, tuple(timestamps))))
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for index, timestamp in enumerate(timestamps):
            dest = out_dir / f"{prefix}_{index:03d}.jpg"
            write_placeholder_jpeg(dest, f"{prefix}|{index}|{timestamp:.2f}".encode("utf-8"))
            written.append(dest)
        return written

    async def cut_clip(
        self,
        source: Path,
        start: float,
        end: float,
        dest: Path,
        *,
        reencode: bool = False,
        content_key: str | None = None,
        encode_settings: EncodeSettings | None = None,
        has_audio: bool | None = None,
    ) -> Path:
        self.calls.append(("cut_clip", (source, start, end, dest)))
        signature = clip_signature(source, start, end, content_key=content_key)
        write_placeholder_mp4(dest, signature=signature)
        self._produced[str(dest)] = MediaInfo(
            duration=round(max(0.05, end - start), 3),
            width=1080,
            height=1920,
            fps=30.0,
            codec="mock",
            audio_codec="mock",
            has_audio=True,
            size_bytes=dest.stat().st_size,
            duration_source="mock",
        )
        return dest

    async def make_thumbnail(self, source: Path, timestamp: float, dest: Path) -> Path:
        self.calls.append(("make_thumbnail", (source, timestamp, dest)))
        write_placeholder_jpeg(dest, f"{source.name}|{timestamp:.2f}".encode("utf-8"))
        return dest

    async def extract_representative_frame(
        self,
        video: Path,
        timestamp: float,
        dest: Path,
    ) -> Path:
        return await self.make_thumbnail(video, timestamp, dest)

    async def probe_remote(self, url: str) -> MediaInfo:
        """Offline stand-in: deterministic metadata derived from the URL."""

        remote = validate_remote_media_url(url)
        self.calls.append(("probe_remote", (remote,)))
        rng = int(hashlib.sha1(remote.encode("utf-8")).hexdigest()[:8], 16)
        return MediaInfo(
            duration=round(self.default_duration + (rng % 300) / 10.0, 1),
            width=1080,
            height=1920,
            fps=30.0,
            codec="mock",
            audio_codec="mock",
            has_audio=True,
            duration_source="mock",
        )

    async def sample_remote_frames(
        self,
        url: str,
        timestamps: Sequence[float],
        out_dir: Path,
        prefix: str,
        *,
        size: int | None = None,
    ) -> list[Path]:
        remote = validate_remote_media_url(url)
        self.calls.append(("sample_remote_frames", (remote, tuple(timestamps))))
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for index, timestamp in enumerate(timestamps):
            dest = out_dir / f"{prefix}_{index:03d}.jpg"
            write_placeholder_jpeg(dest, f"{remote}|{index}|{timestamp:.2f}".encode("utf-8"))
            written.append(dest)
        return written

    def perceptual_hash(self, image_path: Path, *, signature: bytes | None = None) -> str | None:
        if signature is None:
            signature = image_path.read_bytes()[:256] if image_path.exists() else b""
        return hashlib.sha256(signature).hexdigest()[:16]
