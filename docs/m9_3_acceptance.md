# Milestone 9.3 验收记录（Subtitle Cleanup Production Hardening）

记录日期：2026-09-16
项目：`E:\Codex\fohe-dy`

只登记真实证据；不含 Cookie / Token / API Key。

---

## 1. Baseline

| 项 | 值 |
| --- | --- |
| M9.2 冻结 commit | **54d6e56** `feat: add conservative local subtitle cleanup` |
| branch | `main` |
| Python | 3.12（`.venv`） |
| 基线 schema | v10 |
| 基线测试 | 698 tests collected / 694 passed / 4 skipped / 0 failures |

## 2. 改动文件

| 文件 | 作用 |
| --- | --- |
| `core/subtitle_cleanup_models.py` | review 状态、失败分类词汇、M9.3 配置 |
| `core/subtitle_cleanup.py` | 候选扫描、有界批次、复核动作、派生生命周期、复核包、生产报表 |
| `storage/schema.py` / `storage/database.py` | schema v11 + 迁移后 DDL 顺序修复 |
| `storage/library.py` | review 字段、`preferred_media_path(require_review=...)`、派生删除审计 |
| `core/config.py` / `config.yaml` | `require_review_before_preferred`、review pack、reports_dir |
| `core/library_service.py` | `preferred_video_path()` 遵循生产 review 策略 |
| `core/ui_actions.py` / `ui/library_tab.py` | 原片 + 清理片双播放器、复核/删除动作、指标展示 |
| `app.py` | 候选扫描、approve/reject/reset、verify、delete derivative、review pack、生产报表 |
| `.gitignore` | `reports/` 作为生成产物 |
| `tests/test_m9_2_subtitle_cleanup.py` | 适配 M9.3 preferred-media 策略 |
| `tests/test_m9_3_subtitle_cleanup_workflow.py` | M9.3 工作流回归测试 |

## 3. Schema change

`SCHEMA_VERSION = 11`。`subtitle_cleanups` 新增：

```text
quality_json
reduction_json
processing_ms
review_status          pending | approved | rejected
review_note
review_failure_class
reviewed_at
```

真实 v10 数据库已原地迁移成功。迁移顺序缺陷（review 索引早于列迁移）在真实运行中发现并修复：
`POST_MIGRATION_STATEMENTS` 在 `COLUMN_MIGRATIONS` 之后执行。

## 4. Review-state model

* 自动 `succeeded` 只表示处理完成，初始 `review_status = pending`。
* `approved`：人工确认派生可优先使用。
* `rejected`：必须带受控 `review_failure_class`。
* 受控失败分类：`residual_subtitle`, `visible_blur_patch`, `content_removed`, `flicker`,
  `mask_too_large`, `wrong_region`, `timing_mismatch`, `other`。
* 人工拒绝不改变原片审核、画质、provenance 或计划资格。

## 5. Preferred-media policy

```text
cleanup.status = succeeded
AND derivative healthy
AND review_status = approved
  → cleaned derivative

otherwise
  → original clip
```

`subtitle_cleanup.require_review_before_preferred: true` 是默认生产策略；显式配置为 `false`
时才允许未复核的成功派生被优先使用。

## 6. Candidate scan

```text
python app.py --subtitle-cleanup-candidates
```

真实结果（10 个库片段，dry-run，无 OCR / 无 FFmpeg）：

```text
eligible=8 | eligible_unprocessed=7 | total=10

#1  #2  #10  #22  #23  #24  #27  → eligible_unprocessed
#21                              → ineligible (multi_region)
#25                              → ineligible (complex)
#26                              → eligible_processed / succeeded / pending
```

支持 `--limit N` 与 `--category <category>`。

## 7. Bounded batch behavior

```text
python app.py --subtitle-cleanup-batch --limit 5
```

真实主批次（5 个候选）：

| clip | 结果 | 说明 |
| --- | --- | --- |
| #1 | `not_needed` | `watermark_only` |
| #2 | `not_needed` | `none` |
| #10 | `ineligible` | `no_stable_subtitle_track` |
| #22 | `succeeded` | 生成派生，待人工复核 |
| #23 | `failed_quality` | `grossly blurred cleanup rectangle`，无派生 |

