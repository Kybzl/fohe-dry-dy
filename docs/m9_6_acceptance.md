# Milestone 9.6 验收记录（Duration Resolution & Media Viability Hardening）

记录日期：2026-09-16
项目：`E:\Codex\fohe-dy`

只登记真实证据；不含 Cookie / Token / API Key / signed URL。

---

## 1. Baseline

| 项 | 值 |
| --- | --- |
| M9.5 冻结 commit | **cb9322d** `feat: add novelty-aware production acquisition` |
| branch | `main` |
| schema | v12（本里程碑不新增表） |
| 基线测试 | 763 tests / 759 passed / 4 skipped / 0 failures |
| M9.5 遗留 | 5 new_to_system sources 全部 `duration_unknown_unresolved` |

## 2. 改动文件

| 文件 | 作用 |
| --- | --- |
| `media/ffmpeg.py` | 多信号 duration 解析、`duration_source`、bounded decode |
| `core/orchestrator.py` | 完整 duration/media 阶梯、媒体有效性分类、cache 复用、audit |
| `core/models.py` | duration/probe 指标计数器 |
| `core/config.py` / `config.yaml` | duration decode/timeout、duration retry window |
| `analyzers/candidate_filter.py` | 传递 duration resolution retry window |
| `storage/dedup.py` | duration-unresolved 独立短 retry window |
| `core/dependencies.py` | 注入 duration retry window |
| `app.py` | plan 执行前 backend preflight、`--duration-recheck` |
| `tests/test_m9_6_duration.py` | M9.6 回归测试 |
| `tests/test_m9_1_production_hardening.py` | corrupt downloaded payload 期望更新为 invalid_media |

## 3. 旧 duration-resolution path

```text
candidate discovered
  -> CandidateFilter: candidate.duration is None -> duration_unknown=True
  -> CollectionOrchestrator._process_candidate
  -> CollectionOrchestrator._resolve_duration
       A. DouyinSource.get_download_url(video_id)
       B. MediaToolkit.probe_remote(media_url)
       C. MediaDownloader.download(media_url, cache/probe/<id>.probe)
       D. MediaToolkit.probe(dest)
       E. 失败即 duration_unknown_unresolved（status=rejected, 30-day 语义）
  -> CollectionOrchestrator._duration_within_range
  -> _store_candidate_rejection
```

## 4. M9.5 五个 source 的真实根因

Plan #16 执行时没有对 dtk backend 做 preflight，`DouyinSource` 仍指向
`http://127.0.0.1:8000`；该进程本地 dtk 未运行，因此
`get_download_url()` 抛错，`_resolve_duration` 拿到的是空 media URL，
从未进入 remote probe 或 controlled download。

五个 source 行在 M9.5 结束时 `media_url = NULL`、`duration = NULL`：

| platform_video_id | M9.5 before | 实际根因 | controlled recheck after | method / host |
| --- | --- | --- | --- | --- |
| 7643337822116170402 | duration_unknown_unresolved | no playable media URL（dead local backend） | **43.734s** | remote_format / v5-dy-ov-experiment.zjcdn.com |
| 7537550882415463695 | duration_unknown_unresolved | same | **16.9s** | remote_format / v3-dy-o.zjcdn.com |
| 7578886056784692453 | duration_unknown_unresolved | same | **38.134s** | remote_format / v5-dy-ov-experiment.zjcdn.com |
| 7156514076686896415 | duration_unknown_unresolved | same | **20.922s** | remote_format / v5-hl-zenl-ov.zjcdn.com |
| 7685208795508772115 | duration_unknown_unresolved | same | **15.07s** | remote_format / v3-dy-o.zjcdn.com |

额外发现并修复的潜在缺陷：`_duration_within_range()` 使用了 final clip 的
3–15s 限制，而不是 source candidate 的 5–300s 限制；15–44s 的合法 source
会被错误标为 `duration_out_of_range`。现在使用 CandidateFilter 的 source limits。

## 5–6. 新 resolution ladder / evidence types

```text
A trusted discovery metadata
B candidate/dtK media URL already present
C remote media ffprobe
D controlled materialization into cache
E local ffprobe
F bounded decode fallback
```

`ffprobe` 信号：`format.duration`、video stream duration、audio stream duration、
`nb_frames / fps` frame-derived。只有输入内部一致时才使用 derived duration。

方法标记：`metadata`、`remote_format`、`remote_stream`、`local_format`、
`local_stream`、`frame_derived`、`bounded_decode`。

## 7. Bounded decode

`MediaToolkit.measure_bounded_duration()` 使用 FFmpeg progress pipe，最多解析
`duration_resolve_decode_seconds`（默认 30s）。短流结束 → 使用观测 timestamp；
到达 probe bound 且 bound 小于 source max → `duration_unknown_unresolved`；
不会解码任意长度视频。

