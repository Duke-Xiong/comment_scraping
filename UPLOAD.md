# 上传到 Java 数据库接口

下载完成后，结果页每个产品旁边会多一个 **Upload** 按钮，合并文件旁边有 **Upload combined**。点击后由本机 Flask 服务转发到 Java 接口，浏览器不直接访问 Java 服务。

原因：

- Java 接口的 token 不会出现在页面里；
- 避免浏览器 CORS；
- 服务端可以分批、重试、加幂等键。

---

## 1. 配置（.env）

复制示例文件：

```bash
cp .env.example .env
```

编辑 `.env`：

```dotenv
# Java 批量上传接口的完整 URL（不是 base URL）
# 必填；留空时 Upload 按钮会提示未配置
REVIEW_UPLOAD_URL=http://127.0.0.1:8080/api/reviews/batch

# 访问 Java 接口的 token；无认证时留空
REVIEW_UPLOAD_TOKEN=replace-with-token

# 认证 header 名称，默认 Authorization
# 如果 Java 使用 X-API-Key，改成 X-API-Key
REVIEW_UPLOAD_AUTH_HEADER=Authorization

# token 前缀，默认 "Bearer "（末尾空格必须保留，所以加引号）
# 自定义 header 不需要前缀时写成空字符串：""
REVIEW_UPLOAD_AUTH_PREFIX="Bearer "

# 请求超时，单位秒，默认 30
REVIEW_UPLOAD_TIMEOUT=30

# 每个请求发送多少条评论，默认 500，范围 1-10000
REVIEW_UPLOAD_BATCH_SIZE=500

# 5xx / 超时重试次数，默认 2，范围 0-5
REVIEW_UPLOAD_RETRIES=2
```

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `REVIEW_UPLOAD_URL` | 是 | 无 | Java 批量上传接口完整 URL |
| `REVIEW_UPLOAD_TOKEN` | 否 | 空 | 访问 token；无认证时留空 |
| `REVIEW_UPLOAD_AUTH_HEADER` | 否 | `Authorization` | 认证 header 名 |
| `REVIEW_UPLOAD_AUTH_PREFIX` | 否 | `"Bearer "` | token 前缀；保留末尾空格 |
| `REVIEW_UPLOAD_TIMEOUT` | 否 | `30` | 秒 |
| `REVIEW_UPLOAD_BATCH_SIZE` | 否 | `500` | 每批条数，1–10000 |
| `REVIEW_UPLOAD_RETRIES` | 否 | `2` | 5xx/超时重试次数，0–5 |

另有 `REVIEW_ENV_FILE`：只能用真实系统环境变量指定另一个 `.env` 路径，不能写在 `.env` 自身里（默认是项目根目录 / exe 同目录的 `.env`）。

然后正常启动：

```bash
uv run python -m webapp.server
```

Windows 复制：

```bat
copy .env.example .env
```

规则：

- `.env` 放在项目根目录；打包后的 exe 放在 exe 同目录；
- 应用启动时自动读取，不需要再 `export`；
- 真实系统环境变量优先于 `.env`，便于部署覆盖；
- `.env` 含 token，**不要提交 git**（`.gitignore` 已忽略）；
- `.env.example` 可以提交，里面只有占位值。

没有配置 `REVIEW_UPLOAD_URL` 时，按钮仍会显示，但点击会提示：

```text
Upload is not configured. Add REVIEW_UPLOAD_URL to .env (or the environment).
```

---

## 2. 请求格式

Java 端的 DTO 是 `ReviewImportReq`，所以每个批次发送一个对象，评论放在 `reviews` 字段里：

```json
{
  "reviews": [
    {
      "review_id": "422112739",
      "asin": "17556103182",
      "title": "",
      "body": "only one dock worked n I want to get a refund",
      "rating": 1.0,
      "author": "carol",
      "author_id": "6b8745...",
      "review_date": "2026-04-09",
      "country": "",
      "verified_purchase": true,
      "vine_review": false,
      "helpful_votes": 0,
      "variant": "",
      "images": "",
      "permalink": "https://www.walmart.com/reviews/product/17556103182?page=1#review-422112739",
      "found_via": "walmart/relevancy/p1",
      "seller": "Netraty",
      "source": "bazaarvoice"
    }
  ]
}
```

说明：

- 顶层是对象，不是数组；数组放在 `reviews` 字段；
- Walmart 的 `asin` 就是 item ID；`source` 来自页面的 `externalSource`；
- Amazon 的字段结构相同，`source` / `seller` 缺省时由上传模块补为空字符串；
- 每批默认 500 条，可用 `REVIEW_UPLOAD_BATCH_SIZE` 调整；
- 每个批次带以下请求头，Java 可以忽略：
  - `Authorization: Bearer <token>`
  - `Idempotency-Key`
  - `X-Upload-Id`
- 请求库：服务端使用 `requests`，`Content-Type: application/json`。

如果 Java 端要求别的字段名，改一个地方：

```text
webapp/upload_client.py :: build_payload()
```

或改评论行字段映射 `_normalise_rows()`。

## 3. Java 接口需要保证

- 按 `(asin, review_id)` 或至少 `review_id` 做唯一约束/upsert；
- 支持重复提交（幂等），因为网络超时后客户端会重试；
- 返回 2xx 表示该批次成功；
- 4xx 表示请求/数据错误，客户端不会重试；
- 5xx 或超时会自动重试（默认 2 次）；
- 返回 JSON 更好，内容会显示在网页提示里（可选）。

---

## 4. 上传按钮行为

- Amazon 每个产品行：`Upload`
- Amazon 合并文件：`Upload combined`
- Walmart 每个产品行：`Upload`
- Walmart 合并文件：`Upload combined`

行为：

1. 按钮变灰，显示 `Uploading...`；
2. 成功后显示 `Uploaded`，并提示上传条数/批次数；
3. 失败后按钮恢复可点，并显示 Java 返回的错误摘要；
4. 上传不删除本地文件，下载按钮仍可用。

---

## 5. 安全

- `REVIEW_UPLOAD_TOKEN` 只存在本机 `.env` / 进程环境变量中，不写日志、不下发浏览器；
- Java URL 由服务端配置，用户不能通过页面改；
- 上传只允许 `output/` 下由应用生成的 JSON/CSV；
- 不要把 token 写进 `app.js` / `walmart.js`；
- 生产环境建议 Java 侧启用 HTTPS、认证、限流和审计。

---

## 6. 代码位置

```text
webapp/
  upload_client.py   # 读取 JSON/CSV、分批、鉴权、幂等键、requests.post 重试
  server.py          # Amazon POST /api/upload
  walmart_server.py  # Walmart POST /walmart/api/upload
  env.py             # 自动读取 .env
.env.example         # 配置模板
```

---

## 7. 测试

```bash
uv run python tests/test_upload.py
```

覆盖：

- JSON/CSV 读取；
- `.env` 读取与优先级；
- `ReviewImportReq` 对象（`reviews` 字段）；
- 分批；
- Authorization；
- 幂等键；
- 未配置 URL；
- Amazon 上传路由；
- Walmart 上传路由；
- 非法 ASIN。
