"""UI-triggered maintenance actions (Milestone 4, section 14).

Small, testable wrappers so the Gradio callbacks stay thin and the same code
path can be exercised from tests without a browser.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.config import AppSettings
from core.dependencies import build_gateway, build_toolkit
from core.retag import ClipRetagger
from core.task_runner import TaskRunner

LOGGER = logging.getLogger(__name__)


def retag_clip_for_ui(
    settings: AppSettings,
    runner: TaskRunner,
    clip_id: Any,
    *,
    confirmed: bool,
    version: str = "",
) -> str:
    """Re-run clip tagging for one stored clip, keeping its media.

    Requires an explicit confirmation because the configured provider may be a
    paid model.  Returns an operator-facing message; never raises.
    """

    if not confirmed:
        return "请先勾选确认（重新识别会调用真实模型，可能产生费用）。"
    try:
        target = int(clip_id)
    except (TypeError, ValueError):
        return "请先选择要重新识别的素材。"

    library = runner.library
    clip = library.get_clip(target)
    if clip is None:
        return f"素材 #{target} 不存在。"

    gateway = build_gateway(
        settings, provider_name=settings.ai.active_provider, on_call=library.add_ai_run
    )
    retagger = ClipRetagger(
        gateway=gateway,
        toolkit=build_toolkit(settings, backend=settings.media.backend),
        library=library,
        frames_dir=settings.paths.cache_dir / "frames",
        frame_ratios=tuple(settings.analysis.clip_frame_ratios) or (0.2, 0.4, 0.6, 0.8),
        max_width=settings.analysis.preview_max_width,
    )
    try:
        outcome = asyncio.run(retagger.retag(target, version=version, apply=True))
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.exception("retag from UI failed")
        return f"重新识别失败: {exc}"
    finally:
        try:
            asyncio.run(gateway.aclose())
        except Exception:  # pragma: no cover - defensive
            LOGGER.debug("closing the retag gateway failed", exc_info=True)

    if not outcome.ok:
        return f"重新识别失败: {outcome.error or '未知错误'}"
    updated = library.get_clip(target)
    detail = (
        f"{updated.material}/{updated.material_form}/{updated.material_state} "
        f"{updated.process_stage} 总分 {updated.overall_score:.2f}"
        if updated
        else ""
    )
    return (
        f"素材 #{target} 重新识别完成（prompt={outcome.version}，"
        f"tokens={outcome.tokens}）: {detail}；视频文件与来源信息未改动。"
    )


def cleanup_clip_for_ui(
    settings: AppSettings,
    runner: TaskRunner,
    clip_id: Any,
    *,
    force: bool = False,
) -> str:
    """Run the conservative local subtitle cleanup for one clip from the UI.

    No cloud model is involved.  A failure keeps the original media and only
    records a cleanup status; it never changes review/quality/provenance.
    """

    try:
        target = int(clip_id)
    except (TypeError, ValueError):
        return "请先选择要清理的素材。"
    from core.subtitle_cleanup import SubtitleCleanupService

    try:
        service = SubtitleCleanupService(runner.library, settings)
        outcome = asyncio.run(service.cleanup_clip(target, force=bool(force)))
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.exception("subtitle cleanup from UI failed")
        return f"字幕清理失败: {exc}"
    message = "；".join(line.strip() for line in outcome.lines()[1:] if line.strip())
    headline = outcome.lines()[0]
    return f"{headline}；{message}" if message else headline


def review_cleanup_for_ui(
    settings: AppSettings,
    runner: TaskRunner,
    clip_id: Any,
    *,
    status: str,
    note: str = "",
    failure_class: str = "",
) -> str:
    """Approve / reject / reset one cleanup derivative review from the UI."""

    try:
        target = int(clip_id)
    except (TypeError, ValueError):
        return "请先选择要复核的素材。"
    from core.subtitle_cleanup import SubtitleCleanupService

    service = SubtitleCleanupService(runner.library, settings)
    ok, message = service.review_cleanup(
        target, status=status, note=note or "", failure_class=failure_class or ""
    )
    return ("[ok] " if ok else "[warn] ") + message


def delete_cleanup_derivative_for_ui(
    settings: AppSettings,
    runner: TaskRunner,
    clip_id: Any,
    *,
    note: str = "",
) -> str:
    try:
        target = int(clip_id)
    except (TypeError, ValueError):
        return "请先选择要删除派生的素材。"
    from core.subtitle_cleanup import SubtitleCleanupService

    service = SubtitleCleanupService(runner.library, settings)
    ok, message = service.delete_derivative(target, note=note or "")
    return ("[ok] " if ok else "[warn] ") + message