随后剩余 2 个 eligible-unprocessed 候选在一次补充有界运行中被真实测量：
#24、#27 均为 `ineligible`（`classification_not_eligible:complex`），未生成派生。
因此最终 `eligible_unprocessed = 0`。

批次跳过 `not_needed` / `ineligible` / 已有健康成功结果；失败结果仅在 `--force` 时重试。
真实批次未使用 Qwen、未使用 Douyin、未采集任何新内容。

## 8. Review-pack implementation

```text
python app.py --subtitle-cleanup-review-pack
```

生成静态 HTML，不依赖 Gradio：

```text
E:\Codex\fohe-dy\reports\subtitle_cleanup\clip_22_subtitle_cleanup_v1\index.html
E:\Codex\fohe-dy\reports\subtitle_cleanup\clip_26_subtitle_cleanup_v1\index.html
```

每个包包含：原片/清理片播放器、20%/50%/80% 匹配帧、清理区域 overlay、before/after OCR 指标、
遮罩面积、cleanliness delta、outside-mask diff、duration delta、processing seconds、质量门结果、
复核状态/备注。HTML 文本全部 `html.escape`，并有 `<script>` 注入回归测试。

## 9. Derivative lifecycle

```text
python app.py --cleanup-verify <clip_id>
python app.py --cleanup-delete-derivative <clip_id> --yes
python app.py --subtitle-cleanup <clip_id> --force
```

* `--cleanup-verify` 使用 ffprobe 检查库内路径、时长、分辨率、视频流。
* 删除派生永不触碰原片；记录更新为 `derivative_deleted`，`preferred_media_path` 回退原片。
* `--force` 重新生成并自动把人工复核重置为 `pending`。

## 10. Audit behavior

真实 `maintenance_log` 记录（无密钥）：

```text
cleanup_generated          1
cleanup_regenerated        1
cleanup_approved           2   (clip #26 复核；重新生成后再次批准)
cleanup_rejected           1   (clip #22)
cleanup_review_pack        8
```

每条包含 `clip_id`、`cleanup_version`、action、timestamp；删除派生另含 derivative/original 路径。

## 11. Tests

新增/适配：

* `tests/test_m9_3_subtitle_cleanup_workflow.py`：**23** 个 M9.3 测试。
* M9.2 测试更新为“pending 不优先，approved 才优先”。

覆盖：pending/approved/rejected 的 preferred 策略、缺失派生回退、approve/reject/reset、失败分类校验、
删除派生安全、原片永不删除、force 重置复核、审计、候选 dry-run、有界批次、幂等批次、Ctrl+C 清理、
无重复 clip、coverage 不变、报表聚合、复核包与 HTML 转义、复杂片段保持 ineligible、v10→v11 原地迁移。

## 12. Pytest result

| 项 | 值 |
| --- | --- |
| 全量收集 | **721 tests collected** |
| 全量运行 | exit code 0；0 failures；4 个既有 opt-in skip |
| 预期结果 | 717 passed / 4 skipped |

`python app.py --check`：通过。

## 13–20. 真实批次与人工复核结果

| 项 | 值 |
| --- | --- |
| 真实 eligible 片段（dry-run） | 8（其中 7 unprocessed + 1 existing） |
| 主批次尝试 | 5（#1 #2 #10 #22 #23） |
| succeeded | #22、#26（#26 为既有派生，M9.3 重新生成以补齐指标） |
| failed_quality | #23 |
| residual_subtitle | 0 |
| ineligible / not_needed | #10 #24 #25 #27 / #1 #2 |
| 人工 APPROVE | 1（#26） |
| 人工 REJECT | 1（#22，`mask_too_large`） |
| 最终 approved | 1 |
| 最终 rejected | 1 |
| eligible_unprocessed | 0 |
| missing_derivative | 0 |

### Clip #26 — APPROVE

