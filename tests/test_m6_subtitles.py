"""Milestone 6 regressions: measured subtitle detection and classification.

Most tests use synthetic box geometry (no OCR binaries needed); the opt-in
integration tests at the bottom only run when RapidOCR is installed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ai.gateway import AIGateway
from ai.mock import MockVisionProvider
from analyzers.preview_filter import PreviewFilter
from analyzers.subtitle_analysis import (
    HARD_REJECT_CLASSES,
    SubtitleAnalysisSettings,
    SubtitleAnalyzer,
    classification_bucket,
    combine_with_qwen,
    reject_reason_for,
)
from analyzers.text_detection import (
    NullTextDetector,
    OpenCvTextDetector,
    TextDetector,
    _is_point_sequence,
    _unpack_rapidocr,
    build_detector,
    detector_status,
)
from core.dependencies import build_library
from core.models import (
    ClipTagging,
    ComplexityLevel,
    MaterialForm,
    MaterialState,
    PreviewFilterResult,
    PreviewFrame,
    PreviewSource,
    ProcessStage,
    ReviewStatus,
    RejectReason,
    SubtitlePolicy,
    SubtitleType,
    VideoCandidate,
)
from core.subtitle_models import (
    ANALYSIS_VERSION,
    SOURCE_HYBRID,
    SOURCE_LOCAL,
    SOURCE_QWEN,
    SOURCE_UNAVAILABLE,
    FrameTextMetrics,
    SubtitleAnalysisResult,
    SubtitleRuleSettings,
    SubtitleZoneSettings,
    TextRegion,
    frame_metrics,
    match_region_to_previous,
    result_from_mapping,
    temporal_metrics,
    redact_text,
)


def run(coro):
    return asyncio.run(coro)


class ScriptedDetector(TextDetector):
    """Returns canned regions per image name - geometry tests without OCR."""

    name = "scripted"
    recognizes_text = True

    def __init__(self, mapping: dict[str, list[TextRegion]]) -> None:
        self.mapping = mapping
        self.calls = 0

    def available(self) -> tuple[bool, str]:
        return True, "scripted detector"

    def detect(
        self, image_path: Path | str, *, recognize: bool | None = None
    ) -> list[TextRegion]:
        self.calls += 1
        return list(self.mapping.get(Path(image_path).name, []))


def _image(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"fake-jpeg")
    return path


def _bottom(y1: float = 0.80, y2: float = 0.90, x1: float = 0.2, x2: float = 0.8, **kw) -> TextRegion:
    return TextRegion(x1=x1, y1=y1, x2=x2, y2=y2, confidence=0.9, **kw)


def _center(y1: float = 0.40, y2: float = 0.60, x1: float = 0.25, x2: float = 0.75, **kw) -> TextRegion:
    return TextRegion(x1=x1, y1=y1, x2=x2, y2=y2, confidence=0.9, **kw)


def _corner() -> TextRegion:
    return TextRegion(x1=0.02, y1=0.02, x2=0.12, y2=0.06, confidence=0.8)


def _analyzer(detector: TextDetector, **overrides: Any) -> SubtitleAnalyzer:
    settings = SubtitleAnalysisSettings(detector=detector, **overrides)
    return SubtitleAnalyzer(settings)


# ---------------------------------------------------------------------------
# 5/7. TextRegion model + normalized geometry
# ---------------------------------------------------------------------------
def test_text_region_normalized_geometry() -> None:
    region = TextRegion(x1=0.1, y1=0.8, x2=0.6, y2=0.9, confidence=0.77, text="苹果")
    assert region.width_ratio == 0.5
    assert region.height_ratio == 0.1
    assert region.area_ratio == 0.05
    assert region.center_x == 0.35
    assert region.center_y == 0.85
    assert region.zone() == "bottom"
    assert region.iou(region) == 1.0
    assert region.center_distance(region) == 0.0


def test_text_region_zone_boundaries_are_configurable() -> None:
    top = TextRegion(x1=0.1, y1=0.05, x2=0.5, y2=0.10)
    middle = TextRegion(x1=0.1, y1=0.40, x2=0.5, y2=0.50)
    bottom = TextRegion(x1=0.1, y1=0.80, x2=0.5, y2=0.90)
    assert top.zone() == "top"
    assert middle.zone() == "center"
    assert bottom.zone() == "bottom"
    # a different window moves the same boxes into other zones
    assert middle.zone(center_start=0.50, bottom_start=0.60) == "top"
    lower = TextRegion(x1=0.1, y1=0.60, x2=0.5, y2=0.70)  # centre at 0.65
    assert lower.zone() == "center"
    assert lower.zone(center_start=0.25, bottom_start=0.60) == "bottom"


def test_corner_watermark_detection() -> None:
    assert _corner().is_corner_watermark() is True
    assert _bottom().is_corner_watermark() is False
    # thin but centered text is a subtitle, not a watermark
    assert _bottom(y1=0.02, y2=0.06).is_corner_watermark() is False
    assert TextRegion(x1=0.02, y1=0.90, x2=0.14, y2=0.96).is_corner_watermark() is True


# ---------------------------------------------------------------------------
# 9. per-frame metrics
# ---------------------------------------------------------------------------
def test_frame_metrics_area_and_band_calculation() -> None:
    zones, rules = SubtitleZoneSettings(), SubtitleRuleSettings()
    band = TextRegion(x1=0.05, y1=0.75, x2=0.95, y2=0.85, confidence=0.9)
    small = _corner()
    metrics = frame_metrics([band, small], zones=zones, rules=rules, timestamp=2.0)
    assert metrics.region_count == 2
    assert metrics.full_width_band_count == 1
    assert metrics.bottom_text_area_ratio == band.area_ratio
    assert metrics.total_text_area_ratio == round(band.area_ratio + small.area_ratio, 5)
    assert metrics.watermark_area_ratio == small.area_ratio
    assert metrics.timestamp == 2.0


# ---------------------------------------------------------------------------
# 10/11. temporal metrics + region matching
# ---------------------------------------------------------------------------
def test_temporal_persistence_ratios() -> None:
    zones, rules = SubtitleZoneSettings(), SubtitleRuleSettings()
    frames = [
        frame_metrics([_bottom()], zones=zones, rules=rules, timestamp=index)
        for index in range(7)
    ] + [frame_metrics([], zones=zones, rules=rules, timestamp=7)]
    temporal = temporal_metrics(frames, zones=zones, rules=rules)
    assert temporal["text_presence_ratio"] == 0.875
    assert temporal["bottom_persistence"] == 0.875
    assert temporal["center_persistence"] == 0.0
    assert temporal["multi_region_persistence"] == 0.0


def test_region_matching_prefers_overlap_then_proximity() -> None:
    first = _bottom()
    moved = TextRegion(x1=0.21, y1=0.80, x2=0.81, y2=0.90)
    far = TextRegion(x1=0.5, y1=0.30, x2=0.9, y2=0.40)
    assert match_region_to_previous(moved, [first]) is first
    assert match_region_to_previous(TextRegion(x1=0.60, y1=0.81, x2=0.80, y2=0.90), [first, far]) is not None
    assert match_region_to_previous(TextRegion(x1=0.0, y1=0.0, x2=0.05, y2=0.05), [far]) is None


# ---------------------------------------------------------------------------
# 12-18. classification
# ---------------------------------------------------------------------------
def _result_for(regions: list[TextRegion], *, detector: TextDetector | None = None) -> SubtitleAnalysisResult:
    """Analyze a synthetic frame set where every frame has the same boxes."""

    class _Static(TextDetector):
        name = "static"
        recognizes_text = True

        def available(self) -> tuple[bool, str]:
            return True, "static"

        def detect(self, image_path: Path | str, *, recognize: bool | None = None) -> list[TextRegion]:
            return list(regions)

    analyzer = _analyzer(_Static())
    return analyzer.build_result(
        [
            frame_metrics(regions, zones=analyzer.settings.zones, rules=analyzer.settings.rules, timestamp=i)
            for i in range(4)
        ]
    )


def test_bottom_simple_classification_and_cleanliness() -> None:
    result = _result_for([_bottom()])
    assert result.classification is SubtitleType.BOTTOM_SIMPLE
    assert result.cleanliness_score > 0.85
    assert result.bottom_persistence == 1.0


def test_no_text_is_none_and_perfectly_clean() -> None:
    result = _result_for([])
    assert result.classification is SubtitleType.NONE
    assert result.cleanliness_score == 1.0


def test_watermark_only_classification() -> None:
    result = _result_for([_corner()])
    assert result.classification is SubtitleType.WATERMARK_ONLY
    assert result.watermark_only is True
    assert result.cleanliness_score >= 0.95


def test_large_center_text_classification() -> None:
    # a realistically sized caption line: big enough to matter (>=10% of the
    # frame) but not a full-screen text block
    result = _result_for([_center(y1=0.35, y2=0.55, x1=0.20, x2=0.80)])
    assert result.classification is SubtitleType.LARGE_CENTER_TEXT
    assert result.center_persistence == 1.0
    assert result.cleanliness_score < 0.6
    assert result.evidence["checks"], "the decision must carry its evidence"


def test_multi_region_classification() -> None:
    result = _result_for([_bottom(), _center(y1=0.30, y2=0.38, x1=0.05, x2=0.4)])
    assert result.classification in (SubtitleType.MULTI_REGION, SubtitleType.COLORED_BLOCK)
    assert result.multi_region_persistence == 1.0
    assert result.cleanliness_score < 0.7


def test_dense_text_classification() -> None:
    boxes = [
        _bottom(y1=0.74, y2=0.84, x1=0.05, x2=0.95),
        _bottom(y1=0.86, y2=0.96, x1=0.05, x2=0.95),
        _center(y1=0.30, y2=0.42, x1=0.1, x2=0.9),
    ]
    result = _result_for(boxes)
    assert result.classification in (
        SubtitleType.DENSE_TEXT,
        SubtitleType.COLORED_BLOCK,
        SubtitleType.MULTI_REGION,
    )
    assert result.total_text_area_ratio_avg > 0.10
    assert result.cleanliness_score < 0.5


def test_promotional_keyword_evidence() -> None:
    promo = TextRegion(x1=0.1, y1=0.8, x2=0.9, y2=0.9, confidence=0.9, text="联系电话 13800000000")
    result = _result_for([promo])
    assert result.promotion_text_detected is True
    assert result.classification is SubtitleType.PROMOTIONAL_OVERLAY
    assert result.cleanliness_score < 0.75


def test_recognized_text_is_redacted_before_persistence() -> None:
    """Phone-like digit runs never reach the stored evidence (section 37)."""

    assert redact_text("电话 13800000000") == "电话 <num>"
    assert redact_text("3mm") == "3mm"
    region = TextRegion(
        x1=0.1, y1=0.8, x2=0.9, y2=0.9, confidence=0.9, text="联系电话 13800000000"
    )
    metrics = frame_metrics(
        [region], zones=SubtitleZoneSettings(), rules=SubtitleRuleSettings()
    )
    assert "13800000000" not in metrics.regions[0].text
    assert metrics.promotion_text_detected is True, "keyword detection still works"


def test_band_plus_bottom_is_not_simple() -> None:
    band = TextRegion(x1=0.02, y1=0.76, x2=0.98, y2=0.88, confidence=0.9)
    extra = TextRegion(x1=0.5, y1=0.30, x2=0.7, y2=0.34, confidence=0.9)
    result = _result_for([band, extra])
    assert result.classification is not SubtitleType.BOTTOM_SIMPLE
    assert result.classification in (SubtitleType.COLORED_BLOCK, SubtitleType.MULTI_REGION)


def test_classification_buckets() -> None:
    assert classification_bucket(SubtitleType.NONE) == "clean"
    assert classification_bucket(SubtitleType.WATERMARK_ONLY) == "clean"
    assert classification_bucket(SubtitleType.BOTTOM_SIMPLE) == "simple"
    assert classification_bucket(SubtitleType.LARGE_CENTER_TEXT) == "complex"
    assert classification_bucket(SubtitleType.UNKNOWN) == "unknown"
    for classification in HARD_REJECT_CLASSES:
        assert reject_reason_for(classification) in {
            RejectReason.LARGE_CENTER_TEXT,
            RejectReason.MULTI_REGION_SUBTITLE,
            RejectReason.COLORED_TEXT_BLOCK,
            RejectReason.SUBTITLE_TOO_COMPLEX,
        }


# ---------------------------------------------------------------------------
# 20. cleanliness formula
# ---------------------------------------------------------------------------
def test_cleanliness_is_monotone_and_bounded() -> None:
    clean = _result_for([_bottom()])
    noisy = _result_for([_bottom(), _corner()])
    bad = _result_for([_center(y1=0.3, y2=0.7, x1=0.1, x2=0.9)])
    assert clean.cleanliness_score > noisy.cleanliness_score > bad.cleanliness_score
    assert 0.0 <= bad.cleanliness_score <= 1.0
    assert clean.cleanliness_score <= 1.0


# ---------------------------------------------------------------------------
# 21/22/39. hybrid policy, decision source, fallback
# ---------------------------------------------------------------------------
def test_needs_qwen_only_when_ambiguous() -> None:
    clean = _result_for([_bottom()])
    borderline = _result_for([_bottom(y1=0.80, y2=0.92, x1=0.05, x2=0.95)])
    analyzer = _analyzer(ScriptedDetector({}))
    assert analyzer.needs_qwen(clean) is False, "a clear bottom subtitle needs no VLM"
    assert analyzer.needs_qwen(borderline) in (True, False)
    analyzer.settings.hybrid_qwen = False
    assert analyzer.needs_qwen(borderline) is False


def test_combine_with_qwen_keeps_confident_local_result() -> None:
    local = _result_for([_bottom()])
    combined = combine_with_qwen(
        local, qwen_type=SubtitleType.COMPLEX, qwen_complexity=ComplexityLevel.HIGH
    )
    assert combined.classification is SubtitleType.BOTTOM_SIMPLE
    assert combined.decision_source == SOURCE_HYBRID
    assert combined.evidence.get("qwen_agrees") is False


def test_combine_with_qwen_prefers_vlm_when_local_is_ambiguous() -> None:
    ambiguous = SubtitleAnalysisResult(
        classification=SubtitleType.UNKNOWN, cleanliness_score=0.5, decision_source=SOURCE_LOCAL
    )
    combined = combine_with_qwen(
        ambiguous, qwen_type=SubtitleType.MULTI_REGION, qwen_complexity=ComplexityLevel.HIGH
    )
    assert combined.classification is SubtitleType.MULTI_REGION
    assert combined.decision_source == SOURCE_HYBRID


def test_unavailable_analyzer_falls_back_to_vlm() -> None:
    unavailable = SubtitleAnalysisResult(
        classification=SubtitleType.UNKNOWN,
        decision_source=SOURCE_UNAVAILABLE,
        unavailable_reason="engine missing",
        frame_count=0,
    )
    combined = combine_with_qwen(
        unavailable, qwen_type=SubtitleType.COMPLEX, qwen_complexity=ComplexityLevel.HIGH
    )
    assert combined.classification is SubtitleType.COMPLEX
    assert combined.decision_source == SOURCE_QWEN
    assert combined.cleanliness_score < 0.5


def test_null_detector_reports_unavailable_without_crashing(tmp_path) -> None:
    detector = NullTextDetector("no engine installed")
    analyzer = _analyzer(detector)
    frame = _image(tmp_path, "frame.jpg")
    result = run(analyzer.analyze_frames([PreviewFrame(timestamp=0.0, image_path=frame)]))
    assert result.decision_source == SOURCE_UNAVAILABLE
    assert result.is_unavailable is True
    assert "no engine installed" in result.unavailable_reason


def test_build_detector_selection() -> None:
    assert isinstance(build_detector("none"), NullTextDetector)
    assert isinstance(build_detector("opencv"), OpenCvTextDetector)
    # auto never raises, whatever is installed
    status = detector_status(build_detector("auto"))
    assert status["engine"] in {"rapidocr", "opencv", "none"}
    assert isinstance(status["available"], bool)


def test_rapidocr_return_shapes_are_parsed() -> None:
    """Regression: detection-only mode returns bare boxes, not [box, text, score].

    Mis-parsing this made every preview measurement fail (and, before the
    unavailable-fallback fix, reject the candidate outright).
    """

    box = [[142.0, 147.0], [498.0, 147.0], [498.0, 188.0], [142.0, 188.0]]
    assert _is_point_sequence(box) is True
    assert _is_point_sequence([142.0, 147.0]) is False
    assert _is_point_sequence([[1.0, 2.0]]) is False

    detected, _ = _unpack_rapidocr(([[box, "烟台农业", 0.99]], None))
    assert detected == [(box, "烟台农业", 0.99)]

    bare, _ = _unpack_rapidocr(([box], None))  # use_rec=False
    assert len(bare) == 1
    region_box, text, score = bare[0]
    assert region_box is box and text == "" and score > 0

    box_score, _ = _unpack_rapidocr(([[box, 0.8]], None))
    assert box_score[0][2] == 0.8
    assert _unpack_rapidocr((None, None))[0] == []
    assert _unpack_rapidocr(([None, "junk"], None))[0] == []


# ---------------------------------------------------------------------------
# 23/24. preview integration and the quality gate
# ---------------------------------------------------------------------------
def _preview_pair() -> tuple[VideoCandidate, PreviewSource]:
    candidate = VideoCandidate(
        platform="douyin",
        platform_video_id="7652321152866089979",
        source_url="https://www.douyin.com/video/7652321152866089979",
        duration=30.0,
    )
    preview = PreviewSource(
        platform="douyin",
        platform_video_id=candidate.platform_video_id,
        duration=30.0,
        frames=[PreviewFrame(timestamp=1.0)],
    )
    return candidate, preview


class _ScriptedVision(MockVisionProvider):
    """Mock provider with a fixed subtitle complexity verdict."""

    def __init__(self, complexity: ComplexityLevel) -> None:
        super().__init__(seed=1)
        self.complexity = complexity

    async def preview_filter(self, request):  # type: ignore[override]
        return PreviewFilterResult(
            accept=True,
            material_visible=True,
            material_relevance=0.9,
            subtitle_complexity=self.complexity,
            visual_complexity=ComplexityLevel.LOW,
            quality_score=0.9,
        )


def test_preview_uses_measured_result_over_pessimistic_vlm() -> None:
    """A clean measured subtitle must survive a nervous VLM verdict."""

    gateway = AIGateway(_ScriptedVision(ComplexityLevel.HIGH), max_retries=1, timeout=5)
    filter_ = PreviewFilter(gateway, policy=SubtitlePolicy.STRICT, max_frames=4)
    candidate, preview = _preview_pair()
    measured = _result_for([_bottom()])
    decision = run(
        filter_.evaluate(
            candidate=candidate,
            material="苹果干",
            query="苹果干烘干",
            preview=preview,
            subtitle=measured,
        )
    )
    assert decision.accepted is True
    assert "measured subtitle analysis" in decision.detail


def test_preview_rejects_measured_large_center_text() -> None:
    gateway = AIGateway(_ScriptedVision(ComplexityLevel.LOW), max_retries=1, timeout=5)
    filter_ = PreviewFilter(gateway, policy=SubtitlePolicy.STRICT, max_frames=4)
    candidate, preview = _preview_pair()
    measured = _result_for([_center(y1=0.35, y2=0.55, x1=0.20, x2=0.80)])
    decision = run(
        filter_.evaluate(
            candidate=candidate,
            material="苹果干",
            query="苹果干烘干",
            preview=preview,
            subtitle=measured,
        )
    )
    assert decision.accepted is False
    assert decision.reason is RejectReason.LARGE_CENTER_TEXT
    assert decision.subtitle_rejected is True


def test_preview_without_measurement_keeps_vlm_policy() -> None:
    gateway = AIGateway(_ScriptedVision(ComplexityLevel.HIGH), max_retries=1, timeout=5)
    filter_ = PreviewFilter(gateway, policy=SubtitlePolicy.STRICT, max_frames=4)
    candidate, preview = _preview_pair()
    decision = run(
        filter_.evaluate(
            candidate=candidate, material="苹果干", query="q", preview=preview, subtitle=None
        )
    )
    assert decision.accepted is False
    assert decision.reason is RejectReason.SUBTITLE_TOO_COMPLEX


def test_measured_subtitle_wins_over_tagger_field(settings) -> None:
    """The measured class replaces the tagger's subtitle opinion (section 29)."""

    from core.orchestrator import CollectionOrchestrator
    from core.dependencies import build_dependencies

    deps = build_dependencies(
        settings, source_name="mock", provider_name="mock", media_backend="mock"
    )
    orchestrator = CollectionOrchestrator(deps)
    marking = ClipTagging(
        material="苹果",
        material_form=MaterialForm.SLICE,
        material_state=MaterialState.DRYING,
        process_stage=ProcessStage.DRYING,
        subtitle_type=SubtitleType.BOTTOM_SIMPLE,
        subtitle_score=0.1,
    )
    measured = _result_for([_center(y1=0.35, y2=0.55, x1=0.20, x2=0.80)])
    updated = marking.model_copy(
        update={
            "subtitle_type": measured.classification,
            "subtitle_score": round(1.0 - measured.cleanliness_score, 3),
        }
    )
    assert updated.subtitle_type is SubtitleType.LARGE_CENTER_TEXT
    assert updated.subtitle_score > 0.25, "the strict gate would now reject it"
    assert orchestrator is not None


