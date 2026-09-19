# 当前状态说明（跨机器开发交接）

> 注意：本文保留了迁移过程的历史快照，其中部分 HEAD、路径和待办已过期。
> 正式人员交接以仓库根目录 [HANDOFF.md](../HANDOFF.md) 为准。

生成时间：2026-09-19（已按新电脑实况更新）
项目路径（本机）：`F:\Codex\fohe-dy`

> 2026-09-19 当前事实优先于本文后面保留的 2026-09-17 历史快照：代码已迁移到
> F 盘，素材库仍为 `D:\素材库2`，Python/FFmpeg 均由项目 F 盘虚拟环境提供。
> 当前 `main` 已推进到火山去字幕真实验收之后；以 `git log -1` 为准确 HEAD。

这份文档记录**当前这一刻**的实际状态：代码在哪、备份在哪、本机环境是什么样、
哪些东西不会跟着 git 走、以及已知的坑。目的是让你换一台电脑之后不用重新摸索一遍。

---

## 1. 代码状态

| 项 | 值 |
| --- | --- |
| 远端仓库 | `https://github.com/Kybzl/fohe-dy`（**私有**） |
| 分支 | `main` |
| HEAD | `3d47104`（记录本文档更新前的业务代码；之后以 `git log -1` 为准） |
| 提交总数 | 28（本文档更新前） |
| 受跟踪文件 | 167（本文档更新前） |
| 工作区 | 干净，与远端一致（`git status` 无输出） |

最近业务提交：

```text
3d47104 feat: export successful cloud cleanup without post-review
1a8cc7b fix: distinguish isolated cleanup texture changes
1566a35 feat: add zero-cost cloud cleanup preflight
64ccbf6 fix: block solid caption plates before paid cleanup
e0f1c6b feat: automate signed VOD output downloads
```

功能进度在生产链路（Milestone 9.8+）上。火山去字幕已完成真实付费调用、鉴权下载、
失败恢复、费用上限、零费用前置检查和直接导出验收。

---

## 2. 备份位置

有两套，用途不同，互不替代：

### 2.1 代码备份 —— git 仓库

日常开发用这个。`git push` 是增量同步，只同步**受跟踪文件**（见第 4 节）。

### 2.2 全量归档 —— GitHub Release

地址：`https://github.com/Kybzl/fohe-dy/releases/tag/backup-20260917`

| 资产 | 大小 | 内容 |
| --- | --- | --- |
| `fohe-dy-code.tar` | 16.62 MB | 源码、`.git` 完整历史、`.env`、`config.yaml`、`data/`、`logs/`、`reports/`、`exports/`、`docs/` |
| `fohe-dy-venv.tar` | 1192.24 MB | Python 虚拟环境 `.venv/` |
| `fohe-dy-browser_data.tar` | 740.82 MB | Playwright 持久化配置 `browser_data/`（含抖音登录态） |
| `fohe-dy-pytest-tmp.tar` | 1562.01 MB | 测试临时目录 `.pytest-tmp-*/` |

合计 3.43 GB。四个资产都**下载回本地做过 SHA-256 逐字节比对，全部一致**，
不是只看文件大小。解压后顶层目录为 `fohe-dy/`，与原结构一致：

```text
tar -xf fohe-dy-code.tar
```

**唯一未包含的内容**：空目录 `.pytest-tmp/`。它的 ACL 异常，本机连列目录都会被拒绝
（`icacls` 报 Access denied），里面没有任何文件，因此对完整性无实际影响。

**归档是时间点快照，不是同步机制。** 它不会随开发自动更新；需要新快照时得重新打包上传。

### 2.3 本地副本

