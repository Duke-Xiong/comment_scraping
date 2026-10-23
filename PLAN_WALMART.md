# 沃尔玛（Walmart）评论抓取支持 — 规划文档

| 项 | 内容 |
|---|---|
| 状态 | Implemented v1.0（全部决策已确认；独立页面；21 个 Walmart 测试） |
| 日期 | 2026-10-23 |
| 目标读者 | 维护者 / 实现者 |
| 关联文档 | [README.md](README.md)、[HANDOFF.md](HANDOFF.md) |
| 一句话目标 | 新增独立的 Walmart 下载页面，通过右上角 Amazon/Walmart 两段式开关切换；页面参照 Amazon，代码独立互不影响；支持 item ID、星级筛选、仅文字评论、星级汇总、seller/source 字段 |

---

## 1. 背景与目标

当前项目只支持 Amazon。现在新增 Walmart（`www.walmart.com`），但**不把 Walmart 混进 Amazon 页面**：

- Amazon 页面保持现状，只增加右上角切换入口；
- Walmart 使用**独立页面**，布局、流程、进度、结果参照 Amazon；
- Walmart 使用**独立代码包**、独立路由、独立任务、独立输出目录、独立浏览器 profile；
- 两边互不影响：Walmart 的改动不能让 Amazon 的抓取、UI、导出、测试回归。

**成功定义（简版）**：能在 Amazon / Walmart 两个页面间一键切换；Walmart 页面输入 item ID 后能按所选星级抓取评论与评分，导出含 seller/source 的文件；Amazon 页面行为完全不变。

---

## 2. 已确认的需求与范围

### 2.1 已确认决策

| # | 决策 |
|---|---|
| 1 | **站点范围**：`www.walmart.com` 全站、全品类；不做 `walmart.ca` / `walmart.com.mx` |
| 2 | **商品编号**：Walmart item ID（纯数字）或商品链接；不做 UPC/SKU 反查 |
| 3 | **页面形态**：Amazon / Walmart 两个独立页面；右上角 **Amazon / Walmart 两段式开关**切换 |
| 4 | **代码隔离**：Walmart 独立包、模板、JS、路由、任务、输出目录、profile；Amazon 只做最少接入改动 |
| 5 | **页面参照**：Walmart 页面结构、进度、结果、下载交互参照 Amazon |
| 6 | **批量**：不做 Amazon + Walmart 混合批量 |
| 7 | **评分**：**只保留有文字的评价**；没写文字的评分过滤掉，不逐条导出；仍保留评分汇总（总数/平均分/各星级数量） |
| 8 | **星级筛选**：Walmart 页面要做星级筛选 |
| 9 | **字段**：主表追加 `seller` / `source` 列 |
| 10 | **headful**：Walmart 显示 Chrome 窗口可接受 |
| 11 | **CLI**：提供 `python -m walmart_reviews` |
| 12 | **切换按钮**：Amazon/Walmart 两段式开关 |
| 13 | **机器人校验**：采用 **A. 人机配合**（人工按住按钮 + 自动恢复）；不自动破解、v1 不接第三方数据服务 |

> “全站、全品类”指**支持 walmart.com 任意商品的 item ID**，按编号逐个/批量抓取，不是枚举整个站点目录。

### 2.2 In scope（v1）

- `www.walmart.com` 任意商品的评论与评分；
- item ID / `/ip/.../{id}` / `/reviews/product/{id}`；
- 星级筛选（服务端或本地过滤，P0 定）；
- 文字评论分页抓取；**无文字的评价不进入结果**；
- 评分汇总（总数、平均分、各星级数量）；
- `seller` / `source` 列；
- CSV / JSON / XLSX；
- 独立 Walmart 页面 + 两段式切换；
- `python -m walmart_reviews`；
- 离线测试与文档。

### 2.3 评分口径（已确定）

- **只导出有文字的评价**；`reviewText` 为空/纯空白的评分记录一律过滤掉；
- 不尝试获取/还原“只打分、没写文字”的 252 条记录；
- 仍保留评分汇总：`totalReviewCount`、`averageOverallRating`、各星级 `ratingValueXCount`，以及各星级文字评论数 `reviewsWithTextRatingXCount`；
- 这避免了逐条评分的数据源/合规问题，也与 Walmart 评论页实际展示的范围一致。

### 2.4 Out of scope（v1）

- Amazon + Walmart 混合批量；
- `walmart.ca` / `walmart.com.mx`；
- UPC/SKU/商品名反查；
- 卖家评论、Q&A；
- 自动登录 Walmart、自动破解 “Robot or human?”；
- 下载图片文件（只存 URL）；
- 代理轮换、指纹伪装；
- 伪造/推断无文字的逐条评分；
- 把 Walmart 数据写进 Amazon 目录或合并文件。

---

## 3. Walmart 站点调研（2026-10-23 实测）

> 来自本机 headful Chrome 的公开页面访问；页面/风控会变，P0 需复验。

### 3.1 商品编号与 URL

Walmart item ID 是**纯数字**：

