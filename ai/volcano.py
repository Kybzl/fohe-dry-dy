"""Volcano Engine (Ark) vision provider - fallback provider.

Ark exposes an OpenAI compatible ``/chat/completions`` endpoint, so this
adapter reuses the shared client.  Credentials and endpoint come from ``.env``:

    VOLCANO_API_KEY=...
    VOLCANO_ENDPOINT=https://ark.cn-beijing.volces.com/api/v3
    VOLCANO_MODEL=<endpoint id or model name>

``config.yaml`` may override the same values under ``ai.volcano``.  The
provider reports ``configured == False`` when anything is missing, so the
gateway simply skips it instead of failing a task.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from ai.base import (
    ClipTaggingRequest,
    PreviewFilterRequest,
    ProviderNotConfiguredError,
    ProviderSchemaError,
    SegmentDetectionRequest,
    UsageInfo,
    VisionProvider,
)
from ai.openai_compat import OpenAICompatibleClient
from ai.schemas import RESPONSE_MODELS, extract_json_block, render_prompt, schema_instructions

LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
MAX_IMAGES_PER_CALL = 16


class VolcanoProvider(VisionProvider):
    """Real Volcano Engine (Ark) vision provider."""

    name = "volcano"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        timeout: float = 60.0,
        max_retries: int = 2,
        backoff_seconds: float = 1.5,
        json_mode: bool = True,
        max_images: int = MAX_IMAGES_PER_CALL,
        client_factory: Callable[[], Any] | None = None,
        **options: Any,
    ) -> None:
        super().__init__(timeout=timeout, max_retries=max_retries, **options)
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = model
        self.client = OpenAICompatibleClient(
            api_key=api_key,
            base_url=self.base_url,
            timeout=timeout,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            json_mode=json_mode,
            client_factory=client_factory,
            max_images=max_images,
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def model_for(self, operation: str) -> str:
        return self.model

    # -- prompts (same templates as Qwen, different provider) --------------
    def _prompt(self, name: str, *, response_model: str, **values: Any) -> str:
        body = render_prompt(name, **values)
        return body + "\n\nJSON Schema:\n" + schema_instructions(RESPONSE_MODELS[response_model])

    async def preview_filter(self, request: PreviewFilterRequest) -> dict[str, Any]:
        prompt = self._prompt(
            "preview_filter",
            response_model="preview_filter",
            material=request.material,
            query=request.query,
            platform=request.platform,
            title=request.title,
            duration=request.duration or 0,
            frame_count=len(request.frames),
        )
        return await self._invoke("preview_filter", prompt, request)

    async def detect_segments(self, request: SegmentDetectionRequest) -> dict[str, Any]:
        prompt = self._prompt(
            "segment_detection",
            response_model="segment_detection",
            material=request.material,
            query=request.query,
            duration=request.duration,
            min_duration=request.min_segment_duration,
            max_duration=request.max_segment_duration,
            frame_count=len(request.frames),
        )
        return await self._invoke("segment_detection", prompt, request)

    async def tag_clip(self, request: ClipTaggingRequest) -> dict[str, Any]:
        prompt = self._prompt(
            "clip_tagging",
            response_model="clip_tagging",
            material=request.material,
            start=request.start,
            end=request.end,
            duration=round(request.end - request.start, 2),
            segment_description=request.segment_description,
            frame_count=len(request.frames),
        )
        return await self._invoke("clip_tagging", prompt, request)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _invoke(self, task: str, prompt: str, request: Any) -> dict[str, Any]:
        if not self.configured:
            raise ProviderNotConfiguredError(
                "Volcano provider is not configured: set VOLCANO_API_KEY, "
                "VOLCANO_ENDPOINT and VOLCANO_MODEL in .env."
            )
        images: list[Path] = []
        for frame in getattr(request, "frames", []) or []:
            image_path = getattr(frame, "image_path", None)
            if image_path and Path(image_path).exists():
                images.append(Path(image_path))

        result = await self.client.complete(model=self.model, prompt=prompt, images=images)
        self._last_model = result.model or self.model
        self._last_usage = UsageInfo(
            model=self._last_model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
        )
        try:
            payload = json.loads(extract_json_block(result.text))
        except json.JSONDecodeError as exc:
            raise ProviderSchemaError(f"Volcano returned invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ProviderSchemaError("Volcano returned a non-object payload")
        return payload