`E:\Codex\fohe-dy-full-backup\` 下保留了上面那 4 个 tar（约 3.5 GB），与 Release 上的一致。

---

## 3. 本机环境

### 3.1 路径

| 用途 | 路径 | 说明 |
| --- | --- | --- |
| Python 解释器 | `F:\Codex\fohe-dy\.venv\python.exe` | 项目虚拟环境 |
| 虚拟环境 | `F:\Codex\fohe-dy\.venv` | 安装在 F 盘；换机器应重建 |
| FFmpeg | `F:\Codex\fohe-dy\.venv\Library\bin\ffmpeg.exe` | 随当前环境发现 |
| FFprobe | `F:\Codex\fohe-dy\.venv\Library\bin\ffprobe.exe` | 随当前环境发现 |
| 素材库输出根 | `D:\素材库2` | 写在 `config.yaml` 第 18 行 |
| SQLite 索引 | `F:\Codex\fohe-dy\data\library.db` | 本机运行数据，不进 git |

`D:\素材库2` 现有物料目录：红薯干、芒果干、苹果干、苹果片、香菇干、香蕉干。

### 3.2 网络

本机**直连 `github.com` 会超时**（`api.github.com`、`codeload.github.com` 正常）。
所有 GitHub 操作走本机代理 `127.0.0.1:6789`。

代理配置写在**仓库本地** git 配置里（`http.proxy` / `https.proxy`），
只对本仓库生效，**不会跟着仓库传到别的机器**。新机器如果网络正常，不需要设代理。

### 3.3 Git 身份

`user.name` / `user.email` 也是 `--local` 写入的，clone 之后需要重新设：

```text
git config --global user.name  "Kybzl"
git config --global user.email "197232305+Kybzl@users.noreply.github.com"
```

---

## 4. 不会被 git 同步的内容

这些在 `.gitignore` 里，换机器时**不会自动跟过去**，但它们在这台机器上确实存在，
所以很容易误以为「已经同步了」。

| 内容 | 大小 | 不同步的后果 |
| --- | --- | --- |
| `.env` | 1.5 KB | **程序起不来**，必须手工重建这份密钥文件 |
| `data/library.db` | 1.4 MB | 素材库索引丢失，等于从空库开始 |
| `browser_data/` | 740 MB | 抖音登录态丢失，要重新扫码登录 |
| `.venv/` | 1.1 GB | 用 `pip install -r requirements.txt` 重建 |
| `logs/` `reports/` `exports/` `cache/` | — | 本地产物，本来也不该同步 |
| `.pytest-tmp-*/` | 1.4 GB | 测试临时垃圾，可以随时删 |

`.env` 影响最大：它不是可选项，缺了程序起不来。归档里的 `fohe-dy-code.tar` 含有一份
当时的 `.env`，是跨机器时唯一现成的来源（注意那是快照，之后改过的密钥不在里面）。

---

## 5. 换机器继续开发

新机器上：

```text
git clone https://github.com/Kybzl/fohe-dy.git
cd fohe-dy
git config --global user.name  "Kybzl"
git config --global user.email "197232305+Kybzl@users.noreply.github.com"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

然后补两样 git 带不走的东西：

1. 重建 `.env`（可从归档里的 `fohe-dy-code.tar` 取，或按 `.env.example` 填）
2. 抖音重新登录一次，生成新的 `browser_data/`

不同电脑的盘符不需要再改受 Git 跟踪的 `config.yaml`。在各自本地 `.env` 设置：

```text
FOHE_LIBRARY_ROOT=D:/素材库2
FOHE_DATABASE_PATH=./data/library.db
FOHE_CACHE_ROOT=./cache
FOHE_FFMPEG_PATH=./.venv/Library/bin/ffmpeg.exe
FOHE_FFPROBE_PATH=./.venv/Library/bin/ffprobe.exe
```

优先级为：程序显式参数 > 系统环境变量 > `.env` > `config.yaml`。因此换电脑只需
调整本地 `.env`，不会制造需要提交的机器专属配置差异。

迁移前使用 `scripts/backup_runtime_state.ps1 -DestinationRoot <非C盘目录>` 备份
`.env`、`data/library.db`、`config.yaml` 和 `browser_data/`。脚本拒绝默认写入系统盘，
并生成逐文件 SHA-256 `manifest.json`；可先加 `-DryRun` 查看范围。
新电脑完成代码和依赖初始化后，使用
`scripts/restore_runtime_state.ps1 -SnapshotRoot <快照目录>` 校验快照，再追加
`-Apply` 恢复。脚本会限制可恢复路径、防止目录穿越，并在覆盖前把本机状态保存到
`data/restore-rollbacks/`。恢复时必须先关闭 Agent 和浏览器自动化进程。