```
https://www.walmart.com/ip/Apple-AirPods-5/20951672018
https://www.walmart.com/ip/20951672018
https://www.walmart.com/reviews/product/20951672018
https://www.walmart.com/reviews/product/20951672018?sort=relevancy&page=2
```

建议规则：`^\d{6,14}$`，P0 用多个商品确认。

### 3.2 评论页数据在 `__NEXT_DATA__`

```html
<script id="__NEXT_DATA__" type="application/json" nonce="...">...</script>
```

路径：

```
props.pageProps.initialData.data.reviews
```

实测（商品 20951672018）：

| 字段 | 值 | 含义 |
|---|---|---|
| `totalReviewCount` | 273 | **评分数**（ratings） |
| `reviewsWithTextCount` | 21 | **文字评论数** |
| `filteredReviewsCount` | 21 | 当前过滤条件下评论数 |
| `averageOverallRating` | 4.5 | Walmart 平均分 |
| `ratingValueFiveCount` | 212 | 5 星评分数 |
| `reviewsWithTextRatingFiveCount` | 12 | 5 星文字评论数 |
| `pagination` | `{currentSpan: "1-10", pages: [...]}` | 分页 |
| `customerReviews` | 10 条/页 | 当前页文字评论 |

页面文案：`273 ratings | 21 reviews`。

**分页陷阱**：第 2 页 `pagination.next = null`，但 `pagination.pages` 仍有第 3 页；终止条件用 `pages` 最大页号，不能只看 `next`。

### 3.3 分页与星级筛选

- 页大小 10；
- `?sort=relevancy&page=N`；
- 点击 5 星筛选后 URL 变为 `?ratings=5`（实测）；
- 直接重新加载 `?ratings=5` 时遇到 `/blocked`，**尚未验证服务端是否真的按星级过滤**；
- 因此 P0 要确认：
  1. `?ratings=5` 是服务端过滤，还是仅前端状态；
  2. 星级筛选后 `filteredReviewsCount` / `customerReviews` 是否变化；
  3. 筛选是否只包含文字评论，还是也包含无文字评分。

### 3.4 单条评论字段

| 字段 | 示例 | 备注 |
|---|---|---|
| `reviewId` | `"444092364"` | 评论 ID |
| `reviewReferenceId` | UUID | 兜底 ID |
| `rating` | `5` | 1–5 |
| `reviewTitle` | `"Great purchase."` | 可能 `null` |
| `reviewText` | 文本 | 可能为空；需验证截断 |
| `reviewSubmissionTime` | `"10/13/2026"` | `MM/DD/YYYY` |
| `userNickname` | 昵称 | 可能 `anonymous` |
| `authorId` | hash | 作者 ID |
| `userLocation` | `null`/地点 | 可映射 `country` |
| `badges` | `VerifiedPurchaser` | 已验证 |
| `positiveFeedback` | `0`/`2` | 有用票 |
| `negativeFeedback` | `0` | 点踩 |
| `photos`/`media` | `[]` | 图片 |
| `sellerName` | `"Walmart.com"` | 卖家 |
| `fulfilledBy` | `"Walmart"` | 履约方 |
| `itemId`/`itemName` | 商品 | |
| `externalSource` | `"bazaarvoice"` | 来源 |
| `syndicationSource` | `null` | 联合评论 |

### 3.5 风控（关键限制）

1. 评论页**不需要登录**；
2. `headless=True` 直接跳 `/blocked`（“Robot or human?”），已通过校验的 profile 也一样；
3. `requests` 返回 **HTTP 412**（PerimeterX）；
4. headful 连续请求几页也会被挑战，需要人工按住按钮；
5. `robots.txt` `Allow: /reviews/product/`，但不等于 ToS 允许。

结论：Walmart 只能 **browser + headful**，独立 profile，人工校验门禁，3–6 秒串行间隔。

### 3.6 P0 必须补齐

- 无文字评分在 `customerReviews` 中是否以空 `reviewText` 出现（确认过滤规则）；
- 高评论量商品分页上限；
- `?ratings=N` 是否服务端过滤；
- `sort` 可选值；
- `photos` / 图片 URL；
- `reviewText` 是否截断；
- 单条 permalink；
- 联合评论/变体重复；
- 挑战频率与冷却。

---

## 4. 总体方案：独立页面 + 独立代码

### 4.1 原则

1. 两个独立页面，不是同一表单的站点下拉；
2. Walmart 独立路由 `/walmart`，独立 SSE；
3. Walmart 独立包 `walmart_reviews/`，不 import `amazon_reviews`；
4. 独立 Job、输出目录、profile；
5. Amazon 只增加：注册 blueprint、首页开关、开关样式、打包收集。

### 4.2 用户视角

```text
右上角固定开关：[ Amazon | Walmart ]

/ (Amazon)      → 点击 Walmart → /walmart
/walmart        → 点击 Amazon  → /
```

