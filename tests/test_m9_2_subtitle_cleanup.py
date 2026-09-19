"""Milestone 9.2 regression tests: conservative local subtitle cleanup.

Normal tests never touch the cloud, Douyin, a browser or the production
library.  They exercise the pure geometry rules and the service with small
deterministic fakes; the real FFmpeg/RapidOCR acceptance lives in the
operator runbook, not in this file.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from analyzers.subtitle_analysis import SubtitleAnalysisSettings, SubtitleAnalyzer
from analyzers.text_detection import TextDetector
from core.dependencies import build_library
from core.models import (
    ClipArtifact,
    ClipScores,
    ClipTagging,
    MaterialForm,
    MaterialState,
    ProcessStage,
    SegmentTiming,
    ShotType,
    SubtitleType,
)
from core.subtitle_cleanup import (
    CleanupOutcome,
    OcrSample,
    SubtitleCleanupService,
    build_tracks,
    evaluate_evidence_reduction,
    evaluate_quality_guard,
    plan_masks,
    sample_timestamps,
    stable_tracks,
)
from core.subtitle_cleanup_models import (
    CLEANUP_VERSION,
    CleanupMask,
    CleanupStatus,
    SubtitleCleanupConfig,
)
from core.subtitle_models import SubtitleAnalysisResult, TextRegion, frame_metrics
from media.ffmpeg import EncodeSettings, MediaInfo
from media.subtitle_cleanup import FFmpegDelogoEngine, OpenCVInpaintEngine


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Deterministic test doubles
# ---------------------------------------------------------------------------
def _textured(path: Path, *, seed: int = 0, size: int = 64) -> Path:
    """Write a small textured grayscale image (no external fixtures)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(image)
    for y in range(0, size, 4):
        for x in range(0, size, 4):
            value = (x * 5 + y * 3 + seed * 17) % 200 + 30
            draw.rectangle([x, y, x + 2, y + 2], fill=value)
    image.save(path, quality=95)
    return path


def _region(
    *,
    y1: float = 0.80,
    y2: float = 0.90,
    x1: float = 0.20,
    x2: float = 0.80,
    text: str = "字幕",
    confidence: float = 0.92,
) -> TextRegion:
    return TextRegion(
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        confidence=confidence,
        text=text,
        engine="scripted",
    )


class ScriptedDetector(TextDetector):
    """Returns canned regions keyed by the frame-name prefix."""

    name = "scripted"
    recognizes_text = True

    def __init__(
        self,
        *,
        before: list[TextRegion] | None = None,
        after: list[TextRegion] | None = None,
    ) -> None:
        self.before = before if before is not None else [_region()]
        self.after = after if after is not None else []
        self.calls = 0

    def available(self) -> tuple[bool, str]:
        return True, "scripted detector"

    def detect(
        self, image_path: Path | str, *, recognize: bool | None = None
    ) -> list[TextRegion]:
        self.calls += 1
        name = Path(image_path).name
        source = self.after if name.startswith("after") else self.before
        return [item.model_copy(deep=True) for item in source]


class FakeToolkit:
    """Duck-typed MediaToolkit double: real files, no FFmpeg."""

    def __init__(
        self,
        *,
        duration: float = 6.0,
        width: int = 1080,
        height: int = 1920,
        fps: float = 30.0,
        after_duration: float | None = None,
        after_width: int | None = None,
        after_height: int | None = None,
        probe_candidate_error: bool = False,
    ) -> None:
        self.duration = duration
        self.width = width
        self.height = height
        self.fps = fps
        self.after_duration = after_duration if after_duration is not None else duration
        self.after_width = after_width if after_width is not None else width
        self.after_height = after_height if after_height is not None else height
        self.probe_candidate_error = probe_candidate_error
        self.extract_calls: list[tuple[str, tuple[float, ...]]] = []

    async def probe(self, path: Path) -> MediaInfo:
        if path.name == "candidate.mp4":
            if self.probe_candidate_error:
                raise RuntimeError("corrupt derivative")
            return MediaInfo(
                duration=self.after_duration,
                width=self.after_width,
                height=self.after_height,
                fps=self.fps,
                codec="h264",
                audio_codec="aac",
                has_audio=True,
            )
        return MediaInfo(
            duration=self.duration,
            width=self.width,
            height=self.height,
            fps=self.fps,
            codec="h264",
            audio_codec="aac",
            has_audio=True,
        )

    async def extract_frames(
        self,
        video: Path,
        timestamps: list[float],
        out_dir: Path,
        prefix: str,
        *,
        size: int | None = None,
    ) -> list[Path]:
        self.extract_calls.append((prefix, tuple(timestamps)))
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for index, _timestamp in enumerate(timestamps):
            written.append(_textured(out_dir / f"{prefix}_{index:03d}.jpg", seed=index + 1))
        return written


