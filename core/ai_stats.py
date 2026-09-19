"""Per-operation AI cost accounting (Milestone 3.7, sections 9/14/15).

Pure helpers over ``ai_runs`` rows so the CLI, the UI and the tests all report
the same numbers: where the tokens actually go, and what one *saved clip*
costs.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

OPERATIONS = ("preview_filter", "segment_detection", "clip_tagging")


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def operation_stats(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """``{operation: {calls, prompt_tokens, ...}}`` for every AI operation."""

    stats: dict[str, dict[str, Any]] = {
        name: {
            "calls": 0,
            "failures": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "latency_ms_total": 0,
            "latency_samples": 0,
            "frames_total": 0,
        }
        for name in OPERATIONS
    }
    for row in rows:
        operation = str(row.get("operation") or "unknown")
        bucket = stats.setdefault(
            operation,
            {
                "calls": 0,
                "failures": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "latency_ms_total": 0,
                "latency_samples": 0,
                "frames_total": 0,
            },
        )
        bucket["calls"] += 1
        if str(row.get("status") or "ok") != "ok":
            bucket["failures"] += 1
        bucket["prompt_tokens"] += _int(row.get("prompt_tokens"))
        bucket["completion_tokens"] += _int(row.get("completion_tokens"))
        bucket["total_tokens"] += _int(row.get("total_tokens"))
        bucket["frames_total"] += _int(row.get("input_frame_count"))
        latency = row.get("latency_ms")
        if latency is not None:
            bucket["latency_ms_total"] += _int(latency)
            bucket["latency_samples"] += 1
    for bucket in stats.values():
        calls = bucket["calls"] or 0
        bucket["avg_tokens_per_call"] = round(bucket["total_tokens"] / calls, 1) if calls else 0.0
        samples = bucket["latency_samples"] or 0
        bucket["avg_latency_ms"] = (
            round(bucket["latency_ms_total"] / samples, 1) if samples else 0.0
        )
        bucket["avg_frames_per_call"] = (
            round(bucket["frames_total"] / calls, 2) if calls else 0.0
        )
    return stats


def summarize_ai_usage(
    rows: Iterable[Mapping[str, Any]],
    *,
    saved_clips: int = 0,
    pipeline_only: bool = True,
) -> dict[str, Any]:
    """Task level summary used by the CLI report (section 14)."""

    rows = list(rows)
    if pipeline_only:
        # retag / A-B evaluation calls are audited but are not production spend
        rows = [row for row in rows if str(row.get("origin") or "pipeline") == "pipeline"]
    stats = operation_stats(rows)
    total_calls = sum(bucket["calls"] for bucket in stats.values())
    total_tokens = sum(bucket["total_tokens"] for bucket in stats.values())
    failures = sum(bucket["failures"] for bucket in stats.values())
    return {
        "operations": stats,
        "total_calls": total_calls,
        "total_tokens": total_tokens,
        "failures": failures,
        "saved_clips": max(0, int(saved_clips)),
        "tokens_per_saved_clip": (
            round(total_tokens / saved_clips, 1) if saved_clips > 0 else None
        ),
        "calls_per_saved_clip": (
            round(total_calls / saved_clips, 2) if saved_clips > 0 else None
        ),
    }


def operation_report_lines(summary: Mapping[str, Any]) -> list[str]:
    """Human readable per-operation table for the CLI."""

    lines = [
        "AI 调用分项:",
        f"  {'operation':<18}{'calls':>6}{'frames':>8}{'in_tok':>9}{'out_tok':>8}"
        f"{'avg_tok':>9}{'avg_ms':>8}{'fail':>6}",
    ]
    for name, bucket in (summary.get("operations") or {}).items():
        lines.append(
            f"  {name:<18}{bucket['calls']:>6}{bucket['frames_total']:>8}"
            f"{bucket['prompt_tokens']:>9}{bucket['completion_tokens']:>8}"
            f"{bucket['avg_tokens_per_call']:>9}{bucket['avg_latency_ms']:>8}"
            f"{bucket['failures']:>6}"
        )
    per_clip = summary.get("tokens_per_saved_clip")
    lines.append(
        f"  合计: calls={summary.get('total_calls', 0)} "
        f"tokens={summary.get('total_tokens', 0)} "
        f"saved_clips={summary.get('saved_clips', 0)} "
        f"tokens/clip={per_clip if per_clip is not None else 'n/a'}"
    )
    return lines
