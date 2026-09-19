"""Gradio front end (Milestone 2).

Functionality first: pick a source (local test video / mock data / Douyin
placeholder), choose the material and duration window, then watch the counters,
the preview verdicts, the detected segments, the AI calls and the produced
clips.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from core.config import AppSettings
from core.models import (
    ClipRecord,
    PipelineResult,
    PipelineStats,
    ProgressEvent,
    SourceVideoReport,
    SubtitlePolicy,
    TaskRequest,
)
from core.task_runner import TaskRunner
from sources.local import DEFAULT_EXTENSIONS

LOGGER = logging.getLogger(__name__)

try:  # Gradio is a heavy optional dependency: keep the module importable.
    import gradio as gr

    GRADIO_AVAILABLE = True
except Exception as exc:  # pragma: no cover - exercised only without gradio
    gr = None  # type: ignore[assignment]
    GRADIO_AVAILABLE = False
    LOGGER.info("gradio is not installed (%s); the web UI is unavailable", exc)

SUBTITLE_CHOICES: list[tuple[str, str]] = [
    ("严格", SubtitlePolicy.STRICT.value),
    ("均衡", SubtitlePolicy.BALANCED.value),
    ("宽松", SubtitlePolicy.LOOSE.value),
    ("关闭", SubtitlePolicy.OFF.value),
]

#: (label, source name, enabled)
SOURCE_CHOICES: list[tuple[str, str, bool]] = [
    ("抖音", "douyin", True),
    ("本地测试视频", "local", True),
    ("模拟数据（离线测试）", "mock", True),
]

ENABLED_SOURCES = [label for label, _value, enabled in SOURCE_CHOICES if enabled]

CLIP_TABLE_HEADERS = [
    "id",
    "物料",
    "形态",
    "状态",
    "工序",
    "片段(s)",
    "时长",
    "总分",
    "字幕",
    "文件",
]

SOURCE_TABLE_HEADERS = ["来源视频", "状态", "预筛", "原因", "时长", "片段数", "AI 调用"]
DOUYIN_TABLE_HEADERS = ["发现方式", "标题", "作者", "状态", "预筛原因", "匹配搜索词"]
SEGMENT_TABLE_HEADERS = ["来源视频", "AI 区间", "校准后区间", "时长", "结果", "描述"]
AI_RUN_TABLE_HEADERS = ["id", "provider", "model", "operation", "status", "延迟(ms)", "tokens"]
YIELD_TABLE_HEADERS = ["搜索词", "候选", "唯一", "通过预筛", "下载", "最终片段"]


def library_path_display(settings: AppSettings) -> str:
    """Configured library root rendered with forward slashes for the UI."""

    return settings.paths.library_root.as_posix()


def parse_library_root(raw: str | None, settings: AppSettings) -> Path:
    """Turn the text box value into a ``Path``.

    Accepts anything the user may paste, including surrounding quotes; a blank
    value falls back to the configured default.  Relative paths stay relative
    here and are anchored to the project root by ``TaskRunner``.
    """

    text = (raw or "").strip().strip('"').strip("'").strip()
    return Path(text) if text else settings.paths.library_root


def collect_local_files(
    uploads: Sequence[Any] | None,
    extra_path: str | None,
    *,
    extensions: Iterable[str] = DEFAULT_EXTENSIONS,
) -> list[Path]:
    """Merge uploaded files and a pasted path/directory into a file list."""

    allowed = {
        (ext if ext.startswith(".") else f".{ext}").lower() for ext in extensions
    }
    collected: list[Path] = []

    def add(path: Path) -> None:
        if path.is_dir():
            candidates = sorted(
                item
                for item in path.glob("*")
                if item.is_file() and item.suffix.lower() in allowed
            )
        else:
            candidates = [path]
        for candidate in candidates:
            if candidate.suffix.lower() not in allowed:
                LOGGER.warning("ignoring unsupported file: %s", candidate)
                continue
            if candidate not in collected:
                collected.append(candidate)

    for item in uploads or []:
        raw = getattr(item, "name", item)
        if raw:
            add(Path(str(raw)))

    text = (extra_path or "").strip().strip('"').strip("'").strip()
    if text:
        add(Path(text))
    return collected


def stats_markdown(stats: PipelineStats, *, status: str = "", extra: str = "") -> str:
    """Render the counters block shown above the gallery."""

    lines = [f"**状态**: {status}" if status else "", "| 指标 | 数量 |", "| --- | --- |"]
    lines.extend(f"| {label} | {value} |" for label, value in stats.as_rows())
    if extra:
        lines.append("")
        lines.append(extra)
    return "\n".join(line for line in lines if line)


def live_status_markdown(
    stats: PipelineStats,
    *,
    target: int,
    query: str = "",
    status: str = "运行中",
    backend: str = "",
    extra: str = "",
) -> str:
    """Live task panel in the layout the specification asks for (section 37)."""

    lines = [
        f"**状态**: {status}" + (f"｜后端: {backend}" if backend else ""),
        "",
        f"- 目标最终片段：**{target}**",
    ]
    if query:
        lines.append(f"- 当前搜索关键词：`{query}`")
    lines += [
        f"- 搜索候选：{stats.searched_candidates}　唯一候选：{stats.unique_candidates}",
        f"- 已预筛：{stats.prescreened}",
        f"- 字幕/复杂画面淘汰：{stats.subtitle_rejected}",
        f"- 物料无关/其他淘汰：{stats.other_rejected}",
        f"- 已下载源视频：{stats.downloads}",
        f"- 进入深度分析：{stats.analyzed}",
        f"- 发现片段：{stats.segments_found}",
        f"- 质量淘汰：{stats.quality_rejected}　重复淘汰：{stats.duplicates_rejected}",
        f"- 最终保存：**{stats.clips_saved} / {target}**",
    ]
    if extra:
        lines += ["", extra]
    return "\n".join(lines)


#: human readable names for the discovery states (sections 16 and 22)
BROWSER_STATUS_LABELS: dict[str, str] = {
    "ok": "浏览器搜索可用",
    "browser_unavailable": "Playwright 浏览器不可用",
    "douyin_unreachable": "抖音不可访问",
    "login_required": "需要登录",
    "verification_required": "需要人工验证",
    "search_timeout": "搜索超时",
    "search_dom_changed": "搜索页结构变化",
    "no_results": "没有搜索结果",
    "browser_crashed": "浏览器异常",
    "dtk_keyword": "API 搜索（dtk 关键词接口）",
    "archive": "后端归档检索",
    "author_posts": "作者作品发现",
    "mix_posts": "合集发现",
    "manual_url": "手工链接",
    "composite": "组合发现",
}


def discovery_status_markdown(
    settings: AppSettings,
    *,
    backend: str | None = None,
    status: str = "",
    detail: str = "",
    states: dict[str, str] | None = None,
    blocked: bool = False,
) -> str:
    """One-line discovery status for the UI (no cookies, no secrets)."""

    source = (backend or settings.sources.active_source or "").lower()
    if source != "douyin":
        return f"**来源**: {source}"
    if states:
        rendered = " ".join(f"`{name}={state}`" for name, state in states.items())
        if blocked:
            return (
                f"**抖音搜索状态**: 发现被阻断（搜索未执行）\n\n"
                f"{rendered}\n\n> {detail or '所有发现后端都不可用'}"
            )
        line = f"**抖音搜索状态**: {BROWSER_STATUS_LABELS.get(status, status or '未知')}｜{rendered}"
        if detail:
            line += f"\n\n> {detail}"
        return line
    browser = settings.sources.douyin.browser_search
    if not browser.enabled:
        label = "API/归档发现（浏览器搜索已关闭）"
    else:
        label = BROWSER_STATUS_LABELS.get(status, "浏览器搜索（未检查）")
    line = (
        f"**抖音搜索状态**: {label}｜后端: {settings.douyin_base_url() or '(未配置)'}"
        f"｜浏览器: {'headless' if browser.headless else '有窗口'}"
    )
    if detail:
        line += f"\n\n> {detail}"
    return line


def _open_douyin_browser(settings: AppSettings) -> None:
    """Open the persistent Douyin profile in its own process (UI button).

    The browser runs detached so the operator can log in or complete a normal
    verification challenge; no credentials are handled by the application.
    """

    import subprocess
    import sys

    script = settings.project_root / "app.py"
    command = [sys.executable, str(script), "--init-douyin-browser"]
    try:
        subprocess.Popen(command, cwd=str(settings.project_root))  # noqa: S603
    except OSError as exc:  # pragma: no cover - environment dependent
        LOGGER.warning("could not start the Douyin browser: %s", exc)


def clip_table_rows(clips: list[ClipRecord]) -> list[list[Any]]:
    """Flatten clip records into table rows."""

    rows: list[list[Any]] = []
    for clip in clips:
        rows.append(
            [
                clip.id,
                clip.material,
                str(clip.material_form),
                str(clip.material_state),
                str(clip.process_stage),
                f"{clip.source_start:.1f}-{clip.source_end:.1f}",
                round(clip.duration, 1),
                round(clip.overall_score, 2),
                str(clip.subtitle_type),
                Path(clip.file_path).name,
            ]
        )
    return rows


def gallery_items(clips: list[ClipRecord]) -> list[tuple[str, str]]:
    """``(image, caption)`` pairs for ``gr.Gallery``."""

    items: list[tuple[str, str]] = []
    for clip in clips:
        image = clip.thumbnail_path or clip.file_path
        if not image or not Path(image).exists():
            continue
        caption = (
            f"#{clip.id} {clip.material}/{clip.material_form}/{clip.material_state} "
            f"{clip.process_stage} {clip.duration:.1f}s score={clip.overall_score:.2f}"
        )
        items.append((str(image), caption))
    return items


def source_preview_rows(reports: list[SourceVideoReport]) -> list[list[Any]]:
    """Debug table: one row per analysed source video."""

    rows: list[list[Any]] = []
    for report in reports:
        name = Path(report.source_path).name if report.source_path else report.platform_video_id
        preview = (
            "accepted"
            if report.preview_accept
            else ("rejected" if report.preview_accept is False else "-")
        )
        reason = report.preview_reason or report.reject_reason or ""
        rows.append(
            [
                name or report.platform_video_id,
                str(report.status),
                f"{preview} {report.preview_detail}".strip(),
                str(reason),
                round(report.duration, 2) if report.duration else "",
                report.segments_found,
                report.ai_calls,
            ]
        )
    return rows


def segment_rows(reports: list[SourceVideoReport]) -> list[list[Any]]:
    """Debug table: detected segments with their refinement and outcome."""

    rows: list[list[Any]] = []
    for report in reports:
        name = Path(report.source_path).name if report.source_path else report.platform_video_id
        for segment in report.segments:
            refined = (
                f"{segment.refined_start:.2f}-{segment.refined_end:.2f}"
                if segment.refined_start is not None and segment.refined_end is not None
                else ""
            )
            outcome = "saved" if segment.saved else str(segment.reject_reason or "skipped")
            rows.append(
                [
                    name or report.platform_video_id,
                    f"{segment.start:.2f}-{segment.end:.2f}",
                    refined,
                    round(max(0.0, segment.end - segment.start), 2),
                    outcome,
                    segment.description,
                ]
            )
    return rows


def ai_run_rows(runs: list[dict[str, Any]]) -> list[list[Any]]:
    """Debug table: the ``ai_runs`` audit trail."""

    return [
        [
            run.get("id"),
            run.get("provider"),
            run.get("model"),
            run.get("operation"),
            run.get("status"),
            run.get("latency_ms"),
            run.get("total_tokens"),
        ]
        for run in runs
    ]


def douyin_source_rows(reports: list[SourceVideoReport]) -> list[list[Any]]:
    """Debug table: acquisition view (source, title, author, verdict)."""

    rows: list[list[Any]] = []
    for report in reports:
        rows.append(
            [
                report.discovery_backend or report.platform,
                (report.title or report.platform_video_id)[:60],
                report.source_author or "",
                str(report.status),
                str(report.preview_reason or report.reject_reason or ""),
                " / ".join(report.matched_queries[:3]) if report.matched_queries else "",
            ]
        )
    return rows


def yield_rows(rows: list[dict[str, Any]]) -> list[list[Any]]:
    """Debug table: what each search term actually produced (section 31)."""

    return [
        [
            row.get("query"),
            row.get("candidate_count"),
            row.get("unique_candidate_count"),
            row.get("preview_accept_count"),
            row.get("download_count"),
            row.get("final_clip_count"),
        ]
        for row in rows
    ]


def clip_tags_json(clips: list[ClipRecord]) -> str:
    """Structured metadata of the produced clips (for copy/paste debugging)."""

    payload = [
        {
            "id": clip.id,
            "material": clip.material,
            "material_form": str(clip.material_form),
            "material_state": str(clip.material_state),
            "process_stage": str(clip.process_stage),
            "equipment_type": clip.equipment_type,
            "equipment_visible": clip.equipment_visible,
            "scene": clip.scene,
            "shot_type": str(clip.shot_type),
            "camera_motion": str(clip.camera_motion),
            "people": clip.people,
            "people_count": clip.people_count,
            "person_role": str(clip.person_role),
            "subtitle_type": str(clip.subtitle_type),
            "subtitle_score": clip.subtitle_score,
            "edit_roles": [str(role) for role in clip.edit_roles],
            "description": clip.description,
            "source_start": clip.source_start,
            "source_end": clip.source_end,
            "duration": clip.duration,
            "scores": {
                "material_relevance": clip.material_score,
                "visual_quality": clip.visual_quality_score,
                "subtitle_cleanliness": clip.subtitle_cleanliness_score,
                "stability": clip.stability_score,
                "composition": clip.composition_score,
                "overall": clip.overall_score,
            },
            "tags": sorted(clip.tags),
            "file_path": str(clip.file_path),
            "thumbnail_path": str(clip.thumbnail_path) if clip.thumbnail_path else None,
        }
        for clip in clips
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _result_markdown(result: PipelineResult, log_lines: list[str], *, target: int) -> str:
    usage = result.ai_usage or {}
    tail = "\n".join(f"- {line}" for line in log_lines[-14:])
    extra = (
        f"耗时 {result.elapsed_seconds:.2f}s｜搜索词 {len(result.queries)} 个｜"
        f"AI 调用 {usage.get('ai_calls', 0)} 次（失败 {usage.get('failures', 0)}）｜"
        f"tokens {usage.get('total_tokens', 0)}\n\n{tail}"
    )
    return live_status_markdown(
        result.stats,
        target=max(target, len(result.clips)),
        status=result.status.value,
        extra=extra,
    )


def build_ui(settings: AppSettings, runner: TaskRunner | None = None):
    """Create the Gradio Blocks application (does not launch it)."""

    if not GRADIO_AVAILABLE:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "gradio is not installed. Install it with 'pip install -r requirements.txt' "
            "or run 'python app.py --demo' for the headless mock run."
        )

    runner = runner or TaskRunner(settings)

    with gr.Blocks(title=settings.app.name) as demo:
        gr.Markdown(f"# {settings.app.name}")
        gr.Markdown(
            "输入工业烘干物料，选择本地测试视频或模拟数据，系统会完成 "
            "FFprobe 探测 → 抽帧 → AI 预筛 → 时间段识别 → PySceneDetect 校准 → "
            "FFmpeg 切片 → AI 标签化 → 去重 → SQLite 入库。\n\n"
            f"当前配置: source=**{settings.sources.active_source}** / "
            f"ai=**{settings.ai.active_provider}** / media=**{settings.media.backend}**"
        )

        with gr.Tabs():
            with gr.Tab("素材采集"):

                with gr.Row():
                    material_box = gr.Textbox(label="目标物料", value="苹果干", placeholder="例如: 苹果干")
                    target_box = gr.Number(
                        label="最终素材数量",
                        value=settings.pipeline.default_target_clip_count,
                        precision=0,
                    )
                    min_box = gr.Number(
                        label="最短片段(秒)", value=settings.pipeline.default_min_clip_duration
                    )
                    max_box = gr.Number(
                        label="最长片段(秒)", value=settings.pipeline.default_max_clip_duration
                    )

                with gr.Row():
                    subtitle_box = gr.Dropdown(
                        label="字幕过滤",
                        choices=[label for label, _ in SUBTITLE_CHOICES],
                        value="严格",
                    )
                    path_box = gr.Textbox(
                        label="素材保存路径",
                        value=library_path_display(settings),
                        info="素材片段与缩略图保存到此目录；SQLite 索引仍保留在项目 data/ 下",
                    )
                    source_box = gr.Radio(
                        label="来源",
                        choices=[label for label, _value, enabled in SOURCE_CHOICES if enabled],
                        value=ENABLED_SOURCES[0],
                        info=(
                            f"抖音后端: {settings.sources.douyin.base_url}"
                            f"（后端需独立部署，见 docs/douyin_backend.md）"
                        ),
                    )

                with gr.Row():
                    upload_box = gr.File(
                        label="本地测试视频（仅本地来源需要，可多选）",
                        file_count="multiple",
                        file_types=[".mp4", ".mov", ".m4v"],
                        type="filepath",
                    )
                    local_path_box = gr.Textbox(
                        label="本地视频路径 / 抖音链接（可选）",
                        placeholder="D:/test/apple.mp4 或 https://www.douyin.com/video/...",
                    )

                with gr.Row():
                    start_button = gr.Button("开始采集", variant="primary")
                    stop_button = gr.Button("停止")
                    browser_button = gr.Button("初始化抖音浏览器", variant="secondary")

                status_box = gr.Markdown(stats_markdown(PipelineStats(), status="待运行"))
                discovery_box = gr.Markdown(
                    discovery_status_markdown(settings, backend=settings.sources.active_source)
                )
                gallery = gr.Gallery(label="最终 Clip", columns=4, height=320, show_label=True)
                table = gr.Dataframe(
                    headers=CLIP_TABLE_HEADERS, label="素材明细", wrap=True, interactive=False
                )

                with gr.Accordion("调试信息（来源 / 预筛 / 时间段 / AI 调用）", open=False):
                    douyin_table = gr.Dataframe(
                        headers=DOUYIN_TABLE_HEADERS,
                        label="抖音来源（标题 / 作者 / 预筛结论）",
                        wrap=True,
                        interactive=False,
                    )
                    source_table = gr.Dataframe(
                        headers=SOURCE_TABLE_HEADERS,
                        label="来源视频与预筛结果",
                        wrap=True,
                        interactive=False,
                    )
                    yield_table = gr.Dataframe(
                        headers=YIELD_TABLE_HEADERS,
                        label="搜索词产出（search_yields）",
                        wrap=True,
                        interactive=False,
                    )
                    segment_table = gr.Dataframe(
                        headers=SEGMENT_TABLE_HEADERS,
                        label="AI 检测时间段与校准结果",
                        wrap=True,
                        interactive=False,
                    )
                    ai_table = gr.Dataframe(
                        headers=AI_RUN_TABLE_HEADERS,
                        label="AI 调用审计（ai_runs）",
                        wrap=True,
                        interactive=False,
                    )
                    tags_box = gr.Code(label="片段结构化标签 (JSON)", language="json", lines=14)

                log_box = gr.Textbox(label="运行日志", lines=10, max_lines=20, interactive=False)

                def _resolve_source_label(label: str) -> str:
                    for choice_label, value, _enabled in SOURCE_CHOICES:
                        if choice_label == label:
                            return value
                    return "local"

                def _run_generator(
                    material: str,
                    target: float,
                    min_duration: float,
                    max_duration: float,
                    subtitle_label: str,
                    save_path: str,
                    source_label: str,
                    uploads: Sequence[Any] | None,
                    local_path: str,
                ) -> Iterator[tuple]:
                    policy = next(
                        (value for label, value in SUBTITLE_CHOICES if label == subtitle_label),
                        SubtitlePolicy.STRICT.value,
                    )
                    source_name = _resolve_source_label(source_label)
                    # The free-text field doubles as a Douyin URL box (section 39).
                    raw_text = (local_path or "").strip()
                    douyin_urls = [
                        token
                        for token in raw_text.replace(",", " ").split()
                        if token.startswith(("http://", "https://")) and "douyin.com" in token
                    ]
                    path_text = "" if douyin_urls else raw_text
                    files = collect_local_files(
                        uploads,
                        path_text,
                        extensions=settings.sources.local.normalised_extensions(),
                    )
                    if source_name == "local" and not files:
                        message = "请先选择本地测试视频（上传文件或填写本地路径）"
                        yield (
                            live_status_markdown(PipelineStats(), target=int(target or 1), status="未开始", extra=message),
                            discovery_status_markdown(settings, backend=source_name),
                            [],
                            [],
                            [],
                            [],
                            [],
                            [],
                            [],
                            "",
                            message,
                        )
                        return

                    if source_name == "douyin":
                        # Backend preflight (Milestone 3.6 section 6): a configured but
                        # dead backend must not walk all keyword variants.
                        from core.backend_resolver import (
                            apply_backend_selection,
                            resolve_douyin_backend,
                        )

                        try:
                            selection = asyncio.run(resolve_douyin_backend(settings))
                        except Exception as exc:  # pragma: no cover - defensive
                            LOGGER.warning("backend preflight failed: %s", exc)
                        else:
                            apply_backend_selection(settings, selection)
                            if selection.blocked:
                                message = (
                                    "后端预检未通过（backend_blocked）：采集未开始。\n"
                                    + "\n".join(selection.summary_lines())
                                )
                                yield (
                                    live_status_markdown(
                                        PipelineStats(),
                                        target=int(target or 1),
                                        status="后端不可用",
                                        backend=source_name,
                                        extra=message,
                                    ),
                                    discovery_status_markdown(
                                        settings, backend=source_name, status="backend_unavailable"
                                    ),
                                    [],
                                    [],
                                    [],
                                    [],
                                    [],
                                    [],
                                    [],
                                    "",
                                    message,
                                )
                                return

                    request = TaskRequest(
                        material=(material or "").strip() or "苹果干",
                        target_clip_count=int(target or 1),
                        min_clip_duration=float(min_duration or 3),
                        max_clip_duration=float(max_duration or 15),
                        subtitle_policy=SubtitlePolicy(policy),
                        library_root=parse_library_root(save_path, settings),
                        source=source_name,
                        local_files=files,
                        douyin_urls=douyin_urls,
                        provider=None,
                        media_backend=None,
                    )
                    events: queue.Queue[ProgressEvent] = queue.Queue()
                    log_lines: list[str] = []
                    holder: dict[str, PipelineResult | None] = {"result": None}

                    def worker() -> None:
                        try:
                            holder["result"] = asyncio.run(runner.run_async(request, on_event=events.put))
                        except Exception as exc:  # pragma: no cover - defensive
                            LOGGER.exception("ui worker failed")
                            log_lines.append(f"任务失败: {exc}")

                    runner.reset_cancel()
                    thread = threading.Thread(target=worker, daemon=True, name="collection-task")
                    thread.start()

                    latest_stats = PipelineStats()
                    current_query = ""
                    discovery_line = discovery_status_markdown(settings, backend=source_name)
                    yield (
                        live_status_markdown(
                            latest_stats,
                            target=request.target_clip_count,
                            status="运行中",
                            backend=source_name,
                        ),
                        discovery_line,
                        [],
                        [],
                        [],
                        [],
                        [],
                        [],
                        [],
                        "",
                        "",
                    )
                    while thread.is_alive() or not events.empty():
                        while not events.empty():
                            event = events.get()
                            latest_stats = event.stats
                            log_lines.append(event.message)
                            if event.stage == "search":
                                current_query = event.message
                        yield (
                            live_status_markdown(
                                latest_stats,
                                target=request.target_clip_count,
                                query=current_query,
                                status="运行中",
                                backend=source_name,
                                extra=f"当前 {len(log_lines)} 条事件",
                            ),
                            discovery_line,
                            [],
                            [],
                            [],
                            [],
                            [],
                            [],
                            [],
                            "",
                            "\n".join(log_lines[-40:]),
                        )
                        time.sleep(0.2)
                    thread.join(timeout=5)

                    result = holder["result"]
                    if result is None:  # pragma: no cover - defensive
                        yield (
                            live_status_markdown(
                                PipelineStats(), target=request.target_clip_count, status="失败"
                            ),
                            discovery_line,
                            [],
                            [],
                            [],
                            [],
                            [],
                            [],
                            [],
                            "",
                            "\n".join(log_lines[-40:]),
                        )
                        return
                    log_lines.extend(result.messages)
                    runs = runner.library.list_ai_runs(task_id=result.task_id, limit=50)
                    yields = runner.library.list_search_yields(task_id=result.task_id)
                    yield (
                        _result_markdown(result, log_lines, target=request.target_clip_count),
                        discovery_status_markdown(
                            settings,
                            backend=result.discovery_backend or source_name,
                            status=result.discovery_status,
                            detail=result.discovery_detail,
                            states=result.discovery_states,
                            blocked=result.discovery_blocked,
                        ),
                        gallery_items(list(result.clips)),
                        clip_table_rows(list(result.clips)),
                        douyin_source_rows(list(result.source_videos)),
                        source_preview_rows(list(result.source_videos)),
                        yield_rows(yields),
                        segment_rows(list(result.source_videos)),
                        ai_run_rows(runs),
                        clip_tags_json(list(result.clips)),
                        "\n".join(log_lines[-40:]),
                    )

                outputs = [
                    status_box,
                    discovery_box,
                    gallery,
                    table,
                    douyin_table,
                    source_table,
                    yield_table,
                    segment_table,
                    ai_table,
                    tags_box,
                    log_box,
                ]
                start_button.click(
                    fn=_run_generator,
                    inputs=[
                        material_box,
                        target_box,
                        min_box,
                        max_box,
                        subtitle_box,
                        path_box,
                        source_box,
                        upload_box,
                        local_path_box,
                    ],
                    outputs=outputs,
                )
                stop_button.click(fn=lambda: (runner.request_cancel(), "已请求停止")[1], outputs=[log_box])
                browser_button.click(
                    fn=lambda: (
                        _open_douyin_browser(settings),
                        "已在独立的浏览器窗口中打开抖音搜索页：请手动完成登录或验证，"
                        "完成后关闭该窗口即可；后续采集复用该持久化会话。",
                    )[1],
                    outputs=[log_box],
                )

                def _initial_view() -> tuple:
                    clips = runner.recent_clips(limit=12)
                    runs = runner.library.list_ai_runs(limit=20)
                    recent_yields = runner.library.list_search_yields(limit=20)
                    return (
                        live_status_markdown(
                            PipelineStats(),
                            target=settings.pipeline.default_target_clip_count,
                            status="待运行",
                            backend=settings.sources.active_source,
                        ),
                        discovery_status_markdown(settings),
                        gallery_items(clips),
                        clip_table_rows(clips),
                        [],  # douyin acquisition rows (populated after a run)
                        [],  # per-source preview rows
                        yield_rows(recent_yields),
                        [],  # segment rows
                        ai_run_rows(runs),
                        clip_tags_json(clips),
                        "",
                    )

                demo.load(fn=_initial_view, outputs=outputs)

            # -- Milestone 4 tabs (素材库 / 任务记录 / 系统检查) --------------
            try:
                from ui.library_tab import (
                    build_coverage_tab,
                    build_library_tab,
                    build_system_tab,
                    build_tasks_tab,
                )
                from ui.plan_tab import build_plan_tab

                build_library_tab(settings, runner)
                build_coverage_tab(settings, runner)
                build_plan_tab(settings, runner)
                build_tasks_tab(settings, runner)
                build_system_tab(settings, runner)
            except Exception as exc:  # pragma: no cover - the acquisition tab still works
                LOGGER.exception("could not build the material library tabs")
                with gr.Tab("素材库"):
                    gr.Markdown(f"素材库标签页初始化失败: {exc}")

    demo.queue()
    return demo


def launch(
    settings: AppSettings,
    *,
    host: str | None = None,
    port: int | None = None,
    share: bool | None = None,
    **kwargs: Any,
):
    """Build and launch the UI (blocking)."""

    ui = build_ui(settings)
    return ui.launch(
        server_name=host or settings.ui.host,
        server_port=port or settings.ui.port,
        share=settings.ui.share if share is None else share,
        **kwargs,
    )
