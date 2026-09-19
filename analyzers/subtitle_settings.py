"""Bridge from ``config.yaml`` to the subtitle analyzer (Milestone 6, section 40).

Keeps the analyzer free of the config schema and keeps the config free of
analyzer internals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from analyzers.subtitle_analysis import SubtitleAnalysisSettings
from core.subtitle_models import (
    SubtitleCleanlinessWeights,
    SubtitleRuleSettings,
    SubtitleZoneSettings,
)


def build_subtitle_settings(config: Any | None, *, base: Path | None = None) -> SubtitleAnalysisSettings:
    """Translate an ``AppSettings.subtitle_analysis`` block into analyzer settings."""

    if config is None:
        return SubtitleAnalysisSettings()
    zones = SubtitleZoneSettings(
        center_start=float(getattr(config, "center_zone_start", 0.25)),
        bottom_start=float(getattr(config, "bottom_zone_start", 0.72)),
        band_min_width=float(getattr(config, "band_min_width", 0.70)),
    )
    rules = SubtitleRuleSettings(
        simple_bottom_max_area=float(getattr(config, "simple_bottom_max_area", 0.12)),
        large_center_min_area=float(getattr(config, "large_center_min_area", 0.10)),
        dense_text_area=float(getattr(config, "dense_text_area", 0.25)),
        multi_region_min_persistence=float(
            getattr(config, "multi_region_min_persistence", 0.40)
        ),
        center_text_reject_persistence=float(
            getattr(config, "center_text_reject_persistence", 0.50)
        ),
        band_min_persistence=float(getattr(config, "band_min_persistence", 0.40)),
        simple_bottom_max_regions=int(getattr(config, "simple_bottom_max_regions", 3)),
    )
    raw_weights = getattr(config, "cleanliness_weights", None)
    weights = (
        SubtitleCleanlinessWeights.model_validate(dict(raw_weights))
        if raw_weights
        else SubtitleCleanlinessWeights()
    )
    debug_raw = str(getattr(config, "debug_dir", "") or "").strip()
    debug_dir = None
    if debug_raw:
        debug_dir = Path(debug_raw)
        if not debug_dir.is_absolute() and base is not None:
            debug_dir = base / debug_dir
    return SubtitleAnalysisSettings(
        enabled=bool(getattr(config, "enabled", True)),
        engine=str(getattr(config, "engine", "auto") or "auto"),
        max_frames=max(1, int(getattr(config, "max_frames", 4))),
        preview_max_frames=max(1, int(getattr(config, "preview_max_frames", 3))),
        skip_with_mock_media=bool(getattr(config, "skip_with_mock_media", True)),
        force_with_mock=bool(getattr(config, "force_with_mock", False)),
        hybrid_qwen=bool(getattr(config, "hybrid_qwen", True)),
        use_cache=bool(getattr(config, "cache", True)),
        debug_dir=debug_dir,
        zones=zones,
        rules=rules,
        weights=weights,
    )