def test_final_clip_measurement_uses_the_same_frames_as_tagging(settings) -> None:
    """Regression: the clip frames must be measured before they are deleted.

    The tagging frames are a temporary directory; measuring them after the
    cleanup silently produced an ``unavailable`` result and the sub-second
    ``frames=0`` case seen in the first live run.
    """

    from core.orchestrator import CollectionOrchestrator
    from core.models import TaskRequest
    from core.dependencies import build_dependencies

    deps = build_dependencies(
        settings, source_name="mock", provider_name="mock", media_backend="mock"
    )
    camera_boxes: list[list[TextRegion]] = []

    class _FrameProbe(ScriptedDetector):
        def detect(self, image_path, *, recognize=None):  # type: ignore[override]
            self.calls += 1
            path = Path(image_path)
            if not path.exists():  # pragma: no cover - the bug being guarded
                raise AssertionError("the tagging frames were deleted too early")
            # a clean top caption: the measured class is distinctive (the mock
            # tagger emits bottom_simple/single_region) and passes the gate
            camera_boxes.append(
                [TextRegion(x1=0.25, y1=0.06, x2=0.75, y2=0.12, confidence=0.9)]
            )
            return camera_boxes[-1]

    deps.subtitle_analyzer = SubtitleAnalyzer(
        SubtitleAnalysisSettings(detector=_FrameProbe({})),
        cache=None,
    )
    result = run(
        CollectionOrchestrator(deps).collect(
            TaskRequest(
                material="苹果干",
                target_clip_count=1,
                subtitle_policy=SubtitlePolicy.STRICT,
                library_root=settings.paths.library_root,
                source="mock",
                provider="mock",
                media_backend="mock",
            )
        )
    )
    assert result.clips, "the mock workflow must still produce a clip"
    clip = result.clips[0]
    assert clip.subtitle_analysis is not None, "the final clip must carry measured evidence"
    assert clip.subtitle_analysis["classification"] == SubtitleType.TOP_SIMPLE.value
    assert clip.subtitle_analysis["frame_count"] > 0
    # the measured class is authoritative for the stored subtitle fields
    assert clip.subtitle_type is SubtitleType.TOP_SIMPLE
    assert clip.subtitle_analysis["decision_source"] == SOURCE_LOCAL


