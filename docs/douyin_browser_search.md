# 抖音浏览器搜索发现（Milestone 3.5）

## 1. 职责边界

Playwright **只负责发现公开视频链接**：

```text
Playwright（搜索页）
   ↓  公开 https://www.douyin.com/video/<aweme_id>
dtk /api/v1/douyin/video（详情 + 播放流）
   ↓
DouyinSource 归一化 → 现有 M2 管线（远程抽帧 → Qwen → 切片 → 标签 → 入库）
```

明确**不做**：

* 不下载源视频（下载由 dtk 直链 + 现有 downloader 完成）
* 不破解签名
* 不绕过验证码 / 滑块 / 短信验证 / 登录限制
* 不做 stealth / 指纹伪装等反检测改造
* 不访问私密内容，不导出 Cookie 到日志或数据库

## 2. 安装

```bash
pip install playwright
python -m playwright install chromium     # 只需要 Chromium
```

本项目已把 `playwright` 写进 `requirements.txt`；浏览器二进制只装 Chromium。

## 3. 配置

```yaml
sources:
  douyin:
    browser_search:
      enabled: true
      profile_dir: "./browser_data/douyin"   # 持久化 profile（相对项目根）
      headless: false                        # 首次人工登录/验证建议 false
      max_scrolls_per_query: 8
      scroll_delay_seconds: 1.0
      max_results_per_query: 50
      navigation_timeout_seconds: 30
      page_settle_seconds: 6
      keep_context_open: true                # 一个任务内复用同一个浏览器
      keep_open_on_challenge: true           # 遇到验证墙时保留窗口供人工处理
```

## 4. 人工登录 / 验证（不会被自动化）

```bash
python app.py --init-douyin-browser      # 打开可见窗口 + 抖音搜索页
```

操作者在这个窗口里：

1. 正常扫码/短信登录一次（**密码永远不进入配置或 `.env`**）
2. 如出现滑块等验证，人工完成后关闭窗口

会话保存在持久化 profile 目录中，后续采集复用；程序不会读取、打印或存储任何
Cookie 明文。

诊断：

```bash
python app.py --check-douyin-browser
```

可能的状态（`sources/douyin_search.py::BrowserSearchStatus`）：

| 状态 | 含义 | 处理 |
| --- | --- | --- |
| `ok` | 搜索结果正常 | 无需操作 |
| `upstream_bad_gateway` | 抖音边缘返回 **502 Bad Gateway**（导航状态码或页面文本） | 上游/网络网关问题，**不是**验证码；程序按 `upstream_retry_count` 有界重试后退避 |
| `upstream_http_error` | 抖音边缘返回 503/504 等服务器错误 | 同上，按有界重试处理 |
| `login_required` | 结果区被登录墙挡住 | `--init-douyin-browser` 登录一次 |
| `verification_required` | 抖音返回验证中间页/滑块 | 人工完成验证一次 |
| `douyin_unreachable` | 导航失败或 5xx | 检查网络/代理；不要高频重试 |
| `browser_unavailable` | 缺少 Playwright/Chromium | `pip install playwright && playwright install chromium` |
| `search_timeout` | 页面在超时内没有结果 | 调大 `navigation_timeout_seconds` |
| `search_dom_changed` | 有结果卡片但识别不到 `/video/` 链接 | 抖音改版，需要更新选择器 |
| `no_results` | 页面没有结果 | 换关键词 |
| `browser_crashed` | 页面/渲染进程异常 | 自动丢弃上下文，下一个查询重新启动 |

命中 `login_required` / `verification_required` / `douyin_unreachable` /
`browser_unavailable` / `upstream_bad_gateway` / `upstream_http_error` 后，
**同一个任务内不再重复尝试浏览器**（避免反复触发风控或反复打挂掉的网关），
需要时调用 `reset_block()`（或重新运行 `--init-douyin-browser` 后重启任务）。

### 网关错误与验证码的区分（重要）

抖音边缘（`kngx`）失败时会返回类似：

```text
502 Bad Gateway
kngx/1.10.2
```

这类页面的 `<title>` 可能是“验证中间页”，但它是**上游网关错误**，不是验证码。
分类顺序因此是：

