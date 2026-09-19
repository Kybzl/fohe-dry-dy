"""Qwen (Alibaba Cloud Model Studio) multimodal provider - primary provider.

Talks to the OpenAI compatible ``/chat/completions`` endpoint with image parts.
Everything is configuration driven:

* ``QWEN_API_KEY`` / ``QWEN_BASE_URL`` from ``.env`` (never logged)
* ``ai.qwen.preview_model`` / ``analysis_model`` / ``fallback_model`` from
  ``config.yaml`` (an empty value falls back to ``QWEN_VISION_MODEL``)

Responses are returned as plain dicts; the ``AIGateway`` validates them against
the Pydantic schemas, so a malformed answer can never reach the library.
"""

from __future__ import annotations

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
from ai.schemas import RESPONSE_MODELS, render_prompt, schema_instructions
from core.models import ClipTagging, PreviewFilterResult, SegmentDetectionResult

LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-vl-max"
DEFAULT_PREVIEW_MODEL = "qwen-vl-max"
MAX_IMAGES_PER_CALL = 16


class QwenProvider(VisionProvider):
    """Real Qwen vision provider."""

    name = "qwen"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        model: str = "",
        preview_model: str = "",
        analysis_model: str = "",
        fallback_model: str = "",
        timeout: float = 60.0,
        max_retries: int = 2,
        backoff_seconds: float = 1.5,
        json_mode: bool = True,
        max_images: int = MAX_IMAGES_PER_CALL,
        confidence_escalation_threshold: float = 0.70,
        client_factory: Callable[[], Any] | None = None,
        **options: Any,
    ) -> None:
        super().__init__(timeout=timeout, max_retries=max_retries, **options)
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        default_model = model or DEFAULT_MODEL
        self.preview_model = preview_model or default_model
        self.analysis_model = analysis_model or default_model
        self.fallback_model = fallback_model
        self.confidence_escalation_threshold = confidence_escalation_threshold
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

    # -- configuration -----------------------------------------------------
    @property
    def configured(self) -> bool:
        """True when an API key and a base URL are available."""

        return bool(self.api_key and self.base_url)

    def model_for(self, operation: str) -> str:
        if operation == "preview_filter":
            return self.preview_model or DEFAULT_PREVIEW_MODEL
        return self.analysis_model or DEFAULT_MODEL

    def escalate_model(self, operation: str) -> str:
        return self.fallback_model or self.model_for(operation)

    # -- prompt construction -----------------------------------------------
    def build_preview_prompt(self, request: PreviewFilterRequest) -> str:
        body = render_prompt(
            "preview_filter",
            material=request.material,
            query=request.query,
            platform=request.platform,
            title=request.title,
            duration=request.duration or 0,
            frame_count=len(request.frames),
        )
        return body + "\n\nJSON Schema:\n" + schema_instructions(RESPONSE_MODELS["preview_filter"])

    def build_segment_prompt(self, request: SegmentDetectionRequest) -> str:
        body = render_prompt(
            "segment_detection",
            material=request.material,
            query=request.query,
            duration=request.duration,
            min_duration=request.min_segment_duration,
            max_duration=request.max_segment_duration,
            target_stage=request.target_process_stage or "不限",
            frame_count=len(request.frames),
        )
        return body + "\n\nJSON Schema:\n" + schema_instructions(
            RESPONSE_MODELS["segment_detection"]
        )

    def build_tagging_prompt(self, request: ClipTaggingRequest) -> str:
        version = self.prompt_version_for("clip_tagging", request)
        body = render_prompt(
            "clip_tagging",
            version=version,
            material=request.material,
            requested_material=request.requested_material or request.material,
            query=request.query,
            title=request.title,
            start=request.start,
            end=request.end,
            duration=round(request.end - request.start, 2),
            segment_description=request.segment_description,
            frame_count=len(request.frames),
        )
        return body + "\n\nJSON Schema:\n" + schema_instructions(RESPONSE_MODELS["clip_tagging"])

    # -- VisionProvider ----------------------------------------------------
    async def preview_filter(self, request: PreviewFilterRequest) -> dict[str, Any]:
        return await self._invoke(
            "preview_filter",
            self.build_preview_prompt(request),
            request,
            escalate=lambda payload: self._should_escalate_preview(payload),
        )

    async def detect_segments(self, request: SegmentDetectionRequest) -> dict[str, Any]:
        return await self._invoke(
            "segment_detection", self.build_segment_prompt(request), request
        )

    async def tag_clip(self, request: ClipTaggingRequest) -> dict[str, Any]:
        return await self._invoke("clip_tagging", self.build_tagging_prompt(request), request)

    async def aclose(self) -> None:
        await self.client.aclose()

    # -- internals ---------------------------------------------------------
    async def _invoke(
        self,
        task: str,
        prompt: str,
        request: Any,
        *,
        escalate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        """Call Qwen once (plus an optional escalation call) and return JSON."""

        if not self.configured:
            raise ProviderNotConfiguredError(
                "Qwen provider is not configured: set QWEN_API_KEY (and optionally "
                "QWEN_BASE_URL) in .env."
            )

        images = self._frame_paths(request)
        model = self.model_for(task)
        result = await self.client.complete(
            model=model, prompt=prompt, images=images, enable_thinking=False
        )
        self._record_result_usage(result, model)
        try:
            payload = self._parse_json(result.text)
        except ProviderSchemaError:
            # Qwen3 vision occasionally returns a truncated/quoted JSON value
            # even with response_format=json_object. Retry once with an
            # explicit repair instruction; malformed data is never accepted.
            LOGGER.warning("%s returned malformed JSON; retrying once", task)
            repair_prompt = (
                prompt
                + "\n\nThe previous response was invalid. Return exactly one complete JSON object "
                "matching the schema. Do not quote the object or add Markdown."
                + (
                    ' If no valid segment exists, return exactly {"segments":[]}.'
                    if task == "segment_detection"
                    else ""
                )
            )
            result = await self.client.complete(
                model=model,
                prompt=repair_prompt,
                images=images,
                enable_thinking=False,
            )
            self._record_result_usage(result, model)
            payload = self._parse_json(result.text)

        payload = self._normalize_payload(task, payload)

        if escalate is not None and self.fallback_model and escalate(payload):
            LOGGER.info(
                "escalating %s to fallback model %s (low confidence)",
                task,
                self.fallback_model,
            )
            escalated = await self.client.complete(
                model=self.fallback_model,
                prompt=prompt,
                images=images,
                enable_thinking=False,
            )
            self._last_model = escalated.model or self.fallback_model
            self._last_usage = UsageInfo(
                model=self._last_model,
                prompt_tokens=escalated.prompt_tokens,
                completion_tokens=escalated.completion_tokens,
                total_tokens=escalated.total_tokens,
            )
            payload = self._parse_json(escalated.text)
        return payload

    @staticmethod
    def _normalize_payload(task: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Normalize explicit Qwen no-result answers to the canonical schema.

        Some instruct models explain that no segment exists using fields such
        as ``usable=false`` or ``error='No valid segments found'``.  This is a
        valid negative verdict, not a provider failure.  Positive or ambiguous
        non-schema payloads remain untouched and are rejected by the gateway.
        """

        if task != "segment_detection" or "segments" in payload:
            return payload
        if payload.get("usable") is False:
            return {"segments": []}
        explanation = " ".join(
            str(payload.get(key, ""))
            for key in ("error", "warning", "note", "summary", "description")
        ).lower()
        no_result_markers = (
            "no valid segment",
            "no usable segment",
            "does not contain",
            "cannot be extracted",
            "未发现有效片段",
            "没有有效片段",
            "不包含目标工序",
        )
        if any(marker in explanation for marker in no_result_markers):
            return {"segments": []}
        return payload

    def _record_result_usage(self, result: Any, fallback_model: str) -> None:
        self._last_model = result.model or fallback_model
        self._last_usage = UsageInfo(
            model=self._last_model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
        )

    @staticmethod
    def _frame_paths(request: Any) -> list[Path]:
        paths: list[Path] = []
        for frame in getattr(request, "frames", []) or []:
            image_path = getattr(frame, "image_path", None)
            if image_path and Path(image_path).exists():
                paths.append(Path(image_path))
        return paths

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        from ai.schemas import extract_json_block

        import json

        raw = extract_json_block(text)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            from ai.base import ProviderSchemaError

            raise ProviderSchemaError(f"Qwen returned invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            from ai.base import ProviderSchemaError

            raise ProviderSchemaError(f"Qwen returned {type(payload).__name__}, expected object")
        return payload

    def _should_escalate_preview(self, payload: dict[str, Any]) -> bool:
        """Escalate only on genuine low confidence, never on a clear answer."""

        if not isinstance(payload, dict):
            return False
        try:
            relevance = float(payload.get("material_relevance", 0.0))
            quality = float(payload.get("quality_score", 0.0))
        except (TypeError, ValueError):
            return False
        if payload.get("accept") is False:
            return False
        confidence = min(relevance, quality)
        return confidence < self.confidence_escalation_threshold
