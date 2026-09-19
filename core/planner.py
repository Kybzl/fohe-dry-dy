"""Coverage gaps -> collection plan (Milestone 7, sections 3-10/30-36/47/48).

Deterministic by construction: the planner only reads SQLite history and the
Milestone 5 coverage/gap logic.  It never calls Qwen, never touches Douyin and
never starts acquisition - a plan is a *draft* until an operator approves it.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from core.config import AppSettings
from core.coverage import PRIORITY_HEALTHY, CoverageAnalyzer
from core.keyword_expander import KeywordExpander
from core.plans import (
    CollectionPlan,
    CollectionPlanItem,
    PlanEffectiveness,
    PlanEstimate,
    PlanQuery,
    QueryOrigin,
)
from core.subtitle_ops import SubtitleOps
from storage.library import MaterialLibrary
from storage.plans import PlanRepository

LOGGER = logging.getLogger(__name__)

#: frozen Milestone 7 scoring semantics (kept for comparison/reporting)
RANK_VERSION_V1 = "query_rank_v1"
#: calibrated Milestone 8 scoring semantics
RANK_VERSION_V2 = "query_rank_v2"
RANKING_VERSIONS: tuple[str, ...] = (RANK_VERSION_V1, RANK_VERSION_V2)


@dataclass
class QueryStatistics:
    """Everything the ranking needs about one query's history."""

    query: str
    runs: int = 0
    candidates: int = 0
    unique_candidates: int = 0
    preview_accepted: int = 0
    downloads: int = 0
    clips: int = 0
    approved: int = 0
    subtitle_rejection_rate: float | None = None
    tokens_per_clip: float | None = None
    #: attributable AI tokens (only when a source maps to exactly one query)
    tokens_total: int = 0

    @property
    def unique_rate(self) -> float:
        if self.candidates <= 0:
            return 1.0
        return min(1.0, max(0.0, self.unique_candidates / self.candidates))

    @property
    def duplicate_rate(self) -> float:
        return 1.0 - self.unique_rate

    @property
    def conversion(self) -> float:
        return (self.clips / self.candidates) if self.candidates else 0.0


@dataclass
class QueryScore:
    """One scored query with its transparent score breakdown (M8 sections 19/20)."""

    query: str
    version: str
    score: float
    confidence: float = 1.0
    components: dict[str, float] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)

    def explanation_lines(self) -> list[str]:
        """Operator readable breakdown, e.g. for ``--explain-query``."""

        labels = {
            "yield": "yield component",
            "conversion": "conversion component",
            "useful_yield": "useful yield",
            "unique_damping": "duplicate damping",
            "approval": "approval component",
            "subtitle_penalty": "subtitle penalty",
            "token_penalty": "token penalty",
            "zero_clip_token_penalty": "zero-clip token penalty",
            "zero_yield_penalty": "zero-yield penalty",
            "subtotal": "subtotal",
            "confidence_factor": "confidence adjustment",
            "confidence_samples": "confidence samples",
            "final": "final score",
        }
        lines = [f"{self.query}  [{self.version}]"]
        for key, value in self.components.items():
            label = labels.get(key, key)
            if key in ("unique_damping", "confidence_factor"):
                lines.append(f"  {label:<24} ×{value:.3f}")
            elif key == "confidence_samples":
                lines.append(f"  {label:<24} {value:.0f}")
            else:
                lines.append(f"  {label:<24} {value:+.3f}")
        return lines


