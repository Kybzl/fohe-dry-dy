"""Material coverage analytics (Milestone 5).

Answers the operator's questions **from SQLite only** - no video is decoded, no
AI is called:

* how many clips exist per process stage / shot / state / edit role
* how good they are (quality distribution + score averages)
* how much of that is human approved and favorited
* where the library has gaps, how urgent they are and what to search next
* which search queries actually paid off and what they cost

Every function is read-only and deterministic.  Nothing here starts an
acquisition task.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from core.config import CoverageSettings
from core.keyword_expander import KeywordExpander
from core.models import ProcessStage, ReviewStatus
from storage.library import MaterialLibrary

LOGGER = logging.getLogger(__name__)

#: priority buckets (section 18)
PRIORITY_CRITICAL = "critical"
PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_HEALTHY = "healthy"

PRIORITY_LABELS: dict[str, str] = {
    PRIORITY_CRITICAL: "严重缺口",
    PRIORITY_HIGH: "高优先级",
    PRIORITY_MEDIUM: "中等",
    PRIORITY_HEALTHY: "已覆盖",
}

#: search terms per gap stage - deterministic, no LLM (section 9)
STAGE_QUERY_TEMPLATES: dict[str, tuple[str, ...]] = {
    "raw_material": ("{material}原料", "{material} raw material"),
    "preparation": ("{material}挑选", "{material}清洗", "{material}备料"),
    "washing": ("{material}清洗机", "{material}气泡清洗"),
    "cutting": ("{material}切片", "{material}切片机", "{material}切条"),
    "loading": ("{material}装料", "{material}上料机", "{material}装盘"),
    "tray_arrangement": ("{material}摆盘", "{material}铺盘", "{material}上架"),
    "before_drying": ("{material}进烘房前", "{material}入炉"),
    "drying": ("{material}烘干过程", "{material}热泵烘干", "{material}烘干中"),
    "inside_dryer": ("{material}烘干机内部", "{material}烘干房内部", "{material}烘干设备内部"),
    "unloading": ("{material}出料", "{material}出炉", "{material}卸料"),
    "finished_product": ("{material}成品", "{material}成品展示", "{material}干货成品"),
    "packaging": ("{material}包装", "{material}装袋", "{material}封口"),
    "equipment": ("{material}烘干设备", "{material}热泵烘干机", "{material}烘干机厂家"),
    "factory": ("{material}加工厂", "{material}车间", "{material}生产线"),
    "other": ("{material}烘干现场",),
}

#: substitution words when the library category carries a product form
FORM_VARIANTS: tuple[str, ...] = ("", "片", "干")

#: short Chinese label per stage, used for one spaced search variant
STAGE_LABELS: dict[str, str] = {
    "raw_material": "原料",
    "preparation": "备料",
    "washing": "清洗",
    "cutting": "切片",
    "loading": "装料",
    "tray_arrangement": "摆盘",
    "before_drying": "进烘房前",
    "drying": "烘干过程",
    "inside_dryer": "烘干机内部",
    "unloading": "出料",
    "finished_product": "成品",
    "packaging": "包装",
    "equipment": "烘干设备",
    "factory": "加工厂",
    "other": "烘干现场",
}


@dataclass
class StageCoverage:
    """Coverage of one process stage inside one library category."""

    stage: str
    total: int = 0
    approved: int = 0
    favorite: int = 0
    target: int = 0
    priority: str = PRIORITY_HEALTHY

    @property
    def missing(self) -> int:
        return max(0, self.target - self.total)

    @property
    def ratio(self) -> float:
        if self.target <= 0:
            return 1.0
        return round(self.total / self.target, 3)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "total": self.total,
            "approved": self.approved,
            "favorite": self.favorite,
            "target": self.target,
            "missing": self.missing,
            "ratio": self.ratio,
            "priority": self.priority,
            "priority_label": PRIORITY_LABELS.get(self.priority, self.priority),
        }


@dataclass
class CoverageReport:
    """Everything the coverage tab / CLI needs for one category."""

    library_category: str = ""
    total: int = 0
    real: int = 0
    approved: int = 0
    favorite: int = 0
    review: dict[str, int] = field(default_factory=dict)
    stages: list[StageCoverage] = field(default_factory=list)
    shots: dict[str, int] = field(default_factory=dict)
    states: dict[str, int] = field(default_factory=dict)
    edit_roles: dict[str, int] = field(default_factory=dict)
    quality: dict[str, int] = field(default_factory=dict)
    score_averages: dict[str, float] = field(default_factory=dict)
    count_mode: str = "all"

    @property
    def gaps(self) -> list[StageCoverage]:
        return [stage for stage in self.stages if stage.missing > 0]

    def as_dict(self) -> dict[str, Any]:
        return {
            "library_category": self.library_category,
            "total": self.total,
            "real": self.real,
            "approved": self.approved,
            "favorite": self.favorite,
            "review": dict(self.review),
            "count_mode": self.count_mode,
            "stages": [stage.as_dict() for stage in self.stages],
            "shots": dict(self.shots),
            "states": dict(self.states),
            "edit_roles": dict(self.edit_roles),
            "quality": dict(self.quality),
            "score_averages": dict(self.score_averages),
            "gaps": [stage.as_dict() for stage in self.gaps],
        }


def priority_for(current: int, target: int, settings: CoverageSettings) -> str:
    """Deterministic priority from ``current / target`` (section 18)."""

    if target <= 0:
        return PRIORITY_HEALTHY
    if current <= 0:
        return PRIORITY_CRITICAL
    ratio = current / target
    if ratio >= float(settings.priority_healthy_ratio):
        return PRIORITY_HEALTHY
    if ratio < float(settings.priority_high_ratio):
        return PRIORITY_HIGH
    return PRIORITY_MEDIUM


class CoverageAnalyzer:
    """Read-only coverage analytics over the clip database."""

    def __init__(self, library: MaterialLibrary, settings: CoverageSettings) -> None:
        self.library = library
        self.settings = settings

    # -- category discovery ------------------------------------------------
    def categories(self) -> list[tuple[str, int]]:
        return self.library.category_counts()

    def _where(self, category: str | None, *, provenance: str | None = None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if category:
            clauses.append(
                "COALESCE(NULLIF(library_category, ''), '(legacy)') = ?"
            )
            params.append(category)
        if provenance:
            clauses.append("provenance = ?")
            params.append(provenance)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    # -- one report --------------------------------------------------------
    def report(self, category: str | None = None) -> CoverageReport:
        """Full coverage report for one library category (``None`` = all)."""

        report = CoverageReport(
            library_category=category or "(全部)", count_mode=self.settings.count_mode
        )
        where, params = self._where(category)
        row = self.library.database.query_one(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN provenance = 'douyin_real' THEN 1 ELSE 0 END) AS real, "
            "SUM(CASE WHEN review_status = 'approved' THEN 1 ELSE 0 END) AS approved, "
            "SUM(CASE WHEN favorite = 1 THEN 1 ELSE 0 END) AS favorite, "
            "COALESCE(AVG(material_score), 0) AS m, "
            "COALESCE(AVG(visual_quality_score), 0) AS v, "
            "COALESCE(AVG(subtitle_cleanliness_score), 0) AS s, "
            "COALESCE(AVG(stability_score), 0) AS st, "
            "COALESCE(AVG(composition_score), 0) AS c, "
            "COALESCE(AVG(overall_score), 0) AS o "
            f"FROM clips {where}",
            params,
        )
        if row:
            report.total = int(row["n"] or 0)
            report.real = int(row["real"] or 0)
            report.approved = int(row["approved"] or 0)
            report.favorite = int(row["favorite"] or 0)
            report.score_averages = {
                "material_score": round(float(row["m"] or 0), 3),
                "visual_quality_score": round(float(row["v"] or 0), 3),
                "subtitle_cleanliness_score": round(float(row["s"] or 0), 3),
                "stability_score": round(float(row["st"] or 0), 3),
                "composition_score": round(float(row["c"] or 0), 3),
                "overall_score": round(float(row["o"] or 0), 3),
            }

        report.review = self.review_coverage(category)
        report.stages = self.process_stage_coverage(category)
        report.shots = self.shot_type_coverage(category)
        report.states = self.state_coverage(category)
        report.edit_roles = self.edit_role_coverage(category)
        report.quality = self.quality_distribution(category)
        return report

    # -- individual dimensions --------------------------------------------
    def _group_counts(
        self,
        column: str,
        category: str | None,
        *,
        approved_only: bool = False,
    ) -> dict[str, int]:
        where, params = self._where(category)
        clauses = [where[6:]] if where else []
        if approved_only:
            clauses.append("review_status = 'approved'")
        combined = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.library.database.query(
            f"SELECT COALESCE(NULLIF({column}, ''), 'unknown') AS key, COUNT(*) AS n "
            f"FROM clips {combined} GROUP BY key ORDER BY n DESC, key",
            params,
        )
        return {row["key"]: int(row["n"]) for row in rows}

    def process_stage_coverage(self, category: str | None = None) -> list[StageCoverage]:
        """Counts per process stage with target, gap and priority (sections 1/8)."""

        where, params = self._where(category)
        rows = self.library.database.query(
            "SELECT COALESCE(NULLIF(process_stage, ''), 'other') AS stage, COUNT(*) AS n, "
            "SUM(CASE WHEN review_status = 'approved' THEN 1 ELSE 0 END) AS approved, "
            "SUM(CASE WHEN favorite = 1 THEN 1 ELSE 0 END) AS favorite "
            f"FROM clips {where} GROUP BY stage",
            params,
        )
        counts = {row["stage"]: row for row in rows}
        stages: list[StageCoverage] = []
        known = [stage.value for stage in ProcessStage]
        for stage in known:
            row = counts.get(stage)
            total = int(row["n"]) if row else 0
            approved = int(row["approved"] or 0) if row else 0
            favorite = int(row["favorite"] or 0) if row else 0
            target = self.settings.target_for(stage)
            measured = approved if self.settings.count_mode == "approved" else total
            stages.append(
                StageCoverage(
                    stage=stage,
                    total=total,
                    approved=approved,
                    favorite=favorite,
                    target=target,
                    priority=priority_for(measured, target, self.settings),
                )
            )
        # a stage the model never used but the library contains is still shown
        for stage, row in counts.items():
            if stage in known:
                continue
            total = int(row["n"])
            stages.append(
                StageCoverage(
                    stage=stage,
                    total=total,
                    approved=int(row["approved"] or 0),
                    favorite=int(row["favorite"] or 0),
                    target=self.settings.target_for(stage),
                    priority=priority_for(total, self.settings.target_for(stage), self.settings),
                )
            )
        return stages

    def shot_type_coverage(self, category: str | None = None) -> dict[str, int]:
        return self._group_counts("shot_type", category)

    def state_coverage(self, category: str | None = None) -> dict[str, int]:
        return self._group_counts("material_state", category)

    def edit_role_coverage(self, category: str | None = None) -> dict[str, int]:
        where, params = self._where(category)
        clauses = [where[6:]] if where else []
        clauses.append("t.category = 'edit_role'")
        combined = "WHERE " + " AND ".join(clauses)
        rows = self.library.database.query(
            f"SELECT t.name AS role, COUNT(DISTINCT c.id) AS n FROM clips c "
            f"JOIN clip_tags ct ON ct.clip_id = c.id "
            f"JOIN tags t ON t.id = ct.tag_id {combined} GROUP BY role ORDER BY n DESC",
            params,
        )
        return {row["role"]: int(row["n"]) for row in rows}

    def quality_distribution(self, category: str | None = None) -> dict[str, int]:
        where, params = self._where(category)
        row = self.library.database.query_one(
            "SELECT "
            "SUM(CASE WHEN overall_score >= 0.90 THEN 1 ELSE 0 END) AS excellent, "
            "SUM(CASE WHEN overall_score >= 0.80 AND overall_score < 0.90 THEN 1 ELSE 0 END) AS good, "
            "SUM(CASE WHEN overall_score >= 0.70 AND overall_score < 0.80 THEN 1 ELSE 0 END) AS fair, "
            "SUM(CASE WHEN overall_score < 0.70 THEN 1 ELSE 0 END) AS weak "
            f"FROM clips {where}",
            params,
        )
        return {
            ">=0.90": int((row or {})["excellent"] or 0),
            "0.80-0.89": int((row or {})["good"] or 0),
            "0.70-0.79": int((row or {})["fair"] or 0),
            "<0.70": int((row or {})["weak"] or 0),
        }

    def review_coverage(self, category: str | None = None) -> dict[str, int]:
        where, params = self._where(category)
        rows = self.library.database.query(
            "SELECT COALESCE(NULLIF(review_status, ''), 'unreviewed') AS status, "
            f"COUNT(*) AS n FROM clips {where} GROUP BY status",
            params,
        )
        counts = {row["status"]: int(row["n"]) for row in rows}
        for status in ReviewStatus:
            counts.setdefault(status.value, 0)
        favourite_row = self.library.database.query_one(
            f"SELECT COUNT(*) AS n FROM clips {where} "
            + ("AND favorite = 1" if where else "WHERE favorite = 1"),
            params,
        )
        counts["favorite"] = int(favourite_row["n"]) if favourite_row else 0
        return counts

    # -- gaps + recommendations -------------------------------------------
    def gap_report(self, category: str | None = None) -> list[StageCoverage]:
        """Stages below their target, ordered by urgency (sections 8/32)."""

        order = {PRIORITY_CRITICAL: 0, PRIORITY_HIGH: 1, PRIORITY_MEDIUM: 2, PRIORITY_HEALTHY: 3}
        gaps = [stage for stage in self.process_stage_coverage(category) if stage.missing > 0]
        return sorted(
            gaps,
            key=lambda stage: (order.get(stage.priority, 9), -stage.missing, stage.stage),
        )

    def recommended_queries(
        self,
        category: str | None,
        stage: str,
        *,
        observed_material: str | None = None,
        limit: int = 6,
    ) -> list[str]:
        """Deterministic search terms for one gap (sections 9/33).

        Templates only: no LLM call, no network, stable order.
        """

        material = (observed_material or "").strip()
        if not material and category:
            base, _form, _category_name = KeywordExpander.split_material(category)
            material = base or category
        material = material or "物料"
        templates = STAGE_QUERY_TEMPLATES.get(stage) or STAGE_QUERY_TEMPLATES["other"]
        queries: list[str] = []
        for template in templates:
            candidate = template.format(material=material)
            if candidate not in queries:
                queries.append(candidate)
            if category and category != material:
                variant = template.format(material=category)
                if variant not in queries:
                    queries.append(variant)
        label = STAGE_LABELS.get(stage)
        if label:
            spaced = f"{category or material} {label}"
            if spaced not in queries:
                queries.insert(0, spaced)
        return queries[: max(1, int(limit))]

    # -- search yield (sections 10/11/12) ---------------------------------
    def search_yield_report(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Per-query yield, ratios and transparent usefulness ranking."""

        rows = self.library.database.query(
            """
            SELECT query,
                   SUM(candidate_count) AS candidates,
                   SUM(unique_candidate_count) AS unique_candidates,
                   SUM(preview_accept_count) AS preview_accepted,
                   SUM(download_count) AS downloads,
                   SUM(final_clip_count) AS clips,
                   COUNT(*) AS runs
            FROM search_yields
            GROUP BY query
            ORDER BY clips DESC, candidates DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        report: list[dict[str, Any]] = []
        for row in rows:
            candidates = int(row["candidates"] or 0)
            unique = int(row["unique_candidates"] or 0)
            accepted = int(row["preview_accepted"] or 0)
            downloads = int(row["downloads"] or 0)
            clips = int(row["clips"] or 0)
            report.append(
                {
                    "query": row["query"],
                    "runs": int(row["runs"] or 0),
                    "candidates": candidates,
                    "unique_candidates": unique,
                    "preview_accepted": accepted,
                    "downloads": downloads,
                    "clips": clips,
                    "unique_rate": round(unique / candidates, 3) if candidates else None,
                    "preview_accept_rate": round(accepted / unique, 3) if unique else None,
                    "download_to_clip_rate": round(clips / downloads, 3)
                    if downloads
                    else None,
                    "candidate_to_clip_rate": round(clips / candidates, 3)
                    if candidates
                    else 0.0,
                }
            )
        return report

    def ranked_queries(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Queries ranked by *useful output*, not by candidate volume (section 11).

        Transparent score: ``clips * 3 + accepted * 1 + candidate_to_clip_rate * 2``
        with approved clips weighted an extra +1 each.  Zero-yield queries stay in
        the list (score 0) so waste is visible.
        """

        report = self.search_yield_report(limit=1000)
        for row in report:
            row["usefulness"] = round(
                row["clips"] * 3
                + row["preview_accepted"]
                + (row["candidate_to_clip_rate"] or 0.0) * 2,
                3,
            )
        ranked = sorted(
            report,
            key=lambda row: (row["usefulness"], row["clips"], row["unique_candidates"]),
            reverse=True,
        )
        return ranked[: max(1, int(limit))]

    def query_cost_analysis(self) -> list[dict[str, Any]]:
        """AI spend per query where it is *reliably attributable* (section 12).

        ``ai_runs`` only knows ``source_video_id`` for preview/segment calls, so a
        query's cost is attributed through the source videos that query
        discovered (``source_videos.matched_queries``).  Tokens that cannot be
        mapped to exactly one query are reported as ``unavailable`` instead of
        being invented.
        """

        rows = self.library.database.query(
            """
            SELECT sv.platform_video_id AS video_id,
                   sv.matched_queries AS matched_queries,
                   COALESCE(SUM(ar.total_tokens), 0) AS tokens,
                   COUNT(ar.id) AS calls
            FROM source_videos sv
            LEFT JOIN ai_runs ar
                   ON ar.source_video_id = sv.id AND ar.origin = 'pipeline'
            GROUP BY sv.id
            """
        )
        per_query: dict[str, dict[str, Any]] = {}
        unattributed_tokens = 0
        unattributed_calls = 0
        for row in rows:
            tokens = int(row["tokens"] or 0)
            calls = int(row["calls"] or 0)
            queries: list[str] = []
            raw = row["matched_queries"]
            if raw:
                try:
                    import json as _json

                    parsed = _json.loads(raw)
                    queries = [str(item) for item in parsed] if isinstance(parsed, list) else []
                except Exception:  # pragma: no cover - defensive
                    queries = []
            if len(queries) == 1:
                bucket = per_query.setdefault(
                    queries[0], {"query": queries[0], "tokens": 0, "calls": 0, "videos": 0}
                )
                bucket["tokens"] += tokens
                bucket["calls"] += calls
                bucket["videos"] += 1
            else:
                unattributed_tokens += tokens
                unattributed_calls += calls

        clips_by_query = {
            row["query"]: int(row["clips"] or 0) for row in self.library.database.query(
                "SELECT query, SUM(final_clip_count) AS clips FROM search_yields GROUP BY query"
            )
        }
        report: list[dict[str, Any]] = []
        for query, bucket in per_query.items():
            clips = clips_by_query.get(query, 0)
            report.append(
                {
                    "query": query,
                    "videos": bucket["videos"],
                    "ai_calls": bucket["calls"],
                    "tokens": bucket["tokens"],
                    "clips": clips,
                    "tokens_per_clip": round(bucket["tokens"] / clips, 1) if clips else None,
                    "attribution": "source_matched_single_query",
                }
            )
        for query, clips in clips_by_query.items():
            if query not in per_query:
                report.append(
                    {
                        "query": query,
                        "videos": 0,
                        "ai_calls": 0,
                        "tokens": 0,
                        "clips": clips,
                        "tokens_per_clip": None,
                        "attribution": "unavailable",
                    }
                )
        report.sort(key=lambda row: row["tokens"], reverse=True)
        if unattributed_tokens or unattributed_calls:
            report.append(
                {
                    "query": "(多条搜索词共享的来源)",
                    "videos": 0,
                    "ai_calls": unattributed_calls,
                    "tokens": unattributed_tokens,
                    "clips": 0,
                    "tokens_per_clip": None,
                    "attribution": "unavailable",
                }
            )
        return report

    # -- source / author / task yield (sections 13/14/15) ------------------
    def source_yield(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.library.database.query(
            """
            SELECT sv.id, sv.platform, sv.platform_video_id, sv.title, sv.author, sv.status,
                   sv.matched_queries,
                   COUNT(c.id) AS clips,
                   COALESCE(AVG(c.overall_score), 0) AS avg_score,
                   SUM(CASE WHEN c.review_status = 'approved' THEN 1 ELSE 0 END) AS approved
            FROM source_videos sv
            LEFT JOIN clips c ON c.source_video_id = sv.id
            GROUP BY sv.id
            HAVING clips > 0
            ORDER BY clips DESC, avg_score DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [
            {
                "source_video_id": row["id"],
                "platform_video_id": row["platform_video_id"],
                "title": row["title"],
                "author": row["author"],
                "status": row["status"],
                "clips": int(row["clips"] or 0),
                "approved": int(row["approved"] or 0),
                "average_score": round(float(row["avg_score"] or 0), 3),
                "clips_per_source_video": round(float(row["clips"] or 0), 2),
            }
            for row in rows
        ]

    def author_yield(self, *, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.library.database.query(
            """
            SELECT sv.author AS author,
                   COUNT(DISTINCT sv.id) AS videos,
                   COUNT(c.id) AS clips,
                   SUM(CASE WHEN c.review_status = 'approved' THEN 1 ELSE 0 END) AS approved,
                   COALESCE(AVG(c.overall_score), 0) AS avg_score
            FROM source_videos sv
            LEFT JOIN clips c ON c.source_video_id = sv.id
            WHERE sv.author IS NOT NULL AND sv.author != ''
            GROUP BY sv.author
            HAVING clips > 0
            ORDER BY clips DESC, avg_score DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [
            {
                "author": row["author"],
                "videos_processed": int(row["videos"] or 0),
                "clips": int(row["clips"] or 0),
                "approved": int(row["approved"] or 0),
                "average_score": round(float(row["avg_score"] or 0), 3),
            }
            for row in rows
        ]

    def task_performance(self, *, limit: int = 30) -> list[dict[str, Any]]:
        # subqueries instead of joins: joining clips *and* search_yields would
        # multiply the rows and inflate every SUM (fan-out)
        rows = self.library.database.query(
            """
            SELECT t.id, t.material, t.target_clip_count, t.status, t.created_at,
                   (SELECT COUNT(*) FROM clips c WHERE c.task_id = t.id) AS clips,
                   (SELECT COALESCE(SUM(sy.candidate_count), 0) FROM search_yields sy
                     WHERE sy.task_id = t.id) AS candidates,
                   (SELECT COALESCE(SUM(sy.download_count), 0) FROM search_yields sy
                     WHERE sy.task_id = t.id) AS downloads,
                   (SELECT COUNT(*) FROM ai_runs ar WHERE ar.task_id = t.id) AS ai_calls,
                   (SELECT COALESCE(SUM(ar.total_tokens), 0) FROM ai_runs ar
                     WHERE ar.task_id = t.id) AS tokens
            FROM tasks t
            ORDER BY t.id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        report: list[dict[str, Any]] = []
        for row in rows:
            clips = int(row["clips"] or 0)
            tokens = int(row["tokens"] or 0)
            report.append(
                {
                    "task_id": row["id"],
                    "material": row["material"],
                    "target": int(row["target_clip_count"] or 0),
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "clips": clips,
                    "candidates": int(row["candidates"] or 0),
                    "downloads": int(row["downloads"] or 0),
                    "ai_calls": int(row["ai_calls"] or 0),
                    "tokens": tokens,
                    "tokens_per_clip": round(tokens / clips, 1) if clips else None,
                }
            )
        return report

    def maintenance_summary(self, *, limit: int = 30) -> list[dict[str, Any]]:
        """Recent operator maintenance actions (sections 28/29)."""

        return self.library.list_maintenance_log(limit=limit)
