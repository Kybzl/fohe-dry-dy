"""Clip cutting, thumbnail selection and per-clip artifact metadata.

The cutter is backend agnostic: it drives whichever ``MediaToolkit`` was
injected (real FFmpeg, or the placeholder mock backend used by tests).
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Sequence

from core.frame_policy import DEFAULT_CLIP_RATIOS, clip_frame_timestamps
from core.models import ClipArtifact, SegmentTiming
from media.ffmpeg import EncodeSettings, MediaToolkit, pick_best_frame_index

LOGGER = logging.getLogger(__name__)

_WHITESPACE = re.compile(r"\s+")


class ClipCutter:
    """Turns an approved time range into a real file plus its thumbnail."""

    def __init__(
        self,
        toolkit: MediaToolkit,
        *,
        thumbnail_timestamp_ratio: float = 0.5,
        thumbnail_candidates: int = 5,
        precise_cut: bool = True,
        encode_settings: EncodeSettings | None = None,
        #: default positions inside a final clip used for tagging (section 7)
        tagging_frame_ratios: Sequence[float] | None = None,
        #: downscale the frames sent to the tagger (image tokens dominate the
        #: cost of clip tagging); the thumbnail keeps the source resolution
        tagging_frame_max_width: int | None = None,
    ) -> None:
        self.toolkit = toolkit
        self.thumbnail_timestamp_ratio = thumbnail_timestamp_ratio
        self.thumbnail_candidates = max(1, thumbnail_candidates)
        self.precise_cut = precise_cut
        self.encode_settings = encode_settings or EncodeSettings()
        self.tagging_frame_ratios: tuple[float, ...] = tuple(
            tagging_frame_ratios or DEFAULT_CLIP_RATIOS
        )
        self.tagging_frame_max_width = (
            int(tagging_frame_max_width) if tagging_frame_max_width else None
        )

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def compute_content_key(material: str, description: str, duration: float) -> str:
        """Semantic fingerprint used for *similarity* grouping (not dedup)."""

        normalized = _WHITESPACE.sub(" ", (description or "").strip())
        payload = f"{material.strip()}|{normalized}|{round(duration, 1)}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def thumbnail_timestamps(self, timing: SegmentTiming) -> list[float]:
        """Candidate timestamps around the middle of the clip.

        Frames near the very start/end are avoided on purpose: those are the
        positions where transitions, fades and blur live.
        """

        duration = timing.duration
        if duration <= 0:
            return []
        if self.thumbnail_candidates == 1:
            return [round(timing.start + duration * self.thumbnail_timestamp_ratio, 3)]
        low, high = 0.3, 0.7
        step = (high - low) / (self.thumbnail_candidates - 1)
        return [
            round(timing.start + duration * (low + step * index), 3)
            for index in range(self.thumbnail_candidates)
        ]

    # -- main entry point --------------------------------------------------
    async def cut(
        self,
        *,
        source_video: Path,
        timing: SegmentTiming,
        dest: Path,
        thumbnail_dest: Path,
        content_key: str,
        encode_settings: EncodeSettings | None = None,
        has_audio: bool | None = None,
        reencode: bool | None = None,
        scratch_dir: Path | None = None,
        tagging_frame_ratios: Sequence[float] | None = None,
        tagging_frames_dir: Path | None = None,
    ) -> ClipArtifact:
        """Cut ``timing`` out of ``source_video`` and hash the result.

        ``tagging_frame_ratios`` / ``tagging_frames_dir`` enable the Milestone 3.7
        single-pass frame plan: the thumbnail candidates and the tagging frames
        are extracted together (each timestamp only once) and the tagging frames
        are kept in ``tagging_frames_dir`` for the tagger to read.
        """

        reencode = self.precise_cut if reencode is None else reencode
        await self.toolkit.cut_clip(
            source_video,
            timing.start,
            timing.end,
            dest,
            reencode=reencode,
            content_key=content_key,
            encode_settings=encode_settings or self.encode_settings,
            has_audio=has_audio,
        )

        ratios = tuple(tagging_frame_ratios or self.tagging_frame_ratios)
        thumbnail_path, tagging_frames = await self._produce_frames(
            source_video=source_video,
            timing=timing,
            thumbnail_dest=thumbnail_dest,
            scratch_dir=scratch_dir,
            tagging_frame_ratios=ratios,
            tagging_frames_dir=tagging_frames_dir,
        )

        sha256 = self.toolkit.sha256_file(dest) if dest.exists() else None
        signature = content_key.encode("utf-8")
        phash = None
        if thumbnail_path is not None:
            phash = self.toolkit.perceptual_hash(thumbnail_path, signature=signature)
        elif dest.exists():
            phash = self.toolkit.perceptual_hash(dest, signature=signature)

        try:
            info = await self.toolkit.probe(dest)
            duration = info.duration or timing.duration
            width, height, fps = info.width, info.height, info.fps
            size_bytes = info.size_bytes
        except Exception as exc:
            LOGGER.debug("probing cut clip %s failed: %s", dest, exc)
            duration, width, height, fps = timing.duration, None, None, None
            size_bytes = dest.stat().st_size if dest.exists() else 0

        return ClipArtifact(
            file_path=dest,
            thumbnail_path=thumbnail_path,
            duration=round(duration or timing.duration, 3),
            width=width,
            height=height,
            fps=fps,
            size_bytes=size_bytes,
            sha256=sha256,
            phash=phash,
            tagging_frames=tagging_frames,
        )

    def tagging_timestamps(self, timing: SegmentTiming, ratios: Sequence[float]) -> list[float]:
        """Absolute timestamps of the frames used for tagging (section 7)."""

        positions = tuple(ratios) or DEFAULT_CLIP_RATIOS
        return clip_frame_timestamps(timing.start, timing.end, len(positions), ratios=positions)

    async def _produce_frames(
        self,
        *,
        source_video: Path,
        timing: SegmentTiming,
        thumbnail_dest: Path,
        scratch_dir: Path | None,
        tagging_frame_ratios: Sequence[float] | None = None,
        tagging_frames_dir: Path | None = None,
    ) -> tuple[Path | None, list[Path]]:
        """Extract the thumbnail candidates (and tagging frames) in one pass.

        Each timestamp is extracted exactly once even when it is needed for both
        purposes, and the tagging frames survive the call when
        ``tagging_frames_dir`` is given (the caller deletes that directory after
        tagging).  Returns ``(thumbnail_path, tagging_frame_paths)``.
        """

        thumbnail_stamps = self.thumbnail_timestamps(timing)
        ratios = tuple(tagging_frame_ratios or ())
        tagging_stamps = (
            self.tagging_timestamps(timing, ratios) if ratios and timing.duration > 0 else []
        )
        if not thumbnail_stamps and not tagging_stamps:
            return None, []
        if not thumbnail_stamps:
            # no thumbnail candidate: the middle tagging frame becomes the
            # thumbnail so a tagged clip always has one
            middle = tagging_stamps[len(tagging_stamps) // 2]
            thumbnail_stamps = [middle]

        temp_context: tempfile.TemporaryDirectory[str] | None = None
        persistent = tagging_frames_dir is not None
        if persistent:
            work_dir = Path(tagging_frames_dir)
            work_dir.mkdir(parents=True, exist_ok=True)
        elif scratch_dir is None:
            temp_context = tempfile.TemporaryDirectory(prefix="clip-thumbs-")
            work_dir = Path(temp_context.name)
        else:
            work_dir = Path(scratch_dir)
            work_dir.mkdir(parents=True, exist_ok=True)

        # one extraction per unique timestamp, whatever it is needed for
        thumb_keys = [round(stamp, 3) for stamp in thumbnail_stamps]
        tag_keys = [round(stamp, 3) for stamp in tagging_stamps]
        unique_keys = sorted(set(thumb_keys) | set(tag_keys))
        extracted: dict[float, Path] = {}
        try:
            for index, timestamp in enumerate(unique_keys):
                target = work_dir / f"{thumbnail_dest.stem}_f{index:02d}.jpg"
                try:
                    if self.tagging_frame_max_width:
                        await self.toolkit.extract_frames(
                            source_video,
                            [timestamp],
                            work_dir,
                            target.stem,
                            size=self.tagging_frame_max_width,
                        )
                        produced = work_dir / f"{target.stem}_000.jpg"
                        if produced.exists():
                            produced.replace(target)
                    else:
                        await self.toolkit.extract_representative_frame(
                            source_video, timestamp, target
                        )
                    extracted[timestamp] = target
                except Exception as exc:
                    LOGGER.debug("frame extraction at %ss failed: %s", timestamp, exc)

            if not extracted:
                LOGGER.warning("no frame could be extracted for %s", source_video.name)
                return None, []

            thumbnail_pool = [extracted[key] for key in thumb_keys if key in extracted]
            if not thumbnail_pool:  # pragma: no cover - defensive
                thumbnail_pool = list(extracted.values())
            best = thumbnail_pool[pick_best_frame_index(thumbnail_pool)]
            thumbnail_dest.parent.mkdir(parents=True, exist_ok=True)
            if self.tagging_frame_max_width:
                # the pool frames are downscaled for the tagger: re-extract the
                # chosen one at full resolution so the delivered thumbnail stays
                # sharp
                best_timestamp = next(
                    (key for key, path in extracted.items() if path == best),
                    thumb_keys[0] if thumb_keys else unique_keys[0],
                )
                try:
                    await self.toolkit.make_thumbnail(
                        source_video, best_timestamp, thumbnail_dest
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    LOGGER.debug("full resolution thumbnail failed: %s", exc)
                    shutil.copy2(best, thumbnail_dest)
            else:
                shutil.copy2(best, thumbnail_dest)

            tagging_frames: list[Path] = (
                [extracted[key] for key in tag_keys if key in extracted] if persistent else []
            )
            return thumbnail_dest, tagging_frames
        except Exception as exc:
            LOGGER.warning("thumbnail generation failed for %s: %s", source_video.name, exc)
            return None, []
        finally:
            if not persistent:
                for leftover in work_dir.glob(f"{thumbnail_dest.stem}_*.jpg"):
                    try:
                        leftover.unlink()
                    except OSError:  # pragma: no cover - defensive
                        pass
            if temp_context is not None:
                temp_context.cleanup()