class FakeEngine:
    """Deterministic engine double that copies the source (original untouched)."""

    name = "fake_engine"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[Path, tuple[CleanupMask, ...]]] = []
        self.attempts = 0

    def available(self) -> tuple[bool, str]:
        return True, "fake engine"

    async def apply(
        self,
        source: Path,
        dest: Path,
        masks: list[CleanupMask] | tuple[CleanupMask, ...],
        *,
        width: int,
        height: int,
        encode_settings: EncodeSettings,
    ) -> Path:
        self.attempts += 1
        if self.fail:
            raise RuntimeError("engine boom")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(Path(source).read_bytes())
        self.calls.append((dest, tuple(masks)))
        return dest


def _add_clip(
    library,
    root: Path,
    *,
    index: int = 1,
    category: str = "苹果干",
    subtitle_type: SubtitleType = SubtitleType.BOTTOM_SIMPLE,
    duration: float = 6.0,
) -> int:
    clip_dir = root / category / "clips"
    thumb_dir = root / category / "thumbnails"
    clip_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)
    video = clip_dir / f"clip_{index:03d}.mp4"
    thumb = thumb_dir / f"clip_{index:03d}.jpg"
    video.write_bytes(b"original-video")
    thumb.write_bytes(b"thumb")
    tagging = ClipTagging(
        material="苹果",
        material_form=MaterialForm.SLICE,
        material_state=MaterialState.DRYING,
        process_stage=ProcessStage.DRYING,
        shot_type=ShotType.CLOSE_UP,
        scene="烘干房",
        subtitle_type=subtitle_type,
        subtitle_score=0.1,
        description="字幕清理测试片段",
        scores=ClipScores(
            material_relevance=0.9,
            visual_quality=0.8,
            subtitle_cleanliness=0.4,
            stability=0.8,
            composition=0.8,
            overall=0.8,
        ),
    )
    artifact = ClipArtifact(
        file_path=video,
        thumbnail_path=thumb,
        duration=duration,
        width=1080,
        height=1920,
        fps=30.0,
        sha256=f"sha{index:04d}",
        phash=f"ph{index:06d}",
    )
    return library.insert_clip(
        task_id=None,
        source_video_id=None,
        platform="douyin",
        platform_video_id=f"vid-{index}",
        source_url=f"https://www.douyin.com/video/{index}",
        tagging=tagging,
        timing=SegmentTiming(start=0.0, end=duration),
        artifact=artifact,
        content_key=f"ck-{index}",
        library_category=category,
        provenance="douyin_real",
        tag_prompt_version="clip_tagging_v2",
    )


def _service(
    settings,
    *,
    detector: ScriptedDetector | None = None,
    toolkit: FakeToolkit | None = None,
    engine: FakeEngine | None = None,
) -> tuple[SubtitleCleanupService, ScriptedDetector, FakeToolkit, FakeEngine]:
    library = build_library(settings)
    detector = detector or ScriptedDetector()
    toolkit = toolkit or FakeToolkit()
    engine = engine or FakeEngine()
    analyzer = SubtitleAnalyzer(
        SubtitleAnalysisSettings(detector=detector, max_frames=6)
    )
    service = SubtitleCleanupService(
        library,
        settings,
        analyzer=analyzer,
        toolkit=toolkit,
        engine=engine,
    )
    return service, detector, toolkit, engine


