"""Real Qwen provider against a mocked HTTP transport (no paid calls)."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai.base import (
    ClipTaggingRequest,
    PreviewFilterRequest,
    ProviderPermanentError,
    ProviderSchemaError,
    ProviderTransientError,
    SegmentDetectionRequest,
)
from ai.gateway import AIGateway
from ai.openai_compat import OpenAICompatibleClient
from ai.qwen import QwenProvider
from media.placeholder import write_placeholder_jpeg


def run(coro):
    return asyncio.run(coro)


class FakeTransport:
    """Records requests and answers with scripted responses."""

    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        response = self.responses[index]
        if isinstance(response, Exception):
            raise response
        payload = dict(response)
        status = payload.pop("__status__", 200)
        return httpx.Response(status, json=payload)

    def client_factory(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), timeout=5.0)


def completion(payload: Any, *, tokens: int = 42, model: str = "qwen3-vl-flash") -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {
            "prompt_tokens": tokens - 10,
            "completion_tokens": 10,
            "total_tokens": tokens,
        },
    }


def preview_result(accept: bool = True, relevance: float = 0.92) -> dict:
    return {
        "accept": accept,
        "material_visible": True,
        "material_relevance": relevance,
        "subtitle_complexity": "low",
        "visual_complexity": "low",
        "quality_score": 0.88,
        "reject_reason": None,
    }


def frames(tmp_path: Path, count: int = 2) -> list:
    from core.models import PreviewFrame

    out = []
    for index in range(count):
        path = tmp_path / f"frame_{index}.jpg"
        write_placeholder_jpeg(path, f"frame-{index}".encode())
        out.append(PreviewFrame(timestamp=float(index * 5), image_path=path))
    return out


def preview_request(tmp_path: Path, count: int = 2) -> PreviewFilterRequest:
    return PreviewFilterRequest(
        material="苹果干",
        query="苹果干",
        platform="local",
        platform_video_id="local_abc",
        title="苹果烘干",
        duration=30.0,
        frames=frames(tmp_path, count),
    )


def make_provider(transport: FakeTransport, **kwargs) -> QwenProvider:
    payload = {
        "api_key": "test-key-not-real",
        "base_url": "https://example.invalid/compatible-mode/v1",
        "preview_model": "qwen3-vl-flash",
        "analysis_model": "qwen3-vl-plus",
        "timeout": 5.0,
        "max_retries": 2,
        "backoff_seconds": 0.0,
        "client_factory": transport.client_factory,
    }
    payload.update(kwargs)
    return QwenProvider(**payload)


# ---------------------------------------------------------------------------
# request construction
# ---------------------------------------------------------------------------
def test_request_construction_uses_config_models_and_images(tmp_path: Path) -> None:
    transport = FakeTransport([completion(preview_result())])
    provider = make_provider(transport)
    result = run(provider.preview_filter(preview_request(tmp_path)))
    assert result["accept"] is True

    request = transport.requests[0]
    assert str(request.url) == "https://example.invalid/compatible-mode/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-key-not-real"
    body = json.loads(request.content.decode("utf-8"))
    assert body["model"] == "qwen3-vl-flash"
    assert body["response_format"] == {"type": "json_object"}
    assert body["enable_thinking"] is False
    content = body["messages"][-1]["content"]
    image_parts = [part for part in content if part["type"] == "image_url"]
    assert len(image_parts) == 2
    data_url = image_parts[0]["image_url"]["url"]
    assert data_url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(data_url.split(",", 1)[1])
    text_part = [part for part in content if part["type"] == "text"][0]["text"]
    assert "苹果干" in text_part


def test_analysis_operations_use_the_analysis_model(tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            completion({"segments": []}),
            completion({"material": "苹果", "edit_roles": ["process"]}),
        ]
    )
    provider = make_provider(transport)
    run(
        provider.detect_segments(
            SegmentDetectionRequest(
                material="苹果干",
                duration=30.0,
                frames=frames(tmp_path, 1),
                min_segment_duration=3.0,
                max_segment_duration=15.0,
            )
        )
    )
    run(
        provider.tag_clip(
            ClipTaggingRequest(
                material="苹果干",
                duration=30.0,
                start=4.0,
                end=10.0,
                segment_description="苹果片铺盘",
                frames=frames(tmp_path, 1),
            )
        )
    )
    models = [json.loads(request.content.decode("utf-8"))["model"] for request in transport.requests]
    assert models == ["qwen3-vl-plus", "qwen3-vl-plus"]


def test_segment_prompt_includes_target_process_stage(tmp_path: Path) -> None:
    transport = FakeTransport([completion({"segments": []})])
    provider = make_provider(transport)
    request = SegmentDetectionRequest(
        material="香菇干",
        query="香菇烘干过程",
        duration=30.0,
        frames=frames(tmp_path, 1),
        min_segment_duration=3.0,
        max_segment_duration=15.0,
        target_process_stage="drying",
    )

    run(provider.detect_segments(request))

    body = json.loads(transport.requests[0].content.decode("utf-8"))
    prompt = body["messages"][-1]["content"][-1]["text"]
    assert "目标工序: drying" in prompt
    assert "仅仅把\n  香菇放在托盘或烘干房内，不等于 `drying`" in prompt


def test_missing_frame_files_are_skipped(tmp_path: Path) -> None:
    from core.models import PreviewFrame

    transport = FakeTransport([completion(preview_result())])
    provider = make_provider(transport)
    request = preview_request(tmp_path, count=1)
    request.frames.append(PreviewFrame(timestamp=99.0, image_path=tmp_path / "gone.jpg"))
    run(provider.preview_filter(request))
    body = json.loads(transport.requests[0].content.decode("utf-8"))
    image_parts = [
        part for part in body["messages"][-1]["content"] if part["type"] == "image_url"
    ]
    assert len(image_parts) == 1


def test_max_images_is_enforced(tmp_path: Path) -> None:
    transport = FakeTransport([completion(preview_result())])
    provider = make_provider(transport, max_images=2)
    run(provider.preview_filter(preview_request(tmp_path, count=5)))
    body = json.loads(transport.requests[0].content.decode("utf-8"))
    image_parts = [
        part for part in body["messages"][-1]["content"] if part["type"] == "image_url"
    ]
    assert len(image_parts) == 2


# ---------------------------------------------------------------------------
# JSON parsing / schema validation
# ---------------------------------------------------------------------------
def test_fenced_json_is_parsed(tmp_path: Path) -> None:
    text = "```json\n" + json.dumps(preview_result()) + "\n```"
    transport = FakeTransport([completion(text)])
    provider = make_provider(transport)
    assert run(provider.preview_filter(preview_request(tmp_path)))["accept"] is True


def test_invalid_json_raises_schema_error(tmp_path: Path) -> None:
    transport = FakeTransport([completion("not json at all")])
    provider = make_provider(transport)
    with pytest.raises(ProviderSchemaError):
        run(provider.preview_filter(preview_request(tmp_path)))


def test_malformed_json_is_repaired_once(tmp_path: Path) -> None:
    transport = FakeTransport(
        [completion('"unterminated'), completion(preview_result())]
    )
    provider = make_provider(transport)

    result = run(provider.preview_filter(preview_request(tmp_path)))

    assert result["accept"] is True
    assert len(transport.requests) == 2
    retry_body = json.loads(transport.requests[1].content.decode("utf-8"))
    retry_prompt = retry_body["messages"][-1]["content"][-1]["text"]
    assert "Return exactly one complete JSON object" in retry_prompt


@pytest.mark.parametrize(
    "payload",
    [
        {"usable": False, "description": "", "start": 0, "end": 0},
        {"error": "No valid segments found for the requested process stage."},
    ],
)
def test_segment_explicit_no_result_is_normalized(tmp_path: Path, payload: dict) -> None:
    transport = FakeTransport([completion(payload)])
    provider = make_provider(transport)
    request = SegmentDetectionRequest(
        material="香菇干",
        duration=30.0,
        frames=frames(tmp_path, 1),
        target_process_stage="cutting",
    )

    assert run(provider.detect_segments(request)) == {"segments": []}


def test_ambiguous_invalid_segment_payload_is_not_normalized(tmp_path: Path) -> None:
    transport = FakeTransport([completion({"description": "possibly relevant"})])
    provider = make_provider(transport)
    gateway = AIGateway(provider, max_retries=1, timeout=5.0)
    request = SegmentDetectionRequest(
        material="香菇干",
        duration=30.0,
        frames=frames(tmp_path, 1),
        target_process_stage="cutting",
    )

    assert run(gateway.detect_segments(request)) is None
    assert gateway.stats.schema_errors == 1


def test_gateway_validates_qwen_payload(tmp_path: Path) -> None:
    bad = dict(preview_result())
    bad["material_relevance"] = 7.5
    transport = FakeTransport([completion(bad)])
    provider = make_provider(transport)
    gateway = AIGateway(provider, max_retries=1, timeout=5.0)
    assert run(gateway.preview_filter(preview_request(tmp_path))) is None
    assert gateway.stats.schema_errors == 1
    assert gateway.records[0].status == "schema_error"
    assert gateway.records[0].prompt_version == "preview_filter_v1"


def test_gateway_accepts_valid_qwen_payload_and_records_usage(tmp_path: Path) -> None:
    transport = FakeTransport([completion(preview_result(), tokens=123)])
    provider = make_provider(transport)
    gateway = AIGateway(provider, max_retries=1, timeout=5.0)
    result = run(gateway.preview_filter(preview_request(tmp_path)))
    assert result is not None and result.accept is True
    assert gateway.stats.total_tokens == 123
    record = gateway.records[0]
    assert record.status == "ok"
    assert record.provider == "qwen"
    assert record.model == "qwen3-vl-flash"
    assert record.input_frame_count == 2
    assert record.input_video_duration == 30.0
    assert record.latency_ms is not None
    assert json.loads(record.result_json)["accept"] is True


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------
def test_rate_limit_is_retried_then_succeeds(tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            {"__status__": 429, "error": "slow down"},
            completion(preview_result()),
        ]
    )
    provider = make_provider(transport, max_retries=3)
    assert run(provider.preview_filter(preview_request(tmp_path)))["accept"] is True
    assert len(transport.requests) == 2


def test_server_error_is_retried(tmp_path: Path) -> None:
    transport = FakeTransport([{"__status__": 503, "error": "unavailable"}] * 3)
    provider = make_provider(transport, max_retries=3)
    with pytest.raises(ProviderTransientError):
        run(provider.preview_filter(preview_request(tmp_path)))
    assert len(transport.requests) == 3


def test_client_error_is_not_retried(tmp_path: Path) -> None:
    transport = FakeTransport([{"__status__": 400, "error": "bad request"}] * 3)
    provider = make_provider(transport, max_retries=3)
    with pytest.raises(ProviderPermanentError):
        run(provider.preview_filter(preview_request(tmp_path)))
    assert len(transport.requests) == 1


def test_unauthorized_is_not_retried(tmp_path: Path) -> None:
    transport = FakeTransport([{"__status__": 401, "error": "bad key"}] * 3)
    provider = make_provider(transport, max_retries=3)
    with pytest.raises(ProviderPermanentError):
        run(provider.preview_filter(preview_request(tmp_path)))
    assert len(transport.requests) == 1


def test_timeout_is_marked_as_timeout(tmp_path: Path) -> None:
    transport = FakeTransport([httpx.ConnectTimeout("timed out")] * 2)
    provider = make_provider(transport, max_retries=1, timeout=0.01)
    gateway = AIGateway(provider, max_retries=1, timeout=5.0)
    assert run(gateway.preview_filter(preview_request(tmp_path))) is None
    assert gateway.records[0].status == "timeout"
    assert gateway.stats.timeouts == 1


def test_gateway_falls_back_to_volcano_style_provider(tmp_path: Path) -> None:
    failing_transport = FakeTransport([{"__status__": 500, "error": "boom"}] * 3)
    failing = make_provider(failing_transport, max_retries=2)
    healthy_transport = FakeTransport([completion(preview_result(relevance=0.95))])
    healthy = make_provider(healthy_transport, preview_model="fallback-model")

    gateway = AIGateway(failing, healthy, max_retries=1, timeout=5.0)
    result = run(gateway.preview_filter(preview_request(tmp_path)))
    assert result is not None and result.material_relevance > 0.9
    assert gateway.stats.fallbacks == 1
    assert {record.provider for record in gateway.records} == {"qwen"}
    assert [record.status for record in gateway.records] == ["transient_error", "ok"]
    assert gateway.records[0].error_type == "transient_error"


# ---------------------------------------------------------------------------
# escalation and configuration
# ---------------------------------------------------------------------------
def test_low_confidence_escalates_to_the_fallback_model(tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            completion(preview_result(relevance=0.55), model="qwen3-vl-flash"),
            completion(preview_result(relevance=0.94), model="qwen3-vl-max"),
        ]
    )
    provider = make_provider(
        transport, fallback_model="qwen3-vl-max", confidence_escalation_threshold=0.7
    )
    result = run(provider.preview_filter(preview_request(tmp_path)))
    assert result["material_relevance"] == 0.94
    models = [json.loads(request.content.decode("utf-8"))["model"] for request in transport.requests]
    assert models == ["qwen3-vl-flash", "qwen3-vl-max"]
    usage = provider.consume_usage()
    assert usage is not None and usage.model == "qwen3-vl-max"


def test_confident_answer_is_not_escalated(tmp_path: Path) -> None:
    transport = FakeTransport([completion(preview_result(relevance=0.95))])
    provider = make_provider(transport, fallback_model="qwen3-vl-max")
    run(provider.preview_filter(preview_request(tmp_path)))
    assert len(transport.requests) == 1


def test_provider_without_key_reports_not_configured() -> None:
    provider = QwenProvider(api_key="", base_url="https://example.invalid/v1")
    assert provider.configured is False


def test_client_rejects_missing_configuration() -> None:
    client = OpenAICompatibleClient(api_key="", base_url="")
    assert client.configured is False
    with pytest.raises(Exception):
        run(client.complete(model="m", prompt="p"))
