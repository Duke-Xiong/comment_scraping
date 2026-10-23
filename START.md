# 启动指南

这个文档只讲**怎么把项目跑起来**。更完整的说明见 [README.md](README.md)，Walmart 的行为和坑见 [HANDOFF_WALMART.md](HANDOFF_WALMART.md)。

---

## 1. 前置条件

| 需要 | 说明 |
|---|---|
| Chrome 或 Edge | 项目驱动本机已安装的浏览器，不额外下载 Chromium |
| uv（推荐） | 自动安装固定版本 Python 3.13 和依赖，见下方安装命令 |
| 本机桌面环境 | Walmart 需要可见 Chrome 窗口和人工按住校验；远程无头服务器不可用 |

如果不用 uv，也可以走 Python 3.10+ + `requirements.txt` 的旧方式，见 [README.md](README.md)。

### 安装 uv

macOS / Linux：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows PowerShell：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

安装后重开终端，确认：

```bash
uv --version
```

---

## 2. 最快启动（推荐）

进入项目目录：

```bash
cd amazon-review-downloader-main
```

恢复锁定环境：

```bash
uv sync --frozen
```

启动：

```bash
uv run python -m webapp.server
```

终端会打印实际访问地址，例如：

```text
Ready. Opening http://127.0.0.1:5000 in your browser.
```

打开该地址即可。

- 5000 被占用时会自动换端口，**以终端打印的 URL 为准**；
- 这个终端窗口要一直开着，`Ctrl+C` 结束；
- 不需要手动 `pip install`，也不需要手动创建 `.venv`。

### 双击启动

- macOS / Linux：`./run-uv.sh`
- Windows：双击 `run-uv.bat`

这两个脚本会先执行 `uv sync --frozen`，再启动应用。

---

## 3. 第一次使用

### Amazon 页面

1. 粘贴 ASIN 或商品链接，一行一个；
2. 选择 marketplace；
3. 选择星级；
4. 点 **Start download**；
5. 第一次会打开 Chrome 的 Amazon 登录页，登录（含 OTP）后程序自动继续；
6. 文件保存在 `output/`。

### Walmart 页面

点右上角 **Walmart** 切换到 `/walmart`。

1. 粘贴 Walmart item ID（例如 `20951672018`）或商品链接；
2. 选择星级；
3. 点 **Start download**；
4. 第一次会打开 Chrome；如果看到 **“Robot or human?”**，切到 Chrome，**按住按钮**直到通过；
5. 程序检测到通过后自动继续（最多等 15 分钟）；
6. 文件保存在 `output/walmart/`。

注意：

- Walmart 只导出**有文字**的评价；没写文字的评分会被过滤，但评分总数/平均分/星级分布仍会汇总；
- 不要共用别人的 `output/walmart/.browser-profile`，那里面有登录与校验会话；
- 遇到校验不要连续刷新，越刷越严格。

---

## 4. 命令行（可选）

Walmart：

```bash
uv run python -m walmart_reviews 20951672018 --stars 5,4 --max-pages 200
```

Amazon：

```bash
uv run python -m amazon_reviews B08H2Y91P1 --domain amazon.in \
  --engine browser --headful --coverage standard
```

Walmart 命令行同样会打开 Chrome，需要人工完成校验/登录。

---

## 5. 常见问题

### `uv: command not found`

安装 uv 后**重开终端**，或检查 `~/.local/bin` 是否在 `PATH` 中。

### `uv sync --frozen` 失败

- 检查网络；
- 确认 `uv.lock`、`pyproject.toml`、`.python-version` 没有被改动或删除；
- 不要手改 `uv.lock`。

### 提示 Python 版本不对

`.python-version` 固定为 3.13；uv 会自动下载。若公司网络拦截 Python 下载，可以手动安装 Python 3.13，或临时改 `.python-version` 后重新 `uv lock`。

### 页面打不开 / 404

- 看终端打印的实际端口，不一定是 5000；
- 如果之前开着旧版应用，先 `Ctrl+C` 停掉再重启；
- 确认访问的是 `127.0.0.1`，不是别的机器的 IP。

### 只抓到约 9 条 Amazon 评论

没有登录，或登录的是另一个 marketplace。重新运行并在 Chrome 里登录。

### Walmart 一直卡在 “Robot or human?”

- 切到 Chrome 窗口按住按钮；
- 如果反复出现，先停止，等一段时间再试；
- 不要用无头模式、不要连续刷新、不要同时开多个任务。

### “Could not launch a browser”

安装 Chrome/Edge；或：

```bash
uv run python -m playwright install chromium
```

### 想让局域网其他设备访问

默认只绑定 `127.0.0.1`，局域网访问不到。不要直接把端口暴露到公网；推荐 SSH 隧道：

```bash
ssh -L 8000:127.0.0.1:5000 你的用户名@Mac的局域网IP
```

然后在另一台设备打开 `http://127.0.0.1:8000/`。

注意：Chrome、登录和人工校验仍然发生在运行项目的这台机器上。

---

## 6. 更新与版本锁定

依赖已经锁定在 `uv.lock`，不要随意删除。更新依赖时：

```bash
uv add 包名==版本
uv lock
uv sync
```

只恢复环境、不更新版本：

```bash
uv sync --frozen
```

提交代码时：

- **要提交** `pyproject.toml`、`uv.lock`、`.python-version`；
- **不要提交** `.venv/`、`output/`、`*.browser-profile`、`.checkpoint_*.json`。

---

## 7. 构建可执行文件（可选）

先安装构建依赖：

```bash
uv sync --frozen --extra build
uv run python build_exe.py
```

平台限制：

- 在 macOS 上构建 → `dist/AmazonReviewDownloader`（只能给 macOS 用）；
- 要 Windows `.exe`，必须在 Windows x64 或 GitHub Actions `windows-latest` 上构建；
- Mac 不能直接交叉编译出 Windows exe。

---

## 8. 安全与合规

- 应用只监听本机 `127.0.0.1`，默认局域网不可见；
- 不要把端口映射到公网；
- 当前没有登录认证，局域网共享前必须自己加 token/反向代理；
- 每个用户使用自己的 Amazon/Walmart 账号和 profile；
- 抓取请保持低频、个人研究用途，遵守网站条款；不要自动破解校验。