# ---------------------------------------------------------------------------
# Eligibility rules
# ---------------------------------------------------------------------------
def test_eligibility_bottom_top_single() -> None:
    config = SubtitleCleanupConfig()
    assert config.eligible("bottom_simple")
    assert config.eligible("top_simple")
    assert config.eligible("single_region")


def test_ineligible_multi_region_and_colored_block() -> None:
    config = SubtitleCleanupConfig()
    assert not config.eligible("multi_region")
    assert not config.eligible("colored_block")
    assert not config.eligible("large_center_text")
    assert not config.eligible("promotional_overlay")
    assert not config.eligible("dense_text")


def test_none_and_watermark_are_not_needed(settings) -> None:
    for index, classification in enumerate(
        (SubtitleType.NONE, SubtitleType.WATERMARK_ONLY), start=1
    ):
        detector = ScriptedDetector(
            before=[] if classification is SubtitleType.NONE else [
                TextRegion(
                    x1=0.02,
                    y1=0.02,
                    x2=0.12,
                    y2=0.06,
                    confidence=0.8,
                    text="",
                )
            ]
        )
        service, _detector, _toolkit, engine = _service(settings, detector=detector)
        clip_id = _add_clip(service.library, settings.paths.library_root, index=index)
        outcome = run(service.cleanup_clip(clip_id))
        assert outcome.status is CleanupStatus.NOT_NEEDED
        assert engine.calls == []
        record = service.library.subtitle_cleanup(clip_id)
        assert record is not None and record["status"] == "not_needed"


def test_ineligible_complex_classification(settings) -> None:
    before = [
        _region(y1=0.72, y2=0.80, text="底部"),
        _region(y1=0.20, y2=0.32, text="顶部"),
    ]
    service, _detector, _toolkit, engine = _service(
        settings, detector=ScriptedDetector(before=before)
    )
    clip_id = _add_clip(service.library, settings.paths.library_root, index=3)
    outcome = run(service.cleanup_clip(clip_id))
    assert outcome.status is CleanupStatus.INELIGIBLE
    assert "classification_not_eligible" in outcome.reason
    assert engine.calls == []


# ---------------------------------------------------------------------------
# Temporal tracking / mask geometry
# ---------------------------------------------------------------------------
def test_track_association_keeps_changing_text_in_one_track() -> None:
    samples = [
        OcrSample(timestamp=0.0, region=_region(text="第一句")),
        OcrSample(timestamp=0.5, region=_region(text="第二句")),
        OcrSample(timestamp=1.0, region=_region(text="第三句")),
    ]
    tracks = build_tracks(samples, config=SubtitleCleanupConfig(), total_samples=3)
    assert len(tracks) == 1
    assert tracks[0].sample_count == 3
    assert "第一句" in tracks[0].texts and "第三句" in tracks[0].texts


def test_two_boxes_in_one_frame_do_not_inflate_persistence() -> None:
    samples = [
        OcrSample(timestamp=0.0, region=_region(text="上行", y1=0.80, y2=0.85)),
        OcrSample(timestamp=0.0, region=_region(text="下行", y1=0.86, y2=0.91)),
        OcrSample(timestamp=0.5, region=_region(text="上行", y1=0.80, y2=0.85)),
        OcrSample(timestamp=0.5, region=_region(text="下行", y1=0.86, y2=0.91)),
    ]
    tracks = build_tracks(samples, config=SubtitleCleanupConfig(), total_samples=2)
    assert len(tracks) == 2
    assert all(track.persistence_ratio <= 1.0 for track in tracks)
    assert sorted(track.sample_count for track in tracks) == [2, 2]


def test_jitter_threshold_rejects_wobbly_track() -> None:
    samples = [
        OcrSample(timestamp=0.0, region=_region(y1=0.90, y2=1.00)),
        OcrSample(timestamp=0.5, region=_region(y1=0.72, y2=0.82)),
        OcrSample(timestamp=1.0, region=_region(y1=0.90, y2=1.00)),
        OcrSample(timestamp=1.5, region=_region(y1=0.72, y2=0.82)),
    ]
    config = SubtitleCleanupConfig(max_center_distance=0.25)
    tracks = build_tracks(samples, config=config, total_samples=4)
    assert len(tracks) == 1
    assert tracks[0].vertical_jitter > config.max_vertical_jitter
    assert stable_tracks(tracks, config=config) == []


