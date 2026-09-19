# 工业烘干短视频素材库 Agent

> 人员交接请先阅读 [HANDOFF.md](HANDOFF.md)；其中包含当前验收状态、非 Git 资产、
> 新电脑恢复顺序、付费 API 边界和双方确认清单。

输入一个工业烘干物料（苹果干 / 香蕉干 / 辣椒 / 药材 ...），系统自动完成
视频探测、抽帧、AI 预筛、有效时间段识别、镜头边界校准、真实切片、结构化标签
并写入本地素材库。

新电脑建议把仓库放到非系统盘后运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_portable_workstation.ps1 -LibraryRoot "D:\素材库2"
```

脚本在项目目录内创建环境，补齐 `.env` 中缺失的便携路径配置，但不会覆盖已有密钥。

迁移电脑前，把不会进入 Git 的运行状态备份到另一个非系统盘：

```powershell
# 先预演；不会复制文件
powershell -ExecutionPolicy Bypass -File .\scripts\backup_runtime_state.ps1 `
  -DestinationRoot "G:\fohe-dy-backups" -DryRun

# 正式备份 .env、数据库、config.yaml 和抖音浏览器登录态
powershell -ExecutionPolicy Bypass -File .\scripts\backup_runtime_state.ps1 `
  -DestinationRoot "G:\fohe-dy-backups"
```

每个快照包含逐文件 SHA-256 清单。快照内含密钥和登录态，禁止提交 Git 或公开分享。

在新电脑初始化代码环境后，先预览并校验快照，再显式恢复：

```powershell
# 只校验 SHA-256 和显示恢复范围
powershell -ExecutionPolicy Bypass -File .\scripts\restore_runtime_state.ps1 `
  -SnapshotRoot "G:\fohe-dy-backups\fohe-dy-runtime-YYYYMMDD-HHMMSS"

# 确认后恢复；覆盖前会在 data/restore-rollbacks 中保存本机副本
powershell -ExecutionPolicy Bypass -File .\scripts\restore_runtime_state.ps1 `
  -SnapshotRoot "G:\fohe-dy-backups\fohe-dy-runtime-YYYYMMDD-HHMMSS" -Apply
```

恢复时必须关闭正在运行的 Agent、浏览器自动化和抖音后端，避免数据库或浏览器文件被占用。

**当前版本：Milestone 9.8+ —— 自动采集、生产计划、Qwen 视觉分析、真实 FFmpeg 切片与火山引擎去字幕**

```text
抖音关键词（或本地 MP4 / MOV / M4V）
  -> 关键词优先级排序
  -> 发现：dtk 关键词接口（若实例提供）→ Playwright 浏览器搜索 → 归档/作者/合集/手工链接
  -> 全局候选去重（platform_video_id / URL）+ 本地元数据预筛
  -> 远程抽帧预览（不下载完整视频）
  -> Qwen 预筛（物料相关性 / 字幕复杂度 / 画面质量）
  -> 只下载通过预筛的视频
  -> FFprobe 真实元数据（时长 / 分辨率 / fps / 编码 / 是否有音轨）
  -> AI 有效时间段识别（只分析通过预筛的视频）
  -> 时间戳校验（越界 / 倒序 / NaN 一律丢弃或裁剪）
  -> PySceneDetect 镜头边界校准（可配置最大调整量）
  -> FFmpeg 真实切片（libx264 + CRF，默认精切；保留原始分辨率）
  -> 缩略图生成（多候选帧 + 黑帧/模糊规避）
  -> AI 片段标签化（结构化 JSON + Pydantic 校验）
  -> 去重（SHA256 + 代表帧 pHash；content_key 仅作相似度分组）
  -> SQLite 入库（含来源溯源 + ai_runs 审计 + search_yields 产出统计）
  -> 保存到 D:/素材库2/<物料>/{clips,thumbnails}
  -> Gradio 展示结果与调试信息
```

抖音采集通过**独立部署的** [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)
（dtk v5）后端完成，本项目只通过 HTTP 消费它的公开 API：安装与接口细节见
[docs/douyin_backend.md](docs/douyin_backend.md)。
关键词搜索由 Playwright 在公开搜索页完成（不破解验证码、不绕过登录），
细节见 [docs/douyin_browser_search.md](docs/douyin_browser_search.md)。

## 1. 快速开始

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt

python app.py                     # 启动 Gradio Web UI (http://127.0.0.1:7860)
python app.py --check             # 环境自检
python app.py --demo              # 离线 mock 冒烟测试

# 真实本地视频 + 真实 FFmpeg + 真实 AI
python app.py --local-video "D:/test/apple_drying.mp4" --material "苹果干" --target 5
python app.py --local-video "D:/test/" --material "苹果干" --target 5 --provider qwen
python app.py --local-video "D:/test/a.mp4" --provider mock   # 只验证 FFmpeg 链路

# 真实抖音采集（需要先自建后端，见 docs/douyin_backend.md）
python app.py --check-config                        # 打印生效配置（不含任何密钥）
python app.py --check-douyin                        # 后端连通性 / 鉴权 / 能力
python app.py --check-douyin-browser                # 浏览器搜索状态（含登录/验证墙判定）
python app.py --init-douyin-browser                 # 打开持久化浏览器：人工登录/验证一次
python app.py --douyin-search "苹果干烘干" --target 2   # 小规模真实验证
python app.py --douyin-url "https://www.douyin.com/video/..." --material "苹果干"
python app.py --resume-task 12 --target 20          # 断点续采

# 素材库维护 / 打标质量（Milestone 3.7）
python app.py --list-demo-clips                     # 按数据判定 mock/local/真实抖音
python app.py --remove-demo-clips                   # 只删可证明是占位片段的记录（dry run）
python app.py --remove-demo-clips --yes             # 真正删除（真实抖音片段永不删除）
python app.py --tag-report                          # 打标质量清单（无 AI 调用）
python app.py --retag-clip 21 --provider qwen --yes # 单片段重新打标（保留视频文件）
python app.py --ab-tagging --clips 21,22,23 --yes   # v1 vs v2 对比（不修改生产标签）
python app.py --backfill-clip-metadata --yes        # 只给历史片段补齐空分类/来源

# 素材库管理 / 检索 / 审核 / 导出（Milestone 4）
python app.py                                       # 打开 素材采集 / 素材库 / 任务记录 / 系统检查
python app.py --check-library                       # 只读检查：缺失文件、孤立文件、统计
python app.py --list-clips --provenance douyin_real # 按筛选条件列出素材
python app.py --list-clips --material 苹果 --min-overall-score 0.8
python app.py --export-clips --clips 21,22,23 --export-format both
python app.py --export-clips --provenance douyin_real --export-format csv

# 素材覆盖 / 采集策略 / 运维（Milestone 5）
python app.py --coverage-report "苹果干"        # 工序/镜头/状态/审核覆盖 + 质量分布
python app.py --coverage-gaps "苹果干"          # 缺口 + 确定性补采搜索词
python app.py --search-yield-report             # 搜索词产出排名 + AI 成本归属
python app.py --review-export                   # 导出审核 CSV（clip_id/状态/备注/收藏）
python app.py --review-import review.csv        # 审核 CSV 导入（默认 dry run）
python app.py --review-import review.csv --yes  # 确认导入（只改审核字段）
python app.py --repair-thumbnails --yes         # 缺失缩略图重建（不动视频）
python app.py --quarantine-orphans --yes        # 孤立文件移入 quarantine/（永不删除）
python app.py --maintenance-log                 # 维护审计日志
python app.py --preset-save "苹果干-无人物高分" --library-category 苹果干 --people false --min-overall-score 0.85
python app.py --preset-list
```

