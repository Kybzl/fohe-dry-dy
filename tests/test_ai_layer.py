"""Mock provider behaviour, gateway retry/fallback and schema validation."""

from __future__ import annotations

import asyncio

import pytest

from ai.base import (
    ClipTaggingRequest,
    PreviewFilterRequest,
    ProviderError,
    ProviderNotConfiguredError,
    SegmentDetectionRequest,
)
from ai.gateway import AIGateway
from ai.mock import MockVisionProvider
from ai.qwen import QwenProvider
from ai.schemas import extract_json_block, parse_json_payload, render_prompt
from ai.volcano import VolcanoProvider
from core.models import (
    ComplexityLevel,
    EditRole,
    MaterialState,
    PersonRole,
    PreviewFilterResult,
    ProcessStage,
    RejectReason,
    SubtitleType,
)


def run(coro):
    return asyncio.run(coro)


def preview_request(scenario: str, video_id: str = "dy-1") -> PreviewFilterRequest:
    return PreviewFilterRequest(
        material="苹果干",
        query="苹果干",
        platform="douyin",
        platform_video_id=video_id,
        title="苹果烘干实拍",
        duration=48.0,
        context={"mock_scenario": scenario},
    )


def segment_request(scenario: str = "clean", duration: float = 60.0) -> SegmentDetectionRequest:
    return SegmentDetectionRequest(
        material="苹果干",
        query="苹果干",
        platform="douyin",
        platform_video_id="dy-1",
        duration=duration,
        context={"mock_scenario": scenario},
        min_segment_duration=3.0,
        max_segment_duration=15.0,
    )


def tag_request(description: str) -> ClipTaggingRequest:
    return ClipTaggingRequest(
        material="苹果干",
        query="苹果干",
        platform="douyin",
        platform_video_id="dy-1",
        duration=48.0,
        start=5.0,
        end=13.0,
        segment_description=description,
        segment_relevance=0.94,
    )


# ---------------------------------------------------------------------------
# Mock provider
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("scenario", "expected_accept", "expected_reason"),
    [
        ("clean", True, None),
        ("borderline_subtitle", True, None),
        ("multi_region_subtitle", False, RejectReason.MULTI_REGION_SUBTITLE),
        ("colored_text_block", False, RejectReason.COLORED_TEXT_BLOCK),
        ("split_screen", False, RejectReason.SPLIT_SCREEN),
        ("low_quality", False, RejectReason.LOW_QUALITY),
        ("no_material", False, RejectReason.NO_MATERIAL),
    ],
)
def test_mock_preview_verdicts(scenario: str, expected_accept: bool, expected_reason) -> None:
    provider = MockVisionProvider(seed=3)
    result = run(provider.preview_filter(preview_request(scenario)))
    assert result.accept is expected_accept
    assert result.reject_reason is expected_reason


def test_mock_preview_is_deterministic() -> None:
    provider = MockVisionProvider(seed=5)
    first = run(provider.preview_filter(preview_request("clean")))
    second = run(provider.preview_filter(preview_request("clean")))
    assert first == second
    assert first.subtitle_complexity is ComplexityLevel.LOW


def test_mock_segment_detection_returns_usable_ranges() -> None:
    provider = MockVisionProvider(seed=11)
    result = run(provider.detect_segments(segment_request()))
    assert len(result.segments) >= 2
    assert all(segment.end > segment.start for segment in result.segments)
    assert all(segment.end <= 60.0 for segment in result.segments)
    assert any(segment.duration < 3.0 for segment in result.segments), "expects a short range"


def test_mock_segment_detection_can_return_nothing() -> None:
    provider = MockVisionProvider(seed=11)
    result = run(provider.detect_segments(segment_request("no_usable_segment")))
    assert result.segments == []


def test_mock_tagging_infers_stage_and_people() -> None:
    provider = MockVisionProvider(seed=2)
    tagging = run(provider.tag_clip(tag_request("工人从烘干房推出托盘并卸下苹果干")))
    assert tagging.process_stage is ProcessStage.UNLOADING
    assert tagging.material_state is MaterialState.DRIED
    assert tagging.people is True
    assert tagging.people_count == 1
    assert tagging.person_role is PersonRole.WORKER
    assert EditRole.PROCESS in tagging.edit_roles
    assert 0.0 <= tagging.scores.overall <= 1.0


def test_mock_tagging_marks_tray_arrangement() -> None:
    provider = MockVisionProvider(seed=2)
    tagging = run(provider.tag_clip(tag_request("苹果片被均匀铺放在不锈钢烘干托盘上")))
    assert tagging.process_stage is ProcessStage.TRAY_ARRANGEMENT
    assert tagging.equipment_visible is True
    assert tagging.subtitle_type in (SubtitleType.NONE, SubtitleType.BOTTOM_SIMPLE)


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------
class FailingProvider(MockVisionProvider):
    def __init__(self, *, exc: Exception | None = None) -> None:
        super().__init__(seed=1)
        self.calls = 0
        self.exc = exc or RuntimeError("boom")

    async def preview_filter(self, request):  # type: ignore[override]
        self.calls += 1
        raise self.exc

    async def detect_segments(self, request):  # type: ignore[override]
        self.calls += 1
        raise self.exc

    async def tag_clip(self, request):  # type: ignore[override]
        self.calls += 1
        raise self.exc


