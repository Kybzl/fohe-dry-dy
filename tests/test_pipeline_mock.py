"""End-to-end mock workflow: 苹果干 -> 5 clips -> SQLite -> cleanup."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from core.dependencies import build_dependencies
from core.models import SourceVideoStatus, SubtitlePolicy, TaskRequest, TaskStatus
from core.orchestrator import CollectionOrchestrator
from core.task_runner import TaskRunner
from media.downloader import MockDownloader


def test_mock_collection_produces_five_clips(runner, settings, task_request) -> None:
    result = runner.run(task_request("苹果干", 5))

    assert result.status is TaskStatus.SUCCEEDED
    assert result.task_id is not None
    assert len(result.clips) == 5
    assert result.queries[0] == "苹果干"
    assert len(result.queries) == 10

    stats = result.stats
    assert stats.searched_candidates > 0
    assert stats.prescreened > 0
    assert stats.subtitle_rejected >= 1, "强调字幕淘汰必须被演示"
    assert stats.duplicates_rejected >= 1
    assert stats.analyzed >= 1
    assert stats.clips_saved == 5
    assert stats.errors == 0

    for clip in result.clips:
        assert clip.id is not None
        assert 3.0 - 1e-6 <= clip.duration <= 15.0 + 1e-6
        assert clip.source_start >= 0.0
        assert clip.source_end > clip.source_start
        assert Path(clip.file_path).exists()
        assert clip.thumbnail_path and Path(clip.thumbnail_path).exists()
        assert "苹果" in str(clip.file_path)
        assert clip.sha256 and clip.phash
        assert clip.source_url.startswith("https://")
        assert clip.tags, "每个片段都必须带标签"
        assert clip.overall_score > 0

    counts = runner.library.stats()
    assert counts["tasks"] == 1
    assert counts["clips"] == 5
    assert counts["tags"] > 0
    assert counts["clip_tags"] >= 5
    assert counts["source_videos"] >= 1

    summary = result.summary_text()
    assert "最终保存数量: 5" in summary


def test_temporary_source_videos_are_deleted(runner, settings, task_request) -> None:
    runner.run(task_request("苹果干", 5))
    leftovers = [path for path in settings.paths.cache_dir.rglob("*") if path.is_file()]
    assert all(path.suffix != ".mp4" for path in leftovers)
    assert not list(settings.paths.cache_dir.glob("*.meta.json"))


def test_second_run_never_stores_a_duplicate_clip(runner, task_request) -> None:
    runner.run(task_request("苹果干", 5))
    second = runner.run(task_request("苹果干", 5))

    rows = runner.library.database.query(
        "SELECT content_key, COUNT(*) AS n FROM clips GROUP BY content_key HAVING n > 1"
    )
    duplicates = runner.library.database.query(
        "SELECT sha256, COUNT(*) AS n FROM clips GROUP BY sha256 HAVING n > 1"
    )
    assert rows == []
    assert duplicates == []
    assert second.stats.duplicates_rejected >= 1
    assert runner.library.count_clips() == 5 + len(second.clips)


def test_results_are_deterministic_for_the_same_seed(tmp_path) -> None:
    from core.config import load_settings
    from core.task_runner import TaskRunner

    def build(tag: str):
        root = tmp_path / tag
        local = load_settings(
            overrides={
                "storage": {
                    "library_root": str(root / "library"),
                    "database_path": str(root / "data" / "library.db"),
                    "cache_root": str(root / "cache"),
                }
            }
        )
        runner = TaskRunner(local)
        return runner, runner.run(
            TaskRequest(
                material="苹果干",
                target_clip_count=5,
                subtitle_policy=SubtitlePolicy.STRICT,
                # the offline stack, so the test never touches a real source
                source="mock",
                provider="mock",
                media_backend="mock",
                library_root=local.paths.library_root,
            )
        )

    _, first = build("a")
    _, second = build("b")
    signature = lambda result: [  # noqa: E731 - small helper for readability
        (round(clip.source_start, 2), round(clip.source_end, 2), clip.description)
        for clip in result.clips
    ]
    assert signature(first) == signature(second)
    assert first.stats.model_dump() == second.stats.model_dump()


def test_other_materials_are_supported(runner, task_request) -> None:
    result = runner.run(task_request("香蕉干", 3))
    assert result.queries[0] == "香蕉干"
    assert result.clips
    assert all(clip.material == "香蕉" for clip in result.clips)
    assert all("香蕉" in str(clip.file_path) for clip in result.clips)


def test_download_failures_do_not_abort_the_task(settings) -> None:
    dependencies = build_dependencies(
        settings, source_name="mock", provider_name="mock", media_backend="mock"
    )
    dependencies.downloader = MockDownloader(fail_on=("mock.local",))
    orchestrator = CollectionOrchestrator(dependencies)
    result = asyncio.run(
        orchestrator.collect(
            TaskRequest(material="苹果干", target_clip_count=5, subtitle_policy=SubtitlePolicy.STRICT)
        )
    )
    assert result.status is TaskStatus.PARTIAL
    assert result.clips == []
    assert result.stats.errors >= 1
    assert any("获取源视频失败" in message for message in result.messages)
    assert all(
        report.status
        in (
            SourceVideoStatus.FAILED,
            SourceVideoStatus.FAILED_DOWNLOAD,
            SourceVideoStatus.REJECTED,
            SourceVideoStatus.REJECTED_PREVIEW,
        )
        for report in result.source_videos
    )


def test_cancellation_is_honoured(settings) -> None:
    dependencies = build_dependencies(
        settings, source_name="mock", provider_name="mock", media_backend="mock"
    )
    cancel_event = threading.Event()
    cancel_event.set()
    orchestrator = CollectionOrchestrator(dependencies, cancel_event=cancel_event)
    result = asyncio.run(
        orchestrator.collect(TaskRequest(material="苹果干", target_clip_count=5))
    )
    assert result.status is TaskStatus.CANCELLED
    assert result.clips == []


def test_candidate_budget_is_enforced(tmp_path) -> None:
    from core.config import load_settings
    from core.task_runner import TaskRunner

    local = load_settings(
        overrides={
            "storage": {
                "library_root": str(tmp_path / "library"),
                "database_path": str(tmp_path / "data" / "library.db"),
                "cache_root": str(tmp_path / "cache"),
            },
            "pipeline": {"max_candidates_per_task": 6},
        }
    )
    runner = TaskRunner(local)
    result = runner.run(
        TaskRequest(
            material="苹果干",
            target_clip_count=30,
            source="mock",
            provider="mock",
            media_backend="mock",
        )
    )
    assert result.status is TaskStatus.PARTIAL
    assert result.stats.examined_candidates <= 6
    assert len(result.clips) < 30
    assert any("达到候选上限" in message for message in result.messages)


def test_task_is_recorded_with_final_status(runner, task_request) -> None:
    result = runner.run(task_request("苹果干", 2))
    task = runner.library.get_task(result.task_id)
    assert task is not None
    assert task.material == "苹果干"
    assert task.target_clip_count == 2
    assert task.status is TaskStatus.SUCCEEDED
    assert task.subtitle_policy is SubtitlePolicy.STRICT
    assert task.min_clip_duration == 3.0
    assert task.max_clip_duration == 15.0


def test_resume_rejects_unknown_task_without_creating_a_row(runner, task_request) -> None:
    request = task_request("苹果干", 2, resume_task_id=999)

    result = runner.run(request)

    assert result.status is TaskStatus.FAILED
    assert result.task_id == 999
    assert result.error == "任务 #999 不存在"
    assert runner.library.list_tasks() == []


def test_resume_rejects_identity_mismatch_without_mutating_task(
    runner, task_request
) -> None:
    original = task_request("苹果干", 2)
    task_id = runner.library.create_task(original, status=TaskStatus.PARTIAL)
    request = task_request("香菇干", 5, resume_task_id=task_id)

    result = runner.run(request)

    assert result.status is TaskStatus.FAILED
    assert "恢复参数与原任务不一致" in (result.error or "")
    stored = runner.library.get_task(task_id)
    assert stored is not None
    assert stored.material == "苹果干"
    assert stored.status is TaskStatus.PARTIAL


def test_resume_allows_target_count_to_change(runner, task_request) -> None:
    original = task_request("苹果干", 1)
    first = runner.run(original)
    request = task_request("苹果干", 3, resume_task_id=first.task_id)

    resumed = runner.run(request)

    assert resumed.task_id == first.task_id
    assert resumed.status is TaskStatus.SUCCEEDED
    assert len(resumed.clips) >= 3


def test_new_runner_recovers_tasks_left_running_after_process_exit(
    settings, task_request
) -> None:
    first_runner = TaskRunner(settings)
    request = task_request("苹果干", 2)
    task_id = first_runner.library.create_task(
        request,
        status=TaskStatus.RUNNING,
        error="runner_pid:99999999",
    )
    source_id = first_runner.library.upsert_source_video(
        task_id=task_id,
        platform="douyin",
        platform_video_id="interrupted-1",
        source_url="https://www.douyin.com/video/interrupted-1",
        status=SourceVideoStatus.DOWNLOADING,
    )

    restarted_runner = TaskRunner(settings)

    assert restarted_runner.recovered_tasks == {task_id: 1}
    task = restarted_runner.library.get_task(task_id)
    assert task is not None
    assert task.status is TaskStatus.PARTIAL
    assert task.error == "interrupted_by_process_restart"
    source = restarted_runner.library.get_source_video_by_id(source_id)
    assert source is not None
    assert source.status is SourceVideoStatus.DISCOVERED
    assert restarted_runner.library.normalize_interrupted_tasks() == {}


def test_new_runner_does_not_touch_task_owned_by_live_process(
    settings, task_request
) -> None:
    import os

    first_runner = TaskRunner(settings)
    request = task_request("苹果干", 2)
    task_id = first_runner.library.create_task(
        request,
        status=TaskStatus.RUNNING,
        error=f"runner_pid:{os.getpid()}",
    )

    second_runner = TaskRunner(settings)

    assert second_runner.recovered_tasks == {}
    task = second_runner.library.get_task(task_id)
    assert task is not None
    assert task.status is TaskStatus.RUNNING
