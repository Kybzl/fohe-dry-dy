# Milestone 9.5 验收记录（Novelty-aware Production Acquisition）

记录日期：2026-09-16
项目：`E:\Codex\fohe-dy`

只登记真实证据；不含 Cookie / Token / API Key。

---

## 1. Baseline

| 项 | 值 |
| --- | --- |
| M9.4 冻结 commit | **76f4e67** `feat: integrate production expansion with cleanup routing` |
| branch | `main` |
| schema | v11 → **v12** |
| 基线测试 | 744 tests / 740 passed / 4 skipped / 0 failures |
| 冻结 | CDP/browser、dtk、Qwen routing、source dedup、`subtitle_cleanup_v1`、cleanup review policy、`query_rank_v2` 公式 |

## 2. 改动文件

| 文件 | 作用 |
| --- | --- |
| `core/novelty.py` | novelty / saturation / query-family / actionability |
| `core/config.py` / `config.yaml` | `novelty:` 阈值与 primary/reserve 配置 |
| `core/plans.py` | `PlanQuery` novelty 字段、`PlanProgress.query_audit` |
| `core/production.py` | family-diverse primary/reserve 查询、gap actionability、有效优先级 |
| `core/plan_runner.py` | saturated primary 跳过、reserve 激活、saturation-weighted candidate share、query audit |
| `core/models.py` | `TaskRequest` plan metadata；`PipelineStats` novelty counters；`PipelineResult.query_audit` |
| `core/orchestrator.py` | 发现时区分 current-run unique / new-to-system / known / processed / represented |
| `storage/schema.py` / `storage/database.py` | schema v12 + `search_yields` novelty 列与迁移后索引 |
| `storage/library.py` | 持久化 novelty/yield audit 字段 |
| `app.py` | plan 查询族/饱和度/有效优先级展示、query execution audit |
| `tests/test_m9_5_novelty.py` | M9.5 回归测试 |

## 3. Novelty definitions

| 概念 | 定义 |
| --- | --- |
| `current_run_duplicate` | 同一 run 内已被另一个 query 发出的 candidate |
| `new_to_system` | `platform + platform_video_id` 从未出现在 `source_videos` |
| `known_source` | 已存在于 `source_videos`，不论状态 |
| `already_processed` | known source 处于 terminal historical state |
| `already_represented` | known source 已有至少一个 semantic clip |
| `recent_failure` | known source 仍在 retry/failure window；retry 与 novelty 分开 |

`failed_ai` 仍然计入 `known_source`，不是 novel source；M9.5 未修改任何 retry policy。

## 4–5. Saturation model and thresholds

使用 `search_yields` + `source_videos.matched_queries` + source/task history 的确定性模型。
状态：`fresh | mixed | saturated | unknown`。

配置（`config.yaml → novelty`）：

```text
window_days: 30
saturation_expiry_days: 30
min_candidates_for_saturation: 4
min_samples_for_saturation: 3
saturated_known_source_rate: 0.90
mixed_known_source_rate: 0.50
recent_zero_novelty_runs: 2
fresh_min_novelty_rate: 0.25
```

恢复/过期：超过 `saturation_expiry_days` 没有新证据时，历史 saturated 判定自动回退为 `unknown`。

## 6. Query families

小型固定 taxonomy：

```text
material_process | heat_pump | dryer_equipment | drying_room
inside_dryer | factory_line | finished_product | other
```

primary 查询优先一个 family 一个 query，避免四个近义词挤占列表；reserve 使用未占用的 family。

## 7–8. Primary / reserve design and scheduling

Plan item 可含 4 primary + 最多 4 reserve。

* primary 正常参与排序；已经明确 saturated 的 primary 被跳过并记录 `primary_saturated`。
* reserve 仅在 live primary 耗尽、primary 全部 saturated、primary 零 novelty、browser failure 或预算仍有剩余时激活。
* 每次激活写入 `reserve_activation_reason`。
* candidate share = `剩余候选预算 / 剩余 non-saturated 查询数 × saturation share weight`。

真实 run 中出现的 activation reason：

```text
primary_zero_novelty
remaining_budget_reallocation
browser_failure
```

## 9–10. Actionability and reporting

```text
coverage_priority = 原有 coverage priority（未改）
query_actionability = fresh 1.0 | unknown 0.70 | mixed 0.50 | saturated 0.10
effective_priority = coverage_priority × query_actionability
                     × (1 - 0.5 × saturated-primary-fraction)
```

`query_rank_v2` 公式保持不变；`PlanQuery.raw_score` 单独暴露 raw score，`score` 仍保留原
rediscovery penalty 语义。

`--production-gaps` 现在显示：覆盖优先、行动力、有效优先、最佳查询、query family、historical novelty、
known-source rate、saturated primary 数量；不再显示误导性的 “0% duplicate risk”。

## 11. M9.4 plan #15 offline before/after

Plan #15 历史记录未修改。离线用 M9.5 novelty 层重新计算：

