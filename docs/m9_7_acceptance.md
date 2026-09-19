# Milestone 9.7 验收记录（Provider Readiness Gate & Resumable Production Pause）

记录日期：2026-09-16
项目：`E:\Codex\fohe-dy`

只登记真实证据；不含 Cookie / Token / API Key。

---

## 1. Baseline

| 项 | 值 |
| --- | --- |
| M9.6 冻结 commit | **b3edcd5** `fix: harden duration resolution and media viability` |
| branch | `main` |
| schema | v12 → **v13** |
| 基线测试 | 781 tests / 777 passed / 4 skipped / 0 failures |
| 外部阻塞 | Qwen HTTP 403 Free quota exhausted |

## 2. 改动文件

| 文件 | 作用 |
| --- | --- |
| `ai/base.py` | `ProviderAccountBlockedError` 与 subtype |
| `ai/openai_compat.py` | HTTP 401/403/404/429/5xx 的 provider failure 分类 |
| `ai/audit.py` | `account_blocked` error_type |
| `ai/readiness.py` | readiness probe、failure taxonomy、无 secrets 诊断 |
| `ai/gateway.py` | account-blocking circuit breaker、跳过后续 provider calls |
| `core/models.py` | `PipelineResult` provider fields、provider metrics |
| `core/orchestrator.py` | 首次 account blocker 后停止调度新 candidate |
| `core/plan_runner.py` | `provider_unavailable` pause、checkpoint、provider state |
| `core/plans.py` / `storage/plans.py` | `PauseReason.PROVIDER_UNAVAILABLE` + plan provider evidence |
| `core/plan_service.py` | provider 暂停状态/原因/model/恢复指令 |
| `core/config.py` / `config.yaml` | provider readiness 配置 |
| `storage/schema.py` | schema v13 provider 状态列 |
| `app.py` | `--check-ai-provider`、plan 运行前 readiness gate |
| `tests/conftest.py` | 单元测试默认不访问真实 provider |
| `tests/test_m9_7_provider_readiness.py` | M9.7 回归测试 |

## 3. Provider failure taxonomy

| class | subtype 示例 | 行为 |
| --- | --- | --- |
| `account_blocking` | `quota_exhausted`, `authentication_failed`, `model_access_denied` | 立即 trip circuit，暂停 plan |
| `transient_provider` | `rate_limited`, `provider_timeout`, `provider_5xx`, `transient_error` | 现有 bounded retry；不永久 trip |
| `request_specific` | `schema_error`, `request_error` | 仅该次请求失败，不 trip |

`ai_runs` 保留原始 status/error；account-blocking 记录 `error_type=account_blocked`。

## 4–5. Readiness implementation / probe cost

`ai/readiness.py` 对 effective models 做最小 probe：

* model 从 `provider.model_for(operation)` 动态读取；
* 三个 operation 若解析到同一 model，只 probe 一次；
* 无图片、`max_tokens=1`、短文本 prompt；
* bypass `AIGateway`，不写入 preview/segment/tagging 的 production token 统计；
* 记录 provider/model/latency/reachable/authorized/failure class/subtype/timestamp，错误文本经 `sanitize_error`。

## 6–7. Circuit-break / transient retry

* 首次 `account_blocking`：`AIGateway` trip circuit，further AI calls 直接跳过，不创建 fake `ai_runs`。
* `transient_provider`：保留现有 bounded retry（provider `max_retries` / backoff），一次普通 timeout/5xx 不永久 trip。
* 无 background polling；恢复由人工 `--run-collection-plan <id>` 触发，重新执行 readiness。

## 8–10. Pause / candidate / resume semantics

* 新 pause reason：`provider_unavailable`；detail subtype：`quota_exhausted` 等。
* 不会误标为 `candidate_budget_exhausted` / `query_space_exhausted` / content rejection。
* 未获得 AI verdict 的 candidate 保持 `failed_ai`/execution failure 语义；未尝试的 candidate 不生成失败行。
* resume 重新 preflight；保留 executed queries、candidate budget、preview/download/token accounting、qualifying count、reserve activation history。

## 11–13. CLI/UI、ai_runs、token accounting

新增：

```bash
python app.py --check-ai-provider
```

