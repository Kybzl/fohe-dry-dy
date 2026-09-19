"""Milestone 3.7 regressions: tagging quality, cost accounting, safe cleanup.

No test touches a paid provider: every AI call is served by a scripted local
provider, and every media operation by the mock toolkit.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from ai.base import (
    ClipTaggingRequest,
    PreviewFilterRequest,
    SegmentDetectionRequest,
    UsageInfo,
    VisionProvider,
)
from ai.gateway import AIGateway
from core.ai_stats import operation_stats, summarize_ai_usage
from core.dependencies import build_dependencies, build_library
from core.frame_policy import (
    PreviewFrameBand,
    clip_frame_ratios,
    clip_frame_timestamps,
    preview_frame_count,
)
from core.models import (
    ClipScores,
    ClipTagging,
    ComplexityLevel,
    DetectedSegment,
    EditRole,
    MaterialForm,
    MaterialState,
    PreviewFilterResult,
    PreviewFrame,
    ProcessStage,
    SegmentDetectionResult,
    ShotType,
    SubtitlePolicy,
    SubtitleType,
    TaskRequest,
    TaskStatus,
)
from core.provenance import (
    DOUYIN_REAL,
    LOCAL_TEST,
    MOCK,
    ClipIntegrity,
    classify_provenance,
    demo_removal_verdict,
)
from core.tag_audit import TagRow, analyse_tags
from core.task_runner import TaskRunner


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# scripted provider
# ---------------------------------------------------------------------------
class ScriptedProvider(VisionProvider):
    """Deterministic stand-in that records what the pipeline asked for."""

    name = "scripted"

    def __init__(
        self,
        *,
        accept: bool = True,
        state: MaterialState = MaterialState.DRYING,
        description: str = "",
        segments: int = 1,
        identical_description: bool = False,
    ) -> None:
        super().__init__(timeout=5.0, max_retries=1)
        self.accept = accept
        self.state = state
        self.description = description
        self.segments = segments
        self.identical_description = identical_description
        self.calls: dict[str, int] = {"preview_filter": 0, "segment_detection": 0, "clip_tagging": 0}
        self.tagging_requests: list[ClipTaggingRequest] = []
        self._tag_index = 0
        self.usage_per_call = 120

    def consume_usage(self) -> UsageInfo | None:
        # a real provider reports usage for every call; the scripted one does too
        return UsageInfo(
            model="scripted-model",
            prompt_tokens=self.usage_per_call - 20,
            completion_tokens=20,
            total_tokens=self.usage_per_call,
        )

    async def preview_filter(self, request: PreviewFilterRequest) -> PreviewFilterResult:
        self.calls["preview_filter"] += 1
        return PreviewFilterResult(
            accept=self.accept,
            material_visible=True,
            material_relevance=0.9 if self.accept else 0.1,
            subtitle_complexity=ComplexityLevel.LOW,
            visual_complexity=ComplexityLevel.LOW,
            quality_score=0.9 if self.accept else 0.2,
        )

    async def detect_segments(self, request: SegmentDetectionRequest) -> SegmentDetectionResult:
        self.calls["segment_detection"] += 1
        return SegmentDetectionResult(
            segments=[
                DetectedSegment(
                    start=2.0 + index,
                    end=9.0 + index,
                    description=f"切片铺盘 #{request.platform_video_id}-{index}",
                    material_relevance=0.93,
                )
                for index in range(self.segments)
            ]
        )

    async def tag_clip(self, request: ClipTaggingRequest) -> ClipTagging:
        self.calls["clip_tagging"] += 1
        self.tagging_requests.append(request)
        self._tag_index += 1
        if self.identical_description:
            description = "苹果片均匀铺在多层不锈钢烘干托盘上，镜头稳定，无人物。"
            scores = ClipScores(
                material_relevance=0.96,
                visual_quality=0.90,
                subtitle_cleanliness=0.87,
                stability=0.92,
                composition=0.88,
                overall=0.91,
            )
        else:
            description = self.description or (
                f"第 {self._tag_index} 段画面：苹果片正在烘干，帧数 {len(request.frames)}"
            )
            scores = ClipScores(
                material_relevance=0.80 + self._tag_index * 0.02,
                visual_quality=0.70 + self._tag_index * 0.01,
                subtitle_cleanliness=0.9,
                stability=0.6 + self._tag_index * 0.05,
                composition=0.75,
                overall=0.70 + self._tag_index * 0.03,
            )
        return ClipTagging(
            # deliberately echo the *requested* material: the pipeline must map
            # it to the canonical tag without touching the observed state
            material=request.requested_material or request.material,
            material_form=MaterialForm.SLICE,
            material_state=self.state,
            process_stage=ProcessStage.TRAY_ARRANGEMENT,
            shot_type=ShotType.CLOSE_UP,
            subtitle_type=SubtitleType.BOTTOM_SIMPLE,
            subtitle_score=0.1,
            edit_roles=[EditRole.PROCESS],
            description=description,
            scores=scores,
        )


def _deps(settings, provider: ScriptedProvider):
    """Dependencies whose AI stages all use ``provider``."""

    from analyzers.preview_filter import PreviewFilter
    from analyzers.video_analyzer import VideoAnalyzer
    from media.frame_sampler import FrameSampler

    deps = build_dependencies(
        settings, source_name="mock", provider_name="mock", media_backend="mock"
    )
    gateway = AIGateway(provider, max_retries=1, timeout=5.0, on_call=deps.library.add_ai_run)
    deps.gateway = gateway
    deps.preview_filter = PreviewFilter(gateway, policy=SubtitlePolicy.STRICT, max_frames=8)
    deps.analyzer = VideoAnalyzer(
        gateway,
        FrameSampler(deps.toolkit, max_frames=8),
        frame_count=8,
        min_segment_duration=3.0,
        max_segment_duration=15.0,
    )
    return deps


def _collect(settings, provider: ScriptedProvider, *, target: int = 1, **request_kwargs):
    from core.orchestrator import CollectionOrchestrator

    deps = _deps(settings, provider)
    request = TaskRequest(
        material=request_kwargs.pop("material", "苹果干"),
        target_clip_count=target,
        subtitle_policy=SubtitlePolicy.STRICT,
        library_root=settings.paths.library_root,
        source="mock",
        provider="mock",
        media_backend="mock",
        **request_kwargs,
    )
    result = run(CollectionOrchestrator(deps).collect(request))
    return result, deps


# ---------------------------------------------------------------------------
# 3/4. prompt v2 replaces the copyable example with rules
# ---------------------------------------------------------------------------
def test_clip_tagging_v2_prompt_replaces_the_copyable_example() -> None:
    from ai.schemas import load_prompt

    v1 = load_prompt("clip_tagging", "clip_tagging_v1")
    v2 = load_prompt("clip_tagging", "clip_tagging_v2")

    copied_description = "苹果片均匀铺在多层不锈钢烘干托盘上，镜头稳定，无人物。"
    assert copied_description in v1, "v1 is kept unchanged for A/B comparison"
    assert copied_description not in v2
    assert "0.96" not in v2 and "0.90" not in v2, "no copyable score vector"
    assert "独立观察" in v2 and "不要" in v2
    # observe-before-classify order and the score rubric must be present
    for token in ("material_form", "material_state", "process_stage", "overall", "0.95"):
        assert token in v2
    assert "不允许" in v2, "the prompt must forbid deriving state from the request"


def test_clip_tagging_v2_is_the_default_and_v1_still_loadable() -> None:
    from ai.schemas import prompt_version

    assert prompt_version("clip_tagging") == "clip_tagging_v2"
    assert prompt_version("clip_tagging", "v1") == "clip_tagging_v1"
    assert prompt_version("preview_filter") == "preview_filter_v1"


def test_request_can_pin_a_prompt_version() -> None:
    from ai.schemas import prompt_version

    provider = ScriptedProvider()
    request = ClipTaggingRequest(
        material="苹果干", start=0, end=5, prompt_version="clip_tagging_v1"
    )
    assert provider.prompt_version_for("clip_tagging", request) == "clip_tagging_v1"
    assert provider.prompt_version_for("clip_tagging") == "clip_tagging_v2"
    assert prompt_version("clip_tagging", request.prompt_version) == "clip_tagging_v1"


# ---------------------------------------------------------------------------
# 1/2. observation beats task intent
# ---------------------------------------------------------------------------
def test_observed_state_is_not_forced_by_the_requested_material(settings) -> None:
    provider = ScriptedProvider(state=MaterialState.FRESH)
    result, deps = _collect(settings, provider, material="苹果干")

    assert result.clips, "the scripted run must produce a clip"
    clip = result.clips[0]
    assert clip.material_state is MaterialState.FRESH, "observed state must survive"
    assert clip.material == "苹果", "the canonical material tag still normalises"
    assert clip.library_category == "苹果干", "the physical category keeps the intent"


def test_tagging_request_separates_intent_from_observation(settings) -> None:
    provider = ScriptedProvider()
    _collect(settings, provider, material="苹果干")
    request = provider.tagging_requests[0]
    assert request.requested_material == "苹果干"
    assert request.frames, "the final clip frames must be sent to the tagger"


# ---------------------------------------------------------------------------
# 7. final clip frames
# ---------------------------------------------------------------------------
def test_clip_tagging_uses_frames_from_the_final_clip(settings) -> None:
    provider = ScriptedProvider()
    result, deps = _collect(settings, provider)

    clip = result.clips[0]
    assert provider.tagging_requests[0].frames, "tagging must see the final clip"
    rows = deps.library.list_ai_runs(task_id=result.task_id)
    tagging_rows = [row for row in rows if row["operation"] == "clip_tagging"]
    assert tagging_rows and tagging_rows[0]["input_frame_count"] == 4
    # frames are temporary: the cache is clean after the run
    assert clip.file_path.exists()
    leftovers = [path for path in settings.paths.cache_dir.rglob("*") if path.is_file()]
    assert leftovers == [], leftovers


def test_clip_frame_sampling_covers_the_clip_band() -> None:
    stamps = clip_frame_timestamps(10.0, 20.0, 4)
    assert stamps == [12.0, 14.0, 16.0, 18.0]
    assert clip_frame_ratios(4) == [0.2, 0.4, 0.6, 0.8]
    assert clip_frame_ratios(2) == [0.2, 0.8]
    # never sample the very edge of the clip
    assert clip_frame_timestamps(0.0, 1.0, 4)[0] > 0.0
    assert clip_frame_timestamps(0.0, 1.0, 4)[-1] < 1.0


def test_clipper_reuses_one_extraction_pass_for_thumbnail_and_tagging(settings, tmp_path) -> None:
    from media.clipper import ClipCutter
    from media.ffmpeg import MockMediaToolkit
    from core.models import SegmentTiming

    toolkit = MockMediaToolkit()
    cutter = ClipCutter(toolkit, thumbnail_candidates=5, tagging_frame_ratios=(0.2, 0.4, 0.6, 0.8))
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stub")
    timing = SegmentTiming(start=0.0, end=10.0)
    frames_dir = tmp_path / "frames"
    artifact = run(
        cutter.cut(
            source_video=source,
            timing=timing,
            dest=tmp_path / "clip.mp4",
            thumbnail_dest=tmp_path / "thumb.jpg",
            content_key="key",
            tagging_frames_dir=frames_dir,
        )
    )
    assert artifact.thumbnail_path is not None and artifact.thumbnail_path.exists()
    assert len(artifact.tagging_frames) == 4
    # 5 thumbnail candidates + 4 tagging stamps share 0.4/0.6 -> 7 extractions
    extractions = [
        call
        for call in toolkit.calls
        if call[0] in ("extract_representative_frame", "make_thumbnail")
    ]
    assert len(extractions) == 7, "each timestamp is extracted exactly once"
    assert frames_dir.exists()


def test_clipper_bounds_tagging_frames_but_keeps_a_sharp_thumbnail(settings, tmp_path) -> None:
    from core.models import SegmentTiming
    from media.clipper import ClipCutter
    from media.ffmpeg import MockMediaToolkit

    toolkit = MockMediaToolkit()
    cutter = ClipCutter(
        toolkit,
        thumbnail_candidates=3,
        tagging_frame_ratios=(0.2, 0.4, 0.6, 0.8),
        tagging_frame_max_width=320,
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stub")
    artifact = run(
        cutter.cut(
            source_video=source,
            timing=SegmentTiming(start=0.0, end=8.0),
            dest=tmp_path / "clip.mp4",
            thumbnail_dest=tmp_path / "thumb.jpg",
            content_key="key",
            tagging_frames_dir=tmp_path / "frames",
        )
    )
    assert len(artifact.tagging_frames) == 4
    bounded = [call for call in toolkit.calls if call[0] == "extract_frames"]
    # union of 3 thumbnail candidates (0.3/0.5/0.7) and 4 tagging positions
    # (0.2/0.4/0.6/0.8) = 7 unique timestamps, each extracted exactly once
    assert len(bounded) == 7, "one bounded extraction per unique timestamp"
    # the delivered thumbnail is re-extracted at full resolution
    assert any(call[0] == "make_thumbnail" for call in toolkit.calls)
    # the real FFmpeg path applies the bound through ``scale``
    from media.ffmpeg import FFmpegToolkit

    args = FFmpegToolkit.build_frame_args(Path("clip.mp4"), 1.0, Path("f.jpg"), size=320)
    assert "-vf" in args and any(str(item).startswith("scale=320") for item in args)


# ---------------------------------------------------------------------------
# 11. adaptive preview cost
# ---------------------------------------------------------------------------
def test_adaptive_preview_frame_count() -> None:
    bands = (
        PreviewFrameBand(15.0, 6),
        PreviewFrameBand(45.0, 8),
        PreviewFrameBand(90.0, 10),
        PreviewFrameBand(0.0, 12),
    )
    assert preview_frame_count(8, bands=bands, ceiling=16) == 6
    assert preview_frame_count(30, bands=bands, ceiling=16) == 8
    assert preview_frame_count(60, bands=bands, ceiling=16) == 10
    assert preview_frame_count(600, bands=bands, ceiling=16) == 12
    assert preview_frame_count(None, bands=bands, ceiling=16) == 6, "unknown duration stays cheap"
    assert preview_frame_count(600, bands=bands, ceiling=8) == 8, "ceiling still wins"


def test_orchestrator_preview_count_respects_configuration(settings) -> None:
    from core.orchestrator import CollectionOrchestrator

    deps = build_dependencies(
        settings, source_name="mock", provider_name="mock", media_backend="mock"
    )
    orchestrator = CollectionOrchestrator(deps)
    assert orchestrator._preview_frame_count(10) == 6  # noqa: SLF001
    assert orchestrator._preview_frame_count(60) == 10  # noqa: SLF001
    assert orchestrator._preview_frame_count(120) == 12  # noqa: SLF001
    settings.analysis.preview_frame_adaptive = False
    assert orchestrator._preview_frame_count(10) == 16  # noqa: SLF001 - the ceiling
    settings.analysis.preview_max_frames = 8
    assert orchestrator._preview_frame_count(10) == 8  # noqa: SLF001


# ---------------------------------------------------------------------------
# 12. segment detection only after acceptance
# ---------------------------------------------------------------------------
def test_rejected_preview_never_runs_segment_or_tagging(settings) -> None:
    provider = ScriptedProvider(accept=False)
    result, deps = _collect(settings, provider)

    assert result.clips == []
    assert provider.calls["preview_filter"] >= 1
    assert provider.calls["segment_detection"] == 0
    assert provider.calls["clip_tagging"] == 0
    rows = deps.library.list_ai_runs(task_id=result.task_id)
    assert {row["operation"] for row in rows} == {"preview_filter"}


# ---------------------------------------------------------------------------
# 14/15/16/17. cost accounting and audit linkage
# ---------------------------------------------------------------------------
def test_operation_stats_and_tokens_per_clip(settings) -> None:
    provider = ScriptedProvider()
    result, deps = _collect(settings, provider)
    rows = deps.library.list_ai_runs(task_id=result.task_id)
    summary = summarize_ai_usage(rows, saved_clips=len(result.clips))

    ops = summary["operations"]
    assert ops["preview_filter"]["calls"] >= 1
    assert ops["segment_detection"]["calls"] >= 1
    assert ops["clip_tagging"]["calls"] == len(result.clips)
    assert ops["clip_tagging"]["avg_tokens_per_call"] == 120.0
    assert summary["total_tokens"] == 120 * len(rows)
    assert summary["tokens_per_saved_clip"] == round(summary["total_tokens"] / len(result.clips), 1)
    assert summary["failures"] == 0


def test_clip_tagging_audit_row_references_the_clip(settings) -> None:
    provider = ScriptedProvider(segments=2)
    result, deps = _collect(settings, provider, target=2)
    clip_ids = {clip.id for clip in result.clips}
    assert clip_ids

    rows = deps.library.list_ai_runs(task_id=result.task_id)
    tagging = [row for row in rows if row["operation"] == "clip_tagging"]
    previews = [row for row in rows if row["operation"] == "preview_filter"]
    segments = [row for row in rows if row["operation"] == "segment_detection"]

    assert tagging, "each saved clip is tagged"
    assert all(row["clip_id"] in clip_ids for row in tagging)
    assert all(row["source_video_id"] is not None for row in tagging)
    assert all(row["clip_id"] is None for row in previews + segments)
    assert all(row["source_video_id"] is not None for row in previews + segments)


def test_tag_prompt_version_is_persisted_per_clip(settings) -> None:
    provider = ScriptedProvider()
    result, deps = _collect(settings, provider)
    clip = deps.library.get_clip(result.clips[0].id)
    assert clip is not None
    assert clip.tag_prompt_version == "clip_tagging_v2"


# ---------------------------------------------------------------------------
# 5/27. identical output is a diagnostic, not a rejection
# ---------------------------------------------------------------------------
def test_identical_tagging_output_is_flagged_but_not_rejected(settings) -> None:
    provider = ScriptedProvider(segments=1, identical_description=True)
    result, _deps_used = _collect(settings, provider, target=2)

    assert len(result.clips) == 2, "identical output must never be rejected"
    assert any("标签诊断" in message for message in result.messages)


def test_tag_audit_detects_duplicate_descriptions_and_scores() -> None:
    rows = [
        TagRow(
            clip_id=1,
            description="同一句话",
            process_stage="drying",
            scores=(0.9, 0.9, 0.9, 0.9, 0.9, 0.9),
        ),
        TagRow(
            clip_id=2,
            description="同一句话",
            process_stage="drying",
            scores=(0.9, 0.9, 0.9, 0.9, 0.9, 0.9),
        ),
    ]
    report = analyse_tags(rows, version="clip_tagging_v2")
    assert report.clips == 2
    assert report.distinct_descriptions == 1
    assert report.distinct_score_vectors == 1
    assert report.single_stage is True
    assert len(report.flag_lines()) == 3
    assert report.description_diversity == 0.5


# ---------------------------------------------------------------------------
# 18/19/31. physical category vs semantic observation
# ---------------------------------------------------------------------------
def test_physical_folder_follows_the_library_category(settings) -> None:
    provider = ScriptedProvider()
    result, _deps_used = _collect(
        settings, provider, material="苹果片", library_category="苹果干"
    )
    clip = result.clips[0]
    assert clip.library_category == "苹果干"
    assert "苹果干" in str(clip.file_path)
    assert clip.material == "苹果"


def test_semantic_material_and_category_are_independent(settings) -> None:
    from core.models import ClipQuery

    provider = ScriptedProvider()
    result, deps = _collect(
        settings, provider, material="苹果片", library_category="苹果干"
    )
    assert deps.library.query_clips(ClipQuery(library_category="苹果干"))
    assert deps.library.query_clips(ClipQuery(material="苹果"))
    assert deps.library.query_clips(ClipQuery(library_category="苹果片")) == []
    assert deps.library.list_clips(material="苹果"), "cross-state material search works"


def test_cross_state_material_search_spans_categories(settings) -> None:
    from core.models import ClipQuery

    provider = ScriptedProvider(state=MaterialState.FRESH)
    result, deps = _collect(settings, provider, material="苹果干")
    fresh = deps.library.query_clips(
        ClipQuery(material="苹果", material_state=MaterialState.FRESH)
    )
    assert fresh and fresh[0].id == result.clips[0].id


# ---------------------------------------------------------------------------
# 21/22/23. provenance and safe removal
# ---------------------------------------------------------------------------
def test_provenance_classification() -> None:
    assert classify_provenance("douyin", "7652321152866089979") == DOUYIN_REAL
    assert classify_provenance("douyin", "dyd668a08fa18c3a") == MOCK
    assert classify_provenance("local", "local_abc123") == LOCAL_TEST
    assert classify_provenance("mock", "x") == MOCK
    assert classify_provenance("douyin", "7652321152866089979", source_adapter="mock") == MOCK
    assert classify_provenance("weibo", "1") == "unknown"


def test_demo_removal_never_targets_real_clips() -> None:
    placeholder = ClipIntegrity(width=640, height=360, size_bytes=1024)
    real = ClipIntegrity(width=1080, height=1920, size_bytes=6_000_000)

    removable, reason = demo_removal_verdict(provenance=MOCK, integrity=placeholder)
    assert removable and "mock" in reason

    removable, reason = demo_removal_verdict(provenance=DOUYIN_REAL, integrity=placeholder)
    assert not removable and "never removed" in reason, (
        "a low resolution real clip is still protected"
    )

    removable, _reason = demo_removal_verdict(provenance=LOCAL_TEST, integrity=real)
    assert not removable, "local tests need an explicit opt-in"
    removable, _reason = demo_removal_verdict(
        provenance=LOCAL_TEST, integrity=real, include_local_tests=True
    )
    assert removable


def _persist_clip(library, *, platform: str, video_id: str, root: Path, provenance: str = ""):
    """Write a real clip row + files so removal can be exercised."""

    from core.models import ClipArtifact, ClipTagging, SegmentTiming

    root.mkdir(parents=True, exist_ok=True)
    video = root / f"{video_id}.mp4"
    thumb = root / f"{video_id}.jpg"
    video.write_bytes(b"video-bytes")
    thumb.write_bytes(b"thumb-bytes")
    tagging = ClipTagging(
        material="苹果",
        material_form=MaterialForm.SLICE,
        description="测试片段",
        scores=ClipScores(material_relevance=0.9, visual_quality=0.9, overall=0.9),
    )
    artifact = ClipArtifact(
        file_path=video,
        thumbnail_path=thumb,
        duration=6.0,
        width=1080,
        height=1920,
        sha256="deadbeef",
        phash="0123456789abcdef",
    )
    return library.insert_clip(
        task_id=None,
        source_video_id=None,
        platform=platform,
        platform_video_id=video_id,
        source_url=f"https://www.douyin.com/video/{video_id}",
        tagging=tagging,
        timing=SegmentTiming(start=0.0, end=6.0),
        artifact=artifact,
        content_key="ck-test",
        library_category="苹果干",
        provenance=provenance,
        tag_prompt_version="clip_tagging_v2",
    )


def test_remove_clip_is_complete_and_keeps_provenance(settings, tmp_path) -> None:
    library = build_library(settings)
    library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="7652321152866089979",
        source_url="https://www.douyin.com/video/7652321152866089979",
    )
    source = library.get_source_video("douyin", "7652321152866089979")
    clip_id = _persist_clip(
        library,
        platform="douyin",
        video_id="7652321152866089979",
        root=tmp_path / "clips",
        provenance=DOUYIN_REAL,
    )
    clip = library.get_clip(clip_id)
    assert clip is not None
    library.add_ai_run(
        {
            "task_id": None,
            "source_video_id": source.id if source else None,
            "clip_id": clip_id,
            "provider": "scripted",
            "model": "m",
            "operation": "clip_tagging",
            "prompt_version": "clip_tagging_v2",
            "started_at": "2026-01-01T00:00:00+00:00",
            "status": "ok",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )

    report = library.remove_clip(clip_id)
    assert report.found
    assert len(report.removed_files) == 2
    assert not Path(clip.file_path).exists()
    assert clip.thumbnail_path is not None and not Path(clip.thumbnail_path).exists()
    assert library.get_clip(clip_id) is None
    assert library.count_clips() == 0
    assert library.get_source_video("douyin", "7652321152866089979") is not None, (
        "source provenance must survive a clip removal"
    )
    runs = library.list_ai_runs(clip_id=None, task_id=None)
    assert all(row["clip_id"] is None for row in runs), "audit rows are unlinked, not deleted"


def test_remove_demo_clips_only_removes_mock(settings, tmp_path, capsys) -> None:
    import app as app_module

    library = build_library(settings)
    mock_id = _persist_clip(
        library, platform="douyin", video_id="dyd668a08fa18c3a", root=tmp_path / "mock",
        provenance=MOCK,
    )
    real_id = _persist_clip(
        library, platform="douyin", video_id="7652321152866089979", root=tmp_path / "real",
        provenance=DOUYIN_REAL,
    )
    local_id = _persist_clip(
        library, platform="local", video_id="local_abc", root=tmp_path / "local",
        provenance=LOCAL_TEST,
    )

    code = app_module.run_remove_demo_clips(settings, confirm=False, include_local=False)
    assert code == 0
    output = capsys.readouterr().out
    assert "dry run" in output and f"clip #{mock_id}" in output
    assert library.get_clip(mock_id) is not None, "a dry run must not delete anything"

    code = app_module.run_remove_demo_clips(settings, confirm=True, include_local=False)
    assert code == 0
    out = capsys.readouterr().out
    assert f"已删除 clip #{mock_id}" in out
    assert library.get_clip(mock_id) is None
    assert library.get_clip(real_id) is not None, "real Douyin clips are never removed"
    assert library.get_clip(local_id) is not None, "local tests need the explicit opt-in"


def test_backfill_fills_only_empty_metadata(settings, tmp_path) -> None:
    library = build_library(settings)
    clip_id = _persist_clip(
        library,
        platform="douyin",
        video_id="7652321152866089979",
        root=tmp_path / "苹果干" / "clips",
    )
    # simulate a pre-v4 row: clear the new columns
    library.database.execute(
        "UPDATE clips SET library_category = '', provenance = '', tag_prompt_version = '' "
        "WHERE id = ?",
        (clip_id,),
    )
    library.add_ai_run(
        {
            "task_id": None,
            "source_video_id": None,
            "clip_id": clip_id,
            "provider": "scripted",
            "model": "m",
            "operation": "clip_tagging",
            "prompt_version": "clip_tagging_v1",
            "started_at": "2026-01-01T00:00:00+00:00",
            "status": "ok",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )

    changes = library.backfill_clip_metadata(apply=False)
    assert {change["field"] for change in changes} == {
        "library_category",
        "provenance",
        "tag_prompt_version",
    }
    assert library.get_clip(clip_id).library_category == "", "dry run writes nothing"

    library.backfill_clip_metadata(apply=True)
    after = library.get_clip(clip_id)
    assert after is not None
    assert after.library_category == "苹果干"
    assert after.provenance == DOUYIN_REAL
    assert after.tag_prompt_version == "clip_tagging_v1"
    # a filled row is not touched again
    assert library.backfill_clip_metadata(apply=False) == []


def test_backfill_skips_ambiguous_prompt_versions(settings, tmp_path) -> None:
    library = build_library(settings)
    clip_id = _persist_clip(
        library,
        platform="douyin",
        video_id="7652321152866089979",
        root=tmp_path / "苹果干" / "clips",
    )
    library.database.execute(
        "UPDATE clips SET library_category = '', provenance = '', tag_prompt_version = '' "
        "WHERE id = ?",
        (clip_id,),
    )
    for index, version in enumerate(("clip_tagging_v1", "clip_tagging_v2")):
        library.add_ai_run(
            {
                "task_id": None,
                "source_video_id": None,
                "clip_id": clip_id,
                "provider": "scripted",
                "model": "m",
                "operation": "clip_tagging",
                "prompt_version": version,
                "started_at": f"2026-01-0{index + 1}T00:00:00+00:00",
                "status": "ok",
                "created_at": f"2026-01-0{index + 1}T00:00:00+00:00",
            }
        )
    changes = library.backfill_clip_metadata(apply=True)
    prompt_change = [
        change for change in changes if change["field"] == "tag_prompt_version"
    ][0]
    assert prompt_change["new"] == "" and "ambiguous" in prompt_change["reason"]
    after = library.get_clip(clip_id)
    assert after is not None and after.tag_prompt_version == "", (
        "an ambiguous prompt history is never guessed"
    )
    assert after.library_category == "苹果干" and after.provenance == DOUYIN_REAL


# ---------------------------------------------------------------------------
# 28/29. retagging
# ---------------------------------------------------------------------------
def test_retag_preserves_media_and_provenance(settings, tmp_path) -> None:
    from core.retag import ClipRetagger

    library = build_library(settings)
    clip_id = _persist_clip(
        library,
        platform="douyin",
        video_id="7652321152866089979",
        root=tmp_path / "clips",
        provenance=DOUYIN_REAL,
    )
    before = library.get_clip(clip_id)
    assert before is not None

    provider = ScriptedProvider(state=MaterialState.PREPARED, description="重打标后的新描述")
    gateway = AIGateway(provider, max_retries=1, timeout=5.0, on_call=library.add_ai_run)
    retagger = ClipRetagger(
        gateway=gateway,
        toolkit=deps_toolkit(settings),
        library=library,
        frames_dir=settings.paths.cache_dir / "frames",
    )
    outcome = run(retagger.retag(clip_id, version="clip_tagging_v2", apply=True))
    assert outcome.ok and outcome.applied
    assert outcome.tokens == 120

    after = library.get_clip(clip_id)
    assert after is not None
    assert Path(after.file_path).exists(), "the video file is never touched"
    assert after.sha256 == before.sha256
    assert after.provenance == DOUYIN_REAL
    assert after.library_category == before.library_category
    assert after.created_at == before.created_at
    assert after.description == "重打标后的新描述"
    assert after.material_state is MaterialState.PREPARED
    assert after.tag_prompt_version == "clip_tagging_v2"
    # the new audit row is linked to the clip
    rows = library.ai_runs_for_clip(clip_id)
    assert rows and rows[-1]["operation"] == "clip_tagging"
    assert rows[-1]["prompt_version"] == "clip_tagging_v2"
    assert not [p for p in (settings.paths.cache_dir).rglob("*") if p.is_file()]


def deps_toolkit(settings):
    from core.dependencies import build_toolkit

    return build_toolkit(settings, backend="mock")


# ---------------------------------------------------------------------------
# 30. schema migration
# ---------------------------------------------------------------------------
def test_schema_migration_is_idempotent_on_an_existing_database(tmp_path) -> None:
    from storage.database import Database
    from storage.schema import SCHEMA_VERSION

    database = Database(tmp_path / "old.db")
    database.initialize()
    with database.transaction() as connection:
        for column in ("library_category", "provenance", "tag_prompt_version"):
            try:
                connection.execute(f"ALTER TABLE clips DROP COLUMN {column}")
            except sqlite3.OperationalError:  # pragma: no cover - very old sqlite
                pytest.skip("this SQLite build cannot drop columns")
        connection.execute("ALTER TABLE tasks DROP COLUMN request_json")
    database.initialize()  # re-adds the columns, no error, no duplicate tables
    with database.transaction() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(clips)")}
        task_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(tasks)")
        }
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone()["value"]
    assert {"library_category", "provenance", "tag_prompt_version"} <= columns
    assert "request_json" in task_columns
    assert version == str(SCHEMA_VERSION)
    assert database.missing_tables() == []


# ---------------------------------------------------------------------------
# task level reporting
# ---------------------------------------------------------------------------
def test_operation_stats_handles_missing_usage() -> None:
    stats = operation_stats(
        [
            {"operation": "clip_tagging", "status": "ok"},
            {"operation": "clip_tagging", "status": "timeout", "latency_ms": 100},
        ]
    )
    assert stats["clip_tagging"]["calls"] == 2
    assert stats["clip_tagging"]["failures"] == 1
    assert stats["clip_tagging"]["avg_tokens_per_call"] == 0.0
    assert stats["clip_tagging"]["avg_latency_ms"] == 100.0


def test_task_cost_report_ignores_evaluation_runs() -> None:
    rows = [
        {
            "operation": "clip_tagging",
            "status": "ok",
            "total_tokens": 100,
            "origin": "pipeline",
        },
        {
            "operation": "clip_tagging",
            "status": "ok",
            "total_tokens": 900,
            "origin": "evaluation",
        },
    ]
    production = summarize_ai_usage(rows, saved_clips=1)
    assert production["total_tokens"] == 100
    assert production["tokens_per_saved_clip"] == 100.0
    everything = summarize_ai_usage(rows, saved_clips=1, pipeline_only=False)
    assert everything["total_tokens"] == 1000


def test_retag_and_evaluation_runs_are_marked_in_the_audit(settings, tmp_path) -> None:
    from core.retag import ClipRetagger

    library = build_library(settings)
    clip_id = _persist_clip(
        library,
        platform="douyin",
        video_id="7652321152866089979",
        root=tmp_path / "clips",
        provenance=DOUYIN_REAL,
    )
    provider = ScriptedProvider()
    gateway = AIGateway(provider, max_retries=1, timeout=5.0, on_call=library.add_ai_run)
    retagger = ClipRetagger(
        gateway=gateway,
        toolkit=deps_toolkit(settings),
        library=library,
        frames_dir=settings.paths.cache_dir / "frames",
    )
    run(retagger.retag(clip_id, apply=False))
    rows = library.ai_runs_for_clip(clip_id)
    assert [row["origin"] for row in rows] == ["retag"]

    run(retagger.compare([clip_id], versions=("clip_tagging_v1", "clip_tagging_v2")))
    origins = {row["origin"] for row in library.ai_runs_for_clip(clip_id)}
    assert origins == {"retag", "evaluation"}
    assert {row["prompt_version"] for row in library.ai_runs_for_clip(clip_id)} >= {
        "clip_tagging_v1",
        "clip_tagging_v2",
    }


def test_task_report_includes_per_operation_costs(settings, capsys) -> None:
    import app as app_module

    runner = TaskRunner(settings)
    provider = ScriptedProvider()
    deps = _deps(settings, provider)
    from core.orchestrator import CollectionOrchestrator

    request = TaskRequest(
        material="苹果干",
        target_clip_count=1,
        subtitle_policy=SubtitlePolicy.STRICT,
        library_root=settings.paths.library_root,
        source="mock",
        provider="mock",
        media_backend="mock",
    )
    result = run(CollectionOrchestrator(deps).collect(request))
    runner.library = deps.library
    app_module._print_result(result, runner)
    output = capsys.readouterr().out
    assert "AI 调用分项:" in output
    assert "clip_tagging" in output
    assert "tokens/clip=" in output