def test_persistence_threshold_rejects_transient_track() -> None:
    samples = [
        OcrSample(timestamp=0.0, region=_region()),
        OcrSample(timestamp=0.5, region=_region()),
    ]
    tracks = build_tracks(samples, config=SubtitleCleanupConfig(), total_samples=8)
    assert tracks
    assert tracks[0].persistence_ratio == 0.25
    assert stable_tracks(tracks, config=SubtitleCleanupConfig()) == []


def test_mask_padding_and_total_area() -> None:
    samples = [OcrSample(timestamp=float(i), region=_region()) for i in range(4)]
    tracks = build_tracks(samples, config=SubtitleCleanupConfig(), total_samples=4)
    masks, reason = plan_masks(
        tracks, config=SubtitleCleanupConfig(), width=1080, height=1920
    )
    assert reason == ""
    assert masks
    mask = masks[0]
    assert mask.x1 < 0.20 and mask.x2 > 0.80
    assert mask.y1 < 0.80 and mask.y2 > 0.90
    assert mask.active_intervals


def test_max_mask_area_guard_rejects_large_band() -> None:
    samples = [
        OcrSample(timestamp=float(i), region=_region(y1=0.10, y2=0.95, x1=0.05, x2=0.95))
        for i in range(4)
    ]
    tracks = build_tracks(samples, config=SubtitleCleanupConfig(), total_samples=4)
    masks, reason = plan_masks(
        tracks, config=SubtitleCleanupConfig(), width=1080, height=1920
    )
    assert masks == []
    assert reason in {"mask_geometry_guard", "no_stable_subtitle_track"}


def test_time_bounded_cleanup_filter() -> None:
    mask = CleanupMask(
        x1=0.2,
        y1=0.8,
        x2=0.8,
        y2=0.9,
        active_intervals=[[1.0, 2.5], [3.0, 4.0]],
    )
    filter_text = FFmpegDelogoEngine.build_delogo_filter(
        mask, width=1080, height=1920, time_scoped=True
    )
    assert "enable='between(t,1.000,2.500)+between(t,3.000,4.000)'" in filter_text
    assert "band=" not in filter_text
    whole = FFmpegDelogoEngine.build_delogo_filter(
        mask, width=1080, height=1920, time_scoped=False
    )
    assert "enable=" not in whole
    args = FFmpegDelogoEngine.build_args(
        Path("in.mp4"),
        Path("out.mp4"),
        [mask],
        width=1080,
        height=1920,
        encode_settings=EncodeSettings(),
        time_scoped=True,
    )
    assert "delogo=" in args[args.index("-filter_complex") + 1]
    assert "-map" in args and "0:a?" in args


def test_opencv_inpaint_mask_is_time_scoped() -> None:
    mask = CleanupMask(
        x1=0.25,
        y1=0.75,
        x2=0.75,
        y2=0.90,
        active_intervals=[[1.0, 2.0]],
    )
    inactive = OpenCVInpaintEngine.build_frame_mask(
        [mask], width=100, height=80, timestamp=0.5, time_scoped=True
    )
    active = OpenCVInpaintEngine.build_frame_mask(
        [mask], width=100, height=80, timestamp=1.5, time_scoped=True
    )

    assert int(inactive.sum()) == 0
    assert int(active.sum()) == 255 * 50 * 12
    assert active.shape == (80, 100)


def test_opencv_refines_rectangle_to_bright_text_strokes() -> None:
    import numpy as np

    frame = np.full((80, 100, 3), 80, dtype=np.uint8)
    region = np.zeros((80, 100), dtype=np.uint8)
    region[50:70, 10:90] = 255
    # Subtitle-like narrow bright glyphs inside a much wider OCR envelope.
    frame[54:66, 30:33] = 245
    frame[54:66, 42:45] = 245

    refined = OpenCVInpaintEngine.refine_text_mask(frame, region)

    assert refined[58, 31] == 255
    assert refined[58, 43] == 255
    assert refined[58, 28] == 255  # anti-aliased/outlined edge padding
    assert refined[58, 15] == 0
    assert 0 < int(np.count_nonzero(refined)) < int(np.count_nonzero(region)) // 3


