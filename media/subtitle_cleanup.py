"""Deterministic local cleanup engines for subtitle-region derivatives.

Version 1 intentionally supports one bounded, deterministic operation:
FFmpeg's ``delogo`` filter over the *smallest safe rectangles* found by the
Milestone 9.2 temporal tracker.  No generative model, GPU dependency or cloud
video service is involved.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Sequence

from core.subtitle_cleanup_models import CleanupMask
from media.ffmpeg import (
    EncodeSettings,
    FFmpegError,
    close_transport,
    discover_binary,
)

LOGGER = logging.getLogger(__name__)


class CleanupEngineError(RuntimeError):
    """The local cleanup engine could not produce a usable derivative."""


class CleanupEngine(ABC):
    """Apply one set of cleanup masks to one source video."""

    name: str = "unknown"

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """``(usable, explanation)`` - never raises."""

    @abstractmethod
    async def apply(
        self,
        source: Path,
        dest: Path,
        masks: Sequence[CleanupMask],
        *,
        width: int,
        height: int,
        encode_settings: EncodeSettings,
    ) -> Path:
        """Write the cleaned derivative to ``dest`` (temporary path)."""


class FFmpegDelogoEngine(CleanupEngine):
    """Deterministic FFmpeg ``delogo`` with optional time-scoped filters."""

    name = "ffmpeg_delogo"

    def __init__(
        self,
        *,
        ffmpeg_bin: str = "ffmpeg",
        timeout: float = 300.0,
        time_scoped: bool = True,
    ) -> None:
        self.ffmpeg_bin = discover_binary(ffmpeg_bin, "ffmpeg")
        self.timeout = float(timeout)
        self.time_scoped = bool(time_scoped)

    def available(self) -> tuple[bool, str]:
        resolved = shutil.which(self.ffmpeg_bin) or (
            self.ffmpeg_bin if os.path.isfile(self.ffmpeg_bin) else None
        )
        if resolved is None:
            return False, f"ffmpeg not found: {self.ffmpeg_bin}"
        return True, resolved

    # -- pure argument builders (unit tested without FFmpeg) ---------------
    @staticmethod
    def build_delogo_filter(
        mask: CleanupMask,
        *,
        width: int,
        height: int,
        time_scoped: bool = True,
    ) -> str:
        x, y, box_w, box_h = mask.pixel_box(width, height)
        options = [
            f"x={x}",
            f"y={y}",
            f"w={box_w}",
            f"h={box_h}",
        ]
        if time_scoped and mask.active_intervals:
            expressions = []
            for interval in mask.active_intervals:
                if len(interval) < 2:
                    continue
                start = max(0.0, float(interval[0]))
                end = max(start, float(interval[1]))
                expressions.append(f"between(t,{start:.3f},{end:.3f})")
            if expressions:
                options.append("enable='" + "+".join(expressions) + "'")
        return "delogo=" + ":".join(options)

    @classmethod
    def build_filter_complex(
        cls,
        masks: Sequence[CleanupMask],
        *,
        width: int,
        height: int,
        time_scoped: bool = True,
    ) -> str:
        filters = [
            cls.build_delogo_filter(
                mask,
                width=width,
                height=height,
                time_scoped=time_scoped,
            )
            for mask in masks
        ]
        if not filters:
            raise CleanupEngineError("no cleanup masks were supplied")
        return f"[0:v]{','.join(filters)}[v]"

    @classmethod
    def build_args(
        cls,
        source: Path,
        dest: Path,
        masks: Sequence[CleanupMask],
        *,
        width: int,
        height: int,
        encode_settings: EncodeSettings,
        time_scoped: bool = True,
    ) -> list[str]:
        return [
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-filter_complex",
            cls.build_filter_complex(
                masks,
                width=width,
                height=height,
                time_scoped=time_scoped,
            ),
            "-map",
            "[v]",
            "-map",
            "0:a?",
            *encode_settings.video_args(),
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(dest),
        ]

    # -- execution ---------------------------------------------------------
    async def apply(
        self,
        source: Path,
        dest: Path,
        masks: Sequence[CleanupMask],
        *,
        width: int,
        height: int,
        encode_settings: EncodeSettings,
    ) -> Path:
        usable, note = self.available()
        if not usable:
            raise CleanupEngineError(note)
        dest.parent.mkdir(parents=True, exist_ok=True)
        args = self.build_args(
            source,
            dest,
            masks,
            width=width,
            height=height,
            encode_settings=encode_settings,
            time_scoped=self.time_scoped,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                self.ffmpeg_bin,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:  # pragma: no cover - race with PATH
            raise CleanupEngineError(str(exc)) from exc
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise CleanupEngineError(
                f"ffmpeg delogo timed out after {self.timeout:.0f}s"
            ) from exc
        finally:
            close_transport(process)
        if process.returncode != 0:
            tail = (stderr or b"").decode("utf-8", errors="replace")[-400:]
            raise CleanupEngineError(
                f"ffmpeg delogo exited with {process.returncode}: {tail}"
            )
        if not dest.exists() or dest.stat().st_size <= 0:
            raise CleanupEngineError(f"ffmpeg delogo produced no output: {dest}")
        return dest


class OpenCVInpaintEngine(CleanupEngine):
    """Frame-wise local inpainting with time-scoped subtitle masks.

    Frames are repaired with OpenCV's deterministic Telea algorithm.  A
    temporary MJPEG stream is then encoded with the project's FFmpeg settings
    and the original audio stream is mapped back in.  The source is never
    modified.
    """

    name = "opencv_inpaint"

    def __init__(
        self,
        *,
        ffmpeg_bin: str = "ffmpeg",
        timeout: float = 300.0,
        time_scoped: bool = True,
        radius: float = 3.0,
    ) -> None:
        self.ffmpeg_bin = discover_binary(ffmpeg_bin, "ffmpeg")
        self.timeout = float(timeout)
        self.time_scoped = bool(time_scoped)
        self.radius = max(0.5, float(radius))

    def available(self) -> tuple[bool, str]:
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
        except Exception as exc:
            return False, f"opencv/numpy unavailable: {exc}"
        resolved = shutil.which(self.ffmpeg_bin) or (
            self.ffmpeg_bin if os.path.isfile(self.ffmpeg_bin) else None
        )
        if resolved is None:
            return False, f"ffmpeg not found: {self.ffmpeg_bin}"
        return True, f"opencv + {resolved}"

    @staticmethod
    def mask_active(mask: CleanupMask, timestamp: float, *, time_scoped: bool) -> bool:
        if not time_scoped or not mask.active_intervals:
            return True
        value = float(timestamp)
        return any(
            len(interval) >= 2
            and float(interval[0]) <= value <= float(interval[1])
            for interval in mask.active_intervals
        )

    @classmethod
    def build_frame_mask(
        cls,
        masks: Sequence[CleanupMask],
        *,
        width: int,
        height: int,
        timestamp: float,
        time_scoped: bool = True,
    ):
        import numpy as np

        bitmap = np.zeros((height, width), dtype=np.uint8)
        for mask in masks:
            if not cls.mask_active(mask, timestamp, time_scoped=time_scoped):
                continue
            x, y, box_w, box_h = mask.pixel_box(width, height)
            bitmap[y : y + box_h, x : x + box_w] = 255
        return bitmap

    @staticmethod
    def refine_text_mask(frame, region_mask):
        """Reduce broad OCR rectangles to locally bright subtitle strokes.

        OCR returns a bounding envelope, not character pixels.  Feeding that
        whole envelope to Telea stretches the surrounding scene across a wide
        band.  Most Douyin captions in this workflow are bright glyphs, so a
        local luminance top-hat gives a conservative, frame-specific stroke
        mask while keeping the rectangle as a hard safety boundary.
        """

        import cv2
        import numpy as np

        if frame is None or region_mask is None or not np.any(region_mask):
            return np.zeros(region_mask.shape, dtype=np.uint8)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # A 15 px neighbourhood is larger than a normal glyph stroke at the
        # source resolutions we accept, but remains small enough to follow a
        # changing video background.
        local_background = cv2.GaussianBlur(gray, (15, 15), 0)
        delta = cv2.subtract(gray, local_background)
        strokes = np.where(
            (region_mask > 0) & (gray >= 145) & (delta >= 18),
            255,
            0,
        ).astype(np.uint8)
        # Join anti-aliased fragments and cover the immediate glyph edge;
        # dilation is deliberately bounded so it cannot recreate the OCR box.
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        strokes = cv2.morphologyEx(strokes, cv2.MORPH_CLOSE, kernel)
        # Two pixels of expansion cover anti-aliased rims and the dark outline
        # immediately adjoining a bright caption stroke.  This remains far
        # smaller than the OCR envelope and avoids restoring the old band
        # inpainting behaviour.
        strokes = cv2.dilate(strokes, kernel, iterations=2)
        return cv2.bitwise_and(strokes, region_mask)

    def _render_silent(
        self,
        source: Path,
        silent_path: Path,
        masks: Sequence[CleanupMask],
        *,
        width: int,
        height: int,
    ) -> None:
        import cv2

        capture = cv2.VideoCapture(str(source))
        if not capture.isOpened():
            raise CleanupEngineError(f"opencv cannot open source: {source}")
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        if fps <= 0:
            capture.release()
            raise CleanupEngineError("opencv source has invalid fps")
        actual_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or width)
        actual_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or height)
        writer = cv2.VideoWriter(
            str(silent_path),
            cv2.VideoWriter_fourcc(*"MJPG"),
            fps,
            (actual_width, actual_height),
        )
        if not writer.isOpened():
            capture.release()
            raise CleanupEngineError("opencv could not create intermediate video")
        frame_index = 0
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                timestamp = frame_index / fps
                bitmap = self.build_frame_mask(
                    masks,
                    width=actual_width,
                    height=actual_height,
                    timestamp=timestamp,
                    time_scoped=self.time_scoped,
                )
                if bitmap.any():
                    stroke_mask = self.refine_text_mask(frame, bitmap)
                    if stroke_mask.any():
                        frame = cv2.inpaint(
                            frame, stroke_mask, self.radius, cv2.INPAINT_TELEA
                        )
                writer.write(frame)
                frame_index += 1
        finally:
            capture.release()
            writer.release()
        if frame_index <= 0 or not silent_path.exists() or silent_path.stat().st_size <= 0:
            raise CleanupEngineError("opencv produced no frames")

    async def apply(
        self,
        source: Path,
        dest: Path,
        masks: Sequence[CleanupMask],
        *,
        width: int,
        height: int,
        encode_settings: EncodeSettings,
    ) -> Path:
        usable, note = self.available()
        if not usable:
            raise CleanupEngineError(note)
        if not masks:
            raise CleanupEngineError("no cleanup masks were supplied")
        dest.parent.mkdir(parents=True, exist_ok=True)
        handle, raw_path = tempfile.mkstemp(
            prefix="subtitle_inpaint_", suffix=".avi", dir=str(dest.parent)
        )
        os.close(handle)
        silent_path = Path(raw_path)
        try:
            await asyncio.to_thread(
                self._render_silent,
                source,
                silent_path,
                masks,
                width=width,
                height=height,
            )
            args = [
                "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(silent_path), "-i", str(source),
                "-map", "0:v:0", "-map", "1:a?",
                *encode_settings.video_args(),
                "-c:a", "copy", "-movflags", "+faststart", str(dest),
            ]
            process = await asyncio.create_subprocess_exec(
                self.ffmpeg_bin,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError as exc:
                process.kill()
                await process.wait()
                raise CleanupEngineError(
                    f"ffmpeg inpaint encode timed out after {self.timeout:.0f}s"
                ) from exc
            finally:
                close_transport(process)
            if process.returncode != 0:
                tail = (stderr or b"").decode("utf-8", errors="replace")[-400:]
                raise CleanupEngineError(
                    f"ffmpeg inpaint encode exited with {process.returncode}: {tail}"
                )
            if not dest.exists() or dest.stat().st_size <= 0:
                raise CleanupEngineError(f"opencv inpaint produced no output: {dest}")
            return dest
        finally:
            try:
                silent_path.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("could not remove inpaint intermediate %s", silent_path)
