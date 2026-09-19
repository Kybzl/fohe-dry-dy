"""``AIGateway``: one entry point for every vision call.

Responsibilities:

* call the primary provider and fall back to the secondary provider
* enforce a timeout and bounded retries on every call
* validate the returned payload against the Pydantic schema
* record every attempt in ``AICallRecord`` for the ``ai_runs`` audit table
* never raise into the pipeline -- a failed call returns ``None`` so the caller
  can skip that video or clip and keep processing
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from ai.audit import AICallRecord, classify_error
from ai.base import (
    ClipTaggingRequest,
    PreviewFilterRequest,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderSchemaError,
    SegmentDetectionRequest,
    VisionProvider,
)
from ai.schemas import parse_json_payload
from core.models import ClipTagging, PreviewFilterResult, SegmentDetectionResult

LOGGER = logging.getLogger(__name__)

#: VisionProvider method name -> ``ai_runs.operation``
OPERATION_NAMES: dict[str, str] = {
    "preview_filter": "preview_filter",
    "detect_segments": "segment_detection",
    "tag_clip": "clip_tagging",
}

AuditCallback = Callable[[AICallRecord], None]


@dataclass
class GatewayStats:
    """Lightweight counters, surfaced in the UI status panel."""

    calls: int = 0
    failures: int = 0
    fallbacks: int = 0
    schema_errors: int = 0
    timeouts: int = 0
    skipped_unconfigured: int = 0
    skipped_circuit: int = 0
    circuit_trips: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float | None = None
    per_method: dict[str, int] = field(default_factory=dict)
    per_provider: dict[str, int] = field(default_factory=dict)

    def record(self, method: str) -> None:
        self.calls += 1
        self.per_method[method] = self.per_method.get(method, 0) + 1

    def add_usage(
        self,
        *,
        provider: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        estimated_cost: float | None,
    ) -> None:
        self.per_provider[provider] = self.per_provider.get(provider, 0) + 1
        self.prompt_tokens += int(prompt_tokens or 0)
        self.completion_tokens += int(completion_tokens or 0)
        self.total_tokens += int(total_tokens or 0)
        if estimated_cost is not None:
            self.estimated_cost = (self.estimated_cost or 0.0) + float(estimated_cost)

    def usage_summary(self) -> dict[str, Any]:
        return {
            "ai_calls": self.calls,
            "failures": self.failures,
            "fallbacks": self.fallbacks,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost": self.estimated_cost,
            "per_provider": dict(self.per_provider),
        }


class AIGateway:
    """Retrying, falling back, schema validating front door for vision calls."""

    def __init__(
        self,
        primary: VisionProvider,
        fallback: VisionProvider | None = None,
        *,
        timeout: float = 60.0,
        max_retries: int = 2,
        backoff_seconds: float = 1.5,
        on_call: AuditCallback | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.backoff_seconds = backoff_seconds
        self.stats = GatewayStats()
        self.on_call = on_call
        self.records: list[AICallRecord] = []
        #: most recently emitted audit record (success or failure)
        self.last_record: AICallRecord | None = None
        #: Milestone 9.7: authoritative account-level blocker trips this circuit
        self.circuit_open = False
        self.circuit_failure_class = ""
        self.circuit_failure_subtype = ""
        self.circuit_provider = ""
        self.circuit_model = ""
        self.circuit_operation = ""
        self.circuit_detail = ""
        self.circuit_tripped_at: str = ""

    # -- public API --------------------------------------------------------
    async def preview_filter(self, request: PreviewFilterRequest) -> PreviewFilterResult | None:
        """Return the pre-filter verdict, or ``None`` when every provider failed."""

        return await self._call("preview_filter", request, PreviewFilterResult)

    async def detect_segments(self, request: SegmentDetectionRequest) -> SegmentDetectionResult | None:
        """Return detected usable time ranges, or ``None`` on total failure."""

        return await self._call("detect_segments", request, SegmentDetectionResult)

    async def tag_clip(self, request: ClipTaggingRequest) -> ClipTagging | None:
        """Return structured tags for one clip, or ``None`` on total failure."""

        return await self._call("tag_clip", request, ClipTagging)

    async def aclose(self) -> None:
        for provider in (self.primary, self.fallback):
            if provider is None:
                continue
            try:
                await provider.aclose()
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.debug("closing provider %s failed: %s", provider.name, exc)

    # -- provider circuit (Milestone 9.7) ---------------------------------
    def circuit_state(self) -> dict[str, Any]:
        return {
            "open": self.circuit_open,
            "failure_class": self.circuit_failure_class,
            "failure_subtype": self.circuit_failure_subtype,
            "provider": self.circuit_provider,
            "model": self.circuit_model,
            "operation": self.circuit_operation,
            "detail": self.circuit_detail,
            "tripped_at": self.circuit_tripped_at,
        }

    def reset_circuit(self) -> None:
        self.circuit_open = False
        self.circuit_failure_class = ""
        self.circuit_failure_subtype = ""
        self.circuit_provider = ""
        self.circuit_model = ""
        self.circuit_operation = ""
        self.circuit_detail = ""
        self.circuit_tripped_at = ""

    def _trip_circuit(
        self,
        *,
        failure: Any,
        provider: VisionProvider,
        operation: str,
        detail: str,
    ) -> None:
        self.circuit_open = True
        self.circuit_failure_class = str(failure.failure_class)
        self.circuit_failure_subtype = str(failure.subtype)
        self.circuit_provider = provider.name
        self.circuit_model = provider.model_for(operation)
        self.circuit_operation = operation
        from ai.audit import sanitize_error

        self.circuit_detail = sanitize_error(detail, max_length=300)
        self.circuit_tripped_at = datetime.now(timezone.utc).isoformat()
        self.stats.circuit_trips += 1
        LOGGER.error(
            "provider circuit tripped (%s/%s) provider=%s operation=%s",
            failure.failure_class,
            failure.subtype,
            provider.name,
            operation,
        )

    # -- internals ---------------------------------------------------------
    async def _call(
        self,
        method: str,
        request: BaseModel,
        model: type[BaseModel],
    ) -> Any | None:
        operation = OPERATION_NAMES.get(method, method)
        self.stats.record(operation)
        if self.circuit_open:
            self.stats.skipped_circuit += 1
            LOGGER.info(
                "ai.%s skipped: provider circuit open (%s/%s)",
                operation,
                self.circuit_failure_class,
                self.circuit_failure_subtype,
            )
            return None
        providers: list[tuple[str, VisionProvider]] = [("primary", self.primary)]
        if self.fallback is not None:
            providers.append(("fallback", self.fallback))

        last_error: Exception | None = None
        for role, provider in providers:
            if provider is None:
                continue
            if not self._is_configured(provider):
                self.stats.skipped_unconfigured += 1
                LOGGER.debug("provider %s is not configured; skipping", provider.name)
                continue
            record = self._new_record(provider, operation, request)
            if role == "fallback":
                self.stats.fallbacks += 1
                LOGGER.warning("ai.%s: falling back to provider %s", operation, provider.name)
            try:
                raw = await self._call_provider(provider, method, request, model)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                self._record_failure(record, provider, operation, exc, request)
                continue
            self._record_success(record, provider, operation, request, raw)
            return raw

        if last_error is not None:
            LOGGER.error("ai.%s failed on all providers: %s", operation, last_error)
        else:
            LOGGER.error("ai.%s: no configured provider available", operation)
        return None

    @staticmethod
    def _is_configured(provider: VisionProvider) -> bool:
        """Providers may expose ``configured``; anything else counts as ready."""

        flag = getattr(provider, "configured", None)
        return True if flag is None else bool(flag)

    @staticmethod
    def _audit_ids(request: BaseModel) -> dict[str, int | None]:
        audit = getattr(request, "audit", None)
        return {
            "task_id": getattr(audit, "task_id", None),
            "source_video_id": getattr(audit, "source_video_id", None),
            "clip_id": getattr(audit, "clip_id", None),
            "origin": getattr(audit, "origin", None) or "pipeline",
        }

    def _new_record(
        self,
        provider: VisionProvider,
        operation: str,
        request: BaseModel,
    ) -> AICallRecord:
        frames = getattr(request, "frames", None) or []
        return AICallRecord(
            provider=provider.name,
            operation=operation,
            model=provider.model_for(operation),
            prompt_version=provider.prompt_version_for(operation, request),
            input_frame_count=len(frames),
            input_video_duration=getattr(request, "duration", None),
            **self._audit_ids(request),
        )

    def _record_failure(
        self,
        record: AICallRecord,
        provider: VisionProvider,
        operation: str,
        exc: Exception,
        request: BaseModel,
    ) -> None:
        status = classify_error(exc)
        from ai.readiness import classify_provider_failure

        failure = classify_provider_failure(exc)
        if isinstance(exc, ProviderNotConfiguredError):
            status = "not_configured"
        elif isinstance(exc, ProviderSchemaError):
            status = "schema_error"
            self.stats.schema_errors += 1
        if status == "timeout":
            self.stats.timeouts += 1
        self.stats.failures += 1
        record.finish(status=status, error=exc)
        if failure.account_blocking:
            self._trip_circuit(
                failure=failure,
                provider=provider,
                operation=operation,
                detail=str(exc),
            )
        LOGGER.warning(
            "ai.%s via provider %s failed (%s): %s", operation, provider.name, status, exc
        )
        self._emit(record)

    def _record_success(
        self,
        record: AICallRecord,
        provider: VisionProvider,
        operation: str,
        request: BaseModel,
        payload: Any,
    ) -> None:
        usage = provider.consume_usage()
        if usage is not None:
            record.model = usage.model or record.model
            record.prompt_tokens = usage.prompt_tokens
            record.completion_tokens = usage.completion_tokens
            record.total_tokens = usage.total_tokens
            record.estimated_cost = usage.estimated_cost
            self.stats.add_usage(
                provider=provider.name,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                estimated_cost=usage.estimated_cost,
            )
        else:
            self.stats.per_provider[provider.name] = (
                self.stats.per_provider.get(provider.name, 0) + 1
            )
        record.finish(status="ok")
        record.result_payload(payload)
        self._emit(record)

    def _emit(self, record: AICallRecord) -> None:
        self.records.append(record)
        self.last_record = record
        if self.on_call is None:
            return
        try:
            row_id = self.on_call(record)
            if isinstance(row_id, int):
                # the audit row id lets the caller back-patch ``clip_id`` once
                # the clip row exists (Milestone 3.7, section 16)
                record.row_id = row_id
        except Exception as exc:  # pragma: no cover - audit must never break a task
            LOGGER.debug("audit callback failed: %s", exc)

    async def _call_provider(
        self,
        provider: VisionProvider,
        method: str,
        request: BaseModel,
        model: type[BaseModel],
    ) -> Any | None:
        handler = getattr(provider, method)
        attempt = 0
        while True:
            attempt += 1
            try:
                raw = await asyncio.wait_for(handler(request), timeout=self.timeout)
                return self._coerce(raw, model)
            except asyncio.CancelledError:
                raise
            except (ProviderSchemaError, ValidationError):
                # A schema problem will not fix itself on a retry.
                raise
            except ProviderError:
                raise
            except Exception as exc:
                if attempt >= self.max_retries:
                    raise ProviderError(
                        f"{provider.name}.{method} failed after {attempt} attempt(s): {exc}"
                    ) from exc
                delay = self.backoff_seconds * attempt
                LOGGER.debug(
                    "retrying %s.%s in %.1fs (attempt %s)", provider.name, method, delay, attempt
                )
                await asyncio.sleep(delay)

    @staticmethod
    def _coerce(raw: Any, model: type[BaseModel]) -> BaseModel:
        """Accept a typed model, a dict or a raw JSON string from a provider."""

        if raw is None:
            raise ProviderError("provider returned no payload")
        if isinstance(raw, model):
            return raw
        if isinstance(raw, (dict, str)):
            return parse_json_payload(raw, model)
        if isinstance(raw, BaseModel):
            return model.model_validate(raw.model_dump())
        raise ProviderError(f"unexpected provider payload type: {type(raw)!r}")
