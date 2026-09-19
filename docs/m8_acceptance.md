# Milestone 8 验收记录（真实运行证据）

记录日期：2026-09-15
项目：`E:\Codex\fohe-dy`

本文件只登记**真实运行**产生的证据，不包含任何 Cookie / Token / API Key。

---

## 1. 端到端真实验收（M8.3.2 / M8 final）

命令：

```powershell
.\.venv\Scripts\python.exe app.py --douyin-search "芒果烘干" `
    --material "芒果" --library-category "芒果干" --target 1 `
    --provider qwen --output-dir "D:/素材库2"
```

| 项 | 值 |
| --- | --- |
| task | **#66**（`succeeded`，目标 1 / 保存 1，耗时 196 s） |
| source video | **#643** `7501650921036205350`（`status=processed`） |
| clip | **#27** `D:\素材库2\芒果干\clips\material_c51ac1e3_d3331592.mp4` |
| 发现路径 | real Douyin browser discovery（CDP attach，operator Chrome） → remote dtk（`https://demo.douyin.wtf`） |
| 处理链 | RapidOCR 字幕测量 → Qwen 预筛 → 真实下载 → segment_detection → PySceneDetect → FFmpeg 切片 → 片段终检 OCR → `clip_tagging_v2` → SQLite |
| 搜索产出 | `芒果烘干`: 候选 5 → 唯一 1 → 预筛通过 1 → 下载 1 → 片段 1 |
| AI 调用 | 3 次，全部 `status=ok`，`model=qwen3-vl-plus-2025-09-23` |
| tokens | **24,799**（prompt 24,137 / completion 662），平均延迟 7.4 s |
| 结果 | **1/1 成功**（达到 target=1 后停止） |

### 片段校验（`--validate-clip 27`）

```text
file_exists / inside_library_root / provenance_is_real / thumbnail_exists
ffprobe_readable / duration_in_range / video_stream        → 全部 ok
duration 15.0s | 720x1280 | provenance=douyin_real | tag_prompt_version=clip_tagging_v2
subtitle_analysis_v1 | rapidocr | single_region | cleanliness=0.949 | decision_source=local
ai_runs: #288 preview_filter, #289 segment_detection, #290 clip_tagging (clip_id=27)
```

## 2. 回归测试（最终状态）

```text
python -m pytest -q -p no:cacheprovider    →  626 tests, 622 passed, 4 skipped, 0 failures
python app.py --check-library              →  素材总数 10 | 真实抖音 7 | 缺失 0 | 孤儿 0
cache/                                     →  0 entries
python app.py --check-config               →  preview_filter / segment_detection / clip_tagging
                                              全部 = qwen3-vl-plus-2025-09-23
```

## 3. 本次 M8.3.x 关键修复（供回溯）

| 问题 | 根因 | 修复 |
| --- | --- | --- |
| 新进程只解析到 `127.0.0.1:8000` | 远端 dtk 之前只靠临时环境变量 | `sources.douyin.fallback_base_urls` 配置远端地址（凭据仍只在 `.env`） |
| 搜索页只有空壳卡片、无 `/video/` 链接 | Playwright 启动的浏览器被判定为自动化 | CDP 接入操作者自己启动的 Chrome（`--open-douyin-browser` / `cdp_url`） |
| 同一浏览器 9222 被其他项目抢占导致 `browser_unavailable` | 多项目共用一个调试端口 | 本项目专用端口 `cdp_port: 9230` |
| 换模型不生效 | `preview_model`（config）与 `QWEN_VISION_MODEL`（env）分别驱动不同操作 | 三个操作统一走生产模型，`--check-config` 打印逐操作路由与来源 |
| 非验证类失败被说成“被验证拦截” | CLI 文案 | 按真实状态输出（`浏览器会话不可用（browser_unavailable）` 等） |
