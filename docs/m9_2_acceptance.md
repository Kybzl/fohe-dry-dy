# Milestone 9.2 验收记录（Conservative Local Subtitle Cleanup）

记录日期：2026-09-16
项目：`E:\Codex\fohe-dy`

只登记真实证据；不含 Cookie / Token / API Key。

---

## 1. Baseline

| 项 | 值 |
| --- | --- |
| M9.1 冻结 commit | **5f51399** `docs: record m9.1 production hardening acceptance` |
| branch | `main` |
| Python | 3.12（`.venv`） |
| 基线测试 | 666 tests / 662 passed / 4 skipped / 0 failures |
| 素材库 | `D:/素材库2`，10 clips / 0 missing media / 0 missing thumbnails / 0 orphans / cache empty |

## 2. 改动文件

| 文件 | 作用 |
| --- | --- |
| `core/subtitle_cleanup_models.py` | 清理配置、状态、轨迹/掩码模型（无 I/O） |
| `core/subtitle_cleanup.py` | 资格门、2fps OCR 几何、时间轨迹、掩码安全、质量门、原子发布、服务与报表 |
| `media/subtitle_cleanup.py` | 确定性 FFmpeg `delogo` 引擎（可时间范围限定） |
| `storage/schema.py` | schema v10：`subtitle_cleanups` 表 + 索引 |
| `storage/library.py` | 清理记录读写、`preferred_media_path()`、健康报告纳入派生文件 |
| `core/config.py` / `config.yaml` | `subtitle_cleanup:` 配置块（阈值全部可调） |
| `app.py` | `--subtitle-cleanup` / `--subtitle-cleanup-report` / `--subtitle-cleanup-batch --limit` / `--force` |
| `core/library_service.py` | `preferred_video_path()`：优先成功派生文件，否则原片 |
| `core/ui_actions.py` / `ui/library_tab.py` | 单片段“运行字幕清理”按钮、清理状态展示、派生预览 |
| `tests/test_m9_2_subtitle_cleanup.py` | 32 个 M9.2 回归测试（纯几何 + 服务 fake，无云端调用） |

## 3. Schema change

`SCHEMA_VERSION = 10`，新表：

```sql
subtitle_cleanups(
  id, clip_id, version, status, engine, source_analysis_version,
  output_path, eligible, skip_reason, regions_json,
  before_metrics_json, after_metrics_json, settings_json,
  error, created_at, updated_at,
  UNIQUE(clip_id, version)
)
```

状态集合：`not_needed | ineligible | pending | succeeded | failed_processing | failed_quality | residual_subtitle`。

## 4. 清理算法

```text
资格门（bottom_simple / top_simple / single_region）
  → 2 fps 本地 RapidOCR 几何采样（无 Qwen / 无云端）
  → 时间轨迹关联（IoU + 垂直几何，文本变化不拆轨）
  → 最小安全矩形 + 面积/宽度/高度/数量守卫
  → FFmpeg delogo（按 track 活跃区间 time-scoped）
  → ffprobe + 视觉质量门（时长/分辨率/掩码外差异/黑块/糊块）
  → 清理后 OCR 前后对比（字幕证据必须可量化减少）
  → 临时文件 → ffprobe → 原子发布到 <分类>/clean/
  → SQLite subtitle_cleanups 记录
```

原片 `clips/*.mp4` 不可变；失败/不确定时保留原片并记录状态。

## 5. 资格规则

| 分类 | 行为 |
| --- | --- |
| `none`, `watermark_only` | `not_needed`，不生成派生文件 |
| `bottom_simple`, `top_simple`, `single_region` | 进入几何/稳定性/掩码安全门 |
| `multi_region`, `colored_block`, `large_center_text`, `promotional_overlay`, `dense_text`, `complex` | `ineligible`，不尝试 |

## 6. OCR 采样

* 引擎：RapidOCR（PP-OCR ONNX，CPU）仅本地
* 采样率：`2.0 fps`（`subtitle_cleanup.sample_fps`）
* 上限：`240` 帧（`max_samples`）
* 每次采样记录：timestamp / bbox / normalized bbox / confidence / recognized text

## 7. 时间轨迹规则

| 参数 | 默认值 |
| --- | --- |
| IoU 关联 | `min_iou = 0.20` |
| 中心距离 | `max_center_distance = 0.10` |
| 最短持续 | `min_persistence = 0.55` |
| 最短时长 | `min_track_seconds = 0.30` |
| 垂直抖动 | `max_vertical_jitter = 0.05` |
| 包络增长 | `max_union_growth = 0.75` |
| 包络面积 | `max_union_area_ratio = 0.15` |

同一采样时刻一条轨迹最多吸收一个文字框；持久度按**不同时间戳**计算，避免同帧多框导致 >1.0。

## 8. 掩码安全规则

| 参数 | 默认值 |
| --- | --- |
| 外扩 | `margin_ratio = 0.02` 或至少 `4 px` |
| 最大宽度 | `0.90` |
| 最大高度 | `0.30` |
| 单区域最大面积 | `0.10` |
| 总遮罩面积 | `0.20` |
| 最大区域数 | `3` |

时间范围：每个轨迹按 `padding_seconds = 0.20` 扩展后合并，delogo 使用 `enable='between(t,...)'`，不对整个片段盲目固定擦除。

## 9. 处理引擎