`--resume-task` 会在启动任何采集后端前校验原任务：任务必须存在，且素材、
片段时长范围和字幕策略必须与原任务一致；可以调高 `--target` 继续补齐。
校验失败时不会访问外部服务，也不会改写原任务状态。
每个完整遍历候选列表的搜索词会在 `search_yields.stop_reason` 写入 `completed`；
同一任务恢复时会跳过这些检查点，但达到目标、预算、取消或服务故障中断的搜索词
仍会重试。如果整轮搜索词都已完成，再次恢复会刷新整轮，以发现后来新发布的视频。
正在运行的任务会记录所属进程 PID。程序重启时，只有所属进程已不存在的
`running` 任务才会自动改为可恢复的 `partial`，同时释放中断的预览、下载和分析状态。
它不会自动续采，也不会干扰另一个仍在正常采集的进程。
新任务会把不含密钥的完整请求保存到 `tasks.request_json`。因此只需运行
`python app.py --resume-task 12`，素材、搜索词、来源、分类、工序目标和后端选择会自动恢复。
可显式传入 `--target 20` 调高目标；素材库路径默认使用当前电脑配置，不照搬旧电脑路径。
旧任务没有请求快照时仍使用原有参数校验流程。

**物理分类 vs 语义标签（Milestone 3.7）**：任务里的 `library_category`
（默认等于 `material`，可用 `--library-category` 显式指定）决定文件落在
`D:/素材库2/<library_category>/`；AI 观察到的是语义标签
（`material` / `material_form` / `material_state` / `process_stage`），两者互不覆盖。
`material_state` **不会**因为任务搜索的是“苹果干”就被强制写成 `dried`。

采集前的**后端预检**（Milestone 3.6 §6）：`--douyin-search` / `--douyin-url`
会先解析并探测后端；如果配置里写着的 `127.0.0.1:8000` 根本没有 dtk 在跑，
任务会在开始前以 `backend_blocked` 结束，**不会**把 11 个关键词逐个撞死。
解析顺序：配置/环境变量里的地址 → `sources.douyin.fallback_base_urls`
（或 `DOUYIN_BACKEND_FALLBACK_URLS`）→ 报告 `backend_blocked` 与具体原因。

浏览器搜索依赖 Playwright + Chromium：

```bash
pip install playwright
python -m playwright install chromium
```

`browser_search.headless` 默认 `false`：首次需要一个可见窗口让操作者完成正常的
登录或滑块验证；会话保存在 `browser_data/douyin`，之后可复用。程序不会读取、
记录或导出任何 Cookie。

`browser_search.browser_channel`（默认 `auto`）决定用哪个浏览器引擎驱动这个
隔离的持久化配置目录：`auto` = 已安装的 Chrome → Edge → Playwright 自带
Chromium；也可显式写 `chrome` / `msedge` / `chromium`，或用
`browser_executable_path` 指定可执行文件。`--init-douyin-browser`、
`--check-douyin-browser` 与真实搜索走同一套解析逻辑（同一 profile / channel /
executable / headless / locale），诊断里会打印实际使用的通道。这只是浏览器
会话一致性，**不是**指纹伪装、stealth 插件、验证码绕过或代理轮换。

搜索页使用视频标签页（`/search/<关键词>?type=video`）；若该标签页没有结果且
没有出现验证/登录墙，会再尝试普通搜索页一次。连续
`browser_search.empty_result_limit`（默认 3）次“成功但 0 结果”后，本次任务
不再驱动浏览器——这是产出保护，任务仍会如实报告“搜索正常执行，但 0 个结果”，
不会被标成 `discovery_blocked`。

常用参数：`--min-duration` / `--max-duration` / `--subtitle-policy` /
`--output-dir` / `--source` / `--provider` / `--media` / `--port`。

素材库默认输出目录 `D:/素材库2`；SQLite 索引固定在项目内
`data/library.db`；临时文件固定在项目内 `cache/`。

## 2. 目录结构

```text
.
├── app.py                       # CLI + 入口：UI / --check / --demo / --local-video
├── config.yaml                  # storage / sources / ai / analysis / media / network
├── .env.example                 # 密钥模板（复制为 .env）
├── core/
│   ├── models.py                # Pydantic 模型与固定枚举
│   ├── config.py                # 配置加载、路径锚定、日志
│   ├── keyword_expander.py      # 规则模板关键词扩展
│   ├── dependencies.py          # 依赖装配（local/mock/douyin、qwen/volcano/mock、ffmpeg/mock）
│   ├── orchestrator.py          # 采集工作流编排
│   └── task_runner.py           # 任务入口、状态、取消、审计回调
├── sources/
│   ├── base.py                  # VideoSource 抽象接口
│   ├── douyin_backend.py        # dtk v5 HTTP 客户端（信封/鉴权/异步任务/限流）
│   ├── douyin_browser_search.py # Playwright 浏览器搜索发现（无验证码绕过）
│   ├── douyin_models.py         # 上游 Content -> 本项目模型 的归一化
│   ├── douyin_search.py         # 发现后端抽象：关键词(可选)/归档/作者/合集/手工链接
│   ├── local.py                 # LocalFileSource（真实本地视频，Milestone 2）
│   ├── mock.py                  # MockVideoSource（离线 mock）
│   └── douyin.py / bilibili.py / tiktok.py / xiaohongshu.py
├── ai/
│   ├── base.py                  # VisionProvider 接口、UsageInfo、AuditContext
│   ├── gateway.py               # 超时 / 重试 / 降级 / schema 校验 / 审计
│   ├── openai_compat.py         # OpenAI 兼容多模态 HTTP 客户端（Qwen + Volcano 共用）
│   ├── qwen.py                  # 真实 Qwen Provider
│   ├── volcano.py               # 真实 Volcano Provider（fallback）
│   ├── mock.py                  # MockVisionProvider
│   ├── audit.py                 # ai_runs 记录与密钥脱敏
│   ├── schemas.py               # JSON 解析、schema 指令、prompt 版本
│   └── prompts/*.txt            # preview_filter / segment_detection / clip_tagging
├── analyzers/
│   ├── candidate_filter.py      # 本地规则过滤（AI 之前）
│   ├── preview_filter.py        # AI 预筛 + 字幕策略 + 无帧拒绝
│   ├── video_analyzer.py        # 时间段识别 + 时间戳校验 + 按镜头边界切分
│   ├── scene_refiner.py         # PySceneDetect 校准（兼容 0.6/0.7 API）
│   └── quality_gate.py          # 最终质量闸门
├── media/
│   ├── ffmpeg.py                # MediaToolkit 抽象 + 真实 FFmpegToolkit + Mock
│   ├── downloader.py            # HttpDownloader / LocalFileDownloader / MockDownloader
│   ├── frame_sampler.py         # 抽帧策略（uniform / uniform_edges / dense_start）
│   └── clipper.py               # 切片 + 缩略图选择 + SHA256/pHash
├── storage/
│   ├── schema.py                # SQLite DDL + 列迁移（schema v2）
│   ├── database.py              # sqlite3 访问层 + 迁移执行
│   ├── models.py                # row <-> Pydantic 映射
│   ├── library.py               # 素材库读写、条件查询、ai_runs 统计
│   └── dedup.py                 # SHA256 / pHash 硬去重 + 相似度分组
├── ui/gradio_app.py             # Gradio 界面（含调试面板）
├── docs/douyin_backend.md       # 抖音后端部署、接口契约与常见错误
├── docs/douyin_browser_search.md# 浏览器搜索：状态机、人工登录/验证、选择器策略
└── tests/                       # 280+ 测试（含真实 FFmpeg / 真实 PySceneDetect）
```

