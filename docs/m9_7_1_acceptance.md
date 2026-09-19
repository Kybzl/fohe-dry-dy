# M9.7.1 Acceptance — Provider Recovery & Browser-Gated Query Retry

Date: 2026-09-16
Project: `E:\Codex\fohe-dy`

No secrets are recorded.

## Root causes

### A. Stale provider current-state

`run_collection_plan()` persisted provider failure evidence on readiness
failure but never replaced it on a later healthy readiness result.
`PlanService.pause_reason_text()` also appended provider failure subtype
whenever `provider_failure_subtype` existed, regardless of the current pause
reason, so a `human_verification_required` pause displayed the old
`quota_exhausted / qwen3-vl-plus-2025-09-23` values.

### B. Query slot consumed by login wall

`PlanRunner._apply_task_result()` unconditionally appended every query to
`executed_queries`.  A browser `login_required` outcome with 0 candidates was
therefore treated as a completed zero-yield query and could not be retried.

## Fix

* Plan current state now stores `provider_ready` / `provider_name` and clears
  failure fields on a healthy readiness result.  Historical events and
  `maintenance_log` rows remain untouched.
* `PlanService` only shows provider failure details when the current
  `pause_reason == provider_unavailable`; otherwise it shows current healthy
  provider evidence.
* Operational discovery states (`login_required`, `verification_required`,
  `browser_unavailable`, `upstream_bad_gateway`, etc.) now count as
  `queries_attempted` only, not `executed_queries`.
* Only `ok` / `no_results` (or legacy empty status) complete a query.
* `PlanRunner` pauses with `human_verification_required` or
  `backend_unavailable` as appropriate; the same query remains retryable.

## Plan #18 repair

`scripts/repair_m9_7_1_plan18.py` performed the minimal auditable repair:

| Before | After |
| --- | --- |
| task #97 preserved, status `partial` | unchanged |
| query `红薯热泵烘干` in `executed_queries` | removed from completed set |
| query audit entry `completed` implicit | `completed=false`, `operational_blocker=true` |
| provider failure `account_blocking/quota_exhausted` | cleared |
| provider model `qwen3-vl-plus-2025-09-23` | `qwen-vl-max`, `provider_ready=true` |
| pause reason `human_verification_required` | unchanged |
| budgets 0/0/0 | unchanged |

The repair is idempotent: a second run returns `changed=false` with no further
mutations.  No task/source/clip row was deleted or modified by the repair;
`maintenance_log.m9_7_1_plan18_repair` records the change.

## Real validation

```text
python app.py --verify-douyin-browser --verify-query "红薯热泵烘干"
→ session_usable, 5 real /video/ links
```

The operator browser was already logged in, so the SAME plan #18 was resumed
once.  The previously blocked query `红薯热泵烘干` was retried, completed, and
later candidates reached real preview/download:

```text
queries_attempted = 8
executed_queries  = 7 (1 saturated primary skipped)
candidates        = 8
previews          = 3
downloads         = 1
AI calls          = 7
tokens            = 51,084
clips             = 0
plan status       = paused
pause reason      = candidate_budget_exhausted
provider          = qwen | ready=true | model=qwen-vl-max
```

No clip was produced: real content was rejected by the existing subtitle/quality
gates (`subtitle_too_complex`).  No threshold was weakened.

## Regression

```text
805 tests collected / 801 passed / 4 skipped / 0 failures
--check: passed
--check-library: healthy, 10 clips, 0 missing, 0 orphans
--production-ready-report: 10/10 semantic clips production-ready
--check-ai-provider: qwen ready=true, model=qwen-vl-max
cache: 0 files
```

`m9-final` remains at `23a0037` (pre-hotfix freeze).  The hotfix commit is
reported separately.
