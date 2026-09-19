"""UI helpers and (when gradio is installed) the Blocks application."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.models import ClipRecord, MaterialForm, PipelineStats, ProcessStage, SubtitleType
from ui import gradio_app
from ui.gradio_app import (
    CLIP_TABLE_HEADERS,
    ENABLED_SOURCES,
    SOURCE_CHOICES,
    SUBTITLE_CHOICES,
    clip_table_rows,
    gallery_items,
    library_path_display,
    parse_library_root,
    stats_markdown,
)


def _clip(tmp_path: Path, clip_id: int = 1) -> ClipRecord:
    image = tmp_path / f"clip_{clip_id}.jpg"
    image.write_bytes(b"thumb")
    video = tmp_path / f"clip_{clip_id}.mp4"
    video.write_bytes(b"video")
    return ClipRecord(
        id=clip_id,
        material="苹果",
        material_form=MaterialForm.SLICE,
        process_stage=ProcessStage.TRAY_ARRANGEMENT,
        subtitle_type=SubtitleType.BOTTOM_SIMPLE,
        duration=8.0,
        source_start=5.0,
        source_end=13.0,
        overall_score=0.9,
        file_path=video,
        thumbnail_path=image,
    )


def test_stats_markdown_lists_the_required_counters() -> None:
    markdown = stats_markdown(PipelineStats(clips_saved=5), status="已完成")
    assert "已完成" in markdown
    for label in ("搜索候选", "已预筛", "字幕淘汰", "去重淘汰", "进入分析", "最终保存数量"):
        assert label in markdown
    assert "| 最终保存数量 | 5 |" in markdown


def test_clip_table_rows_match_the_headers(tmp_path: Path) -> None:
    rows = clip_table_rows([_clip(tmp_path, 1), _clip(tmp_path, 2)])
    assert len(rows) == 2
    assert len(rows[0]) == len(CLIP_TABLE_HEADERS)
    assert rows[0][0] == 1
    assert rows[0][1] == "苹果"
    assert rows[0][5] == "5.0-13.0"


def test_gallery_items_skip_missing_files(tmp_path: Path) -> None:
    clip = _clip(tmp_path, 1)
    broken = clip.model_copy(update={"thumbnail_path": tmp_path / "nope.jpg", "file_path": tmp_path / "nope.mp4"})
    items = gallery_items([clip, broken])
    assert len(items) == 1
    assert items[0][0] == str(clip.thumbnail_path)
    assert "#1" in items[0][1]


def test_source_and_subtitle_choices() -> None:
    assert [value for _, value in SUBTITLE_CHOICES] == ["strict", "balanced", "loose", "off"]
    # Douyin is the default acquisition source in Milestone 3; local files and
    # mock data stay available for debugging.
    assert [value for _, value, _ in SOURCE_CHOICES] == ["douyin", "local", "mock"]
    assert [value for _, value, enabled in SOURCE_CHOICES if enabled] == [
        "douyin",
        "local",
        "mock",
    ]
    assert ENABLED_SOURCES[0] == "抖音"


def test_library_path_display_uses_the_configured_root() -> None:
    from core.config import load_settings

    settings = load_settings(overrides={"paths": {"library_root": "D:/素材库2"}})
    assert library_path_display(settings) == "D:/素材库2"


def test_parse_library_root_handles_quotes_and_blank_input(settings) -> None:
    assert parse_library_root('  "D:/素材库2"  ', settings) == Path("D:/素材库2")
    assert parse_library_root("'D:/素材库2'", settings) == Path("D:/素材库2")
    assert parse_library_root("", settings) == settings.paths.library_root
    assert parse_library_root(None, settings) == settings.paths.library_root
    assert parse_library_root("library", settings) == Path("library")


@pytest.mark.skipif(not gradio_app.GRADIO_AVAILABLE, reason="gradio is not installed")
def test_build_ui_creates_a_blocks_app(settings, runner) -> None:
    demo = gradio_app.build_ui(settings, runner)
    assert hasattr(demo, "launch")
    assert callable(demo.launch)


@pytest.mark.skipif(gradio_app.GRADIO_AVAILABLE, reason="gradio is installed")
def test_build_ui_explains_missing_gradio(settings, runner) -> None:
    with pytest.raises(RuntimeError, match="gradio is not installed"):
        gradio_app.build_ui(settings, runner)
