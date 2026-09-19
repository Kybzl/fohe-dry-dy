# Milestone 9.4 验收记录（Production Library Expansion + Cleanup Integration）

记录日期：2026-09-16
项目：`E:\Codex\fohe-dy`

只登记真实证据；不含 Cookie / Token / API Key。

---

## 1. Baseline

| 项 | 值 |
| --- | --- |
| M9.3 冻结 commit | **5cc8805** `feat: harden subtitle cleanup production workflow` |
| branch | `main` |
| 基线测试 | 721 tests collected / 717 passed / 4 skipped / 0 failures |
| 清理算法 | `subtitle_cleanup_v1` 冻结，未修改 |

## 2. 改动文件

| 文件 | 作用 |
| --- | --- |
| `core/cleanup_routing.py` | 新入库片段的 cleanup 资格路由；清理尝试有界且默认关闭 |
| `core/production_ready.py` | 语义片段级 production-ready 报告 |
| `core/plan_runner.py` | 采集任务完成后自动调用 cleanup router |
| `app.py` | `--production-ready-report`、`--cleanup-new-clips`、`--cleanup-new-limit`、check-library 首选媒体审计 |
| `core/subtitle_cleanup_models.py` / `config.yaml` | 新片段 routing/auto-cleanup 配置；生产计划候选预算收紧到 8 |
| `core/library_service.py` | 导出清单暴露 `preferred_media_path`（不改变语义原始路径） |
| `storage/library.py` | 健康报告区分“已批准但派生缺失” |
| `tests/test_m9_4_production_integration.py` | M9.4 回归测试（23 个） |

## 3. 选中的生产缺口

真实 `python app.py --production-gaps` 的确定性排名（最高分并列时按执行顺序取第一）：

| category | stage | current | target | gap | priority | best query | duplicate risk |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 辣椒干 | drying | 0 | 2 | 2 | **5.60** | 辣椒干 烘干过程 | 0% |

其它同分候选：香蕉干 drying（5.60）；苹果干/苹果片 drying（4.90）。

## 4. 为什么排名最高

* drying 是最高优先工序（`stage_priorities.drying = 1`）。
* 当前真实覆盖为 0，缺口 2。
* 生产权重综合了工序优先级、缺口、编辑角色、历史转化、重复/字幕/token 惩罚。
* 确定性排序在 5.60 并列候选中先返回“辣椒干 drying”。

## 5. 已批准计划

计划 **#15** `M9.4 辣椒干 drying bounded`：

```text
draft #14 创建后归档（预览预算 10，超出 M9.4 建议 <=8）
#15 使用收紧后的生产默认值创建
2026-09-16 04:10:11 人工批准
```

## 6. Objective queries

```text
辣椒干 烘干过程
辣椒烘干过程
辣椒干烘干过程
辣椒热泵烘干
```

均为 `generated_template`，由现有 `query_rank_v2` 排序（M9.4 未修改排序语义）。

## 7. Budgets

| 项 | 计划预算 | 实际使用 |
| --- | --- | --- |
| qualifying target | 1 | 0 |
| candidates | ≤8 | 8 |
| previews | ≤8 | 0 |
| downloads | ≤4 | 0 |
| AI tokens | ≤60,000 | 0 |

## 8–14. 真实采集结果

| 指标 | 结果 |
| --- | --- |
| candidates | 8 |
| unique candidates | 8 |
| dedup suppressed | **8 / 8**（全部 `already_processed`） |
| previews | 0 |
| valid content rejections | 0 |
| provider failures | 0 |
| downloads | 0 |
| new clips | **0** |
| qualifying clips | 0 |
| off-target clips | 0 |
| token use | 0 |

每个查询返回 2 个候选，全部是历史已处理来源（保留原结论 `failed_ai`）；没有触发任何 preview、下载或 Qwen 调用。

## 15–18. 结果分类

