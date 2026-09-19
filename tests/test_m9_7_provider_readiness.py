"""Milestone 9.7 regression tests: provider readiness and circuit break."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai.base import (
    ProviderAccountBlockedError,
    ProviderNotConfiguredError,
    ProviderPermanentError,
    ProviderSchemaError,
    ProviderTransientError,
)
from ai.gateway import AIGateway
from ai.openai_compat import OpenAICompatibleClient
from ai.readiness import (
    FAILURE_ACCOUNT_BLOCKING,
    FAILURE_REQUEST_SPECIFIC,
    FAILURE_TRANSIENT,
    check_provider_readiness,
    classify_provider_failure,
)
from core.models import PipelineResult, TaskStatus
from core.dependencies import build_library
from core.plans import PauseReason, PlanStatus
from tests.test_m9_1_production_hardening import _clip, _plan, _result, run
from core.plan_runner import PlanRunner
from core.models import ProcessStage


class _FakeClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    async def complete(self, *, model: str, **kwargs):
        self.calls.append(model)
        if self.error:
            raise self.error
        return SimpleNamespace(text="OK", total_tokens=1)


class _FakeProvider:
    name = "qwen"
    configured = True

    def __init__(self, *, error: Exception | None = None, models: dict[str, str] | None = None):
        self.client = _FakeClient(error=error)
        self.models = models or {
            "preview_filter": "qwen3-vl-plus-2025-09-23",
            "segment_detection": "qwen3-vl-plus-2025-09-23",
            "clip_tagging": "qwen3-vl-plus-2025-09-23",
        }
        self.closed = False

    def model_for(self, operation: str) -> str:
        return self.models.get(operation, self.models.get("preview_filter", "model"))

    async def aclose(self) -> None:
        self.closed = True


def _settings(settings):
    settings.ai.active_provider = "qwen"
    settings.ai.provider_readiness.enabled = True
    return settings


# ---------------------------------------------------------------------------
# Failure taxonomy
# ---------------------------------------------------------------------------
def test_403_quota_is_account_blocking() -> None:
    exc = OpenAICompatibleClient.classify_status(
        403,
        '{"error":{"message":"Free quota exhausted. AllocationQuota.FreeTierOnly"}}',
    )
    failure = classify_provider_failure(exc)
    assert failure.failure_class == FAILURE_ACCOUNT_BLOCKING
    assert failure.subtype == "quota_exhausted"
    assert failure.retryable is False


def test_403_auth_is_account_blocking() -> None:
    failure = classify_provider_failure(
        OpenAICompatibleClient.classify_status(403, "unauthorized invalid api key")
    )
    assert failure.failure_class == FAILURE_ACCOUNT_BLOCKING
    assert failure.subtype == "authentication_failed"


def test_model_access_denied_is_account_blocking() -> None:
    failure = classify_provider_failure(
        ProviderAccountBlockedError("model access denied", subtype="model_access_denied")
    )
    assert failure.failure_class == FAILURE_ACCOUNT_BLOCKING
    assert failure.subtype == "model_access_denied"


def test_transient_errors_are_not_account_blocking() -> None:
    for exc, subtype in (
        (OpenAICompatibleClient.classify_status(429, "rate limit"), "rate_limited"),
        (OpenAICompatibleClient.classify_status(503, "unavailable"), "provider_5xx"),
        (TimeoutError("timed out"), "provider_timeout"),
    ):
        failure = classify_provider_failure(exc)
        assert failure.failure_class == FAILURE_TRANSIENT
        assert failure.subtype == subtype
        assert failure.retryable is True


def test_schema_error_is_request_specific() -> None:
    failure = classify_provider_failure(ProviderSchemaError("bad schema"))
    assert failure.failure_class == FAILURE_REQUEST_SPECIFIC
    assert failure.subtype == "schema_error"


# ---------------------------------------------------------------------------
# Readiness probe
# ---------------------------------------------------------------------------
def test_readiness_success_uses_dynamic_routing(settings) -> None:
    provider = _FakeProvider(
        models={
            "preview_filter": "preview-model",
            "segment_detection": "analysis-model",
            "clip_tagging": "analysis-model",
        }
    )
    readiness = run(check_provider_readiness(provider, _settings(settings)))
    assert readiness.ready is True
    assert readiness.models["preview_filter"] == "preview-model"
    assert readiness.models["segment_detection"] == "analysis-model"
    # one probe per unique effective model
    assert provider.client.calls == ["preview-model", "analysis-model"]


def test_readiness_quota_block(settings) -> None:
    provider = _FakeProvider(
        error=ProviderAccountBlockedError(
            "Free quota exhausted", subtype="quota_exhausted"
        )
    )
    readiness = run(check_provider_readiness(provider, _settings(settings)))
    assert readiness.ready is False
    assert readiness.failure_class == FAILURE_ACCOUNT_BLOCKING
    assert readiness.subtype == "quota_exhausted"
    assert readiness.reachable is True


def test_readiness_auth_and_transient(settings) -> None:
    auth = run(
        check_provider_readiness(
            _FakeProvider(error=ProviderNotConfiguredError("missing api key")),
            _settings(settings),
        )
    )
    assert auth.failure_class == FAILURE_ACCOUNT_BLOCKING
    assert auth.subtype == "authentication_failed"
    transient = run(
        check_provider_readiness(
            _FakeProvider(error=ProviderTransientError("HTTP 503 unavailable")),
            _settings(settings),
        )
    )
    assert transient.failure_class == FAILURE_TRANSIENT
    assert transient.subtype == "provider_5xx"


def test_readiness_probe_does_not_use_acquisition_gateway(settings) -> None:
    provider = _FakeProvider()
    gateway = AIGateway(provider)
    readiness = run(check_provider_readiness(provider, _settings(settings)))
    assert readiness.ready
    assert gateway.stats.calls == 0
    assert gateway.records == []


def test_readiness_diagnostics_have_no_secret(settings) -> None:
    provider = _FakeProvider(
        error=ProviderPermanentError("api_key=secret-value unauthorized")
    )
    readiness = run(check_provider_readiness(provider, _settings(settings)))
    text = "\n".join(readiness.summary_lines())
    assert "secret-value" not in text
    assert "<redacted>" in text


# ---------------------------------------------------------------------------
# Gateway circuit breaker
# ---------------------------------------------------------------------------
class _BlockingPreviewProvider:
    name = "qwen"
    configured = True

    def __init__(self) -> None:
        self.calls = 0

    def model_for(self, operation: str) -> str:
        return "qwen3-vl-plus-2025-09-23"

    def prompt_version_for(self, operation: str, request) -> str:
        return "v1"

    async def preview_filter(self, request):
        self.calls += 1
        raise ProviderAccountBlockedError(
            "HTTP 403 Free quota exhausted", subtype="quota_exhausted"
        )

    async def detect_segments(self, request):  # pragma: no cover - unused
        raise RuntimeError

    async def tag_clip(self, request):  # pragma: no cover - unused
        raise RuntimeError

    async def aclose(self) -> None:
        return None


def _preview_request():
    from ai.base import PreviewFilterRequest

    return PreviewFilterRequest(material="红薯干", query="红薯烘干")


def test_account_blocker_trips_circuit_and_skips_later_calls() -> None:
    provider = _BlockingPreviewProvider()
    records: list[str] = []
    gateway = AIGateway(provider, on_call=lambda record: records.append(record.status))
    for _ in range(4):
        assert run(gateway.preview_filter(_preview_request())) is None
    assert provider.calls == 1
    assert gateway.stats.circuit_trips == 1
    assert gateway.stats.skipped_circuit == 3
    assert gateway.records[0].status == "account_blocked"
    assert len(records) == 1, "skipped calls must not create fake ai_runs"
    assert gateway.circuit_failure_subtype == "quota_exhausted"


def test_gateway_provider_failure_is_not_content_rejection() -> None:
    provider = _BlockingPreviewProvider()
    gateway = AIGateway(provider)
    assert run(gateway.preview_filter(_preview_request())) is None
    assert gateway.circuit_state()["failure_class"] == FAILURE_ACCOUNT_BLOCKING
    assert gateway.records[0].error_type == "account_blocked"


# ---------------------------------------------------------------------------
# Plan pause / resume semantics
# ---------------------------------------------------------------------------
def _provider_failure_result(clips=None) -> PipelineResult:
    return _result(clips or [], task_id=1).model_copy(
        update={
            "provider_unavailable": True,
            "provider_failure_class": FAILURE_ACCOUNT_BLOCKING,
            "provider_failure_subtype": "quota_exhausted",
            "provider_model": "qwen3-vl-plus-2025-09-23",
            "provider_operation": "preview_filter",
            "provider_detail": "HTTP 403 Free quota exhausted",
        }
    )


def test_plan_pauses_provider_unavailable_without_budget_labels(settings) -> None:
    library = build_library(settings)
    service, plan = _plan(
        settings, library, queries=["红薯烘干设备", "红薯热泵烘干"], candidates=8
    )
    assert service.approve(plan.id).ok

    async def executor(request):
        return _provider_failure_result()

    outcome = run(
        PlanRunner(plan.id, library=library, settings=settings, executor=executor).run()
    )
    assert outcome.pause_reason == PauseReason.PROVIDER_UNAVAILABLE.value
    assert outcome.pause_reason not in (
        PauseReason.CANDIDATE_BUDGET_EXHAUSTED.value,
        PauseReason.QUERY_SPACE_EXHAUSTED.value,
    )
    stored = service.get_plan(plan.id)
    assert stored is not None
    assert stored.pause_reason == PauseReason.PROVIDER_UNAVAILABLE.value
    assert stored.provider_failure_subtype == "quota_exhausted"
    assert stored.provider_model == "qwen3-vl-plus-2025-09-23"
    assert stored.items[0].status.value == "paused"
    assert stored.items[0].progress.queries_attempted == 1


def test_successful_work_before_circuit_is_preserved(settings) -> None:
    library = build_library(settings)
    service, plan = _plan(
        settings, library, queries=["红薯烘干设备", "红薯热泵烘干"], candidates=8
    )
    assert service.approve(plan.id).ok
    seen: list[str] = []

    async def executor(request):
        seen.append(request.query_seed or "")
        if len(seen) == 1:
            return _result([_clip(1, stage=ProcessStage.PREPARATION)], task_id=1)
        return _provider_failure_result()

    outcome = run(
        PlanRunner(plan.id, library=library, settings=settings, executor=executor).run()
    )
    stored = service.get_plan(plan.id)
    assert outcome.pause_reason == PauseReason.PROVIDER_UNAVAILABLE.value
    assert stored is not None and stored.progress.clips_saved == 1
    assert stored.progress.qualifying_clips == 0


def test_resume_preserves_accounting_and_does_not_repeat_completed_query(settings) -> None:
    library = build_library(settings)
    service, plan = _plan(
        settings,
        library,
        queries=["红薯烘干设备", "红薯热泵烘干", "红薯烘干房内部"],
        candidates=8,
    )
    assert service.approve(plan.id).ok
    first_seen: list[str] = []

    async def blocked(request):
        first_seen.append(request.query_seed or "")
        return _provider_failure_result()

    run(PlanRunner(plan.id, library=library, settings=settings, executor=blocked).run())
    paused = service.get_plan(plan.id)
    assert paused is not None and paused.progress.queries_attempted == 1
    executed_before = list(paused.items[0].progress.executed_queries)

    second_seen: list[str] = []

    async def ready(request):
        second_seen.append(request.query_seed or "")
        return _result([_clip(2, stage=ProcessStage.DRYING)], task_id=2)

    outcome = run(
        PlanRunner(plan.id, library=library, settings=settings, executor=ready).run()
    )
    stored = service.get_plan(plan.id)
    assert outcome.qualifying_clips == 1
    assert stored is not None
    assert stored.progress.queries_attempted >= 2
    assert executed_before[0] not in second_seen, "completed query must not repeat"
    assert stored.items[0].progress.qualifying_clips == 1


def test_transient_bounded_retry_does_not_loop_forever() -> None:
    class _TransientProvider:
        name = "qwen"
        configured = True

        def __init__(self) -> None:
            self.calls = 0

        def model_for(self, operation: str) -> str:
            return "model"

        def prompt_version_for(self, operation: str, request) -> str:
            return "v1"

        async def preview_filter(self, request):
            self.calls += 1
            raise ProviderTransientError("HTTP 503 unavailable")

        async def aclose(self) -> None:
            return None

    provider = _TransientProvider()
    gateway = AIGateway(provider, max_retries=2, backoff_seconds=0.0)
    assert run(gateway.preview_filter(_preview_request())) is None
    # provider-level retry is covered by tests/test_qwen_http.py; the gateway
    # itself must not loop forever on a provider exception.
    assert provider.calls == 1
    assert gateway.circuit_open is False
    assert gateway.records[-1].status == "transient_error"
