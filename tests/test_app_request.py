from __future__ import annotations

from types import SimpleNamespace

import app
from core.config import save_cloud_cleanup_limits
from core.dependencies import build_library
from core.keyword_expander import KeywordExpander
from core.models import ProcessStage, TaskRequest
from core.orchestrator import CollectionOrchestrator


def test_explicit_material_overrides_douyin_search_inference(settings) -> None:
    args = app.parse_args(
        [
            "--douyin-search",
            "香菇正在烘干",
            "--material",
            "香菇干",
            "--library-category",
            "香菇干",
            "--target-stage",
            "drying",
        ]
    )

    request = app.build_request(args, settings)

    assert request.material == "香菇干"
    assert request.query_seed == "香菇正在烘干"
    assert request.library_category == "香菇干"
    assert str(request.target_process_stage) == "drying"


def test_resume_loads_full_persisted_request_and_uses_current_library(settings) -> None:
    library = build_library(settings)
    original = TaskRequest(
        material="香菇干",
        query_seed="香菇烘干实拍",
        explicit_queries=["香菇烘干实拍"],
        target_clip_count=9,
        target_process_stage=ProcessStage.DRYING,
        library_root="Z:/old-machine/library",
        library_category="香菇干",
        source="douyin",
        provider="qwen",
        media_backend="ffmpeg",
    )
    task_id = library.create_task(original)

    request = app.build_request(app.parse_args(["--resume-task", str(task_id)]), settings)

    assert request.material == "香菇干"
    assert request.query_seed == "香菇烘干实拍"
    assert request.explicit_queries == ["香菇烘干实拍"]
    assert request.target_clip_count == 9
    assert request.target_process_stage is ProcessStage.DRYING
    assert request.library_category == "香菇干"
    assert request.source == "douyin"
    assert request.provider == "qwen"
    assert request.media_backend == "ffmpeg"
    assert request.library_root == settings.paths.library_root
    assert request.resume_task_id == task_id


def test_resume_explicit_target_overrides_checkpoint(settings) -> None:
    library = build_library(settings)
    task_id = library.create_task(TaskRequest(material="香菇干", target_clip_count=9))

    request = app.build_request(
        app.parse_args(["--resume-task", str(task_id), "--target", "15"]), settings
    )

    assert request.target_clip_count == 15


def test_stage_targeted_ad_hoc_queries_do_not_fall_back_to_drying() -> None:
    orchestrator = CollectionOrchestrator.__new__(CollectionOrchestrator)
    orchestrator.keyword_expander = KeywordExpander()
    orchestrator.deps = SimpleNamespace(
        source=SimpleNamespace(search_mode="keyword"),
        library=object(),
    )
    orchestrator.settings = SimpleNamespace(
        pipeline=SimpleNamespace(keyword_max_queries=10),
        coverage=SimpleNamespace(),
    )
    request = SimpleNamespace(
        explicit_queries=[],
        material="香菇干",
        library_category="香菇干",
        target_process_stage=ProcessStage.CUTTING,
        query_seed="鲜香菇切片机加工现场",
    )

    queries = orchestrator._plan_queries(request)

    assert queries[0] == "鲜香菇切片机加工现场"
    assert any("切片" in query or "切条" in query for query in queries[1:])
    assert not any("烘干" in query for query in queries)


def test_local_cleanup_v2_cli_overrides_are_parsed() -> None:
    args = app.parse_args(
        [
            "--subtitle-cleanup",
            "30",
            "--local-cleanup-engine",
            "opencv_inpaint",
            "--cleanup-version",
            "subtitle_cleanup_v2",
        ]
    )

    assert args.subtitle_cleanup == 30
    assert args.local_cleanup_engine == "opencv_inpaint"
    assert args.cleanup_version == "subtitle_cleanup_v2"


def test_cloud_cleanup_paid_batch_cap_defaults_to_three(settings) -> None:
    assert settings.cloud_cleanup.max_paid_tasks_per_run == 3


def test_cloud_cleanup_paid_batch_cap_clamps_requested_limit(
    settings, monkeypatch, capsys
) -> None:
    seen: list[int] = []

    class FakeLibrary:
        def inventory_clips(self, *, limit: int):
            seen.append(limit)
            return []

    settings.cloud_cleanup.max_paid_tasks_per_run = 2
    monkeypatch.setattr(
        app,
        "_cloud_cleanup_service",
        lambda _settings: SimpleNamespace(library=FakeLibrary()),
    )

    result = app.run_cloud_cleanup_batch(
        settings, limit=50, engine="volcengine"
    )

    assert result == 0
    assert seen == [2]
    assert "没有可处理的片段" in capsys.readouterr().out


def test_cloud_cleanup_limits_can_be_persisted(tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "cloud_cleanup:\n"
        "  max_paid_tasks_per_run: 3 # per run\n"
        "  max_paid_tasks_per_day: 10 # per day\n",
        encoding="utf-8",
    )

    save_cloud_cleanup_limits(config, per_run=7, per_day=25)

    saved = config.read_text(encoding="utf-8")
    assert "max_paid_tasks_per_run: 7 # per run" in saved
    assert "max_paid_tasks_per_day: 25 # per day" in saved
    assert not config.with_name(".config.yaml.tmp").exists()
