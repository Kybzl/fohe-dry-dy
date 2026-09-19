"""``ai_runs`` audit trail, usage statistics and secret handling."""

from __future__ import annotations

import asyncio
import json

from ai.audit import AICallRecord, sanitize_error
from ai.base import AuditContext, PreviewFilterRequest, ProviderTransientError
from ai.gateway import AIGateway
from ai.mock import MockVisionProvider
from core.models import PreviewFrame, TaskRequest
from storage.database import Database
from storage.library import MaterialLibrary


def run(coro):
    return asyncio.run(coro)


def make_request(tmp_path, **audit) -> PreviewFilterRequest:
    from media.placeholder import write_placeholder_jpeg

    path = tmp_path / "f.jpg"
    write_placeholder_jpeg(path, b"x")
    return PreviewFilterRequest(
        material="苹果干",
        query="苹果干",
        platform="local",
        platform_video_id="local_1",
        duration=30.0,
        frames=[PreviewFrame(timestamp=1.0, image_path=path)],
        context={"mock_scenario": "clean"},
        audit=AuditContext(**audit),
    )


def test_sanitize_error_redacts_secrets() -> None:
    raw = (
        "HTTP 401: api_key=sk-abcdef123456 authorization=Bearer abc.def.ghi "
        "token: 'supersecret' cookie=session%3Dxyz"
    )
    cleaned = sanitize_error(raw)
    assert "sk-abcdef123456" not in cleaned
    assert "abc.def.ghi" not in cleaned
    assert "supersecret" not in cleaned
    assert "session%3Dxyz" not in cleaned
    assert "<redacted>" in cleaned


def test_sanitize_error_truncates_long_messages() -> None:
    cleaned = sanitize_error("x" * 5000)
    assert len(cleaned) <= 600
    assert cleaned.endswith("...")


def test_gateway_persists_ai_runs(settings, tmp_path) -> None:
    library = MaterialLibrary(Database(settings.paths.database), settings.paths.library_root)
    library.initialize()
    task_id = library.create_task(TaskRequest(material="苹果干"))
    source_video_id = library.upsert_source_video(
        task_id=task_id,
        platform="local",
        platform_video_id="local_1",
        source_url="file:///x.mp4",
    )

    gateway = AIGateway(
        MockVisionProvider(seed=1),
        max_retries=1,
        timeout=5.0,
        on_call=library.add_ai_run,
    )
    result = run(
        gateway.preview_filter(
            make_request(tmp_path, task_id=task_id, source_video_id=source_video_id)
        )
    )
    assert result is not None and result.accept is True

    runs = library.list_ai_runs(task_id=task_id)
    assert len(runs) == 1
    record = runs[0]
    assert record["provider"] == "mock"
    assert record["operation"] == "preview_filter"
    assert record["prompt_version"] == "preview_filter_v1"
    assert record["status"] == "ok"
    assert record["source_video_id"] == source_video_id
    assert record["input_frame_count"] == 1
    assert record["input_video_duration"] == 30.0
    assert record["latency_ms"] is not None
    assert json.loads(record["result_json"])["accept"] is True
    assert record["error_message"] is None


def test_failed_calls_are_persisted_with_a_reason(settings, tmp_path) -> None:
    library = MaterialLibrary(Database(settings.paths.database), settings.paths.library_root)
    library.initialize()
    task_id = library.create_task(TaskRequest(material="苹果干"))

    class BrokenProvider(MockVisionProvider):
        async def preview_filter(self, request):  # type: ignore[override]
            raise ProviderTransientError("HTTP 503: upstream unavailable")

    gateway = AIGateway(
        BrokenProvider(),
        max_retries=1,
        backoff_seconds=0.0,
        timeout=5.0,
        on_call=library.add_ai_run,
    )
    assert run(gateway.preview_filter(make_request(tmp_path, task_id=task_id))) is None
    runs = library.list_ai_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "transient_error"
    assert runs[0]["error_type"] == "transient_error"
    assert "503" in runs[0]["error_message"]


def test_usage_summary_aggregates_tokens_and_latency(settings) -> None:
    library = MaterialLibrary(Database(settings.paths.database), settings.paths.library_root)
    library.initialize()
    task_id = library.create_task(TaskRequest(material="苹果干"))
    for index, (provider, status, prompt, completion, latency) in enumerate(
        [
            ("qwen", "ok", 100, 20, 900),
            ("qwen", "ok", 200, 30, 1100),
            ("volcano", "transient_error", None, None, 200),
        ]
    ):
        record = AICallRecord(
            provider=provider,
            operation="preview_filter",
            model=f"model-{index}",
            prompt_version="preview_filter_v1",
            task_id=task_id,
        )
        record.prompt_tokens = prompt
        record.completion_tokens = completion
        record.total_tokens = (prompt or 0) + (completion or 0)
        record.finish(status=status, error=ProviderTransientError("boom") if status != "ok" else None)
        record.latency_ms = latency
        library.add_ai_run(record)

    summary = library.ai_usage_summary(task_id=task_id)
    assert summary["ai_calls"] == 3
    assert summary["successful_calls"] == 2
    assert summary["failed_calls"] == 1
    assert summary["prompt_tokens"] == 300
    assert summary["completion_tokens"] == 50
    assert summary["average_latency_ms"] is not None
    assert summary["estimated_cost"] is None
    providers = {item["provider"] for item in summary["providers"]}
    assert providers == {"qwen", "volcano"}


def test_scope_isolation_by_task(settings, tmp_path) -> None:
    library = MaterialLibrary(Database(settings.paths.database), settings.paths.library_root)
    library.initialize()
    first = library.create_task(TaskRequest(material="苹果干"))
    second = library.create_task(TaskRequest(material="香蕉干"))
    gateway = AIGateway(MockVisionProvider(seed=1), max_retries=1, timeout=5.0, on_call=library.add_ai_run)
    run(gateway.preview_filter(make_request(tmp_path, task_id=first)))
    run(gateway.preview_filter(make_request(tmp_path, task_id=second)))
    assert len(library.list_ai_runs(task_id=first)) == 1
    assert len(library.list_ai_runs(task_id=second)) == 1
    assert library.ai_usage_summary(task_id=first)["ai_calls"] == 1
    assert library.ai_usage_summary()["ai_calls"] == 2
