"""Deterministic mock vision provider.

It answers the three ``VisionProvider`` capabilities with plausible,
reproducible values so the whole pipeline can be exercised without spending a
single API call:

* ``preview_filter``  - driven by ``context["mock_scenario"]`` emitted by
  ``sources.mock.MockVideoSource`` (no_material, split_screen, subtitle
  problems, low quality, ...)
* ``detect_segments`` - returns several usable ranges plus one too-short and
  one too-long range so the normaliser in ``analyzers/video_analyzer.py`` is
  really used
* ``tag_clip``        - keyword driven tagging over the segment description,
  which mirrors what a real VLM does and keeps the output explainable
"""

from __future__ import annotations

import hashlib
import logging
import random
from typing import Any

from ai.base import (
    ClipTaggingRequest,
    PreviewFilterRequest,
    SegmentDetectionRequest,
    VisionProvider,
)
from core.models import (
    CameraMotion,
    ClipScores,
    ClipTagging,
    ComplexityLevel,
    DetectedSegment,
    EditRole,
    MaterialForm,
    MaterialState,
    PersonRole,
    PreviewFilterResult,
    ProcessStage,
    RejectReason,
    SegmentDetectionResult,
    ShotType,
    SubtitleType,
)

LOGGER = logging.getLogger(__name__)

# Scenario keys produced by sources/mock.py
SCENARIO_CLEAN = "clean"
SCENARIO_BORDERLINE_SUBTITLE = "borderline_subtitle"
SCENARIO_MULTI_REGION_SUBTITLE = "multi_region_subtitle"
SCENARIO_COLORED_TEXT_BLOCK = "colored_text_block"
SCENARIO_SPLIT_SCREEN = "split_screen"
SCENARIO_LOW_QUALITY = "low_quality"
SCENARIO_NO_MATERIAL = "no_material"
SCENARIO_NO_SEGMENT = "no_usable_segment"

#: scenario -> (accept, material_visible, relevance, subtitle, visual, quality, reject_reason)
SCENARIO_VERDICTS: dict[str, tuple[bool, bool, float, ComplexityLevel, ComplexityLevel, float, RejectReason | None]] = {
    SCENARIO_CLEAN: (True, True, 0.91, ComplexityLevel.LOW, ComplexityLevel.LOW, 0.88, None),
    SCENARIO_BORDERLINE_SUBTITLE: (
        True,
        True,
        0.86,
        ComplexityLevel.MEDIUM,
        ComplexityLevel.MEDIUM,
        0.79,
        None,
    ),
    SCENARIO_MULTI_REGION_SUBTITLE: (
        False,
        True,
        0.72,
        ComplexityLevel.HIGH,
        ComplexityLevel.HIGH,
        0.55,
        RejectReason.MULTI_REGION_SUBTITLE,
    ),
    SCENARIO_COLORED_TEXT_BLOCK: (
        False,
        True,
        0.68,
        ComplexityLevel.HIGH,
        ComplexityLevel.HIGH,
        0.5,
        RejectReason.COLORED_TEXT_BLOCK,
    ),
    SCENARIO_SPLIT_SCREEN: (
        False,
        True,
        0.6,
        ComplexityLevel.MEDIUM,
        ComplexityLevel.HIGH,
        0.52,
        RejectReason.SPLIT_SCREEN,
    ),
    SCENARIO_LOW_QUALITY: (
        False,
        True,
        0.55,
        ComplexityLevel.MEDIUM,
        ComplexityLevel.MEDIUM,
        0.28,
        RejectReason.LOW_QUALITY,
    ),
    SCENARIO_NO_MATERIAL: (
        False,
        False,
        0.05,
        ComplexityLevel.LOW,
        ComplexityLevel.LOW,
        0.7,
        RejectReason.NO_MATERIAL,
    ),
}


class _SegmentTemplate:
    """One synthetic but realistic shot description."""

    __slots__ = ("key", "text", "duration", "relevance", "shared")

    def __init__(self, key: str, text: str, duration: float | None, relevance: float, shared: bool = False) -> None:
        self.key = key
        self.text = text
        self.duration = duration
        self.relevance = relevance
        self.shared = shared


