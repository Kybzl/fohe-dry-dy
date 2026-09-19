"""Local filter, AI pre-filter, segment normalisation, refinement, quality gate."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ai.gateway import AIGateway
from ai.mock import MockVisionProvider
from analyzers.candidate_filter import CandidateFilter
from analyzers.preview_filter import PreviewFilter
from analyzers.quality_gate import QualityGate
from analyzers.scene_refiner import (
    PySceneDetectBoundaryDetector,
    SceneRefiner,
    StaticBoundaryDetector,
)
from analyzers.video_analyzer import VideoAnalyzer, normalize_segments
from core.models import (
    ClipScores,
    ClipTagging,
    DetectedSegment,
    MaterialForm,
    MaterialState,
    PreviewFrame,
    PreviewSource,
    ProcessStage,
    RejectReason,
    SubtitlePolicy,
    SubtitleType,
    VideoCandidate,
)
from media.ffmpeg import MockMediaToolkit
from media.frame_sampler import FrameSampler
from storage.database import Database
from storage.dedup import DeduplicationService
from storage.library import MaterialLibrary


def run(coro):
    return asyncio.run(coro)


def make_candidate(**overrides) -> VideoCandidate:
    payload = {
        "platform": "douyin",
        "platform_video_id": "dy-test-1",
        "source_url": "https://mock.test/1",
        "title": "苹果片烘干全过程",
        "author": "烘干设备老张",
        "duration": 45.0,
        "metadata": {"mock_scenario": "clean"},
    }
    payload.update(overrides)
    return VideoCandidate(**payload)


def make_preview(video_id: str = "dy-test-1", count: int = 3) -> PreviewSource:
    return PreviewSource(
        platform="douyin",
        platform_video_id=video_id,
        duration=45.0,
        frames=[
            PreviewFrame(timestamp=float(index * 5), image_path=Path(f"frame_{index}.jpg"))
            for index in range(count)
        ],
    )


def make_filter(settings) -> CandidateFilter:
    library = MaterialLibrary(Database(settings.paths.database), settings.paths.library_root)
    library.initialize()
    return CandidateFilter(DeduplicationService(library))


# ---------------------------------------------------------------------------
# CandidateFilter
# ---------------------------------------------------------------------------
def test_candidate_filter_accepts_a_plausible_hit(settings) -> None:
    decision = make_filter(settings).evaluate(make_candidate(), material="苹果干")
    assert decision.accepted is True
    assert decision.reason is None


def test_candidate_filter_rejects_missing_url(settings) -> None:
    candidate = make_candidate(source_url="")
    decision = make_filter(settings).evaluate(candidate, material="苹果干")
    assert decision.reason is RejectReason.UNREACHABLE


def test_candidate_filter_rejects_duplicates_within_a_run(settings) -> None:
    candidate_filter = make_filter(settings)
    seen_video_ids = {candidate.dedup_key for candidate in [make_candidate()]}
    seen_urls = {"https://mock.test/1"}
    duplicate_video = candidate_filter.evaluate(
        make_candidate(), material="苹果干", seen_video_ids=seen_video_ids
    )
    assert duplicate_video.reason is RejectReason.DUPLICATE_VIDEO
    duplicate_url = candidate_filter.evaluate(
        make_candidate(platform_video_id="dy-test-2"),
        material="苹果干",
        seen_urls=seen_urls,
    )
    assert duplicate_url.reason is RejectReason.DUPLICATE_URL


def test_candidate_filter_rejects_bad_durations(settings) -> None:
    candidate_filter = make_filter(settings)
    # Milestone 9.1: "the upstream gave no duration" is *duration_unknown*, not
    # "out of range" - the orchestrator probes the media metadata first (only a
    # measured duration may be judged against the range).
    unknown = candidate_filter.evaluate(make_candidate(duration=None), material="苹果干")
    assert unknown.accepted is True
    assert unknown.duration_unknown is True
    assert unknown.reason is not RejectReason.DURATION_OUT_OF_RANGE
    assert candidate_filter.evaluate(
        make_candidate(duration=900.0), material="苹果干"
    ).reason is RejectReason.DURATION_OUT_OF_RANGE
    assert candidate_filter.evaluate(
        make_candidate(duration=2.0), material="苹果干"
    ).reason is RejectReason.DURATION_OUT_OF_RANGE


def test_candidate_filter_rejects_unrelated_titles(settings) -> None:
    candidate = make_candidate(title="手机支架开箱与使用评测")
    decision = make_filter(settings).evaluate(candidate, material="苹果干")
    assert decision.reason is RejectReason.TITLE_IRRELEVANT
    related = make_candidate(title="苹果干加工厂探访")
    assert make_filter(settings).evaluate(related, material="苹果干").accepted is True


def test_candidate_filter_skips_already_processed_videos(settings) -> None:
    library = MaterialLibrary(Database(settings.paths.database), settings.paths.library_root)
    library.initialize()
    from core.models import SourceVideoStatus

    library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="dy-test-1",
        source_url="https://mock.test/1",
        status=SourceVideoStatus.PROCESSED,
    )
    candidate_filter = CandidateFilter(DeduplicationService(library))
    decision = candidate_filter.evaluate(make_candidate(), material="苹果干")
    assert decision.reason is RejectReason.ALREADY_PROCESSED


# ---------------------------------------------------------------------------
# PreviewFilter
# ---------------------------------------------------------------------------
def make_gateway(**provider_kwargs) -> AIGateway:
    return AIGateway(
        MockVisionProvider(seed=7, **provider_kwargs),
        max_retries=1,
        backoff_seconds=0.0,
        timeout=5.0,
    )


def test_preview_filter_accepts_clean_material() -> None:
    preview_filter = PreviewFilter(make_gateway(), policy=SubtitlePolicy.STRICT)
    decision = run(
        preview_filter.evaluate(
            candidate=make_candidate(),
            material="苹果干",
            query="苹果干",
            preview=make_preview(),
        )
    )
    assert decision.accepted is True
    assert decision.result is not None
    assert decision.result.material_relevance > 0.8


def test_preview_filter_rejects_heavy_subtitles() -> None:
    preview_filter = PreviewFilter(make_gateway(), policy=SubtitlePolicy.STRICT)
    candidate = make_candidate(metadata={"mock_scenario": "multi_region_subtitle"})
    decision = run(
        preview_filter.evaluate(
            candidate=candidate, material="苹果干", query="苹果干", preview=make_preview()
        )
    )
    assert decision.accepted is False
    assert decision.reason is RejectReason.MULTI_REGION_SUBTITLE
    assert decision.subtitle_rejected is True


def test_strict_policy_rejects_medium_subtitle_complexity() -> None:
    candidate = make_candidate(metadata={"mock_scenario": "borderline_subtitle"})
    strict = PreviewFilter(make_gateway(), policy=SubtitlePolicy.STRICT)
    strict_decision = run(
        strict.evaluate(
            candidate=candidate, material="苹果干", query="苹果干", preview=make_preview()
        )
    )
    assert strict_decision.accepted is False
    assert strict_decision.reason is RejectReason.SUBTITLE_TOO_COMPLEX
    assert strict_decision.subtitle_rejected is True

    balanced = PreviewFilter(make_gateway(), policy=SubtitlePolicy.BALANCED)
    balanced_decision = run(
        balanced.evaluate(
            candidate=candidate, material="苹果干", query="苹果干", preview=make_preview()
        )
    )
    assert balanced_decision.accepted is True


def test_preview_filter_rejects_missing_material() -> None:
    preview_filter = PreviewFilter(make_gateway(), policy=SubtitlePolicy.STRICT)
    candidate = make_candidate(metadata={"mock_scenario": "no_material"})
    decision = run(
        preview_filter.evaluate(
            candidate=candidate, material="苹果干", query="苹果干", preview=make_preview()
        )
    )
    assert decision.accepted is False
    assert decision.reason is RejectReason.NO_MATERIAL


def test_preview_filter_reports_provider_failure() -> None:
    class BrokenProvider(MockVisionProvider):
        async def preview_filter(self, request):  # type: ignore[override]
            raise RuntimeError("provider exploded")

    gateway = AIGateway(BrokenProvider(), max_retries=1, backoff_seconds=0.0, timeout=5.0)
    preview_filter = PreviewFilter(gateway, policy=SubtitlePolicy.STRICT)
    decision = run(
        preview_filter.evaluate(
            candidate=make_candidate(), material="苹果干", query="苹果干", preview=make_preview()
        )
    )
    assert decision.accepted is False
    assert decision.ai_failed is True
    assert gateway.stats.failures >= 1


# ---------------------------------------------------------------------------
# normalize_segments
# ---------------------------------------------------------------------------
def test_normalize_segments_drops_short_and_clamps_ranges() -> None:
    segments = [
        DetectedSegment(start=1.0, end=2.0, usable=True),
        DetectedSegment(start=5.0, end=12.0, usable=True),
        DetectedSegment(start=40.0, end=60.0, usable=True),
    ]
    normalized = normalize_segments(segments, 45.0, min_duration=3.0, max_duration=15.0)
    assert [round(item.start, 1) for item in normalized] == [5.0, 40.0]
    assert normalized[-1].end == 45.0


def test_normalize_segments_splits_long_ranges() -> None:
    segments = [DetectedSegment(start=0.0, end=40.0, usable=True)]
    normalized = normalize_segments(segments, 40.0, min_duration=3.0, max_duration=15.0)
    assert len(normalized) == 3
    assert all(item.duration <= 15.0 + 1e-6 for item in normalized)
    assert normalized[0].start == 0.0
    assert normalized[-1].end == 40.0


def test_normalize_segments_drops_unusable_and_overlapping() -> None:
    segments = [
        DetectedSegment(start=5.0, end=12.0, usable=False),
        DetectedSegment(start=5.0, end=12.0, usable=True),
        DetectedSegment(start=6.0, end=13.0, usable=True),
        DetectedSegment(start=20.0, end=26.0, usable=True),
    ]
    normalized = normalize_segments(segments, 30.0, min_duration=3.0, max_duration=15.0)
    assert [round(item.start, 1) for item in normalized] == [5.0, 20.0]


def test_normalize_segments_respects_max_segments() -> None:
    segments = [
        DetectedSegment(start=float(index) * 20.0, end=float(index) * 20.0 + 8.0)
        for index in range(10)
    ]
    normalized = normalize_segments(
        segments, 200.0, min_duration=3.0, max_duration=15.0, max_segments=3
    )
    assert len(normalized) == 3


def test_normalize_segments_handles_empty_input() -> None:
    assert normalize_segments([], 30.0, min_duration=3.0, max_duration=15.0) == []
    assert normalize_segments(
        [DetectedSegment(start=1.0, end=9.0)], 0.0, min_duration=3.0, max_duration=15.0
    ) == []


# ---------------------------------------------------------------------------
# SceneRefiner
# ---------------------------------------------------------------------------
def test_scene_refiner_snaps_to_nearby_boundaries() -> None:
    refiner = SceneRefiner(tolerance_seconds=2.0, max_expansion_ratio=0.35)
    segment = DetectedSegment(start=5.2, end=11.8, description="苹果片铺盘")
    timing = refiner.refine(segment, [4.96, 12.04], duration=50.0)
    assert timing.start == 4.96
    assert timing.end == 12.04
    assert timing.scene_refined is True
    assert timing.ai_start == 5.2


def test_scene_refiner_ignores_distant_boundaries() -> None:
    refiner = SceneRefiner(tolerance_seconds=2.0, max_expansion_ratio=0.35)
    segment = DetectedSegment(start=5.2, end=11.8)
    timing = refiner.refine(segment, [1.0, 30.0], duration=50.0)
    assert timing.start == 5.2
    assert timing.end == 11.8
    assert timing.scene_refined is False


def test_scene_refiner_never_expands_beyond_tolerance() -> None:
    refiner = SceneRefiner(tolerance_seconds=1.0, max_expansion_ratio=0.5)
    segment = DetectedSegment(start=5.0, end=15.0)
    timing = refiner.refine(segment, [2.0, 19.0], duration=60.0)
    assert timing.start >= 4.0
    assert timing.end <= 16.0


def test_scene_refiner_clamps_to_video_duration() -> None:
    refiner = SceneRefiner(tolerance_seconds=2.0, max_expansion_ratio=0.35)
    segment = DetectedSegment(start=9.0, end=19.0)
    timing = refiner.refine(segment, [], duration=19.5)
    assert timing.end <= 19.5
    assert timing.start >= 0.0


def test_boundary_detectors_degrade_gracefully(tmp_path: Path) -> None:
    assert StaticBoundaryDetector().detect(tmp_path / "missing.mp4") == []
    assert PySceneDetectBoundaryDetector().detect(tmp_path / "missing.mp4") == []


def test_scene_refiner_boundaries_for_is_async_safe(tmp_path: Path) -> None:
    refiner = SceneRefiner(StaticBoundaryDetector())
    assert run(refiner.boundaries_for(tmp_path / "missing.mp4")) == []


# ---------------------------------------------------------------------------
# QualityGate
# ---------------------------------------------------------------------------
def good_tagging(**overrides) -> ClipTagging:
    scores = ClipScores(
        material_relevance=0.95,
        visual_quality=0.9,
        subtitle_cleanliness=0.88,
        stability=0.9,
        composition=0.85,
    )
    scores.overall = scores.recompute_overall()
    payload = {
        "material": "苹果",
        "material_form": MaterialForm.SLICE,
        "material_state": MaterialState.DRYING,
        "process_stage": ProcessStage.TRAY_ARRANGEMENT,
        "subtitle_type": SubtitleType.BOTTOM_SIMPLE,
        "subtitle_score": 0.1,
        "description": "苹果片铺盘",
        "scores": scores,
    }
    payload.update(overrides)
    return ClipTagging(**payload)


def test_quality_gate_accepts_a_clean_clip() -> None:
    gate = QualityGate(policy=SubtitlePolicy.STRICT)
    assert gate.evaluate(
        tagging=good_tagging(), duration=8.0, required_material="苹果干"
    ).accepted is True


def test_quality_gate_rejects_wrong_observed_material() -> None:
    gate = QualityGate(policy=SubtitlePolicy.STRICT)

    decision = gate.evaluate(
        tagging=good_tagging(material="辣椒"),
        duration=8.0,
        required_material="香菇干",
    )

    assert decision.accepted is False
    assert decision.reason is RejectReason.NO_MATERIAL
    assert "辣椒" in decision.detail and "香菇" in decision.detail


def test_quality_gate_rejects_complex_subtitles() -> None:
    gate = QualityGate(policy=SubtitlePolicy.STRICT)
    decision = gate.evaluate(
        tagging=good_tagging(subtitle_type=SubtitleType.COLORED_BLOCK), duration=8.0
    )
    assert decision.accepted is False
    assert decision.subtitle_rejected is True
    assert decision.reason is RejectReason.SUBTITLE_TOO_COMPLEX


def test_subtitle_score_limit_follows_the_policy() -> None:
    tagging = good_tagging(subtitle_score=0.5)
    strict = QualityGate(policy=SubtitlePolicy.STRICT)
    assert strict.evaluate(tagging=tagging, duration=8.0).accepted is False
    off = QualityGate(policy=SubtitlePolicy.OFF)
    assert off.evaluate(tagging=tagging, duration=8.0).accepted is True
    balanced = QualityGate(policy=SubtitlePolicy.BALANCED)
    assert balanced.evaluate(tagging=tagging, duration=8.0).accepted is False
    balanced_with_limits = QualityGate(
        policy=SubtitlePolicy.BALANCED, subtitle_score_limit={"balanced": 0.6}
    )
    assert balanced_with_limits.evaluate(tagging=tagging, duration=8.0).accepted is True


def test_quality_gate_rejects_low_scores_and_bad_durations() -> None:
    gate = QualityGate(policy=SubtitlePolicy.STRICT)
    weak = good_tagging(scores=ClipScores(material_relevance=0.2, overall=0.3))
    assert gate.evaluate(tagging=weak, duration=8.0).reason is RejectReason.NO_MATERIAL
    low_quality = good_tagging(scores=ClipScores(material_relevance=0.9, visual_quality=0.2))
    assert gate.evaluate(tagging=low_quality, duration=8.0).reason is RejectReason.LOW_QUALITY
    assert gate.evaluate(
        tagging=good_tagging(), duration=1.0, min_duration=3.0
    ).reason is RejectReason.SEGMENT_TOO_SHORT
    assert gate.evaluate(
        tagging=good_tagging(), duration=40.0, max_duration=15.0
    ).reason is RejectReason.QUALITY_GATE


def test_quality_gate_with_policy_keeps_thresholds() -> None:
    gate = QualityGate(policy=SubtitlePolicy.STRICT, min_overall_score=0.9)
    relaxed = gate.with_policy(SubtitlePolicy.BALANCED)
    assert relaxed.policy is SubtitlePolicy.BALANCED
    assert relaxed.min_overall_score == 0.9


# ---------------------------------------------------------------------------
# VideoAnalyzer
# ---------------------------------------------------------------------------
def test_video_analyzer_returns_normalized_ranges(tmp_path: Path) -> None:
    toolkit = MockMediaToolkit()
    sampler = FrameSampler(toolkit, max_frames=8)
    analyzer = VideoAnalyzer(
        make_gateway(),
        sampler,
        frame_count=8,
        min_segment_duration=3.0,
        max_segment_duration=15.0,
        max_segments=6,
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    segments = run(
        analyzer.analyze(
            video_path=video,
            duration=60.0,
            material="苹果干",
            query="苹果干",
            platform="douyin",
            platform_video_id="dy-test-1",
            frames_dir=tmp_path / "frames",
            context={"mock_scenario": "clean"},
        )
    )
    assert segments
    for segment in segments:
        assert segment.duration >= 3.0 - 1e-6
        assert segment.duration <= 15.0 + 1e-6
        assert segment.end <= 60.0


def test_video_analyzer_returns_empty_without_usable_footage(tmp_path: Path) -> None:
    toolkit = MockMediaToolkit()
    analyzer = VideoAnalyzer(make_gateway(), FrameSampler(toolkit))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    segments = run(
        analyzer.analyze(
            video_path=video,
            duration=60.0,
            material="苹果干",
            query="苹果干",
            platform="douyin",
            platform_video_id="dy-test-1",
            context={"mock_scenario": "no_usable_segment"},
        )
    )
    assert segments == []
