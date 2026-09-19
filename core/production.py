"""Coverage-driven production planning (Milestone 9).

This module turns *real* library state into an ordered list of acquisition
objectives (one per material + process stage) and into a draft collection plan
the operator can approve.  It is deliberately read-only and deterministic:

* targets come from ``production_coverage`` configuration, never from SQL/UI
* the library is authoritative for what is already covered
* every priority component is exposed (``priority_components``) so the operator
  can see *why* a gap is ranked where it is
* query generation is objective-specific (material + stage) and reuses
  ``query_rank_v2`` history, with explicit duplicate/rediscovery penalties
* nothing here starts acquisition: a plan is a draft until a human approves it
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from core.config import AppSettings
from core.coverage import CoverageAnalyzer, StageCoverage
from core.keyword_expander import KeywordExpander
from core.novelty import SATURATION_UNKNOWN, NoveltyAnalyzer
from core.planner import RANK_VERSION_V2, Planner, QueryStatistics
from core.plans import (
    CollectionPlan,
    CollectionPlanItem,
    PlanQuery,
    QueryOrigin,
)
from storage.library import MaterialLibrary

LOGGER = logging.getLogger(__name__)


def _family_templates(material: str) -> dict[str, tuple[str, ...]]:
    """Small, explicit query-family templates (Milestone 9.5, section 6)."""

    m = material or ""
    return {
        "material_process": (f"{m}烘干过程", f"{m}烘干", f"{m}干燥过程"),
        "heat_pump": (f"{m}热泵烘干", f"{m}空气能烘干"),
        "dryer_equipment": (f"{m}烘干设备", f"{m}烘干机", f"{m}烘干设备内部"),
        "drying_room": (f"{m}烘干房", f"{m}烘房"),
        "inside_dryer": (f"{m}烘干房内部", f"{m}烘干机内部"),
        "factory_line": (f"{m}烘干车间", f"{m}烘干生产线", f"{m}烘干流水线"),
        "finished_product": (f"{m}烘干成品", f"{m}成品", f"{m}包装"),
    }

#: process stages that matter most for industrial drying footage (section 1)
PRIMARY_STAGES: tuple[str, ...] = (
    "drying",
    "inside_dryer",
    "before_drying",
    "finished_product",
    "equipment",
    "factory",
)

#: tokens that make a *historical* query relevant to one objective.  A query
#: that never mentions the objective (e.g. the bare material name ``苹果干``)
#: must not become the first query of a ``drying`` objective just because its
#: raw historical score is high (M8 revealed exactly this divergence).
STAGE_MATCH_TOKENS: dict[str, tuple[str, ...]] = {
    "drying": ("烘干", "热泵", "干燥", "烘干房", "烘干机"),
    "inside_dryer": ("内部", "里面", "烘干房内", "设备内", "箱内"),
    "before_drying": ("进烘房", "入炉", "上架前", "烘房前", "装车"),
    "finished_product": ("成品", "干货", "出货", "成品展示"),
    "equipment": ("烘干机", "烘干设备", "干燥机", "热泵", "设备"),
    "factory": ("厂", "车间", "生产线", "加工厂"),
    "loading": ("装料", "上料", "装盘", "入料"),
    "tray_arrangement": ("摆盘", "铺盘", "上架", "托盘"),
    "unloading": ("出料", "出炉", "卸料", "下料"),
    "packaging": ("包装", "装袋", "封口"),
    "preparation": ("挑选", "清洗", "备料", "切片", "切条"),
}


def stage_tokens(stage: str) -> tuple[str, ...]:
    """Match tokens for one objective (config data, never hardcoded SQL)."""

    return STAGE_MATCH_TOKENS.get(stage, ())


def _matches_stage(query: str, tokens: Sequence[str]) -> bool:
    """Whether a historical query actually mentions this objective."""

    if not tokens:
        return True
    text = query or ""
    return any(token in text for token in tokens)


@dataclass
class ProductionGap:
    """One material + process-stage objective with its evidence."""

    category: str
    stage: str
    current: int
    target: int
    gap: int
    stage_rank: int
    priority: float
    priority_label: str
    edit_role_gap: list[str] = field(default_factory=list)
    best_query: str = ""
    best_query_score: float = 0.0
    best_query_origin: str = ""
    duplicate_risk: float | None = None
    rediscovery_rate: float | None = None
    subtitle_rejection_rate: float | None = None
    tokens_per_clip: float | None = None
    estimated_tokens: int = 0
    last_attempted: str = ""
    status: str = "gap"
    queries: list[str] = field(default_factory=list)
    priority_components: dict[str, float] = field(default_factory=dict)
    # -- Milestone 9.5 novelty/actionability --------------------------------
    coverage_priority: float = 0.0
    query_actionability: float = 0.0
    effective_priority: float = 0.0
    best_query_family: str = ""
    query_saturation: str = ""
    saturation_reason: str = ""
    library_novelty_rate: float | None = None
    known_source_rate: float | None = None
    historical_candidates: int = 0
    historical_new_to_system: int = 0
    primary_families: list[str] = field(default_factory=list)
    saturated_primary_count: int = 0
    primary_count: int = 0
    saturated_primary_fraction: float = 0.0
    historical_known_source_rate: float | None = None
    historical_library_novelty_rate: float | None = None
    saturated_families: list[str] = field(default_factory=list)

    @property
    def covered(self) -> bool:
        return self.gap <= 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "process_stage": self.stage,
            "current": self.current,
            "target": self.target,
            "gap": self.gap,
            "stage_rank": self.stage_rank,
            "priority": round(self.priority, 3),
            "priority_label": self.priority_label,
            "edit_role_gap": list(self.edit_role_gap),
            "best_query": self.best_query,
            "best_query_score": round(self.best_query_score, 3),
            "best_query_origin": self.best_query_origin,
            "duplicate_risk": self.duplicate_risk,
            "rediscovery_rate": self.rediscovery_rate,
            "subtitle_rejection_rate": self.subtitle_rejection_rate,
            "tokens_per_clip": self.tokens_per_clip,
            "estimated_tokens": self.estimated_tokens,
            "last_attempted": self.last_attempted,
            "status": self.status,
            "queries": list(self.queries),
            "priority_components": {
                key: round(value, 3) for key, value in self.priority_components.items()
            },
            "coverage_priority": round(self.coverage_priority, 3),
            "query_actionability": round(self.query_actionability, 3),
            "effective_priority": round(self.effective_priority, 3),
            "best_query_family": self.best_query_family,
            "query_saturation": self.query_saturation,
            "saturation_reason": self.saturation_reason,
            "library_novelty_rate": self.library_novelty_rate,
            "known_source_rate": self.known_source_rate,
            "historical_candidates": self.historical_candidates,
            "historical_new_to_library": self.historical_new_to_system,
            "primary_families": list(self.primary_families),
            "saturated_primary_count": self.saturated_primary_count,
            "primary_count": self.primary_count,
            "saturated_primary_fraction": round(self.saturated_primary_fraction, 3),
            "historical_known_source_rate": self.historical_known_source_rate,
            "historical_library_novelty_rate": self.historical_library_novelty_rate,
            "saturated_families": list(self.saturated_families),
        }


class ProductionCoverageService:
    """Coverage gaps -> ranked production objectives -> draft plans."""

    def __init__(
        self,
        library: MaterialLibrary,
        settings: AppSettings,
        *,
        planner: Planner | None = None,
        analyzer: CoverageAnalyzer | None = None,
    ) -> None:
        self.library = library
        self.settings = settings
        self.coverage = analyzer or CoverageAnalyzer(library, settings.coverage)
        self.planner = planner or Planner(library, settings)
        self.novelty = NoveltyAnalyzer(library, settings)
        self.production = settings.production_coverage
        self._stats: dict[str, QueryStatistics] | None = None

    # -- materials ---------------------------------------------------------
    def materials(self) -> list[str]:
        """Configured production materials plus categories already in the library."""

        ordered: list[str] = []
        for name in self.production.materials:
            if name and name not in ordered:
                ordered.append(name)
        for name, _count in self.coverage.categories():
            if name and name not in ordered:
                ordered.append(name)
        return ordered

    def targets_for(self, category: str) -> dict[str, int]:
        """Desired minimum per stage for one material (config driven)."""

        return dict(self.production.process_stage_targets)

    # -- history -----------------------------------------------------------
    def query_statistics(self) -> dict[str, QueryStatistics]:
        if self._stats is None:
            self._stats = self.planner.query_statistics()
        return self._stats

    def rediscovery_rates(self) -> dict[str, float]:
        """Share of a query's discovered videos that are already processed."""

        rows = self.library.database.query(
            "SELECT matched_queries AS queries, status FROM source_videos "
            "WHERE matched_queries IS NOT NULL AND matched_queries <> ''"
        )
        totals: dict[str, int] = {}
        processed: dict[str, int] = {}
        for row in rows:
            try:
                queries = json.loads(row["queries"]) or []
            except Exception:  # pragma: no cover - defensive
                queries = []
            if not isinstance(queries, list):
                continue
            known = str(row["status"]) in ("processed", "analyzed", "no_usable_segment")
            for query in {str(item) for item in queries if item}:
                totals[query] = totals.get(query, 0) + 1
                if known:
                    processed[query] = processed.get(query, 0) + 1
        return {
            query: processed.get(query, 0) / total
            for query, total in totals.items()
            if total
        }

    def last_attempts(self) -> dict[str, str]:
        """Most recent ``search_yields`` timestamp per query."""

        rows = self.library.database.query(
            "SELECT query, MAX(created_at) AS last FROM search_yields GROUP BY query"
        )
        return {str(row["query"]): str(row["last"] or "") for row in rows}

    # -- objectives --------------------------------------------------------
    def queries_for(
        self,
        category: str,
        stage: str,
        *,
        limit: int | None = None,
        material: str | None = None,
    ) -> list[PlanQuery]:
        """Ranked, objective-specific query list for one objective."""

        limit = int(limit or self.production.queries_per_item)
        base = material or category
        # objective templates use the *material* form (苹果/辣椒) while the
        # category keeps its product form (苹果干/辣椒干): searching "辣椒干烘干"
        # is narrower than "辣椒烘干过程", and the M9 examples ask for the
        # material form first
        template_material = KeywordExpander.split_material(base)[0] or base
        templates = self.coverage.recommended_queries(
            category or None,
            stage,
            observed_material=template_material,
            limit=limit + 2,
        )
        chosen: list[PlanQuery] = []
        seen: set[str] = set()
        chosen_families: set[str] = set()
        base_name = KeywordExpander.split_material(base)[0] if base else ""
        tokens = stage_tokens(stage)
        rediscovery = self.rediscovery_rates()
        historical: list[PlanQuery] = []
        for entry in self.query_statistics().values():
            if entry.clips <= 0:
                continue
            if base_name and base_name not in entry.query:
                continue
            scored = self.planner.score(entry, version=RANK_VERSION_V2)
            historical.append(
                PlanQuery(
                    query=entry.query,
                    origin=QueryOrigin.HISTORICAL,
                    score=scored.score,
                    raw_score=scored.score,
                    rank_version=scored.version,
                    components=scored.components,
                    clips=entry.clips,
                    candidates=entry.candidates,
                    unique_candidates=entry.unique_candidates,
                    duplicate_rate=entry.duplicate_rate if entry.candidates else None,
                    subtitle_rejection_rate=entry.subtitle_rejection_rate,
                    tokens_per_clip=entry.tokens_per_clip,
                    tokens_total=entry.tokens_total,
                    approved_clips=entry.approved,
                    evidence=list(scored.evidence),
                )
            )
        for entry in historical:
            rate = rediscovery.get(entry.query)
            entry.score = self._apply_rediscovery_penalty(entry, rate)
        # objective relevance first: only stage-relevant history may lead an
        # objective; a bare material query is kept as a last resort
        relevant = [entry for entry in historical if _matches_stage(entry.query, tokens)]
        unrelated = [entry for entry in historical if entry not in relevant]
        relevant.sort(key=lambda item: (-item.score, item.query))
        unrelated.sort(key=lambda item: (-item.score, item.query))
        for entry in relevant:
            if len(chosen) >= max(1, limit - 2):
                break
            if entry.query in seen:
                continue
            family = self.novelty.query_family(entry.query)
            if family in chosen_families:
                continue
            seen.add(entry.query)
            entry.family = family
            chosen_families.add(family)
            chosen.append(entry)
        family_templates = _family_templates(template_material)
        for family in self.novelty.query_families_for_stage(stage):
            if len(chosen) >= limit:
                break
            if family in chosen_families:
                continue
            for template in family_templates.get(family, ()):
                query = template.format(material=template_material)
                if query in seen:
                    continue
                seen.add(query)
                chosen_families.add(family)
                chosen.append(
                    PlanQuery(
                        query=query,
                        origin=QueryOrigin.GENERATED_TEMPLATE,
                        rank_version=RANK_VERSION_V2,
                        family=family,
                        evidence=[f"目标工序查询族 {family}（未验证）"],
                    )
                )
                break
        for entry in unrelated:
            if len(chosen) >= limit:
                break
            if entry.query in seen:
                continue
            seen.add(entry.query)
            chosen.append(entry)
        for index, query in enumerate(chosen):
            self._annotate_novelty(query, role="primary", order=index + 1)
        return chosen

    def reserve_queries_for(
        self,
        category: str,
        stage: str,
        *,
        existing: Sequence[PlanQuery] = (),
        limit: int | None = None,
    ) -> list[PlanQuery]:
        """Family-diverse reserve queries not already present in ``existing``."""

        limit = int(limit or self.settings.novelty.reserve_queries_per_item)
        base = category
        material = KeywordExpander.split_material(base)[0] or base
        seen = {query.query for query in existing}
        templates = _family_templates(material)
        chosen: list[PlanQuery] = []
        for family in self.novelty.query_families_for_stage(stage):
            for template in templates.get(family, ()):
                text = template.format(material=material)
                if text in seen:
                    continue
                seen.add(text)
                query = PlanQuery(
                    query=text,
                    origin=QueryOrigin.GENERATED_TEMPLATE,
                    rank_version=RANK_VERSION_V2,
                    family=family,
                    evidence=[f"预留查询族 {family}（primary 饱和时激活）"],
                )
                self._annotate_novelty(
                    query, role="reserve", order=len(existing) + len(chosen) + 1
                )
                chosen.append(query)
                if len(chosen) >= max(0, limit):
                    return chosen
        return chosen

    def all_queries_for(
        self,
        category: str,
        stage: str,
        *,
        primary_limit: int | None = None,
        reserve_limit: int | None = None,
    ) -> list[PlanQuery]:
        primary = self.queries_for(category, stage, limit=primary_limit)
        reserve = self.reserve_queries_for(
            category, stage, existing=primary, limit=reserve_limit
        )
        return [*primary, *reserve]

    def _annotate_novelty(self, query: PlanQuery, *, role: str, order: int) -> None:
        novelty = self.novelty.stats(query.query)
        query.role = role
        query.planned_order = int(order)
        query.family = query.family or novelty.family
        query.query_saturation = novelty.saturation
        query.saturation_reason = novelty.saturation_reason
        query.library_novelty_rate = novelty.library_novelty_rate
        query.known_source_rate = novelty.known_source_rate
        query.recent_new_source_count = novelty.recent_new_source_count
        base = float(query.raw_score or 0.0)
        if base <= 0:
            base = float(self.settings.novelty.unverified_query_prior)
        query.effective_production_priority = self.novelty.effective_query_priority(
            base, novelty
        )

    def _apply_rediscovery_penalty(self, query: PlanQuery, rate: float | None) -> float:
        """Down-rank a query whose candidates are mostly already processed."""

        if rate is None:
            return query.score
        penalty = float(self.production.recently_processed_weight) * rate
        query.components = {
            **query.components,
            "rediscovery_penalty": -round(penalty, 3),
        }
        if rate:
            query.evidence = [
                *query.evidence,
                f"重复发现率 {rate:.0%}（该查询已发现的视频中已处理的比例）",
            ]
        return query.score - penalty

    # -- gaps --------------------------------------------------------------
    def gaps(
        self,
        *,
        category: str | None = None,
        stage: str | None = None,
        include_covered: bool = False,
        limit: int | None = None,
    ) -> list[ProductionGap]:
        """Ranked production gaps from the real library state."""

        categories = [category] if category else self.materials()
        stages = [stage] if stage else []
        gaps: list[ProductionGap] = []
        rediscovery = self.rediscovery_rates()
        attempts = self.last_attempts()
        for name in categories:
            rows = {row.stage: row for row in self.coverage.process_stage_coverage(name)}
            wanted = stages or self._targeted_stages(rows)
            for stage_name in wanted:
                row = rows.get(stage_name)
                current = int(row.total) if row else 0
                target = int(
                    self.production.process_stage_targets.get(
                        stage_name, int(self.settings.coverage.target_for(stage_name))
                    )
                )
                gap = max(0, target - current)
                queries = self.queries_for(name, stage_name)
                best = (
                    max(
                        queries,
                        key=lambda item: (
                            item.effective_production_priority,
                            item.score,
                            -item.planned_order,
                        ),
                    )
                    if queries
                    else None
                )
                rate = rediscovery.get(best.query) if best else None
                stats = self.query_statistics().get(best.query) if best else None
                novelty = self.novelty.stats(best.query) if best else None
                primary_queries = [query for query in queries if query.role == "primary"]
                primary_stats = [
                    self.novelty.stats(query.query) for query in primary_queries
                ]
                saturated_count = sum(
                    1
                    for entry in primary_stats
                    if entry.saturation == "saturated"
                )
                saturated_fraction = (
                    saturated_count / len(primary_stats) if primary_stats else 0.0
                )
                base_actionability = (
                    self.novelty.actionability(novelty) if novelty else 0.0
                )
                actionability = base_actionability * (
                    1.0
                    - float(self.settings.novelty.saturated_primary_penalty)
                    * saturated_fraction
                )
                role_gap = self._edit_role_gap(name, stage_name)
                components, priority = self._priority(
                    stage_name, gap, role_gap, best, rate, stats
                )
                effective = round(priority * actionability, 3)
                gaps.append(
                    ProductionGap(
                        category=name,
                        stage=stage_name,
                        current=current,
                        target=target,
                        gap=gap,
                        stage_rank=int(
                            self.production.stage_priorities.get(
                                stage_name, self.production.stage_priority_default
                            )
                        ),
                        priority=priority,
                        coverage_priority=priority,
                        query_actionability=round(actionability, 3),
                        effective_priority=effective,
                        priority_label=self._priority_label(gap, priority),
                        edit_role_gap=role_gap,
                        best_query=best.query if best else "",
                        best_query_score=best.score if best else 0.0,
                        best_query_origin=str(best.origin) if best else "",
                        duplicate_risk=(
                            round(1.0 - (best.unique_candidates / best.candidates), 3)
                            if best and best.candidates
                            else None
                        ),
                        rediscovery_rate=round(rate, 3) if rate is not None else None,
                        subtitle_rejection_rate=(
                            round(stats.subtitle_rejection_rate, 3)
                            if stats and stats.subtitle_rejection_rate is not None
                            else None
                        ),
                        tokens_per_clip=stats.tokens_per_clip if stats else None,
                        estimated_tokens=(
                            int(best.tokens_per_clip)
                            if best and best.tokens_per_clip
                            else int(
                                self.settings.collection_planning.estimated_tokens_per_analysis
                            )
                        ),
                        last_attempted=attempts.get(best.query, "") if best else "",
                        status="gap" if gap > 0 else "covered",
                        queries=[query.query for query in queries],
                        priority_components=components,
                        best_query_family=novelty.family if novelty else "",
                        query_saturation=novelty.saturation if novelty else "",
                        saturation_reason=novelty.saturation_reason if novelty else "",
                        library_novelty_rate=(
                            novelty.library_novelty_rate if novelty else None
                        ),
                        known_source_rate=novelty.known_source_rate if novelty else None,
                        historical_candidates=(
                            novelty.historical_candidates if novelty else 0
                        ),
                        historical_new_to_system=(
                            novelty.historical_new_to_system if novelty else 0
                        ),
                        primary_families=[
                            query.family for query in queries if query.role == "primary"
                        ],
                        saturated_primary_count=saturated_count,
                        primary_count=len(primary_stats),
                        saturated_primary_fraction=round(saturated_fraction, 3),
                        historical_known_source_rate=(
                            round(
                                min(
                                    1.0,
                                    sum(
                                        entry.known_source_count
                                        for entry in primary_stats
                                    )
                                    / max(
                                        1,
                                        sum(
                                            entry.historical_candidates
                                            for entry in primary_stats
                                        ),
                                    ),
                                ),
                                4,
                            )
                            if sum(
                                entry.historical_candidates for entry in primary_stats
                            )
                            else None
                        ),
                        historical_library_novelty_rate=(
                            round(
                                min(
                                    1.0,
                                    sum(
                                        entry.historical_new_to_system
                                        for entry in primary_stats
                                    )
                                    / max(
                                        1,
                                        sum(
                                            entry.historical_candidates
                                            for entry in primary_stats
                                        ),
                                    ),
                                ),
                                4,
                            )
                            if sum(
                                entry.historical_candidates for entry in primary_stats
                            )
                            else None
                        ),
                        saturated_families=[
                            query.family
                            for query, entry in zip(primary_queries, primary_stats)
                            if entry.saturation == "saturated"
                        ],
                    )
                )
        if not include_covered:
            gaps = [entry for entry in gaps if entry.gap > 0]
        gaps.sort(
            key=lambda entry: (
                -entry.effective_priority,
                -entry.priority,
                entry.stage_rank,
                entry.category,
                entry.stage,
            )
        )
        return gaps[: int(limit)] if limit else gaps

    def _targeted_stages(self, rows: dict[str, StageCoverage]) -> list[str]:
        """The configured production objectives (never the whole enum).

        Only stages with an explicit target in ``production_coverage`` become
        objectives: a value with no configured target is *not* a production
        objective, it is just something the library happens to contain.
        ``rows`` is accepted for signature symmetry with the coverage report.
        """

        del rows  # the library state decides *how much* is missing, not *what* to target
        return list(self.production.process_stage_targets)

    def _edit_role_gap(self, category: str, stage: str) -> list[str]:
        """Which useful edit roles are still missing for this objective."""

        row = self.library.database.query_one(
            "SELECT edit_roles FROM clips WHERE library_category = ? AND process_stage = ? LIMIT 1",
            (category, stage),
        )
        present: set[str] = set()
        if row and row["edit_roles"]:
            try:
                present = {str(item) for item in (json.loads(row["edit_roles"]) or [])}
            except Exception:  # pragma: no cover - defensive
                present = set()
        wanted = ("process", "equipment", "detail", "result")
        return [role for role in wanted if role not in present]

    def _priority(
        self,
        stage: str,
        gap: int,
        role_gap: Sequence[str],
        best: PlanQuery | None,
        rediscovery: float | None,
        stats: QueryStatistics | None,
    ) -> tuple[dict[str, float], float]:
        """Deterministic, inspectable acquisition priority (section 2)."""

        p = self.production
        rank = int(p.stage_priorities.get(stage, p.stage_priority_default))
        components = {
            "stage": round(float(p.weight_stage) * (1.0 / max(1, rank)), 3),
            "gap": round(float(p.weight_gap) * math.log1p(max(0, gap)), 3),
            "edit_role": round(float(p.weight_edit_role) * (len(role_gap) / 4.0), 3),
            "query_yield": round(
                # scaled so a strong historical query (score ~1-2) is
                # comparable to the duplicate/rediscovery penalties below
                float(p.weight_query_yield) * (max(0.0, best.score) if best else 0.0) / 2.0,
                3,
            ),
            "duplicate_penalty": round(
                -float(p.penalty_duplicate) * ((best.duplicate_rate or 0.0) if best else 0.0),
                3,
            ),
            "subtitle_penalty": round(
                -float(p.penalty_subtitle)
                * ((stats.subtitle_rejection_rate or 0.0) if stats else 0.0),
                3,
            ),
            "token_penalty": round(
                -float(p.penalty_tokens)
                * (
                    min(1.0, (stats.tokens_per_clip or 0.0) / max(1, p.token_reference))
                    if stats
                    else 0.0
                ),
                3,
            ),
            "rediscovery_penalty": round(
                -float(p.recently_processed_weight) * (rediscovery or 0.0), 3
            ),
        }
        return components, round(sum(components.values()), 3)

    @staticmethod
    def _priority_label(gap: int, priority: float) -> str:
        if gap <= 0:
            return "已覆盖"
        if priority >= 3.0:
            return "严重缺口"
        if priority >= 1.5:
            return "高优先级"
        if priority >= 0.5:
            return "中等"
        return "低优先级"

    # -- plan suggestion ---------------------------------------------------
    def build_plan(
        self,
        *,
        category: str | None = None,
        stage: str | None = None,
        limit: int | None = None,
        name: str | None = None,
        include_covered: bool = False,
    ) -> CollectionPlan:
        """Draft production plan (never persisted here, never auto-approved)."""

        p = self.production
        gaps = self.gaps(
            category=category,
            stage=stage,
            include_covered=include_covered,
            limit=int(limit or p.max_items_per_plan),
        )
        items = [
            CollectionPlanItem(
                process_stage=gap.stage,
                current_count=gap.current,
                target_count=gap.target,
                gap=gap.gap,
                requested_clips=max(1, int(p.default_item_target_clips)),
                priority=self._plan_priority(gap),
                queries=self.all_queries_for(
                    gap.category,
                    gap.stage,
                    primary_limit=self.settings.novelty.primary_queries_per_item,
                    reserve_limit=self.settings.novelty.reserve_queries_per_item,
                ),
                max_candidates=max(1, int(p.default_item_candidates)),
                max_downloads=max(1, int(p.default_item_downloads)),
                max_tokens=max(1000, int(p.default_item_tokens)),
            )
            for gap in gaps
        ]
        label = category or "生产覆盖"
        plan = CollectionPlan(
            name=name or f"{label}生产补采",
            library_category=category or "",
            count_mode=self.settings.coverage.count_mode,
            created_from="production_coverage",
            items=items,
            coverage_before=self.planner.coverage_snapshot(category or ""),
        )
        return self.planner.apply_budget_defaults(plan)

    @staticmethod
    def _plan_priority(gap: ProductionGap) -> str:
        if gap.stage_rank <= 2:
            return "critical"
        if gap.stage_rank <= 6:
            return "high"
        return "medium"

    # -- metrics (section 13) ---------------------------------------------
    def metrics(self, *, category: str | None = None) -> dict[str, Any]:
        """Discovery / preview / download / clips / cost, kept separate."""

        rows = self.coverage.search_yield_report(limit=1000)
        candidates = sum(int(row["candidates"]) for row in rows)
        unique = sum(int(row["unique_candidates"]) for row in rows)
        zero_yield = [
            str(row["query"])
            for row in rows
            if int(row["candidates"]) > 0 and int(row["clips"]) == 0
        ]
        ai = self.library.database.query_one(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens, "
            "SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok, "
            "SUM(CASE WHEN status <> 'ok' THEN 1 ELSE 0 END) AS failed FROM ai_runs"
        )
        clips = self.library.database.query_one(
            "SELECT COUNT(*) AS clips, "
            "SUM(CASE WHEN review_status = 'approved' THEN 1 ELSE 0 END) AS approved FROM clips"
        )
        downloads = self.library.database.query_one(
            "SELECT SUM(CASE WHEN status IN ('processed','analyzed') THEN 1 ELSE 0 END) AS done, "
            "SUM(CASE WHEN status = 'failed_download' THEN 1 ELSE 0 END) AS failed "
            "FROM source_videos"
        )
        # Milestone 9.1 wording: "previously processed" (what dedup/retry policy
        # suppresses) is *not* the same as "already represented in the library"
        # (a source that really produced clips).
        suppressed = self.library.database.query_one(
            "SELECT COUNT(*) AS n FROM source_videos "
            "WHERE status IN ('processed','analyzed','no_usable_segment')"
        )
        represented = self.library.database.query_one(
            "SELECT COUNT(DISTINCT source_video_id) AS n FROM clips "
            "WHERE source_video_id IS NOT NULL"
        )
        return {
            "discovery": {
                "queries": len(rows),
                "candidates": candidates,
                "unique_candidates": unique,
                "duplicate_rate": (
                    round(1.0 - unique / candidates, 3) if candidates else None
                ),
                "previously_processed": int(suppressed["n"] or 0),
                "dedup_suppressed": int(suppressed["n"] or 0),
                "already_represented_in_library": int(represented["n"] or 0),
            },
            "preview": {
                "provider_calls": int(ai["calls"] or 0),
                "provider_ok": int(ai["ok"] or 0),
                "provider_errors": int(ai["failed"] or 0),
            },
            "download": {
                "downloaded": int(downloads["done"] or 0),
                "failed": int(downloads["failed"] or 0),
            },
            "clips": {
                "total": int(clips["clips"] or 0),
                "approved": int(clips["approved"] or 0),
            },
            "cost": {
                "ai_calls": int(ai["calls"] or 0),
                "tokens": int(ai["tokens"] or 0),
            },
            "zero_yield_queries": sorted(zero_yield)[:20],
        }

    # -- reporting ---------------------------------------------------------
    def gap_rows(
        self,
        *,
        category: str | None = None,
        stage: str | None = None,
        include_covered: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return [
            gap.as_dict()
            for gap in self.gaps(
                category=category,
                stage=stage,
                include_covered=include_covered,
                limit=limit,
            )
        ]

    def report_lines(
        self,
        *,
        category: str | None = None,
        stage: str | None = None,
        include_covered: bool = False,
        limit: int | None = None,
    ) -> list[str]:
        gaps = self.gaps(
            category=category,
            stage=stage,
            include_covered=include_covered,
            limit=limit,
        )
        metrics = self.metrics(category=category)
        header = (
            f"{'分类':<8}{'工序':<14}{'缺口':>4} "
            f"{'覆盖优先':>8}{'行动力':>7}{'有效优先':>8} "
            f"{'最佳查询':<22}{'族':<15}{'novelty':>8}{'known':>7} "
            f"{'sat-primary':<12}{'最近尝试':<11}"
        )
        lines = ["生产覆盖缺口（按有效生产优先级排序）:", "", header]
        for gap in gaps:
            novelty = (
                f"{gap.historical_library_novelty_rate:.0%}"
                if gap.historical_library_novelty_rate is not None
                else "-"
            )
            known = (
                f"{gap.historical_known_source_rate:.0%}"
                if gap.historical_known_source_rate is not None
                else "-"
            )
            saturation = (
                f"{gap.saturated_primary_count}/{gap.primary_count}"
                if gap.primary_count
                else "-"
            )
            lines.append(
                f"{gap.category:<8}{gap.stage:<14}{gap.gap:>4} "
                f"{gap.coverage_priority:>8.2f}{gap.query_actionability:>7.2f}"
                f"{gap.effective_priority:>8.2f} "
                f"{gap.best_query[:22]:<22}{gap.best_query_family[:15]:<15}"
                f"{novelty:>8}{known:>7} {saturation:<12}"
                f"{(gap.last_attempted or '-')[:10]:<11}"
            )
        if not gaps:
            lines.append("（没有待补的缺口）")
        lines.extend(
            [
                "",
                "历史指标（分开统计，零产出查询不隐藏）:",
                f"  discovery: 查询 {metrics['discovery']['queries']} | "
                f"候选 {metrics['discovery']['candidates']} | "
                f"唯一 {metrics['discovery']['unique_candidates']} | "
                f"重复率 {metrics['discovery']['duplicate_rate']}",
                f"  preview  : 提供商调用 {metrics['preview']['provider_calls']} | "
                f"成功 {metrics['preview']['provider_ok']} | "
                f"提供商错误 {metrics['preview']['provider_errors']}（不算内容淘汰）",
                f"  dedup    : 已处理/被去重抑制 {metrics['discovery']['previously_processed']}"
                f"（previously processed） | 已产出片段的来源 "
                f"{metrics['discovery']['already_represented_in_library']}"
                f"（already represented in library）",
                f"  download : 成功 {metrics['download']['downloaded']} | "
                f"失败 {metrics['download']['failed']}",
                f"  clips    : 片段 {metrics['clips']['total']} | "
                f"已批准 {metrics['clips']['approved']}",
                f"  cost     : AI 调用 {metrics['cost']['ai_calls']} | "
                f"tokens {metrics['cost']['tokens']}",
            ]
        )
        if metrics["zero_yield_queries"]:
            lines.append(
                "  零产出查询（有候选但 0 片段）: "
                + ", ".join(metrics["zero_yield_queries"][:8])
            )
        return lines