- 两段式开关，当前站点高亮；
- 整页跳转，不做同一 DOM 内切 view，保证 JS/任务隔离；
- 两页可同时打开，但不建议同时跑抓取（两个 Chrome、同一 IP）。

### 4.3 文件结构

```text
walmart_reviews/
  __init__.py
  __main__.py            # python -m walmart_reviews
  models.py              # WalmartReview（18 列）
  parser.py              # __NEXT_DATA__ -> WalmartPage
  scraper.py             # headful、分页、星级筛选、挑战门禁
  store.py               # 去重/checkpoint/CSV/JSON/XLSX
  preflight.py
  cli.py

webapp/
  walmart_server.py      # blueprint + WalmartJob + SSE
  templates/walmart.html
  static/walmart.js
  static/walmart.css
  static/site-switch.css # 两段式开关，两个页面共用

tests/
  test_walmart.py
  fixtures/walmart_reviews_page1.json
  fixtures/walmart_reviews_page2.json
  fixtures/walmart_ratings_page.json
  fixtures/walmart_reviews_page.html

HANDOFF_WALMART.md
```

### 4.4 Amazon 侧最小改动

| 文件 | 改动 |
|---|---|
| [webapp/server.py](webapp/server.py) | 注册 `walmart_bp`（2 行） |
| [webapp/templates/index.html](webapp/templates/index.html) | 引入 `site-switch.css` + 两段式开关 markup |
| [build_exe.py](build_exe.py) | `--collect-submodules walmart_reviews` |
| [README.md](README.md) / [START HERE.txt](START HERE.txt) | 使用说明 |

不改：`amazon_reviews/**`、`webapp/static/app.js`、Amazon `/api/*`、`JOB`、`output/` 根目录、Amazon profile、现有 30 个测试。

### 4.5 隔离检查

- `walmart_reviews/` 与 `walmart_server.py` 不 import `amazon_reviews`；
- `walmart.js` 不引用 Amazon DOM/全局变量；
- 输出只写 `output/walmart/`；
- 独立 profile / Job / SSE；
- `python tests/test_parser.py` 仍 30/30；
- Amazon 首页只多开关，DOM/行为不变。

---

## 5. Walmart 模块设计

### 5.1 数据模型与输出列

Walmart 主表 = Amazon 16 列 + 2 个新列，顺序如下：

```
review_id, asin, title, body, rating, author, author_id, review_date,
country, verified_purchase, vine_review, helpful_votes, variant,
images, permalink, found_via,
seller, source
```

| 新列 | 取值 | 说明 |
|---|---|---|
| `seller` | `sellerName` | Walmart 卖家 |
| `source` | `externalSource` | 如 `bazaarvoice` |

- `asin` 存 item ID（沿用旧列名）；
- `WalmartReview` 独立 dataclass，不 import Amazon；
- 每一行都必须是**有文字的评价**；`reviewText` 为空/纯空白 → 丢弃，不生成行；
- 评分汇总另放 JSON `summary` 与 XLSX `Ratings` 表，不占评论行。

### 5.2 商品编号解析

- 裸数字 `^\d{6,14}$`；
- `/ip/.../{id}`；
- `/reviews/product/{id}`；
- 忽略 query；
- 10 位字母数字 → 报“不是 Walmart item ID”。

### 5.3 抓取流程（文字评论）

```text
open_browser(profile=output/walmart/.browser-profile, headless=False)
page = 1
while True:
    goto /reviews/product/{id}?sort=relevancy&page={page}
    [+ 星级参数，见 §5.7]
    if blocked: wait_for_human_challenge() or abort
    parsed = parse_next_data(html)
    store.add_many(parsed.reviews)
    if page == 1: store.capture_totals(parsed)
    store.save_checkpoint(page, total_pages)
    if page >= total_pages or page >= max_pages: break
    page += 1
    sleep(3–6s)
```

- `total_pages` 取 `pagination.pages` 最大页号；
- 缺失时按 `ceil(reviewsWithTextCount/10)`；
- `max_pages` 默认 200，达到即 `page_limit_reached=True`；
- 每页 checkpoint，续跑跳页；
- 按 `reviewId` 去重。

### 5.4 人工校验门禁

- URL `/blocked` 或正文 `Robot or human?` / `Activate and hold` → `challenge`；
- 提示用户到 Chrome 按住按钮；
- 每 2 秒轮询；恢复后重载当前页；
- 超时 900 秒 → 当前产品失败，明确写“机器人校验超时”；
- 绝不自动破解。

### 5.4.1 机器人校验：可行策略与边界

**结论：没有合规、稳定、可承诺的“全自动解决”方法。** Walmart 使用的是 PerimeterX，自动点击/过验证码服务既不可靠，也通常违反 ToS，并可能连累账号和 IP。v1 采用“人工过一次 + 会话复用 + 降速”，不集成自动破解。

合规可行的做法：