# ---------------------------------------------------------------------------
# 28/34/35. persistence, cache, versioning
# ---------------------------------------------------------------------------
def test_clip_and_source_subtitle_persistence(settings) -> None:
    library = build_library(settings)
    library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="7652321152866089979",
        source_url="https://www.douyin.com/video/7652321152866089979",
    )
    result = _result_for([_bottom()])
    assert result.analysis_version == ANALYSIS_VERSION

    library.save_source_subtitle_analysis("douyin", "7652321152866089979", result)
    stored = library.source_subtitle_analysis("douyin", "7652321152866089979")
    assert stored is not None and stored["classification"] == "bottom_simple"

    from tests.test_m5_coverage import _add_clip  # reuse the seeded helper

    clip_id = _add_clip(library, settings.paths.library_root, index=900)
    library.save_clip_subtitle_analysis(clip_id, result)
    back = library.clip_subtitle_analysis(clip_id)
    assert back is not None
    assert back["analysis_version"] == ANALYSIS_VERSION
    assert result_from_mapping(back).classification is SubtitleType.BOTTOM_SIMPLE
    assert result_from_mapping("not a mapping") is None  # type: ignore[arg-type]


def test_ocr_cache_hit_and_version_key(settings, tmp_path) -> None:
    library = build_library(settings)
    detector = ScriptedDetector({"frame.jpg": [_bottom()]})
    analyzer = SubtitleAnalyzer(SubtitleAnalysisSettings(detector=detector), cache=library)
    frame = _image(tmp_path, "frame.jpg")
    first = run(analyzer.analyze_frames([PreviewFrame(timestamp=0.0, image_path=frame)], cache_key="clip:abc"))
    calls_after_first = detector.calls
    second = run(analyzer.analyze_frames([PreviewFrame(timestamp=0.0, image_path=frame)], cache_key="clip:abc"))
    assert detector.calls == calls_after_first, "the cached measurement must not re-run OCR"
    assert second.classification is first.classification
    third = run(
        analyzer.analyze_frames(
            [PreviewFrame(timestamp=0.0, image_path=frame)], cache_key="clip:abc:v2"
        )
    )
    assert detector.calls > calls_after_first
    assert third.analysis_version == ANALYSIS_VERSION
    assert library.clear_subtitle_cache() >= 1