TEMPLATES: tuple[_SegmentTemplate, ...] = (
    _SegmentTemplate("cutting", "{base}原料清洗后由切片机切成厚薄均匀的{form_word}", None, 0.9),
    _SegmentTemplate("tray", "{form_word}被均匀铺放在不锈钢烘干托盘上", None, 0.94),
    _SegmentTemplate("dryer", "烘干房内多层托盘上的{form_word}，热风循环带走水分", None, 0.95),
    _SegmentTemplate("dryer_detail", "热泵烘干房内托盘特写，{form_word}表面逐渐干燥", None, 0.92),
    _SegmentTemplate("finished", "烘干完成的{material}成品颜色均匀，质感干燥", None, 0.96),
    _SegmentTemplate("unloading", "工人从烘干房推出托盘并卸下{material}", None, 0.89),
    _SegmentTemplate("equipment", "热泵烘干机组的控制面板与风道特写", 6.0, 0.7),
    _SegmentTemplate("packaging", "干燥后的{material}进入包装环节", None, 0.83),
    _SegmentTemplate(
        "shared_tray",
        "烘干房内多层不锈钢托盘上的{form_word}，镜头稳定",
        8.0,
        0.93,
        shared=True,
    ),
)

# Keyword -> process stage.  First match wins, so order matters.
KEYWORD_STAGE_RULES: tuple[tuple[tuple[str, ...], ProcessStage], ...] = (
    (("卸下", "推出托盘", "出料"), ProcessStage.UNLOADING),
    (("成品", "烘干完成"), ProcessStage.FINISHED_PRODUCT),
    (("包装", "装箱"), ProcessStage.PACKAGING),
    (("切片机", "清洗", "切成"), ProcessStage.CUTTING),
    (("烘干房", "热风循环"), ProcessStage.INSIDE_DRYER),
    (("托盘", "铺放", "铺在"), ProcessStage.TRAY_ARRANGEMENT),
    (("控制面板", "机组", "风道"), ProcessStage.EQUIPMENT),
    (("原料", "运输筐"), ProcessStage.RAW_MATERIAL),
    (("烘干", "干燥"), ProcessStage.DRYING),
)

STAGE_PRESETS: dict[ProcessStage, dict[str, Any]] = {
    ProcessStage.RAW_MATERIAL: {
        "state": MaterialState.FRESH,
        "roles": [EditRole.RAW_MATERIAL, EditRole.INTRO],
        "equipment_visible": False,
        "equipment_type": None,
        "scene": "原料堆放区",
        "shot_type": ShotType.MEDIUM,
        "camera_motion": CameraMotion.STATIC,
    },
    ProcessStage.CUTTING: {
        "state": MaterialState.PREPARED,
        "roles": [EditRole.PROCESS, EditRole.RAW_MATERIAL],
        "equipment_visible": True,
        "equipment_type": "slicing_machine",
        "scene": "加工车间",
        "shot_type": ShotType.CLOSE_UP,
        "camera_motion": CameraMotion.STATIC,
    },
    ProcessStage.TRAY_ARRANGEMENT: {
        "state": MaterialState.PREPARED,
        "roles": [EditRole.PROCESS, EditRole.DETAIL],
        "equipment_visible": True,
        "equipment_type": "drying_tray",
        "scene": "烘干房内部",
        "shot_type": ShotType.CLOSE_UP,
        "camera_motion": CameraMotion.STATIC,
    },
    ProcessStage.INSIDE_DRYER: {
        "state": MaterialState.DRYING,
        "roles": [EditRole.PROCESS, EditRole.EQUIPMENT],
        "equipment_visible": True,
        "equipment_type": "heat_pump_dryer",
        "scene": "烘干房内部",
        "shot_type": ShotType.DETAIL,
        "camera_motion": CameraMotion.STATIC,
    },
    ProcessStage.DRYING: {
        "state": MaterialState.DRYING,
        "roles": [EditRole.PROCESS],
        "equipment_visible": True,
        "equipment_type": "heat_pump_dryer",
        "scene": "烘干房内部",
        "shot_type": ShotType.MEDIUM,
        "camera_motion": CameraMotion.STATIC,
    },
    ProcessStage.UNLOADING: {
        "state": MaterialState.DRIED,
        "roles": [EditRole.PROCESS, EditRole.TRANSITION],
        "equipment_visible": True,
        "equipment_type": "drying_tray",
        "scene": "烘干房内部",
        "shot_type": ShotType.MEDIUM,
        "camera_motion": CameraMotion.HANDHELD,
    },
    ProcessStage.FINISHED_PRODUCT: {
        "state": MaterialState.FINISHED,
        "roles": [EditRole.RESULT, EditRole.DETAIL],
        "equipment_visible": False,
        "equipment_type": None,
        "scene": "成品展示台",
        "shot_type": ShotType.CLOSE_UP,
        "camera_motion": CameraMotion.STATIC,
    },
    ProcessStage.PACKAGING: {
        "state": MaterialState.FINISHED,
        "roles": [EditRole.RESULT, EditRole.ENDING],
        "equipment_visible": True,
        "equipment_type": "packaging_machine",
        "scene": "包装车间",
        "shot_type": ShotType.MEDIUM,
        "camera_motion": CameraMotion.STATIC,
    },
    ProcessStage.EQUIPMENT: {
        "state": MaterialState.UNKNOWN,
        "roles": [EditRole.EQUIPMENT, EditRole.DETAIL],
        "equipment_visible": True,
        "equipment_type": "heat_pump_dryer",
        "scene": "烘干房内部",
        "shot_type": ShotType.CLOSE_UP,
        "camera_motion": CameraMotion.PAN,
    },
}