1. **人工过一次**：用户在 Chrome 窗口按住按钮；校验通过后 clearance 存在 `output/walmart/.browser-profile`，后续复用；
2. **只用 headful**：headless 和 `requests` 必被拦，不浪费时间；
3. **降速串行**：页间 3–6 秒随机；每 5–10 页多停 10–20 秒；不并发、不多标签；
4. **不要清 profile**：不要每次新建浏览器上下文；正常关闭浏览器；
5. **挑战门禁**：一旦 `/blocked` 立即停止请求，提示用户处理；恢复后先冷却 30–60 秒再继续；
6. **限制批量**：单批 ≤ 20 个商品；同一商品连续触发 2 次挑战就停止，提示稍后重试；
7. **记录指标**：每 10 页的挑战次数、profile 恢复后的持续页数，写进 P0 报告，用来决定 `max_pages` 和冷却时间。

如果必须**无人值守**，正确方向是换数据源，而不是破解：

- Bazaarvoice 官方 API / Walmart 合作方接口（需授权与 passkey）；
- 有资质的第三方数据服务（如 Apify、SerpApi、ScraperAPI 等），由对方处理风控；
- 需要评估费用、数据授权、Walmart ToS 和是否允许转存。

**明确不实现**：自动按住按钮、模拟鼠标轨迹、调用打码平台、stealth/undetected 插件、住宅代理轮换、指纹伪造。这些属于绕过校验，既不可靠，也会提高封禁与合规风险。程序只负责“检测到校验 → 停下来提示 → 等人工完成 → 自动恢复”。

**可以做的是“人机配合”**：把 Chrome 窗口置前、显示当前页码和等待倒计时、校验通过后自动继续；不代替人去完成校验。

### 5.5 存储、checkpoint、导出

| 内容 | 路径/命名 |
|---|---|
| 输出目录 | `output/walmart/` |
| 单产品 | `walmart_reviews_{id}.csv/json/xlsx` |
| 合并 | `walmart_reviews_all_products.*` |
| checkpoint | `output/walmart/.checkpoint_walmart_{id}.json` |
| profile | `output/walmart/.browser-profile/` |

- CSV：UTF-8 BOM，18 列（只含有文字的评价）；
- JSON：`{summary, reviews}`；
- XLSX：`Reviews` + `Summary` + `Ratings`；
- 无文字的评价在 `WalmartStore.add_many()` 入口过滤，不写 checkpoint、不计数；
- 导出格式隔离；
- 合并文件 ≥2 个产品有数据时生成。

### 5.6 评分汇总（逐条评分已排除）

**口径**：只导出有文字的评价；没写文字的评分不做逐条记录，只做汇总。

从第 1 页的 `reviews` 对象读取并保存：

```json
{
  "asin": "20951672018",
  "reviews_downloaded": 21,
  "reviews_reported_by_walmart": 21,
  "ratings_reported_by_walmart": 273,
  "average_rating_reported": 4.5,
  "rating_distribution_ratings": {"5": 212, "4": 24, "3": 12, "2": 2, "1": 23},
  "rating_distribution_reviews": {"5": 12, "4": 2, "3": 1, "2": 1, "1": 5},
  "coverage_pct": 100.0
}
```

- `reviews_downloaded` = 去重后的文字评论数；
- `reviews_reported_by_walmart` = `reviewsWithTextCount`；
- `ratings_reported_by_walmart` = `totalReviewCount`；
- `average_rating_reported` = `averageOverallRating`（Walmart 自己算的，不从样本重算）；
- `rating_distribution_ratings` = `ratingValueXCount`；
- `rating_distribution_reviews` = `reviewsWithTextRatingXCount`。

过滤规则：

- `reviewText` 为 `None`、空字符串或只有空白 → 丢弃；
- `reviewTitle` 为空但有正文 → 保留；
- 过滤发生在入库前，保证 UI 数字、CSV 行数、覆盖率一致。

### 5.7 星级筛选

**UI**：

- 5 个星级 checkbox + 快捷项：All / Positive (4–5) / Critical (1–3) / Clear；
- 默认全选；
- 与 Amazon 页面一致。

**抓取策略（P0 已确认 + v1 实现取舍）**：

- P0 实测：`?ratings=5` **服务端确实生效**（12 条、2 页、全部 5 星）；
- v1 实现仍使用**本地过滤**：抓全部文字评论后按 `rating` 过滤。理由是多星级选择时 checkpoint、分页和进度更简单一致；
- 单星服务端抓取作为后续优化，不改变输出正确性；
- 星级筛选只作用于**文字评论**；无文字评分不进入结果；
- 覆盖率分母：所选星级的 `reviewsWithTextRatingXCount` 之和；
- 评分汇总仍展示全部星级的 `ratingValueXCount`；
- 只选部分星级时，在 UI 明确“这是筛选后的文字评论样本，不代表全部评分分布”。

---

## 6. Web UI 设计

### 6.1 路由

| 路由 | 用途 |
|---|---|
| `/` | Amazon 页面（不变） |
| `/walmart` | Walmart 页面 |
| `/walmart/api/start` | 启动 |
| `/walmart/api/stop` | 停止 |
| `/walmart/api/status` | 状态 |
| `/walmart/api/events` | SSE |
| `/walmart/api/download/<name>` | 下载 `output/walmart/` |

