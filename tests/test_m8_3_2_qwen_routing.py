"""Milestone 8.3.2: effective Qwen model routing must come from the config.

The bug: ``config.yaml`` pinned ``ai.qwen.preview_model`` and ``.env`` pinned
``QWEN_VISION_MODEL``; switching the model in one place left the other in
charge of a whole operation class, and ``--check-config`` printed a single
env-first name that hid the split.

No network: the provider object is constructed locally.
"""

from __future__ import annotations

import pytest

from core.diagnostics import describe_effective_config, describe_qwen_routing

OPERATIONS = ("preview_filter", "segment_detection", "clip_tagging")


def _routing_map(lines: list[str]) -> dict[str, str]:
    """``{'preview_filter': 'model'}`` from the printed routing block."""

    out: dict[str, str] = {}
    for line in lines:
        if not line.startswith("[info]   "):
            continue
        body = line[len("[info]   ") :]
        parts = body.split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


def test_configured_models_win_over_the_env_default(settings, monkeypatch) -> None:
    """The exact bug class: config fields must drive their own operations."""

    monkeypatch.setenv("QWEN_API_KEY", "test-key")
    monkeypatch.setenv("QWEN_VISION_MODEL", "env-default-model")
    settings.ai.qwen = {
        "preview_model": "prod-preview-model",
        "analysis_model": "prod-analysis-model",
    }
    routing = _routing_map(describe_qwen_routing(settings))
    assert routing["preview_filter"] == "prod-preview-model"
    assert routing["segment_detection"] == "prod-analysis-model"
    assert routing["clip_tagging"] == "prod-analysis-model"
    # the generic default still reflects the environment, but no acquisition
    # operation silently falls back to it while a config value exists
    assert routing["default"] == "env-default-model"


def test_operations_fall_back_to_the_env_default_when_unset(settings, monkeypatch) -> None:
    monkeypatch.setenv("QWEN_API_KEY", "test-key")
    monkeypatch.setenv("QWEN_VISION_MODEL", "env-default-model")
    settings.ai.qwen = {"preview_model": "", "analysis_model": ""}
    routing = _routing_map(describe_qwen_routing(settings))
    for operation in OPERATIONS:
        assert routing[operation] == "env-default-model"


def test_routing_report_names_the_winning_layer(settings, monkeypatch) -> None:
    monkeypatch.setenv("QWEN_API_KEY", "test-key")
    monkeypatch.setenv("QWEN_VISION_MODEL", "env-default-model")
    settings.ai.qwen = {"preview_model": "prod-preview-model", "analysis_model": ""}
    report = "\n".join(describe_qwen_routing(settings))
    assert "config ai.qwen.preview_model" in report
    assert "env QWEN_VISION_MODEL" in report
    assert "env-default-model" in report


def test_effective_config_reports_operation_routing(settings, monkeypatch) -> None:
    monkeypatch.setenv("QWEN_API_KEY", "test-key")
    monkeypatch.setenv("QWEN_VISION_MODEL", "env-default-model")
    settings.ai.qwen = {"preview_model": "prod-preview-model", "analysis_model": "prod-analysis-model"}
    report = "\n".join(describe_effective_config(settings, detect_browsers=False))
    assert "qwen configured: yes" in report, "existing contract stays intact"
    assert "qwen routing (effective, per operation):" in report
    for operation in OPERATIONS:
        assert operation in report
    assert "prod-preview-model" in report and "prod-analysis-model" in report


def test_routing_report_never_leaks_the_key(settings, monkeypatch) -> None:
    monkeypatch.setenv("QWEN_API_KEY", "sk-super-secret-value")
    report = "\n".join(describe_qwen_routing(settings))
    assert "sk-super-secret-value" not in report


def test_shipped_config_has_no_acquisition_operation_on_a_stale_model() -> None:
    """The production model must be the only one used for acquisition.

    This guards the operator-facing requirement: after M8.3.2 no acquisition
    operation may resolve to the old flash model unless that is deliberate.
    """

    from core.config import load_settings

    settings = load_settings()
    routing = _routing_map(describe_qwen_routing(settings))
    stale = "qwen3-vl-flash-2025-10-15"
    assert stale not in routing.get("preview_filter", "")
    for operation in OPERATIONS:
        assert routing.get(operation), f"{operation} has no effective model"
    # preview and analysis agree in the shipped configuration
    assert routing["preview_filter"] == routing["segment_detection"] == routing["clip_tagging"]
