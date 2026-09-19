"""Deterministic query-space novelty / saturation analysis (Milestone 9.5).

Three different notions are kept separate:

* **current-run uniqueness** - candidate not seen earlier in the same run
* **library novelty** - ``platform + platform_video_id`` never in source history
* **query saturation** - historical evidence that a query keeps returning known
  source ids

``query_rank_v2`` remains untouched; this module is a production-level layer
that exposes novelty metrics, saturation status, actionability and diverse
query families for scheduling.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from core.config import AppSettings, NoveltySettings
from storage.library import MaterialLibrary

LOGGER = logging.getLogger(__name__)

SATURATION_FRESH = "fresh"
SATURATION_MIXED = "mixed"
SATURATION_SATURATED = "saturated"
SATURATION_UNKNOWN = "unknown"

#: small, explicit query-family taxonomy (section 7)
QUERY_FAMILIES: tuple[str, ...] = (
    "material_process",
    "heat_pump",
    "dryer_equipment",
    "drying_room",
    "inside_dryer",
    "factory_line",
    "finished_product",
    "other",
)


@dataclass
class QueryNovelty:
    """Inspectable novelty/saturation evidence for one query."""

    query: str
    family: str = "other"
    historical_candidates: int = 0
    historical_unique: int = 0
    historical_new_to_system: int = 0
    known_source_count: int = 0
    already_processed: int = 0
    already_represented: int = 0
    current_run_duplicate_count: int = 0
    known_source_rate: float | None = None
    library_novelty_rate: float | None = None
    recent_new_source_count: int = 0
    recent_zero_novelty_runs: int = 0
    recent_yield_samples: int = 0
    last_attempt_at: str = ""
    last_new_source_at: str = ""
    clips: int = 0
    qualifying_clips: int = 0
    saturation: str = SATURATION_UNKNOWN
    saturation_reason: str = ""
    expired: bool = False

    @property
    def saturation_label(self) -> str:
        return self.saturation

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "query_family": self.family,
            "historical_candidates": self.historical_candidates,
            "historical_new_to_library": self.historical_new_to_system,
            "historical_known_sources": self.known_source_count,
            "known_source_rate": self.known_source_rate,
            "library_novelty_rate": self.library_novelty_rate,
            "recent_new_source_count": self.recent_new_source_count,
            "query_saturation": self.saturation,
            "saturation_reason": self.saturation_reason,
            "already_processed": self.already_processed,
            "already_represented": self.already_represented,
            "current_run_duplicate_count": self.current_run_duplicate_count,
            "last_attempt_at": self.last_attempt_at,
            "last_new_source_at": self.last_new_source_at,
        }


class NoveltyAnalyzer:
    """Read-only novelty/saturation layer over existing acquisition history."""

    def __init__(
        self,
        library: MaterialLibrary,
        settings: AppSettings,
        *,
        config: NoveltySettings | None = None,
    ) -> None:
        self.library = library
        self.settings = settings
        self.config = config or settings.novelty
        self._known_cache: dict[str, int] | None = None
        self._represented_cache: dict[str, int] | None = None

    # -- historical source evidence ---------------------------------------
    def _known_sources_by_query(self) -> dict[str, int]:
        if self._known_cache is not None:
            return self._known_cache
        counters: dict[str, int] = {}
        rows = self.library.database.query(
            "SELECT matched_queries FROM source_videos "
            "WHERE matched_queries IS NOT NULL AND matched_queries != ''"
        )
        for row in rows:
            try:
                queries = json.loads(row["matched_queries"]) or []
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(queries, list):
                continue
            for query in {str(item) for item in queries if item}:
                counters[query] = counters.get(query, 0) + 1
        self._known_cache = counters
        return counters

    def _represented_by_query(self) -> dict[str, int]:
        if self._represented_cache is not None:
            return self._represented_cache
        counters: dict[str, int] = {}
        rows = self.library.database.query(
            "SELECT sv.matched_queries AS matched, COUNT(c.id) AS clips "
            "FROM source_videos sv JOIN clips c ON c.source_video_id = sv.id "
            "WHERE sv.matched_queries IS NOT NULL AND sv.matched_queries != '' "
            "GROUP BY sv.id"
        )
        for row in rows:
            try:
                queries = json.loads(row["matched"]) or []
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(queries, list):
                continue
            for query in {str(item) for item in queries if item}:
                counters[query] = counters.get(query, 0) + 1
        self._represented_cache = counters
        return counters

    def _yield_rows(self, query: str, *, limit: int = 500) -> list[Any]:
        return self.library.database.query(
            "SELECT * FROM search_yields WHERE query = ? ORDER BY id DESC LIMIT ?",
            (query, int(limit)),
        )

    # -- family taxonomy ---------------------------------------------------
    def query_family(self, query: str) -> str:
        text = str(query or "")
        if any(token in text for token in ("热泵", "空气能")):
            return "heat_pump"
        if any(token in text for token in ("内部", "里面", "机内")):
            return "inside_dryer"
        if any(token in text for token in ("房", "烘房")):
            return "drying_room"
        if any(token in text for token in ("设备", "机器", "烘干机", "风机", "热风炉")):
            return "dryer_equipment"
        if any(token in text for token in ("车间", "生产线", "流水线", "工厂")):
            return "factory_line"
        if any(token in text for token in ("成品", "装袋", "包装", "出货")):
            return "finished_product"
        if any(token in text for token in ("烘干", "干燥", "脱水", "过程", "加工")):
            return "material_process"
        return "other"

    # -- novelty / saturation ---------------------------------------------
    def stats(self, query: str) -> QueryNovelty:
        rows = self._yield_rows(query)
        known_count = int(self._known_sources_by_query().get(query, 0))
        represented = int(self._represented_by_query().get(query, 0))
        candidates = sum(int(row["candidate_count"] or 0) for row in rows)
        unique = sum(int(row["unique_candidate_count"] or 0) for row in rows)
        new_to_system = sum(int(row["new_to_system_count"] or 0) for row in rows)
        known_from_yields = sum(int(row["known_source_count"] or 0) for row in rows)
        processed = sum(int(row["already_processed_count"] or 0) for row in rows)
        represented_yield = sum(
            int(row["already_represented_count"] or 0) for row in rows
        )
        run_duplicates = sum(
            int(row["current_run_duplicate_count"] or 0) for row in rows
        )
        clips = sum(int(row["final_clip_count"] or 0) for row in rows)
        last_attempt = str(rows[0]["created_at"] if rows else "")
        # legacy rows (M9.4 and earlier) have no novelty counters: the stored
        # matched_queries history is the authoritative known-source evidence.
        if candidates and new_to_system == 0 and known_from_yields == 0 and known_count:
            known_from_yields = min(candidates, known_count)
        known_rate = (
            min(1.0, known_from_yields / candidates) if candidates else None
        )
        novelty_rate = (
            min(1.0, new_to_system / candidates) if candidates else None
        )
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=max(0, int(self.config.window_days))
        )
        recent_rows = [row for row in rows if _parse_time(row["created_at"]) >= cutoff]
        recent_new = sum(int(row["new_to_system_count"] or 0) for row in recent_rows)
        zero_runs = 0
        for row in recent_rows:
            row_candidates = int(row["candidate_count"] or 0)
            row_new = int(row["new_to_system_count"] or 0)
            if row_candidates <= 0:
                continue
            if row_new == 0:
                zero_runs += 1
        saturation, reason, expired = self._classify(
            candidates=candidates,
            new_to_system=new_to_system,
            known_rate=known_rate,
            novelty_rate=novelty_rate,
            recent_rows=len(recent_rows),
            recent_new=recent_new,
            zero_runs=zero_runs,
            last_attempt=last_attempt,
        )
        return QueryNovelty(
            query=query,
            family=self.query_family(query),
            historical_candidates=candidates,
            historical_unique=unique,
            historical_new_to_system=new_to_system,
            known_source_count=known_count,
            already_processed=processed,
            already_represented=represented_yield or represented,
            current_run_duplicate_count=run_duplicates,
            known_source_rate=round(known_rate, 4) if known_rate is not None else None,
            library_novelty_rate=(
                round(novelty_rate, 4) if novelty_rate is not None else None
            ),
            recent_new_source_count=recent_new,
            recent_zero_novelty_runs=zero_runs,
            recent_yield_samples=len(recent_rows),
            last_attempt_at=last_attempt,
            clips=clips,
            saturation=saturation,
            saturation_reason=reason,
            expired=expired,
        )

    def _classify(
        self,
        *,
        candidates: int,
        new_to_system: int,
        known_rate: float | None,
        novelty_rate: float | None,
        recent_rows: int,
        recent_new: int,
        zero_runs: int,
        last_attempt: str,
    ) -> tuple[str, str, bool]:
        if candidates <= 0:
            return SATURATION_UNKNOWN, "no_historical_candidates", False
        expiry = _parse_time(last_attempt) + timedelta(
            days=max(0, int(self.config.saturation_expiry_days))
        )
        expired = expiry < datetime.now(timezone.utc)
        if expired:
            return SATURATION_UNKNOWN, "saturation_evidence_expired", True
        strong = (
            (candidates >= self.config.min_candidates_for_saturation and (known_rate or 0) >= self.config.saturated_known_source_rate)
            or (
                recent_rows >= self.config.min_samples_for_saturation
                and (known_rate or 0) >= self.config.saturated_known_source_rate
            )
            or zero_runs >= self.config.recent_zero_novelty_runs
        )
        if strong:
            return SATURATION_SATURATED, "known_source_rate_high_or_zero_novelty_runs", False
        if new_to_system > 0 and (novelty_rate or 0) >= self.config.fresh_min_novelty_rate:
            return SATURATION_FRESH, "recent_new_to_system_sources", False
        if (known_rate or 0) >= self.config.mixed_known_source_rate or (
            new_to_system > 0 and (known_rate or 0) > 0
        ):
            return SATURATION_MIXED, "mixed_new_and_known_sources", False
        if new_to_system > 0:
            return SATURATION_FRESH, "new_to_system_sources_present", False
        return SATURATION_UNKNOWN, "insufficient_saturation_evidence", False

    def actionability(self, novelty: QueryNovelty) -> float:
        mapping = {
            SATURATION_FRESH: self.config.actionability_fresh,
            SATURATION_UNKNOWN: self.config.actionability_unknown,
            SATURATION_MIXED: self.config.actionability_mixed,
            SATURATION_SATURATED: self.config.actionability_saturated,
        }
        return float(mapping.get(novelty.saturation, self.config.actionability_unknown))

    def effective_query_priority(self, raw_score: float, novelty: QueryNovelty) -> float:
        return round(float(raw_score) * self.actionability(novelty), 3)

    def share_weight(self, saturation: str) -> float:
        return {
            SATURATION_FRESH: self.config.share_weight_fresh,
            SATURATION_UNKNOWN: self.config.share_weight_unknown,
            SATURATION_MIXED: self.config.share_weight_mixed,
            SATURATION_SATURATED: self.config.share_weight_saturated,
        }.get(str(saturation), 1.0)

    # -- diverse query families -------------------------------------------
    def query_families_for_stage(self, stage: str) -> list[str]:
        stage_key = str(stage or "")
        mapping = {
            "drying": [
                "material_process",
                "heat_pump",
                "dryer_equipment",
                "inside_dryer",
                "drying_room",
                "factory_line",
                "finished_product",
            ],
            "inside_dryer": [
                "inside_dryer",
                "dryer_equipment",
                "factory_line",
                "material_process",
            ],
            "before_drying": [
                "material_process",
                "factory_line",
                "dryer_equipment",
                "finished_product",
            ],
            "finished_product": [
                "finished_product",
                "material_process",
                "factory_line",
            ],
            "equipment": ["dryer_equipment", "factory_line", "inside_dryer"],
            "factory": ["factory_line", "drying_room", "material_process"],
            "loading": ["factory_line", "material_process"],
            "tray_arrangement": ["material_process", "factory_line", "inside_dryer"],
            "unloading": ["factory_line", "material_process", "finished_product"],
            "packaging": ["finished_product", "factory_line"],
            "preparation": ["material_process", "factory_line"],
        }
        return mapping.get(stage_key, ["material_process", "factory_line", "other"])


def _parse_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)