class MockVisionProvider(VisionProvider):
    """Offline, deterministic stand-in for Qwen / Volcano."""

    name = "mock"

    def __init__(self, *, seed: int = 20260914, timeout: float = 60.0, max_retries: int = 2, **options: Any) -> None:
        super().__init__(timeout=timeout, max_retries=max_retries, **options)
        self.seed = seed

    # -- 1. preview filter --------------------------------------------------
    async def preview_filter(self, request: PreviewFilterRequest) -> PreviewFilterResult:
        scenario = str(request.context.get("mock_scenario", SCENARIO_CLEAN))
        verdict = SCENARIO_VERDICTS.get(scenario, SCENARIO_VERDICTS[SCENARIO_CLEAN])
        accept, visible, relevance, subtitle, visual, quality, reason = verdict
        rng = self._rng("preview", request.platform_video_id)

        def jitter(value: float, spread: float = 0.04) -> float:
            return round(min(1.0, max(0.0, value + rng.uniform(-spread, spread))), 3)

        return PreviewFilterResult(
            accept=accept,
            material_visible=visible,
            material_relevance=jitter(relevance),
            subtitle_complexity=subtitle,
            visual_complexity=visual,
            quality_score=jitter(quality),
            reject_reason=reason,
        )

    # -- 2. segment detection ----------------------------------------------
    async def detect_segments(self, request: SegmentDetectionRequest) -> SegmentDetectionResult:
        scenario = str(request.context.get("mock_scenario", SCENARIO_CLEAN))
        if scenario == SCENARIO_NO_SEGMENT:
            return SegmentDetectionResult(segments=[])

        duration = float(request.duration)
        rng = self._rng("segments", request.platform_video_id)
        base = _material_base(request.material)
        form_word = f"{base}片" if base else request.material

        indexes = list(range(len(TEMPLATES)))
        rng.shuffle(indexes)
        indexes = indexes[: rng.randint(2, 3)]

        segments: list[DetectedSegment] = []
        cursor = rng.uniform(2.0, 6.0)
        for position, index in enumerate(indexes):
            template = TEMPLATES[index]
            segment_duration = template.duration or round(rng.uniform(5.0, 11.0), 1)
            start = round(min(cursor, max(0.0, duration - segment_duration - 0.5)), 2)
            end = round(min(start + segment_duration, duration), 2)
            if end - start < 1.0:
                continue
            segments.append(
                DetectedSegment(
                    start=start,
                    end=end,
                    description=template.text.format(material=request.material, base=base, form_word=form_word),
                    material_relevance=round(
                        min(1.0, template.relevance + rng.uniform(-0.03, 0.03)), 3
                    ),
                    usable=True,
                )
            )
            cursor += segment_duration + rng.uniform(3.0, 9.0)
            if position == 0 and duration > 40:
                # A too-long range: the normaliser must split it.
                long_start = round(min(cursor, duration - 20.0), 2)
                if long_start > 0 and long_start + 12.0 < duration:
                    segments.append(
                        DetectedSegment(
                            start=long_start,
                            end=round(long_start + 34.0, 2),
                            description=f"长时间连续拍摄{base}烘干过程",
                            material_relevance=0.8,
                            usable=True,
                        )
                    )
                    cursor = long_start + 36.0

        # A too-short range: dropped downstream (min clip duration).
        if duration > 12:
            short_start = round(min(cursor, duration - 2.0), 2)
            if short_start > 0:
                segments.append(
                    DetectedSegment(
                        start=short_start,
                        end=round(short_start + 1.4, 2),
                        description=f"{form_word}一闪而过",
                        material_relevance=0.42,
                        usable=False,
                    )
                )

        LOGGER.debug("mock detect_segments %s -> %s ranges", request.platform_video_id, len(segments))
        return SegmentDetectionResult(segments=segments)

    # -- 3. clip tagging ----------------------------------------------------
    async def tag_clip(self, request: ClipTaggingRequest) -> ClipTagging:
        scenario = str(request.context.get("mock_scenario", SCENARIO_CLEAN))
        rng = self._rng("tag", request.platform_video_id, request.start, request.end)
        description = request.segment_description or f"{request.material}烘干画面"
        stage = _infer_stage(description)
        preset = STAGE_PRESETS.get(stage, STAGE_PRESETS[ProcessStage.DRYING])
        # the mock observes the requested material here; a real provider must
        # derive it from the clip frames (Milestone 3.7, sections 1/2)
        base = _material_base(request.requested_material or request.material)

        form = _infer_form(description, base)
        state = _infer_state(description, preset["state"])
        people, people_count, person_role = _infer_people(description)

        if scenario == SCENARIO_BORDERLINE_SUBTITLE:
            subtitle_type, subtitle_score = SubtitleType.SINGLE_REGION, 0.38
        elif rng.random() < 0.45:
            subtitle_type, subtitle_score = SubtitleType.NONE, 0.02
        else:
            subtitle_type, subtitle_score = SubtitleType.BOTTOM_SIMPLE, 0.12

        material_relevance = round(
            min(1.0, max(0.5, request.segment_relevance + rng.uniform(-0.02, 0.02))), 3
        )
        visual_quality = round(rng.uniform(0.82, 0.94), 3)
        subtitle_cleanliness = round(max(0.0, 1.0 - subtitle_score), 3)
        stability = round(rng.uniform(0.85, 0.97), 3)
        composition = round(rng.uniform(0.8, 0.93), 3)
        scores = ClipScores(
            material_relevance=material_relevance,
            visual_quality=visual_quality,
            subtitle_cleanliness=subtitle_cleanliness,
            stability=stability,
            composition=composition,
        )
        scores.overall = scores.recompute_overall()

        return ClipTagging(
            material=base or request.material,
            material_form=form,
            material_state=state,
            process_stage=stage,
            equipment_type=preset["equipment_type"],
            equipment_visible=bool(preset["equipment_visible"]),
            scene=str(preset["scene"]),
            shot_type=preset["shot_type"],
            camera_motion=preset["camera_motion"],
            people=people,
            people_count=people_count,
            person_role=person_role,
            subtitle_type=subtitle_type,
            subtitle_score=subtitle_score,
            edit_roles=list(preset["roles"]),
            description=description,
            scores=scores,
        )

    # -- helpers ------------------------------------------------------------
    def _rng(self, *parts: object) -> random.Random:
        joined = "|".join(str(part) for part in (self.seed, *parts))
        return random.Random(int(hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12], 16))