def test_service_selects_opencv_inpaint_engine(settings) -> None:
    config = settings.subtitle_cleanup.model_copy(deep=True)
    config.engine = "opencv_inpaint"
    config.inpaint_radius = 4.5
    service = SubtitleCleanupService(build_library(settings), settings, config=config)

    engine = service._engine()

    assert isinstance(engine, OpenCVInpaintEngine)
    assert engine.radius == 4.5


def test_sample_timestamps_are_bounded() -> None:
    config = SubtitleCleanupConfig(sample_fps=2.0, max_samples=240)
    stamps = sample_timestamps(4.5, config=config)
    assert stamps[:3] == [0.0, 0.5, 1.0]
    assert max(stamps) < 4.5
    assert len(stamps) == 9


# ---------------------------------------------------------------------------
# Quality guard
# ---------------------------------------------------------------------------
def _info(duration: float = 6.0, width: int = 64, height: int = 64) -> MediaInfo:
    return MediaInfo(
        duration=duration,
        width=width,
        height=height,
        fps=30.0,
        codec="h264",
        has_audio=True,
    )


def test_quality_guard_passes_identical_frames(tmp_path: Path) -> None:
    before = [_textured(tmp_path / f"before_{i}.jpg", seed=i) for i in range(2)]
    after = [_textured(tmp_path / f"after_{i}.jpg", seed=i) for i in range(2)]
    mask = CleanupMask(x1=0.2, y1=0.8, x2=0.8, y2=0.9)
    ok, metrics = evaluate_quality_guard(
        before,
        after,
        [mask],
        config=SubtitleCleanupConfig(),
        before_info=_info(),
        after_info=_info(),
    )
    assert ok
    assert metrics["passed"]
    assert metrics["outside_mean_diff_max"] <= 12.0


def test_quality_guard_detects_black_patch(tmp_path: Path) -> None:
    before = [_textured(tmp_path / "b.jpg", seed=1)]
    after_path = tmp_path / "a.jpg"
    _textured(after_path, seed=1)
    image = Image.open(after_path).convert("L")
    ImageDraw.Draw(image).rectangle([10, 50, 55, 60], fill=0)
    image.save(after_path)
    mask = CleanupMask(x1=0.2, y1=0.78, x2=0.8, y2=0.92)
    ok, metrics = evaluate_quality_guard(
        before,
        [after_path],
        [mask],
        config=SubtitleCleanupConfig(),
        before_info=_info(),
        after_info=_info(),
    )
    assert not ok
    assert "solid/black" in " ".join(metrics["checks"])


def test_quality_guard_detects_gross_blur(tmp_path: Path) -> None:
    before = [_textured(tmp_path / "b.jpg", seed=2)]
    after_path = tmp_path / "a.jpg"
    _textured(after_path, seed=2)
    image = Image.open(after_path).convert("L")
    ImageDraw.Draw(image).rectangle([10, 50, 55, 60], fill=128)
    image.save(after_path)
    mask = CleanupMask(x1=0.2, y1=0.78, x2=0.8, y2=0.92)
    ok, metrics = evaluate_quality_guard(
        before,
        [after_path],
        [mask],
        config=SubtitleCleanupConfig(),
        before_info=_info(),
        after_info=_info(),
    )
    assert not ok
    assert metrics["blur_regions"] >= 1 or "solid/black" in " ".join(metrics["checks"])


def test_quality_guard_detects_directional_inpaint_smear(tmp_path: Path) -> None:
    before_path = _textured(tmp_path / "b.jpg", seed=12)
    after_path = tmp_path / "a.jpg"
    _textured(after_path, seed=12)
    image = Image.open(after_path).convert("L")
    # Preserve horizontal texture/variance but stretch a single row through
    # the mask, reproducing the visually obvious band seen in real footage.
    strip = image.crop((13, 53, 52, 54)).resize((39, 7))
    image.paste(strip, (13, 53))
    image.save(after_path, quality=95)
    mask = CleanupMask(x1=0.2, y1=0.82, x2=0.8, y2=0.94)

    ok, metrics = evaluate_quality_guard(
        [before_path],
        [after_path],
        [mask],
        config=SubtitleCleanupConfig(),
        before_info=_info(),
        after_info=_info(),
    )

    assert not ok
    assert metrics["smear_regions"] >= 1
    assert "directional inpaint smear" in metrics["checks"]