### 6.2 两段式开关

```html
<nav class="site-switch" aria-label="Site">
  <a href="/" class="active" aria-current="page">Amazon</a>
  <a href="/walmart">Walmart</a>
</nav>
```

- 固定右上角，胶囊容器，当前站点高亮；
- Amazon 页 `Amazon` 高亮；Walmart 页 `Walmart` 高亮；
- 共用 `site-switch.css`；
- 纯 `<a>` 整页跳转，不共享 JS 状态；
- 键盘可聚焦，`aria-current` 正确。

### 6.3 Walmart 页面布局

复用 `style.css` 的卡片/按钮/进度条/统计/队列/结果表，`walmart.css` 只写差异。

1. **Header**：`Walmart Review Downloader` + 两段式开关。
2. **Setup**
   - `Products` textarea，一行一个 item ID/链接；
   - 计数；
   - 固定站点说明 `Walmart US (walmart.com)`；
   - **星级选择**（5 个 checkbox + 快捷项）；
   - Advanced：`Max pages`（默认 200）；
   - `Start download`。
3. **Challenge**
   - `Walmart needs a quick human check`；
   - 步骤：切到 Chrome → 按住按钮 → 自动继续；
   - 计时器 + Cancel。
4. **Progress**
   - 整批 + 当前产品进度条；
   - 统计：`reviews total`、`this product`、`Walmart reports`、`complete`、`elapsed`、`est. left`；
   - 当前页 `page X of Y`；
   - 队列、Activity log。
5. **Results**
   - 汇总：reviews/ratings saved、products done、平均分、verified；
   - 评分分布表：各星级 ratings 与 reviews 数量；
   - 合并下载；
   - 每产品：item ID、数量、覆盖率、下载；
   - 页数上限/筛选提示。

### 6.4 前端 JS

- `walmart.js` 独立；
- SSE `/walmart/api/events`；
- 事件：`starting`/`scraping`/`challenge`/`product_done`/`done`/`error`；
- 字段：`page`、`total_pages`、`reviews`、`reviews_total`、`ratings_total`、`percent`、`elapsed`；
- 下载前缀 `/walmart/api/download/`；
- 不引用 Amazon DOM/全局变量。

### 6.5 后端任务

- `WalmartJob` 独立单例；
- 独立 queue、stop flag、running；
- 单任务互斥（Walmart 内）；
- 与 Amazon `JOB` 无共享；
- 独立 `OUTPUT_DIR`、profile；
- SSE 心跳 15 秒。

---

## 7. CLI（独立）

```bash
# 基本
python -m walmart_reviews 20951672018 --headful

# 星级筛选
python -m walmart_reviews 20951672018 --stars 5,4 --headful \
  --format csv,json,xlsx --out output/walmart --max-pages 200

# 批量 + 链接
python -m walmart_reviews 20951672018 123456789 \
  "https://www.walmart.com/ip/.../20951672018"
```

参数：

| 参数 | 说明 |
|---|---|
| `--stars` | `5,4,3,2,1` 子集；默认全部 |
| `--max-pages` | 默认 200 |
| `--format` | `csv,json,xlsx` |
| `--out` | 默认 `output/walmart` |
| `--no-resume` / `--keep-checkpoint` | 断点控制 |
| `-v` / `-q` | 日志 |

- 强制 headful；
- 不支持 `--domain`、`--retailer`、`--cookies-from-browser`、`--engine http`；
- Amazon CLI 不动。

---

## 8. 批量（Walmart 内部）

- 一个任务多个 item ID，串行；
- 一个浏览器 profile 复用；
- 每个产品独立 `WalmartStore`；
- 失败隔离；
- 全部结束写合并文件；
- 不做混合批量；
- 两站任务可同时开但不建议。

---

## 9. 覆盖率与错误

```
文字评论覆盖率 = unique reviews / reviews_reported_by_walmart
评分覆盖率   = unique ratings / ratings_reported_by_walmart
```

| 情况 | 行为 |
|---|---|
| 404 | 产品失败 |
| 挑战超时 | 产品失败，提示人工校验 |
| 文字评论 0 | 失败/警告；仍保留评分汇总 |
| 达到 max_pages | `page_limit_reached=True`，提示不完整 |
| 无文字评分 | 入库前过滤，不生成行；评分汇总仍保留 |
| 星级筛选 | 文字评论分母用对应星级的 `reviewsWithTextRatingXCount`；评分汇总另用 `ratingValueXCount` |

---

## 10. 字段映射

