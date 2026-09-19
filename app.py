"""Entry point for the industrial drying material library agent.

    python app.py                                  # Gradio web UI
    python app.py --check                          # environment doctor
    python app.py --local-video "D:/test/apple.mp4" --material "苹果干"
    python app.py --demo                           # offline mock smoke test

Real local video runs use real FFprobe, real FFmpeg cutting, real SQLite and,
when credentials exist in .env, the real Qwen vision model.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from core.config import (
    DEFAULT_CONFIG_PATH,
    AppSettings,
    configure_logging,
    load_settings,
)
from core.keyword_expander import strip_process_words
from core.models import PipelineResult, ProcessStage, SubtitlePolicy, TaskRequest, TaskStatus
from core.plans import PauseReason, PlanStatus
from core.task_runner import TaskRunner
from sources.douyin_backend import UPSTREAM_API_VERSION, UPSTREAM_PROJECT
from sources.douyin_browser_search import (
    HUMAN_WALL_STATUSES,
    DouyinBrowserSearchBackend,
)
from storage.schema import TABLE_NAMES

LOGGER = logging.getLogger("agent")

MIN_PYTHON = (3, 12)


def _configure_console_encoding() -> None:
    """Make Windows CLI output safe for Chinese text and emoji.

    Redirected output commonly inherits the legacy GBK code page.  A source
    title containing an emoji must not turn an otherwise successful task into
    a non-zero exit after its clip has already been saved.
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # StringIO/closed streams used by embedders may not be mutable.
            continue


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="工业烘干短视频素材库 Agent (Milestone 2)",
    )
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--check", action="store_true", help="run the environment doctor and exit")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="print the effective configuration (no secrets) and exit",
    )
    parser.add_argument(
        "--check-douyin",
        action="store_true",
        help="check the Douyin backend (reachability, auth, capabilities) and exit",
    )
    parser.add_argument(
        "--check-ai-provider",
        action="store_true",
        help="check AI provider readiness with the effective production model routing",
    )
    parser.add_argument(
        "--check-douyin-browser",
        action="store_true",
        help="check the Playwright browser discovery path and exit",
    )
    parser.add_argument(
        "--verify-douyin-browser",
        action="store_true",
        help=(
            "interactive: keep the same browser context alive while the operator "
            "completes Douyin verification, then re-check for real search results"
        ),
    )
    parser.add_argument(
        "--open-douyin-browser",
        action="store_true",
        help=(
            "start the operator's own Chrome/Edge with a debug port (V3.2 model) "
            "so the app can attach over CDP instead of launching an "
            "automation-flagged browser"
        ),
    )
    parser.add_argument(
        "--cdp-url",
        default=None,
        metavar="URL",
        help="force a CDP endpoint for this run (default: config/auto-detect)",
    )
    parser.add_argument(
        "--verify-query",
        default=None,
        metavar="QUERY",
        help="search term used by --verify-douyin-browser (default: 苹果干烘干)",
    )
    parser.add_argument(
        "--interactive-verification",
        action="store_true",
        help=(
            "with --run-collection-plan: verify the Douyin session in the same "
            "process/context, then run the plan without relaunching the browser"
        ),
    )
    parser.add_argument(
        "--cleanup-new-clips",
        action="store_true",
        help=(
            "Milestone 9.4: after acquisition, classify every new clip and "
            "attempt bounded subtitle cleanup for eligible new clips"
        ),
    )
    parser.add_argument(
        "--cleanup-new-limit",
        type=int,
        default=2,
        metavar="N",
        help="maximum cleanup attempts on newly acquired clips (default 2)",
    )
    parser.add_argument(
        "--init-douyin-browser",
        action="store_true",
        help="open the persistent Douyin browser profile so the operator can log in / verify",
    )
    parser.add_argument("--demo", action="store_true", help="offline mock smoke test and exit")

    # real local video integration
    parser.add_argument(
        "--local-video",
        action="append",
        default=[],
        metavar="PATH",
        help="local video file or directory (repeatable); enables the local source",
    )
    parser.add_argument(
        "--material",
        default=None,
        help="target material (default: 苹果干; for --list-clips it is a filter)",
    )
    parser.add_argument("--target", type=int, default=None, help="target clip count (default 5)")
    parser.add_argument(
        "--target-stage",
        choices=[stage.value for stage in ProcessStage],
        default=None,
        help="only save clips classified into this process stage",
    )
    parser.add_argument("--min-duration", type=float, default=None, help="shortest clip (seconds)")
    parser.add_argument("--max-duration", type=float, default=None, help="longest clip (seconds)")
    parser.add_argument(
        "--subtitle-policy",
        default=None,
        choices=[policy.value for policy in SubtitlePolicy],
        help="subtitle filtering policy",
    )
    parser.add_argument("--output-dir", default=None, help="material library root override")

    # real Douyin acquisition
    parser.add_argument(
        "--douyin-search",
        default=None,
        metavar="KEYWORD",
        help="run a real Douyin keyword acquisition (or the supported fallback discovery)",
    )
    parser.add_argument(
        "--douyin-url",
        action="append",
        default=[],
        metavar="URL",
        help="process explicit Douyin post URL(s) (repeatable, debugging aid)",
    )
    parser.add_argument("--resume-task", type=int, default=None, help="resume an earlier task id")

    # Milestone 3.7: tagging quality / library maintenance
    parser.add_argument(
        "--library-category",
        default=None,
        help="physical library folder for this task (default: the material)",
    )
    parser.add_argument(
        "--list-demo-clips",
        action="store_true",
        help="inspect stored clips and classify mock/local/real provenance",
    )
    parser.add_argument(
        "--remove-demo-clips",
        action="store_true",
        help="remove provably synthetic (mock) clips; dry run unless --yes is given",
    )
    parser.add_argument(
        "--include-local-tests",
        action="store_true",
        help="also treat local test clips as removable in the demo cleanup",
    )
    parser.add_argument(
        "--retag-clip",
        type=int,
        default=None,
        metavar="ID",
        help="re-run clip tagging for one stored clip (keeps its media)",
    )
    parser.add_argument(
        "--prompt-version",
        default=None,
        help="clip_tagging prompt version for --retag-clip / --ab-tagging",
    )
    parser.add_argument(
        "--tag-report",
        action="store_true",
        help="compact prompt-tuning table for stored clips (no AI calls)",
    )
    parser.add_argument(
        "--backfill-clip-metadata",
        action="store_true",
        help="fill empty library_category/provenance on historical clips "
        "(dry run unless --yes)",
    )
    parser.add_argument(
        "--ab-tagging",
        action="store_true",
        help="compare clip_tagging prompt versions on existing clips (costs tokens)",
    )
    parser.add_argument(
        "--clips",
        default=None,
        metavar="IDS",
        help="comma separated clip ids for --ab-tagging (default: newest real clips)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm a command that deletes files or spends tokens",
    )

    # Milestone 4: material library management / export
    parser.add_argument(
        "--check-library",
        action="store_true",
        help="read-only inventory of the material library (missing/orphan files)",
    )
    parser.add_argument(
        "--list-clips",
        action="store_true",
        help="list clips matching the given filters (paged, newest first)",
    )
    parser.add_argument(
        "--export-clips",
        action="store_true",
        help="export selected or filtered clips as JSON/CSV into exports/",
    )
    parser.add_argument(
        "--export-format",
        default="json",
        choices=["json", "csv", "both"],
        help="manifest format for --export-clips",
    )
    parser.add_argument("--provenance", default=None, help="clip provenance filter")
    parser.add_argument("--material-state", default=None, help="observed material state")
    parser.add_argument("--process-stage", default=None, help="process stage filter")
    parser.add_argument(
        "--people",
        default=None,
        choices=["true", "false"],
        help="people-present filter",
    )
    parser.add_argument("--shot-type", default=None, help="shot type filter")
    parser.add_argument("--edit-role", default=None, help="edit role filter")
    parser.add_argument(
        "--review-status",
        default=None,
        metavar="A,B",
        help="human review status filter (comma separated)",
    )
    parser.add_argument(
        "--favorite",
        action="store_true",
        help="only favorites (for --list-clips / --export-clips)",
    )
    parser.add_argument("--free-text", default=None, help="free-text search")
    parser.add_argument("--min-overall-score", type=float, default=None)
    parser.add_argument("--max-overall-score", type=float, default=None)
    parser.add_argument("--limit", type=int, default=None, help="rows for --list-clips")
    parser.add_argument(
        "--sort",
        default="newest",
        help="sort option code for --list-clips/--export-clips",
    )

    # Milestone 5: coverage / acquisition strategy / maintenance
    parser.add_argument(
        "--coverage-report",
        default=None,
        metavar="CATEGORY",
        help="coverage report for one library category (e.g. 苹果干)",
    )
    parser.add_argument(
        "--coverage-gaps",
        default=None,
        metavar="CATEGORY",
        help="gap report + recommended search terms for one category",
    )
    parser.add_argument(
        "--search-yield-report",
        nargs="?",
        const="",
        default=None,
        metavar="CATEGORY",
        help="search query yield ranking and AI cost analysis",
    )
    parser.add_argument(
        "--review-export",
        action="store_true",
        help="export clip_id/review_status/review_note/favorite to a CSV",
    )
    parser.add_argument(
        "--review-import",
        default=None,
        metavar="CSV",
        help="import a review CSV (dry run unless --yes)",
    )
    parser.add_argument(
        "--repair-thumbnails",
        action="store_true",
        help="rebuild missing thumbnails from the stored MP4 (dry run unless --yes)",
    )
    parser.add_argument(
        "--quarantine-orphans",
        action="store_true",
        help="move orphan files to quarantine/ (dry run unless --yes; never deletes)",
    )
    parser.add_argument(
        "--maintenance-log",
        action="store_true",
        help="show the maintenance audit trail",
    )
    parser.add_argument("--preset-list", action="store_true", help="list filter presets")
    parser.add_argument(
        "--preset-save",
        default=None,
        metavar="NAME",
        help="save the current filter flags as a named preset",
    )
    parser.add_argument(
        "--preset-delete", default=None, metavar="NAME", help="delete a filter preset"
    )

    # Milestone 6: measured subtitle analysis
    parser.add_argument(
        "--subtitle-report",
        nargs="?",
        const="",
        default=None,
        metavar="CATEGORY",
        help="measured subtitle class distribution and cleanliness statistics",
    )
    parser.add_argument(
        "--analyze-subtitles",
        type=int,
        default=None,
        metavar="CLIP_ID",
        help="measure text regions of one stored clip (report only unless --yes)",
    )
    parser.add_argument(
        "--analyze-subtitles-all",
        action="store_true",
        help="measure stored clips in bulk (dry run unless --yes)",
    )
    parser.add_argument(
        "--subtitle-engine",
        default=None,
        help="override subtitle_analysis.engine (auto|rapidocr|opencv|none)",
    )

    # Milestone 9.2: conservative local subtitle cleanup (derivative only)
    parser.add_argument(
        "--subtitle-cleanup",
        type=int,
        default=None,
        metavar="CLIP_ID",
        help="run conservative local subtitle cleanup for one stored clip",
    )
    parser.add_argument(
        "--local-cleanup-engine",
        default=None,
        choices=["ffmpeg_delogo", "opencv_inpaint"],
        help="override the local cleanup engine for one cleanup run",
    )
    parser.add_argument(
        "--cleanup-version",
        default=None,
        help="override cleanup derivative/audit version for one cleanup run",
    )
    parser.add_argument(
        "--subtitle-cleanup-report",
        action="store_true",
        help="show subtitle cleanup status counts and success metrics",
    )
    parser.add_argument(
        "--subtitle-cleanup-batch",
        action="store_true",
        help="run cleanup for an explicitly bounded batch (requires --limit)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="retry an existing failed/not-needed cleanup result (with --subtitle-cleanup)",
    )
    parser.add_argument(
        "--subtitle-cleanup-candidates",
        action="store_true",
        help="dry-run: list cleanup eligibility from stored library metadata",
    )
    parser.add_argument(
        "--cleanup-approve",
        type=int,
        default=None,
        metavar="CLIP_ID",
        help="approve one successful cleanup derivative",
    )
    parser.add_argument(
        "--cleanup-reject",
        type=int,
        default=None,
        metavar="CLIP_ID",
        help="reject one cleanup derivative (requires --review-failure-class)",
    )
    parser.add_argument(
        "--cleanup-reset-review",
        type=int,
        default=None,
        metavar="CLIP_ID",
        help="reset one cleanup review to pending",
    )
    parser.add_argument(
        "--review-note",
        default="",
        help="human review note for approve/reject/reset",
    )
    parser.add_argument(
        "--review-failure-class",
        default="",
        help="controlled failure class for --cleanup-reject",
    )
    parser.add_argument(
        "--cleanup-verify",
        type=int,
        default=None,
        metavar="CLIP_ID",
        help="verify one cleanup derivative with ffprobe",
    )
    parser.add_argument(
        "--cleanup-delete-derivative",
        type=int,
        default=None,
        metavar="CLIP_ID",
        help="delete one rejected cleanup derivative (requires --yes); original is never deleted",
    )
    parser.add_argument(
        "--subtitle-cleanup-review-pack",
        nargs="?",
        const="",
        default=None,
        metavar="CLIP_ID",
        help="generate static HTML review pack for one clip or all successful derivatives",
    )
    parser.add_argument(
        "--check-volcengine-cleanup",
        action="store_true",
        help="check Volcano Engine VOD refined subtitle-erase readiness (no secrets)",
    )
    parser.add_argument(
        "--subtitle-cleanup-cloud",
        type=int,
        default=None,
        metavar="CLIP_ID",
        help="run opt-in Volcano Engine refined subtitle erase for one clip",
    )
    parser.add_argument(
        "--subtitle-cleanup-cloud-preflight",
        type=int,
        default=None,
        metavar="CLIP_ID",
        help="run the full local paid-cloud eligibility check without upload or API calls",
    )
    parser.add_argument(
        "--subtitle-cleanup-cloud-batch",
        action="store_true",
        help="run cloud cleanup for an explicitly bounded list (requires --limit)",
    )
    parser.add_argument(
        "--engine",
        default=None,
        choices=["local", "volcengine", "manual"],
        help="cleanup engine policy (local | volcengine | manual)",
    )

    # Milestone 7: collection planning / approval / execution
    parser.add_argument(
        "--create-collection-plan",
        default=None,
        metavar="CATEGORY",
        help="generate a DRAFT collection plan from the real coverage gaps",
    )
    parser.add_argument(
        "--production-gaps",
        action="store_true",
        help=(
            "Milestone 9: ranked production coverage gaps "
            "(category / stage / gap / priority / best query / risk)"
        ),
    )
    parser.add_argument(
        "--production-ready-report",
        action="store_true",
        help=(
            "Milestone 9.4: per semantic clip show preferred media, cleanup "
            "review state and production readiness"
        ),
    )
    parser.add_argument(
        "--duration-recheck",
        action="store_true",
        help=(
            "Milestone 9.6: run the bounded duration/media ladder on historical "
            "duration_unknown_unresolved sources (read-only audit)"
        ),
    )
    parser.add_argument(
        "--create-production-plan",
        action="store_true",
        help="create ONE draft production plan from the highest-priority real gaps",
    )
    parser.add_argument(
        "--category",
        default=None,
        metavar="NAME",
        help="filter --production-gaps / --create-production-plan by library category",
    )
    parser.add_argument(
        "--stage",
        default=None,
        metavar="STAGE",
        help="filter --production-gaps / --create-production-plan by process stage",
    )
    parser.add_argument(
        "--include-covered",
        action="store_true",
        help="also list process stages that already meet their target",
    )
    parser.add_argument(
        "--list-collection-plans",
        action="store_true",
        help="list collection plans with their progress",
    )
    parser.add_argument(
        "--show-collection-plan",
        type=int,
        default=None,
        metavar="PLAN_ID",
        help="show one plan: targets, budgets, estimate, results",
    )
    parser.add_argument(
        "--approve-collection-plan",
        type=int,
        default=None,
        metavar="PLAN_ID",
        help="approve a draft plan (required before execution)",
    )
    parser.add_argument(
        "--run-collection-plan",
        type=int,
        default=None,
        metavar="PLAN_ID",
        help="execute an approved plan (use --dry-run first)",
    )
    parser.add_argument(
        "--pause-collection-plan",
        type=int,
        default=None,
        metavar="PLAN_ID",
        help="pause a running plan (cooperative)",
    )
    parser.add_argument(
        "--resume-collection-plan",
        type=int,
        default=None,
        metavar="PLAN_ID",
        help="resume a paused plan",
    )
    parser.add_argument(
        "--cancel-collection-plan",
        type=int,
        default=None,
        metavar="PLAN_ID",
        help="cancel a plan (results are kept)",
    )
    parser.add_argument(
        "--archive-collection-plan",
        type=int,
        default=None,
        metavar="PLAN_ID",
        help="archive a plan (kept auditable, hidden from the active list, not runnable)",
    )
    parser.add_argument(
        "--unarchive-collection-plan",
        type=int,
        default=None,
        metavar="PLAN_ID",
        help="restore an archived plan to the active list",
    )
    parser.add_argument(
        "--mark-test-plan",
        type=int,
        default=None,
        metavar="PLAN_ID",
        help="mark a plan as an acceptance/stub plan",
    )
    parser.add_argument(
        "--unmark-test-plan",
        type=int,
        default=None,
        metavar="PLAN_ID",
        help="clear the acceptance/stub marker",
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="include archived plans in --list-collection-plans",
    )
    parser.add_argument(
        "--query-ranking-report",
        action="store_true",
        help="compare query_rank_v1 with query_rank_v2 on the real history",
    )
    parser.add_argument(
        "--explain-query",
        default=None,
        metavar="QUERY",
        help="show the transparent score components of one search term",
    )
    parser.add_argument(
        "--ranking-version",
        default=None,
        choices=["query_rank_v1", "query_rank_v2"],
        help="ranking version used by --query-ranking-report/--explain-query",
    )
    parser.add_argument(
        "--plan-linkage",
        type=int,
        default=None,
        metavar="PLAN_ID",
        help="show the real plan→item→task→source→clip id chain",
    )
    parser.add_argument(
        "--validate-clip",
        type=int,
        default=None,
        metavar="CLIP_ID",
        help="validate one saved clip (file/ffprobe/duration/thumbnail/provenance)",
    )
    parser.add_argument(
        "--count-mode",
        default=None,
        choices=["all", "approved"],
        help="coverage counting mode for plan generation",
    )
    parser.add_argument(
        "--include-healthy",
        action="store_true",
        help="also plan items for stages that already meet their target",
    )
    parser.add_argument(
        "--plan-name", default=None, help="optional name for the generated plan"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what a plan would do without any external call",
    )
    parser.add_argument(
        "--plan-note", default="", help="approval note"
    )

    # component overrides
    parser.add_argument("--source", default=None, choices=["local", "mock", "douyin"])
    parser.add_argument("--provider", default=None, choices=["qwen", "volcano", "mock"])
    parser.add_argument("--media", default=None, choices=["ffmpeg", "mock", "auto"])

    # ui
    parser.add_argument("--host", default=None, help="UI host")
    parser.add_argument("--port", type=int, default=None, help="UI port")
    parser.add_argument("--share", action="store_true", help="create a public Gradio link")
    parser.add_argument("--log-level", default=None, help="override the log level")
    return parser.parse_args(argv)