def test_quality_guard_detects_outside_change(tmp_path: Path) -> None:
    before = [_textured(tmp_path / "b.jpg", seed=3)]
    after = [_textured(tmp_path / "a.jpg", seed=99)]
    mask = CleanupMask(x1=0.2, y1=0.8, x2=0.8, y2=0.9)
    ok, metrics = evaluate_quality_guard(
        before,
        after,
        [mask],
        config=SubtitleCleanupConfig(),
        before_info=_info(),
        after_info=_info(),
    )
    assert not ok
    assert metrics["outside_mean_diff_max"] > 12.0


def test_quality_guard_detects_duration_and_resolution_drift(tmp_path: Path) -> None:
    before = [_textured(tmp_path / "b.jpg", seed=4)]
    after = [_textured(tmp_path / "a.jpg", seed=4)]
    mask = CleanupMask(x1=0.2, y1=0.8, x2=0.8, y2=0.9)
    ok, metrics = evaluate_quality_guard(
        before,
        after,
        [mask],
        config=SubtitleCleanupConfig(),
        before_info=_info(duration=6.0),
        after_info=_info(duration=9.0),
    )
    assert not ok and "duration drift" in " ".join(metrics["checks"])
    ok, metrics = evaluate_quality_guard(
        before,
        after,
        [mask],
        config=SubtitleCleanupConfig(),
        before_info=_info(width=64, height=64),
        after_info=_info(width=128, height=64),
    )
    assert not ok and "resolution drift" in " ".join(metrics["checks"])


def test_evidence_reduction_and_residual_detection() -> None:
    config = SubtitleCleanupConfig()
    mask = CleanupMask(x1=0.2, y1=0.8, x2=0.8, y2=0.9)
    analyzer = SubtitleAnalyzer(
        SubtitleAnalysisSettings(detector=ScriptedDetector(before=[_region()]))
    )
    before = analyzer.build_result(
        [frame_metrics([_region()], zones=analyzer.settings.zones, rules=analyzer.settings.rules, timestamp=0.0)]
    )
    after_clean = analyzer.build_result(
        [frame_metrics([], zones=analyzer.settings.zones, rules=analyzer.settings.rules, timestamp=0.0)]
    )
    after_residual = analyzer.build_result(
        [frame_metrics([_region()], zones=analyzer.settings.zones, rules=analyzer.settings.rules, timestamp=0.0)]
    )
    clean = evaluate_evidence_reduction(before, after_clean, [mask], config=config)
    residual = evaluate_evidence_reduction(before, after_residual, [mask], config=config)
    assert clean["success"]
    assert not residual["success"]
    assert residual["residual_count"] >= 1


# ---------------------------------------------------------------------------
# Service end-to-end (no cloud, no real FFmpeg)
# ---------------------------------------------------------------------------
def test_cleanup_success_end_to_end(settings) -> None:
    service, _detector, _toolkit, engine = _service(settings)
    clip_id = _add_clip(service.library, settings.paths.library_root, index=10)
    before_sha = hashlib.sha256(Path(service.library.get_clip(clip_id).file_path).read_bytes()).hexdigest()
    outcome = run(service.cleanup_clip(clip_id))
    assert outcome.status is CleanupStatus.SUCCEEDED, outcome.lines()
    assert outcome.output_path is not None and Path(outcome.output_path).exists()
    assert Path(outcome.output_path).name.endswith("__subtitle_cleanup_v1.mp4")
    assert Path(outcome.output_path).parent.name == "clean"
    after_sha = hashlib.sha256(Path(service.library.get_clip(clip_id).file_path).read_bytes()).hexdigest()
    assert before_sha == after_sha
    record = service.library.subtitle_cleanup(clip_id)
    assert record is not None
    assert record["clip_id"] == clip_id
    assert record["status"] == "succeeded"
    assert record["output_path"] == str(outcome.output_path)
    assert record["regions"]
    assert len(engine.calls) == 1


