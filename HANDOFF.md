# fohe-dy 项目交接手册

更新日期：2026-09-19（Asia/Shanghai）  
交接分支：`main`  
交接基线：`1d3a1dd5ca48b8caaa938a8c67b2344f8726febb`  
交接仓库：`https://github.com/Kybzl/fohe-dry-dy`（公开、无旧仓库历史的安全快照）

本文件是接手人的首要入口。`README.md` 是完整功能说明，
`docs/current_state.md` 保留迁移过程和历史背景；两者发生冲突时，以本文件、
当前代码和 `python app.py --check-config` 的输出为准。

## 1. 一句话说明

这是一个工业烘干短视频素材采集 Agent：从抖音或本地视频发现候选，使用 Qwen
完成视觉预筛、片段识别和结构化标签，再由 FFmpeg 切片并写入 SQLite 索引与
`D:\素材库2`。系统还提供覆盖驱动采集计划、断点续跑、素材审核、字幕检测，
以及经过本地零费用前置检测后才允许调用的火山引擎 VOD 付费去字幕流程。

## 2. 当前验收状态

交接前在本机实际检查结果：

| 项目 | 当前状态 |
| --- | --- |
| Python | 3.12.14，项目内 `.venv` |
| 数据库 | schema v16，`data/library.db`，约 4 MB |
| 素材库 | `D:\素材库2` |
| 视频完整性 | 9 条数据库记录 / 9 个视频 / 0 缺失 / 0 孤立 |
| 缩略图 | 0 缺失 / 0 孤立 |
| 真实来源 | 9 个均为真实抖音素材 |
| 人工审核 | 9 个待审核，0 个已批准 |
| 字幕清理 | 5 条记录，3 个派生文件，0 个批准产物缺失 |
| 当前 AI | Qwen，`qwen3-vl-flash-2026-01-22` |
| 媒体后端 | FFmpeg / FFprobe，来自项目虚拟环境 |
| 抖音后端 | 首选 `http://127.0.0.1:8000`，另有远端 fallback |
| 浏览器 | Chrome，持久化 profile 位于 `browser_data/douyin` |
| 自动化测试 | 878 项可收集；交接前全量结果为 874 passed / 4 skipped / 0 failed |

交接时不需要继续处理视频，也不要为了验收调用付费 API。

## 3. 已完成功能

- 抖音关键词、明确 URL、本地视频和离线 mock 采集。
- DTK v5 下载后端探测、鉴权、异步任务、限流及失败分类。
- Playwright/Chrome 搜索与人工登录、滑块验证后的同会话继续；不破解验证码。
- Qwen 预筛、片段识别、标签化、调用审计与 token 统计。
- FFprobe、镜头边界校准、FFmpeg 精切、缩略图生成和内容去重。
- SQLite 素材库、溯源、审核、导出、覆盖报告和补采建议。
- 需要人工批准的采集计划、预算上限、暂停/恢复/取消和崩溃恢复。
- 单任务安全恢复：请求快照、PID 所有者、搜索词检查点、自动释放中断来源。
- 本地字幕分析与火山引擎 VOD 去字幕。
- 云去字幕在付费调用前执行本地颜色块/底板检测；云任务成功后直接导出，
  不再做耗时且重复的处理后视觉复检。
- 单次和每日付费任务硬上限，可在配置中调整。
- 跨电脑路径覆盖、工作站初始化、运行状态备份与 SHA-256 校验恢复。

## 4. 不在 Git 中的资产

以下内容必须单独交付或在新电脑重建：

| 内容 | 作用 | 建议交付方式 |
| --- | --- | --- |
| `.env` | API 密钥、后端鉴权和本机路径 | 密码管理器或加密文件，禁止聊天明文发送 |
| `data/library.db` | 素材、任务、审核和云处理索引 | 运行状态快照或加密移动盘 |
| `D:\素材库2` | 最终视频、缩略图和 clean 产物 | 移动盘或受控文件共享，保持目录结构 |
| `browser_data/` | 抖音持久化登录态 | 可不交；建议接手人在新电脑重新登录 |
| `.venv/` | Python 和 FFmpeg 环境 | 不交付，使用脚本在非 C 盘重建 |

本地已有一份通过 manifest 大小和 SHA-256 恢复预演的运行状态快照，
但它包含 `.env` 凭据，因此不在公开仓库中提供路径或下载。是否通过加密渠道
交付由项目所有者决定；接手人也可以使用 `.env.example` 重新配置。

