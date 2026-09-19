"""Milestone 2 analyser behaviour: timestamps, splitting, real scene cuts."""

from __future__ import annotations

from pathlib import Path

import pytest

from analyzers.scene_refiner import PySceneDetectBoundaryDetector, SceneRefiner
from analyzers.video_analyzer import normalize_segments, split_long_segment
from core.models import DetectedSegment


def test_split_respects_scene_boundaries() -> None:
    segment = DetectedSegment(start=0.0, end=40.0, usable=True)
    boundaries = [14.2, 28.4, 40.0]
    chunks = split_long_segment(
        segment,
        min_duration=3.0,
        max_duration=15.0,
        boundaries=boundaries,
        boundary_tolerance=1.5,
    )
    starts = [round(chunk.start, 1) for chunk in chunks]
    ends = [round(chunk.end, 1) for chunk in chunks]
    # cuts land on the detected boundaries instead of blind 15s multiples
    assert starts == [0.0, 14.2, 28.4]
    assert ends == [14.2, 28.4, 40.0]
    assert all(chunk.duration >= 3.0 for chunk in chunks)


def test_split_ignores_boundaries_outside_the_tolerance() -> None:
    segment = DetectedSegment(start=0.0, end=30.0, usable=True)
    chunks = split_long_segment(
        segment,
        min_duration=3.0,
        max_duration=15.0,
        boundaries=[13.0],
        boundary_tolerance=1.5,
    )
    assert [round(chunk.start, 1) for chunk in chunks] == [0.0, 15.0]


def test_split_falls_back_to_even_chunks_without_boundaries() -> None:
    segment = DetectedSegment(start=0.0, end=30.0, usable=True)
    chunks = split_long_segment(
        segment, min_duration=3.0, max_duration=15.0, boundaries=[], boundary_tolerance=1.5
    )
    assert [round(chunk.duration, 1) for chunk in chunks] == [15.0, 15.0]


def test_short_tail_is_merged_instead_of_dropped() -> None:
    segment = DetectedSegment(start=0.0, end=32.0, usable=True)
    chunks = split_long_segment(
        segment, min_duration=3.0, max_duration=15.0, boundaries=[], boundary_tolerance=1.5
    )
    assert len(chunks) == 2
    assert round(chunks[-1].end, 1) == 32.0
    assert chunks[0].duration <= 15.0


def test_timestamps_outside_the_source_are_clamped() -> None:
    # model_construct bypasses validation: the point is that the *analyser*
    # clamps whatever a model returns instead of trusting it.
    negative = DetectedSegment.model_construct(
        start=-5.0, end=6.0, usable=True, description="", material_relevance=0.6
    )
    overflowing = DetectedSegment.model_construct(
        start=25.0, end=99.0, usable=True, description="", material_relevance=0.6
    )
    normalized = normalize_segments(
        [negative, overflowing],
        30.0,
        min_duration=3.0,
        max_duration=15.0,
    )
    assert [round(item.start, 1) for item in normalized] == [0.0, 25.0]
    assert [round(item.end, 1) for item in normalized] == [6.0, 30.0]


def test_nan_and_inverted_ranges_are_rejected() -> None:
    nan = DetectedSegment.model_construct(
        start=float("nan"), end=10.0, usable=True, description="", material_relevance=0.5
    )
    normalized = normalize_segments(
        [nan], 30.0, min_duration=3.0, max_duration=15.0
    )
    assert normalized == []


def test_zero_length_and_missing_frames_are_ignored() -> None:
    assert (
        normalize_segments([], 30.0, min_duration=3.0, max_duration=15.0) == []
    )
    assert (
        normalize_segments(
            [DetectedSegment(start=1.0, end=20.0)], 0.0, min_duration=3.0, max_duration=15.0
        )
        == []
    )


def test_normalize_uses_boundaries_when_splitting() -> None:
    normalized = normalize_segments(
        [DetectedSegment(start=0.0, end=44.0, description="连续画面")],
        44.0,
        min_duration=3.0,
        max_duration=15.0,
        boundaries=[14.5, 29.5, 44.0],
        boundary_tolerance=1.5,
    )
    assert [round(item.start, 1) for item in normalized] == [0.0, 14.5, 29.5]
    assert all(item.duration <= 15.0 for item in normalized)


# ---------------------------------------------------------------------------
# real PySceneDetect
# ---------------------------------------------------------------------------
def test_real_pyscenedetect_finds_the_scene_cuts(real_ffmpeg, sample_video: Path) -> None:
    detector = PySceneDetectBoundaryDetector()
    boundaries = detector.detect(sample_video)
    if not boundaries:
        pytest.skip("PySceneDetect could not analyse the sample video here")
    # the sample video has a hard cut every 6 seconds
    expected = [6.0, 12.0, 18.0, 24.0]
    found = [
        target
        for target in expected
        if any(abs(value - target) <= 1.0 for value in boundaries)
    ]
    assert len(found) >= 3, boundaries


def test_real_refinement_snaps_to_detected_cuts(real_ffmpeg, sample_video: Path) -> None:
    boundaries = PySceneDetectBoundaryDetector().detect(sample_video)
    if not boundaries:
        pytest.skip("PySceneDetect could not analyse the sample video here")
    refiner = SceneRefiner(
        PySceneDetectBoundaryDetector(),
        tolerance_seconds=1.5,
        max_expansion_ratio=0.35,
    )
    segment = DetectedSegment(start=6.4, end=11.7, description="第二段画面")
    timing = refiner.refine(segment, boundaries, duration=30.0)
    assert timing.scene_refined is True
    assert timing.start == pytest.approx(6.0, abs=0.6)
    assert timing.end == pytest.approx(12.0, abs=0.6)
    assert timing.ai_start == 6.4


def test_refinement_never_expands_into_unrelated_content() -> None:
    refiner = SceneRefiner(tolerance_seconds=1.5, max_expansion_ratio=0.35)
    segment = DetectedSegment(start=10.0, end=13.0)
    timing = refiner.refine(segment, [0.0, 30.0], duration=40.0)
    assert timing.start == 10.0
    assert timing.end == 13.0
    assert timing.scene_refined is False