## 3. 关键接口

```python
class VideoSource(ABC):                       # sources/base.py
    search_mode: str = "keyword"              # "collection" = 本地文件集合
    async def search(self, query: str, limit: int) -> list[VideoCandidate]: ...
    async def get_video_info(self, video_id: str) -> VideoInfo: ...
    async def get_preview(self, video_id: str) -> PreviewSource: ...
    async def get_download_url(self, video_id: str) -> str: ...

class VisionProvider(ABC):                    # ai/base.py
    async def preview_filter(self, request: PreviewFilterRequest) -> ...: ...
    async def detect_segments(self, request: SegmentDetectionRequest) -> ...: ...
    async def tag_clip(self, request: ClipTaggingRequest) -> ...: ...
    def model_for(self, operation: str) -> str: ...
    def consume_usage(self) -> UsageInfo | None: ...

class MediaToolkit(ABC):                      # media/ffmpeg.py
    async def probe(self, path) -> MediaInfo: ...
    async def extract_frames(self, video, timestamps, out_dir, prefix, size=None) -> list[Path]: ...
    async def cut_clip(self, source, start, end, dest, *, reencode, encode_settings, has_audio): ...
    async def make_thumbnail(self, source, timestamp, dest) -> Path: ...
    async def extract_representative_frame(self, video, timestamp, dest) -> Path: ...
    def perceptual_hash(self, image_path, signature=None) -> str | None: ...
```

核心编排代码只依赖这三个接口，具体实现由 `core/dependencies.py` 注入。

## 4. 配置要点

```yaml
storage:
  library_root: "D:/素材库2"      # 绝对路径原样使用，相对路径相对项目根
  database_path: "./data/library.db"
  cache_root: "./cache"

sources:
  active_source: "douyin"         # douyin | local | mock
  douyin:
    backend: "dtk"
    base_url: "http://127.0.0.1:8000"   # 自建后端地址，不硬编码在业务代码里
    fallback_base_urls: []              # 主地址不可用时的备用后端（按顺序探测）
    api_key_env: "DOUYIN_BACKEND_API_KEY"
    request_timeout_seconds: 30
    task_wait_seconds: 20               # ?wait=，上限 30
    task_poll_interval_seconds: 1
    search_page_size: 20
    max_search_pages_per_query: 3
    max_candidates_per_query: 50
    concurrent_requests: 2              # 保守并发
    enable_remote_preview: true         # 远程抽帧预览，不先下载整片
    media_url_refresh_attempts: 1       # 直链过期后刷新一次
    retry_rejected_after_days: 30
    retry_failed_after_hours: 24
    discovery:                          # 上游无关键词搜索时的受支持发现方式
      author_sec_uids: []
      mix_ids: []
      archive_search: true
      manual_urls: []
    browser_search:
      enabled: true
      profile_dir: "./browser_data/douyin"
      browser_channel: "auto"           # auto | chromium | chrome | msedge
      browser_executable_path: ""       # 可选：显式可执行文件（存在时优先）
      headless: false
      locale: "zh-CN"
      empty_result_limit: 3             # 连续 0 结果后跳过剩余关键词（非阻断）
      upstream_retry_count: 2           # 502/503/504 有界重试

ai:
  active_provider: "qwen"         # qwen | volcano | mock
  fallback_provider: null         # 例如 "volcano"
  qwen:
    preview_model: "qwen3-vl-flash-2025-10-15"
    analysis_model: ""            # 留空则用 QWEN_VISION_MODEL 或默认值
    fallback_model: ""            # 低置信度时升级用的模型
  limits:
    max_preview_frames: 16
    max_ai_calls_per_source: 5
    max_retries: 2
    confidence_escalation_threshold: 0.70

analysis:
  sampling_strategy: "uniform"
  preview_max_frames: 16
  preview_max_width: 640
  analysis_max_frames: 12
  scene_boundary_max_adjustment_seconds: 1.5

media:
  backend: "ffmpeg"               # ffmpeg | mock | auto
  ffmpeg_path: ""                 # 留空则从 PATH 查找
  ffprobe_path: ""
  video_codec: "libx264"
  crf: 18
  preset: "medium"
  audio_codec: "aac"
  audio_bitrate: "128k"
  precise_cut: true

network:
  timeout_seconds: 60
  max_retries: 2

collection:                       # 采集预算（防止失控）
  max_candidates_per_task: 200
  max_source_downloads_per_task: 50
  max_ai_calls_per_task: 300
  max_task_runtime_minutes: 60
  initial_candidate_multiplier: 5
  stop_when_target_reached: true

debug:
  keep_source_videos: false       # 设 true 才保留 cache/ 里的源视频

performance:
  douyin_concurrency: 2
  downloads: 2
  ai_analysis: 2
```

## 5. AI 调用与审计

* Qwen 为主，Volcano 为备用。仅在**服务级失败**（超时 / 429 / 5xx / 无效响应）
  或配置的低置信度时降级，语义上的低分不会触发降级。
* 所有调用都会写入 SQLite `ai_runs`：provider、model、operation、
  prompt_version、耗时、帧数、token 数、状态、脱敏后的错误信息、结果 JSON。
* 密钥只从 `.env` 读取，绝不入库、绝不打印。
* 每个源视频的 AI 调用次数受 `ai.limits.max_ai_calls_per_source` 约束。