def build_request(args: argparse.Namespace, settings: AppSettings) -> TaskRequest:
    """Translate CLI arguments into a ``TaskRequest``."""

    files = [Path(item).expanduser() for item in args.local_video]
    douyin_urls = [str(url) for url in getattr(args, "douyin_url", []) or []]
    keyword = getattr(args, "douyin_search", None)
    library_root = Path(args.output_dir).expanduser() if args.output_dir else settings.paths.library_root
    resume_task_id = getattr(args, "resume_task", None)
    if resume_task_id is not None:
        from core.dependencies import build_library

        stored = build_library(settings).get_task_request(resume_task_id)
        if stored is not None:
            # Machine-local paths follow the current workstation unless the
            # operator explicitly supplied replacements on this invocation.
            updates: dict[str, object] = {
                "resume_task_id": resume_task_id,
                "library_root": library_root,
            }
            if args.target is not None:
                updates["target_clip_count"] = args.target
            if args.material:
                updates["material"] = args.material
            if args.min_duration is not None:
                updates["min_clip_duration"] = args.min_duration
            if args.max_duration is not None:
                updates["max_clip_duration"] = args.max_duration
            if args.subtitle_policy is not None:
                updates["subtitle_policy"] = SubtitlePolicy(args.subtitle_policy)
            if args.output_dir:
                updates["library_root"] = Path(args.output_dir).expanduser()
            if args.source:
                updates["source"] = args.source
            if args.provider:
                updates["provider"] = args.provider
            if args.media:
                updates["media_backend"] = args.media
            if files:
                updates["local_files"] = files
                updates["source"] = "local"
            if douyin_urls:
                updates["douyin_urls"] = douyin_urls
                updates["source"] = "douyin"
            if keyword:
                updates["query_seed"] = keyword
                updates["source"] = "douyin"
            return stored.model_copy(update=updates)
    if args.material:
        # An explicit material is authoritative.  The search phrase may contain
        # modifiers such as "正在" that cannot be safely removed by the generic
        # process-word stripper.
        material = args.material
    elif keyword:
        # ``--douyin-search`` takes a whole search phrase; the library and the
        # keyword templates want the material, and the phrase itself is kept as
        # the first query.
        material = strip_process_words(keyword)
    else:
        material = "苹果干"
    return TaskRequest(
        material=material,
        query_seed=keyword or None,
        target_clip_count=args.target or 5,
        target_process_stage=(
            ProcessStage(args.target_stage) if getattr(args, "target_stage", None) else None
        ),
        min_clip_duration=args.min_duration or settings.pipeline.default_min_clip_duration,
        max_clip_duration=args.max_duration or settings.pipeline.default_max_clip_duration,
        subtitle_policy=SubtitlePolicy(
            args.subtitle_policy or settings.pipeline.default_subtitle_policy
        ),
        library_root=library_root,
        library_category=(getattr(args, "library_category", None) or None),
        source=args.source
        or ("local" if files else ("douyin" if (keyword or douyin_urls) else None)),
        local_files=files,
        douyin_urls=douyin_urls,
        provider=args.provider,
        media_backend=args.media,
        resume_task_id=resume_task_id,
    )


async def _douyin_doctor(settings: AppSettings) -> tuple[bool, list[str]]:
    """Backend resolution, connectivity, auth and capability report (section 3/6)."""

    from core.backend_resolver import apply_backend_selection, resolve_douyin_backend
    from core.dependencies import build_douyin_client
    from sources.douyin import DouyinSource

    douyin = settings.sources.douyin
    lines = [
        f"[info] upstream contract: {UPSTREAM_PROJECT} {UPSTREAM_API_VERSION}",
        f"[info] configured backend: {douyin.backend} "
        f"{douyin.base_url or '(base_url unset)'}",
    ]
    if not settings.douyin_api_key() and not settings.douyin_session_cookie():
        lines.append(
            f"[warn] no backend credential found in {douyin.api_key_env} / "
            "DOUYIN_BACKEND_SESSION_COOKIE (.env); unauthenticated calls will be refused"
        )

    selection = await resolve_douyin_backend(settings)
    lines.extend(selection.summary_lines())
    if not selection.usable:
        lines.append("[info] no expensive search was performed by this check")
        return False, lines

    apply_backend_selection(settings, selection)
    client = build_douyin_client(settings)
    capabilities = await client.health(deep=True)
    lines.extend(capabilities.summary_lines())
    source = DouyinSource(client=client)
    lines.extend(await source.describe_backends())
    await client.aclose()
    lines.append("[info] no expensive search was performed by this check")
    return True, lines


async def _preflight_douyin(settings: AppSettings):
    """Resolve + apply the Douyin backend before any collection task (section 6)."""

    from core.backend_resolver import apply_backend_selection, resolve_douyin_backend

    selection = await resolve_douyin_backend(settings)
    apply_backend_selection(settings, selection)
    return selection


def run_check_ai_provider(settings: AppSettings) -> int:
    """``--check-ai-provider``: minimal readiness probe of effective routing."""

    from ai.readiness import check_provider_readiness
    from core.dependencies import build_provider

    provider = build_provider(settings, settings.ai.active_provider)
    readiness = asyncio.run(check_provider_readiness(provider, settings))
    print("# AI provider readiness")
    print("\n".join(readiness.summary_lines()))
    try:
        asyncio.run(provider.aclose())
    except Exception:  # pragma: no cover - defensive
        LOGGER.debug("closing readiness provider failed", exc_info=True)
    return 0 if readiness.ready else 1


async def _douyin_browser_doctor(settings: AppSettings) -> tuple[bool, list[str]]:
    """Report the browser discovery path state (sections 4, 22 and 28)."""

    from core.browser_config import resolve_browser_config
    from core.dependencies import build_browser_search
    from sources.douyin_search import BrowserSearchStatus

    browser_settings = settings.sources.douyin.browser_search
    launch = resolve_browser_config(settings, headless=False)
    lines = list(launch.summary_lines())
    lines.append(
        f"[info] search enabled: {browser_settings.enabled} "
        f"(keep_context_open={browser_settings.keep_context_open})"
    )
    available, note = DouyinBrowserSearchBackend.playwright_available()
    lines.append(f"[{'ok' if available else 'warn'}] browser available: {note}")
    if not available:
        lines.append("[warn] install with: pip install playwright && playwright install chromium")
        return False, lines

    backend = build_browser_search(settings, headless=False)
    lines.append(
        f"[info] launched channel: {backend.browser_channel or 'chromium'}"
        + (
            f" executable={backend.browser_executable_path}"
            if backend.browser_executable_path
            else ""
        )
    )
    try:
        await backend.open()
    except Exception as exc:
        lines.append(f"[warn] browser could not start: {exc}")
        return False, lines
    try:
        outcome = await backend.search("抖音", 1)
    finally:
        await backend.close()

    status = outcome.status or BrowserSearchStatus.OK.value
    lines.append(
        f"[{'ok' if status == BrowserSearchStatus.OK.value else 'warn'}] search page state: {status}"
    )
    if outcome.detail:
        lines.append(f"[info] {outcome.detail}")
    diagnostics = outcome.diagnostics or {}
    if diagnostics:
        for key in (
            "requested_url",
            "final_url",
            "http_status",
            "page_title",
            "browser_channel",
            "browser_executable",
            "profile_dir",
        ):
            if diagnostics.get(key) not in (None, ""):
                lines.append(f"[info] {key}: {diagnostics[key]}")
    if status == BrowserSearchStatus.OK.value:
        lines.append(f"[ok] search page accessible, {len(outcome.candidates)} result(s) on the first page")
    elif status == BrowserSearchStatus.UPSTREAM_BAD_GATEWAY.value:
        code = diagnostics.get("http_status") or 502
        lines.append(f"[warn] Douyin upstream returned HTTP {code} Bad Gateway")
        lines.append(
            "[info] this is an upstream/network gateway response, "
            "not a CAPTCHA classification"
        )
    elif status == BrowserSearchStatus.UPSTREAM_HTTP_ERROR.value:
        code = diagnostics.get("http_status") or "5xx"
        lines.append(f"[warn] Douyin upstream returned HTTP {code}")
        lines.append(
            "[info] this is an upstream/network gateway response, "
            "not a CAPTCHA classification"
        )
    elif status == BrowserSearchStatus.VERIFICATION_REQUIRED.value:
        lines.append(
            "[warn] fresh search navigation is challenged: Douyin rendered its "
            "verification page for this request"
        )
    elif status == BrowserSearchStatus.LOGIN_REQUIRED.value:
        lines.append(
            "[warn] Douyin shows a login wall before search results"
        )
    if status != BrowserSearchStatus.OK.value:
        # section 7 of Milestone 8.1: state the three facts separately and send
        # the operator to the interactive command instead of an init/close loop
        profile_exists = backend.profile_dir.exists()
        lines.append(
            f"[info] persistent profile exists: {profile_exists} "
            f"({backend.profile_dir})"
        )
        lines.append(
            f"[info] browser config aligned: channel={backend.browser_channel or 'chromium'} "
            f"headless={backend.headless} locale={backend.locale}"
        )
        lines.append(
            "[info] this command is a non-interactive diagnostic: it does not "
            "keep the page alive and it will not wait for you"
        )
        lines.append(
            "[info] 下一步: 运行 'python app.py --verify-douyin-browser' —— "
            "它会在同一个浏览器窗口里保留验证页，等你人工完成后用同一个上下文复查搜索结果"
        )
        lines.append(
            "[info] '--init-douyin-browser' 只用于首次登录/初始化，"
            "并不能保证之后的新进程可以访问搜索页（实测不可靠）"
        )
    if status == BrowserSearchStatus.SEARCH_DOM_CHANGED.value:
        lines.append(
            "[warn] 抖音验证已通过，但当前搜索结果 DOM 暂未识别"
            "（卡片存在但没有可用的 /video/ 链接）"
        )
        lines.append(
            "[info] 实测原因: Playwright 启动的浏览器会被抖音识别为自动化，"
            "只渲染空壳卡片；请用 'python app.py --open-douyin-browser' "
            "启动你自己的浏览器（CDP 接入）后重试"
        )
    lines.append("[info] no CAPTCHA solving or stealth evasion is performed by design")
    ok = status == BrowserSearchStatus.OK.value
    return ok, lines


async def _init_douyin_browser(settings: AppSettings) -> int:
    """Open the persistent profile so the operator can log in once.

    Milestone 8.1: this is **profile initialization only**.  Real evidence
    showed that a completed verification does not reliably survive a browser /
    process restart, so this command is no longer the acceptance path — use
    ``--verify-douyin-browser`` (same context, no restart) instead.
    """

    from core.dependencies import build_browser_search

    backend = build_browser_search(settings, headless=False, keep_open_on_challenge=True)
    print(f"打开抖音浏览器配置目录: {backend.profile_dir}")
    print(
        f"将使用浏览器通道: {backend.browser_channel or 'chromium'}"
        + (
            f"（可执行文件 {backend.browser_executable_path}）"
            if backend.browser_executable_path
            else "（Playwright 自带 Chromium）"
        )
    )
    print(
        "浏览器窗口将打开抖音搜索页：请手动完成登录或验证，完成后回到这里按 Ctrl+C 结束。\n"
        "不会记录密码、不会导出 Cookie、不会自动破解验证码。"
    )
    await backend.open()
    try:
        assert backend._context is not None  # noqa: SLF001 - internal by design
        page = await backend._context.new_page()  # noqa: SLF001
        await page.goto(
            "https://www.douyin.com/search/%E8%8B%B9%E6%9E%9C%E5%B9%B2",
            wait_until="domcontentloaded",
        )
        while True:
            await asyncio.sleep(2)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("关闭浏览器，保存会话到持久化配置目录。")
    finally:
        await backend.close()
    return 0


