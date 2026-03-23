# SPELAB

项目说明

**需求与实现说明（结构化中文文档）**：见 [docs/需求与实现说明.md](docs/需求与实现说明.md)，便于后续继续沟通与扩展。

## 简介

SPELAB 项目根目录。当前包含针对 [SPELAB Auto Parts](https://www.spelabautoparts.com/) 的全站爬虫，用于收集所有页面的 title 与 URL。数据会写入**本机 SQLite 数据库**，并可通过**本地前台页面**触发爬取、实时查看抓取结果。

## 本地数据库与前台页面（推荐）

### 安装依赖

```bash
cd SPELAB
pip install -r requirements.txt
```

### 启动本地服务

在项目根目录 **SPELAB** 下执行（本机若无 `python` 命令请用 `python3`）：

```bash
python3 -m server.app
```

浏览器访问：**http://127.0.0.1:5001**（使用 5001 端口，避免与 macOS 的 AirPlay 占用 5000 冲突）

- **开始爬取**：点击按钮后，爬虫在你这台电脑本地运行，无需额外开终端。

### 进程管理（后台常驻，关掉终端也不退出）

希望服务一直在本机跑、不用管，可用 **launchd** 托管（崩溃自动重启、可选开机自启）。

1. **首次**：创建日志目录  
   ```bash
   mkdir -p ~/Library/Logs/SPELAB
   ```

2. **安装**：把项目里的 plist 拷到当前用户目录并加载（新版 macOS 请用 `bootstrap`，不要用已废弃的 `load`）  
   ```bash
   cp /Users/fengchangrui/Desktop/cursor/SPELAB/scripts/com.spelab.server.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.spelab.server.plist
   ```

3. **之后**：服务会一直在后台跑，关掉终端、关掉 Cursor 都不影响。  
   - 查看日志：`tail -f ~/Library/Logs/SPELAB/server.log`  
   - 停止服务：`launchctl bootout gui/$(id -u) com.spelab.server`  
   - 再次启动：`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.spelab.server.plist`  
   - 开机自启：plist 在 `~/Library/LaunchAgents/` 且执行过 `bootstrap` 后，下次登录会随系统自动起来（若不想开机自启，可编辑 plist 把 `RunAtLoad` 改为 `false`）。

若项目路径不是 `/Users/fengchangrui/Desktop/cursor/SPELAB`，需先改 plist 里的 `WorkingDirectory` 和 `StandardOutPath`/`StandardErrorPath` 再拷贝加载。
- **实时查看**：爬取过程中页面会约每 1.5 秒刷新，表格中会持续出现新抓到的 **title** 与 **url**。
- **数据落盘**：所有记录写入本机 **data/egr.db**（SQLite），可长期保留、自行备份或用其他工具查询。

### 数据文件说明

| 路径 | 说明 |
|------|------|
| **data/egr.db** | SQLite 数据库，表 `pages` 原始抓取；表 `pages_classified` 带页面类型 |
| output/egr_pages.json | 仅在使用命令行直接跑爬虫时生成（见下方） |
| output/egr_pages.csv | 同上，便于 Excel 打开 |
| output/egr_pages_classified.json | 整理后的分类数据（含 page_type），见下方「页面分类」 |
| output/egr_pages_classified.csv | 同上，含列 title, url, created_at, page_type |

### 页面分类（区分产品页 / 分类页 / 博客页等）

在现有抓取数据基础上，可按 **URL 规则** 自动区分页面类型，并保存为另一份数据：

- **类型**：`homepage`（首页）、`product`（产品页 `/products/xxx`）、`collection`（分类页 `/collections/xxx`）、`blog`（博客 `/blogs/xxx`）、`page`（单页 `/pages/xxx`）、`other`（其他）。
- **实现**：`server/classify.py` 根据路径规则判断；`server/build_classified.py` 从 `pages` 表读取 → 分类 → 写入表 `pages_classified` 并导出 JSON/CSV。
- **使用**：
  - 命令行（在项目根目录）：`python3 -m server.build_classified`，会生成 `output/egr_pages_classified.json` 与 `.csv`。
  - 接口：`POST /api/classified/rebuild` 重新整理；`GET /api/classified/counts` 查各类型数量；`GET /api/classified/pages?page_type=product` 按类型查列表。

## 仅命令行跑爬虫（可选）

不启动网页时，也可以直接运行脚本，结果只写 JSON/CSV（不写 DB）：

```bash
python3 crawler/crawl_egr.py
```

## 深度爬取（蜘蛛式 + Sitemap，默认不请求 API）

- **蜘蛛式遍历**：爬虫会跟随页面内所有本站链接递归抓取，且**保留 URL 查询串**（如 `?page=2`），分页会当作不同页面抓取。
- **Sitemap 种子**：会尝试从 sitemap、robots.txt 拉取 URL 作为种子；若目标站 sitemap 返回 500 等则自动跳过，不影响后续步骤。
- **默认不请求 Shopify API**：为避免 429 限流，**默认不调用** products.json/collections.json，只靠「首页 + sitemap + 页面内链接」发现 URL，不会因 API 触发限流。
- **可选开启 Shopify API**：若需要更多种子（商品/分类页），可在 `crawler/crawl_egr.py` 里把 `crawl(..., use_shopify_api=True)` 打开（或由 server 传入）；建议间隔几分钟再跑，并接受可能的 429。
- **可选 Google site 搜索补全**：若感觉 sitemap + 蜘蛛仍漏页，可勾选控制台「用 Google site 搜索补全种子」。爬取前会请求 Google Custom Search API（`site:域名`），把谷歌收录的 URL 并入种子，再蜘蛛遍历。需在项目根目录 `.env` 中配置 `GOOGLE_CSE_API_KEY`、`GOOGLE_CSE_CX`（见下方说明）；未配置时该选项不生效。
- **Wayback 种子（默认开）**：从 [archive.org](https://web.archive.org/) 的 CDX API 拉取该域名历史上被收录过的 URL 作为种子，**免费、无需配置**，可补 sitemap 不全或历史页面。可在 `crawl(..., use_wayback=False)` 关闭。
- **RSS/Atom 种子（默认开）**：自动尝试 `/feed`、`/rss`、`/blog/feed` 等常见路径，解析出博客/文章链接并入种子。可在 `crawl(..., use_feeds=False)` 关闭。
- **Sitemap 路径**：除常见 `sitemap.xml` 外，会尝试 Shopify 等常用分片路径（如 `sitemap_products_1.xml`、`sitemap_pages_1.xml`、`sitemap_blogs_1.xml`），提高发现率。
- **蜘蛛优先级队列**：爬取时优先处理「商品/分类页」→「博客页」→「其他」，同一优先级内再按入队顺序，重要页面先出、更快入库。
- 可在 `crawl()` 调用处关闭 sitemap：`use_sitemap=False`。

## 说明

- 请求间隔与并发数在 `crawler/crawl_egr.py` 顶部可调（`REQUEST_DELAY`、`WORKERS`）。
- **单 IP 下避免 429**：不依赖多 IP，采用「全局速率限制 + 遇 429 自动降速」：
  - **全局速率限制**：全进程任意两次请求之间至少间隔 `RATE_LIMIT_INITIAL`（默认 1.5s），多线程也共用这一间隔，避免瞬时并发过高。
  - **尊重 Retry-After**：若服务端返回 429 且带 `Retry-After` 头，按该秒数等待后再重试。
  - **自适应加长间隔**：一旦收到 429，自动把当前间隔乘以 1.5（上限 `RATE_LIMIT_MAX`，默认 10s），后续请求自动变慢，减少再次 429。
- 若仍频繁 429，可把 `RATE_LIMIT_INITIAL` 调到 2～2.5，或把 `WORKERS` 改为 1。

### Google site 搜索补全（可选）

用于发现 sitemap 或蜘蛛可能漏掉的页面。在控制台勾选「用 Google site 搜索补全种子」后，爬取前会调用 Google Custom Search API 查询 `site:你的域名`，将结果 URL 并入种子。

**配置步骤：**

1. **Custom Search Engine（可搜索整站）**：打开 [Programmable Search Engine](https://programmablesearchengine.google.com/)，新建搜索引擎，选择「搜索整个网络」即可（不必只搜某一站）；创建后进入该引擎，复制 **搜索引擎 ID**（即 `cx`）。
2. **API Key**：在 [Google Cloud Console](https://console.cloud.google.com/) 为项目启用「Custom Search API」，在「凭据」中创建 API 密钥。
3. 在项目根目录的 `.env` 中写入：
   ```bash
   GOOGLE_CSE_API_KEY=你的API密钥
   GOOGLE_CSE_CX=你的搜索引擎ID
   ```

免费配额约 100 次查询/天，每次最多 10 条结果，即约 1000 条 URL/天；代码默认最多取 200 条（可改 `crawler/crawl_egr.py` 里 `fetch_google_site_urls(..., max_results=200)`）。

### 多 IP（可选，直接可用的服务）

无需自建代理池，设环境变量即可走「多 IP」请求，减轻单 IP 限流：

| 方式 | 环境变量 | 说明 |
|------|----------|------|
| **ScraperAPI** | `SCRAPER_API_KEY` | 注册 [scraperapi.com](https://www.scraperapi.com/) 拿 API Key，免费额度约 1000 次/月，他们自动多 IP 转发，设 `export SCRAPER_API_KEY=你的key` 后启动爬虫即可。 |
| **任意 HTTP(S) 代理** | `EGR_PROXY_URL` 或 `HTTP_PROXY` | 格式如 `http://user:pass@host:port` 或 `http://host:port`。若使用 Smartproxy、Oxylabs 等旋转代理，把他们的代理地址填进去即可。 |

启动前在终端执行一次即可，例如：
```bash
export SCRAPER_API_KEY=你的key
python3 -m server.app
```
或代理：
```bash
export EGR_PROXY_URL=http://user:pass@gate.smartproxy.com:10000
python3 -m server.app
```

- 若需调整起始 URL、输出路径或关闭 sitemap/Shopify API，可修改 `crawler/crawl_egr.py`。