```bash
python -c "import sqlite3;c=sqlite3.connect('data/library.db');print(c.execute('select provider,model,operation,status,total_tokens from ai_runs').fetchall())"
```

## 6. 素材库查询（为后续自动混剪准备）

```python
from core.models import ClipQuery, ProcessStage, SubtitleType
from storage.database import Database
from storage.library import MaterialLibrary

library = MaterialLibrary(Database("data/library.db"), "D:/素材库2")
clips = library.query_clips(ClipQuery(
    material="苹果",
    process_stage=ProcessStage.DRYING,
    people=False,
    subtitle_type=[SubtitleType.NONE, SubtitleType.BOTTOM_SIMPLE],
    min_overall_score=0.80,
    limit=50,
))
```

## 7. 去重规则（Milestone 2 修正）

* 硬去重：**SHA256** 与 **代表帧 pHash**（阈值 `dedup.phash_max_distance`）。
* `source_video_id` 只用于避免重复分析同一个源视频；**同一个源视频可以产出多个片段**。
* `content_key` 不再作为硬去重条件，只用于相似度分组与排序
  （`dedup.use_content_key_as_duplicate: true` 可显式改回旧行为）。

## 8. 缓存与文件安全

* 用户原始视频不会被移动、修改或删除；本地文件会**复制**到 `cache/` 处理。
* 处理结束后删除：暂存副本、抽帧、缩略图候选等临时文件。
* `D:/素材库2` 下的成品片段与缩略图永不自动删除。

## 8.1 物料归一化与拒绝原因优先级（Milestone 3.6）

**物料本体（§18）**：用户输入/素材库目录保持原样（`D:/素材库2/苹果干`），
结构化标签统一为 `material = 苹果` + `material_state = dried`（正在烘干的画面
则是 `drying`，`苹果片` → `material = 苹果`）。规则集中在
`core/normalization.py`，mock、Qwen 与离线兜底标签走同一条路径，不再出现
`material = 苹果干` 与 `material = 苹果` 混用。

**拒绝原因优先级（§1/§20）**：

```text
内容/媒体终局结论（no_material、subtitle_too_complex、processed …）
  >  重复发现记账（duplicate_video / duplicate_url / already_processed）
```

同一个视频被另一个关键词再次发现时，只合并 `matched_queries`，**不会**覆盖已
存库的结论，也不会再写一条“本地淘汰 [duplicate_video]”，更不会重复调用 AI
预览。真正的重新判定（例如过了重试窗口后再处理）仍然会覆盖旧结论。

## 8.2 打标质量、成本与安全清理（Milestone 3.7）

**片段标注只看成片本身**。`clip_tagging_v2`（`ai/prompts/clip_tagging_v2.txt`）
去掉了 v1 里那段可被逐字复制的苹果示例与固定分数向量，改为：

```text
观察画面 → 基础物料 → 形态 → 生命周期状态 → 工序 → 设备 → 人物/字幕/运镜 → 打分 → JSON
```

* 任务意图只作为背景（`requested_material`），不能决定画面结论；
  片段只含 `material_form=whole`（如“苹果烘干机”口播）时不会被硬套成“铺盘切片”。
* 打标读取**最终成片**按 20/40/60/80% 抽取的 4 张帧
  （`analysis.clip_tagging_max_frames` / `clip_frame_ratios`），与缩略图选取
  共用同一次抽帧（每个时间点只抽一次）；送给模型的帧会按
  `analysis.preview_max_width`（默认 640）缩放以减少图片 token，
  交付缩略图仍按原始分辨率单独重抽一次。
* 描述/分数逐字相同只是**诊断信号**，会在报告与日志里标记，绝不因此淘汰片段。
* `ai_runs`：`preview_filter` / `segment_detection` 记录 `source_video_id`、
  `clip_id` 为 NULL；`clip_tagging` 在片段入库后回填真实 `clip_id`。
  另外记录 `origin`（`pipeline` / `retag` / `evaluation`），
  任务级成本统计只计 `pipeline`，重打标与 A/B 评估单独可见。
* 成本：预览帧数按视频时长自适应（`analysis.preview_frame_bands`），
  报告里给出各操作的 calls/tokens/平均时长与 **tokens per saved clip**。
* 安全清理：`--list-demo-clips` / `--remove-demo-clips` 依据
  `provenance`（mock / local_test / douyin_real）与体积/分辨率证据，
  只删除可证明是占位片的片段；真实抖音片段永不删除。
* 单片段重打标：`--retag-clip <id>` 只更新语义标签与 `tag_prompt_version`，
  视频、缩略图、哈希、provenance、创建时间与审计历史都保留。

## 8.3 素材库管理（Milestone 4）

UI 现在分为四个标签页：**素材采集 / 素材库 / 任务记录 / 系统检查**。
采集功能保持原样，新功能集中在素材库：

* **筛选**：素材分类（library_category）、识别物料、形态、状态、工序、设备可见、
  景别、运镜、人物角色、剪辑角色、字幕类型（多选）、来源类型（provenance）、
  打标 prompt 版本、审核状态（多选）、收藏、关键词（描述/场景/标题/作者）、
  总分区间、时长区间、入库时间区间。留空 = 不限制。
* **分页/排序**：LIMIT/OFFSET 服务端分页（每页 20/50/100），排序走白名单
  （最新入库 / 最早入库 / 综合评分高低 / 时长短长 / 物料相关度 / 字幕洁净度）。
* **业务分类 vs 视觉标签**：`素材分类 (library_category)` 与
  `识别物料 (material)` 始终分开显示与筛选，互不覆盖。
* **浏览**：列表用入库时保存的缩略图（不预载 MP4、不重新解码），
  只有选中某条素材时才把该 MP4 载入播放器；播放前校验路径属于 `D:/素材库2`。
* **人工审核**：批准 / 拒绝 / 需要复核 / 清除审核状态 + 审核备注 + 收藏，
  与 AI 标签完全分离；标记"拒绝"不会删除文件。
* **重新识别标签**：复用 `ClipRetagger`（`--retag-clip`），保留视频、缩略图、
  provenance、分类、审核与收藏，写新的 `ai_runs` 行；需要勾选确认。
* **安全删除**：复用 `MaterialLibrary.remove_clip()`，需要输入
  `删除素材 #ID` 才执行；删除前校验路径位于素材库/项目内，来源记录保留。
* **导出**：JSON / CSV 通用素材清单，写入 `E:/Codex/fohe-dy/exports/`，
  超过 `library.max_export_rows`（默认 5000）时要求缩小筛选范围。
* **健康检查**：`--check-library` 只读报告缺失视频/缩略图与孤立文件
  （绝不自动删除或重建）。

## 8.4 素材覆盖与运维（Milestone 5）

新增标签页 **素材覆盖**（与 素材采集 / 素材库 / 任务记录 / 系统检查 并列），
以及只读 CLI 报告：