## 5. 接手人在新电脑上的操作

所有程序放在非 C 盘。下面以 `F:\Codex\fohe-dy` 为例：

```powershell
F:
New-Item -ItemType Directory -Force F:\Codex | Out-Null
Set-Location F:\Codex
git clone https://github.com/Kybzl/fohe-dry-dy.git
Set-Location .\fohe-dy

powershell -ExecutionPolicy Bypass -File .\scripts\setup_portable_workstation.ps1 `
  -LibraryRoot "D:\素材库2"
```

如果项目所有者交付了运行状态快照，先关闭 Agent、抖音后端和浏览器，再执行：

```powershell
# 只读校验，不覆盖
powershell -ExecutionPolicy Bypass -File .\scripts\restore_runtime_state.ps1 `
  -SnapshotRoot "X:\安全交付目录\fohe-dy-runtime-YYYYMMDD-HHMMSS"

# 校验通过后显式恢复；覆盖前会创建 data/restore-rollbacks
powershell -ExecutionPolicy Bypass -File .\scripts\restore_runtime_state.ps1 `
  -SnapshotRoot "X:\安全交付目录\fohe-dy-runtime-YYYYMMDD-HHMMSS" -Apply
```

如果不交付浏览器 profile，接手人自行初始化抖音会话：

```powershell
.\.venv\python.exe app.py --init-douyin-browser
```

出现登录或滑块时由接手人手工完成；程序不会自动破解验证码。

## 6. 首次验收顺序

以下命令不调用付费去字幕 API：

```powershell
git status
git log -1 --oneline
.\.venv\python.exe app.py --check-config
.\.venv\python.exe app.py --check
.\.venv\python.exe app.py --check-library
.\.venv\python.exe app.py --check-douyin
.\.venv\python.exe app.py --check-douyin-browser
.\.venv\python.exe app.py --demo
.\.venv\python.exe -m pytest -q
```

通过标准：

- `git status` 干净，HEAD 不早于本文件交接基线。
- 生效素材库路径是接手电脑上的非系统盘目录。
- `--check-library` 不报告缺失或孤立文件。
- Qwen 模型显示为交付时约定模型。
- 抖音后端和浏览器会话可用；若需要重新登录，这是正常迁移步骤。
- 测试没有失败；依赖真实媒体或外部环境的项目允许显示 skip。

## 7. 日常运行

```powershell
# Web UI
.\.venv\python.exe app.py

# 小规模真实采集；可能产生 Qwen 调用费用，但不自动调用火山去字幕
.\.venv\python.exe app.py --douyin-search "香菇烘干实拍" --material "香菇干" --target 2

# 恢复新版本创建的任务；自动加载原始搜索词、来源和后端参数
.\.venv\python.exe app.py --resume-task 12