def test_gateway_falls_back_to_secondary_provider() -> None:
    primary = FailingProvider()
    gateway = AIGateway(
        primary, MockVisionProvider(seed=4), max_retries=1, backoff_seconds=0.0, timeout=5.0
    )
    result = run(gateway.preview_filter(preview_request("clean")))
    assert result is not None and result.accept is True
    assert gateway.stats.fallbacks == 1
    assert gateway.stats.failures == 1
    assert primary.calls == 1


def test_gateway_retries_before_giving_up() -> None:
    primary = FailingProvider()
    gateway = AIGateway(primary, max_retries=3, backoff_seconds=0.0, timeout=5.0)
    assert run(gateway.preview_filter(preview_request("clean"))) is None
    assert primary.calls == 3
    assert gateway.stats.failures == 1


def test_gateway_returns_none_instead_of_raising() -> None:
    gateway = AIGateway(
        FailingProvider(), FailingProvider(), max_retries=1, backoff_seconds=0.0, timeout=5.0
    )
    assert run(gateway.preview_filter(preview_request("clean"))) is None
    assert run(gateway.detect_segments(segment_request())) is None
    assert run(gateway.tag_clip(tag_request("苹果片铺盘"))) is None
    assert gateway.stats.failures == 6


def test_gateway_validates_provider_payloads() -> None:
    class DictProvider(MockVisionProvider):
        async def preview_filter(self, request):  # type: ignore[override]
            return {
                "accept": True,
                "material_visible": True,
                "material_relevance": 0.9,
                "subtitle_complexity": "low",
                "visual_complexity": "low",
                "quality_score": 0.8,
                "reject_reason": None,
            }

        async def tag_clip(self, request):  # type: ignore[override]
            return "```json\n{\"material\": \"苹果\", \"edit_roles\": [\"process\"]}\n```"

    gateway = AIGateway(DictProvider(), max_retries=1, backoff_seconds=0.0, timeout=5.0)
    preview = run(gateway.preview_filter(preview_request("clean")))
    assert isinstance(preview, PreviewFilterResult) and preview.accept is True
    tagging = run(gateway.tag_clip(tag_request("苹果片铺盘")))
    assert tagging is not None and tagging.material == "苹果"
    assert tagging.edit_roles == [EditRole.PROCESS]


def test_gateway_rejects_invalid_payload_without_crashing() -> None:
    class BadProvider(MockVisionProvider):
        async def preview_filter(self, request):  # type: ignore[override]
            return '{"accept": "maybe", "material_relevance": 7}'

    gateway = AIGateway(BadProvider(), max_retries=2, backoff_seconds=0.0, timeout=5.0)
    assert run(gateway.preview_filter(preview_request("clean"))) is None
    assert gateway.stats.schema_errors >= 1


def test_gateway_times_out_slow_providers() -> None:
    class SlowProvider(MockVisionProvider):
        async def preview_filter(self, request):  # type: ignore[override]
            await asyncio.sleep(0.5)
            return await super().preview_filter(request)

    gateway = AIGateway(SlowProvider(), max_retries=1, backoff_seconds=0.0, timeout=0.05)
    assert run(gateway.preview_filter(preview_request("clean"))) is None
    assert gateway.stats.failures == 1


# ---------------------------------------------------------------------------
# Real providers: structure only in Milestone 1
# ---------------------------------------------------------------------------
def test_qwen_provider_without_key_is_not_configured() -> None:
    provider = QwenProvider(api_key="")
    assert provider.configured is False
    with pytest.raises(ProviderNotConfiguredError):
        run(provider.preview_filter(preview_request("clean")))
    gateway = AIGateway(provider, max_retries=1, backoff_seconds=0.0, timeout=5.0)
    assert run(gateway.preview_filter(preview_request("clean"))) is None
    # An unconfigured provider is skipped rather than counted as a failure.
    assert gateway.stats.skipped_unconfigured == 1
    assert gateway.stats.failures == 0


def test_qwen_prompt_contains_material_and_schema() -> None:
    request = preview_request("clean")
    prompt = QwenProvider(api_key="x").build_preview_prompt(request)
    assert "苹果干" in prompt
    assert "JSON Schema" in prompt
    assert "reject_reason" in prompt
    segment_prompt = QwenProvider(api_key="x").build_segment_prompt(segment_request())
    assert "segments" in segment_prompt


def test_volcano_provider_requires_key_and_model() -> None:
    provider = VolcanoProvider(api_key="k", model="")
    assert provider.configured is False
    with pytest.raises(ProviderNotConfiguredError):
        run(provider.tag_clip(tag_request("苹果片铺盘")))
    assert VolcanoProvider(api_key="k", model="m").configured is True


def test_schema_helpers_round_trip() -> None:
    raw = 'noise ```json\n{"segments": []}\n``` tail'
    assert extract_json_block(raw).startswith("{")
    with pytest.raises(ProviderError):
        parse_json_payload('{"accept": 1}', PreviewFilterResult)
    # Milestone 3.7: the tagging prompt separates task intent from the visual
    # verdict, so the intent arrives as ``requested_material``.
    prompt = render_prompt(
        "clip_tagging",
        requested_material="苹果干",
        query="苹果干烘干",
        title="",
        start=1,
        end=2,
        duration=1,
        segment_description="x",
        frame_count=4,
    )
    assert "苹果干" in prompt and "{{" not in prompt