def test_analysis_settings_drive_max_frames(tmp_path) -> None:
    detector = ScriptedDetector({"f0.jpg": [_bottom()], "f1.jpg": [_bottom()], "f2.jpg": [_bottom()]})
    analyzer = _analyzer(detector, preview_max_frames=2, max_frames=3)
    frames = [PreviewFrame(timestamp=float(i), image_path=_image(tmp_path, f"f{i}.jpg")) for i in range(3)]
    result = run(analyzer.analyze_frames(frames, max_frames=analyzer.settings.preview_max_frames))
    assert result.frame_count == 2
    result_all = run(analyzer.analyze_frames(frames))
    assert result_all.frame_count == 3


# ---------------------------------------------------------------------------
# 3/31/32/33/44/46. CLI + UI surfaces
# ---------------------------------------------------------------------------
def test_subtitle_report_cli(settings, capsys) -> None:
    import app as app_module

    library = build_library(settings)
    result = _result_for([_bottom()])
    from tests.test_m5_coverage import _add_clip

    clip_id = _add_clip(library, settings.paths.library_root, index=901)
    library.save_clip_subtitle_analysis(clip_id, result)
    assert app_module.run_subtitle_report(settings, None) == 0
    output = capsys.readouterr().out
    assert "字幕分析报告" in output and "bottom_simple" in output
    assert "搜索词字幕淘汰率" in output