输出 provider、effective models、reachable、authorized、ready、failure class/subtype、latency、checked_at。

Plan status 显示：

```text
AI provider: account_blocking | subtype: quota_exhausted | model: qwen3-vl-plus-2025-09-23
最近 provider 检查: ...
恢复方式: 修复 provider 后执行 --run-collection-plan <id>
```

失败调用保留真实 `ai_runs`；circuit 跳过不创建 fake rows。失败请求 token += 0；readiness probe 不计入 acquisition usage。

## 14. M9.6 offline regression before/after

确定性 replay：candidate 1 触发 403 quota，candidate 2–4 必须不再调用 provider。

| 指标 | M9.6 实际 | M9.7 回归 |
| --- | --- | --- |
| preview provider calls | 4 | **1** |
| ai_runs rows | 4 | **1** |
| circuit | 无 | tripped |
| plan pause | candidate_budget_exhausted | provider_unavailable / quota_exhausted |

## 15–16. Tests / pytest

| 项 | 值 |
| --- | --- |
| 新增 M9.7 测试 | **16** |
| 全量收集 | **797 tests collected** |
| 全量运行 | exit code 0；0 failures；4 个既有 opt-in skip |
| 预期结果 | 793 passed / 4 skipped |

覆盖 readiness success、quota/auth/model-access、429/5xx/timeout、circuit trip、后续零调用、无 fake ai_runs、provider failure 非 content rejection、plan pause/subtype、成功工作保留、resume recheck/accounting/no repeat、bounded transient、readiness 不计 production tokens、dynamic routing、诊断无 secrets。

## 17. Real provider readiness result

```text
provider=qwen
effective models:
  preview_filter    qwen3-vl-plus-2025-09-23
  segment_detection qwen3-vl-plus-2025-09-23
  clip_tagging      qwen3-vl-plus-2025-09-23
reachable=true | authorized=false | ready=false
failure_class=account_blocking
subtype=quota_exhausted
detail=HTTP 403 Free quota exhausted
```

## 18–25. Real Phase A acceptance

计划 **#18** `M9.7 provider readiness Phase A` 已人工批准后尝试运行。

| 项 | 结果 |
| --- | --- |
| plan state before run | approved，0 progress |
| acquisition calls | **0** |
| candidates discovered | **0** |
| Qwen acquisition calls | **0** |
| downloads | 0 |
| clips | 0 |
| pause reason | `provider_unavailable` |
| subtype | `quota_exhausted` |
| plan status | `paused` |
| tasks max / sources max / clips | 96 / 663 / 10（未变化） |

Provider gate 在 discovery 之前停止；没有浏览器搜索、没有下载、没有 Qwen acquisition call。

## 26. Resume result

Phase B 未执行：Qwen 仍返回 HTTP 403 quota_exhausted。计划 #18 保持 paused，未创建替代 plan。
后续 quota 恢复后只需执行：

```bash
python app.py --check-ai-provider
python app.py --run-collection-plan 18
```

同一 plan 会重新 preflight，并从 checkpoint 继续。

## 27–29. Health / production-ready / cache

`--check-library`：

```text
clips 10 | videos 10 | missing videos 0 | missing thumbnails 0
unreferenced videos 0 | unreferenced thumbnails 0
missing cleanup derivatives 0 | approved-but-missing 0
```

`--production-ready-report`：

```text
语义片段 10 | original-only 9 | cleaned-preferred 1
生产就绪 10 | 缺失首选媒体 0
cleanup: pending 0 | approved 1 | rejected 1 | failed 1
missing-approved-derivative 0
```

`cache/` 文件数：**0**。

## 30. Remaining limitations

* Phase B（真实恢复后继续采集）等待外部 Qwen quota/billing 恢复；M9.7 按规格不轮询、不降低模型或路由、不伪造 provider 成功。
* readiness probe 需要一个有效 model access 才能判定 ready；它不代表一次完整 acquisition call。
* circuit 状态是单次 run 内存态，plan 的 provider 暂停证据持久化在 SQLite；恢复必须由人工重新运行 plan（无 scheduler）。
* 未实现自动混剪、subtitle_cleanup_v2、scheduler、concurrency 或新平台。