def test_cleanup_caches_quality_and_reduction(settings) -> None:
    service, _detector, _toolkit, _engine = _service(settings)
    clip_id = _add_clip(service.library, settings.paths.library_root, index=11)
    outcome = run(service.cleanup_clip(clip_id))
    assert outcome.quality.get("passed") is True
    assert outcome.reduction.get("success") is True
    assert outcome.before is not None and outcome.after is not None
    assert outcome.after.max_text_regions < outcome.before.max_text_regions


def test_idempotent_rerun_and_force_retry(settings) -> None:
    service, _detector, _toolkit, engine = _service(settings)
    clip_id = _add_clip(service.library, settings.paths.library_root, index=12)
    first = run(service.cleanup_clip(clip_id))
    assert first.status is CleanupStatus.SUCCEEDED
    second = run(service.cleanup_clip(clip_id))
    assert second.reused is True
    assert second.status is CleanupStatus.SUCCEEDED
    assert len(engine.calls) == 1
    third = run(service.cleanup_clip(clip_id, force=True))
    assert third.status is CleanupStatus.SUCCEEDED
    assert len(engine.calls) == 2


def test_forced_not_needed_keeps_previous_derivative_referenced(settings) -> None:
    service, _detector, _toolkit, engine = _service(settings)
    clip_id = _add_clip(service.library, settings.paths.library_root, index=23)
    first = run(service.cleanup_clip(clip_id))
    assert first.status is CleanupStatus.SUCCEEDED
    # A later forced measurement classifies the clip as no text at all.
    detector = ScriptedDetector(before=[], after=[])
    analyzer = SubtitleAnalysisSettings(detector=detector, max_frames=6)
    followup = SubtitleCleanupService(
        service.library,
        settings,
        analyzer=SubtitleAnalyzer(analyzer),
        toolkit=_toolkit,
        engine=engine,
    )
    second = run(followup.cleanup_clip(clip_id, force=True))
    assert second.status is CleanupStatus.NOT_NEEDED
    record = service.library.subtitle_cleanup(clip_id)
    assert record is not None
    assert record["output_path"] == str(first.output_path)
    health = service.library.health_report()
    assert str(Path(first.output_path).resolve()) not in health["orphan_media"]


def test_engine_failure_records_failed_processing(settings) -> None:
    engine = FakeEngine(fail=True)
    service, _detector, _toolkit, _engine = _service(settings, engine=engine)
    clip_id = _add_clip(service.library, settings.paths.library_root, index=13)
    outcome = run(service.cleanup_clip(clip_id))
    assert outcome.status is CleanupStatus.FAILED_PROCESSING
    assert "cleanup_engine_failed" in outcome.reason
    record = service.library.subtitle_cleanup(clip_id)
    assert record is not None and record["status"] == "failed_processing"
    assert not Path(outcome.output_path).exists() if outcome.output_path else True


def test_corrupt_derivative_records_failed_processing(settings) -> None:
    toolkit = FakeToolkit(probe_candidate_error=True)
    service, _detector, _toolkit, _engine = _service(settings, toolkit=toolkit)
    clip_id = _add_clip(service.library, settings.paths.library_root, index=14)
    outcome = run(service.cleanup_clip(clip_id))
    assert outcome.status is CleanupStatus.FAILED_PROCESSING
    assert "ffprobe_after_failed" in outcome.reason


def test_duration_drift_records_failed_quality(settings) -> None:
    toolkit = FakeToolkit(after_duration=12.0)
    service, _detector, _toolkit, _engine = _service(settings, toolkit=toolkit)
    clip_id = _add_clip(service.library, settings.paths.library_root, index=15)
    outcome = run(service.cleanup_clip(clip_id))
    assert outcome.status is CleanupStatus.FAILED_QUALITY


