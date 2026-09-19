"""Provider readiness probe and failure taxonomy (Milestone 9.7).

The readiness probe deliberately bypasses ``AIGateway``: it is a minimal,
separately identifiable provider check, not an acquisition call, and it must
never inflate preview/segment/tagging token metrics.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable, Sequence

from ai.base import (
    ProviderAccountBlockedError,
    ProviderNotConfiguredError,
    ProviderPermanentError,
    ProviderSchemaError,
    ProviderTransientError,
)
from ai.audit import sanitize_error

LOGGER = logging.getLogger(__name__)

FAILURE_ACCOUNT_BLOCKING = "account_blocking"
FAILURE_TRANSIENT = "transient_provider"
FAILURE_REQUEST_SPECIFIC = "request_specific"
FAILURE_NONE = ""

READINESS_PROMPT = "Provider readiness probe. Reply with OK. Do not analyze images."


@dataclass
class ProviderFailure:
    failure_class: str
    subtype: str = ""
    retryable: bool = False
    message: str = ""
    status_code: int | None = None

    @property
    def account_blocking(self) -> bool:
        return self.failure_class == FAILURE_ACCOUNT_BLOCKING


def _status_from_message(text: str) -> int | None:
    lowered = text.lower()
    for status in (400, 401, 403, 404, 408, 409, 425, 429, 500, 502, 503, 504):
        if f"http {status}" in lowered or f"status {status}" in lowered:
            return status
    return None


def classify_provider_failure(exc: BaseException) -> ProviderFailure:
    """Map one provider exception onto the M9.7 failure taxonomy."""

    text = str(exc)
    lowered = text.lower()
    status = _status_from_message(text)
    if isinstance(exc, ProviderAccountBlockedError):
        return ProviderFailure(
            FAILURE_ACCOUNT_BLOCKING,
            subtype=getattr(exc, "subtype", "account_blocked"),
            retryable=False,
            message=text,
            status_code=status,
        )
    if isinstance(exc, ProviderNotConfiguredError):
        return ProviderFailure(
            FAILURE_ACCOUNT_BLOCKING,
            subtype="authentication_failed",
            retryable=False,
            message=text,
            status_code=status,
        )
    if isinstance(exc, ProviderSchemaError):
        return ProviderFailure(
            FAILURE_REQUEST_SPECIFIC,
            subtype="schema_error",
            retryable=False,
            message=text,
            status_code=status,
        )
    if isinstance(exc, ProviderTransientError) or isinstance(exc, TimeoutError):
        subtype = "provider_timeout" if "timeout" in lowered or "timed out" in lowered else (
            "rate_limited" if "429" in lowered or "rate limit" in lowered else (
                "provider_5xx" if status is not None and status >= 500 else "transient_error"
            )
        )
        return ProviderFailure(
            FAILURE_TRANSIENT,
            subtype=subtype,
            retryable=True,
            message=text,
            status_code=status,
        )
    if isinstance(exc, ProviderPermanentError):
        if any(
            token in lowered
            for token in ("quota", "free tier", "allocationquota", "insufficient balance")
        ):
            return ProviderFailure(
                FAILURE_ACCOUNT_BLOCKING,
                subtype="quota_exhausted",
                message=text,
                status_code=status,
            )
        if any(
            token in lowered
            for token in ("401", "403", "unauthorized", "authentication", "bad key", "invalid api key")
        ):
            return ProviderFailure(
                FAILURE_ACCOUNT_BLOCKING,
                subtype="authentication_failed",
                message=text,
                status_code=status,
            )
        if any(
            token in lowered
            for token in ("model not found", "model does not exist", "access denied", "permission")
        ):
            return ProviderFailure(
                FAILURE_ACCOUNT_BLOCKING,
                subtype="model_access_denied",
                message=text,
                status_code=status,
            )
        return ProviderFailure(
            FAILURE_REQUEST_SPECIFIC,
            subtype="request_error",
            message=text,
            status_code=status,
        )

    # Generic transport/HTTP exceptions: inspect the message deterministically.
    if any(token in lowered for token in ("quota", "free tier", "allocationquota")):
        return ProviderFailure(
            FAILURE_ACCOUNT_BLOCKING, subtype="quota_exhausted", message=text, status_code=status
        )
    if any(
        token in lowered
        for token in ("401", "403", "unauthorized", "authentication", "invalid api key")
    ):
        return ProviderFailure(
            FAILURE_ACCOUNT_BLOCKING,
            subtype="authentication_failed",
            message=text,
            status_code=status,
        )
    if status is not None and status >= 500:
        return ProviderFailure(
            FAILURE_TRANSIENT,
            subtype="provider_5xx",
            retryable=True,
            message=text,
            status_code=status,
        )
    if status == 429 or "rate limit" in lowered:
        return ProviderFailure(
            FAILURE_TRANSIENT,
            subtype="rate_limited",
            retryable=True,
            message=text,
            status_code=status,
        )
    if "timeout" in lowered or "timed out" in lowered:
        return ProviderFailure(
            FAILURE_TRANSIENT,
            subtype="provider_timeout",
            retryable=True,
            message=text,
            status_code=status,
        )
    return ProviderFailure(
        FAILURE_REQUEST_SPECIFIC, subtype="unknown", message=text, status_code=status
    )


@dataclass
class ProviderReadiness:
    provider: str
    models: dict[str, str] = field(default_factory=dict)
    reachable: bool = False
    authorized: bool = False
    ready: bool = False
    failure_class: str = FAILURE_NONE
    subtype: str = ""
    detail: str = ""
    latency_ms: int = 0
    probe_count: int = 0
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def summary_lines(self) -> list[str]:
        lines = [
            f"[info] provider: {self.provider}",
            f"[info] effective models: {self.models}",
            f"[info] reachable: {self.reachable} | authorized: {self.authorized} | "
            f"ready: {self.ready}",
            f"[info] latency_ms: {self.latency_ms} | probes: {self.probe_count}",
            f"[info] checked_at: {self.checked_at}",
        ]
        if self.ready:
            lines.append("[ok] provider readiness: ready")
        else:
            lines.append(
                f"[warn] provider readiness: {self.failure_class or 'unavailable'}"
                + (f" / {self.subtype}" if self.subtype else "")
            )
            if self.detail:
                lines.append(f"[warn] detail: {self.detail[:200]}")
        return lines


async def _default_probe(provider: Any, model: str, timeout: float) -> None:
    client = getattr(provider, "client", None)
    if client is None or not hasattr(client, "complete"):
        return None
    return await asyncio.wait_for(
        client.complete(
            model=model,
            prompt=READINESS_PROMPT,
            images=[],
            json_mode=False,
            max_tokens=1,
        ),
        timeout=timeout,
    )


async def check_provider_readiness(
    provider: Any,
    settings: Any,
    *,
    operations: Sequence[str] = ("preview_filter", "segment_detection", "clip_tagging"),
    probe: Callable[[Any, str, float], Awaitable[Any]] | None = None,
) -> ProviderReadiness:
    """Minimal bounded probe of the exact effective production routing."""

    models = {
        operation: str(provider.model_for(operation)) for operation in operations
    }
    readiness = ProviderReadiness(provider=str(getattr(provider, "name", "unknown")), models=models)
    readiness_config = getattr(getattr(settings, "ai", None), "provider_readiness", None)
    enabled = bool(getattr(readiness_config, "enabled", True))
    timeout = float(
        getattr(readiness_config, "timeout_seconds", 20.0)
        or settings.ai.request_timeout_seconds
        or 20.0
    )
    if not enabled:
        readiness.ready = True
        readiness.reachable = True
        readiness.authorized = True
        readiness.detail = "provider_readiness.enabled = false"
        return readiness
    if str(getattr(provider, "name", "")) == "mock":
        readiness.ready = True
        readiness.reachable = True
        readiness.authorized = True
        readiness.detail = "mock provider"
        return readiness
    configured = getattr(provider, "configured", True)
    if not bool(configured):
        failure = classify_provider_failure(
            ProviderNotConfiguredError("provider is not configured")
        )
        readiness.failure_class = failure.failure_class
        readiness.subtype = failure.subtype
        readiness.detail = sanitize_error(failure.message)
        return readiness

    started = time.perf_counter()
    unique_models = list(dict.fromkeys(models.values()))
    probe_fn = probe or _default_probe
    try:
        for model in unique_models:
            await probe_fn(provider, model, timeout)
            readiness.probe_count += 1
    except Exception as exc:
        failure = classify_provider_failure(exc)
        readiness.failure_class = failure.failure_class
        readiness.subtype = failure.subtype
        readiness.detail = sanitize_error(failure.message)
        readiness.reachable = not isinstance(exc, ProviderNotConfiguredError)
        readiness.latency_ms = int((time.perf_counter() - started) * 1000)
        return readiness
    readiness.latency_ms = int((time.perf_counter() - started) * 1000)
    readiness.reachable = True
    readiness.authorized = True
    readiness.ready = True
    return readiness