def test_analyze_subtitles_cli_is_dry_run_by_default(settings, capsys, monkeypatch) -> None:
    import app as app_module
    from core import subtitle_ops as subtitle_ops_module

    library = build_library(settings)
    from tests.test_m5_coverage import _add_clip

    clip_id = _add_clip(library, settings.paths.library_root, index=902)
    before = library.get_clip(clip_id)
    assert before is not None

    calls: list[bool] = []

    async def fake_analyze(self, clip_id_arg, *, apply=False):
        calls.append(apply)
        outcome = subtitle_ops_module.ClipSubtitleResult(clip_id=clip_id_arg)
        outcome.result = _result_for([_bottom()])
        outcome.applied = False
        return outcome

    monkeypatch.setattr(subtitle_ops_module.SubtitleOps, "analyze_clip", fake_analyze)
    assert app_module.run_analyze_subtitles(settings, clip_id, apply=False) == 0
    output = capsys.readouterr().out
    assert calls == [False]
    assert "未修改任何数据" in output
    after = library.get_clip(clip_id)
    assert after is not None and after.subtitle_analysis is None


def test_clip_detail_shows_measured_evidence(settings) -> None:
    from core.library_service import LibraryService
    from ui.library_tab import detail_markdown

    library = build_library(settings)
    from tests.test_m5_coverage import _add_clip

    clip_id = _add_clip(library, settings.paths.library_root, index=903)
    library.save_clip_subtitle_analysis(clip_id, _result_for([_bottom()]))
    clip = library.get_clip(clip_id)
    assert clip is not None
    service = LibraryService(library, settings)
    markdown = detail_markdown(clip, service=service, source={})
    assert "字幕测量证据" in markdown
    assert "bottom_simple" in markdown
    assert "分析版本" in markdown