## 8–10. Media 有效性 / unresolved / retry 语义

* `invalid_media_source`：HTML/JSON/error payload、zero-byte、corrupt container、
  无 video stream。
* `media_unreachable`：没有可解析的 media URL / 网络不可达。
* `duration_unknown_unresolved`：媒体可达但没有可靠 duration。
* 三者都不是 `duration_out_of_range`；只有 measured duration 才能产生 range verdict。
* duration unresolved 存储为 `status=failed_media`，使用独立配置
  `duration_resolution_retry_hours: 6.0`，不占用 30-day content rejection cooldown。
* 30-day content rejection、24h provider retry、already_processed 语义未改。

## 11–13. Download 预算、cache 复用、audit

controlled materialization：

* 消耗 `stats.downloads` / download budget（`probe_downloads` 单独记录）；
* 记录 `probe_bytes`、`probe_latency_ms`；
* 有效 media 保留在 `cache/<platform>_<id>.mp4`；
* preview 与后续 acquisition 通过 `prefetched_path` 复用同一文件；
  `_stage_source()` 检测到缓存后直接返回，不再二次下载；
* invalid/unresolved payload 立即删除，task 结束仍会清理 cache。

每次 resolution 写 `maintenance_log.duration_resolution`，包含 method、state、
duration、error_category、latency、media host（仅 host，不保存 signed URL/query/token）。

## 14–15. Tests / pytest

| 项 | 值 |
| --- | --- |
| 新增 M9.6 测试 | **18** |
| 全量收集 | **781 tests collected** |
| 全量运行 | exit code 0；0 failures；4 个既有 opt-in skip |
| 预期结果 | 777 passed / 4 skipped |

覆盖：metadata/remote format/remote stream/local format/local stream/frame-derived、
bounded decode short/over-bound、timeout、HTTP failure、HTML、corrupt video、
valid-but-unresolved、source range、download 预算、cache 复用、same-task no retry、
later-task retry、content/provider retry 不变、audit 不含 signed credential、cache cleanup。

`python app.py --check`：通过。

## 16–20. 五源 before/after 结果

```text
resolved count       5 / 5
invalid-media count  0
unreachable count    0
still-unresolved     0
```

五个 source 均从 `duration_unknown_unresolved` 变为 `duration_unknown_resolved`
（read-only recheck，未修改历史 source 状态）。

## 21–30. 真实 M9.6 production acceptance

Novelty-aware planner 未修改，重新执行 `--production-gaps` 后选择：

| 项 | 值 |
| --- | --- |
| category / stage | **红薯干 / drying** |
| coverage priority | 5.60 |
| actionability | 1.00 |
| effective priority | **5.60** |
| plan | #17 `M9.6 红薯干 duration hardening` |
| budgets | target 1 / candidates ≤8 / previews ≤8 / downloads ≤4 / tokens ≤60,000 |
| queries | 8/8（4 primary + 4 reserve） |

真实 run：

| 指标 | 结果 |
| --- | --- |
| candidates | 8 |
| current-run unique | 8 |
| new-to-system | **4** |
| known-source | 4 |
| duration resolved (new candidates) | 4/4 via remote probe (format) |
| durations | 35.109s / 50.736s / 60.309s / 91.882s |
| candidates reaching preview | **4** |
| downloads | 0 |
| clips | 0 |
| qualifying / off-target | 0 / 0 |
| tokens | 0 |
| AI calls | 4 |

四个 new-to-system candidate 的 duration 全部解析成功并进入 preview_filter；preview
verdict 被 Qwen HTTP 403（free quota exhausted）阻断，记录为 provider failure，
不是 content rejection，也不是 duration failure。

**M9.6 duration-resolution acceptance: PASS**（new candidate → duration resolved →
reaches preview_filter）。content verdict/clip 仍受 Qwen billing/quota 限制。

## 31. Cleanup routing

没有新 clip 产生，因此 cleanup attempts = 0；没有新的 pending review。
现有 review 状态保持 #26 approved、#22 rejected、#23 failed_quality。

## 32–34. Health / production-ready / cache

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

## 35. Remaining limitations

* Qwen preview provider 当前返回 HTTP 403（free quota exhausted），因此本 run
  无法产生内容 verdict 或 clip；这不是 duration/media-resolution 失败。
* Bounded decode 默认上限 30s；超过该 probe bound 且 ffprobe 无 duration 时仍会
  诚实地 unresolved，不会放宽 duration safety gate。
* controlled materialization 会消耗 download budget；这是有意设计，避免把
  duration probe 变成隐藏的免费下载。
* 未实现自动混剪、subtitle_cleanup_v2、generative inpainting、scheduler、
  concurrency 或新平台。
