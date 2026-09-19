# Milestone 9.1 验收记录（Production Hardening）

记录日期：2026-09-15
项目：`E:\Codex\fohe-dy`

只登记真实证据，不含 Cookie / Token / API Key。

---

## Baseline

| 项 | 值 |
| --- | --- |
| M9 commit | **73a8abf** `feat: add coverage-driven production acquisition` |
| M9.1 commit | **81ee457** `fix: harden production acquisition semantics` |
| branch | `main` |
| Python | 3.12.10（`.venv`） |
| 测试 | M9.1 全量 **666 tests / 662 passed / 4 skipped / 0 failures** |
| M9 冻结门槛 | 提交 M9 前全量 646/642/4/0 + `--check` 通过 + `--check-library` 通过 |

## Query scheduling（Issue A）

**Before（M9，plan #11 辣椒干）**

```text
candidate budget 10
q1 吃 5、q2 吃 5 → q3/q4 从未执行
executed 2/4 → stop reason 误报 preview_budget_exhausted（previews 0）
```

**After（M9.1）**：每个 objective query 获得 `剩余候选预算 // 未执行查询数`（至少 1）的公平份额，
预算耗尽前每条未执行查询都能轮到；target 达成则立即停止。

| plan | 目标 | 预算 | planned | executed | 每条 query 的候选 | stop reason |
| --- | --- | --- | --- | --- | --- | --- |
| #12 辣椒干/drying | 1 命中 | 候选 ≤10 | 4 | **4** | 2 / 2 / 3 / 3 | `candidate_budget_exhausted` |
| #13 香蕉干/drying | 1 命中 | 候选 ≤10 | 4 | **4** | 2 / 2 / 3 / 3 | `candidate_budget_exhausted` |

`plan_item.progress.executed_queries` 精确记录实际执行的查询（#12: `辣椒干 烘干过程 / 辣椒烘干过程 /
辣椒干烘干过程 / 辣椒热泵烘干`；#13: `香蕉干 烘干过程 / 香蕉烘干过程 / 香蕉干烘干过程 / 香蕉热泵烘干`）。

## Duration（Issue B）

`duration=None` 现在标记为 `duration_unknown` → 触发低成本 media metadata probe
（远端 media URL → probe_remote；必要时受控下载 + ffprobe；HTML/非视频识别为 `invalid_media_source`），
只有**测得**的时长才会进入范围判断。

库内统计（M9.1 之后）：

```text
known valid            32
known out of range      9
unknown                 4  (全部为 duration_unknown_unresolved)
unknown resolved        0  (真实 probe 未能取得时长)
unknown unresolved      4
invalid media           0
```

两次真实运行中共产生 4 条 `duration_unknown_unresolved`（此前会被错误写成 `duration_out_of_range`）；
历史上 8 条 NULL duration 的旧记录保持原样（不在本次证据范围内，未改写）。

## Legacy provider migration（Issue C）

```text
rows scanned            27   (rejected_preview 且三项预筛分数均为 NULL)
rows definitely matched 26   (ai_runs preview_filter permanent_error + provider 错误标记 quota/403)
rows migrated           26   → failed_ai, reject_reason = NULL
rows skipped ambiguous   1   (无 provider 证据，保持 rejected_preview)
audit location          maintenance_log: migrate_legacy_provider_failure ×26 + summary ×1
```

迁移后重试语义（真实查询 dedup 决策）：

```text
7558045241366875432: skip=True reason=recent_failure detail=status=failed_ai age=6:42 < 24.0h
```

不再是 30 天内容淘汰窗口；重复执行迁移时 `migrated = 0`（幂等）。

## Stop reason（Issue D）

| 场景 | Before | After |
| --- | --- | --- |
| candidates 10/10、previews 0 | `preview_budget_exhausted` | **`candidate_budget_exhausted`** |
| 所有目标查询执行完且预算仍有剩余 | `partially_completed`（无说明） | **`query_space_exhausted`**（诚实耗尽） |
| 目标达成 | （无停止原因） | **`target_reached`**（状态 completed） |
| 预算真正耗尽（token/download/preview） | 已正确 | 保持不变（优先级：token → download → preview → candidate） |

## 辣椒 regression（plan #12）

```text
生成: --create-production-plan --category 辣椒干 --stage drying   → 草稿 #12（1 目标）
预算: 候选 ≤10 / 下载 ≤4 / tokens ≤60000
批准: 人工批准；执行: --run-collection-plan 12 --interactive-verification（CDP 9230）
```

| 项 | 值 |
| --- | --- |
| planned / executed queries | 4 / **4**（2,2,3,3 候选） |
| candidates / unique | 10 / 10 |
| dedup 抑制（previously processed / failed_ai 迁移行） | 7 条 rediscovery（保留既有结论，未重跑） |
| duration_unknown | 0（本轮候选均为已有记录） |
| duration_unknown_resolved / unresolved / invalid_media | 0 / 0 / 0 |
| duration_out_of_range | 0 |
| previews / downloads / Qwen calls / provider failures | 0 / 0 / 0 / 0 |
| qualifying / off-target / rejected | 0 / 0 / 10（全部为已知记录） |
| tokens | 0 |
| coverage before → after | 辣椒干 drying 0 → 0（delta 0） |
| stop reason | `candidate_budget_exhausted`（previews 0 时不再误报 preview budget） |

**回归 PASS**：4 条 objective query 全部获得执行机会；`duration=None` 不再记成 out_of_range；
迁移行按 `failed_ai` 短窗口工作；stop reason 与真实预算一致；dedup/quality gate 未放宽；library 未被污染。

## Fresh objective acceptance（plan #13，香蕉干/drying）

按 §11 的真实 ranking + 污染过滤选择：优先级同为 5.60 的 辣椒干/香蕉干 中，辣椒干搜索空间含 26 条
legacy provider 失败记录，香蕉干历史来源为 0（污染最少）→ 选 **香蕉干 / drying**。

| 项 | 值 |
| --- | --- |
| planned / executed queries | 4 / **4**（2,2,3,3 候选） |
| candidates / unique | 10 / 10 |
| previews / downloads | 0 / 0 |
| duration_unknown_resolved / unresolved / invalid_media | 0 / **4** / 0（新增记录，语义正确） |
| Qwen calls / tokens | 0 / 0 |
| clips / qualifying / off-target | 0 / 0 / 0 |
| coverage before → after | 香蕉干 drying 0 → 0 |
| stop reason | `candidate_budget_exhausted`（§12-F，合法结果） |

## Library

```text
clips before  10
clips after   10   (两次验收均未产生片段：候选全部为已知记录或时长不可解)
missing media 0
missing thumbnails 0
orphans       0
cache         0 entries
```

## Final conclusion

```text
PASS
```

理由：四个 hardening 目标全部实现并有真实/测试证据；两次真实运行的 stop reason、duration 语义与
query fairness 与设计一致；M8 冻结能力未改动；未放宽任何阈值或 dedup 策略。唯一未达成的是"获得
qualifying clip"，那是候选供给与预算语义的正常结果（§12-F），不是实现缺陷。

```text
M9.1 COMPLETE
M9.2 subtitle cleanup NOT STARTED
M10 NOT STARTED
```