| 结果 | 判定 |
| --- | --- |
| 新片段 | 0 |
| 目标命中 | 0 |
| off-target 有效片段 | 0 |
| 真实验收结果 | **HONEST EXHAUSTION**（候选/查询预算真实耗尽） |

计划状态：`paused` / `candidate_budget_exhausted`；4/4 查询已执行；runtime 78 秒。

## 19. Coverage before / after

辣椒干分类所有工序执行前后均为 0：

```text
raw_material 0 | preparation 0 | cutting 0 | tray_arrangement 0
before_drying 0 | drying 0 | inside_dryer 0 | unloading 0
finished_product 0 | packaging 0 | equipment 0 | factory 0
```

## 20–23. 新片段 cleanup routing / attempts

| 项 | 结果 |
| --- | --- |
| 新片段 subtitle classes | 无（没有新片段） |
| cleanup routing per new clip | 无 |
| cleanup attempts | 0（`--cleanup-new-clips --cleanup-new-limit 2` 已启用，但没有新 eligible 片段） |
| cleanup successes / failures | 0 / 0 |

代码路径已由 23 个 M9.4 单元测试覆盖（not_needed / ineligible / eligible / bounded attempts / success stays pending）。

## 24. Pending review count

没有新增 pending cleanup。现有真实库状态不变：

```text
cleanup approved 1 (#26)
cleanup rejected 1 (#22)
failed_quality    1 (#23)
```

## 25. Preferred media result

真实查询：

```text
#26 approved + healthy -> D:\素材库2\香菇干\clean\shiitake_545660e7__subtitle_cleanup_v1.mp4
其它所有片段            -> 原片
```

M9.4 新增的 `preferred_media_path` 使用面审计：

* 导出清单新增 `preferred_media_path` / `preferred_media_kind`，但 `file_path` 仍是原片，语义哈希/溯源/coverage/dedup/plan qualifying 全部继续使用原片。
* UI 详情仍同时暴露原片和清理派生；删除/溯源路径仍使用原片。

## 26. Production-ready report

真实输出摘要：

```text
语义片段 10 | original-only 9 | cleaned-preferred 1
生产就绪 10 | 缺失首选媒体 0
cleanup: pending 0 | approved 1 | rejected 1 | failed 1
missing-approved-derivative 0
```

生产就绪定义：首选媒体文件存在且健康；**不要求**完成字幕清理。

## 27. 真实 none / watermark_only 控制

本轮没有新片段入库，因此没有自然获得新的真实 `none` / `watermark_only` 控制样本。
该限制继续保留；未为了制造控制样本而额外搜索。

## 28. Tests

| 项 | 值 |
| --- | --- |
| 新增 M9.4 测试 | **23** |
| 全量收集 | **744 tests collected** |
| 全量运行 | exit code 0；0 failures；4 个既有 opt-in skip |
| 预期结果 | 740 passed / 4 skipped |

`python app.py --check`：通过。

## 29. check-library

```text
[ok] 数据库片段记录: 10
[ok] 实际存在的视频文件: 10
[ok] 缺失视频: 0
[ok] 缺失缩略图: 0
[ok] 未被数据库引用的视频: 0
[ok] 未被引用的缩略图: 0
[ok] 字幕清理派生文件缺失: 0
[info] 字幕清理记录: 9 | 存在的派生文件: 2
[ok] 已批准但派生文件缺失: 0
```

## 30. Cache state

`cache/` 文件数：**0**。

## Remaining limitations

* 最高优先目标虽然历史重复风险显示 0%，实际 8/8 候选都是已处理来源。这是查询空间污染的真实证据，
  但 M9.4 按规格没有修改 `query_rank_v2` 或生产计划语义；后续若要避免该类花费，应显式提供新的
  objective queries 或在配置层提供 query-space 多样性策略。
* 本轮没有新片段，因此 M9.4 的 cleanup routing 只在单元测试中验证；真实 pending/approve/reject
  流程仍使用 M9.3 的 #22/#26 证据。
* 没有自动混剪、subtitle_cleanup_v2、生成式修复、并发或调度。