| 输出列 | Walmart 字段 | 转换 |
|---|---|---|
| `review_id` | `reviewId` | 缺失用 `reviewReferenceId`；评分记录用接口真实 ID |
| `asin` | item ID | 校验 `review.itemId` |
| `title` | `reviewTitle` | `null` → `""` |
| `body` | `reviewText` | `null` → `""`；验证截断 |
| `rating` | `rating` | `int` → `float` |
| `author` | `userNickname` | 可能 `anonymous` |
| `author_id` | `authorId` | |
| `review_date` | `reviewSubmissionTime` | `%m/%d/%Y` → ISO |
| `country` | `userLocation` | 可空 |
| `verified_purchase` | `VerifiedPurchaser` badge | 布尔 |
| `vine_review` | — | `False` |
| `helpful_votes` | `positiveFeedback` | 默认 0 |
| `variant` | `itemVariantInfo` / `itemName` | P0 |
| `images` | `photos` / `media` | P0 |
| `permalink` | — | 评论页 + `#review-{id}`，P0 |
| `found_via` | — | `walmart/{sort}/p{page}` |
| `seller` | `sellerName` | 新列 |
| `source` | `externalSource` | 新列 |

无文字评分不生成行；缺失字段留空，不猜。

---

## 11. 分阶段实施计划

| 阶段 | 内容 | 产出 | 预估 | 退出条件 |
|---|---|---|---|---|
| **P0 调研/POC** | 星级筛选是否服务端、分页上限、sort、photos、正文截断、挑战频率、无文字评分过滤 | POC 报告 + fixture | 1–1.5 天 | 星级策略确定；挑战频率可接受 |
| **P1 独立骨架** | 包、blueprint、模板、两段式开关、独立输出/profile/Job | 两页面可切换，抓取为桩 | 0.5–1 天 | Amazon 30 测试全绿 |
| **P2 解析器** | `__NEXT_DATA__`、字段、分页、评分汇总、空正文过滤、block | `parser.py` + 单测 | 1–1.5 天 | fixture 正确 |
| **P3 抓取器** | headful、逐页、星级筛选、挑战、checkpoint | `scraper.py` | 1–1.5 天 | 实测全量、可续跑 |
| **P4 页面** | setup（含星级）、challenge、progress、results（评分分布）、下载 | `walmart.js` + 模板 | 1–1.5 天 | UI 端到端 |
| **P5 CLI/测试/文档** | CLI、隔离检查、README/START/HANDOFF_WALMART、打包 | 发布候选 | 1 天 | 验收全过 |

总计约 **5–7 个开发日**（已排除逐条评分的逆向/第三方工作）。

P0 不再是“逐条评分硬门禁”，但**星级筛选策略**和**挑战频率**仍是 P0 的决策点。

---

## 12. 测试计划

### 12.1 单元测试

| 用例 | 断言 |
|---|---|
| `parse_item_id` 各种形式 | 正确/拒绝 ASIN |
| `parse_next_data` 正常 | 10 条、字段正确 |
| `reviewTitle=null` / `anonymous` | 不崩溃 |
| `positiveFeedback=2` | `helpful_votes=2` |
| 日期 | ISO |
| 分页 `pages` 3 页、`next=null` | `total_pages=3` |
| 计数 273/21 | 分开保存 |
| 评分汇总 | 各星级数量正确 |
| **空正文过滤** | `reviewText` 为 `None`/空/空白 → 不生成行、不计数 |
| **星级筛选** | 只保留所选星级；分母用 `reviewsWithTextRatingXCount` |
| **seller/source** | 映射正确 |
| `Robot or human?` / 404 | `challenge` / `notfound` |
| 导出 | 18 列、CSV/JSON/XLSX、`Ratings` 表 |
| 合并 | 行数/去重正确 |
| 隔离 | 不 import `amazon_reviews` |

### 12.2 路由/开关测试

- `GET /` 含 `/walmart` 链接，Amazon 高亮；
- `GET /walmart` 含 `/` 链接，Walmart 高亮；
- `/walmart/api/status` 初始正常；
- 目录穿越 404；
- Amazon `/api/status` 不受影响。

### 12.3 Fixture

- `__NEXT_DATA__` JSON 片段（2–3 条文字评论）；
- 评分汇总 fixture；
- 最小 HTML 外壳；
- 不提交完整页面。

### 12.4 手动联调

- 20951672018：21 条文字评论、3 页；
- 星级筛选：5 星只出 5 星文字评论；核对 `reviewsWithTextRatingFiveCount`；
- 无文字评分不进结果：核对行数等于 `reviewsWithTextCount`；
- 高评论量商品：翻页上限；
- 触发 challenge：提示/恢复；
- 断点续跑；
- 两页面切换，Amazon 任务不受影响；
- CLI 一次端到端。

### 12.5 回归

- 30 个 Amazon 测试全绿；
- Amazon 输出/命名/checkpoint 不变；
- Amazon 首页仅多开关。

---

## 13. 风险与合规