* **覆盖矩阵**：按 `library_category`（业务分类）统计工序、镜头、物料状态、
  剪辑角色、质量分布与各维度平均分；`material`（识别物料）仍可单独筛选。
* **审核叠加**：每个工序同时给出 `全部` 与 `已批准` 计数；`coverage.count_mode`
  可选择用哪种口径计算缺口（默认 `all`，不会自动排除未审核片段）。
* **缺口与优先级**：`current / target` → `critical`（0）/ `high`（<50%）/
  `medium`（50-99%）/ `healthy`（≥100%），目标来自 `coverage.preferred_process_stages`。
* **补采建议**：按缺口生成**确定性模板**搜索词（不调用 LLM、不自动开始采集），
  例如 `苹果干 烘干机内部`、`苹果片 烘干房内部`。
* **搜索词产出**：候选/唯一/通过预筛/下载/最终片段 + 各种转化率，
  并按「有效产出」排名（`clips*3 + accepted + 转化率*2`），候选多但 0 片段
  的词排后面但仍然可见。
* **AI 成本归属**：只有能被单一搜索词可靠归属的 `ai_runs` 才计入
  `tokens/clip`，其余记为 `unavailable`，不编造归属。
* **筛选预设**：SQLite `filter_presets` 表保存命名筛选组合（只存筛选字段，
  校验后拒绝任何未知键），UI 可保存/载入/删除。
* **审核导入导出**：`--review-export` / `--review-import`，CSV 校验 clip_id、
  状态与收藏值，默认 dry run，导入只改审核字段、不动 AI 标签。
* **安全维护**：`--repair-thumbnails`（缺失缩略图用 FFmpeg 重建，不动视频）、
  `--quarantine-orphans`（孤立文件移动到 `quarantine/`，保留相对路径，
  **永不删除**）；两者默认 dry run，需要 `--yes` 才执行。
* **维护审计**：`maintenance_log` 表记录 operation/target/details/created_at，
  与 `ai_runs` 完全分开。

## 8.5 字幕测量分析（Milestone 6）

字幕复杂度不再只依赖 VLM 的单一标签，而是**本地测量 + 可解释证据**：

```text
抽样帧（复用预览帧 / 成片打标帧）
  → 本地文字检测（RapidOCR/PP-OCR，CPU；缺依赖时退回 OpenCV 区域提议）
  → 归一化文本框 + 画面分区
  → 单帧度量（区域数/覆盖率/分区覆盖率/横幅数/水印面积/推广关键词）
  → 时间度量（文字出现率、底部/中央/多区域/横幅持续比例）
  → 确定性分类 + 洁净度公式
  → 仅在结论不明确时才请 Qwen 做语义裁定（hybrid）
  → SubtitleAnalysisResult（analysis_version=subtitle_analysis_v1）
```

* **分类**：`none / watermark_only / bottom_simple / top_simple / single_region /
  multi_region / colored_block / large_center_text / promotional_overlay /
  dense_text / unknown`。细化规则见
  [analyzers/subtitle_analysis.py](analyzers/subtitle_analysis.py)。
* **洁净度**：`1 - Σ(权重 × 实测值)`，权重与阈值都在 `config.yaml`
  `subtitle_analysis:` 下可调；LLM 不再负责给数值。
* **水印/角标**：小面积角标单独分类，几乎不扣分（不会因为抖音水印淘汰素材）。
* **横幅 vs 底字幕**：只有“够宽（≥70%）且够厚（≥8% 高度）”的持续色块才算
  caption bar；细长的多行底字幕仍然是 `bottom_simple`（真实素材校准结果）。
* **混合判定**：`decision_source = local | hybrid | qwen_fallback | unavailable`；
  本地引擎不可用时自动退回 Qwen 语义判定，采集流程不受影响。
* **缓存**：按 `sha256 + 分析版本 + 阈值签名` 缓存，同一片段不会重复 OCR，
  阈值改动后缓存自动失效。
* **持久化**：`clips.subtitle_analysis_json` / `source_videos.subtitle_analysis_json`；
  测量结果对 `subtitle_type`、`subtitle_score`、`subtitle_cleanliness_score`
  具有权威性（打标提示词不会覆盖测量值）。
* **命令**：

```bash
python app.py --subtitle-report                  # 字幕分类分布 + 洁净度 + 淘汰率
python app.py --subtitle-report "苹果干"
python app.py --analyze-subtitles 21             # 单片段测量（默认只报告）
python app.py --analyze-subtitles 21 --yes       # 写入测量字段
python app.py --analyze-subtitles-all --limit 20 # 批量测量（默认 dry run）
python app.py --analyze-subtitles-all --library-category 苹果干 --limit 5 --yes
```

**明确不做**：不做字幕擦除、不做 inpaint/模糊/裁切去字、不做自动混剪。

## 8.6 采集计划与批量执行（Milestone 7）

新增标签页 **采集计划**，把「覆盖缺口 + 搜索词产出 + 字幕淘汰率 + token 成本」
变成**人工审批后**才执行的采集计划：

```text
覆盖缺口 → 生成草稿计划 → 查看/编辑 → 人工批准 → 受预算限制执行
        → 实时状态 → 暂停/恢复/取消 → 结果回写 → 重新计算覆盖
```

* **计划模型**：`collection_plans` / `collection_plan_items` /
  `collection_plan_tasks` / `collection_plan_events`（schema v8）。
  状态: draft / approved / running / paused / completed / partially_completed /
  cancelled / failed。**草稿不会执行**，没有隐藏的自动运行或定时任务。
* **生成**：`python app.py --create-collection-plan "苹果干"`（`--count-mode approved`
  可改用已批准口径，`--include-healthy` 可为已达标工序建目标）。已达标工序默认跳过。
* **查询排序（透明公式）**：

```text
score = 3.0*历史片段 + 1.0*通过预筛 + 2.0*候选→片段转化率 + 0.5*已批准片段
        - 2.5*字幕淘汰率 - 1.5*重复发现率 - 1.5*min(1, tokens/片段 / 参考值)
```

  历史查询优先，不足时补 2 条确定性模板查询，并标注
  `query_origin = historical | generated_template`（模板词不代表已验证）。
* **预算**：每目标 + 计划两级硬上限（唯一候选 / 预览 / 下载 / tokens / 运行时长），
  各目标预算会被压缩以适配计划上限；token 上限带可配置安全余量，
  到达上限自动 `paused`（`token_budget_exhausted` 等），不会无限采集。
* **执行**：调用**现有**采集管线（每个查询一个 task），不复制 downloader / Qwen /
  FFmpeg；`plan_id → plan_item_id → task_id` 全链路留痕，复用 tasks/search_yields/
  source_videos/clips/ai_runs。
* **目标命中**：只有 `process_stage` 与目标一致的片段才算命中（`qualifying`）；
  其它有效片段照常入库并记为 off-target 价值。不向 Qwen 施加"必须是某工序"的强制提示。