def test_resolution_drift_records_failed_quality(settings) -> None:
    toolkit = FakeToolkit(after_width=1920, after_height=1080)
    service, _detector, _toolkit, _engine = _service(settings, toolkit=toolkit)
    clip_id = _add_clip(service.library, settings.paths.library_root, index=16)
    outcome = run(service.cleanup_clip(clip_id))
    assert outcome.status is CleanupStatus.FAILED_QUALITY


def test_residual_subtitle_keeps_original(settings) -> None:
    detector = ScriptedDetector(before=[_region()], after=[_region()])
    service, _detector, _toolkit, _engine = _service(settings, detector=detector)
    clip_id = _add_clip(service.library, settings.paths.library_root, index=17)
    outcome = run(service.cleanup_clip(clip_id))
    assert outcome.status is CleanupStatus.RESIDUAL_SUBTITLE
    assert outcome.reduction["residual_count"] >= 1
    record = service.library.subtitle_cleanup(clip_id)
    assert record is not None and record["status"] == "residual_subtitle"
    assert record["output_path"] in (None, "")


def test_atomic_publish_and_cache_cleanup(settings) -> None:
    service, _detector, _toolkit, _engine = _service(settings)
    clip_id = _add_clip(service.library, settings.paths.library_root, index=18)
    outcome = run(service.cleanup_clip(clip_id))
    assert outcome.status is CleanupStatus.SUCCEEDED
    dest = Path(outcome.output_path)
    assert dest.exists()
    assert list(dest.parent.glob("*.part")) == []
    temp_root = settings.paths.cache_dir / "subtitle_cleanup"
    assert not temp_root.exists() or not any(temp_root.rglob("*"))


def test_preferred_media_path_and_health_report(settings) -> None:
    service, _detector, _toolkit, _engine = _service(settings)
    clip_id = _add_clip(service.library, settings.paths.library_root, index=19)
    clip = service.library.get_clip(clip_id)
    assert service.library.preferred_media_path(clip) == Path(clip.file_path)
    outcome = run(service.cleanup_clip(clip_id))
    assert outcome.status is CleanupStatus.SUCCEEDED
    # M9.3 production policy: pending review keeps the original preferred.
    assert service.library.preferred_media_path(clip) == Path(clip.file_path)
    ok, _message = service.review_cleanup(clip_id, status="approved")
    assert ok
    assert service.library.preferred_media_path(clip) == Path(outcome.output_path)
    health = service.library.health_report()
    assert str(Path(outcome.output_path).resolve()) not in health["orphan_media"]
    assert health["cleanup_outputs_present"] >= 1
    assert health["cleanup_records"] >= 1


def test_cleanup_does_not_change_clip_semantics(settings) -> None:
    service, _detector, _toolkit, _engine = _service(settings)
    clip_id = _add_clip(service.library, settings.paths.library_root, index=20)
    before = service.library.get_clip(clip_id)
    run(service.cleanup_clip(clip_id))
    after = service.library.get_clip(clip_id)
    assert after.subtitle_type == before.subtitle_type
    assert after.overall_score == before.overall_score
    assert after.provenance == before.provenance
    assert str(after.review_status) == str(before.review_status)


def test_cleanup_report_counts(settings) -> None:
    service, _detector, _toolkit, _engine = _service(settings)
    clip_id = _add_clip(service.library, settings.paths.library_root, index=21)
    run(service.cleanup_clip(clip_id))
    report = service.report()
    assert report["total_records"] == 1
    assert report["status_counts"].get("succeeded") == 1
    assert report["successes"][0]["clip_id"] == clip_id
    assert report["successes"][0]["masked_area_ratio"] > 0


def test_success_is_not_influenced_by_quality_or_acceptance(settings) -> None:
    """A cleanup failure is derivative state only (section 24)."""

    engine = FakeEngine(fail=True)
    service, _detector, _toolkit, _engine = _service(settings, engine=engine)
    clip_id = _add_clip(service.library, settings.paths.library_root, index=22)
    before = service.library.get_clip(clip_id)
    run(service.cleanup_clip(clip_id))
    after = service.library.get_clip(clip_id)
    assert after.overall_score == before.overall_score
    assert after.subtitle_type == before.subtitle_type
    assert after.provenance == before.provenance