| 风险 | 影响 | 缓解 |
|---|---|---|
| 无文字评分不可逐条获取 | 已确认不做，不算风险 | 只导出文字评论；评分只做汇总 |
| 星级筛选是前端 | 无法减少请求 | 本地过滤，抓全量后筛 |
| PerimeterX 挑战 | 中断 | headful、profile、慢速、人工门禁 |
| 分页上限 | 覆盖不全 | `max_pages` + 明确提示 |
| JSON 结构变化 | 0 条 | 严格校验，缺失报错 |
| 评分数/评论数混淆 | 覆盖率错误 | 分母分开 |
| 代码重复 | 维护成本 | 独立测试/HANDOFF_WALMART |
| 两站同时跑 | 风控叠加 | UI 提示不建议 |
| seller/source 新列 | 与 Amazon 文件列不同 | 独立目录，文档说明 |
| ToS | 账号/IP | 低频个人研究，不自动破解 |

> `robots.txt` 允许路径不等于 ToS 允许；本项目只抓公开评论页，不接登录/付费接口。

---

## 14. 验收标准（DoD）

- [ ] 右上角 `[Amazon | Walmart]` 两段式开关，两页互相跳转，当前项高亮；
- [ ] Amazon 页面除开关外无变化，30 个测试全绿；
- [ ] Walmart 页面布局与 Amazon 对齐（setup/challenge/progress/results/downloads）；
- [ ] 输入 item ID 可抓取文字评论；20951672018 得到 21 条、3 页；
- [ ] 星级筛选生效（服务端或本地），文字评论分母正确；
- [ ] **无文字评分不生成行**，结果行数等于文字评论数；
- [ ] CSV/JSON/XLSX 含 `seller` / `source`；
- [ ] 评分汇总（273 总数、平均分、各星级数量）正确；
- [ ] 挑战提示/恢复/超时正确；
- [ ] 输出只在 `output/walmart/`，独立 profile/Job/SSE；
- [ ] `walmart_reviews` 不 import `amazon_reviews`；
- [ ] `python -m walmart_reviews` 端到端可用；
- [ ] README/START HERE/HANDOFF_WALMART 更新；
- [ ] exe 包含 Walmart 模块。

---

## 15. 待确认/需你决策

1. ✅ **评分口径（已确认）**：只导出有文字的评价；无文字评分过滤掉，不逐条导出；评分只做汇总。
2. ✅ **星级筛选兜底（已确认）**：优先服务端 `?ratings=N`；若只是前端过滤，抓全量后本地筛选。
3. ✅ **新增列（已确认）**：追加 `seller` / `source` 两列；不新增 `record_type`。
4. ✅ **规模（已确认）**：单批 ≤20 个 item，串行，支持断点续跑。
5. ✅ **headful（已确认）**：可接受。
6. ✅ **人工校验（已确认）**：采用 A 人机配合；程序检测、提示、暂停、恢复；不自动破解，v1 不接第三方数据服务。

全部关键决策已确认，可以进入 P0。

---

## 16. 明确不做（v1）

- 伪造无文字评分记录；
- 自动破解 Walmart 校验（含自动按住按钮、模拟鼠标、打码平台）；
- v1 接入 Bazaarvoice/第三方数据服务；
- 代理/指纹伪装；
- Amazon + Walmart 混合批量；
- UPC/SKU 反查；
- 卖家评论/Q&A/其他国家站；
- 图片文件下载；
- 定时任务/增量监控；
- 把 Walmart 写入 Amazon 目录/合并文件。

---

## 17. 调研证据与参考

