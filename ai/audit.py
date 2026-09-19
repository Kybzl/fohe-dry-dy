"""AI call audit trail (``ai_runs``) and error sanitisation.

Every provider attempt -- successful or not -- produces one ``AICallRecord``.
Secrets never reach this module: keys are redacted before an error message is
stored, and the request payload itself is never persisted.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

#: substrings that mark a value as secret when it appears in an error message
SENSITIVE_KEYS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "access_token",
    "refresh_token",
    "secret",
    "password",
    "cookie",
)

_KEY_VALUE = re.compile(
    r"(?i)\b(api[-_]?key|authorization|token|secret|password|cookie)\b\s*[:=]\s*"
    r"[\"']?([^\s\"',;)]+)"
)
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]+")
_SK_TOKEN = re.compile(r"\bsk-[A-Za-z0-9._\-]{6,}")


def sanitize_error(message: object, *, max_length: int = 600) -> str:
    """Redact credentials and truncate an error message for persistence."""

    text = str(message or "")
    # Order matters: redact whole "Bearer <token>" values before the generic
    # key=value rule, otherwise the token would survive as the value.
    text = _BEARER.sub("Bearer <redacted>", text)
    text = _SK_TOKEN.sub("sk-<redacted>", text)
    text = _KEY_VALUE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    if len(text) > max_length:
        text = text[: max_length - 3] + "..."
    return text


def classify_error(exc: BaseException) -> str:
    """Short, stable error type used in ``ai_runs.error_type``."""

    from ai.base import (
        ProviderAccountBlockedError,
        ProviderNotConfiguredError,
        ProviderPermanentError,
        ProviderSchemaError,
        ProviderTransientError,
    )

    name = type(exc).__name__
    lowered = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if isinstance(exc, ProviderSchemaError):
        return "schema_error"
    if isinstance(exc, ProviderNotConfiguredError):
        return "not_configured"
    if isinstance(exc, ProviderAccountBlockedError):
        return "account_blocked"
    if isinstance(exc, ProviderTransientError):
        if "429" in lowered or "rate limit" in lowered:
            return "rate_limit"
        return "transient_error"
    if isinstance(exc, ProviderPermanentError):
        return "permanent_error"
    if "schema" in lowered:
        return "schema_error"
    if "json" in lowered:
        return "invalid_json"
    return "".join(
        f"_{char.lower()}" if char.isupper() else char for char in name
    ).lstrip("_")


@dataclass
class AICallRecord:
    """One entry of the ``ai_runs`` table."""

    provider: str
    operation: str
    model: str = ""
    prompt_version: str = ""
    status: str = "ok"
    latency_ms: int | None = None
    input_frame_count: int = 0
    input_video_duration: float | None = None
    error_type: str | None = None
    error_message: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost: float | None = None
    result_json: str | None = None
    task_id: int | None = None
    source_video_id: int | None = None
    clip_id: int | None = None
    #: ``pipeline`` / ``retag`` / ``evaluation`` (see ``AuditContext``)
    origin: str = "pipeline"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    #: ``ai_runs.id`` once the audit row was written.  Not a column: it lets the
    #: caller back-patch ``clip_id`` after the clip row exists (section 16).
    row_id: int | None = None

    def finish(self, *, status: str, error: BaseException | None = None) -> "AICallRecord":
        self.finished_at = datetime.now(timezone.utc)
        self.status = status
        self.latency_ms = int((self.finished_at - self.started_at).total_seconds() * 1000)
        if error is not None:
            self.error_type = classify_error(error)
            self.error_message = sanitize_error(error)
        return self

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_row(self) -> dict[str, Any]:
        """Flatten into SQLite column values."""

        return {
            "task_id": self.task_id,
            "source_video_id": self.source_video_id,
            "clip_id": self.clip_id,
            "origin": self.origin,
            "provider": self.provider,
            "model": self.model,
            "operation": self.operation,
            "prompt_version": self.prompt_version,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "latency_ms": self.latency_ms,
            "input_frame_count": self.input_frame_count,
            "input_video_duration": self.input_video_duration,
            "status": self.status,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost": self.estimated_cost,
            "result_json": self.result_json,
            "created_at": self.started_at.isoformat(),
        }

    def result_payload(self, payload: Any) -> None:
        """Store the model answer (or the validated object) as JSON."""

        if payload is None:
            self.result_json = None
            return
        try:
            if hasattr(payload, "model_dump"):
                data = payload.model_dump(mode="json")
            else:
                data = payload
            self.result_json = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            self.result_json = None