class Planner:
    """Turns coverage gaps into an operator-reviewable draft plan."""

    def __init__(
        self,
        library: MaterialLibrary,
        settings: AppSettings,
        *,
        analyzer: CoverageAnalyzer | None = None,
        repository: PlanRepository | None = None,
    ) -> None:
        self.library = library
        self.settings = settings
        self.coverage = analyzer or CoverageAnalyzer(library, settings.coverage)
        self.repo = repository or PlanRepository(library.database)
        self.planning = settings.collection_planning

    # -- history -----------------------------------------------------------
    def query_statistics(self) -> dict[str, QueryStatistics]:
        stats: dict[str, QueryStatistics] = {}
        for row in self.coverage.search_yield_report(limit=1000):
            stats[row["query"]] = QueryStatistics(
                query=row["query"],
                runs=int(row["runs"]),
                candidates=int(row["candidates"]),
                unique_candidates=int(row["unique_candidates"]),
                preview_accepted=int(row["preview_accepted"]),
                downloads=int(row["downloads"]),
                clips=int(row["clips"]),
            )
        for row in self.coverage.query_cost_analysis():
            entry = stats.setdefault(row["query"], QueryStatistics(query=row["query"]))
            entry.tokens_per_clip = row["tokens_per_clip"]
            # only tokens attributable to exactly one query may drive the
            # zero-clip penalty (section 18): shared buckets stay unattributed
            if row.get("attribution") == "source_matched_single_query":
                entry.tokens_total = int(row.get("tokens") or 0)
        subtitles = SubtitleOps(self.library, self.settings).search_yield_subtitle_insight(
            limit=500
        )
        for row in subtitles:
            entry = stats.setdefault(row["query"], QueryStatistics(query=row["query"]))
            entry.subtitle_rejection_rate = row["subtitle_rejection_rate"]
        for query, counts in self.library.query_approved_counts().items():
            entry = stats.setdefault(query, QueryStatistics(query=query))
            entry.approved = int(counts.get("approved", 0))
        return stats

    # -- ranking (sections 6/30/31/32/48) ---------------------------------
    def score_query(self, stats: QueryStatistics) -> tuple[float, list[str]]:
        """``query_rank_v1`` - the Milestone 7 score, kept for comparison.

        ``score = w_clips*clips + w_accepted*accepted + w_conv*conversion
                  + w_approved*approved
                  - w_sub*subtitle_rejection - w_dup*duplicate_rate
                  - w_tok*normalised tokens per clip``

        Duplicate rate is ``1 - unique/candidates``: a query that mostly
        rediscovers known videos is less useful than its raw volume suggests.

        Its semantics are frozen: Milestone 8 adds :meth:`score_query_v2` and
        never rewrites the historical meaning of a stored v1 score (M8 §19).
        """

        scored = self.score_v1(stats)
        return scored.score, scored.evidence

    def score_v1(self, stats: QueryStatistics) -> QueryScore:
        """The frozen Milestone 7 scoring semantics, with components exposed."""

        p = self.planning
        conversion = stats.clips / stats.candidates if stats.candidates else 0.0
        duplicate_rate = stats.duplicate_rate if stats.candidates else 0.0
        token_factor = 0.0
        if stats.tokens_per_clip:
            token_factor = min(1.0, stats.tokens_per_clip / max(1, p.query_token_reference))
        subtitle = stats.subtitle_rejection_rate or 0.0

        clip_component = p.query_weight_clips * stats.clips
        accepted_component = p.query_weight_accepted * stats.preview_accepted
        conversion_component = p.query_weight_conversion * conversion
        approval_component = p.query_weight_approved * stats.approved
        subtitle_penalty = p.query_penalty_subtitle * subtitle
        duplicate_penalty = p.query_penalty_duplicate * duplicate_rate
        token_penalty = p.query_penalty_tokens * token_factor
        score = (
            clip_component
            + accepted_component
            + conversion_component
            + approval_component
            - subtitle_penalty
            - duplicate_penalty
            - token_penalty
        )
        evidence = [
            f"历史片段 {stats.clips}",
            f"通过预筛 {stats.preview_accepted}",
            f"转化率 {conversion:.0%}",
            f"重复率 {duplicate_rate:.0%}",
        ]
        if stats.approved:
            evidence.append(f"已批准 {stats.approved}")
        if stats.subtitle_rejection_rate is not None:
            evidence.append(f"字幕淘汰率 {stats.subtitle_rejection_rate:.0%}")
        if stats.tokens_per_clip:
            evidence.append(f"tokens/片段 {stats.tokens_per_clip:.0f}")
        components = {
            "clips_component": round(clip_component, 3),
            "accepted_component": round(accepted_component, 3),
            "conversion_component": round(conversion_component, 3),
            "approval_component": round(approval_component, 3),
            "subtitle_penalty": round(-subtitle_penalty, 3),
            "duplicate_penalty": round(-duplicate_penalty, 3),
            "token_penalty": round(-token_penalty, 3),
            "final": round(score, 3),
        }
        return QueryScore(
            query=stats.query,
            version=RANK_VERSION_V1,
            score=round(score, 3),
            confidence=1.0,
            components=components,
            evidence=evidence,
        )

    def score_v2(self, stats: QueryStatistics) -> QueryScore:
        """``query_rank_v2`` (M8 §12-§22): bounded volume + quality weighting.

        ``yield      = w_yield * log1p(final_clips)``
        ``conversion = w_conv  * (clips / candidates)``
        ``useful     = yield + conversion``
        ``damped     = useful * unique_rate``        (duplicate damping, §14)
        ``subtotal   = damped + w_approved*log1p(approved)
                        - w_sub * subtitle_rejection_rate
                        - w_tok * min(1, tokens_per_clip / token_reference)
                        - w_zero_clip_tokens * min(1, log1p(tokens) / log1p(token_reference))
                        - w_zero_yield``             (only when clips == 0, §18/§22)
        ``score      = subtotal * confidence``       (confidence §15)
        ``confidence = samples / (samples + k)``, ``samples = unique candidates``

        The confidence factor pulls both directions toward zero: a tiny sample
        (``1 candidate, 1 clip``) can no longer outrank a proven query, and a
        barely-measured query is not punished as hard as a heavily-measured bad
        one.  Raw candidate volume never adds score by itself (§13).
        """

        p = self.planning
        clips = max(0, int(stats.clips))
        candidates = max(0, int(stats.candidates))
        unique = max(0, min(int(stats.unique_candidates), candidates)) if candidates else 0
        unique_rate = stats.unique_rate
        conversion = stats.conversion
        k = max(0.0, float(p.rank2_confidence_k))
        confidence = (unique / (unique + k)) if (unique + k) > 0 else 1.0

        yield_component = float(p.rank2_weight_yield) * math.log1p(clips)
        conversion_component = float(p.rank2_weight_conversion) * conversion
        useful = yield_component + conversion_component
        damping = unique_rate ** max(0.0, float(p.rank2_duplicate_power))
        damped = useful * damping
        approval_component = float(p.rank2_weight_approved) * math.log1p(
            max(0, int(stats.approved))
        )
        subtitle_penalty = float(p.rank2_penalty_subtitle) * (
            stats.subtitle_rejection_rate or 0.0
        )
        token_reference = max(1, int(p.rank2_token_reference))
        token_penalty = 0.0
        if stats.tokens_per_clip:
            token_penalty = float(p.rank2_penalty_tokens) * min(
                1.0, stats.tokens_per_clip / token_reference
            )
        zero_clip_penalty = 0.0
        if clips == 0 and stats.tokens_total > 0:
            zero_clip_penalty = float(p.rank2_penalty_zero_clip_tokens) * min(
                1.0,
                math.log1p(stats.tokens_total) / math.log1p(token_reference),
            )
        zero_yield_penalty = 0.0
        if clips == 0 and candidates >= max(1, int(p.rank2_zero_yield_min_candidates)):
            zero_yield_penalty = float(p.rank2_penalty_zero_yield)

        subtotal = (
            damped
            + approval_component
            - subtitle_penalty
            - token_penalty
            - zero_clip_penalty
            - zero_yield_penalty
        )
        score = subtotal * confidence

        evidence = [
            f"历史片段 {clips}（log 归一化，不随历史次数线性增长）",
            f"转化率 {conversion:.0%}",
            f"唯一候选率 {unique_rate:.0%}（重复率 {stats.duplicate_rate:.0%}）",
        ]
        if stats.approved:
            evidence.append(f"已批准 {stats.approved}（加分项，非必需）")
        if stats.subtitle_rejection_rate is not None:
            evidence.append(f"字幕淘汰率 {stats.subtitle_rejection_rate:.0%}")
        if stats.tokens_per_clip:
            evidence.append(f"tokens/片段 {stats.tokens_per_clip:.0f}")
        if zero_clip_penalty:
            evidence.append(f"零片段但已消耗 tokens {stats.tokens_total}")
        evidence.append(f"样本置信度 ×{confidence:.2f}（唯一候选 {unique}）")

        components = {
            "yield": round(yield_component, 3),
            "conversion": round(conversion_component, 3),
            "useful_yield": round(useful, 3),
            "unique_damping": round(damping, 3),
            "approval": round(approval_component, 3),
            "subtitle_penalty": round(-subtitle_penalty, 3),
            "token_penalty": round(-token_penalty, 3),
            "zero_clip_token_penalty": round(-zero_clip_penalty, 3),
            "zero_yield_penalty": round(-zero_yield_penalty, 3),
            "subtotal": round(subtotal, 3),
            "confidence_factor": round(confidence, 3),
            "confidence_samples": float(unique),
            "final": round(score, 3),
        }
        return QueryScore(
            query=stats.query,
            version=RANK_VERSION_V2,
            score=round(score, 3),
            confidence=round(confidence, 3),
            components=components,
            evidence=evidence,
        )

    def score(self, stats: QueryStatistics, *, version: str | None = None) -> QueryScore:
        """Score one query with the requested ranking version."""

        chosen = (version or self.planning.ranking_version or RANK_VERSION_V2).strip()
        if chosen == RANK_VERSION_V1:
            return self.score_v1(stats)
        return self.score_v2(stats)

    def rank_queries(
        self,
        category: str,
        stage: str,
        *,
        material: str | None = None,
        limit: int = 4,
        statistics: dict[str, QueryStatistics] | None = None,
        version: str | None = None,
    ) -> list[PlanQuery]:
        """Historical queries first (ranked), then deterministic templates.

        New plans use ``query_rank_v2`` unless the configuration (or the caller)
        asks for the frozen v1 semantics.  Only queries with at least one final
        clip are eligible: the plan layer never bets budget on a query that has
        never produced material.
        """

        stats = statistics if statistics is not None else self.query_statistics()
        chosen_version = (version or self.planning.ranking_version or RANK_VERSION_V2).strip()
        # A plan searches *its own* material: a query that produced clips for a
        # different material must not be recommended for this category.
        base_material = (
            KeywordExpander.split_material(category)[0] if category else ""
        ) or category
        ranked: list[PlanQuery] = []
        for entry in stats.values():
            if entry.clips <= 0:
                continue
            if base_material and base_material not in entry.query:
                continue
            scored = self.score(entry, version=chosen_version)
            ranked.append(
                PlanQuery(
                    query=entry.query,
                    origin=QueryOrigin.HISTORICAL,
                    score=scored.score,
                    rank_version=scored.version,
                    components=scored.components,
                    clips=entry.clips,
                    candidates=entry.candidates,
                    unique_candidates=entry.unique_candidates,
                    subtitle_rejection_rate=entry.subtitle_rejection_rate,
                    duplicate_rate=entry.duplicate_rate if entry.candidates else None,
                    tokens_per_clip=entry.tokens_per_clip,
                    tokens_total=entry.tokens_total,
                    approved_clips=entry.approved,
                    evidence=scored.evidence,
                )
            )
        ranked.sort(key=lambda item: (-item.score, item.query))
        chosen = ranked[: max(0, limit - 2)]

        # coverage-specific templates for this gap (section 7)
        templates = self.coverage.recommended_queries(category, stage, limit=4)
        for query in templates:
            if len(chosen) >= limit:
                break
            if any(item.query == query for item in chosen):
                continue
            chosen.append(
                PlanQuery(
                    query=query,
                    origin=QueryOrigin.GENERATED_TEMPLATE,
                    score=0.0,
                    rank_version=chosen_version,
                    evidence=["未验证的模板查询（覆盖缺口生成）"],
                )
            )
        if not chosen:
            chosen = [
                PlanQuery(
                    query=query,
                    origin=QueryOrigin.GENERATED_TEMPLATE,
                    rank_version=chosen_version,
                    evidence=["未验证的模板查询"],
                )
                for query in templates[:limit]
            ]
        return chosen

    # -- ranking reporting (sections 20/21/22) -----------------------------
    def ranking_table(
        self,
        *,
        limit: int = 12,
        queries: Sequence[str] | None = None,
        statistics: dict[str, QueryStatistics] | None = None,
    ) -> list[dict[str, Any]]:
        """Compare ``query_rank_v1`` with ``query_rank_v2`` on real history."""

        stats = statistics if statistics is not None else self.query_statistics()
        # "(多条搜索词共享的来源)" style buckets are bookkeeping, not search terms
        entries = [entry for entry in stats.values() if not entry.query.startswith("(")]
        if queries is not None:
            wanted = [str(query) for query in queries]
            by_query = {entry.query: entry for entry in entries}
            entries = [
                by_query.get(query, QueryStatistics(query=query)) for query in wanted
            ]
        elif limit > 0:
            entries = sorted(
                entries,
                key=lambda entry: (-entry.clips, -entry.candidates, entry.query),
            )[:limit]

        v1 = {entry.query: self.score_v1(entry) for entry in entries}
        v2 = {entry.query: self.score_v2(entry) for entry in entries}
        order_v1 = sorted(v1, key=lambda name: (-v1[name].score, name))
        order_v2 = sorted(v2, key=lambda name: (-v2[name].score, name))
        rank_v1 = {name: index + 1 for index, name in enumerate(order_v1)}
        rank_v2 = {name: index + 1 for index, name in enumerate(order_v2)}

        rows: list[dict[str, Any]] = []
        for entry in entries:
            rows.append(
                {
                    "query": entry.query,
                    "clips": entry.clips,
                    "candidates": entry.candidates,
                    "unique_candidates": entry.unique_candidates,
                    "unique_rate": round(entry.unique_rate, 3),
                    "duplicate_rate": round(entry.duplicate_rate, 3)
                    if entry.candidates
                    else None,
                    "conversion": round(entry.conversion, 3),
                    "subtitle_rejection_rate": entry.subtitle_rejection_rate,
                    "tokens_total": entry.tokens_total,
                    "tokens_per_clip": entry.tokens_per_clip,
                    "approved_clips": entry.approved,
                    "score_v1": v1[entry.query].score,
                    "score_v2": v2[entry.query].score,
                    "rank_v1": rank_v1.get(entry.query),
                    "rank_v2": rank_v2.get(entry.query),
                    "rank_delta": (
                        (rank_v1.get(entry.query) or 0) - (rank_v2.get(entry.query) or 0)
                    ),
                    "confidence": v2[entry.query].confidence,
                    "components_v2": dict(v2[entry.query].components),
                }
            )
        return rows

    def explain_query(
        self,
        query: str,
        *,
        version: str | None = None,
        statistics: dict[str, QueryStatistics] | None = None,
    ) -> list[str]:
        """Component-by-component explanation for one query (section 20)."""

        stats = statistics if statistics is not None else self.query_statistics()
        entry = stats.get(query) or QueryStatistics(query=query)
        scored = self.score(entry, version=version)
        lines = scored.explanation_lines()
        if scored.evidence:
            lines.append("  依据: " + "；".join(scored.evidence))
        return lines

    # -- snapshots ---------------------------------------------------------
    def coverage_snapshot(self, category: str) -> dict[str, Any]:
        """Stored on the plan so we can explain later why it existed (section 33)."""

        stages = self.coverage.process_stage_coverage(category)
        return {
            "library_category": category,
            "count_mode": self.settings.coverage.count_mode,
            "process_stage": {
                stage.stage: {
                    "current": stage.total,
                    "approved": stage.approved,
                    "target": stage.target,
                    "priority": stage.priority,
                }
                for stage in stages
            },
        }

    @staticmethod
    def coverage_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
        """``{stage: delta}`` between two coverage snapshots (section 34)."""

        delta: dict[str, int] = {}
        before_stages = (before or {}).get("process_stage") or {}
        after_stages = (after or {}).get("process_stage") or {}
        if not before_stages or not after_stages:
            # nothing to compare yet (the plan has not finished a run)
            return {}
        for stage in sorted(set(before_stages) | set(after_stages)):
            old = int((before_stages.get(stage) or {}).get("current") or 0)
            new = int((after_stages.get(stage) or {}).get("current") or 0)
            if new != old:
                delta[stage] = new - old
        return delta

    # -- plan generation (sections 3/4/5/8/9/47/48) ------------------------
    def build_plan(
        self,
        category: str,
        *,
        count_mode: str | None = None,
        include_healthy: bool = False,
        name: str | None = None,
        created_from: str = "coverage_gap",
        material: str | None = None,
    ) -> CollectionPlan:
        """Draft plan from the real coverage gaps.  Nothing is persisted here."""

        mode = count_mode or self.settings.coverage.count_mode
        previous_mode = self.settings.coverage.count_mode
        self.settings.coverage.count_mode = mode
        try:
            gaps = list(self.coverage.gap_report(category))
            if include_healthy:
                # the operator explicitly wants objectives for stages that
                # already meet their target (section 47)
                known = {stage.stage for stage in gaps}
                for stage in self.coverage.process_stage_coverage(category):
                    if stage.priority == PRIORITY_HEALTHY and stage.stage not in known:
                        gaps.append(stage)
            snapshot = self.coverage_snapshot(category)
        finally:
            self.settings.coverage.count_mode = previous_mode

        statistics = self.query_statistics()
        p = self.planning
        items: list[CollectionPlanItem] = []
        remaining_target = max(0, int(p.max_plan_target_clips))
        for gap in gaps:
            if len(items) >= max(1, int(p.max_items_per_plan)):
                break
            if gap.priority == PRIORITY_HEALTHY and not include_healthy:
                continue
            desired = gap.missing if gap.missing > 0 else (1 if include_healthy else 0)
            requested = min(
                desired,
                max(1, int(p.max_requested_clips_per_stage)),
                remaining_target or desired,
            )
            if requested <= 0:
                continue
            items.append(
                CollectionPlanItem(
                    process_stage=gap.stage,
                    current_count=gap.total,
                    target_count=gap.target,
                    gap=gap.missing,
                    requested_clips=requested,
                    priority=gap.priority,
                    queries=self.rank_queries(
                        category, gap.stage, material=material, statistics=statistics
                    ),
                    max_candidates=max(1, int(p.default_item_candidates)),
                    max_downloads=max(1, int(p.default_item_downloads)),
                    max_tokens=max(1000, int(p.default_item_tokens)),
                )
            )
            remaining_target = max(0, remaining_target - requested)
            if remaining_target == 0:
                break

        plan = CollectionPlan(
            name=name or f"{category}补采",
            library_category=category,
            count_mode=mode,
            created_from=created_from,
            items=items,
            coverage_before=snapshot,
        )
        return self.apply_budget_defaults(plan)

    def apply_budget_defaults(self, plan: CollectionPlan) -> CollectionPlan:
        """Set item + plan budgets so they always fit the global ceilings.

        The global ceilings win: when the per-item defaults would exceed the
        plan budget, every item is scaled down proportionally (minimum 1) so
        the sum fits exactly (section 9).
        """

        p = self.planning
        self._fit_plan_budget(plan, "max_candidates", int(p.max_plan_previews), minimum=1)
        self._fit_plan_budget(plan, "max_downloads", int(p.max_plan_downloads), minimum=1)
        self._fit_plan_budget(plan, "max_tokens", int(p.max_plan_ai_tokens), minimum=1000)

        plan.target_final_clips = sum(item.requested_clips for item in plan.items)
        plan.max_preview_candidates = min(
            sum(item.max_candidates for item in plan.items) or 0,
            max(1, int(p.max_plan_previews)),
        )
        plan.max_downloads = min(
            sum(item.max_downloads for item in plan.items) or 0,
            max(1, int(p.max_plan_downloads)),
        )
        plan.max_ai_tokens = min(
            sum(item.max_tokens for item in plan.items) or 0,
            max(1000, int(p.max_plan_ai_tokens)),
        )
        plan.max_runtime_minutes = float(p.max_plan_runtime_minutes)
        return plan

    def _fit_plan_budget(
        self, plan: CollectionPlan, attribute: str, limit: int, *, minimum: int = 1
    ) -> None:
        """Cap one per-item budget so the items fit the plan ceiling."""

        if limit <= 0 or not plan.items:
            return
        for item in plan.items:
            setattr(item, attribute, max(minimum, min(int(getattr(item, attribute)), limit)))
        values = [int(getattr(item, attribute)) for item in plan.items]
        total = sum(values)
        if total <= limit:
            return
        scaled = [max(minimum, int(limit * value / total)) for value in values]
        while sum(scaled) > limit:
            index = max(range(len(scaled)), key=lambda position: scaled[position])
            if scaled[index] <= minimum:
                break
            scaled[index] -= 1
        for item, value in zip(plan.items, scaled):
            setattr(item, attribute, value)

    # -- validation (sections 8/9/19) --------------------------------------
    def validate(self, plan: CollectionPlan) -> list[str]:
        """Problems that must be fixed before approval; empty = valid."""

        problems: list[str] = []
        p = self.planning
        if not plan.items:
            problems.append("计划没有任何目标（工序都已达标或缺缺口为空）")
        if plan.target_final_clips <= 0:
            problems.append("目标片段数为 0")
        if plan.target_final_clips > int(p.max_plan_target_clips):
            problems.append(
                f"目标片段 {plan.target_final_clips} 超过上限 {p.max_plan_target_clips}"
            )
        if plan.max_preview_candidates > int(p.max_plan_previews):
            problems.append(
                f"预览预算 {plan.max_preview_candidates} 超过上限 {p.max_plan_previews}"
            )
        if plan.max_downloads > int(p.max_plan_downloads):
            problems.append(
                f"下载预算 {plan.max_downloads} 超过上限 {p.max_plan_downloads}"
            )
        if plan.max_ai_tokens > int(p.max_plan_ai_tokens):
            problems.append(
                f"AI token 预算 {plan.max_ai_tokens} 超过上限 {p.max_plan_ai_tokens}"
            )
        token_sum = sum(item.max_tokens for item in plan.items)
        if token_sum > plan.max_ai_tokens and token_sum > 0:
            problems.append(
                f"各目标 token 预算合计 {token_sum} 超过计划预算 {plan.max_ai_tokens}"
            )
        download_sum = sum(item.max_downloads for item in plan.items)
        if download_sum > plan.max_downloads and download_sum > 0:
            problems.append(
                f"各目标下载预算合计 {download_sum} 超过计划预算 {plan.max_downloads}"
            )
        for item in plan.items:
            if item.requested_clips <= 0:
                problems.append(f"{item.process_stage}: requested_clips 必须 > 0")
            if item.max_tokens <= 0 or item.max_downloads <= 0 or item.max_candidates <= 0:
                problems.append(f"{item.process_stage}: 预算必须为正数")
            if not item.queries:
                problems.append(f"{item.process_stage}: 没有可用查询词")
        return problems

    # -- estimate (sections 10/46) -----------------------------------------
    def estimate(self, plan: CollectionPlan) -> PlanEstimate:
        """Preview/download/token estimate, honest about its confidence."""

        p = self.planning
        history = self.query_statistics()
        analysed = [
            entry
            for entry in history.values()
            if entry.tokens_per_clip is not None
        ]
        productive = [entry for entry in history.values() if entry.clips > 0]
        basis: list[str] = []
        confidence = "low"
        if analysed:
            tokens_per_analysis = int(
                sum(entry.tokens_per_clip or 0 for entry in analysed) / len(analysed)
            )
            basis.append(
                f"tokens/片段 历史均值 {tokens_per_analysis}"
                f"（{len(analysed)} 个可归属查询）"
            )
            confidence = "medium" if len(analysed) >= 3 else "low"
        else:
            tokens_per_analysis = int(p.estimated_tokens_per_analysis)
            basis.append("缺少可归属历史，使用配置估算值")
        if productive:
            downloads_per_clip = max(
                1.0,
                sum(entry.downloads for entry in productive)
                / max(1, sum(entry.clips for entry in productive)),
            )
            basis.append(
                f"历史均值: 每查询 {sum(entry.clips for entry in productive) / len(productive):.2f} "
                f"片段、每片段 {downloads_per_clip:.2f} 次下载"
            )
        else:
            downloads_per_clip = 2.0
            basis.append("没有历史产出，按保守假设估算")

        previews = min(
            int(plan.max_preview_candidates) or int(p.max_plan_previews),
            max(1, int(p.max_plan_previews)),
        )
        downloads = min(
            int(plan.max_downloads)
            or int(math.ceil(plan.target_final_clips * downloads_per_clip)),
            max(1, int(p.max_plan_downloads)),
        )
        tokens = int(
            previews * int(p.estimated_tokens_per_preview)
            + downloads * tokens_per_analysis
        )
        if plan.max_ai_tokens:
            tokens = min(tokens, int(plan.max_ai_tokens))
        return PlanEstimate(
            previews=previews,
            downloads=downloads,
            ai_tokens=tokens,
            runtime_minutes=float(plan.max_runtime_minutes or p.max_plan_runtime_minutes),
            confidence=confidence,
            basis=basis,
        )

    # -- effectiveness (sections 34/35/36) ---------------------------------
    def effectiveness(self, plan: CollectionPlan) -> PlanEffectiveness:
        """Post-run accounting including off-target but valid material."""

        progress = plan.progress
        qualifying = sum(item.progress.qualifying_clips for item in plan.items)
        off_target = max(0, progress.clips_saved - qualifying)
        hit_rate = (
            round(qualifying / progress.clips_saved, 3) if progress.clips_saved else None
        )
        return PlanEffectiveness(
            objective_hit_rate=hit_rate,
            tokens_per_qualifying_clip=(
                round(progress.ai_tokens / qualifying, 1) if qualifying else None
            ),
            downloads_per_qualifying_clip=(
                round(progress.downloads / qualifying, 2) if qualifying else None
            ),
            qualifying_clips=qualifying,
            off_target_clips=off_target,
            stage_breakdown=dict(progress.stage_breakdown),
            coverage_delta=self.coverage_delta(plan.coverage_before, plan.coverage_after),
            before=plan.coverage_before,
            after=plan.coverage_after,
        )