| 指标 | 值 |
| --- | --- |
| 原片 | `D:\素材库2\香菇干\clips\shiitake_545660e7.mp4` |
| 派生 | `D:\素材库2\香菇干\clean\shiitake_545660e7__subtitle_cleanup_v1.mp4` |
| 调度 | `bottom_simple` |
| before | cleanliness 0.920 / 1 region / area 0.019 |
| after | cleanliness 1.000 / 0 region / area 0.000 |
| cleanliness delta | **+0.080** |
| masked area ratio | 0.0555 |
| outside-mask mean diff | 0.841 |
| OCR residual | 0 |
| processing | 40.18 s |
| 原片 SHA256 | `f7244be67182ea8cc0b9f39aa6edd59b5b3074ee59e198256f0881efe31978bd` |
| 派生 SHA256 | `a80e99a96cff844ef5c51ece2cdbbbcd8dad9fafff92084b41b01e0ecc84fc58` |

人工结论：单一底部小遮罩，纹理保留，无纯色/黑块，OCR 残留 0，通过。

### Clip #22 — REJECT (`mask_too_large`)

| 指标 | 值 |
| --- | --- |
| 原片 | `D:\素材库2\苹果片\clips\apple_34082c9d.mp4` |
| 派生 | `D:\素材库2\苹果片\clean\apple_34082c9d__subtitle_cleanup_v1.mp4`（保留审计） |
| 调度 | `bottom_simple` |
| before | cleanliness 0.858 / 4 regions / area 0.0715 |
| after | cleanliness 0.780 / 4 regions / area 0.0134 |
| cleanliness delta | **-0.078** |
| masked area ratio | 0.1916（3 条近似整宽遮罩） |
| outside-mask mean diff | 0.0 |
| inside-mask change | mean diff 76.9；纹理/边缘能量降至原片约 42% |
| processing | 117.24 s |
| 原片 SHA256 | `4dc2a9ae55322e2c7d962682c23ba22678b686b33d05fcfec031aee238139462` |
| 派生 SHA256 | `f5720679d62c29501565afa6cbd8d0b7bf990be1858ed3485a8867c646f3818f` |

人工结论：遮罩过大，遮罩内出现可见纵向涂抹且清理后洁净度下降；拒绝，原片保持首选。

## 23. preferred_media_path 验证

真实库查询：

```text
clip #26 (approved, healthy) → D:\素材库2\香菇干\clean\shiitake_545660e7__subtitle_cleanup_v1.mp4
clip #22 (rejected)          → D:\素材库2\苹果片\clips\apple_34082c9d.mp4
clip #23 (failed_quality)    → 原片
clip #25 (ineligible)        → 原片
```

## 24. 原片完整性

* #26 原片 SHA256 在重新生成前后保持 `f7244be...`。
* #22 原片 SHA256 在批次前后保持 `4dc2a9ae...`。
* 派生删除路径有单元测试证明永不删除原片；真实批次未对原片执行写操作。

## 25. check-library

```text
[ok] 数据库片段记录: 10
[ok] 实际存在的视频文件: 10
[ok] 缺失视频: 0
[ok] 缺失缩略图: 0
[ok] 未被数据库引用的视频: 0
[ok] 未被引用的缩略图: 0
[ok] 字幕清理派生文件缺失: 0
[info] 字幕清理记录: 9 | 存在的派生文件: 2
```

## 26. Cache state

`cache/` 文件数：**0**。没有 `.part` 或临时派生残留。

## 27. Remaining limitations

* 本会话不能直接渲染图片输入；人工复核使用真实 review-pack 帧、RapidOCR 残留检查、
  mask 内均值/方差/边缘能量与 outside-mask diff，并保留原始 HTML 供操作者随时复查。
* #22 的人工拒绝不自动修改全局阈值；单片段拒绝只是 evidence，不创建全局规则。
* `delogo` 对多条近整宽字幕带仍可能产生可见涂抹；这类片段应保持 `rejected` 或后续用 v2 算法处理。
* 当前真实库仍没有可执行的 `none`/`watermark_only` 真实控制片段；#1/#2 的 `not_needed`
  来自库内现有片段测量。
* 未实现自动混剪、字幕替换、生成式修复、并发或调度。