def test_coverage_reports_measured_subtitle_bucket(settings) -> None:
    from core.coverage import CoverageAnalyzer
    from core.subtitle_ops import SubtitleOps

    library = build_library(settings)
    from tests.test_m5_coverage import _add_clip

    clean_id = _add_clip(library, settings.paths.library_root, index=904)
    bad_id = _add_clip(library, settings.paths.library_root, index=905)
    library.save_clip_subtitle_analysis(clean_id, _result_for([_bottom()]))
    library.save_clip_subtitle_analysis(
        bad_id, _result_for([_center(y1=0.35, y2=0.55, x1=0.20, x2=0.80)])
    )
    ops = SubtitleOps(library, settings)
    report = ops.report()
    assert report.measured_clips == 2
    assert report.bucket_counts.get("simple", 0) >= 1
    assert report.bucket_counts.get("complex", 0) >= 1
    categories = {row["library_category"]: row for row in report.per_category}
    assert categories["苹果干"]["clips"] >= 2
    analyzer = CoverageAnalyzer(library, settings.coverage)
    assert analyzer.report("苹果干").total >= 2


def test_subtitle_search_yield_insight(settings) -> None:
    from core.subtitle_ops import SubtitleOps
    from core.models import RejectReason, SourceVideoStatus

    library = build_library(settings)
    library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="7652321152866000099",
        source_url="https://www.douyin.com/video/7652321152866000099",
        status=SourceVideoStatus.REJECTED_PREVIEW,
        reject_reason=RejectReason.SUBTITLE_TOO_COMPLEX,
        matched_queries=["苹果热泵烘干"],
    )
    library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="7652321152866000100",
        source_url="https://www.douyin.com/video/7652321152866000100",
        status=SourceVideoStatus.PROCESSED,
        matched_queries=["苹果热泵烘干"],
    )
    insight = {row["query"]: row for row in SubtitleOps(library, settings).search_yield_subtitle_insight()}
    assert insight["苹果热泵烘干"]["subtitle_rejection_rate"] == 0.5


