"""Migrate *provably* mis-recorded provider failures (Milestone 9.1, issue C).

Background: during the M8.3.1 quota outage, "the vision provider returned no
verdict" was written as ``rejected_preview / other``.  That is a content
rejection in the current schema, so those rows sit in the 30-day retry window
even though the content was never judged.

M9 already fixed the *future* semantics (``failed_ai`` + hours retry).  This
script migrates only rows where the provider failure is **provable**:

* no preview verdict was ever stored (all three preview scores are NULL), and
* an ``ai_runs`` row for the same source video failed with a provider error
  (quota / 403 / auth / rate limit / timeout)

Everything else - including an ambiguous ``other`` rejection - is skipped.

Safety: dry-run by default, ``--apply`` to write.  Every modification is
audited in ``maintenance_log`` (original status/reason, timestamp, version,
evidence).  Idempotent: migrated rows no longer match the evidence query.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import load_settings  # noqa: E402
from core.models import SourceVideoStatus  # noqa: E402
from storage.library import MaterialLibrary  # noqa: E402

MIGRATION_VERSION = "m9_1_provider_failure_v1"

#: the only statuses this migration may rewrite
LEGACY_STATUS = str(SourceVideoStatus.REJECTED_PREVIEW)
#: provider-side failure markers inside ``ai_runs``
PROVIDER_ERROR_MARKERS = ("quota", "403", "auth", "rate limit", "429", "unauthorized")


@dataclass
class MigrationRow:
    source_id: int
    platform_video_id: str
    old_status: str
    old_reason: str
    evidence: str
    proposed_status: str
    proposed_retry: str
    migrate: bool


def _evidence_rows(library: MaterialLibrary) -> list[MigrationRow]:
    rows = library.database.query(
        """
        SELECT sv.id, sv.platform_video_id, sv.status, sv.reject_reason,
               sv.preview_material_score, sv.preview_subtitle_score,
               sv.preview_quality_score
        FROM source_videos sv
        WHERE sv.status = ?
          AND sv.preview_material_score IS NULL
          AND sv.preview_subtitle_score IS NULL
          AND sv.preview_quality_score IS NULL
        ORDER BY sv.id
        """,
        (LEGACY_STATUS,),
    )
    out: list[MigrationRow] = []
    for row in rows:
        runs = library.database.query(
            "SELECT id, operation, status, error_type, error_message FROM ai_runs "
            "WHERE source_video_id = ? AND status <> 'ok' ORDER BY id",
            (int(row["id"]),),
        )
        evidence = ""
        for run in runs:
            text = f"{run['error_type'] or ''} {run['error_message'] or ''}".lower()
            marker = next((m for m in PROVIDER_ERROR_MARKERS if m in text), "")
            if marker:
                evidence = (
                    f"ai_runs#{run['id']} {run['operation']} status={run['status']} "
                    f"provider_error={marker}"
                )
                break
        out.append(
            MigrationRow(
                source_id=int(row["id"]),
                platform_video_id=str(row["platform_video_id"]),
                old_status=str(row["status"]),
                old_reason=str(row["reject_reason"] or ""),
                evidence=evidence,
                proposed_status=str(SourceVideoStatus.FAILED_AI),
                proposed_retry="retry_failed_after_hours (no 30-day content window)",
                migrate=bool(evidence),
            )
        )
    return out


def run_migration(library: MaterialLibrary, *, apply: bool = False) -> dict[str, object]:
    """Dry-run (default) or apply the audited migration."""

    rows = _evidence_rows(library)
    migrated: list[int] = []
    for row in rows:
        if not row.migrate:
            continue
        if apply:
            library.database.execute(
                "UPDATE source_videos SET status = ?, reject_reason = NULL, updated_at = ? "
                "WHERE id = ? AND status = ?",
                (
                    str(SourceVideoStatus.FAILED_AI),
                    _iso_now(),
                    row.source_id,
                    LEGACY_STATUS,
                ),
            )
            library.log_maintenance(
                "migrate_legacy_provider_failure",
                target_type="source_video",
                target_id=str(row.source_id),
                details={
                    "migration_version": MIGRATION_VERSION,
                    "platform_video_id": row.platform_video_id,
                    "old_status": row.old_status,
                    "old_reason": row.old_reason,
                    "new_status": row.proposed_status,
                    "retry_semantics": row.proposed_retry,
                    "evidence": row.evidence,
                    "applied_at": _iso_now(),
                },
            )
            migrated.append(row.source_id)
    if apply and migrated:
        library.log_maintenance(
            "migrate_legacy_provider_failure_summary",
            target_type="library",
            target_id=None,
            details={
                "migration_version": MIGRATION_VERSION,
                "scanned": len(rows),
                "migrated": len(migrated),
                "source_ids": migrated,
                "applied_at": _iso_now(),
            },
        )
    return {
        "scanned": len(rows),
        "definite": sum(1 for row in rows if row.migrate),
        "skipped_ambiguous": sum(1 for row in rows if not row.migrate),
        "migrated": migrated,
        "applied": apply,
        "rows": rows,
    }


def _iso_now() -> str:
    from core.models import utc_now

    return utc_now().isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the migration (default: dry run)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args()

    settings = load_settings()
    from core.dependencies import build_library

    library = build_library(settings)
    report = run_migration(library, apply=args.apply)

    if args.json:
        payload = {
            "applied": report["applied"],
            "scanned": report["scanned"],
            "definite": report["definite"],
            "skipped_ambiguous": report["skipped_ambiguous"],
            "migrated": report["migrated"],
            "rows": [row.__dict__ for row in report["rows"]],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"迁移模式: {mode}（版本 {MIGRATION_VERSION}）")
    print(f"扫描 rejected_preview 且无预筛结论的 source: {report['scanned']}")
    for row in report["rows"]:
        flag = "MIGRATE" if row.migrate else "SKIP"
        print(
            f"  [{flag}] source #{row.source_id} {row.platform_video_id} "
            f"{row.old_status}/{row.old_reason or '-'}"
        )
        print(f"         → {row.proposed_status}（{row.proposed_retry}）")
        print(f"         证据: {row.evidence or '无明确 provider 失败证据（保持不变）'}")
    print()
    print(
        f"可迁移 {report['definite']} 行 | 跳过（证据不足）{report['skipped_ambiguous']} 行"
    )
    if not args.apply:
        print("这是 dry run：数据库未修改。加 --apply 才会写入（并记录审计）。")
    else:
        print(f"已迁移 {len(report['migrated'])} 行，审计写入 maintenance_log。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