* **暂停/恢复/取消**：协作式（当前任务完成后再停）；暂停/取消会保存状态并保证缓存清理，
  已产出片段与任务记录全部保留。人工验证/后端不可用会暂停并写明原因，不会耗尽剩余查询。
* **崩溃恢复**：进程重启后 `running` 的计划统一规范化为 `paused`
  （`pause_reason = interrupted`），不会静默续跑，由操作者显式恢复。
* **命令**：

```bash
python app.py --create-collection-plan "苹果干"
python app.py --list-collection-plans
python app.py --show-collection-plan 3
python app.py --run-collection-plan 3 --dry-run      # 打印查询/预算/预计 tokens，零外部调用
python app.py --approve-collection-plan 3 --plan-note "验收"
python app.py --run-collection-plan 3                # 真正执行（受预算限制）
python app.py --pause-collection-plan 3
python app.py --resume-collection-plan 3
python app.py --cancel-collection-plan 3
```

**明确不做**：不做自动混剪/编排/时间线/渲染，不做字幕擦除，不做其他平台。

## 8.7 查询排序校准与计划可观测性（Milestone 8）

M8 只做三件事：真实验收（人工验证后恢复）、查询词排序校准、计划执行可观测性。
不新增并发（`max_concurrent_collection_tasks` 保持 1），不新增调度/自动审批。

### query_rank_v2（新计划默认）

`query_rank_v1`（M7 公式）语义已冻结，只用于对比报表；新计划改用 v2：

```text
useful      = 3.0*log1p(最终片段) + 2.0*(片段/候选)
damped      = useful * 唯一率                    # 27 候选 3 唯一 → ×0.11
subtotal    = damped + 0.6*log1p(已批准片段)
              - 2.5*字幕淘汰率
              - 1.5*min(1, tokens每片段/60000)
              - 3.0*min(1, log1p(tokens)/log1p(60000))   # 零片段却花钱
              - 1.0                                        # 有候选但零片段
confidence  = 唯一候选 / (唯一候选 + 5)
score       = subtotal * confidence
```

设计要点（全部来自真实历史数据，不做"调参到某个词第一"）：

* **体量归一化**：历史片段按 `log1p` 计入，重复跑同一查询不再线性抬高分数。
* **重复阻尼**：有用产出乘以唯一率，重复发现率高的查询被强惩罚。
* **样本置信度**：`samples/(samples+k)` 双向收敛——`1 候选 1 片段 100%` 不再压过
  已验证查询，样本很少的差查询也不会被过度惩罚。
* **字幕/成本/零产出**：M6 测量到的字幕淘汰率、tokens/片段、以及"花了 token 却零片段"
  都计入扣分；零片段且从未消耗 token 的查询保持在 0 分附近（信息不足）。
* **人工批准**：已批准片段只作为加分项，未审核占多数时排序依然稳定。

真实生产库上的对比（`--query-ranking-report`）：

```text
查询词          片段 候选 唯一率 字幕淘汰  tokens     v1      v2   名次v1 名次v2
苹果干            5   27  11%      -        0    16.037   0.239      1      3
苹果干烘干         3   21  81%    57%       0     9.572   1.677      2      1
苹果片烘干         2   10  20%     0%    58318    6.471   0.003      3      4
苹果干制作         1    7  14%     0%    36141    2.596  -0.025      5     13
香蕉干            1    1 100%      -        0     6.000   0.680      4      2
苹果热泵烘干        0   37  68%    50%    44071    0.264  -4.305      8     17
苹果干烘干房        0    5 100%    80%     6974   -1.000  -2.707     10     15
```

### 可观测性

* **执行时间线**（`--show-collection-plan` / UI）：由 `collection_plan_events` 渲染
  `创建 → 批准 → 执行 → 查询 i/n → 任务 #id → 暂停原因 → 恢复 → 目标达成 → 结束`。
* **实时状态**：当前目标 / 当前（或下一条）查询 / 候选与预算使用
  （`previews: 3 / 8 [###-------] 38%`）/ 目标命中与非目标片段。
* **暂停原因人话化**：`human_verification_required → 需要人工完成抖音验证`，
  原始代码仍保留在括号里（`backend_unavailable`、`token_budget_exhausted`、
  `download_budget_exhausted`、`preview_budget_exhausted`、`operator`、`interrupted`）。

### 计划归档 / 验收标记（schema v9）

`collection_plans.archived` 与 `test_plan` 两个标记位：归档计划仍可审计、仍在历史里，
但不出现在默认列表且**不可执行**；`test_plan` 用于区分验收/stub 计划与真实采集计划。
M7 遗留的验收计划 #6/#7/#8 已用命令（而非 SQL）标记：

```bash
python app.py --archive-collection-plan 6
python app.py --mark-test-plan 6
python app.py --list-collection-plans --include-archived
python app.py --unarchive-collection-plan 6
```

### 新增命令

```bash
python app.py --query-ranking-report                 # v1 vs v2 对比表
python app.py --explain-query "苹果片烘干"            # 单个查询的分项打分
python app.py --plan-linkage 4                       # plan→item→task→source→clip 链路
python app.py --validate-clip 26                     # 文件/ffprobe/时长/缩略图/来源校验
```

### 人工验证（唯一的机器无法代劳的一步）

**Milestone 8.1 起，权威路径是「同一个浏览器上下文内完成验证」。**
实测结论：`--init-douyin-browser` 完成后关闭窗口再开新进程，
**并不能可靠把验证状态带过去**（持久化 profile 不保证新进程能访问搜索页），
所以不要再走 init → 关闭 → 重开的循环。

交互式验证（同一个 context，不重启浏览器）：

```powershell
cd F:\Codex\fohe-dy
.\.venv\Scripts\python.exe app.py --verify-douyin-browser
# 可选搜索词： --verify-douyin-browser --verify-query "苹果干烘干"
```

行为：启动与真实采集完全相同的持久化 Chrome → 打开搜索页 →
检测到验证页时**保留同一个 context 与 page**，在终端提示后等待人工处理
（可以按 Enter 立即复查，也可以直接在浏览器里完成、程序自动轮询；
`Ctrl+C` 取消并干净关闭，profile 保留）→ 用**同一个** page 复查 →
必须看到真实 `/video/` 链接才判定 `session_usable`。
不做任何验证码破解、代理轮换或指纹伪装。

计划执行也可以在同一个进程里先过验证门再跑（推荐）：

```powershell
.\.venv\Scripts\python.exe app.py --run-collection-plan 10 --interactive-verification
```

它会先交互式确认会话（写事件 `human_verification_completed`），
如果计划原来是 `paused(human_verification_required)` 就转成 `running`（写 `resumed` 事件），
然后用**同一个已通过验证的浏览器会话**执行计划，不再启动第二个浏览器。
`queries_attempted`、预算、任务关联与检查点全部保持不变。