# 提高原任务目标
.\.venv\python.exe app.py --resume-task 12 --target 20
```

单任务恢复规则：

- 不存在或身份参数不一致时，在连接外部服务前拒绝执行。
- 完整执行的搜索词写入 `completed` 检查点，恢复时跳过。
- 目标、预算、取消或服务故障造成的中断不会被误标完成。
- 进程崩溃留下的 `running` 会变为 `partial`；活进程拥有的任务不会被误改。
- schema v16 的任务保存非密钥请求快照；当前电脑素材库路径覆盖旧电脑路径。

## 8. 付费和外部副作用边界

Qwen 调用可能计费。火山引擎 VOD 上传、处理和存储可能计费，必须遵守：

1. 不把“继续”“下一步”解释为授权付费调用。
2. 每次上传具体视频并创建付费任务前，取得项目所有者对该视频的明确授权。
3. 先执行本地零费用前置检查；存在持久白色/彩色字幕底板时不上传。
4. 使用 `config.yaml` 的 `cloud_cleanup.max_paid_tasks_per_run` 与
   `max_paid_tasks_per_day` 硬上限。
5. 火山成功产物直接保存，不做处理后视觉复检；只做文件结构探测。
6. 临时下载 URL 保持鉴权，只在下载窗口短暂开放外网访问，完成后关闭。

已验收产物示例：

`D:\素材库2\香菇干\clean\shiitake_dbb5a474__subtitle_cleanup_v2_volcengine.mp4`

不要为了交接重复上传该视频或重新创建付费任务。

## 9. 关键配置与凭据

`.env.example` 有中文说明。接手人需要核对这些变量是否由所有者授权：

- `QWEN_API_KEY`、`QWEN_BASE_URL`、`QWEN_VISION_MODEL`
- `DOUYIN_BACKEND_API_KEY`、`DOUYIN_BACKEND_SESSION_COOKIE`
- `VOLCENGINE_ACCESS_KEY_ID`、`VOLCENGINE_SECRET_ACCESS_KEY`
- `VOLCENGINE_VOD_SPACE_NAME`、`VOLCENGINE_VOD_STORAGE_DOMAIN`
- `VOLCENGINE_VOD_URL_AUTH_KEY`
- `FOHE_LIBRARY_ROOT`、`FOHE_DATABASE_PATH`、`FOHE_CACHE_ROOT`
- `FOHE_FFMPEG_PATH`、`FOHE_FFPROBE_PATH`

当前已知安全债务：历史提交和当前 `.env.example` 曾出现真实凭据。项目所有者此前决定
暂不处理，因此交接不擅自轮换；接手人必须知道这是已接受但尚未关闭的风险。

## 10. 代码导航

| 路径 | 职责 |
| --- | --- |
| `app.py` | CLI 分发、诊断与 Web UI 入口 |
| `core/orchestrator.py` | 单次采集主流程和搜索词检查点 |
| `core/task_runner.py` | 任务入口、并发锁、恢复校验与崩溃恢复 |
| `core/plan_runner.py` | 采集计划预算、暂停、恢复和查询执行 |
| `core/cloud_cleanup.py` | 火山去字幕状态机、付费上限和产物下载 |
| `storage/library.py` | SQLite 素材、任务、审核和维护接口 |
| `storage/schema.py` | 数据库 schema 与原地迁移 |
| `sources/douyin*.py` | DTK、浏览器搜索和抖音数据转换 |
| `media/` | 下载、探测、切片、抽帧和云媒体接口 |
| `analyzers/` | 预筛、字幕、画质和镜头分析 |
| `tests/` | 离线回归和少量环境条件测试 |

## 11. 已知限制

- Web UI 的进一步体验优化被明确延后；CLI 和核心流程优先。
- 验证码只能人工完成，系统不提供破解。
- DTK 参考后端通常不提供真正的关键词搜索，关键词发现主要依赖浏览器搜索。
- SQLite 与素材目录必须成对迁移；只复制数据库或只复制视频都会造成完整性异常。
- 浏览器登录态有账号与设备风险，优先让接手人在自己的设备重新登录。
- `.env.example` 的凭据暴露属于尚未处理的安全债务。

## 12. 建议后续顺序

1. 由接手人按第 6 节完成首次验收并记录结果。
2. 决定是否轮换历史泄露凭据，并把 `.env.example` 恢复为纯占位模板。
3. 为采集任务增加正式的进程级互斥锁/租约，进一步强化多进程执行安全。
4. 给运行状态快照增加可选加密封装，降低人工交接凭据的风险。
5. 再恢复 Web UI 工作：展示运行上限、断点状态和付费预算，但不改变核心语义。

## 13. 双方交接确认清单

移交人：

- [ ] 已确认接手人可从公开仓库 clone；如需 push，已单独授予写权限。
- [ ] 已通过私密渠道决定是否交付 `.env`，未在聊天或 Git 中发送密钥。
- [ ] 已交付 `D:\素材库2` 与匹配的 `data/library.db`，或确认从空库开始。
- [ ] 已说明哪些操作会产生 Qwen/火山费用。
- [ ] 已说明验证码必须人工完成。

接手人：

- [ ] 已在非 C 盘完成初始化。
- [ ] 已完成 GitHub 身份验证和 Git 用户配置。
- [ ] 已运行第 6 节全部验收命令。
- [ ] 已确认素材库 9/9 完整，或记录迁移后的实际数量。
- [ ] 已确认 `.env`、数据库和素材目录均已进入自己的备份策略。
- [ ] 已理解付费 API 需要逐次明确授权。

双方确认完成后，建议在仓库 Issue 或内部工单记录：接手人、日期、验收 HEAD、
素材数量、凭据是否轮换、浏览器是否重新登录，以及尚未完成的事项。
