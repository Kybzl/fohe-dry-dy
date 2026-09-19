"""Re-tag an existing clip without touching its media (section 28).

The clip file, thumbnail, hashes, provenance and creation time stay exactly as
they are; only the semantic tags, scores and the recorded prompt version change
and the audit trail grows by the new ``ai_runs`` rows.

The same service powers the ``clip_tagging_v1`` vs ``clip_tagging_v2`` A/B
comparison, which never writes to the production rows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ai.base import AuditContext, ClipTaggingRequest
from ai.gateway import AIGateway
from ai.schemas import prompt_version
from core.frame_policy import clip_frame_timestamps
from core.models import ClipRecord, ClipTagging, PreviewFrame
from core.normalization import normalize_tagging
from core.tag_audit import TagRow
from media.ffmpeg import MediaToolkit
from storage.library import MaterialLibrary

LOGGER = logging.getLogger(__name__)


@dataclass
class RetagOutcome:
    """Result of one ``retag`` / A/B tagging call for a single clip."""

    clip_id: int
    version: str
    tagging: ClipTagging | None = None
    error: str = ""
    applied: bool = False
    tokens: int | None = None
    latency_ms: int | None = None

    @property
    def ok(self) -> bool:
        return self.tagging is not None

    def tag_row(self, clip: ClipRecord) -> TagRow:
        tagging = self.tagging
        if tagging is None:
            return TagRow(
                clip_id=clip.id,
                source_video_id=clip.source_video_id,
                prompt_version=self.version,
            )
        return TagRow(
            clip_id=clip.id,
            source_video_id=clip.source_video_id,
            description=tagging.description,
            material=tagging.material,
            material_form=str(tagging.material_form),
            material_state=str(tagging.material_state),
            process_stage=str(tagging.process_stage),
            shot_type=str(tagging.shot_type),
            subtitle_type=str(tagging.subtitle_type),
            overall=tagging.scores.overall,
            prompt_version=self.version,
            total_tokens=self.tokens,
            latency_ms=self.latency_ms,
            scores=(
                tagging.scores.material_relevance,
                tagging.scores.visual_quality,
                tagging.scores.subtitle_cleanliness,
                tagging.scores.stability,
                tagging.scores.composition,
                tagging.scores.overall,
            ),
        )


class ClipRetagger:
    """Samples frames from a stored clip and re-runs ``clip_tagging``."""

    def __init__(
        self,
        *,
        gateway: AIGateway,
        toolkit: MediaToolkit,
        library: MaterialLibrary,
        frames_dir: Path,
        frame_ratios: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8),
        max_width: int | None = 640,
    ) -> None:
        self.gateway = gateway
        self.toolkit = toolkit
        self.library = library
        self.frames_dir = Path(frames_dir)
        self.frame_ratios = tuple(frame_ratios) or (0.2, 0.4, 0.6, 0.8)
        self.max_width = max_width

    # -- frames ------------------------------------------------------------
    async def sample_frames(self, clip: ClipRecord) -> list[PreviewFrame]:
        """Sample the same positions the pipeline uses, from the clip file."""

        path = Path(clip.file_path)
        if not path.exists():
            raise FileNotFoundError(f"clip file is missing: {path}")
        duration = float(clip.duration or (clip.source_end - clip.source_start) or 0.0)
        if duration <= 0:
            raise ValueError(f"clip #{clip.id} has no usable duration")
        stamps = clip_frame_timestamps(0.0, duration, len(self.frame_ratios), ratios=self.frame_ratios)
        out_dir = self.frames_dir / f"retag_{clip.id}"
        written = await self.toolkit.extract_frames(
            path, stamps, out_dir, f"clip{clip.id}", size=self.max_width
        )
        return [
            PreviewFrame(timestamp=timestamp, image_path=image, source="clip")
            for timestamp, image in zip(stamps, written)
        ]

    def cleanup_frames(self, clip_id: int) -> None:
        """Delete the frames sampled for one clip (they are regenerable)."""

        self._cleanup(self.frames_dir / f"retag_{clip_id}")

    def _cleanup(self, directory: Path) -> None:
        """Frames are regenerable: never leave them behind."""

        try:
            if directory.exists():
                for file in directory.rglob("*"):
                    if file.is_file():
                        file.unlink(missing_ok=True)
                directory.rmdir()
        except OSError as exc:  # pragma: no cover - defensive
            LOGGER.debug("could not clean retag frames %s: %s", directory, exc)

    # -- tagging -----------------------------------------------------------
    async def tag_clip(
        self,
        clip: ClipRecord,
        *,
        version: str = "",
        frames: list[PreviewFrame] | None = None,
        origin: str = "retag",
    ) -> RetagOutcome:
        resolved = prompt_version("clip_tagging", version)
        owns_frames = frames is None
        if frames is None:
            frames = await self.sample_frames(clip)
        try:
            return await self._tag_with_frames(clip, resolved, frames, origin=origin)
        finally:
            if owns_frames and clip.id is not None:
                self.cleanup_frames(int(clip.id))

    async def _tag_with_frames(
        self,
        clip: ClipRecord,
        resolved: str,
        frames: list[PreviewFrame],
        *,
        origin: str = "retag",
    ) -> RetagOutcome:
        request = ClipTaggingRequest(
            material=clip.library_category or clip.material,
            requested_material=clip.library_category or clip.material,
            query="",
            platform=clip.platform,
            platform_video_id=clip.platform_video_id,
            title=clip.source_title,
            duration=float(clip.duration or 0.0),
            context={"retag": True, "previous_description": clip.description},
            start=clip.source_start,
            end=clip.source_end,
            segment_description=clip.description,
            segment_relevance=clip.material_score,
            frames=frames,
            prompt_version=resolved,
            # the clip row already exists: link the audit row immediately
            audit=AuditContext(
                task_id=clip.task_id,
                source_video_id=clip.source_video_id,
                clip_id=clip.id,
                origin=origin,
            ),
        )
        tagging = await self.gateway.tag_clip(request)
        record = self.gateway.last_record
        outcome = RetagOutcome(
            clip_id=int(clip.id or 0),
            version=resolved,
            tokens=getattr(record, "total_tokens", None),
            latency_ms=getattr(record, "latency_ms", None),
        )
        if tagging is None:
            outcome.error = "tagging failed on all providers"
            return outcome
        outcome.tagging = normalize_tagging(
            tagging, request_material=clip.library_category or clip.material
        )
        return outcome

    async def retag(
        self,
        clip_id: int,
        *,
        version: str = "",
        apply: bool = True,
    ) -> RetagOutcome:
        """Re-tag one stored clip; optionally persist the new tags."""

        clip = self.library.get_clip(clip_id)
        if clip is None:
            return RetagOutcome(clip_id=clip_id, version=version, error="clip not found")
        outcome = await self.tag_clip(clip, version=version)
        if outcome.ok and apply:
            self.library.replace_clip_tags(
                clip_id,
                outcome.tagging,  # type: ignore[arg-type]
                prompt_version=outcome.version,
            )
            outcome.applied = True
        return outcome

    async def compare(
        self,
        clip_ids: list[int],
        *,
        versions: tuple[str, ...] = ("clip_tagging_v1", "clip_tagging_v2"),
    ) -> dict[str, list[RetagOutcome]]:
        """Tag the same clips with several prompt versions (no writes)."""

        results: dict[str, list[RetagOutcome]] = {}
        for version in versions:
            results[version] = []
        for clip_id in clip_ids:
            clip = self.library.get_clip(clip_id)
            if clip is None:
                for version in versions:
                    results[version].append(
                        RetagOutcome(clip_id=clip_id, version=version, error="clip not found")
                    )
                continue
            try:
                # one frame set per clip, reused by every version so the
                # comparison isolates the prompt (section 26)
                frames = await self.sample_frames(clip)
            except Exception as exc:
                for version in versions:
                    results[version].append(
                        RetagOutcome(clip_id=clip_id, version=version, error=str(exc)[:200])
                    )
                continue
            try:
                for version in versions:
                    results[version].append(
                        await self.tag_clip(
                            clip, version=version, frames=frames, origin="evaluation"
                        )
                    )
            finally:
                self.cleanup_frames(clip_id)
        return results