- 评论页：[Walmart Reviews — 20951672018](https://www.walmart.com/reviews/product/20951672018)
- 商品页：[Walmart Item — 20951672018](https://www.walmart.com/ip/Apple-AirPods-5/20951672018)
- [Walmart robots.txt](https://www.walmart.com/robots.txt)（`Allow: /reviews/product/`）
- 本机实测（2026-10-23）：
  - headful 第 1 页：`273 ratings | 21 reviews`，JSON 路径 `props.pageProps.initialData.data.reviews`；
  - 第 2 页：`currentSpan = "11-20"`，`pages` 含 1/2/3，`next = null`；
  - 点击 5 星后 URL 变为 `?ratings=5`；
  - 直接加载 `?ratings=5` 时遇到 `/blocked`，服务端过滤结果未确认；
  - headless：`/blocked`（`Robot or human?`）；
  - `requests`：HTTP 412，PerimeterX；
  - 连续请求数页后 headful 也被挑战。
- 参考（商业服务，仅交叉验证，不引入依赖）：
  - [Apify Walmart Reviews Scraper](https://apify.com/knotless_cadence/walmart-reviews-scraper)
  - [SerpApi Walmart Product Reviews API](https://serpapi.com/walmart-product-reviews-api)

---

## 18. 机器人校验方案调研（2026-10-23）

结论：**没有“免费 + 稳定 + 合规 + 全自动”的一键方案**。现实可选的是四类，按推荐顺序：

| 方案 | 无人值守 | 成本 | 合规/ToS | 稳定性 | 适合场景 |
|---|---|---|---|---|---|
| **A. 人机配合**（当前方案） | 否，偶发按一次 | 免费 | 较低（仍受 ToS 约束） | 中高 | 本地工具、个人研究 |
| **B. 官方/授权接口** | 是 | 账号/合同/授权 | 低（授权范围内） | 高 | 品牌方、Walmart 卖家、Bazaarvoice 客户 |
| **C. 商业数据服务** | 是 | 按请求/订阅付费 | 中高（需看供应商条款与数据权利） | 取决于供应商 | 需要规模/无人值守 |
| **D. 自动绕过**（stealth/打码/代理） | 名义上是 | 开发+代理+打码费用 | 高（通常违反 ToS） | 低（持续对抗） | 不建议 |

### 18.1 官方/授权路线

- Walmart Marketplace 有 Reviews API 文档，但属于**卖家/合作伙伴侧接口**，需要账号与授权，覆盖范围以官方为准，不能当作任意公开商品的评论 API：  
  [Walmart Marketplace — Reviews API overview](https://developer.walmart.com/us-marketplace/docs/reviews-api-overview)
- Walmart 评论后端是 Bazaarvoice（实测 `externalSource: "bazaarvoice"`）。Bazaarvoice Conversations API 可读取 UGC，但需要 **client passkey / 合同授权**，Walmart 的 passkey 不公开：  
  [Bazaarvoice Conversations API](https://developers.bazaarvoice.com/v1.0-ConversationsAPI/docs/home)
- 如果你是 Walmart 卖家或 Bazaarvoice 客户，这是唯一“稳定 + 合规 + 可无人值守”的方向；否则拿不到授权。

### 18.2 商业数据服务

这些服务自己处理代理/风控，按请求收费，通常提供结构化 JSON；适合不想维护浏览器对抗的场景：

- [Apify Walmart Reviews Scraper](https://apify.com/knotless_cadence/walmart-reviews-scraper)
- [SerpApi Walmart Product Reviews API](https://serpapi.com/walmart-product-reviews-api)
- [ScraperAPI — How to scrape Walmart reviews](https://www.scraperapi.com/blog/scrape-walmart-reviews/)

注意：

- 需要实测它们返回的字段、分页、seller/source 是否完整；
- 费用、速率限制、数据授权、是否允许转存都要看条款；
- 使用第三方意味着把商品 URL 发给对方，需你确认。

### 18.3 自动绕过（不建议）

社区确实存在 stealth/undetected 浏览器、打码平台、住宅代理等方案。它们的特点是：

- 需要持续对抗 PerimeterX，今天能过不代表明天能过；
- 通常违反 Walmart ToS，可能封账号/IP；
- 维护成本高，失败时表现为“抓一半停住”，比人机配合更差。

本项目**不实现**这一类方案，只保留“检测 → 提示 → 人工完成 → 自动恢复”。

### 18.4 决定与建议

**已选 A：人机配合。** v1 按“检测 → 提示 → 人工按住按钮 → 恢复后自动继续”实现；不集成自动破解，也不接第三方数据服务。

B/C 保留为未来选项，仅在以下情况再评估：

- 必须无人值守；
- 人机配合的挑战频率高到不可用；
- 有 Bazaarvoice/Walmart 授权，或愿意为商业数据服务付费。

---

## 19. 下一步

1. ✅ 全部决策已确认（§15 第 2、4 项按默认）；
2. 执行 P0：
   - `?ratings=5` 是否服务端过滤；
   - 挑战频率/持续页数/冷却时间；
   - 高评论量商品分页上限；
   - 图片、正文完整性、空正文过滤；
3. P0 通过后按 P1 → P5 实现；
4. P1 完成时先验收“两页切换 + Amazon 零回归”，再继续。

---

## 20. P0 执行记录

### 2026-10-23 第一次探测

- 环境：全新 headful Chrome profile `output/.walmart-poc4`，正常 UA，访问商品 `20951672018`；
- 结果：**第一个请求就被重定向到 `/blocked`，标题 `Robot or human?`**；
- 说明：
  - 连续多次探测后，当前 IP/会话已被 PerimeterX 标记；
  - 即使是 headful + 新 profile，也不能保证第一次就通过；
  - 需要人工在 Chrome 窗口完成一次按住校验，P0 才能继续；
- 未验证项：`?ratings=5` 是否服务端过滤、高评论量商品分页上限、图片/正文完整性；
- 结论：这不是代码问题，是风控状态问题；符合“人机配合”方案的预期，不能靠改代码绕过。

### 2026-10-23 第二次探测（人工校验通过后）

- 人工在 Chrome 窗口完成 press-and-hold，约 **406 秒**后恢复；
- 基础页：`filteredReviewsCount = 21`、`reviewsWithTextCount = 21`、`totalReviewCount = 273`，页 1-3；
- `?ratings=5`：**服务端过滤确认生效** —— `filteredReviewsCount = 12`、`pagination.total = 12`、`pages = [1,2]`，页面上全部是 5 星；
- 结论：
  - 星级筛选可以走服务端 `?ratings=N`；
  - v1 实现仍先采用“抓全量 + 本地过滤”，原因是多星级选择时 checkpoint/分页更简单、行为一致；单星优化留到后续；
  - 人工校验可能要几分钟，**timeout 调整为 900 秒**（原 300 秒会误杀）。
