# Milestone 9 验收记录（覆盖驱动的生产采集）

记录日期：2026-09-15
项目：`E:\Codex\fohe-dy`

本文件只登记真实运行证据，不含 Cookie / Token / API Key。

---

## 1. 生产目标配置（`config.yaml -> production_coverage`）

```text
process_stage_targets:  drying 2 / inside_dryer 1 / before_drying 1 / finished_product 2
                        equipment 1 / factory 1 （次要: loading/tray_arrangement/
                        unloading/packaging/preparation 各 1）
stage_priorities:       drying 1 / inside_dryer 2 / before_drying 3 / finished_product 4
                        / equipment 5 / factory 6 / …（未列出走默认 20）
materials:              苹果干 香菇干 红薯干 香蕉干 芒果干 辣椒干（+ 库里已有分类）
default_item_target_clips 1 / candidates 10 / downloads 4 / tokens 60000
```

目标只写在配置里，SQL/UI 不硬编码；库里没有该工序的片段时缺口由
「目标 − 当前」得出，达标工序默认不列为目标。

## 2. 优先级算法（确定性、可检视）

```text
priority = 3.0 × (1 / stage_rank)          # 工序优先级（drying 最高）
         + 1.0 × log1p(gap)                # 缺口大小
         + 1.5 × (缺失 edit_role 数 / 4)    # 剪辑用途缺失
         + 2.0 × (最佳查询 v2 分数 / 2)      # 历史产出
         − 2.0 × duplicate_rate            # 重复发现（单次）
         − 1.5 × subtitle_rejection_rate    # 字幕淘汰
         − 1.0 × min(1, tokens/片段 / 60000)
         − 2.0 × rediscovery_rate           # 二次发现（跨运行已处理比例）
```

`--production-gaps` 会打印每个缺口的 `priority_components`，排名完全可复核。

## 3. 本机真实缺口 Top（2026-09-15 21:18）

```text
分类     工序            当前 目标 缺口  优先级  最佳查询            分数   重复风险
辣椒干   drying          0    2    2    5.60  辣椒干 烘干过程        0.00    0%
香蕉干   drying          0    2    2    5.60  香蕉干 烘干过程        0.00    0%
苹果干   drying          0    2    2    4.90  苹果干烘干            1.11   19%
苹果片   drying          0    2    2    4.90  苹果干烘干            1.11   19%
```

## 4. 本次真实验收：plan #11（辣椒干 / drying，目标 1 个命中片段）

```text
生成: --create-production-plan --category 辣椒干 --stage drying   → 草稿 #11（1 目标）
目标查询（草稿编辑后）: 辣椒干 烘干过程 / 辣椒烘干过程 / 辣椒干烘干过程 / 辣椒热泵烘干
预算: 候选 ≤10 / 下载 ≤4 / tokens ≤60000（计划级同值）
批准: 人工批准（approve，附验收注记）
执行: --run-collection-plan 11 --interactive-verification（CDP 9230 共享会话，
      session_usable，5 个真实 /video/ 链接）
```

### 执行结果（HONEST EXHAUSTION）

| 项 | 值 |
| --- | --- |
| 执行查询 | `辣椒干 烘干过程`（task #67）、`辣椒烘干过程`（task #68） |
| 候选 / 唯一 | 5 + 5 = 10 / **10** |
| 重复发现（已在库） | 7（既有结论保留，未重跑） |
| 本地淘汰 | 3（`duration_out_of_range`: 上游未返回时长） |
| 预览 | **0**（没有候选进入预筛） |
| 下载 / 片段 | 0 / 0 |
| 目标命中 | 0 / 1 |
| AI 调用 / tokens | **0 / 0**（未触达 Qwen） |
| 结束状态 | `paused(preview_budget_exhausted)`（候选预算 10/10） |
| 覆盖变化 | 辣椒干 drying 0 → 0（delta 0） |

**结论**：这是 §15 允许的 *honest exhaustion*。根因是两类真实约束：

1. 辣椒搜索空间里 7 个候选是早先 M8.3.1「配额 403」那几次尝试留下的
   `rejected_preview/other` 行，仍在 30 天重试窗口内 → 按设计跳过（未改动策略）；
2. 3 个候选上游 dtk 未返回时长，被本地 `duration_out_of_range` 过滤。

M9 已经修好的语义（本次未再产生新污染）：AI 提供商错误（如 403 配额）现在记录为
`failed_ai`（按小时重试），**不再**写成内容淘汰 `rejected_preview`，
统计上进入 `errors` 而不是内容淘汰桶。

## 5. 测试与状态

```text
python -m pytest -q -p no:cacheprovider   →  646 tests, 642 passed, 4 skipped, 0 failures
python app.py --check-library             →  素材总数 10 | 真实抖音 7 | 缺失 0 | 孤儿 0
cache/                                    →  0 entries
```
