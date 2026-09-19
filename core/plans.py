"""Collection plan domain models (Milestone 7, sections 1/2/17/33/35).

A plan is a *proposal* the operator approves; it never starts itself.  These
models are pure data - the storage repository and the runner own the side
effects.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PlanStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


#: statuses that may not be started or edited structurally
TERMINAL_PLAN_STATUSES: frozenset[PlanStatus] = frozenset(
    {
        PlanStatus.COMPLETED,
        PlanStatus.PARTIALLY_COMPLETED,
        PlanStatus.CANCELLED,
        PlanStatus.FAILED,
    }
)

#: statuses that allow structural edits (draft only, section 19/20)
EDITABLE_PLAN_STATUSES: frozenset[PlanStatus] = frozenset({PlanStatus.DRAFT})


class PlanItemStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SATISFIED = "satisfied"
    EXHAUSTED = "exhausted"
    PAUSED = "paused"
    FAILED = "failed"


class PauseReason(StrEnum):
    NONE = ""
    OPERATOR = "operator"
    #: Douyin asked for QR login / CAPTCHA / slider / SMS (section 25)
    HUMAN_VERIFICATION_REQUIRED = "human_verification_required"
    #: dtk backend unavailable after the bounded retries (section 26)
    BACKEND_UNAVAILABLE = "backend_unavailable"
    #: hard budget stops (sections 27/28/29)
    TOKEN_BUDGET_EXHAUSTED = "token_budget_exhausted"
    DOWNLOAD_BUDGET_EXHAUSTED = "download_budget_exhausted"
    PREVIEW_BUDGET_EXHAUSTED = "preview_budget_exhausted"
    #: the process died while running: never auto-resumed (section 24)
    INTERRUPTED = "interrupted"
    RUNTIME_EXHAUSTED = "runtime_exhausted"
    # -- Milestone 9.1 production stop reasons -----------------------------
    #: the objective was met: this is a *stop reason*, not a failure
    TARGET_REACHED = "target_reached"
    #: every eligible objective query was executed (honest exhaustion)
    QUERY_SPACE_EXHAUSTED = "query_space_exhausted"
    #: the unique-candidate budget ran out before the queries did
    CANDIDATE_BUDGET_EXHAUSTED = "candidate_budget_exhausted"
    #: the provider/session failed fatally (not a content judgement)
    PROVIDER_FATAL_ERROR = "provider_fatal_error"
    #: the source itself is exhausted (no more usable rows for this task)
    SOURCE_EXHAUSTED = "source_exhausted"
    #: the configured AI provider is account-blocked / unavailable (M9.7)
    PROVIDER_UNAVAILABLE = "provider_unavailable"


#: operator-facing labels for the raw pause reasons (section 27 of M8)
PAUSE_REASON_LABELS: dict[str, str] = {
    PauseReason.NONE: "未暂停",
    PauseReason.OPERATOR: "操作者手动暂停",
    PauseReason.HUMAN_VERIFICATION_REQUIRED: "需要人工完成抖音验证",
    PauseReason.BACKEND_UNAVAILABLE: "dtk 后端不可用",
    PauseReason.TOKEN_BUDGET_EXHAUSTED: "AI token 预算已用尽",
    PauseReason.DOWNLOAD_BUDGET_EXHAUSTED: "下载预算已用尽",
    PauseReason.PREVIEW_BUDGET_EXHAUSTED: "候选/预览预算已用尽",
    PauseReason.INTERRUPTED: "进程中断后暂停（需人工恢复）",
    PauseReason.RUNTIME_EXHAUSTED: "计划运行时间已用尽",
    PauseReason.TARGET_REACHED: "目标已达成",
    PauseReason.QUERY_SPACE_EXHAUSTED: "所有目标查询已执行（诚实耗尽）",
    PauseReason.CANDIDATE_BUDGET_EXHAUSTED: "候选预算已用尽",
    PauseReason.PROVIDER_FATAL_ERROR: "AI 提供商致命错误",
    PauseReason.SOURCE_EXHAUSTED: "可用来源已耗尽",
    PauseReason.PROVIDER_UNAVAILABLE: "AI provider 不可用",
}


def pause_reason_label(reason: Any, *, fallback: str = "") -> str:
    """Human readable pause reason; unknown values keep their raw code."""

    raw = str(reason or "").strip()
    if not raw:
        return fallback or PAUSE_REASON_LABELS[PauseReason.NONE]
    return PAUSE_REASON_LABELS.get(raw, raw)


class QueryOrigin(StrEnum):
    HISTORICAL = "historical"
    GENERATED_TEMPLATE = "generated_template"


class PlanQuery(BaseModel):
    """One search term a plan item may use, with its ranking evidence."""

    model_config = ConfigDict(extra="ignore")

    query: str
    origin: QueryOrigin = QueryOrigin.GENERATED_TEMPLATE
    score: float = 0.0
    #: which scoring semantics produced ``score`` (section 19 of M8)
    rank_version: str = ""
    #: named score contributions, so the operator can see *why* (section 20)
    components: dict[str, float] = Field(default_factory=dict)
    clips: int = 0
    candidates: int = 0
    unique_candidates: int = 0
    subtitle_rejection_rate: float | None = None
    duplicate_rate: float | None = None
    tokens_per_clip: float | None = None
    tokens_total: int = 0
    approved_clips: int = 0
    evidence: list[str] = Field(default_factory=list)
    # -- Milestone 9.5 novelty-aware planning ------------------------------
    #: ``primary`` queries are planned first; ``reserve`` may be activated when
    #: a primary returns only known sources or the query space is saturated
    role: str = "primary"
    family: str = "other"
    raw_score: float = 0.0
    library_novelty_rate: float | None = None
    known_source_rate: float | None = None
    query_saturation: str = "unknown"
    saturation_reason: str = ""
    recent_new_source_count: int = 0
    effective_production_priority: float = 0.0
    planned_order: int = 0
    reserve_activation_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class PlanProgress(BaseModel):
    """Counters tracked per item and aggregated per plan (section 17)."""

    model_config = ConfigDict(extra="ignore")

    candidates_seen: int = 0
    unique_candidates: int = 0
    preview_calls: int = 0
    downloads: int = 0
    clips_saved: int = 0
    qualifying_clips: int = 0
    ai_calls: int = 0
    ai_tokens: int = 0
    queries_attempted: int = 0
    queries_remaining: int = 0
    #: the concrete queries this item actually executed (Milestone 9 audit)
    executed_queries: list[str] = Field(default_factory=list)
    #: process stage -> saved clip count (off-target value stays visible)
    stage_breakdown: dict[str, int] = Field(default_factory=dict)
    #: Milestone 9.5 per-query execution audit (novelty, family, reserve reason)
    query_audit: list[dict[str, Any]] = Field(default_factory=list)
    runtime_seconds: float = 0.0

    def add(self, other: "PlanProgress") -> "PlanProgress":
        merged = self.model_copy(deep=True)
        for field in (
            "candidates_seen",
            "unique_candidates",
            "preview_calls",
            "downloads",
            "clips_saved",
            "qualifying_clips",
            "ai_calls",
            "ai_tokens",
            "queries_attempted",
        ):
            setattr(merged, field, getattr(merged, field) + getattr(other, field))
        merged.queries_remaining = self.queries_remaining + other.queries_remaining
        merged.runtime_seconds = round(self.runtime_seconds + other.runtime_seconds, 2)
        for stage, count in other.stage_breakdown.items():
            merged.stage_breakdown[stage] = merged.stage_breakdown.get(stage, 0) + count
        merged.query_audit.extend(other.query_audit)
        return merged


class CollectionPlanItem(BaseModel):
    """One coverage objective inside a plan (section 2)."""

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    plan_id: int | None = None
    process_stage: str
    current_count: int = 0
    target_count: int = 0
    gap: int = 0
    requested_clips: int = 1
    priority: str = "medium"
    queries: list[PlanQuery] = Field(default_factory=list)
    max_candidates: int = 0
    max_downloads: int = 0
    max_tokens: int = 0
    progress: PlanProgress = Field(default_factory=PlanProgress)
    status: PlanItemStatus = PlanItemStatus.PENDING
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def satisfied(self) -> bool:
        return self.progress.qualifying_clips >= self.requested_clips

    @property
    def remaining_clips(self) -> int:
        return max(0, self.requested_clips - self.progress.qualifying_clips)

    @property
    def tokens_remaining(self) -> int:
        return max(0, int(self.max_tokens) - int(self.progress.ai_tokens))

    @property
    def downloads_remaining(self) -> int:
        return max(0, int(self.max_downloads) - int(self.progress.downloads))

    @property
    def candidates_remaining(self) -> int:
        return max(0, int(self.max_candidates) - int(self.progress.unique_candidates))

    def next_query(self) -> PlanQuery | None:
        """Next unused query, highest ranked first (deterministic order)."""

        attempted = int(self.progress.queries_attempted)
        if attempted >= len(self.queries):
            return None
        return self.queries[attempted]


class CollectionPlan(BaseModel):
    """A plan the operator reviews, approves and (optionally) runs."""

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    name: str = ""
    status: PlanStatus = PlanStatus.DRAFT
    library_category: str = ""
    count_mode: str = "all"
    created_from: str = "coverage_gap"
    target_final_clips: int = 0
    max_preview_candidates: int = 0
    max_downloads: int = 0
    max_ai_tokens: int = 0
    max_runtime_minutes: float = 0.0
    coverage_before: dict[str, Any] = Field(default_factory=dict)
    coverage_after: dict[str, Any] = Field(default_factory=dict)
    progress: PlanProgress = Field(default_factory=PlanProgress)
    pause_reason: str = ""
    #: Milestone 9.7 provider-unavailable detail (never a secret)
    provider_failure_class: str = ""
    provider_failure_subtype: str = ""
    provider_ready: bool = False
    provider_name: str = ""
    provider_model: str = ""
    provider_operation: str = ""
    provider_checked_at: str = ""
    approval_note: str = ""
    #: operator archive flag: auditable, hidden from the active list, never runnable
    archived: bool = False
    #: marks an acceptance/stub plan so it is not mistaken for real collection history
    test_plan: bool = False
    items: list[CollectionPlanItem] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    approved_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def approved(self) -> bool:
        return self.approved_at is not None

    @property
    def runnable(self) -> bool:
        """Only an approved (or paused/resumed) plan may execute."""

        if self.archived:
            return False
        return self.status in {
            PlanStatus.APPROVED,
            PlanStatus.PAUSED,
            PlanStatus.RUNNING,
        }

    @property
    def pause_reason_label(self) -> str:
        return pause_reason_label(self.pause_reason)

    @property
    def tokens_remaining(self) -> int:
        return max(0, int(self.max_ai_tokens) - int(self.progress.ai_tokens))

    @property
    def downloads_remaining(self) -> int:
        return max(0, int(self.max_downloads) - int(self.progress.downloads))

    @property
    def previews_remaining(self) -> int:
        return max(0, int(self.max_preview_candidates) - int(self.progress.preview_calls))

    @property
    def open_items(self) -> list[CollectionPlanItem]:
        return [item for item in self.items if not item.satisfied]

    def sorted_items(self) -> list[CollectionPlanItem]:
        """Execution order: priority first, then the planner's query ranking."""

        order = {"critical": 0, "high": 1, "medium": 2, "healthy": 3}
        return sorted(
            self.items,
            key=lambda item: (
                order.get(item.priority, 9),
                item.process_stage,
                -(item.queries[0].score if item.queries else 0.0),
            ),
        )

    def estimate_lines(self) -> list[str]:
        lines = [
            f"计划: {self.name or '(未命名)'} [{self.status}]",
            f"素材分类: {self.library_category} | 统计口径: {self.count_mode}",
            f"目标片段: {self.target_final_clips}",
            "目标:",
        ]
        for item in self.sorted_items():
            lines.append(
                f"  {item.process_stage:<18}{item.requested_clips}"
                f"   (当前 {item.current_count}/{item.target_count}, 缺口 {item.gap}, "
                f"{item.priority})"
            )
        return lines


class PlanEstimate(BaseModel):
    """Dry-run cost estimate shown before approval (sections 10/46)."""

    model_config = ConfigDict(extra="ignore")

    previews: int = 0
    downloads: int = 0
    ai_tokens: int = 0
    runtime_minutes: float = 0.0
    confidence: str = "low"
    basis: list[str] = Field(default_factory=list)
    #: monetary cost is only shown when vendor pricing is configured
    estimated_cost: float | None = None


class PlanEffectiveness(BaseModel):
    """Post-run accounting (sections 34/35/36)."""

    model_config = ConfigDict(extra="ignore")

    objective_hit_rate: float | None = None
    tokens_per_qualifying_clip: float | None = None
    downloads_per_qualifying_clip: float | None = None
    qualifying_clips: int = 0
    off_target_clips: int = 0
    stage_breakdown: dict[str, int] = Field(default_factory=dict)
    coverage_delta: dict[str, int] = Field(default_factory=dict)
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
