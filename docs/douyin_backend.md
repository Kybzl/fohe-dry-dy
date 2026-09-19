# Douyin 后端接入说明（Milestone 3）

`fohe-dy` **不实现**抖音签名、Cookie 管理或逆向逻辑。抖音采集通过一个
独立部署的后端服务完成，两者之间只有 HTTP：

```text
fohe-dy  --HTTP-->  Douyin_TikTok_Download_API (dtk v5)  --签名/身份池-->  Douyin
```

这条边界的意义：上游可以独立升级、独立重启、独立换 Cookie/代理，
`fohe-dy` 只消费它的公开 API。

## 1. 使用的上游项目与版本

| 项目 | 仓库 | 适配版本 | 参考提交 |
| --- | --- | --- | --- |
| Douyin_TikTok_Download_API ("dtk") | `Evil0ctal/Douyin_TikTok_Download_API` | `v5.0.3`（OpenAPI `info.version`） | `4ef5ed55e7c81eb2c7bf781b132128e24112fa29`（main，2026-09-14） |

核对方式（实现前已执行）：

```bash
# 上游文档
curl -s https://raw.githubusercontent.com/Evil0ctal/Douyin_TikTok_Download_API/main/llms.txt
curl -s https://raw.githubusercontent.com/Evil0ctal/Douyin_TikTok_Download_API/main/documents/en/11-api.md
# 上游 OpenAPI（来自其官方 demo 实例，只读）
curl -s https://demo.douyin.wtf/openapi.json -o openapi.json
```

代码里的常量把这个契约写死在一个地方，便于升级时对照：
`sources/douyin_backend.py` 的 `UPSTREAM_PROJECT` / `UPSTREAM_API_VERSION`。

## 2. 部署上游服务

按上游仓库的说明用 Docker Compose 自建（上游 README / `documents/en/01-quickstart.md`）。
默认监听 `127.0.0.1:8000`。我们的配置项与之对应：

```yaml
sources:
  active_source: "douyin"
  douyin:
    backend: "dtk"
    base_url: "http://127.0.0.1:8000"
    api_key_env: "DOUYIN_BACKEND_API_KEY"
```

API Key 在上游控制台的 **API keys** 页面创建（形如 `dtk_0a1b2c3d4e5f_<random>`），
只显示一次。把它写进本项目的 `.env`：

```text
DOUYIN_BACKEND_API_KEY=dtk_...
```

`fohe-dy` 不会打印、不会入库、不会写日志该值；请求头使用上游文档指定的
`X-API-Key`。

## 3. 健康检查

```bash
python app.py --check-douyin
```

检查顺序（全部为轻量调用，不做大规模搜索）：

1. `GET /healthz`（必要时 `/readyz`）→ 后端是否可达
2. `GET /api/v1/auth/me` → API Key 是否被接受，返回角色与 scopes
3. `GET /api/v1/system/status` → 版本与 commit
4. `GET /openapi.json` → 能力探测（内容读取 / 归档检索 / 任务 / 关键词搜索）

示例输出：

```text
[ok] Douyin backend reachable (http://127.0.0.1:8000)
[ok] API authentication accepted (account=admin)
[ok] backend version: 5.0.3 commit=4ef5ed5
[ok] content endpoint available
[ok] archive search (q=) available
[ok] async task endpoint available
[warn] keyword search endpoint: NOT provided by this backend version
[warn] Evil0ctal/Douyin_TikTok_Download_API v5.0.3 exposes no keyword search endpoint;
       discovery uses archive search, author/mix seeds or manual URLs
```

后端不可用时应用照常启动，只是 `sources.active_source=douyin` 的采集会失败并给出
明确原因；把来源切成"本地测试视频"或"模拟数据"即可继续使用其它链路。

## 4. 本适配器实际调用的接口

全部来自 v5.0.3 的 `/openapi.json`（83 个路径）中被文档化且与采集相关的部分：

| 用途 | 方法 | 路径 |
| --- | --- | --- |
| 单条内容（含媒体流） | GET | `/api/v1/{platform}/video?aweme_id=` 或 `?url=` |
| 作者作品列表（游标分页） | GET | `/api/v1/{platform}/user/posts?sec_user_id=&cursor=&count=` |
| 合集/播放列表作品 | GET | `/api/v1/{platform}/mix/posts?mix_id=&cursor=&count=` |
| 归档检索（`q=` 子串匹配标题/描述） | GET | `/api/v1/archive?q=&cursor=&limit=&platform=` |
| 解析任意分享链接 | POST | `/api/v1/parse` |
| 异步任务状态 | GET | `/api/v1/tasks/{task_id}` |
| 身份校验 | GET | `/api/v1/auth/me` |
| 版本信息 | GET | `/api/v1/system/status` |

统一响应信封（成功与失败一致）：

```json
{"success": true, "data": {}, "error": null, "meta": {"request_id": "..."}}
```

错误处理基于 `error.code`（稳定枚举）而不是 `error.message`（会翻译）。

## 5. 异步任务模型

上游的数据接口**默认异步**：提交返回 `202` + `task_id`，随后轮询
`GET /api/v1/tasks/{task_id}`：

```text
queued -> running -> done | failed
```

要点（都已实现，见 `sources/douyin_backend.py`）：

* 完成后的负载在 `body.data.data`（多一层嵌套），`result_meta` 在 `data.result_meta`
* **失败任务仍是 HTTP 200**：失败信息在 `data.state == "failed"` 与 `data.error`
* 任务错误对象带 `retryable` 布尔值，我们据此决定是否允许重试
* 提交时可用 `?wait=<秒>`（上限 30）让服务端等待，我们用
  `sources.douyin.task_wait_seconds` 配置它，超时后自动转轮询
