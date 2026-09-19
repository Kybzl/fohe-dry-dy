"""JSON schema helpers to keep every model response validated.

Real providers (Qwen / Volcano) return free form text.  ``parse_json_payload``
is the only place allowed to turn that text into typed objects: a response
that cannot be validated raises ``ProviderError`` and the gateway then falls
back to the secondary provider instead of crashing the task.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ai.base import (
    ClipTaggingRequest,
    PreviewFilterRequest,
    ProviderError,
    ProviderSchemaError,
    SegmentDetectionRequest,
)
from core.models import ClipTagging, PreviewFilterResult, SegmentDetectionResult

LOGGER = logging.getLogger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

#: ``operation -> {version -> file}``.  Older versions stay on disk so an A/B
#: comparison (or an explicit retag) can reproduce exactly what the model saw.
PROMPT_FILES: dict[str, dict[str, Path]] = {
    "preview_filter": {"preview_filter_v1": PROMPTS_DIR / "preview_filter.txt"},
    "segment_detection": {
        "segment_detection_v1": PROMPTS_DIR / "segment_detection.txt",
        "segment_detection_v2": PROMPTS_DIR / "segment_detection_v2.txt",
    },
    "clip_tagging": {
        "clip_tagging_v1": PROMPTS_DIR / "clip_tagging.txt",
        "clip_tagging_v2": PROMPTS_DIR / "clip_tagging_v2.txt",
    },
}

#: Persisted in ``ai_runs.prompt_version``.  Bump when a prompt changes
#: materially so old and new behaviour can be told apart in the audit trail.
PROMPT_VERSIONS: dict[str, str] = {
    "preview_filter": "preview_filter_v1",
    "segment_detection": "segment_detection_v2",
    # Milestone 3.7: observe-before-classify prompt.  ``clip_tagging_v1`` is
    # still available for comparison runs and is never overwritten.
    "clip_tagging": "clip_tagging_v2",
}

RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "preview_filter": PreviewFilterResult,
    "segment_detection": SegmentDetectionResult,
    "clip_tagging": ClipTagging,
}

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def prompt_version(name: str, version: str | None = None) -> str:
    """Resolve ``(operation, version)`` to a concrete version string."""

    versions = PROMPT_FILES.get(name)
    if versions is None:
        raise KeyError(f"unknown prompt: {name}")
    if version is None or not version:
        return PROMPT_VERSIONS.get(name) or next(iter(versions))
    if version in versions:
        return version
    # tolerate the bare operation name ("clip_tagging") and "v2" shorthand
    if version == name:
        return PROMPT_VERSIONS.get(name) or next(iter(versions))
    suffix = version if version.startswith("_") else f"_{version}"
    candidate = f"{name}{suffix}"
    if candidate in versions:
        return candidate
    raise KeyError(f"unknown prompt version {version!r} for {name!r}")


def load_prompt(name: str, version: str | None = None) -> str:
    """Read a prompt template from ``ai/prompts`` (current version by default)."""

    resolved = prompt_version(name, version)
    path = PROMPT_FILES[name][resolved]
    return path.read_text(encoding="utf-8")


def render_prompt(name: str, version: str | None = None, **values: Any) -> str:
    """Render ``{{placeholder}}`` style placeholders in a prompt template.

    Any placeholder without a value is removed, so a provider can never receive
    a literal ``{{...}}`` token (that would be prompt noise the model may echo).
    """

    template = load_prompt(name, version)
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    return re.sub(r"\{\{\s*[\w.]+\s*\}\}", "", template)


def extract_json_block(text: str) -> str:
    """Pull the JSON object out of a model answer (handles ``` fences)."""

    fenced = _JSON_BLOCK.search(text)
    if fenced:
        return fenced.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text.strip()


def parse_json_payload(payload: str | dict[str, Any], model: type[TModel]) -> TModel:
    """Validate raw provider output against a Pydantic model."""

    if isinstance(payload, str):
        raw = extract_json_block(payload)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderSchemaError(f"provider returned invalid JSON: {exc}") from exc
    else:
        data = payload

    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ProviderSchemaError(
            f"provider JSON failed schema validation for {model.__name__}: {exc}"
        ) from exc


def build_preview_request(**values: Any) -> PreviewFilterRequest:
    return PreviewFilterRequest(**values)


def build_segment_request(**values: Any) -> SegmentDetectionRequest:
    return SegmentDetectionRequest(**values)


def build_tag_request(**values: Any) -> ClipTaggingRequest:
    return ClipTaggingRequest(**values)


def schema_instructions(model: type[BaseModel]) -> str:
    """Compact JSON-schema hint appended to prompts for real providers."""

    schema = model.model_json_schema()
    return json.dumps(schema, ensure_ascii=False, indent=2)