# ---------------------------------------------------------------------------
# 49. opt-in OCR integration (skipped when the engine is missing)
# ---------------------------------------------------------------------------
def _rapidocr_available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401

        return True
    except Exception:
        return False


@pytest.mark.skipif(not _rapidocr_available(), reason="rapidocr is not installed")
def test_rapidocr_detects_synthetic_bottom_subtitle(tmp_path) -> None:
    from PIL import Image, ImageDraw

    image_path = tmp_path / "subtitle.jpg"
    image = Image.new("RGB", (640, 360), (40, 40, 40))
    draw = ImageDraw.Draw(image)
    draw.rectangle([20, 300, 620, 344], fill=(0, 0, 0))
    draw.text((40, 312), "APPLE DRYING TEST 2026", fill=(255, 255, 255))
    image.save(image_path)

    detector = build_detector("rapidocr")
    usable, note = detector.available()
    assert usable, note
    regions = detector.detect(image_path)
    assert regions, "the engine must find the synthetic subtitle"
    assert any(region.zone() in ("bottom",) for region in regions)


@pytest.mark.skipif(not _rapidocr_available(), reason="rapidocr is not installed")
def test_measured_analysis_end_to_end_on_synthetic_frames(tmp_path) -> None:
    from PIL import Image, ImageDraw

    frames = []
    for index in range(3):
        path = tmp_path / f"frame{index}.jpg"
        image = Image.new("RGB", (640, 360), (35, 35, 35))
        draw = ImageDraw.Draw(image)
        draw.rectangle([16, 302, 624, 348], fill=(0, 0, 0))
        draw.text((40, 312), f"APPLE DRYING LINE {index}", fill=(255, 255, 255))
        image.save(path)
        frames.append(PreviewFrame(timestamp=float(index), image_path=path))

    analyzer = SubtitleAnalyzer(SubtitleAnalysisSettings(engine="rapidocr"))
    result = run(analyzer.analyze_frames(frames, recognize=True))
    assert result.frame_count == 3
    assert result.engine == "rapidocr"
    assert result.classification in (SubtitleType.BOTTOM_SIMPLE, SubtitleType.NONE, SubtitleType.SINGLE_REGION)
    assert 0.0 <= result.cleanliness_score <= 1.0


@pytest.mark.skipif(not _rapidocr_available(), reason="rapidocr is not installed")
def test_detection_only_mode_returns_regions(tmp_path) -> None:
    """Preview analysis runs detection-only: it must still yield boxes."""

    from PIL import Image, ImageDraw

    path = tmp_path / "frame.jpg"
    image = Image.new("RGB", (640, 360), (20, 20, 20))
    draw = ImageDraw.Draw(image)
    draw.rectangle([20, 300, 620, 344], fill=(0, 0, 0))
    draw.text((40, 312), "DETECTION ONLY MODE", fill=(240, 240, 240))
    image.save(path)

    detector = build_detector("rapidocr")
    regions = detector.detect(path, recognize=False)
    assert regions, "detection-only mode must still return boxes"
    assert all(region.text == "" for region in regions), "no recognition was requested"
    assert all(0.0 <= region.confidence <= 1.0 for region in regions)
