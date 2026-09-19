"""Shared OpenAI-compatible multimodal client used by Qwen and Volcano.

Both providers speak the ``/chat/completions`` dialect with image content
parts, so the HTTP plumbing, retry policy and response parsing live here once.
Secrets are only ever sent in the ``Authorization`` header and never logged.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from ai.base import (
    ProviderAccountBlockedError,
    ProviderError,
    ProviderPermanentError,
    ProviderSchemaError,
    ProviderTransientError,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_TEMPERATURE = 0.0


@dataclass
class ChatResult:
    """Normalised answer of one chat completion call."""

    text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    usage_raw: dict[str, Any] = field(default_factory=dict)


class OpenAICompatibleClient:
    """Async ``/chat/completions`` client with bounded transient retries."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout: float = 60.0,
        max_retries: int = 2,
        backoff_seconds: float = 1.5,
        json_mode: bool = True,
        extra_headers: dict[str, str] | None = None,
        client_factory: Callable[[], Any] | None = None,
        max_images: int = 16,
    ) -> None:
        self.api_key = api_key
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.backoff_seconds = backoff_seconds
        self.json_mode = json_mode
        self.extra_headers = dict(extra_headers or {})
        self.max_images = max(1, max_images)
        self._client_factory = client_factory
        self._client: Any = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url)

    # -- client lifecycle --------------------------------------------------
    def _get_client(self) -> Any:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:  # pragma: no cover - exercised only against the real API
                import httpx

                self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            close = getattr(self._client, "aclose", None)
            if close is not None:
                await close()
            self._client = None

    # -- payload construction (pure, unit tested) --------------------------
    @staticmethod
    def encode_image(path: Path | str) -> str:
        """Return a ``data:`` URL for one image file."""

        data = Path(path).read_bytes()
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    @classmethod
    def build_messages(
        cls,
        prompt: str,
        images: Sequence[Path | str] = (),
        *,
        system: str | None = None,
        max_images: int = 16,
    ) -> list[dict[str, Any]]:
        """Build the OpenAI-style messages array with image parts first."""

        content: list[dict[str, Any]] = []
        for image in list(images)[:max_images]:
            try:
                url = cls.encode_image(image)
            except OSError as exc:
                LOGGER.warning("skipping unreadable frame %s: %s", image, exc)
                continue
            content.append({"type": "image_url", "image_url": {"url": url}})
        content.append({"type": "text", "text": prompt})

        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})
        return messages

    @staticmethod
    def build_payload(
        *,
        model: str,
        messages: list[dict[str, Any]],
        json_mode: bool = True,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int | None = None,
        enable_thinking: bool | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if enable_thinking is not None:
            payload["enable_thinking"] = bool(enable_thinking)
        return payload

    @staticmethod
    def parse_response(payload: dict[str, Any], *, fallback_model: str = "") -> ChatResult:
        """Extract text + token usage from a chat completion response."""

        choices = payload.get("choices") or []
        text = ""
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts: list[str] = []
                for part in content:
                    if isinstance(part, dict) and part.get("text"):
                        parts.append(str(part["text"]))
                    elif isinstance(part, str):
                        parts.append(part)
                text = "".join(parts)
        if not text:
            raise ProviderError("provider returned an empty completion")
        usage = payload.get("usage") or {}
        return ChatResult(
            text=text,
            model=str(payload.get("model") or fallback_model),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            usage_raw=usage if isinstance(usage, dict) else {},
        )

    @staticmethod
    def classify_status(status: int, body: str = "") -> ProviderError:
        """Map an HTTP status onto the retry policy."""

        snippet = (body or "").strip()[:200]
        lowered = snippet.lower()
        message = f"provider returned HTTP {status}: {snippet}"
        if status in (408, 409, 425, 429) or status >= 500:
            return ProviderTransientError(message)
        if status in (401,):
            return ProviderAccountBlockedError(
                message, subtype="authentication_failed"
            )
        if status == 403:
            if any(
                token in lowered
                for token in (
                    "quota",
                    "free tier",
                    "free_tier",
                    "allocationquota",
                    "insufficient balance",
                    "arrears",
                )
            ):
                return ProviderAccountBlockedError(
                    message, subtype="quota_exhausted"
                )
            if any(
                token in lowered
                for token in ("access denied", "permission", "forbidden", "not authorized")
            ):
                return ProviderAccountBlockedError(
                    message, subtype="model_access_denied"
                )
            return ProviderAccountBlockedError(
                message, subtype="authentication_failed"
            )
        if status == 404 and any(
            token in lowered for token in ("model", "not found", "not exist")
        ):
            return ProviderAccountBlockedError(
                message, subtype="model_access_denied"
            )
        return ProviderPermanentError(message)

    # -- request -----------------------------------------------------------
    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        images: Sequence[Path | str] = (),
        system: str | None = None,
        json_mode: bool | None = None,
        max_tokens: int | None = None,
        enable_thinking: bool | None = None,
    ) -> ChatResult:
        """POST one chat completion, retrying only transient failures."""

        if not self.configured:
            raise ProviderError("provider is not configured (missing api key or base url)")

        messages = self.build_messages(
            prompt, images, system=system, max_images=self.max_images
        )
        payload = self.build_payload(
            model=model,
            messages=messages,
            json_mode=self.json_mode if json_mode is None else json_mode,
            max_tokens=max_tokens,
            enable_thinking=enable_thinking,
        )
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }

        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._get_client().post(url, headers=headers, json=payload)
                status = int(getattr(response, "status_code", 0))
                if status >= 400:
                    raise self.classify_status(status, getattr(response, "text", ""))
                data = response.json()
                result = self.parse_response(data, fallback_model=model)
                LOGGER.debug(
                    "chat completion ok: model=%s images=%s tokens=%s",
                    result.model,
                    len(images),
                    result.total_tokens,
                )
                return result
            except asyncio.CancelledError:
                raise
            except (ProviderPermanentError, ProviderSchemaError):
                # Bad request / bad key / invalid JSON: retrying is pointless.
                raise
            except ProviderTransientError as exc:
                if attempt < self.max_retries:
                    delay = self.backoff_seconds * attempt
                    LOGGER.warning(
                        "transient provider error (attempt %s/%s), retrying in %.1fs: %s",
                        attempt,
                        self.max_retries,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
            except ProviderError:
                raise
            except Exception as exc:
                error = self._as_provider_error(exc)
                if isinstance(error, ProviderTransientError) and attempt < self.max_retries:
                    delay = self.backoff_seconds * attempt
                    LOGGER.warning(
                        "transient provider error (attempt %s/%s), retrying in %.1fs: %s",
                        attempt,
                        self.max_retries,
                        delay,
                        error,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise error from exc

    @staticmethod
    def _as_provider_error(exc: Exception) -> ProviderError:
        """Translate httpx/transport exceptions into provider errors."""

        name = type(exc).__name__.lower()
        message = f"{type(exc).__name__}: {exc}"
        if "timeout" in name:
            return ProviderTransientError(message)
        if "connect" in name or "transport" in name or "network" in name:
            return ProviderTransientError(message)
        if isinstance(exc, json.JSONDecodeError):
            return ProviderError(f"provider returned invalid JSON: {exc}")
        if isinstance(exc, ProviderError):
            return exc
        return ProviderError(message)