如果验证后仍被拦截，`--check-douyin-browser` 会分别报告
「持久化 profile 是否存在 / 浏览器配置是否一致 / 新导航是否被验证页拦截」，
并指向 `--verify-douyin-browser`；它是非交互诊断，不会等待人工操作。

#### M8.2：验证解除后的「导航竞态」处理

实测（M8.1 真实运行）发现：人工验证完成后，抖音自己会把页面从
`验证码中间页` 跳到 `发现更多精彩视频 - 抖音搜索`，此时如果程序再发一次
`page.goto(搜索页)`，正在进行的 SPA 跳转会被打断并抛
`net::ERR_ABORTED` —— 旧代码把它当成 `douyin_unreachable`，属于误判。
M8.2 的处理：

```text
状态机（互不混淆）:
  verification_required / login_required      人工墙：保持同一 page 等待
  search_pending                              验证已解除、结果尚未渲染（过渡态）
  session_usable(=ok)                         已渲染真实 /video/<id> 链接
  upstream_bad_gateway / upstream_http_error  上游网关 502/503/504
  search_dom_changed / no_results             搜索 UI 已加载但没有可用链接
  douyin_unreachable                          仅在真正不可用时
```

* **先看当前页，再决定是否导航**：如果当前 URL 已经是目标
  `/search/<query>?type=video`（或页面已经渲染出结果），**不再重复 goto**，
  直接等待水合（`search_settle_timeout_seconds`，默认 20s，可配置）。
* **`net::ERR_ABORTED` 可恢复**：不直接判 unreachable，而是等页面自身跳转稳定后，
  用当前 URL / 标题 / 验证标记 / `/video/` 链接重新判定真实状态。
* **空标题视为过渡**：跳转中的空 title 不会结束判定，会继续等待/复查。
* **同一个 context 不变**：整段后处理都用同一个 browser / context / page，不重启。
* 终端提示改为按真实状态输出：验证解除后显示
  `人工验证已解除，正在等待抖音搜索结果加载……`，搜索页已加载但链接未就绪时显示
  `搜索页已加载，等待视频结果渲染……`，遇到自跳转时显示
  `页面正在自行跳转，重新检查当前页面……`。

#### M8.3：用「操作者自己的浏览器」取得真实结果（V3.2 会话模型）

**实测根因**（2026-09-15，同一台机器、同一个 profile、同一个搜索地址）：

| 启动方式 | `navigator.webdriver` | 结果卡片 | `/video/` 链接 | 现象 |
| --- | --- | --- | --- | --- |
| Playwright `launch_persistent_context` | true | 16 个**空壳** `<li>` | **0** | 透明 `#captcha_container` 拦截点击，卡片无文字、无 href、页面无 aweme_id |
| 普通 Chrome 启动 + CDP 接入（同 profile） | false | 16 个**真实**卡片 | **8+** | 卡片带 `//www.douyin.com/video/<id>` 链接，标题/作者正常 |

也就是说：抖音对「被自动化标记的浏览器」只返回空壳结果页，这不是选择器问题，
而是会话被判定不可信。V3.2（`E:\Codex\kb-huny_V3.2_FINAL_20260903`）之所以可行，
是因为它用 `scripts/start_browser.bat` 让**操作者自己启动**浏览器（
`--remote-debugging-port=9222 --user-data-dir=<profile>`，无自动化参数），
再由 MediaCrawler 通过 CDP 接入；V3.2 的搜索本身走的是抖音 Web API + a_bogus 签名
（`media_platform/douyin/client.py::search_info_by_keyword`、`help.get_a_bogus`、
`localStorage.xmst`），**属于私有接口/签名重放，本补丁明确不移植**。

移植过来的只有**会话模型**（公开渲染页 + CDP 接入）：

```powershell
.\.venv\Scripts\python.exe app.py --open-douyin-browser      # 普通方式启动你的 Chrome（调试端口 9222）
#   在窗口里正常登录/验证（程序不代劳、不破解验证码）
.\.venv\Scripts\python.exe app.py --verify-douyin-browser     # CDP 接入同一浏览器并检查真实结果
.\.venv\Scripts\python.exe app.py --run-collection-plan 10 --interactive-verification
```

* 配置：`sources.douyin.browser_search.cdp_url`（留空=自动探测 `cdp_port`，
  也可写 `http://127.0.0.1:9222` 强制接入，或 `off` 强制自己启动浏览器）；
  `cdp_port` 默认 9222。也可用 `--cdp-url` 临时覆盖。
* 接入时**不会关闭操作者的浏览器**（只是断开连接），也不会带任何自动化/隐身参数。
* 计划执行与验证共用**同一个 event loop 与同一个会话**（Playwright 对象不能跨
  `asyncio.run` 复用），且被注入的会话标记为共享，单个 task 结束不会把它拆掉。
* M8.2 的语义全部保留：`search_pending`、`ERR_ABORTED` 恢复、有界稳定化、
  网关 502/503/504 分类、人工验证暂停/恢复、预算与检查点。
* DOM 不一致（卡片存在但没有可用 `/video/` 链接）报 **`search_dom_changed`**，
  终端文案为「抖音验证已通过，但当前搜索结果 DOM 暂未识别」，不再说成被验证拦截。
* 诊断脚本（只读、公开页面）：
  `scripts/diag_douyin_dom.py`（自己启动浏览器时的 DOM 快照）、
  `scripts/diag_douyin_cdp.py`（普通启动 + CDP 的快照），
  结果写在 `logs/douyin-*-dom-<时间戳>.json`（不含 Cookie/Token）。

`--init-douyin-browser` 仍保留（首次登录/初始化 profile 用），但请注意：

```text
它只是一次性的 profile 初始化，不保证后续新进程能访问搜索页。
权威判据是「人工验证后，在同一个上下文里真实搜索到 /video/ 结果」。
```

抖音出现滑块/二维码验证时，计划会 `paused(human_verification_required)`；
按上面的交互式流程处理即可（旧写法
「init → 关闭窗口 → 再 `--resume`/`--run`」**已不再推荐**，因为它依赖 profile
把验证带过进程重启，实测不可靠）。
系统不会做任何验证码破解、代理轮换或指纹伪装。

## 8.8 覆盖驱动的生产采集（Milestone 9）

把「覆盖缺口 → 目标查询 → 人工批准 → 有预算的真实采集 → 命中/非命中统计 →
覆盖重算」串成一条可操作的生产闭环。它不是新管线：仍复用 coverage / planner /
plan_runner / query_rank_v2 / CDP 浏览器 / 远端 dtk / RapidOCR /
`qwen3-vl-plus-2025-09-23` / FFmpeg / `clip_tagging_v2` / SQLite。

```bash
python app.py --production-gaps                       # 按优先级列出真实缺口
python app.py --production-gaps --category 辣椒干 --stage drying
python app.py --create-production-plan --category 辣椒干 --stage drying
#   → 草稿计划（目标查询 + 预算），仍需人工 --approve-collection-plan
python app.py --approve-collection-plan 11 --plan-note "M9 验收"
python app.py --run-collection-plan 11 --interactive-verification
```

