"""Prompt tuning diagnostics (Milestone 3.7, sections 25/26/27).

Compares structured tagging output - from the database, or from an A/B run
against an existing clip - and reports the patterns that made Milestone 3.6
suspicious:

* identical descriptions across *different* clips
* identical complete score vectors
* every clip landing on the same ``process_stage``

These are **signals for human review**, never an automatic verdict: two
genuinely different clips may legitimately share a stage.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

SCORE_FIELDS = (
    "material_score",
    "visual_quality_score",
    "subtitle_cleanliness_score",
    "stability_score",
    "composition_score",
    "overall_score",
)


@dataclass
class TagRow:
    """One clip's tagging facts, extracted from the DB or an A/B call."""

    clip_id: int | None
    source_video_id: int | None = None
    description: str = ""
    material: str = ""
    material_form: str = ""
    material_state: str = ""
    process_stage: str = ""
    shot_type: str = ""
    subtitle_type: str = ""
    overall: float = 0.0
    prompt_version: str = ""
    total_tokens: int | None = None
    latency_ms: int | None = None
    scores: tuple[float, ...] = ()

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "TagRow":
        scores = tuple(float(row.get(name) or 0.0) for name in SCORE_FIELDS)
        return cls(
            clip_id=row.get("clip_id") if row.get("clip_id") is not None else row.get("id"),
            source_video_id=row.get("source_video_id"),
            description=str(row.get("description") or ""),
            material=str(row.get("material") or ""),
            material_form=str(row.get("material_form") or ""),
            material_state=str(row.get("material_state") or ""),
            process_stage=str(row.get("process_stage") or ""),
            shot_type=str(row.get("shot_type") or ""),
            subtitle_type=str(row.get("subtitle_type") or ""),
            overall=float(row.get("overall") if row.get("overall") is not None else scores[-1]),
            prompt_version=str(row.get("prompt_version") or row.get("tag_prompt_version") or ""),
            total_tokens=row.get("total_tokens"),
            latency_ms=row.get("latency_ms"),
            scores=scores,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "source_video_id": self.source_video_id,
            "description": self.description,
            "material": self.material,
            "material_form": self.material_form,
            "material_state": self.material_state,
            "process_stage": self.process_stage,
            "shot_type": self.shot_type,
            "subtitle_type": self.subtitle_type,
            "overall": self.overall,
            "prompt_version": self.prompt_version,
        }


@dataclass
class TagPatternReport:
    """Aggregated diversity + suspicious-pattern signals for one run/version."""

    version: str = ""
    clips: int = 0
    distinct_descriptions: int = 0
    distinct_score_vectors: int = 0
    duplicate_descriptions: list[tuple[str, list[int]]] = field(default_factory=list)
    duplicate_scores: list[tuple[tuple[float, ...], list[int]]] = field(default_factory=list)
    stage_distribution: dict[str, int] = field(default_factory=dict)
    single_stage: bool = False
    total_tokens: int = 0
    avg_latency_ms: float = 0.0

    @property
    def description_diversity(self) -> float:
        return round(self.distinct_descriptions / self.clips, 3) if self.clips else 0.0

    @property
    def score_diversity(self) -> float:
        return round(self.distinct_score_vectors / self.clips, 3) if self.clips else 0.0

    def flag_lines(self) -> list[str]:
        lines: list[str] = []
        for description, ids in self.duplicate_descriptions:
            lines.append(
                f"⚠ 描述逐字相同（clip {ids}）: {description[:40]}"
            )
        for scores, ids in self.duplicate_scores:
            lines.append(
                f"⚠ 分数向量完全相同（clip {ids}）: {list(scores)}"
            )
        if self.single_stage and self.clips > 1:
            only = next(iter(self.stage_distribution), "")
            lines.append(f"⚠ 所有片段都是同一个工序: {only}")
        return lines


def analyse_tags(rows: Sequence[TagRow], *, version: str = "") -> TagPatternReport:
    """Aggregate one tagging output set."""

    report = TagPatternReport(version=version, clips=len(rows))
    if not rows:
        return report
    descriptions: dict[str, list[int]] = {}
    score_vectors: dict[tuple[float, ...], list[int]] = {}
    stages: Counter[str] = Counter()
    latencies: list[int] = []
    for row in rows:
        key = row.clip_id if row.clip_id is not None else -1
        if row.description:
            descriptions.setdefault(row.description, []).append(int(key))
        if row.scores:
            score_vectors.setdefault(row.scores, []).append(int(key))
        stages[row.process_stage or "?"] += 1
        if row.total_tokens:
            report.total_tokens += int(row.total_tokens)
        if row.latency_ms:
            latencies.append(int(row.latency_ms))
    report.distinct_descriptions = len(descriptions)
    report.distinct_score_vectors = len(score_vectors)
    report.duplicate_descriptions = [
        (description, ids) for description, ids in descriptions.items() if len(ids) > 1
    ]
    report.duplicate_scores = [
        (scores, ids) for scores, ids in score_vectors.items() if len(ids) > 1
    ]
    report.stage_distribution = dict(stages)
    report.single_stage = len(stages) == 1
    report.avg_latency_ms = (
        round(sum(latencies) / len(latencies), 1) if latencies else 0.0
    )
    return report


def report_lines(
    report: TagPatternReport,
    rows: Sequence[TagRow],
    *,
    title: str = "",
) -> list[str]:
    """Compact prompt-tuning table (section 25)."""

    lines: list[str] = []
    if title:
        lines.append(title)
    lines.append(
        f"  version={report.version or 'n/a'} clips={report.clips} "
        f"description_diversity={report.description_diversity} "
        f"score_diversity={report.score_diversity} "
        f"tokens={report.total_tokens} avg_latency_ms={report.avg_latency_ms}"
    )
    lines.append(f"  工序分布: {report.stage_distribution}")
    lines.extend(f"  {line}" for line in report.flag_lines())
    header = (
        f"  {'clip':>5} {'src':>5} {'mat':<6} {'form':<8} {'state':<10} "
        f"{'stage':<16} {'shot':<9} {'sub':<14} {'ovr':>5} {'prompt':<16} description"
    )
    lines.append(header)
    for row in rows:
        lines.append(
            f"  {str(row.clip_id):>5} {str(row.source_video_id or '-'):>5} "
            f"{row.material[:6]:<6} {row.material_form[:8]:<8} {row.material_state[:10]:<10} "
            f"{row.process_stage[:16]:<16} {row.shot_type[:9]:<9} {row.subtitle_type[:14]:<14} "
            f"{row.overall:>5.2f} {row.prompt_version[:16]:<16} {row.description[:44]}"
        )
    return lines


def build_tag_rows(records: Iterable[Any]) -> list[TagRow]:
    """Adapt ``ClipRecord`` objects (from the library) to :class:`TagRow`."""

    rows: list[TagRow] = []
    for clip in records:
        rows.append(
            TagRow(
                clip_id=clip.id,
                source_video_id=clip.source_video_id,
                description=clip.description,
                material=clip.material,
                material_form=str(clip.material_form),
                material_state=str(clip.material_state),
                process_stage=str(clip.process_stage),
                shot_type=str(clip.shot_type),
                subtitle_type=str(clip.subtitle_type),
                overall=clip.overall_score,
                prompt_version=clip.tag_prompt_version,
                scores=(
                    clip.material_score,
                    clip.visual_quality_score,
                    clip.subtitle_cleanliness_score,
                    clip.stability_score,
                    clip.composition_score,
                    clip.overall_score,
                ),
            )
        )
    return rows