```text
1. 导航响应状态 502/503/504        -> upstream_bad_gateway / upstream_http_error
2. 页面文本含 502 Bad Gateway/kngx -> upstream_bad_gateway（在登录/验证判定之前）
3. 标题“验证中间页”                 -> verification_required
4. 登录面板（login-full-panel/扫码登录） -> login_required
5. captcha/verifycenter 且无结果      -> verification_required
```

每次尝试都会记录诊断字段（打印在 `--check-douyin-browser`）：
`requested_url` / `final_url` / `http_status` / `page_title` / `browser_status`
（以及内部 `attempts`）。**不记录** Cookie、请求头或任何凭据。

502/503/504 属于**有界可重试**的上游错误：
`browser_search.upstream_retry_count`（默认 2）次重试，线性退避
`upstream_retry_backoff_seconds`（默认 2s），之后放弃并在本任务内停止重试。

## 5. 抓取策略与稳健性

* 只在公开搜索页上操作：导航 → 等待 `page_settle_seconds` → 有界滚动
  （`max_scrolls_per_query`）→ 每轮抽取新增链接
* 停止条件（任一命中即停）：达到 `limit` / 达到 `max_results_per_query` /
  滚动后没有新视频 / 达到最大滚动次数 / 命中登录或验证墙 / 导航超时
* 抽取优先级（在 `sources/douyin_browser_search.py` 集中实现）：
  1. `a[href*="/video/"]`、`a[href*="/note/"]` 锚点
  2. 渲染后 HTML 中的 `/video/<digits>` 链接
  3. 页面数据里的 `aweme_id` / `awemeId` 字段
* URL 规范化：协议相对链接补齐、去掉 `?previous_page=` 等追踪参数，
  统一为 `https://www.douyin.com/video/<id>`；`/search/…`、`/user/…`
  一律不当作候选
* 立即按 `platform_video_id` + 规范化 URL 去重

## 6. 观测与指标

UI 与 CLI 会报告“当前发现方式”（`browser` / `dtk_keyword` / `archive` /
`author_posts` / `mix_posts` / `manual_url`），并把每个搜索词的产出写入
`search_yields`：`query / candidate_count / unique_candidate_count /
preview_accept_count / download_count / final_clip_count`。

## 6.1 Milestone 3.6 实测补充（2026-09-14）

* **搜索页标签页**：`/search/<关键词>` 的“综合”页面对自动化会话只返回空壳
  （DOM 里有结果容器，但 0 个 `/video/` 链接、0 次业务 XHR）。因此
  `SEARCH_URL_TEMPLATE` 固定为 `/search/<关键词>?type=video`，并在完全无结果
  时对普通搜索页再做一次有界兜底。真实运行中视频标签页每次返回 8–10 个链接。
* **验证墙判定只看渲染结果**：`verifycenter` / `rmc-nocaptcha` / `captcha`
  字符串来自**每个页面都会加载**的风控 SDK，不再据此判定
  `verification_required`。只有渲染出验证文案（“请完成安全验证”“滑动验证”
  “拖动滑块”等）才判定需要人工验证；若页面存在结果容器/视频链接，则结构化的
  captcha 标记也会被忽略（避免把普通搜索页误报成人机验证）。
* **未命中不等于被阻断**：连续 `browser_search.empty_result_limit`（默认 3）
  次“成功但 0 结果”后，本任务不再驱动浏览器（节省时间），状态仍是
  `no_results`，不会被记成 `discovery_blocked`。
* **浏览器只给 URL**：候选的时长/标题/作者由真实 dtk 内容接口补齐
  （`sources/douyin.py::_enrich_candidates`），否则“未知时长”会被本地预筛
  误判成 `duration_out_of_range`。
* **诊断字段**：`requested_url / final_url / http_status / page_title /
  browser_status / browser_channel / browser_executable / profile_dir /
  headless`，不含 Cookie、请求头或任何凭据。

## 7. 已知限制

* 抖音可能按网络出口/客户端要求登录或验证：这是**平台策略**，本项目不绕过，
  只报告状态并支持人工完成一次。
* 搜索结果 DOM 会变化：识别不到链接时报告 `search_dom_changed` 而不是静默返回空。
* 页面结构与风控不以本项目为准，生产使用请保持保守频率，并遵守平台条款与
  当地法律；采集仅用于自有素材整理，注意版权与授权。