单个采集任务使用 `--resume-task <id>` 断点续采时，程序先校验任务是否存在，
以及素材、时长范围和字幕策略是否与原任务一致。目标数可以调高；身份参数
不一致时会在初始化外部后端前终止，不改写原任务。
搜索词完整遍历后会持久化 `completed` 检查点，恢复时只执行尚未完成的词；
因目标、预算、取消或提供商故障而中断的词不会误标为完成。整轮已穷尽时，
后续恢复会进入新一轮刷新，保留发现新视频的能力。
任务运行期间会在任务错误字段写入内部 `runner_pid` 所有者标记，正常结束时清除。
新 `TaskRunner` 仅对 PID 已失效（或旧版本没有 PID）的 `running` 任务做崩溃恢复：
任务转为 `partial` 并记录 `interrupted_by_process_restart`，其中间态来源转回
`discovered`。存活 PID 所有的任务保持不变，避免多窗口误恢复。

Schema v16 在 `tasks.request_json` 保存非密钥的完整 `TaskRequest`。CLI 收到
`--resume-task <id>` 时会先加载快照，并允许显式的 `--target`、来源或后端参数覆盖它。
`library_root` 始终默认使用当前工作站配置，避免换电脑后继续写入旧盘符。
恢复时调高的目标会回写到快照；旧 schema 自动增加列，旧任务继续兼容。

FFmpeg / FFprobe 不是 pip 包，要单独装系统级；装好后把 `config.yaml` 里的
`media.ffmpeg_path` / `media.ffprobe_path` 指向实际位置（或留空以从 PATH 自动发现）。

**回到本机时**：先在另一台机器 `git push`，回到这台先 `git pull`。
本机因为网页端提交落后过远端，需要 fast-forward 才对齐，这个流程要保持一致。

---

## 6. 已知问题 / 待办

### 6.1 `.env.example` 里有真实密钥（提交 `c2f0615`）

该提交把 `.env.example` 的占位符换成了真值，涉及 `QWEN_API_KEY`、`DASHSCOPE_API_KEY`、
`DOUYIN_BACKEND_SESSION_COOKIE`、`VOLCENGINE_ACCESS_KEY_ID`、
`VOLCENGINE_SECRET_ACCESS_KEY`、`LAS_API_KEY`，以及私有端点域名和 TOS bucket 名。

仓库是私有的，外部暂时看不到，但：

* `.env.example` 的定位就是「可以公开的模板」，不该放真值；
* 提交历史是永久的，改回来也不会让 `c2f0615` 里的值消失。

**处理方式**：把 `.env.example` 恢复成空占位符，并轮换上述凭据。

### 6.2 `Kybzl/bhyy` 公开仓库的历史仍含代理凭据

该仓库（公开）里的 `kb.yml` / `dogess.yml` 已被删除，但历史提交 `f261187` 仍可读到
服务器地址、`password`、`node_password`。删除文件不等于删除历史，凭据需要轮换。

### 6.3 `config.yaml` 写死了本机绝对路径

`library_root`、`ffmpeg_path`、`ffprobe_path` 都是本机路径，换机器必须改。
`ffmpeg_path` / `ffprobe_path` 可以留空以自动从 PATH 发现，但 `library_root` 需手工指定。

### 6.4 换行符

系统级 `core.autocrlf=true`，git 每次处理文件都会把 LF 转成 CRLF。不同机器上该设置不一致时，
来回同步会产生「整个文件都改了」的假 diff 和大量 warning。建议加 `.gitattributes` 固定。

### 6.5 README 版本号（已修复）

README 顶部已更新为 Milestone 9.8+，不再停留在旧的 Milestone 3.5。

### 6.6 `.pytest-tmp/` 目录 ACL 异常（已绕开）

旧 `.pytest-tmp/` 目录的 ACL 仍由 Windows 拒绝访问，但测试配置已不再使用它。
`pytest.ini` 现在把临时文件和缓存放在 F 盘项目内的
`data/.pytest-temp` 和 `data/.pytest-cache`，不会写入系统盘，也不会提交 Git。

---

## 7. 常用命令

```text
python app.py                     # 启动 Gradio Web UI (http://127.0.0.1:7860)
python app.py --check             # 环境自检
python app.py --demo              # 离线 mock 冒烟测试
python app.py --check-config      # 打印生效配置（不含任何密钥）
python app.py --check-douyin      # 抖音后端连通性 / 鉴权 / 能力
python app.py --check-douyin-browser   # 浏览器搜索状态（含登录 / 验证墙判定）

pytest                            # 临时文件/缓存位于 data/.pytest-*（项目盘）
```

更完整的功能说明见 `README.md`，各里程碑的验收记录见 `docs/m*_acceptance.md`。