* **目标在配置里**：`production_coverage.process_stage_targets` / `stage_priorities`
  / `materials` / 每个目标的候选·下载·token 预算。SQL 与 UI 不硬编码目标。
* **优先级可复核**：`--production-gaps` 打印每一项的分量（工序、缺口、
  edit_role、历史产出、重复率、字幕淘汰、token、二次发现率），公式见
  `core/production.py`。同分按工序序号、分类名稳定排序。
* **目标查询不再被物料展开覆盖**：计划项的网络请求使用
  `TaskRequest.explicit_queries`，一次任务只跑该项的**一个**目标查询；
  实际执行的查询写入 `plan_item.progress.executed_queries` 供审计。
* **三种结果分开统计**：命中片段 / 非命中但有效片段 / 淘汰或执行失败；
  AI 提供商错误记录为 `failed_ai`（按小时重试），**不再**写成内容淘汰，
  统计上计入 `errors`。
* **仍是人工在环**：草稿不会执行；没有调度器、没有并发、没有自动混剪，
  也不做字幕擦除。验收记录见 `docs/m9_acceptance.md`。

## 9. 测试

```bash
python -m pytest -v
```

* 真实 FFmpeg / FFprobe 测试需要可用的 ffmpeg；不可用时自动跳过。
* 真实 PySceneDetect 测试需要 `scenedetect`；不可用时自动跳过。
* 所有外部 HTTP（Qwen / Volcano）在单元测试中都被 mock，不会产生费用。

## 10. 已知限制

* **上游 dtk v5.0.3 没有关键词搜索接口**（83 个路径中无 search、也无 `keyword`
  参数），所以真实关键词发现走 Playwright 浏览器搜索；若连接到的实例将来暴露
  关键词路由，`DtkKeywordSearchBackend` 会自动优先使用。
* **抖音对匿名/未验证客户端设墙**：本机实测搜索页返回结果区为空并带登录面板
  （`login_required`），首页返回验证中间页（`verification_required`）。
  项目不绕过这些限制：请用 `python app.py --init-douyin-browser` 手动完成一次
  登录/验证，持久化 profile 会被后续任务复用。
* 抖音边缘（`kngx`）也会直接返回 **502 Bad Gateway**：这被单独分类为
  `upstream_bad_gateway`（503/504 → `upstream_http_error`），按
  `browser_search.upstream_retry_count` 有界重试后退避，**不会**被误判成
  验证码或登录墙；`--check-douyin-browser` 会打印
  `requested_url / final_url / http_status / page_title / browser_status` 诊断字段。
* 本机没有 Docker/PostgreSQL/Redis，无法本地自建 dtk；真实联网验收使用上游官方
  公开演示实例 `https://demo.douyin.wtf`（v5.0.3，演示凭据 + 会话 Cookie，
  速率受限）。自建方式见 `docs/douyin_backend.md`。
* Volcano 未配置凭据，仅作为可选 fallback 存在。
* 人声/字幕 OCR 未接入，字幕复杂度依赖视觉模型判断。

## 11. 抖音采集的两个阶段（重要）

```text
搜索候选 (searched_candidates)
  -> 唯一候选 (unique_candidates)      # platform_video_id + URL 全局去重
  -> 本地预筛 (已处理过的视频按重试策略跳过)
  -> 远程抽帧 + Qwen 预筛 (prescreened / subtitle_rejected / other_rejected)
  -> 仅下载通过预筛的视频 (downloads)
  -> 深度分析 (analyzed) -> 切片 -> 标签 -> 质量/去重
  -> 最终片段 (clips_saved) 目标达成即停
```

预算（`collection.*`）与目标（`target_clip_count` 表示**最终片段数**，不是视频数）
共同决定任务何时结束；`search_yields` 表记录每个关键词的产出，用于后续优化词库。

### 可靠性（Milestone 3.x）

* **能力探测只做一次**：`/openapi.json` 每个任务只探测一次，成功与失败都缓存；
  后端出现 502/503/504 并有界重试耗尽后，本任务内标记为不可用并**快速失败**，
  不会再为 11 个关键词重复整轮重试。
* **浏览器墙体粘滞**：`verification_required` / `login_required` /
  `upstream_bad_gateway` 一旦出现，本任务内不再重启浏览器逐词重试。
* **发现被阻断 ≠ 0 结果**：所有发现后端都不可用时立即停止搜索并返回
  `discovery_blocked`（CLI/Gradio/任务结果都会显示各后端状态），
  与「搜索正常执行但 0 结果」明确区分。
* **本机后端不走系统代理**：`http://127.0.0.1` / `http://localhost` 默认
  `trust_env=False`（可用 `sources.douyin.trust_env` 显式覆盖）；远程后端保持
  httpx 默认行为，不受影响。
* **Ctrl+C 干净退出**：取消任务 → 关闭 Playwright/HTTP 客户端 → 清理缓存与临时
  文件 → 任务行标记为 `cancelled` → 只打印一行提示，不输出 CPython 对象诊断。

### 真实闭环（Milestone 3.6，2026-09-14 实测）

```text
浏览器搜索 www.douyin.com（视频标签页，真实候选 + 真实标题/作者）
  -> 真实 dtk 内容/播放地址（浏览器只给 URL，元数据由 dtk 补齐）
  -> FFmpeg 远程抽帧 -> 真实 Qwen preview_filter
  -> 仅下载通过预筛的源视频 -> PySceneDetect -> 真实 FFmpeg 精确切割
  -> 真实 Qwen 片段标签 -> pHash/质量门 -> SQLite -> D:/素材库2
```

* 2026 年抖音搜索页的“综合”标签页对自动化会话只渲染空壳（0 个结果、0 次业务
  XHR），因此浏览器搜索固定使用 `?type=video`；普通搜索页作为有界兜底。
* 页面里出现的 `verifycenter` / `nocaptcha` / `captcha` 字符串来自**始终加载**的
  风控 SDK，不再据此判定 `verification_required`：只有渲染出验证文案
  （“请完成安全验证”等）或根本没有搜索 UI 时才判定为实现人机验证。
* 浏览器只发现 URL，缺少时长/标题的候选会先经过真实 dtk 元数据补全，避免把
  “未知时长”误判成 `duration_out_of_range`。

## 12. 下一步（Milestone 4）

1. 在上游具备关键词搜索能力（或自建搜索适配器）后，接入真正的平台搜索。
2. OCR 字幕兜底（PaddleOCR / RapidOCR）与字幕遮挡更精细的判定。
3. 自动混剪：基于 `edit_roles`、工序顺序与分数的片段编排与导出。
4. 素材库检索页（按标签 / 时长 / 分数筛选）与批量任务队列。
5. 其余平台适配器（Bilibili / TikTok / Kuaishou / 小红书）。