def _material_base(material: str) -> str:
    """``苹果干`` -> ``苹果`` (the canonical ontology, section 18)."""

    from core.normalization import material_base

    return material_base(material)


def _infer_stage(description: str) -> ProcessStage:
    for keywords, stage in KEYWORD_STAGE_RULES:
        if any(keyword in description for keyword in keywords):
            return stage
    return ProcessStage.OTHER


def _infer_form(description: str, base: str) -> MaterialForm:
    if "片" in description:
        return MaterialForm.SLICE
    if "粉" in description:
        return MaterialForm.POWDER
    if "条" in description or "丝" in description:
        return MaterialForm.STRIP
    if "块" in description:
        return MaterialForm.PIECE
    if "叶" in description and len(base) < len(description):
        return MaterialForm.LEAF
    return MaterialForm.WHOLE


def _infer_state(description: str, default: MaterialState) -> MaterialState:
    if "烘干完成" in description or "成品" in description:
        return MaterialState.FINISHED
    if "半干" in description:
        return MaterialState.SEMI_DRIED
    if "逐渐干燥" in description or "热风循环" in description:
        return MaterialState.DRYING
    if "原料" in description:
        return MaterialState.FRESH
    if "清洗" in description or "切成" in description or "铺放" in description:
        return MaterialState.PREPARED
    return default


def _infer_people(description: str) -> tuple[bool, int, PersonRole]:
    if "工人" in description or "师傅" in description:
        return True, 1, PersonRole.WORKER
    if "主播" in description:
        return True, 1, PersonRole.HOST
    if "客户" in description:
        return True, 1, PersonRole.CUSTOMER
    if "人群" in description or "多人" in description:
        return True, 3, PersonRole.MULTIPLE_PEOPLE
    return False, 0, PersonRole.NONE