async def _verify_douyin_browser(
    settings: AppSettings, *, query: str, on_message: Any = None, cdp_url: str | None = None
) -> tuple[bool, list[str]]:
    """Milestone 8.1 interactive gate: verify, then search in the same context.

    Returns ``(usable, lines)``.  The browser is only closed when this coroutine
    returns; the challenged page stays alive inside the same context while the
    operator works, and the re-check reuses that very page.
    """

    from core.dependencies import build_browser_search

    emit = on_message or print
    backend = build_browser_search(
        settings,
        headless=False,
        keep_open_on_challenge=True,
        keep_page_on_challenge=True,
        cdp_url=cdp_url,
    )
    mode = backend.describe_mode()
    lines = [
        f"[info] browser mode: {mode}",
        f"[info] browser profile: {backend.profile_dir}",
        f"[info] browser channel: {backend.browser_channel or 'chromium'}"
        + (
            f" executable={backend.browser_executable_path}"
            if backend.browser_executable_path
            else ""
        ),
        f"[info] headless: {backend.headless} locale={backend.locale}",
        f"[info] verification search term: {query}",
        "[info] 浏览器配置与真实搜索完全一致；不会破解验证码，也不会导出 Cookie",
    ]
    if not backend.cdp_url:
        lines.append(
            "[info] 提示: 若搜索页只出现空壳卡片（无 /video/ 链接），"
            "请改用 'python app.py --open-douyin-browser' 启动你自己的浏览器后再验证"
        )
    try:
        check = await backend.ensure_interactive_session(
            query,
            limit=min(5, backend.max_results_per_query),
            on_message=emit,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        lines.append("[warn] 已取消（Ctrl+C）：浏览器会话已关闭，持久化配置目录保留")
        await backend.close()
        return False, lines
    finally:
        pass
    lines.extend(check.summary_lines())
    if check.usable:
        lines.append(
            "[ok] 同一上下文内已能看到真实抖音搜索结果：会话可用（session_usable）"
        )
    elif check.status == "search_dom_changed":
        # section 10 of M8.3: a DOM mismatch is NOT a verification failure
        lines.append(
            "[warn] 抖音验证已通过，但当前搜索结果 DOM 暂未识别"
            "（卡片存在但没有可用的 /video/ 链接）"
        )
        lines.append(
            "[info] 这通常意味着抖音给这个浏览器返回了「空壳」结果页；"
            "请用 'python app.py --open-douyin-browser' 启动你自己的浏览器，"
            "登录/验证后再运行 --verify-douyin-browser"
        )
    elif check.status in HUMAN_WALL_STATUSES:
        lines.append(
            "[warn] 仍不可用：仍处于验证/登录页或没有真实 /video/ 结果；"
            "不会做任何绕过"
        )
    else:
        # browser_unavailable / gateway / unreachable: never worded as a CAPTCHA
        lines.append(
            f"[warn] 浏览器会话不可用（{check.status}）：{check.detail}"
        )
        if check.status == "browser_unavailable":
            lines.append(
                "[info] 如果端口上已经有浏览器，说明它的调试连接被占用或过载："
                "请先停掉其他正在使用该浏览器的任务，或重启 "
                "'python app.py --open-douyin-browser'"
            )
    await backend.close()
    lines.append("[info] 浏览器会话已正常关闭（持久化配置目录保留）")
    return check.usable, lines


def run_open_douyin_browser(settings: AppSettings) -> int:
    """Start the operator's own Chrome/Edge with a CDP debug port (V3.2 model).

    Milestone 8.3: Douyin serves an untrusted "empty shell" search page to a
    browser launched by Playwright (automation flags), while a normally
    launched browser with the same profile returns real result links.  This
    command starts that normal browser; the operator logs in / verifies there
    and the app attaches over CDP.  It is a plain browser launch: no
    automation flags, no stealth, no fingerprint spoofing, no CAPTCHA solving.
    """

    import subprocess

    from core.browser_config import resolve_browser_config
    from core.dependencies import cdp_endpoint_available

    browser = settings.sources.douyin.browser_search
    port = int(browser.cdp_port)
    existing = cdp_endpoint_available(port)
    if existing:
        print(f"[info] 端口 {port} 上已经有一个可接入的浏览器: {existing}")
        print("       直接用 'python app.py --verify-douyin-browser' 即可。")
        return 0
    launch = resolve_browser_config(settings, headless=False)
    binary = launch.executable_path or ""
    if not binary:
        import shutil

        binary = (
            shutil.which("chrome")
            or shutil.which("msedge")
            or shutil.which("chromium")
            or ""
        )
    if not binary or not Path(binary).exists():
        print("[warn] 找不到 Chrome/Edge 可执行文件，请手动启动浏览器并加上参数：")
        print(f"       --remote-debugging-port={port} --user-data-dir=<profile>")
        return 1
    args = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={launch.profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "https://www.douyin.com/",
    ]
    try:
        subprocess.Popen(args, close_fds=True)
    except Exception as exc:
        print(f"[warn] 启动浏览器失败: {exc}")
        return 1
    print(f"[ok] 已启动你的浏览器: {binary}")
    print(f"     profile: {launch.profile_dir}")
    print(f"     CDP 端口: {port}")
    print()
    print("请在打开的窗口里登录 / 完成抖音验证（程序不会代劳，也不会破解验证码），")
    print("确认搜索页能正常出结果后，回到终端运行：")
    print("    python app.py --verify-douyin-browser")
    print("浏览器会保持打开（本命令不会关闭它）。")
    return 0


def _ffmpeg_report(settings: AppSettings) -> list[str]:
    """Probe the configured ffmpeg/ffprobe binaries."""

    lines: list[str] = []
    from media.ffmpeg import FFmpegToolkit

    toolkit = FFmpegToolkit(
        ffmpeg_bin=settings.media.ffmpeg_path or "ffmpeg",
        ffprobe_bin=settings.media.ffprobe_path or "ffprobe",
        timeout=settings.media.command_timeout_seconds,
    )
    available = toolkit.is_available
    lines.append(f"[{'ok' if available else 'warn'}] {toolkit.describe()}")
    for label, binary in (("ffmpeg", toolkit.ffmpeg_bin), ("ffprobe", toolkit.ffprobe_bin)):
        resolved = FFmpegToolkit._resolve(binary)
        if not resolved:
            continue
        try:
            result = subprocess.run(
                [resolved, "-hide_banner", "-version"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            version = (result.stdout or result.stderr).splitlines()[0]
            lines.append(f"[ok] {label}: {version[:80]}")
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
            lines.append(f"[warn] {label} version check failed: {exc}")
    if not available:
        lines.append(
            "[warn] media.backend=ffmpeg needs both binaries; the mock backend "
            "will be used until they are available"
        )
    return lines


def doctor(settings: AppSettings) -> tuple[bool, list[str]]:
    """Check configuration, filesystem, database and optional dependencies."""

    lines: list[str] = []
    ok = True

    version = sys.version_info
    python_ok = (version.major, version.minor) >= MIN_PYTHON
    ok &= python_ok
    lines.append(
        f"[{'ok' if python_ok else 'FAIL'}] python {version.major}.{version.minor}.{version.micro} "
        f"(requires >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})"
    )

    try:
        settings.paths.ensure_directories()
        lines.append(
            f"[ok] data={settings.paths.data_dir} cache={settings.paths.cache_dir} "
            f"logs={settings.paths.log_dir}"
        )
        lines.append(f"[ok] material library root: {settings.paths.library_root}")
    except OSError as exc:
        ok = False
        lines.append(f"[FAIL] cannot create directories: {exc}")

    try:
        from core.dependencies import build_library

        library = build_library(settings)
        missing = library.database.missing_tables()
        tables_ok = not missing
        ok &= tables_ok
        lines.append(
            f"[{'ok' if tables_ok else 'FAIL'}] sqlite {library.database.path} "
            f"tables={', '.join(TABLE_NAMES)}" + (f" missing={missing}" if missing else "")
        )
        counts = library.stats()
        lines.append(
            "[ok] library rows: "
            + " ".join(f"{name}={count}" for name, count in counts.items())
        )
    except (sqlite3.Error, OSError) as exc:
        ok = False
        lines.append(f"[FAIL] database initialisation failed: {exc}")

    from ui.gradio_app import GRADIO_AVAILABLE

    lines.append(
        f"[{'ok' if GRADIO_AVAILABLE else 'warn'}] gradio "
        f"{'available' if GRADIO_AVAILABLE else 'missing -> pip install -r requirements.txt'}"
    )

    lines.extend(_ffmpeg_report(settings))

    from core.diagnostics import describe_effective_config

    lines.extend(describe_effective_config(settings))

    lines.append(
        f"[ok] source={settings.sources.active_source} media={settings.media.backend} "
        f"video_codec={settings.media.video_codec} crf={settings.media.crf} "
        f"preset={settings.media.preset}"
    )
    lines.append(
        f"[ok] ai primary={settings.ai.active_provider} "
        f"fallback={settings.ai.fallback_provider or 'none'}"
    )

    from core.dependencies import build_provider

    for name in [settings.ai.active_provider, settings.ai.fallback_provider]:
        if not name:
            continue
        try:
            provider = build_provider(settings, name)
        except ValueError as exc:
            lines.append(f"[warn] provider {name}: {exc}")
            continue
        configured = getattr(provider, "configured", True)
        model = provider.model_for("preview_filter")
        lines.append(
            f"[{'ok' if configured else 'warn'}] provider {name}: "
            f"configured={configured} preview_model={model}"
        )
        if name == "qwen" and not configured:
            lines.append("[warn] set QWEN_API_KEY in .env to run real AI analysis")

    lines.append(
        f"[ok] analysis: preview_frames<={settings.analysis.preview_max_frames} "
        f"analysis_frames={settings.analysis.analysis_max_frames} "
        f"strategy={settings.analysis.sampling_strategy} "
        f"scene_adjustment={settings.analysis.scene_boundary_max_adjustment_seconds}s"
    )
    return ok, lines


def _print_result(result: PipelineResult, runner: TaskRunner) -> None:
    print(result.summary_text())
    print()

    print("来源视频分析:")
    for report in result.source_videos:
        preview = (
            "accepted" if report.preview_accept else f"rejected({report.preview_reason})"
        )
        print(
            f"  {Path(report.source_path or report.source_url).name or report.platform_video_id}"
            f" [{report.status}] preview={preview} segments={report.segments_found} "
            f"clips={report.clips_saved} ai_calls={report.ai_calls}"
        )
        if report.title:
            print(f"      title={report.title[:60]}")
        for segment in report.segments:
            flag = "saved" if segment.saved else (segment.reject_reason or "skipped")
            print(
                f"      {segment.start:.2f}-{segment.end:.2f}s "
                f"(refined {segment.refined_start:.2f}-{segment.refined_end:.2f}s) "
                f"[{flag}] {segment.description[:36]}"
            )
    print()

    yields = runner.library.list_search_yields(result.task_id)
    if yields:
        print("搜索词产出:")
        for row in yields:
            print(
                f"  {row['query']}: candidates={row['candidate_count']} "
                f"unique={row['unique_candidate_count']} accepted={row['preview_accept_count']} "
                f"downloads={row['download_count']} clips={row['final_clip_count']}"
            )
        print()

    print("生成的片段:")
    for clip in result.clips:
        print(
            f"  #{clip.id} {clip.material} {clip.material_form}/{clip.material_state} "
            f"{clip.process_stage} {clip.source_start:.2f}-{clip.source_end:.2f}s "
            f"({clip.duration:.2f}s) score={clip.overall_score:.2f} "
            f"subtitle={clip.subtitle_type}"
        )
        if clip.source_title or clip.source_author:
            print(
                f"      source: {clip.source_title[:40]} | {clip.source_author}"
                f" | {clip.source_url}"
            )
        print(f"      file={clip.file_path}")
        print(f"      thumb={clip.thumbnail_path}")
        print(f"      tags={sorted(clip.tags)}")

    usage = result.ai_usage or {}
    print()
    print(
        "AI 调用: "
        f"calls={usage.get('ai_calls', 0)} failures={usage.get('failures', 0)} "
        f"prompt_tokens={usage.get('prompt_tokens', 0)} "
        f"completion_tokens={usage.get('completion_tokens', 0)} "
        f"total_tokens={usage.get('total_tokens', 0)} "
        f"estimated_cost={usage.get('estimated_cost')}"
    )
    if usage.get("per_provider"):
        print(f"  providers={usage['per_provider']}")

    # section 14: per-operation cost accounting, tokens per saved clip
    from core.ai_stats import operation_report_lines, summarize_ai_usage

    runs = runner.library.list_ai_runs(task_id=result.task_id, limit=1000)
    summary = summarize_ai_usage(runs, saved_clips=len(result.clips))
    print()
    for line in operation_report_lines(summary):
        print(line)

    db = runner.library
    print()
    print("SQLite:", db.stats())
    print("AI usage (from ai_runs):", db.ai_usage_summary(task_id=result.task_id))
    runs = db.list_ai_runs(task_id=result.task_id, limit=10)
    for run in runs[:10]:
        print(
            f"  ai_run#{run['id']} {run['provider']}/{run['model']} {run['operation']} "
            f"{run['status']} latency={run['latency_ms']}ms tokens={run['total_tokens']} "
            f"prompt={run['prompt_version']}"
        )


def _print_discovery_outcome(result: PipelineResult) -> None:
    """Distinguish 'discovery blocked' from 'search ran, zero results'."""

    states = result.discovery_states or {}
    presented = " ".join(f"{name}={state}" for name, state in states.items()) or "(unknown)"
    if result.discovery_blocked:
        print()
        print(f"发现状态: discovery_blocked（搜索未能执行）")
        print(f"  各后端: {presented}")
        if result.discovery_detail:
            print(f"  原因: {result.discovery_detail}")
        print("  提示: 后端不可用请先修好 dtk（--check-douyin）；")
        print("        浏览器需要人工验证/登录请运行 --init-douyin-browser 后重试。")
    elif result.stats.searched_candidates == 0:
        print()
        print("发现状态: 搜索正常执行，但 0 个结果（不是被阻断）")
        print(f"  各后端: {presented}")
    else:
        print()
        print(
            f"发现状态: 搜索正常（{result.stats.searched_candidates} 个候选，"
            f"唯一 {result.stats.unique_candidates}）"
        )
        print(f"  各后端: {presented}")


def run_task(args: argparse.Namespace, settings: AppSettings) -> int:
    """Run the real (or overridden) collection workflow."""

    request = build_request(args, settings)
    # --- backend preflight (section 6) -----------------------------------
    # A configured-but-dead backend must not be walked through every keyword.
    if (request.source or settings.sources.active_source) == "douyin":
        selection = asyncio.run(_preflight_douyin(settings))
        if selection.blocked:
            print()
            print("后端预检未通过: backend_blocked（不会开始关键词采集）")
            for line in selection.summary_lines():
                print(f"  {line}")
            print(
                "  Python 侧诊断结论: 配置的后端地址没有应答或拒绝了凭据；"
                "本地 127.0.0.1:8000 上没有 dtk 在运行。"
            )
            print("  可在 sources.douyin.fallback_base_urls 中配置一个可用的远端 dtk。")
            return 1
        print()
        for line in selection.summary_lines():
            print(f"  {line}")

    runner = TaskRunner(settings)
    LOGGER.info(
        "task: material=%s target=%s source=%s files=%s provider=%s media=%s",
        request.material,
        request.target_clip_count,
        request.source or settings.sources.active_source,
        [str(path) for path in request.local_files] or "-",
        request.provider or settings.ai.active_provider,
        request.media_backend or settings.media.backend,
    )
    result = runner.run(
        request,
        on_event=lambda event: LOGGER.info("%s | %s", event.stage, event.message),
    )
    _print_result(result, runner)
    _print_discovery_outcome(result)
    return 0 if result.status in (TaskStatus.SUCCEEDED, TaskStatus.PARTIAL) and result.clips else 1


def run_demo(
    settings: AppSettings,
    *,
    material: str,
    target: int,
    library_root: Path | None = None,
) -> int:
    """Offline smoke test: mock source + mock AI + placeholder media."""

    LOGGER.info("running mock demo for %r (target=%s)", material, target)
    runner = TaskRunner(settings)
    request = TaskRequest(
        material=material,
        target_clip_count=target,
        min_clip_duration=settings.pipeline.default_min_clip_duration,
        max_clip_duration=settings.pipeline.default_max_clip_duration,
        subtitle_policy=SubtitlePolicy(settings.pipeline.default_subtitle_policy),
        library_root=library_root or settings.paths.library_root,
        sources=["douyin"],
        source="mock",
        provider="mock",
        media_backend="mock",
    )
    result = runner.run(
        request,
        on_event=lambda event: LOGGER.info("%s | %s", event.stage, event.message),
    )
    _print_result(result, runner)
    succeeded = result.status in (TaskStatus.SUCCEEDED, TaskStatus.PARTIAL) and bool(result.clips)
    return 0 if succeeded else 1


# ---------------------------------------------------------------------------
# Milestone 3.7: library inspection, cleanup, retagging and prompt tuning
# ---------------------------------------------------------------------------
def _clip_inventory(settings: AppSettings) -> tuple[Any, list[tuple[Any, str, bool, str]]]:
    """``(library, [(clip, provenance, removable, reason)])`` for every clip."""

    from core.dependencies import build_library
    from core.provenance import ClipIntegrity, classify_provenance, demo_removal_verdict

    library = build_library(settings)
    rows: list[tuple[Any, str, bool, str]] = []
    for clip in library.inventory_clips():
        provenance = clip.provenance or classify_provenance(
            clip.platform, clip.platform_video_id, source_url=clip.source_url
        )
        size_bytes: int | None = None
        try:
            path = Path(clip.file_path)
            if path.exists():
                size_bytes = path.stat().st_size
        except OSError:  # pragma: no cover - defensive
            size_bytes = None
        removable, reason = demo_removal_verdict(
            provenance=provenance,
            integrity=ClipIntegrity(
                width=clip.width, height=clip.height, size_bytes=size_bytes
            ),
        )
        rows.append((clip, provenance, removable, reason))
    return library, rows


def run_list_demo_clips(settings: AppSettings) -> int:
    """``--list-demo-clips``: classify stored clips from data, never filenames."""

    library, rows = _clip_inventory(settings)
    if not rows:
        print("素材库中没有片段。")
        return 0
    removable = [clip.id for clip, _p, can, _r in rows if can]
    print(f"素材库: {settings.paths.library_root}  片段总数: {len(rows)}")
    print(
        f"  {'id':>4} {'provenance':<12} {'size':>11} {'duration':>8}  {'removable':<9} reason"
    )
    for clip, provenance, can_remove, reason in rows:
        path = Path(clip.file_path)
        size = 0
        try:
            if path.exists():
                size = path.stat().st_size
        except OSError:  # pragma: no cover - defensive
            size = 0
        resolution = f"{clip.width}x{clip.height}" if clip.width else "?"
        print(
            f"  {clip.id:>4} {provenance:<12} {resolution:>11} {clip.duration:>7.2f}s  "
            f"{'yes' if can_remove else 'no':<9} {reason}"
        )
        print(f"        file={clip.file_path} ({size // 1024}KB)")
    print()
    print(
        f"可安全删除（provenance=mock）: {len(removable)} 个 -> {removable}"
        if removable
        else "没有可安全删除的占位/演示片段。"
    )
    print("真实抖音片段与未分类片段永远不会被自动删除。")
    return 0


def run_remove_demo_clips(settings: AppSettings, *, confirm: bool, include_local: bool) -> int:
    """``--remove-demo-clips``: safe library level removal, dry run by default."""

    from core.provenance import ClipIntegrity, classify_provenance, demo_removal_verdict

    from core.dependencies import build_library

    library = build_library(settings)
    targets: list[tuple[Any, str, str]] = []
    for clip in library.inventory_clips():
        provenance = clip.provenance or classify_provenance(
            clip.platform, clip.platform_video_id, source_url=clip.source_url
        )
        size_bytes: int | None = None
        try:
            path = Path(clip.file_path)
            if path.exists():
                size_bytes = path.stat().st_size
        except OSError:  # pragma: no cover - defensive
            size_bytes = None
        integrity = ClipIntegrity(
            width=clip.width, height=clip.height, size_bytes=size_bytes
        )
        removable, reason = demo_removal_verdict(
            provenance=provenance, integrity=integrity, include_local_tests=include_local
        )
        if removable:
            targets.append((clip, provenance, reason))

    if not targets:
        print("没有需要清理的占位/演示片段。")
        return 0

    print(f"{'删除' if confirm else '将删除（dry run）'}以下片段:")
    for clip, provenance, reason in targets:
        print(f"  clip #{clip.id} [{provenance}] {clip.file_path}  ({reason})")

    if not confirm:
        print()
        print("未做任何修改。确认后请追加 --yes 执行（或加 --include-local-tests 一并清理本地测试片段）。")
        return 0

    removed = 0
    for clip, provenance, _reason in targets:
        report = library.remove_clip(clip.id or 0)
        if report.found:
            removed += 1
            print(
                f"  已删除 clip #{report.clip_id} [{provenance}]: "
                f"files={report.removed_files} missing={report.missing_files} "
                f"orphan_tags={report.orphaned_tags}"
            )
    print()
    print(f"共删除 {removed} 个片段（源视频 provenance 记录保留）。")
    return 0


def _tagging_tools(settings: AppSettings, *, provider: str | None):
    """Build the gateway + toolkit + library used by retag/A-B commands."""

    from core.dependencies import build_gateway, build_library, build_toolkit
    from core.retag import ClipRetagger

    library = build_library(settings)
    gateway = build_gateway(settings, provider_name=provider, on_call=library.add_ai_run)
    toolkit = build_toolkit(settings, backend=settings.media.backend)
    retagger = ClipRetagger(
        gateway=gateway,
        toolkit=toolkit,
        library=library,
        frames_dir=settings.paths.cache_dir / "frames",
        frame_ratios=tuple(settings.analysis.clip_frame_ratios)
        or (0.2, 0.4, 0.6, 0.8),
        max_width=settings.analysis.preview_max_width,
    )
    return library, gateway, retagger


def run_tag_report(settings: AppSettings, *, limit: int = 40) -> int:
    """``--tag-report``: compact prompt-tuning table (no AI calls)."""

    from core.dependencies import build_library
    from core.tag_audit import analyse_tags, build_tag_rows, report_lines

    library = build_library(settings)
    clips = library.inventory_clips(limit=limit)
    if not clips:
        print("素材库中没有片段。")
        return 0
    rows = build_tag_rows(clips)
    by_version: dict[str, list[Any]] = {}
    for row in rows:
        by_version.setdefault(row.prompt_version or "(unrecorded)", []).append(row)
    print(f"素材库: {settings.paths.library_root}  片段数: {len(rows)}")
    for version, group in by_version.items():
        report = analyse_tags(group, version=version)
        for line in report_lines(report, group, title=f"prompt_version = {version}"):
            print(line)
        print()
    print("以上为人工复核用信息：描述/分数相同只是诊断信号，不会自动淘汰片段。")
    return 0


def run_backfill_clip_metadata(settings: AppSettings, *, confirm: bool) -> int:
    """``--backfill-clip-metadata``: fill empty category/provenance only."""

    from core.dependencies import build_library

    library = build_library(settings)
    changes = library.backfill_clip_metadata(apply=confirm)
    if not changes:
        print("没有需要回填的历史片段（library_category / provenance 都已存在）。")
        return 0
    verb = "已回填" if confirm else "将回填（dry run）"
    print(f"{verb} {len(changes)} 项:")
    for change in changes:
        if change.get("reason"):
            print(
                f"  clip #{change['clip_id']}: {change['field']} "
                f"跳过（{change['reason']}）"
            )
            continue
        print(
            f"  clip #{change['clip_id']}: {change['field']} "
            f"'{change['old']}' -> '{change['new']}'"
        )
    if not confirm:
        print()
        print("未做任何修改。确认后请追加 --yes 执行（只填空值，不覆盖已有数据）。")
    return 0


def run_retag_clip(
    settings: AppSettings,
    *,
    clip_id: int,
    provider: str | None,
    version: str | None,
    confirm: bool,
) -> int:
    """``--retag-clip ID``: re-tag one clip, keeping its media and provenance."""

    from core.dependencies import build_library

    library = build_library(settings)
    clip = library.get_clip(clip_id)
    if clip is None:
        print(f"找不到片段 #{clip_id}")
        return 1
    resolved_provider = (provider or settings.ai.active_provider or "mock").lower()
    if resolved_provider != "mock" and not confirm:
        print(
            f"重新打标会调用真实模型（provider={resolved_provider}）并产生费用。\n"
            f"确认后请追加 --yes 执行。"
        )
        return 0

    library, gateway, retagger = _tagging_tools(settings, provider=provider)
    outcome = asyncio.run(
        retagger.retag(clip_id, version=version or "", apply=True)
    )
    asyncio.run(gateway.aclose())
    if not outcome.ok:
        print(f"重新打标失败: {outcome.error or 'unknown error'}")
        return 1
    updated = library.get_clip(clip_id)
    print(f"片段 #{clip_id} 重新打标完成 (prompt={outcome.version}, tokens={outcome.tokens})")
    print(f"  之前: {clip.material}/{clip.material_form}/{clip.material_state} "
          f"{clip.process_stage} overall={clip.overall_score:.2f} ({clip.description[:40]})")
    if updated is not None:
        print(f"  之后: {updated.material}/{updated.material_form}/{updated.material_state} "
              f"{updated.process_stage} overall={updated.overall_score:.2f} ({updated.description[:40]})")
        print(f"  文件未改动: {updated.file_path}")
        print(f"  provenance: {updated.provenance} | category: {updated.library_category} "
              f"| prompt: {updated.tag_prompt_version}")
    return 0


def run_ab_tagging(
    settings: AppSettings,
    *,
    clip_ids: list[int],
    provider: str | None,
    confirm: bool,
    limit: int = 5,
) -> int:
    """``--ab-tagging``: clip_tagging_v1 vs v2 on existing clips (no writes)."""

    from core.dependencies import build_library
    from core.tag_audit import analyse_tags, report_lines

    library = build_library(settings)
    if not clip_ids:
        candidates = [
            clip
            for clip in library.inventory_clips(limit=200)
            if Path(clip.file_path).exists()
        ]
        clip_ids = [int(clip.id or 0) for clip in candidates[:limit]]
    if not clip_ids:
        print("没有可用于对比的已入库片段。")
        return 1
    resolved_provider = (provider or settings.ai.active_provider or "mock").lower()
    if resolved_provider != "mock" and not confirm:
        print(
            f"对比会调用真实模型（provider={resolved_provider}）并产生费用:\n"
            f"  {len(clip_ids)} 个片段 × 2 个 prompt 版本\n"
            f"确认后请追加 --yes 执行。"
        )
        return 0

    library, gateway, retagger = _tagging_tools(settings, provider=provider)
    results = asyncio.run(
        retagger.compare(clip_ids, versions=("clip_tagging_v1", "clip_tagging_v2"))
    )
    asyncio.run(gateway.aclose())

    print(f"A/B 对比（{len(clip_ids)} 个已有片段，生产标签未被修改）: {clip_ids}")
    for version, outcomes in results.items():
        rows = []
        for outcome in outcomes:
            clip = library.get_clip(outcome.clip_id)
            if clip is None:
                continue
            rows.append(outcome.tag_row(clip))
        report = analyse_tags(rows, version=version)
        print()
        for line in report_lines(report, rows, title=f"prompt_version = {version}"):
            print(line)
    print()
    print("提示: 文本不同不等于更好。请人工复核语义正确性（物料/形态/状态/工序）。")
    return 0


# ---------------------------------------------------------------------------
# Milestone 4: library management, listing, export, health
# ---------------------------------------------------------------------------
def _library_service(settings: AppSettings):
    from core.dependencies import build_library
    from core.library_service import LibraryService

    return LibraryService(build_library(settings), settings)


def _filters_from_args(args: argparse.Namespace):
    from core.library_service import ClipFilters

    statuses = [
        token.strip()
        for token in str(getattr(args, "review_status", "") or "").replace(" ", "").split(",")
        if token.strip()
    ]
    return ClipFilters(
        library_category=getattr(args, "library_category", "") or "",
        material=getattr(args, "material", "") or "",
        material_state=getattr(args, "material_state", "") or "",
        process_stage=getattr(args, "process_stage", "") or "",
        shot_type=getattr(args, "shot_type", "") or "",
        edit_role=getattr(args, "edit_role", "") or "",
        people=getattr(args, "people", "") or "",
        provenance=getattr(args, "provenance", "") or "",
        review_status=statuses,
        favorite="true" if getattr(args, "favorite", False) else "",
        free_text=getattr(args, "free_text", "") or "",
        min_overall_score=getattr(args, "min_overall_score", None),
        max_overall_score=getattr(args, "max_overall_score", None),
        min_duration=getattr(args, "min_duration", None),
        max_duration=getattr(args, "max_duration", None),
    )


def run_list_clips(args: argparse.Namespace, settings: AppSettings) -> int:
    """``--list-clips``: paged listing with the same filters as the UI."""

    service = _library_service(settings)
    filters = _filters_from_args(args)
    page_size = settings.library.clamp_page_size(args.limit or None)
    page = service.fetch_page(filters, sort_by=args.sort, page_size=page_size)
    print(page.summary())
    print(
        f"  {'id':>4} {'分类':<8} {'物料':<6} {'状态':<10} {'工序':<16} "
        f"{'时长':>6} {'评分':>5} {'审核':<10} {'收藏':<4} provenance"
    )
    for clip in page.clips:
        print(
            f"  {clip.id:>4} {clip.library_category[:8]:<8} {clip.material[:6]:<6} "
            f"{str(clip.material_state)[:10]:<10} {str(clip.process_stage)[:16]:<16} "
            f"{clip.duration:>5.2f}s {clip.overall_score:>5.2f} "
            f"{str(clip.review_status)[:10]:<10} {'★' if clip.favorite else '-':<4} "
            f"{clip.provenance or '(legacy)'}"
        )
    missing = [
        (clip.id, problem)
        for clip in page.clips
        for problem in service.missing_files(clip)
    ]
    if missing:
        print()
        print(f"注意: {len(missing)} 个文件问题: {missing}")
    return 0


def run_export_clips(args: argparse.Namespace, settings: AppSettings) -> int:
    """``--export-clips``: JSON/CSV manifest of selected or filtered clips."""

    service = _library_service(settings)
    formats = (
        ["json", "csv"] if args.export_format == "both" else [args.export_format]
    )
    clip_ids = [
        int(token)
        for token in str(args.clips or "").replace(" ", "").split(",")
        if token.strip().isdigit()
    ]
    results = []
    for fmt in formats:
        if clip_ids:
            results.append(service.export_by_ids(clip_ids, fmt=fmt))
        else:
            results.append(
                service.export_filtered(_filters_from_args(args), fmt=fmt, sort_by=args.sort)
            )
    exit_code = 0
    for result in results:
        if result.get("ok"):
            print(
                f"[ok] {result['format'].upper()} 导出 {result['rows']} 条 -> {result['path']}"
            )
        else:
            print(f"[warn] 导出失败: {result.get('error')}")
            exit_code = 1
    print(f"导出目录: {settings.library.exports_dir}")
    print("提示: 该清单是通用素材清单（不是自动剪辑时间线）。")
    return exit_code


def run_check_library(settings: AppSettings) -> int:
    """``--check-library``: read-only inventory, nothing is deleted."""

    service = _library_service(settings)
    report = service.health()
    overview = service.overview()
    print(f"[info] 素材库根目录: {report['library_root']}")
    print(f"[ok] 数据库片段记录: {report['clips_in_db']}")
    print(f"[ok] 实际存在的视频文件: {report['videos_present']}")
    missing_videos = report["missing_videos"]
    print(
        f"[{'warn' if missing_videos else 'ok'}] 缺失视频: {len(missing_videos)}"
    )
    for item in missing_videos:
        print(f"    clip #{item['clip_id']} {item['reason']}: {item['path']}")
    missing_thumbs = report["missing_thumbnails"]
    print(
        f"[{'warn' if missing_thumbs else 'ok'}] 缺失缩略图: {len(missing_thumbs)}"
    )
    for item in missing_thumbs:
        print(f"    clip #{item['clip_id']} {item['reason']}: {item['path']}")
    orphans = report["orphan_media"]
    print(f"[{'warn' if orphans else 'ok'}] 未被数据库引用的视频: {len(orphans)}")
    for path in orphans[:20]:
        print(f"    {path}")
    orphan_thumbs = report["orphan_thumbnails"]
    print(f"[{'warn' if orphan_thumbs else 'ok'}] 未被引用的缩略图: {len(orphan_thumbs)}")
    for path in orphan_thumbs[:20]:
        print(f"    {path}")
    missing_cleanups = report.get("missing_cleanup_outputs") or []
    print(
        f"[{'warn' if missing_cleanups else 'ok'}] 字幕清理派生文件缺失: "
        f"{len(missing_cleanups)}"
    )
    for item in missing_cleanups:
        print(
            f"    clip #{item.get('clip_id')} {item.get('reason')}: {item.get('path')}"
        )
    print(
        f"[info] 字幕清理记录: {report.get('cleanup_records', 0)} | "
        f"存在的派生文件: {report.get('cleanup_outputs_present', 0)}"
    )
    missing_approved = int(report.get("missing_approved_derivatives") or 0)
    print(
        f"[{'warn' if missing_approved else 'ok'}] "
        f"已批准但派生文件缺失: {missing_approved}"
    )
    print()
    print(
        f"[info] 素材总数 {overview['total']} | 真实抖音 {overview['real']} | "
        f"待审核 {overview['unreviewed']} | 已批准 {overview['approved']} | "
        f"收藏 {overview['favorite']}"
    )
    print("[info] 本命令只读：不会删除或重建任何文件。")
    return 0


# ---------------------------------------------------------------------------
# Milestone 5: coverage, acquisition strategy, maintenance
# ---------------------------------------------------------------------------
def _coverage(settings: AppSettings):
    from core.coverage import CoverageAnalyzer
    from core.dependencies import build_library

    return CoverageAnalyzer(build_library(settings), settings.coverage)


def _coverage_category(settings: AppSettings, wanted: str | None) -> str | None:
    """Resolve a CLI category argument; ``''``/None means "all categories"."""

    if wanted is None:
        return None
    text = wanted.strip()
    if not text or text in ("all", "*", "全部"):
        return None
    return text


def run_coverage_report(settings: AppSettings, category: str | None) -> int:
    """``--coverage-report``: the operator's coverage matrix (sections 1-6/32)."""

    analyzer = _coverage(settings)
    resolved = _coverage_category(settings, category)
    known = [name for name, _count in analyzer.categories()]
    if resolved and known and resolved not in known:
        print(f"[warn] 素材库中没有分类 {resolved!r}；现有分类: {', '.join(known)}")
        return 1
    report = analyzer.report(resolved)
    print(f"# {report.library_category}")
    print(
        f"总素材: {report.total} | 真实抖音: {report.real} | 已批准: {report.approved} | "
        f"收藏: {report.favorite} | 统计口径: {report.count_mode}"
    )
    print()
    print("工序覆盖 (当前 / 目标):")
    for stage in report.stages:
        if stage.total or stage.target:
            print(
                f"  {stage.stage:<18} {stage.total:>4} / {stage.target:<3} "
                f"{stage.priority:<9} 已批准={stage.approved} 收藏={stage.favorite}"
            )
    print()
    print(f"镜头覆盖: {report.shots}")
    print(f"物料状态: {report.states}")
    print(f"剪辑角色: {report.edit_roles}")
    print(f"质量分布: {report.quality}")
    print(
        "平均分: " + ", ".join(f"{key}={value:.3f}" for key, value in report.score_averages.items())
    )
    print(
        f"审核覆盖: 总 {report.total} | 已批准 {report.review.get('approved', 0)} | "
        f"待审核 {report.review.get('unreviewed', 0)} | 需复核 {report.review.get('needs_review', 0)} | "
        f"已拒绝 {report.review.get('rejected', 0)} | 收藏 {report.review.get('favorite', 0)}"
    )
    print()
    gaps = report.gaps
    if gaps:
        print("缺口 (按优先级):")
        for index, gap in enumerate(gaps[:12], start=1):
            print(
                f"  {index:>2}. {gap.stage:<18} current={gap.total:<3} target={gap.target:<3} "
                f"missing={gap.missing:<3} {gap.priority} ({gap.ratio:.0%})"
            )
    else:
        print("没有缺口：所有已配置工序都达到目标。")
    return 0


def run_coverage_gaps(settings: AppSettings, category: str | None) -> int:
    """``--coverage-gaps``: gaps + deterministic search recommendations (9/33)."""

    analyzer = _coverage(settings)
    resolved = _coverage_category(settings, category)
    gaps = analyzer.gap_report(resolved)
    label = resolved or "(全部)"
    if not gaps:
        print(f"{label}: 没有缺口。")
        return 0
    print(f"# {label} 优先补采")
    for index, gap in enumerate(gaps[:10], start=1):
        print(
            f"{index}. {gap.stage}（current={gap.total} target={gap.target} "
            f"missing={gap.missing} {gap.priority}）"
        )
        for query in analyzer.recommended_queries(resolved, gap.stage):
            print(f"     - {query}")
    print()
    print("提示: 这些只是建议词，不会自动开始采集；请由操作者决定。")
    return 0


def run_search_yield_report(settings: AppSettings, category: str | None = None) -> int:
    """``--search-yield-report``: per-query yield, ranking and AI cost (10-12)."""

    analyzer = _coverage(settings)
    print("# 搜索词产出与排名（按有效产出）")
    print(
        f"  {'query':<20}{'runs':>5}{'cand':>7}{'uniq':>7}{'acc':>6}{'dl':>5}{'clips':>7}"
        f"{'cand→clip':>11}{'score':>8}"
    )
    for row in analyzer.ranked_queries(limit=30):
        rate = row["candidate_to_clip_rate"]
        print(
            f"  {row['query'][:20]:<20}{row['runs']:>5}{row['candidates']:>7}"
            f"{row['unique_candidates']:>7}{row['preview_accepted']:>6}"
            f"{row['downloads']:>5}{row['clips']:>7}"
            f"{(f'{rate:.1%}' if rate is not None else 'n/a'):>11}{row['usefulness']:>8}"
        )
    zero = [row for row in analyzer.search_yield_report() if row["clips"] == 0]
    if zero:
        print()
        print(f"0 产出的搜索词（仍然可见）: {', '.join(row['query'] for row in zero)}")
    print()
    print("# AI 成本归属（无法可靠归属的记为 unavailable）")
    print(f"  {'query':<22}{'videos':>7}{'calls':>7}{'tokens':>9}{'clips':>7}{'tok/clip':>10}  attribution")
    for row in analyzer.query_cost_analysis()[:20]:
        per_clip = row["tokens_per_clip"]
        print(
            f"  {row['query'][:22]:<22}{row['videos']:>7}{row['ai_calls']:>7}"
            f"{row['tokens']:>9}{row['clips']:>7}"
            f"{(f'{per_clip:.0f}' if per_clip else 'n/a'):>10}  {row['attribution']}"
        )
    print()
    print("# 来源 / 作者产出")
    for row in analyzer.source_yield(limit=8):
        print(
            f"  来源 {row['platform_video_id'][:18]:<18} clips={row['clips']} "
            f"avg={row['average_score']:.2f} approved={row['approved']} | {row['title'][:32]}"
        )
    for row in analyzer.author_yield(limit=5):
        print(
            f"  作者 {row['author'][:16]:<16} videos={row['videos_processed']} "
            f"clips={row['clips']} avg={row['average_score']:.2f} approved={row['approved']}"
        )
    return 0


def run_review_export(settings: AppSettings) -> int:
    from core.dependencies import build_library
    from core.library_ops import LibraryOps

    ops = LibraryOps(build_library(settings), settings)
    path = ops.export_review_csv()
    print(f"[ok] 已导出审核状态 CSV: {path}")
    print("     字段: clip_id, review_status, review_note, favorite")
    return 0


def run_review_import(settings: AppSettings, path: str, *, confirm: bool) -> int:
    from core.dependencies import build_library
    from core.library_ops import LibraryOps

    ops = LibraryOps(build_library(settings), settings)
    report = ops.import_review_csv(Path(path), dry_run=not confirm)
    print(report.summary())
    for item in report.items[:50]:
        print(
            f"  clip #{item['clip_id']}: {item['old_status']}->{item['new_status']} "
            f"favorite {item['old_favorite']}->{item['new_favorite']} "
            f"note {item['old_note'][:12]!r}->{item['new_note'][:12]!r}"
        )
    for item in report.skipped[:10]:
        print(f"  跳过: {item}")
    for error in report.errors[:10]:
        print(f"  错误: {error}")
    if not confirm:
        print()
        print("未做任何修改。确认后请追加 --yes 执行（导入只改审核字段，不动 AI 标签）。")
    return 1 if report.errors else 0


def run_repair_thumbnails(settings: AppSettings, *, confirm: bool) -> int:
    from core.dependencies import build_library
    from core.library_ops import LibraryOps

    ops = LibraryOps(build_library(settings), settings)
    report = ops.repair_thumbnails(dry_run=not confirm)
    print(report.summary())
    for item in report.items[:50]:
        new = item.get("new_thumbnail", "")
        print(f"  clip #{item['clip_id']}: {item['thumbnail'] or '(空)'} {('-> ' + new) if new else ''}")
    for error in report.errors[:10]:
        print(f"  错误: {error}")
    if not confirm:
        print()
        print("未做任何修改。确认后请追加 --yes 执行（只重建缩略图，不动视频）。")
    return 0


def run_quarantine_orphans(settings: AppSettings, *, confirm: bool) -> int:
    from core.dependencies import build_library
    from core.library_ops import LibraryOps

    ops = LibraryOps(build_library(settings), settings)
    report = ops.quarantine_orphans(dry_run=not confirm)
    print(report.summary())
    print(f"隔离目录: {settings.library.quarantine_dir}")
    for item in report.items[:50]:
        print(
            f"  {item['original_path']} -> {item['quarantine_path']} "
            f"({item['reason']}, {item['timestamp']})"
        )
    for item in report.skipped[:10]:
        print(f"  跳过: {item}")
    for error in report.errors[:10]:
        print(f"  错误: {error}")
    print("说明: 孤立文件只会被移动，永远不会被删除。")
    if not confirm:
        print("未做任何修改。确认后请追加 --yes 执行。")
    return 0


def run_maintenance_log(settings: AppSettings, *, limit: int = 50) -> int:
    from core.dependencies import build_library

    entries = build_library(settings).list_maintenance_log(limit=limit)
    if not entries:
        print("维护日志为空（没有执行过维护操作）。")
        return 0
    print(f"{'id':>4} {'operation':<24}{'target':<14}{'created_at':<22} details")
    for entry in entries:
        print(
            f"{entry['id']:>4} {entry['operation'][:24]:<24}"
            f"{str(entry['target_id'] or entry['target_type'])[:14]:<14}"
            f"{entry['created_at'][:19]:<22} {str(entry['details'])[:60]}"
        )
    return 0


def run_preset_command(args: argparse.Namespace, settings: AppSettings) -> int:
    """``--preset-list`` / ``--preset-save`` / ``--preset-delete`` (sections 21/22)."""

    from core.dependencies import build_library
    from core.library_service import ClipFilters, preset_from_filters, preset_to_filters

    library = build_library(settings)
    if args.preset_list:
        presets = library.list_filter_presets()
        if not presets:
            print("没有已保存的筛选预设。")
            return 0
        for preset in presets:
            print(f"  {preset['name']}: {preset['filters']}")
        return 0
    if args.preset_delete:
        removed = library.delete_filter_preset(args.preset_delete)
        print(f"删除预设 {args.preset_delete!r}: {removed} 行")
        return 0
    if args.preset_save:
        filters = preset_from_filters(_filters_from_args(args))
        # validate round-trip before persisting (a preset must never hold SQL)
        preset_to_filters({"name": args.preset_save, "filters": filters})
        preset_id = library.save_filter_preset(args.preset_save, filters)
        print(f"[ok] 已保存筛选预设 #{preset_id} {args.preset_save!r}: {filters}")
        return 0
    return 0


# ---------------------------------------------------------------------------
# Milestone 6: measured subtitle analysis
# ---------------------------------------------------------------------------
def _subtitle_ops(settings: AppSettings, args: argparse.Namespace | None = None):
    from core.dependencies import build_library
    from core.subtitle_ops import SubtitleOps

    if args is not None and getattr(args, "subtitle_engine", None):
        settings.subtitle_analysis.engine = args.subtitle_engine
    return SubtitleOps(build_library(settings), settings)


def run_subtitle_report(
    settings: AppSettings, category: str | None, args: argparse.Namespace | None = None
) -> int:
    """``--subtitle-report``: measured class distribution (sections 31/46)."""

    ops = _subtitle_ops(settings, args)
    label = (category or "").strip() or None
    report = ops.report(library_category=label)
    analyzer_status = ops._analyzer().status()  # noqa: SLF001 - diagnostics only
    print(f"# 字幕分析报告 {('分类=' + label) if label else '(全部分类)'}")
    print(
        f"素材总数 {report.total_clips} | 已有测量 {report.measured_clips} | "
        f"平均字幕洁净度 {report.average_cleanliness if report.average_cleanliness is not None else 'n/a'}"
        f"（已测量片段 {report.measured_average_cleanliness if report.measured_average_cleanliness is not None else 'n/a'}）"
    )
    print(
        f"检测引擎: {analyzer_status['engine']} (available={analyzer_status['available']}) "
        f"| 判定方式分布: {report.decision_sources}"
    )
    print()
    print("字幕分类计数:")
    for name, count in sorted(report.classification_counts.items(), key=lambda item: -item[1]):
        print(f"  {name:<22}{count:>5}")
    print(f"  分组: {report.bucket_counts}")
    print()
    print(
        f"字幕原因淘汰的来源数量: {report.subtitle_rejections} "
        f"(预览淘汰总数 {report.preview_rejections})"
    )
    print()
    print("按素材分类的干净/简单/复杂分布:")
    print(f"  {'分类':<12}{'片段':>5}{'clean':>7}{'simple':>7}{'complex':>8}{'unknown':>8}")
    for row in report.per_category:
        print(
            f"  {row['library_category'][:12]:<12}{row['clips']:>5}{row.get('clean', 0):>7}"
            f"{row.get('simple', 0):>7}{row.get('complex', 0):>8}{row.get('unknown', 0):>8}"
        )
    print()
    print("搜索词字幕淘汰率（数据可归属时）:")
    for row in ops.search_yield_subtitle_insight(limit=8):
        rate = row["subtitle_rejection_rate"]
        print(
            f"  {row['query'][:18]:<18} seen={row['candidates_seen']:<3} "
            f"preview_rejected={row['preview_rejected']:<3} subtitle_rejected={row['subtitle_rejected']:<3} "
            f"rate={(f'{rate:.0%}' if rate is not None else 'n/a')}"
        )
    return 0


def run_analyze_subtitles(
    settings: AppSettings,
    clip_id: int,
    *,
    apply: bool,
    args: argparse.Namespace | None = None,
) -> int:
    """``--analyze-subtitles``: measure one stored clip (dry-run by default)."""

    ops = _subtitle_ops(settings, args)
    outcome = asyncio.run(ops.analyze_clip(clip_id, apply=apply))
    if outcome.result is None:
        print(f"[warn] 片段 #{clip_id} 分析失败: {outcome.error}")
        return 1
    measured = outcome.result
    if measured.is_unavailable:
        print(f"[warn] 本地字幕分析不可用: {measured.unavailable_reason}")
        print("        采集流程会退回 Qwen 字幕判定（不会中断）。")
        return 1
    print(f"# 片段 #{clip_id} 字幕测量（analysis={measured.analysis_version}）")
    print(f"  当前入库值: {outcome.previous_type} (score={outcome.previous_score:.2f})")
    print(f"  测量分类: {measured.classification}  洁净度 {measured.cleanliness_score:.2f}")
    print(
        f"  文字区域: 平均 {measured.avg_text_regions:.1f}/帧，最多 {measured.max_text_regions}；"
        f"平均覆盖率 {measured.total_text_area_ratio_avg:.3f}，最大 {measured.largest_text_area_ratio:.3f}"
    )
    print(
        f"  时间持续性: 有文字 {measured.text_presence_ratio:.0%}，底部 {measured.bottom_persistence:.0%}，"
        f"中央大字 {measured.center_persistence:.0%}，多区域 {measured.multi_region_persistence:.0%}，"
        f"横幅 {measured.band_persistence:.0%}"
    )
    print(
        f"  判定方式: {measured.decision_source} | 引擎: {measured.engine} | "
        f"帧数: {measured.frame_count} | 耗时: {measured.latency_ms}ms"
    )
    if measured.promotion_text_detected:
        print("  检测到推广类文字（价格/联系方式等关键词）")
    if measured.watermark_only:
        print("  仅检测到角标水印")
    for check in (measured.evidence or {}).get("checks", []):
        print(f"  证据: {check}")
    if apply:
        print("  已写入该片段的测量字段（subtitle_type/subtitle_score/subtitle_analysis_json）。")
    else:
        print()
        print("  未修改任何数据。确认后请追加 --yes 写入测量字段。")
    return 0


def run_analyze_subtitles_all(
    settings: AppSettings,
    args: argparse.Namespace,
) -> int:
    """``--analyze-subtitles-all``: bounded bulk analysis (dry run unless --yes)."""

    ops = _subtitle_ops(settings, args)
    limit = int(args.limit or 20)
    apply = bool(args.yes)
    results = asyncio.run(
        ops.analyze_many(
            library_category=getattr(args, "library_category", None) or None,
            limit=limit,
            apply=apply,
        )
    )
    if not results:
        print("没有可分析的片段。")
        return 0
    print(f"{'将分析' if not apply else '已分析'} {len(results)} 个片段（上限 {limit}）:")
    print(
        f"  {'clip':>5}{'分类':<10}{'原分类':<18}{'测量分类':<18}{'洁净度':>7}"
        f"{'覆盖率':>8}{'区域/帧':>8}  判定"
    )
    for item in results:
        row = item.row()
        print(
            f"  {row[0]:>5}{row[1][:10]:<10}{row[2][:18]:<18}{row[3][:18]:<18}"
            f"{row[4]:>7}{row[5]:>8}{row[6]:>8}  {row[7]}"
        )
    failures = [item for item in results if not item.ok]
    if failures:
        print()
        print(f"失败 {len(failures)} 个: {[item.clip_id for item in failures]}")
    if not apply:
        print()
        print("未修改任何数据。确认后请追加 --yes 写入测量字段（不会改动视频与 AI 标签）。")
    return 0


# ---------------------------------------------------------------------------
# Milestone 9.2: conservative local subtitle cleanup (derivative only)
# ---------------------------------------------------------------------------
def _subtitle_cleanup_service(settings: AppSettings):
    from core.dependencies import build_library
    from core.subtitle_cleanup import SubtitleCleanupService

    return SubtitleCleanupService(build_library(settings), settings)


def run_subtitle_cleanup(
    settings: AppSettings,
    clip_id: int,
    *,
    force: bool = False,
    engine: str | None = None,
    version: str | None = None,
) -> int:
    """``--subtitle-cleanup CLIP_ID``: one conservative derivative attempt."""

    from core.subtitle_cleanup_models import CleanupStatus

    if engine:
        settings.subtitle_cleanup.engine = str(engine)
    if version:
        settings.subtitle_cleanup.version = str(version)
    service = _subtitle_cleanup_service(settings)
    outcome = asyncio.run(service.cleanup_clip(int(clip_id), force=force))
    for line in outcome.lines():
        print(line)
    if outcome.status is CleanupStatus.FAILED_PROCESSING and outcome.reason == "clip_not_found":
        return 1
    return 0


def run_subtitle_cleanup_report(settings: AppSettings) -> int:
    """``--subtitle-cleanup-report``: status distribution and success metrics."""

    from core.subtitle_cleanup_models import CleanupStatus

    service = _subtitle_cleanup_service(settings)
    report = service.report()
    counts = report["counts"]
    print("# 字幕清理生产报告 (subtitle_cleanup_v1)")
    print(f"素材库片段总数: {counts['total_library_clips']} | 清理记录: {report['total_records']}")
    print()
    print("生产状态计数:")
    for key in (
        "not_needed",
        "ineligible",
        "eligible_unprocessed",
        "succeeded_pending_review",
        "approved",
        "rejected",
        "failed_processing",
        "failed_quality",
        "residual_subtitle",
        "missing_derivative",
    ):
        print(f"  {key:<26}{counts.get(key, 0):>5}")
    print()
    successes = report["successes"]
    print(f"成功清理片段: {len(successes)}")
    if successes:
        print(
            f"  {'clip':>5} {'class':<18}{'clean before':>13}{'after':>8}{'delta':>8}"
            f"{'regions':>10}{'mask':>9}{'outside':>9}{'sec':>8}  review"
        )
        for row in successes:
            print(
                f"  {row['clip_id']:>5} {row['subtitle_class'][:18]:<18}"
                f"{row['before_cleanliness']:>13.3f}{row['after_cleanliness']:>8.3f}"
                f"{row['cleanliness_delta']:>+8.3f}"
                f"{str(row['before_regions']):>5}/{str(row['after_regions']):<4}"
                f"{row['masked_area_ratio']:>9.4f}"
                f"{('- ' if row['outside_mask_mean_diff'] is None else str(row['outside_mask_mean_diff'])):>9}"
                f"{row['processing_seconds']:>8.2f}  {row['review_status']}"
            )
    if report["class_metrics"]:
        print()
        print("按原字幕分类的洁净度改善:")
        for item in report["class_metrics"]:
            print(
                f"  {item['subtitle_class']:<18} n={item['count']:<4} "
                f"avg_cleanliness_delta={item['average_cleanliness_delta']:+.3f}"
            )
    print()
    print(
        "提示: succeeded_pending_review 的派生文件不会成为 preferred_media_path；"
        "只有人工 approved 且文件健康的派生才会被优先使用。"
    )
    return 0


def run_subtitle_cleanup_batch(
    settings: AppSettings,
    *,
    limit: int,
    force: bool = False,
) -> int:
    """``--subtitle-cleanup-batch --limit N``: explicitly bounded batch."""

    service = _subtitle_cleanup_service(settings)
    outcomes = asyncio.run(service.cleanup_batch(limit=limit, force=force))
    if not outcomes:
        print("没有可处理的片段。")
        return 0
    print(f"已处理 {len(outcomes)} 个片段（显式上限 {limit}）:")
    for outcome in outcomes:
        print(
            f"  clip #{outcome.clip_id:<5} {outcome.status.value:<20}"
            f"{outcome.reason or '-'}"
        )
    return 0


def run_subtitle_cleanup_candidates(
    settings: AppSettings,
    *,
    category: str | None = None,
    limit: int | None = None,
) -> int:
    """``--subtitle-cleanup-candidates``: read-only eligibility scan."""

    service = _subtitle_cleanup_service(settings)
    candidates = service.candidate_scan(category=category, limit=limit)
    print("# 字幕清理候选扫描（dry-run，不处理、不调用云端）")
    if category:
        print(f"分类过滤: {category}")
    if not candidates:
        print("没有候选片段。")
        return 0
    print(
        f"  {'clip':>5} {'分类':<10} {'subtitle_class':<18} {'洁净':>5} "
        f"{'时长':>6} {'eligibility':<20} {'cleanup':<20} {'review':<9} reason"
    )
    for item in candidates:
        row = item.row()
        print(
            f"  {row[0]:>5} {row[1][:10]:<10} {row[3][:18]:<18} {row[4]:>5.2f} "
            f"{row[5]:>5.2f}s {item.eligibility:<20} {row[7][:20]:<20} "
            f"{row[8][:9]:<9} {row[9]}"
        )
    eligible = [item for item in candidates if item.eligible]
    unprocessed = [item for item in candidates if item.eligibility == "eligible_unprocessed"]
    print()
    print(
        f"eligible={len(eligible)} | eligible_unprocessed={len(unprocessed)} | "
        f"total={len(candidates)}"
    )
    return 0


def run_subtitle_cleanup_review(
    settings: AppSettings,
    *,
    clip_id: int,
    status: str,
    note: str,
    failure_class: str,
    version: str | None = None,
) -> int:
    service = _subtitle_cleanup_service(settings)
    if version:
        service.config.version = str(version)
    ok, message = service.review_cleanup(
        int(clip_id),
        status=status,
        note=note,
        failure_class=failure_class,
        version=version,
    )
    print(f"[{'ok' if ok else 'warn'}] {message}")
    return 0 if ok else 1


def run_cleanup_verify(settings: AppSettings, clip_id: int) -> int:
    service = _subtitle_cleanup_service(settings)
    result = asyncio.run(service.verify_derivative(int(clip_id)))
    print(f"# 字幕清理派生验证 clip #{clip_id}: {'ok' if result['ok'] else 'failed'}")
    for name, passed in result["checks"].items():
        print(f"  [{'ok' if passed else 'warn'}] {name}")
    if result.get("details"):
        print(f"  详情: {result['details']}")
    return 0 if result["ok"] else 1


def run_cleanup_delete_derivative(
    settings: AppSettings,
    clip_id: int,
    *,
    confirm: bool,
    note: str = "",
) -> int:
    if not confirm:
        print(
            f"[warn] 删除派生文件需要显式确认："
            f"python app.py --cleanup-delete-derivative {clip_id} --yes"
        )
        return 0
    service = _subtitle_cleanup_service(settings)
    ok, message = service.delete_derivative(int(clip_id), note=note)
    print(f"[{'ok' if ok else 'warn'}] {message}")
    return 0 if ok else 1


def run_subtitle_cleanup_review_pack(
    settings: AppSettings,
    clip_id: str | int | None,
    *,
    version: str | None = None,
) -> int:
    service = _subtitle_cleanup_service(settings)
    if version:
        service.config.version = str(version)
    ids = None
    if clip_id not in (None, ""):
        ids = [int(clip_id)]
    packs = asyncio.run(service.build_review_pack(clip_ids=ids))
    if not packs:
        print("没有可生成复核包的成功清理派生文件。")
        return 1
    print(f"已生成 {len(packs)} 个字幕清理复核包:")
    for path in packs:
        print(f"  {path}")
    return 0


def _cloud_cleanup_service(settings: AppSettings):
    from core.cloud_cleanup import CloudCleanupService
    from core.dependencies import build_library

    return CloudCleanupService(build_library(settings), settings)


def run_check_volcengine_cleanup(settings: AppSettings) -> int:
    service = _cloud_cleanup_service(settings)
    readiness = service.readiness()
    print("# Volcano Engine VOD refined subtitle erase readiness")
    print("\n".join(readiness.summary_lines()))
    return 0 if readiness.ready else 1


def run_cloud_cleanup_preflight(settings: AppSettings, clip_id: int) -> int:
    service = _cloud_cleanup_service(settings)
    outcome = asyncio.run(service.preflight_clip(int(clip_id)))
    print("# Volcano 字幕擦除零费用预检（不会上传、不会调用付费 API）")
    for line in outcome.lines():
        print(line)
    return 0 if outcome.status.value in {"pending", "not_needed", "ineligible"} else 1


def run_cloud_cleanup(
    settings: AppSettings,
    clip_id: int,
    *,
    engine: str,
    force: bool = False,
) -> int:
    from core.subtitle_cleanup_models import CleanupStatus

    settings.cloud_cleanup.enabled = True
    settings.cloud_cleanup.engine = str(engine or "volcengine")
    service = _cloud_cleanup_service(settings)
    outcome = asyncio.run(service.cleanup_clip(int(clip_id), force=force))
    for line in outcome.lines():
        print(line)
    if outcome.status is CleanupStatus.FAILED_PROCESSING and outcome.reason == "clip_not_found":
        return 1
    return 0


def run_cloud_cleanup_batch(
    settings: AppSettings,
    *,
    limit: int,
    engine: str,
    force: bool = False,
) -> int:
    settings.cloud_cleanup.enabled = True
    settings.cloud_cleanup.engine = str(engine or "volcengine")
    service = _cloud_cleanup_service(settings)
    requested_limit = max(1, int(limit))
    hard_limit = max(1, int(settings.cloud_cleanup.max_paid_tasks_per_run))
    effective_limit = min(requested_limit, hard_limit)
    clips = service.library.inventory_clips(limit=effective_limit)
    if not clips:
        print("没有可处理的片段。")
        return 0
    if requested_limit > hard_limit:
        print(
            f"[warn] 请求 {requested_limit} 个片段，已按付费保护上限缩减为 "
            f"{effective_limit} 个。"
        )
    print(
        f"云清理显式批次：最多 {effective_limit} 个片段"
        f"（付费调用硬上限 {hard_limit}，逐条审计）"
    )
    for clip in clips:
        if clip.id is None:
            continue
        outcome = asyncio.run(service.cleanup_clip(int(clip.id), force=force))
        print(
            f"  clip #{outcome.clip_id:<5} {outcome.status.value:<20}"
            f"{outcome.reason or '-'}"
        )
    return 0


# ---------------------------------------------------------------------------
# Milestone 7: collection plans (create / approve / run / control)
# ---------------------------------------------------------------------------
def _plan_service(settings: AppSettings):
    from core.dependencies import build_library
    from core.plan_service import PlanService

    return PlanService(build_library(settings), settings)


def run_create_collection_plan(args: argparse.Namespace, settings: AppSettings) -> int:
    """``--create-collection-plan``: draft only, never starts acquisition."""

    service = _plan_service(settings)
    plan, action = service.create_plan(
        args.create_collection_plan,
        count_mode=args.count_mode,
        include_healthy=args.include_healthy,
        name=args.plan_name,
    )
    print(action.message)
    if plan is None:
        return 1
    print()
    print("\n".join(service.report_lines(plan)))
    print()
    print("下一步: 检查 / 编辑后执行 --approve-collection-plan", plan.id, "再 --run-collection-plan", plan.id)
    print("提示: 草稿不会执行任何采集；dry-run 可以查看将要运行的查询与预算。")
    return 0


def _production_service(settings: AppSettings):
    from core.dependencies import build_library
    from core.production import ProductionCoverageService

    return ProductionCoverageService(build_library(settings), settings)


def run_production_gaps(
    settings: AppSettings,
    *,
    category: str | None = None,
    stage: str | None = None,
    include_covered: bool = False,
    limit: int = 12,
) -> int:
    """``--production-gaps``: ranked, inspectable production coverage view."""

    service = _production_service(settings)
    lines = service.report_lines(
        category=category,
        stage=stage,
        include_covered=include_covered,
        limit=limit,
    )
    print("\n".join(lines))
    print()
    print("提示: --create-production-plan 会用最高的缺口生成一个**草稿**计划（仍需人工批准）。")
    return 0


def run_production_ready_report(settings: AppSettings) -> int:
    """``--production-ready-report``: semantic clips + preferred media health."""

    from core.dependencies import build_library
    from core.production_ready import ProductionReadyService

    service = ProductionReadyService(build_library(settings), settings)
    report = service.report()
    aggregates = report.aggregates
    print(f"# 生产就绪报告 ({report.cleanup_version})")
    print(
        f"语义片段 {aggregates['total_semantic_clips']} | "
        f"original-only {aggregates['original_only']} | "
        f"cleaned-preferred {aggregates['cleaned_preferred']} | "
        f"生产就绪 {aggregates['production_ready_clips']} | "
        f"缺失首选媒体 {aggregates['missing_preferred_media']}"
    )
    print(
        f"cleanup: pending {aggregates['cleanup_pending']} | "
        f"approved {aggregates['cleanup_approved']} | "
        f"rejected {aggregates['cleanup_rejected']} | "
        f"failed {aggregates['cleanup_failed']} | "
        f"missing-approved-derivative {aggregates['missing_approved_derivatives']}"
    )
    print()
    print(
        f"  {'clip':>5} {'分类':<9} {'工序':<16} {'质检':>5} {'字幕':<16} "
        f"{'cleanup':<18} {'clean-rev':<10} {'clip-rev':<10} {'preferred':<8} "
        f"{'health':<12} path"
    )
    for row in report.rows:
        print(
            f"  {row.clip_id:>5} {row.category[:9]:<9} {row.process_stage[:16]:<16} "
            f"{row.quality:>5.2f} {row.subtitle_class[:16]:<16} "
            f"{(row.cleanup_status or '-')[:18]:<18} {(row.review_status or '-')[:10]:<10} "
            f"{(row.clip_review_status or '-')[:10]:<10} "
            f"{row.preferred_kind:<8} {row.file_health:<12} {row.preferred_path}"
        )
    print()
    print("生产就绪 = 首选媒体文件存在且健康；不要求必须完成字幕清理。")
    return 0 if aggregates["missing_preferred_media"] == 0 else 1


def run_duration_recheck(settings: AppSettings, *, limit: int = 5) -> int:
    """Milestone 9.6 controlled recheck of historical unresolved sources."""

    from types import SimpleNamespace

    from core.dependencies import build_downloader, build_source, build_toolkit
    from core.dependencies import build_library
    from core.models import PipelineStats, RejectReason, VideoCandidate
    from core.orchestrator import CollectionOrchestrator

    if not asyncio.run(_preflight_douyin(settings)).usable:
        print("[warn] no usable Douyin backend for duration recheck")
        return 1
    library = build_library(settings)
    toolkit = build_toolkit(settings, backend=settings.media.backend)
    source = build_source(settings, name="douyin", toolkit=toolkit)
    downloader = build_downloader(settings, source_name="douyin")
    deps = SimpleNamespace(
        toolkit=toolkit,
        source=source,
        downloader=downloader,
        candidate_filter=SimpleNamespace(
            min_duration=settings.pipeline.candidate_min_duration,
            max_duration=settings.pipeline.candidate_max_duration,
        ),
    )
    orchestrator = CollectionOrchestrator.__new__(CollectionOrchestrator)
    orchestrator.deps = deps
    orchestrator.settings = settings
    rows = library.database.query(
        "SELECT * FROM source_videos WHERE reject_reason = ? "
        "ORDER BY id DESC LIMIT ?",
        (str(RejectReason.DURATION_UNKNOWN_UNRESOLVED), int(max(1, limit))),
    )
    if not rows:
        print("没有 duration_unknown_unresolved 历史来源。")
        return 0
    print("# Duration media ladder recheck（read-only，不修改历史状态）")
    for row in rows:
        candidate = VideoCandidate(
            platform=str(row["platform"]),
            platform_video_id=str(row["platform_video_id"]),
            source_url=str(row["source_url"]),
            title=str(row["title"] or ""),
            author=str(row["author"] or ""),
            duration=None,
            media_url=row["media_url"] or None,
        )
        stats = PipelineStats()
        staged: list[Path] = []
        duration, state, detail = asyncio.run(
            orchestrator._resolve_duration(candidate, stats=stats, staged=staged)
        )
        method = str(candidate.metadata.get("duration_method") or "")
        print(
            f"  {candidate.platform_video_id} | before=duration_unknown_unresolved "
            f"| after={state} | duration={duration} | method={method or '-'} | "
            f"detail={detail[:100]}"
        )
        library.log_maintenance(
            "duration_resolution_recheck",
            target_type="source_video",
            target_id=candidate.platform_video_id,
            details={
                "platform_video_id": candidate.platform_video_id,
                "state": state,
                "duration": duration,
                "method": method,
                "detail": detail[:200],
                "probe_downloads": stats.probe_downloads,
                "probe_cache_reused": stats.probe_cache_reused,
            },
        )
        for path in staged:
            path.unlink(missing_ok=True)
    return 0


def run_create_production_plan(
    settings: AppSettings,
    *,
    category: str | None = None,
    stage: str | None = None,
    name: str | None = None,
    limit: int | None = None,
) -> int:
    """``--create-production-plan``: draft production plan from real gaps."""

    from core.plan_service import PlanService

    production = _production_service(settings)
    draft = production.build_plan(
        category=category, stage=stage, name=name, limit=limit
    )
    if not draft.items:
        print("没有可用的生产缺口：所有目标工序都已达标。")
        return 0
    service = PlanService(production.library, settings)
    plan, action = service.save_draft(draft, category=category or draft.library_category)
    print(action.message)
    if plan is None:
        return 1
    print()
    print("\n".join(service.report_lines(plan)))
    print()
    print("计划目标与查询词:")
    for item in plan.sorted_items():
        print(
            f"  {item.process_stage}: 当前 {item.current_count}/{item.target_count}"
            f"（缺口 {item.gap}）需要命中 {item.requested_clips}"
            f" | 预算 候选<={item.max_candidates} 下载<={item.max_downloads} tokens<={item.max_tokens}"
        )
        for query in item.queries:
            print(
                f"      - [{query.role:<7}] {query.query} "
                f"(family={query.family or '-'}, raw={query.raw_score:.2f}, "
                f"score={query.score:.2f}, sat={query.query_saturation or 'unknown'}, "
                f"effective={query.effective_production_priority:.2f})"
            )
    print()
    print(f"下一步: --approve-collection-plan {plan.id}，然后 --run-collection-plan {plan.id}")
    return 0


def run_list_collection_plans(settings: AppSettings, *, include_archived: bool = False) -> int:
    service = _plan_service(settings)
    rows = service.history_rows(include_archived=include_archived)
    if not rows:
        print("还没有任何采集计划。" if not include_archived else "没有找到采集计划（含归档）。")
        return 0
    print(
        f"{'id':>4}  {'分类':<10} {'状态':<20}{'目标':>5} {'命中':>5} {'片段':>5} "
        f"{'tokens':>9} {'下载':>5} {'创建时间':<20} 标记 名称"
    )
    for row in rows:
        marks = []
        if row.get("archived"):
            marks.append("归档")
        if row.get("test_plan"):
            marks.append("测试")
        print(
            f"{row['plan_id']:>4}  {row['category'][:10]:<10} {row['status'][:20]:<20}"
            f"{row['target']:>5} {row['qualifying_clips']:>5} {row['saved_clips']:>5} "
            f"{row['ai_tokens']:>9} {row['downloads']:>5} {row['created_at'][:19]:<20} "
            f"{(','.join(marks) or '-'):<6} {row['name']}"
        )
    return 0


def run_show_collection_plan(
    settings: AppSettings, plan_id: int, *, args: argparse.Namespace | None = None
) -> int:
    service = _plan_service(settings)
    plan = service.get_plan(plan_id)
    if plan is None:
        print(f"计划 #{plan_id} 不存在")
        return 1
    print("\n".join(service.report_lines(plan)))
    problems = service.validate(plan)
    if problems:
        print()
        print("校验问题: " + "；".join(problems))
    print()
    print("\n".join(service.status_lines(plan)))
    print()
    print("查询词（按优先级）:")
    for item in plan.sorted_items():
        print(f"  {item.process_stage} [{item.priority}]")
        for query in item.queries:
            print(
                f"    - [{query.role:<7}] {query.query} ({query.origin}) "
                f"family={query.family or '-'} raw={query.raw_score:.2f} "
                f"score={query.score:.2f} sat={query.query_saturation or 'unknown'} "
                f"known={query.known_source_rate if query.known_source_rate is not None else '-'} "
                f"novelty={query.library_novelty_rate if query.library_novelty_rate is not None else '-'} "
                f"effective={query.effective_production_priority:.2f} "
                f"{('[' + query.rank_version + ']') if query.rank_version else ''} "
                + ("；".join(query.evidence) if query.evidence else "")
            )
    timeline = service.timeline_lines(plan)
    if timeline:
        print()
        print("执行时间线:")
        for line in timeline:
            print(f"  {line}")
    if args is not None and args.dry_run:
        print()
        return run_plan_dry_run(settings, plan_id)
    return 0


def run_plan_dry_run(settings: AppSettings, plan_id: int) -> int:
    """Print exactly what the plan would do - zero external calls (section 43)."""

    service = _plan_service(settings)
    plan = service.get_plan(plan_id)
    if plan is None:
        print(f"计划 #{plan_id} 不存在")
        return 1
    estimate = service.estimate(plan)
    print(f"# dry-run 计划 #{plan_id} {plan.name} [{plan.status}]")
    print(
        f"素材分类: {plan.library_category} | 目标片段: {plan.target_final_clips} | "
        f"统计口径: {plan.count_mode}"
    )
    for item in plan.sorted_items():
        print(
            f"\n目标 {item.process_stage}（{item.priority}）"
            f" 需要 {item.remaining_clips} 个片段，缺口 {item.gap}"
        )
        print(
            f"  预算: 唯一候选 <= {item.max_candidates} | 下载 <= {item.max_downloads} | "
            f"tokens <= {item.max_tokens}"
        )
        for query in item.queries:
            print(f"  - 将搜索: {query.query} ({query.origin})")
    print()
    print(
        f"计划预算: 预览 <= {plan.max_preview_candidates} | 下载 <= {plan.max_downloads} | "
        f"tokens <= {plan.max_ai_tokens} | 运行时间 <= {plan.max_runtime_minutes} 分钟"
    )
    print(
        f"预计消耗: 预览 <= {estimate.previews} | 下载 <= {estimate.downloads} | "
        f"tokens ≈ {estimate.ai_tokens} (confidence={estimate.confidence})"
    )
    for line in estimate.basis:
        print(f"  估算依据: {line}")
    print()
    print("dry-run 完成：没有调用 Douyin、没有调用 Qwen、没有下载任何文件。")
    return 0


def run_approve_collection_plan(
    settings: AppSettings, plan_id: int, *, note: str = ""
) -> int:
    service = _plan_service(settings)
    action = service.approve(plan_id, note=note)
    print(action.message)
    return 0 if action.ok else 1


async def _interactive_gate_then_run(
    runner: Any,
    backend: Any,
    service: Any,
    plan_id: int,
    *,
    query: str,
) -> Any:
    """Run the interactive verification gate and the plan in ONE event loop.

    Milestone 8.3: Playwright objects are bound to the loop that created them,
    so the verified session must not be handed to a second ``asyncio.run``.
    Returns either a ``PipelineRunResult`` or an ``int`` exit code.
    """

    check = await backend.ensure_interactive_session(query, limit=5, on_message=print)
    print("\n".join(check.summary_lines()))
    if not check.usable:
        service.repo.log_event(
            plan_id,
            "human_verification_failed",
            details={"query": query, "status": check.status, "detail": check.detail[:200]},
        )
        print("[warn] 抖音会话仍不可用：计划保持暂停/未执行状态，不会继续。")
        return 1
    service.repo.log_event(
        plan_id,
        "human_verification_completed",
        details={
            "query": query,
            "videos": check.video_count,
            "rounds": check.rounds,
            "mode": backend.describe_mode(),
        },
    )
    refreshed = service.get_plan(plan_id)
    if (
        refreshed is not None
        and refreshed.status is PlanStatus.PAUSED
        and refreshed.pause_reason == PauseReason.HUMAN_VERIFICATION_REQUIRED.value
    ):
        print(service.resume(plan_id).message)
        service.repo.log_event(plan_id, "started", details={"interactive": True})
    return await runner.run()


def run_collection_plan(
    settings: AppSettings,
    plan_id: int,
    *,
    dry_run: bool = False,
    interactive: bool = False,
    verify_query: str | None = None,
    cdp_url: str | None = None,
    cleanup_new_clips: bool = False,
    cleanup_new_limit: int = 2,
) -> int:
    """``--run-collection-plan``: execute an approved plan via the existing pipeline.

    With ``--interactive-verification`` (Milestone 8.1 section 5) the Douyin
    session is verified **first, in the same process**, keeping the same browser
    context: the plan then runs on that already-verified session instead of
    restarting the browser.
    """

    if dry_run:
        return run_plan_dry_run(settings, plan_id)
    from core.dependencies import build_library
    from core.plan_runner import PlanRunner

    library = build_library(settings)
    service = _plan_service(settings)
    browser_backend = None

    # Milestone 9.6: resolve/apply the dtk fallback before any plan task, so
    # duration resolution can request a playable media URL in a fresh process.
    selection = asyncio.run(_preflight_douyin(settings))
    if selection.usable and selection.selected is not None:
        print(f"Douyin backend: {selection.selected.base_url} "
              f"({selection.selected.origin})")
    else:
        print(f"[warn] Douyin backend preflight: {selection.reason}")

    # Milestone 9.7: do not begin AI-dependent acquisition when the configured
    # provider is already account-blocked.  Resume re-runs this gate.
    from ai.readiness import check_provider_readiness
    from core.dependencies import build_provider

    provider = build_provider(settings, settings.ai.active_provider)
    readiness = asyncio.run(check_provider_readiness(provider, settings))
    print("\n".join(readiness.summary_lines()))
    try:
        asyncio.run(provider.aclose())
    except Exception:  # pragma: no cover - defensive
        LOGGER.debug("closing readiness provider failed", exc_info=True)
    if not readiness.ready:
        plan = service.get_plan(plan_id)
        if plan is not None:
            service.repo.set_status(
                plan_id,
                PlanStatus.PAUSED,
                pause_reason=PauseReason.PROVIDER_UNAVAILABLE,
            )
            service.repo.set_provider_state(
                plan_id,
                failure_class=readiness.failure_class,
                subtype=readiness.subtype,
                ready=False,
                provider=readiness.provider,
                model=next(iter(readiness.models.values()), ""),
                operation="readiness",
                checked_at=readiness.checked_at,
            )
        print()
        print(
            "计划暂停：AI provider unavailable | "
            f"原因：{readiness.subtype or readiness.failure_class or 'unknown'}"
        )
        print(
            f"provider={readiness.provider} model="
            f"{next(iter(readiness.models.values()), '-')} "
            f"checked_at={readiness.checked_at}"
        )
        print(f"恢复方式：修复 provider 后执行 --run-collection-plan {plan_id}")
        return 1

    # readiness passed: replace the *current* provider state with fresh healthy
    # evidence.  Historical quota events remain in plan events/audit.
    service.repo.set_provider_state(
        plan_id,
        failure_class="",
        subtype="",
        ready=True,
        provider=readiness.provider,
        model=next(iter(readiness.models.values()), ""),
        operation="readiness",
        checked_at=readiness.checked_at,
    )

    if interactive or cdp_url is not None:
        from core.dependencies import build_browser_search

    if interactive:

        plan = service.get_plan(plan_id)
        if plan is None:
            print(f"计划 #{plan_id} 不存在")
            return 1
        if plan.archived:
            print(f"[warn] 计划 #{plan_id} 已归档，不可执行（先取消归档）")
            return 1
        query = (verify_query or "").strip() or "苹果干烘干"
        for item in plan.sorted_items():
            pending = service.current_query(item)
            if pending is not None and not item.satisfied:
                query = pending.query
                break
        print(f"交互式验证：先在同一个浏览器上下文里确认抖音会话（搜索词「{query}」）")
        browser_backend = build_browser_search(
            settings,
            headless=False,
            keep_open_on_challenge=True,
            keep_page_on_challenge=True,
            cdp_url=cdp_url,
        )
        # the verified session and the plan must live in ONE event loop:
        # Playwright objects cannot be reused across asyncio.run() calls
        browser_backend.shared = True
    elif cdp_url is not None:
        # An explicit CLI CDP target must also reach the browser backend used
        # by plan tasks.  Previously it was only consumed by the interactive
        # gate, so ``--run-collection-plan ... --cdp-url ...`` silently
        # launched a separate persistent browser and lost the verified session.
        browser_backend = build_browser_search(settings, cdp_url=cdp_url)
        browser_backend.shared = True

    runner = PlanRunner(
        plan_id,
        library=library,
        settings=settings,
        on_event=lambda event: LOGGER.info("plan | %s | %s", event.stage, event.message),
        browser_backend=browser_backend,
        cleanup_new=cleanup_new_clips,
        cleanup_new_limit=cleanup_new_limit,
    )
    print(f"执行计划 #{plan_id}（Ctrl+C 会取消当前任务并保留已完成片段）")
    try:
        if interactive and browser_backend is not None:
            outcome = asyncio.run(
                _interactive_gate_then_run(
                    runner, browser_backend, service, plan_id, query=query
                )
            )
            if isinstance(outcome, int):
                return outcome
            result = outcome
        else:
            result = asyncio.run(runner.run())
    except KeyboardInterrupt:
        print("\n计划执行被中断（Ctrl+C）：当前任务已取消，已完成片段保留。")
        if browser_backend is not None:
            asyncio.run(browser_backend.close())
            print("浏览器会话已关闭，持久化配置目录保留。")
        return 130
    finally:
        if browser_backend is not None:
            try:
                asyncio.run(browser_backend.close())
            except Exception:  # pragma: no cover - defensive
                LOGGER.debug("closing the interactive browser failed", exc_info=True)
    for message in result.messages:
        print(f"  {message}")
    if result.refused:
        print(f"[warn] {result.refused}")
        return 1
    print(
        f"计划结束状态: {result.status}"
        + (f"（{result.pause_reason}）" if result.pause_reason else "")
        + f" | 查询 {result.queries_run} | 任务 {result.tasks_run} | "
        f"片段 {result.clips_saved}（命中 {result.qualifying_clips}）| tokens {result.ai_tokens}"
    )
    if result.cleanup_attempts or result.cleanup_successes:
        print(
            f"新片段清理路由: 尝试 {result.cleanup_attempts} | "
            f"成功 {result.cleanup_successes}（成功项保持 pending，需人工复核）"
        )
    print()
    service = _plan_service(settings)
    plan = service.get_plan(plan_id)
    if plan is not None:
        print("\n".join(service.status_lines(plan)))
        print()
        print("\n".join(service.report_lines(plan)))
        timeline = service.timeline_lines(plan)
        if timeline:
            print()
            print("执行时间线:")
            for line in timeline:
                print(f"  {line}")
        audit_entries = [
            entry
            for item in plan.items
            for entry in item.progress.query_audit
        ]
        if audit_entries:
            print()
            print("查询执行审计（novelty / reserve）:")
            print(
                f"  {'query':<24}{'family':<16}{'cand':>5}{'unique':>7}"
                f"{'new':>5}{'known':>6}{'processed':>10}{'represent':>10}"
                f"  reserve/skip"
            )
            for entry in audit_entries:
                marker = (
                    "skipped:" + str(entry.get("stop_reason") or "")
                    if entry.get("skipped")
                    else "reserve:" + str(entry.get("reserve_activation_reason") or "-")
                    if entry.get("was_reserve")
                    else "-"
                )
                print(
                    f"  {str(entry.get('query') or '')[:24]:<24}"
                    f"{str(entry.get('query_family') or '')[:16]:<16}"
                    f"{int(entry.get('candidates') or 0):>5}"
                    f"{int(entry.get('unique') or 0):>7}"
                    f"{int(entry.get('new_to_system') or 0):>5}"
                    f"{int(entry.get('known_source') or 0):>6}"
                    f"{int(entry.get('already_processed') or 0):>10}"
                    f"{int(entry.get('already_represented') or 0):>10}"
                    f"  {marker}"
                )
    return 0 if result.status in (PlanStatus.COMPLETED, PlanStatus.PARTIALLY_COMPLETED) else 1


def run_plan_control(
    settings: AppSettings,
    *,
    pause_id: int | None = None,
    resume_id: int | None = None,
    cancel_id: int | None = None,
    archive_id: int | None = None,
    unarchive_id: int | None = None,
    mark_test_id: int | None = None,
    unmark_test_id: int | None = None,
) -> int:
    """Pause / resume / cancel.  A running plan in another process must be
    stopped through the UI or its own runner; these commands update the
    persisted state an operator controls from the CLI."""

    service = _plan_service(settings)
    if pause_id is not None:
        action = service.pause(pause_id)
    elif resume_id is not None:
        action = service.resume(resume_id)
    elif cancel_id is not None:
        action = service.cancel(cancel_id)
    elif archive_id is not None:
        action = service.archive(archive_id, archived=True)
    elif unarchive_id is not None:
        action = service.archive(unarchive_id, archived=False)
    elif mark_test_id is not None:
        action = service.mark_test_plan(mark_test_id, flag=True)
    elif unmark_test_id is not None:
        action = service.mark_test_plan(unmark_test_id, flag=False)
    else:  # pragma: no cover - defensive
        return 1
    print(action.message)
    return 0 if action.ok else 1


def run_acceptance_checks(
    settings: AppSettings,
    *,
    plan_id: int | None = None,
    clip_id: int | None = None,
) -> int:
    """``--plan-linkage`` / ``--validate-clip`` (M8 sections 34/37)."""

    from core.acceptance import plan_linkage, validate_clip
    from core.dependencies import build_library

    library = build_library(settings)
    if clip_id is not None:
        validation = validate_clip(library, settings, clip_id)
        print("\n".join(validation.lines()))
        if not validation.ok:
            print("未通过的检查: " + "；".join(validation.failures))
        return 0 if validation.ok else 1
    assert plan_id is not None
    linkage = plan_linkage(library, plan_id)
    print("\n".join(linkage.lines()))
    clips = linkage.clip_ids
    if not clips:
        print("  （该计划还没有产出片段）")
        return 0
    exit_code = 0
    for existing in clips:
        validation = validate_clip(library, settings, existing)
        print()
        print("\n".join(validation.lines()))
        if not validation.ok:
            exit_code = 1
    return exit_code


def run_query_ranking_report(
    settings: AppSettings, *, version: str | None = None, explain: str | None = None
) -> int:
    """``--query-ranking-report`` / ``--explain-query`` (M8 sections 20/21)."""

    from core.dependencies import build_library
    from core.planner import Planner

    planner = Planner(build_library(settings), settings)
    if explain:
        for line in planner.explain_query(explain, version=version):
            print(line)
        return 0
    rows = planner.ranking_table(limit=20)
    if not rows:
        print("还没有可用的历史查询数据。")
        return 0
    print("query_rank_v1 vs query_rank_v2（真实历史数据，score 越高越优先）")
    print(
        f"{'查询词':<14}{'片段':>5}{'候选':>6}{'唯一率':>8}{'唯一':>6}{'转化':>7}"
        f"{'字幕淘汰':>9}{'tokens':>9}{'v1':>9}{'v2':>9}{'名次v1':>7}{'名次v2':>7}{'Δ':>4}"
    )
    for row in rows:
        subtitle = (
            f"{row['subtitle_rejection_rate']:.0%}"
            if row["subtitle_rejection_rate"] is not None
            else "-"
        )
        print(
            f"{row['query'][:13]:<14}{row['clips']:>5}{row['candidates']:>6}"
            f"{(row['unique_rate'] or 0):>8.0%}{row['unique_candidates']:>6}"
            f"{(row['conversion'] or 0):>7.0%}{subtitle:>9}{row['tokens_total']:>9}"
            f"{row['score_v1']:>9.3f}{row['score_v2']:>9.3f}"
            f"{row['rank_v1'] or 0:>7}{row['rank_v2'] or 0:>7}{row['rank_delta'] or 0:>4}"
        )
    print()
    print("说明: v2 用 log1p(片段) 限制历史体量、用唯一率阻尼重复发现、")
    print("      对高字幕淘汰/高 token/零片段消耗做扣分，并按样本量做置信度缩放。")
    print("      单条查询的分解: python app.py --explain-query \"苹果片烘干\"")
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_console_encoding()
    args = parse_args(argv)
    settings = load_settings(args.config or DEFAULT_CONFIG_PATH)
    configure_logging(args.log_level or settings.app.log_level, settings.paths.log_dir)

    try:
        return _dispatch(args, settings)
    except KeyboardInterrupt:
        # Ctrl+C anywhere: one clean message, no CPython debug dump.  The task
        # runner already cleaned cache/temp files, closed the browser and HTTP
        # clients, and marked the running task as cancelled.
        print()
        print("已中断（Ctrl+C）。临时文件已清理，素材库中的成品片段未受影响。")
        return 130


def _dispatch(args: argparse.Namespace, settings: AppSettings) -> int:
    """Run the requested mode (kept separate so Ctrl+C is handled in one place)."""

    if args.check:
        ok, lines = doctor(settings)
        print("\n".join(lines))
        print()
        print("环境检查:", "通过" if ok else "存在问题")
        return 0 if ok else 1

    if args.check_config:
        from core.diagnostics import describe_effective_config

        print("\n".join(describe_effective_config(settings)))
        print()
        print("有效配置如上（不含任何密钥）。")
        return 0

    if args.list_demo_clips:
        return run_list_demo_clips(settings)

    if args.check_library:
        return run_check_library(settings)

    if args.list_clips:
        return run_list_clips(args, settings)

    if args.export_clips:
        return run_export_clips(args, settings)

    if args.coverage_report is not None:
        return run_coverage_report(settings, args.coverage_report)

    if args.coverage_gaps is not None:
        return run_coverage_gaps(settings, args.coverage_gaps)

    if args.search_yield_report is not None:
        return run_search_yield_report(settings, args.search_yield_report)

    if args.review_export:
        return run_review_export(settings)

    if args.review_import:
        return run_review_import(settings, args.review_import, confirm=args.yes)

    if args.repair_thumbnails:
        return run_repair_thumbnails(settings, confirm=args.yes)

    if args.quarantine_orphans:
        return run_quarantine_orphans(settings, confirm=args.yes)

    if args.maintenance_log:
        return run_maintenance_log(settings)

    if args.preset_list or args.preset_save or args.preset_delete:
        return run_preset_command(args, settings)

    if args.subtitle_report is not None:
        return run_subtitle_report(settings, args.subtitle_report, args)

    if args.analyze_subtitles is not None:
        return run_analyze_subtitles(
            settings, args.analyze_subtitles, apply=args.yes, args=args
        )

    if args.analyze_subtitles_all:
        return run_analyze_subtitles_all(settings, args)

    if args.subtitle_cleanup_report:
        return run_subtitle_cleanup_report(settings)

    if args.subtitle_cleanup is not None:
        return run_subtitle_cleanup(
            settings,
            args.subtitle_cleanup,
            force=args.force,
            engine=args.local_cleanup_engine,
            version=args.cleanup_version,
        )

    if args.subtitle_cleanup_batch:
        if not args.limit:
            print("[warn] --subtitle-cleanup-batch 必须显式提供 --limit N；不会默认处理整个素材库。")
            return 2
        return run_subtitle_cleanup_batch(
            settings, limit=int(args.limit), force=args.force
        )

    if args.subtitle_cleanup_candidates:
        return run_subtitle_cleanup_candidates(
            settings,
            category=args.category,
            limit=int(args.limit) if args.limit else None,
        )

    if args.cleanup_approve is not None:
        return run_subtitle_cleanup_review(
            settings,
            clip_id=args.cleanup_approve,
            status="approved",
            note=args.review_note,
            failure_class="",
            version=args.cleanup_version,
        )

    if args.cleanup_reject is not None:
        return run_subtitle_cleanup_review(
            settings,
            clip_id=args.cleanup_reject,
            status="rejected",
            note=args.review_note,
            failure_class=args.review_failure_class,
            version=args.cleanup_version,
        )

    if args.cleanup_reset_review is not None:
        return run_subtitle_cleanup_review(
            settings,
            clip_id=args.cleanup_reset_review,
            status="pending",
            note=args.review_note,
            failure_class="",
            version=args.cleanup_version,
        )

    if args.cleanup_verify is not None:
        return run_cleanup_verify(settings, args.cleanup_verify)

    if args.cleanup_delete_derivative is not None:
        return run_cleanup_delete_derivative(
            settings,
            args.cleanup_delete_derivative,
            confirm=args.yes,
            note=args.review_note,
        )

    if args.subtitle_cleanup_review_pack is not None:
        return run_subtitle_cleanup_review_pack(
            settings,
            args.subtitle_cleanup_review_pack,
            version=args.cleanup_version,
        )

    if args.check_volcengine_cleanup:
        return run_check_volcengine_cleanup(settings)

    if args.subtitle_cleanup_cloud_preflight is not None:
        return run_cloud_cleanup_preflight(
            settings, args.subtitle_cleanup_cloud_preflight
        )

    if args.subtitle_cleanup_cloud is not None:
        return run_cloud_cleanup(
            settings,
            args.subtitle_cleanup_cloud,
            engine=args.engine or "volcengine",
            force=args.force,
        )

    if args.subtitle_cleanup_cloud_batch:
        if not args.limit:
            print("[warn] --subtitle-cleanup-cloud-batch 必须显式提供 --limit N。")
            return 2
        return run_cloud_cleanup_batch(
            settings,
            limit=int(args.limit),
            engine=args.engine or "volcengine",
            force=args.force,
        )

    if args.create_collection_plan:
        return run_create_collection_plan(args, settings)

    if args.production_gaps:
        return run_production_gaps(
            settings,
            category=args.category,
            stage=args.stage,
            include_covered=args.include_covered,
        )

    if args.production_ready_report:
        return run_production_ready_report(settings)

    if args.duration_recheck:
        return run_duration_recheck(settings, limit=int(args.limit or 5))

    if args.create_production_plan:
        return run_create_production_plan(
            settings,
            category=args.category,
            stage=args.stage,
            name=args.plan_name,
        )

    if args.list_collection_plans:
        return run_list_collection_plans(settings, include_archived=args.include_archived)

    if args.query_ranking_report or args.explain_query:
        return run_query_ranking_report(
            settings, version=args.ranking_version, explain=args.explain_query
        )

    if args.plan_linkage is not None or args.validate_clip is not None:
        return run_acceptance_checks(
            settings, plan_id=args.plan_linkage, clip_id=args.validate_clip
        )

    if args.show_collection_plan is not None:
        return run_show_collection_plan(settings, args.show_collection_plan, args=args)

    if args.approve_collection_plan is not None:
        return run_approve_collection_plan(
            settings, args.approve_collection_plan, note=args.plan_note or ""
        )

    if args.run_collection_plan is not None:
        return run_collection_plan(
            settings,
            args.run_collection_plan,
            dry_run=args.dry_run,
            interactive=args.interactive_verification,
            verify_query=args.verify_query,
            cdp_url=args.cdp_url,
            cleanup_new_clips=args.cleanup_new_clips,
            cleanup_new_limit=args.cleanup_new_limit,
        )

    if (
        args.pause_collection_plan is not None
        or args.resume_collection_plan is not None
        or args.cancel_collection_plan is not None
        or args.archive_collection_plan is not None
        or args.unarchive_collection_plan is not None
        or args.mark_test_plan is not None
        or args.unmark_test_plan is not None
    ):
        return run_plan_control(
            settings,
            pause_id=args.pause_collection_plan,
            resume_id=args.resume_collection_plan,
            cancel_id=args.cancel_collection_plan,
            archive_id=args.archive_collection_plan,
            unarchive_id=args.unarchive_collection_plan,
            mark_test_id=args.mark_test_plan,
            unmark_test_id=args.unmark_test_plan,
        )

    if args.remove_demo_clips:
        return run_remove_demo_clips(
            settings, confirm=args.yes, include_local=args.include_local_tests
        )

    if args.tag_report:
        return run_tag_report(settings)

    if args.backfill_clip_metadata:
        return run_backfill_clip_metadata(settings, confirm=args.yes)

    if args.retag_clip is not None:
        return run_retag_clip(
            settings,
            clip_id=args.retag_clip,
            provider=args.provider,
            version=args.prompt_version,
            confirm=args.yes,
        )

    if args.ab_tagging:
        clip_ids = [
            int(token)
            for token in str(args.clips or "").replace(" ", "").split(",")
            if token.strip().isdigit()
        ]
        return run_ab_tagging(
            settings,
            clip_ids=clip_ids,
            provider=args.provider,
            confirm=args.yes,
        )

    if args.check_douyin:
        ok, lines = asyncio.run(_douyin_doctor(settings))
        print("\n".join(lines))
        print()
        print("抖音后端检查:", "通过" if ok else "不可用（应用其余功能不受影响）")
        return 0 if ok else 1

    if args.check_ai_provider:
        return run_check_ai_provider(settings)

    if args.check_douyin_browser:
        ok, lines = asyncio.run(_douyin_browser_doctor(settings))
        print("\n".join(lines))
        print()
        print("抖音浏览器搜索检查:", "通过" if ok else "需要人工处理（见上方提示）")
        return 0 if ok else 1

    if args.verify_douyin_browser:
        query = (args.verify_query or "").strip() or "苹果干烘干"
        usable, lines = asyncio.run(
            _verify_douyin_browser(settings, query=query, cdp_url=args.cdp_url)
        )
        print()
        print("\n".join(lines))
        print()
        if usable:
            print("抖音浏览器交互式验证: 会话可用")
        elif any("DOM 暂未识别" in line for line in lines):
            print("抖音浏览器交互式验证: 验证已通过，但搜索结果 DOM 暂未识别")
        elif any("仍处于验证/登录页" in line for line in lines):
            print("抖音浏览器交互式验证: 仍被验证拦截")
        else:
            # any other state (browser_unavailable, gateway 5xx, ...) is not a CAPTCHA
            state = next(
                (line.split("session state:")[-1].strip() for line in lines if "session state:" in line),
                "unknown",
            )
            print(f"抖音浏览器交互式验证: 会话不可用（{state}）")
        return 0 if usable else 1

    if args.open_douyin_browser:
        return run_open_douyin_browser(settings)

    if args.init_douyin_browser:
        return asyncio.run(_init_douyin_browser(settings))

    if args.demo:
        demo_root = Path(args.output_dir).expanduser() if args.output_dir else None
        return run_demo(
            settings,
            material=args.material or "苹果干",
            target=args.target or 5,
            library_root=demo_root,
        )

    if args.local_video or args.douyin_search or args.douyin_url:
        return run_task(args, settings)

    from ui.gradio_app import GRADIO_AVAILABLE, launch

    if not GRADIO_AVAILABLE:
        print(
            "gradio 未安装，无法启动 Web UI。\n"
            "  pip install -r requirements.txt\n"
            "或运行真实本地视频流程:  python app.py --local-video <path> --material 苹果干\n"
            "或运行抖音搜索流程:      python app.py --douyin-search 苹果干 --target 2\n"
            "或运行离线 mock 流程:     python app.py --demo"
        )
        return 1

    print(
        f"启动 {settings.app.name} -> "
        f"http://{args.host or settings.ui.host}:{args.port or settings.ui.port}"
    )
    print(f"素材库输出目录: {settings.paths.library_root}")
    launch(settings, host=args.host, port=args.port, share=args.share)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