* 引擎：FFmpeg `delogo`（标准参数 `x/y/w/h`，确定性、无 GPU）
* 视频：`libx264 / preset medium / crf 18 / yuv420p`
* 音频：`-c:a copy`（保留音频流，不重编码）
* EVCapture FFmpeg 构建不支持非标准 `band` 参数，已在真实验收中移除该参数。

## 10. 质量门

* 时长漂移 ≤ `0.75s`
* 分辨率漂移 ≤ `2px`
* 掩码外平均像素差 ≤ `12`
* 黑块/纯色块检测（mask 内均值/标准差）
* 严重模糊块检测（mask 内局部纹理方差/边缘比率）

质量门失败：删除临时派生、保留原片、记录 `failed_quality`。

## 11. CLI / UI

```bash
python app.py --subtitle-cleanup 26
python app.py --subtitle-cleanup 26 --force
python app.py --subtitle-cleanup-report
python app.py --subtitle-cleanup-batch --limit 5
```

UI 素材库详情显示：原字幕分类、洁净度、清理资格、清理状态、清理版本、派生预览；可对单片段运行本地清理。

## 12. 测试

| 项 | 值 |
| --- | --- |
| 新增测试文件 | `tests/test_m9_2_subtitle_cleanup.py` |
| 新增测试 | **32** |
| 全量收集 | **698 tests collected** |
| 全量运行 | exit code 0；**0 failures**；4 个既有 opt-in skip |
| 预期结果 | 694 passed / 4 skipped |

覆盖：资格（bottom/top/single、multi_region/colored_block、none/watermark）、轨迹 IoU/文本变化、同帧多框不膨胀、抖动、持久度、padding、最大面积、时间范围、引擎失败、损坏派生、时长/分辨率漂移、OCR 残留、质量门、原片不可变、原子发布、幂等、force、DB 关联、cache 清理、强制重跑后旧派生不孤儿。

## 13. Acceptance A — 真实简单字幕

选中片段：**#26**，分类 `bottom_simple`，真实抖音素材。

| 项 | 值 |
| --- | --- |
| 原片 | `D:\素材库2\香菇干\clips\shiitake_545660e7.mp4` |
| 原片 SHA256 | `f7244be67182ea8cc0b9f39aa6edd59b5b3074ee59e198256f0881efe31978bd` |
| 派生 | `D:\素材库2\香菇干\clean\shiitake_545660e7__subtitle_cleanup_v1.mp4` |
| 派生 SHA256 | `a80e99a96cff844ef5c51ece2cdbbbcd8dad9fafff92084b41b01e0ecc84fc58` |
| 清理前 | `bottom_simple` / cleanliness `0.92` / 区域最多 `1` / 覆盖率 `0.019` |
| 清理后 | `none` / cleanliness `1.00` / 区域最多 `0` / 覆盖率 `0.000` |
| 清理区域 | `x1=0.133 y1=0.743 x2=0.864 y2=0.818`，面积比 `0.055`，persistence `100%` |
| 时间范围 | `[0.0, 4.2]`（片长 4.5s） |
| 引擎 | `ffmpeg_delogo` |
| 质量门 | pass，outside diff `0.841` |
| OCR 证据 | before regions `1` → after `0`，residual `0` |
| 处理时间 | 约 `40.4s`（真实 CPU OCR + FFmpeg） |
| DB | `subtitle_cleanups(clip_id=26, status=succeeded, version=subtitle_cleanup_v1)` |

FFprobe（原片 / 派生一致）：

```text
video: h264 1080x1920 30/1
audio: aac
duration: 4.500000
```

原片完整性：验收后 SHA256 仍为 `f7244be...`；幂等重跑（无 `--force`）12ms 返回已有结果，原片 SHA256 不变。

## 14. Acceptance B — 无字幕控制

现有 10 片段真实素材库中**不存在**可用的真实 `none` / `watermark_only` 控制片段：真实片段 #21–#27 的本地测量分别为
`bottom_simple / bottom_simple / top_simple / single_region / complex / bottom_simple / single_region`。

因此 B 无法在不伪造测试媒体的前提下执行；这是素材库覆盖限制，不是清理逻辑失败。

## 15. Acceptance C — 复杂文字控制

真实片段 **#25** 在 2 fps 本地 OCR 下测量为 `complex`（cleanliness `0.84`，区域最多 `3`，覆盖率 `0.042`）。

```text
python app.py --subtitle-cleanup 25
→ [ineligible] 原因: classification_not_eligible:complex
```

未生成派生文件，未尝试破坏性清理。

## 16. 最终库健康

```text
python app.py --check-library
[ok] 数据库片段记录: 10
[ok] 实际存在的视频文件: 10
[ok] 缺失视频: 0
[ok] 缺失缩略图: 0
[ok] 未被数据库引用的视频: 0
[ok] 未被引用的缩略图: 0
[ok] 字幕清理派生文件缺失: 0
[info] 字幕清理记录: 2 | 存在的派生文件: 1
```

`cache/` 文件数：**0**。派生目录无 `*.part` 残留。

## 17. 限制与后续建议（不属于 M9.2 范围）

* v1 只使用 FFmpeg `delogo`；OpenCV inpainting 留待真实视觉质量对比后决定。
* 掩码是每个轨迹的并集矩形，不处理旋转/透视/动态位移字幕。
* 当前素材库没有真实 `none` / `watermark_only` 控制片段；后续采集时应补一个控制样本。
* 不做自动混剪、字幕条替换、OCR/Qwen 改写；M9.2 只产出可选派生。