* 轮询有总时长上限（`max(POLL_INTERVAL, task_wait_seconds*? )` → 代码中为
  `max_task_wait_seconds`），不会无限轮询，也不 busy-poll

## 6. 关键词搜索：上游没有这个能力（重要）

**结论：v5.0.3 不存在任何关键词搜索接口。** 证据：

```bash
# 路径总数与搜索相关路径
python - <<'PY'
import json; s=json.load(open("openapi.json",encoding="utf-8"))
paths=s["paths"]; print(len(paths))
print([p for p in paths if "search" in p.lower()])
print([p for p in paths for m in paths[p] if m in ("get","post")
       for prm in (paths[p][m].get("parameters") or []) if "keyword" in json.dumps(prm).lower()])
PY
# -> 83
# -> []
# -> []
```

上游提供的是 *内容/作者/合集/归档* 读取，而不是平台搜索。因此本适配器：

1. `KeywordSearchBackend` 会在**运行实例的** `/openapi.json` 中探测关键词路由
   （候选表 `sources.douyin.search_endpoint_candidates`）。若某天上游或私有分支
   提供了该能力，会自动启用，**不需要改代码**。
2. 没有时按优先级退化到**受支持的**发现方式：

   | 顺序 | 后端 | 说明 | 配置 |
   | --- | --- | --- | --- |
   | 1 | `keyword` | 仅当实例暴露关键词搜索时 | — |
   | 2 | `archive` | 检索后端已归档内容 `q=<关键词>` | `discovery.archive_search` |
   | 3 | `author_posts` | 遍历指定作者的作品 | `discovery.author_sec_uids` |
   | 4 | `mix_posts` | 遍历指定合集 | `discovery.mix_ids` |
   | 5 | `manual_url` | 显式视频链接 | `--douyin-url` / `discovery.manual_urls` |

3. 在 `--check-douyin` 与任务日志里**明确报告**缺失的能力（不静默降级、
   不伪造路由、不引入浏览器抓取）。

> Milestone 3.5 增加了第 2 级发现方式：**浏览器搜索**（Playwright）。
> 见下面的第 9 节与 `docs/douyin_browser_search.md`。

### 让归档检索/作者发现真正有数据

归档检索读的是**后端自己收集过的内容**。在上游控制台里添加 watchlist
（作者或内容）或做一次 backfill，`fohe-dy` 的关键词检索就能命中这些内容；
也可以直接把目标作者 `sec_user_id` 填进 `sources.douyin.discovery.author_sec_uids`。

## 7. 常见错误

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `[warn] Douyin backend not running` | 后端未启动或端口/地址不对 | 启动上游服务，核对 `base_url` |
| `401/403` + `[warn] API authentication missing/refused` | 没配 `DOUYIN_BACKEND_API_KEY` 或 key 被吊销 | 在控制台新建 key 写入 `.env` |
| `RATE_LIMITED`（HTTP 429） | 触发上游限流 | 降低 `concurrent_requests`；程序已按 `retry_after` 有界退避 |
| `IDENTITY_POOL_EXHAUSTED` / 503 | 上游身份池不可用 | 在上游控制台查看身份池/代理状态（属于上游运维） |
| 任务 `failed` 且 `retryable=false` | 视频被删、私有、ID 无效 | 换候选；我们不会重试这类错误 |
| 媒体 URL 403 过期 | 抖音媒体直链带签名且会过期 | 程序会自动刷新一次 URL 再重试，仍失败则记为 `failed_download` |
| 关键词检索 0 结果 | 上游无关键词搜索，归档/种子为空 | 见第 6 节：配置 author/mix 种子，或使用 `--douyin-url` |

## 8. 本项目不会做的事

* 不修改上游源码、不管理上游数据库迁移、不直接管理 Cookie
* 不实现抖音签名/`a_bogus`/`X-Bogus` 等算法
* 不绕过访问限制获取私有内容
* 不把 Playwright 之类浏览器抓取作为依赖

---

## 9. Milestone 3.5：浏览器搜索与发现顺序

发现顺序在运行时确定，并在 `--check-douyin` / UI 中报告：

```text
1. dtk 关键词搜索      仅当连接到的实例的 /openapi.json 真的暴露该路由
2. 浏览器搜索          Playwright 打开 www.douyin.com 的公开搜索页
3. 归档 / 作者 / 合集 / 手工链接   dtk 已支持的读取方式
```

浏览器只负责**发现公开视频链接**；拿到 `https://www.douyin.com/video/<id>` 之后，
详情、可用播放流与下载依旧由 dtk 提供（`DouyinSource` 归一化）。
配置与状态机见 `docs/douyin_browser_search.md`。

### 现场观测（本机 2026-09-14 实测，作为运维参考）

| 页面 | 浏览器结果 | 说明 |
| --- | --- | --- |
| `www.douyin.com/video/<id>` | 200，正常渲染（含页面内 8 个 `/video/` 链接） | 视频页可匿名访问；页面内有登录浮层 |
| `www.douyin.com/search/<kw>` | 200，页面壳渲染但结果区为空，存在 `login-full-panel` / `扫码登录` | → 判定为 `login_required` |
| `www.douyin.com/`（首页） | 200，返回“验证中间页”（含 `verifycenter`/`captcha`） | → 判定为 `verification_required` |

两种“墙”都不会被绕过：程序报告状态、保留持久化浏览器配置目录，
由操作者用 `python app.py --init-douyin-browser` 手动完成一次登录或验证即可复用。