| query | family | historical candidates | new-to-library | known sources | known rate | novelty rate | saturation |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 辣椒干 烘干过程 | material_process | 9 | 0 | 5 | 55.6% | 0% | saturated |
| 辣椒烘干过程 | material_process | 9 | 0 | 5 | 55.6% | 0% | saturated |
| 辣椒干烘干过程 | material_process | 5 | 0 | 3 | 60.0% | 0% | saturated |
| 辣椒热泵烘干 | heat_pump | 20 | 0 | 5 | 25.0% | 0% | saturated |

Before（M9.4 报告）：`duplicate risk = 0%`。

After（M9.5）：4/4 queries **saturated**，historical new-to-library 0，novelty 0%。
辣椒干 drying 的 actionability 从 0.70 降到 **0.53**，effective priority 从 5.60 降到 **2.94**。

## 12–13. Tests and pytest

| 项 | 值 |
| --- | --- |
| 新增 M9.5 测试 | **19** |
| 全量收集 | **763 tests collected** |
| 全量运行 | exit code 0；0 failures；4 个既有 opt-in skip |
| 预期结果 | 759 passed / 4 skipped |

覆盖：current-run unique ≠ new-to-system、known source（含 failed_ai）、already represented、
known/novelty rate、saturated/fresh、saturation expiry、family assignment、lexical duplicate 多样性、
reserve activation、saturated primary 零预算、fresh reserve 预算、候选硬预算、target_reached、
query_space_exhausted、actionability、deterministic tie-break、raw query_rank_v2、cleanup 非干扰。

`python app.py --check`：通过。

## 14–17. Real selected objective

| 项 | 值 |
| --- | --- |
| category / stage | **红薯干 / drying** |
| coverage priority（raw） | 5.60 |
| query actionability | 0.70 |
| effective priority | **3.92** |
| saturated primary | 0/4 |

选择原因：红薯干 drying 与多个 drying 缺口 raw coverage priority 并列最高（5.60），但 query space
非饱和；M9.4 的辣椒干 drying 因 2/4 primary saturated，actionability 0.53、effective 2.94，被排在后面。

## 18–20. Queries

Primary：

```text
红薯干烘干          material_process
红薯热泵烘干        heat_pump
红薯烘干设备        dryer_equipment
红薯烘干房内部      inside_dryer
```

Reserve：

```text
红薯烘干过程        material_process
红薯烘干            material_process
红薯干燥过程        material_process
红薯空气能烘干      heat_pump
```

实际执行 8/8（bounded query space exhausted）。

## 21–32. Real run metrics

计划 **#16** `M9.5 红薯干 drying novelty`，人工批准后执行；预算 target 1 / candidates ≤8 / previews ≤8 /
downloads ≤4 / tokens ≤60,000。

| 指标 | 结果 |
| --- | --- |
| discovered candidates | 6 |
| current-run unique | 6 |
| new-to-system | **5** |
| known-source | 1 |
| already processed | 1 |
| already represented | 0 |
| previews | 0 |
| downloads | 0 |
| clips | 0 |
| qualifying / off-target | 0 / 0 |
| tokens | 0 |
| stop reason | `query_space_exhausted` |

5 个 new-to-system candidates 全部在本地预筛被 `duration_unknown_unresolved` 拒绝，未进入 preview。
这是明确的 content/duration failure，不是 provider failure，也不是 clean 0-result。

Coverage delta：红薯干 drying 执行前后均为 0；library clip count 仍为 10。

## 33. Cleanup routing

本轮没有产生 valid new clip，因此 M9.4 cleanup routing 实际尝试为 0；没有新的 pending review。
现有真实 review 状态不变（#26 approved、#22 rejected、#23 failed_quality）。

## 34–36. Production-ready / check-library / cache

`--production-ready-report`：

```text
语义片段 10 | original-only 9 | cleaned-preferred 1
生产就绪 10 | 缺失首选媒体 0
cleanup: pending 0 | approved 1 | rejected 1 | failed 1
missing-approved-derivative 0
```

`--check-library`：

```text
数据库片段记录: 10 | 实际视频: 10
缺失视频: 0 | 缺失缩略图: 0
未被引用视频: 0 | 未被引用缩略图: 0
字幕清理记录: 9 | 存在的派生文件: 2
已批准但派生文件缺失: 0
```

`cache/` 文件数：**0**。

## 37. Remaining limitations

* 5 个 new-to-system candidates 都是真实新来源，但本地 duration probe 无法解析时长，全部在 preview 前被
  拒绝。M9.5 只改善 query allocation，不绕过 duration/quality gate。
* 本次 acceptance 证明 M9.5 不再把全部预算花在已饱和的辣椒 query space，并成功发现 5 个新来源；
  但完整 FULL/PARTIAL clip acceptance 仍受媒体元数据/时长可解析性限制。
* reserve 的 “primary 零 novelty” 目前在 primary 阶段结束后触发；没有做更激进的单查询即时 reallocation。
* 没有自动混剪、subtitle_cleanup_v2、generative inpainting、scheduler、concurrency 或新平台。
