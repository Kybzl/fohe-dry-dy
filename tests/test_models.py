"""Domain model validation (enumerations, ranges, JSON contract)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.models import (
    CameraMotion,
    ClipScores,
    ClipTagging,
    DetectedSegment,
    EditRole,
    MaterialForm,
    PersonRole,
    PipelineStats,
    PreviewFilterResult,
    ProcessStage,
    RejectReason,
    SubtitleType,
    TaskRequest,
)


def test_enum_values_match_the_specification() -> None:
    assert ProcessStage.TRAY_ARRANGEMENT == "tray_arrangement"
    assert ProcessStage.FINISHED_PRODUCT == "finished_product"
    assert SubtitleType.BOTTOM_SIMPLE == "bottom_simple"
    assert CameraMotion.ZOOM_IN == "zoom_in"
    assert EditRole.RAW_MATERIAL == "raw_material"
    assert MaterialForm.SLICE == "slice"
    assert RejectReason.MULTI_REGION_SUBTITLE == "multi_region_subtitle"


def test_preview_filter_result_accepts_specification_payload() -> None:
    payload = {
        "accept": True,
        "material_visible": True,
        "material_relevance": 0.91,
        "subtitle_complexity": "low",
        "visual_complexity": "low",
        "quality_score": 0.86,
        "reject_reason": None,
    }
    result = PreviewFilterResult.model_validate(payload)
    assert result.accept is True
    assert result.reject_reason is None
    assert result.model_dump() == payload


def test_rejection_without_reason_is_normalised() -> None:
    result = PreviewFilterResult(
        accept=False,
        material_visible=False,
        material_relevance=0.1,
        subtitle_complexity="low",
        visual_complexity="low",
        quality_score=0.5,
    )
    assert result.reject_reason is RejectReason.OTHER


def test_score_out_of_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PreviewFilterResult(
            accept=True,
            material_visible=True,
            material_relevance=1.4,
            subtitle_complexity="low",
            visual_complexity="low",
            quality_score=0.5,
        )


def test_detected_segment_requires_end_after_start() -> None:
    segment = DetectedSegment(start=5.2, end=11.8, description="苹果片铺盘")
    assert round(segment.duration, 1) == 6.6
    with pytest.raises(ValidationError):
        DetectedSegment(start=10.0, end=10.0)


def test_overall_score_is_recomputed_from_components() -> None:
    scores = ClipScores(
        material_relevance=1.0,
        visual_quality=1.0,
        subtitle_cleanliness=1.0,
        stability=1.0,
        composition=1.0,
    )
    assert scores.recompute_overall() == 1.0
    empty = ClipScores()
    assert empty.recompute_overall() == 0.0


def test_clip_tagging_keeps_people_fields_consistent() -> None:
    tagging = ClipTagging(material="苹果", people_count=2)
    assert tagging.people is True
    assert tagging.person_role is PersonRole.UNKNOWN
    nobody = ClipTagging(material="苹果")
    assert nobody.people is False
    assert nobody.person_role is PersonRole.NONE


def test_edit_roles_are_deduplicated() -> None:
    tagging = ClipTagging(
        material="苹果",
        edit_roles=[EditRole.PROCESS, EditRole.PROCESS, EditRole.DETAIL],
    )
    assert tagging.edit_roles == [EditRole.PROCESS, EditRole.DETAIL]


def test_packaging_stage_alias_is_normalized_in_edit_roles() -> None:
    tagging = ClipTagging(material="香菇", edit_roles=["packaging"])

    assert tagging.edit_roles == [EditRole.PROCESS]


def test_preparation_stage_alias_is_normalized_in_edit_roles() -> None:
    tagging = ClipTagging(
        material="香菇", edit_roles=["detail", "preparation", "process"]
    )

    assert tagging.edit_roles == [EditRole.DETAIL, EditRole.PROCESS]


def test_task_request_validates_duration_window_and_material() -> None:
    with pytest.raises(ValidationError):
        TaskRequest(material="苹果干", min_clip_duration=10, max_clip_duration=5)
    with pytest.raises(ValidationError):
        TaskRequest(material="   ")
    request = TaskRequest(material=" 苹果干 ")
    assert request.material == "苹果干"


def test_stats_rows_cover_the_ui_counters() -> None:
    rows = PipelineStats().as_rows()
    labels = [label for label, _ in rows]
    assert "搜索候选" in labels
    assert "最终保存数量" in labels
    assert all(isinstance(value, int) for _, value in rows)
