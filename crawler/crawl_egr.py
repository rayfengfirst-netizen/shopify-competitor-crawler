#!/usr/bin/env python3
"""
SPELAB 全站爬虫：爬取 https://www.spelabautoparts.com/ 所有页面，
记录每页的 title 和 URL，并保存到文件。
"""

import gzip
import json
import logging
import os
import queue
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# 从项目根目录 .env 加载环境变量（含 SCRAPER_API_KEY），无需每次 export
try:
    from dotenv import load_dotenv
    _root = Path(__file__).resolve().parent.parent
    load_dotenv(_root / ".env")
except Exception:
    pass

# 配置
BASE_URL = "https://www.spelabautoparts.com/"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
REQUEST_DELAY = 1.0  # 基础请求间隔（秒），实际由全局速率限制器控制
WORKERS = 2  # 并发线程数
TIMEOUT = 15
RETRY_429_DELAY = 45  # 无 Retry-After 时默认等待秒数
RATE_LIMIT_INITIAL = 1.5  # 全局：任意两请求之间最少间隔（秒），单 IP 下避免 429
RATE_LIMIT_MAX = 10  # 遇 429 后自动加长间隔的上限（秒）
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# 全局速率限制器，由 crawl() 设置，fetch_page 使用（单 IP 下控制全进程请求频率，避免 429）
_rate_limiter: Optional["RateLimiter"] = None
# 当前爬取使用的域名（用于 is_same_domain），由 crawl(base_url=...) 设置
_current_domain: Optional[str] = None

# 多 IP：从环境变量或 .env 读取，可直接用的服务见 README
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "").strip()  # 设后走 ScraperAPI，自动多 IP
EGR_PROXY_URL = os.environ.get("EGR_PROXY_URL", "").strip() or os.environ.get("HTTP_PROXY", "").strip()


def _request_options(target_url: str) -> Tuple[str, Dict[str, Any], Optional[str]]:
    """
    返回 (实际请求 URL, requests.get 的 kwargs 如 proxies/headers, 若走 ScraperAPI 则返回原始 target 作为 final_url)。
    多 IP 优先 ScraperAPI，其次代理；无则直连。
    """
    if SCRAPER_API_KEY:
        # ScraperAPI：请求他们的 API，他们用多 IP 去抓 target_url，返回目标页内容
        actual_url = f"http://api.scraperapi.com/?api_key={SCRAPER_API_KEY}&url={quote(target_url, safe='')}"
        return actual_url, {}, target_url
    proxies: Dict[str, Any] = {}
    if EGR_PROXY_URL:
        proxies = {"http": EGR_PROXY_URL, "https": EGR_PROXY_URL}
    return target_url, {"proxies": proxies} if proxies else {}, None


def _do_get(url: str, params: Optional[dict] = None, allow_redirects: bool = True) -> Tuple[requests.Response, Optional[str]]:
    """
    发 GET，走代理或 ScraperAPI（若已配置）。返回 (response, original_url_for_final)。
    """
    full_target = url
    if params:
        full_target = url + ("&" if "?" in url else "?") + urlencode(params)
    actual_url, kwargs, original_for_final = _request_options(full_target)
    kwargs.setdefault("headers", HEADERS)
    kwargs.setdefault("timeout", TIMEOUT)
    kwargs.setdefault("allow_redirects", allow_redirects)
    r = requests.get(actual_url, **kwargs)
    return r, original_for_final


class RateLimiter:
    """全进程共享：保证任意两次请求之间至少间隔 current_delay 秒；遇 429 时自动加大间隔。"""

    def __init__(self, initial_delay: float = RATE_LIMIT_INITIAL, max_delay: float = RATE_LIMIT_MAX):
        self._lock = threading.Lock()
        self._last_time = 0.0
        self._current_delay = initial_delay
        self._max_delay = max_delay

    def wait(self) -> None:
        with self._lock:
            now = time.time()
            wait = self._current_delay - (now - self._last_time)
            if wait > 0:
                time.sleep(wait)
            self._last_time = time.time()

    def increase_delay(self) -> None:
        with self._lock:
            self._current_delay = min(self._max_delay, self._current_delay * 1.5)
            logger.warning("限流: 已加大请求间隔至 %.1fs", self._current_delay)


def normalize_url(url: str, keep_query: bool = True) -> str:
    """统一 URL 格式：去掉 fragment，路径末尾斜杠统一。keep_query=True 时保留查询串，便于深度爬取分页等。"""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    if keep_query and parsed.query:
        return f"{parsed.scheme}://{parsed.netloc}{path}?{parsed.query}"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def is_same_domain(url: str) -> bool:
    """判断是否为本站链接。使用 crawl() 设置的 _current_domain（由 base_url 解析）。"""
    global _current_domain
    parsed = urlparse(url)
    domain = (parsed.netloc or "").lower().replace("www.", "")
    key = (_current_domain or "").lower().replace("www.", "")
    if not key:
        return False
    return (key in domain or domain == key) and not domain.startswith("cdn")


# Sitemap 常见路径，用于自动发现（含 Shopify 等常见分片名）
SITEMAP_CANDIDATES = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap_index.xml.gz",
    "/sitemap/sitemap.xml",
    "/sitemap.xml.gz",
    "/sitemap_products_1.xml",
    "/sitemap_pages_1.xml",
    "/sitemap_blogs_1.xml",
    "/sitemap_collections_1.xml",
    "/sitemap_products_1.xml.gz",
    "/sitemap_pages_1.xml.gz",
]


def _parse_sitemap_xml(content: bytes, base_url: str) -> Tuple[List[str], List[str]]:
    """
    解析 sitemap XML，返回 (本页中的页面 URL 列表, 子 sitemap 的 URL 列表)。
    支持 sitemap index 和 urlset，兼容带命名空间的 XML。
    """
    page_urls: List[str] = []
    child_sitemaps: List[str] = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return page_urls, child_sitemaps
    # 带命名空间时 tag 形如 {http://www.sitemaps.org/schemas/sitemap/0.9}loc
    for elem in root.iter():
        tag = (elem.tag or "").split("}")[-1]
        if tag != "loc":
            continue
        text = (elem.text or "").strip()
        if not text or not is_same_domain(text):
            continue
        # 子 sitemap 的 loc 入 child_sitemaps，其余当页面 URL
        if "sitemap" in text.lower():
            child_sitemaps.append(text)
        else:
            page_urls.append(normalize_url(text))
    return list(dict.fromkeys(page_urls)), list(dict.fromkeys(child_sitemaps))


def fetch_sitemap_urls(base_url: str, max_sitemaps: int = 50) -> List[str]:
    """
    从 base_url 尝试常见 sitemap 路径及 robots.txt，收集本站所有页面 URL（深度爬取种子）。
    返回去重后的 URL 列表。
    """
    base = base_url.rstrip("/")
    all_urls: List[str] = []
    seen_urls = set()
    to_fetch: List[str] = []
    for path in SITEMAP_CANDIDATES:
        to_fetch.append(base + path)
    # 尝试从 robots.txt 取 Sitemap（走代理/ScraperAPI 若已配置）
    try:
        r, _ = _do_get(f"{base}/robots.txt")
        if r.ok and "sitemap" in (r.text or "").lower():
            for line in r.text.splitlines():
                line = line.strip()
                if line.lower().startswith("sitemap:"):
                    sitemap_url = line.split(":", 1)[1].strip()
                    if sitemap_url and sitemap_url not in to_fetch:
                        to_fetch.append(sitemap_url)
    except Exception:
        pass
    fetched_sitemaps = 0
    while to_fetch and fetched_sitemaps < max_sitemaps:
        url = to_fetch.pop(0)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        raw = None
        for attempt in range(2):  # 重试一次，应对 500 等临时错误
            try:
                r, _ = _do_get(url)
                r.raise_for_status()
                raw = r.content
                if url.endswith(".gz"):
                    try:
                        raw = gzip.decompress(raw)
                    except Exception:
                        pass
                break
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if attempt == 0 and status is not None and 500 <= status < 600:
                    time.sleep(2)
                    continue
                logger.warning("sitemap 跳过 %s: %s", url, e)
                break
        if raw is None:
            continue
        try:
            page_urls, child_sitemaps = _parse_sitemap_xml(raw, base)
            for u in page_urls:
                if u not in seen_urls and is_same_domain(u):
                    all_urls.append(u)
                    seen_urls.add(u)
            for child in child_sitemaps:
                if child not in seen_urls:
                    to_fetch.append(child)
            fetched_sitemaps += 1
        except Exception as e:
            logger.warning("sitemap 解析跳过 %s: %s", url, e)
    return list(dict.fromkeys(all_urls))


def fetch_wayback_urls(base_url: str, max_results: int = 500) -> List[str]:
    """
    从 archive.org 的 CDX API 拉取该域名历史上被收录过的 URL 作为种子（免费、无需 key）。
    适合 sitemap 不全或想补历史页面的情况。
    """
    parsed = urlparse(base_url)
    domain = (parsed.netloc or "").strip()
    if not domain:
        return []
    try:
        # collapse=urlkey 去重，limit 限制条数
        r = requests.get(
            "https://web.archive.org/cdx/search/cdx",
            params={
                "url": f"{domain}/*",
                "output": "json",
                "collapse": "urlkey",
                "limit": max_results,
            },
            timeout=25,
        )
        r.raise_for_status()
        data = r.json()
        if not data or len(data) < 2:
            return []
        # 第一行是表头，找到 original 列下标（CDX 列为 urlkey,timestamp,original,...）
        header = data[0]
        try:
            orig_idx = header.index("original")
        except ValueError:
            orig_idx = 2
        out: List[str] = []
        for row in data[1:]:
            if orig_idx < len(row) and row[orig_idx]:
                u = normalize_url(row[orig_idx].strip())
                if is_same_domain(u):
                    out.append(u)
        logger.info("Wayback CDX 发现 %d 个 URL（域名 %s）", len(out), domain)
        return list(dict.fromkeys(out))
    except Exception as e:
        logger.warning("Wayback CDX 请求失败: %s", e)
        return []


def fetch_feed_urls(base_url: str) -> List[str]:
    """
    尝试常见 RSS/Atom 路径，解析出文章链接作为种子（博客类站点常用）。
    """
    base = base_url.rstrip("/")
    candidates = ["/feed", "/rss", "/rss.xml", "/feed.xml", "/blog/feed", "/blogs/feed", "/atom.xml", "/feeds/posts/default"]
    out: List[str] = []
    for path in candidates:
        url = base + path
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if not r.ok:
                continue
            r.encoding = r.apparent_encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")
            # RSS <item><link>...</link></item>
            for tag in soup.select("item link"):
                t = (tag.get_text() or "").strip()
                if t:
                    u = normalize_url(urljoin(url, t) if not t.startswith("http") else t)
                    if is_same_domain(u):
                        out.append(u)
            # Atom <entry><link href="..."/>（跳过 rel=self 的 feed 自身链接）
            for tag in soup.find_all("link", href=True):
                h = (tag.get("href") or "").strip()
                if not h:
                    continue
                rel = (tag.get("rel") or "").lower()
                if "self" in rel or "feed" in rel:
                    continue
                u = normalize_url(urljoin(url, h) if not h.startswith("http") else h)
                if is_same_domain(u):
                    out.append(u)
            if out:
                break
        except Exception:
            continue
    if out:
        logger.info("Feed 发现 %d 个 URL", len(out))
    return list(dict.fromkeys(out))


# Google Custom Search API：用 site: 查询补全种子（需配置 GOOGLE_CSE_API_KEY + GOOGLE_CSE_CX）
GOOGLE_CSE_API_KEY = os.environ.get("GOOGLE_CSE_API_KEY", "").strip()
GOOGLE_CSE_CX = os.environ.get("GOOGLE_CSE_CX", "").strip()
GOOGLE_CSE_DELAY = 0.5  # 每页请求间隔，避免触发配额限制


def fetch_google_site_urls(base_url: str, max_results: int = 200) -> List[str]:
    """
    通过 Google Custom Search API 查询 site:域名，把谷歌收录的 URL 作为种子补全。
    需在环境变量或 .env 中配置 GOOGLE_CSE_API_KEY、GOOGLE_CSE_CX；未配置则返回 []。
    max_results: 最多取多少条（每页 10 条，免费约 100 次/天，约 1000 条/天）。
    """
    if not GOOGLE_CSE_API_KEY or not GOOGLE_CSE_CX:
        logger.info("未配置 GOOGLE_CSE_API_KEY / GOOGLE_CSE_CX，跳过 Google site 发现")
        return []
    parsed = urlparse(base_url)
    domain = (parsed.netloc or "").lower().lstrip("www.")
    if not domain:
        return []
    query = f"site:{domain}"
    api_url = "https://www.googleapis.com/customsearch/v1"
    all_urls: List[str] = []
    seen = set()
    start = 1
    num = 10
    while len(all_urls) < max_results:
        try:
            r = requests.get(
                api_url,
                params={
                    "key": GOOGLE_CSE_API_KEY,
                    "cx": GOOGLE_CSE_CX,
                    "q": query,
                    "start": start,
                    "num": num,
                },
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            items = data.get("items") or []
            if not items:
                break
            for item in items:
                link = (item.get("link") or "").strip()
                if not link or link in seen:
                    continue
                # 只保留本站（与 is_same_domain 一致：同主域且排除 cdn）
                p = urlparse(link)
                netloc = (p.netloc or "").lower()
                netloc_no_www = netloc.lstrip("www.")
                if netloc_no_www != domain and domain not in netloc:
                    continue
                if "cdn" in netloc:
                    continue
                seen.add(link)
                all_urls.append(normalize_url(link))
            if len(all_urls) >= max_results:
                break
            # 下一页
            next_info = (data.get("queries") or {}).get("nextPage")
            if not next_info or not next_info:
                break
            start = next_info[0].get("startIndex", start + num)
            if start > 100:
                break
            time.sleep(GOOGLE_CSE_DELAY)
        except Exception as e:
            logger.warning("Google site 查询异常: %s", e)
            break
    logger.info("Google site:%s 发现 %d 个 URL", domain, len(all_urls))
    return list(dict.fromkeys(all_urls))


def _get_with_429_retry(url: str, params: Optional[dict] = None) -> Optional[requests.Response]:
    """请求一次，经全局速率限制；若 429 则按 Retry-After 或默认等待后重试，并加大后续间隔。支持代理/ScraperAPI。"""
    global _rate_limiter
    for attempt in range(2):
        if _rate_limiter:
            _rate_limiter.wait()
        try:
            r, _ = _do_get(url, params=params)
            if r.status_code == 429 and attempt == 0:
                wait_s = _wait_429_retry(r)
                logger.warning("429 限流: 等待 %ds 后重试 API", wait_s)
                time.sleep(wait_s)
                if _rate_limiter:
                    _rate_limiter.increase_delay()
                continue
            return r
        except Exception:
            return None
    return None


def fetch_shopify_api_urls(base_url: str, max_product_pages: int = 20) -> List[str]:
    """
    针对 Shopify 站点：通过 products.json / collections.json 拉取商品与分类 handle。
    请求前先等待几秒避免刚启动即 429；遇 429 会等待后重试一次。
    """
    base = base_url.rstrip("/")
    out: List[str] = []
    # 先等几秒再发请求，避免上次爬取刚触发限流后立刻又打满
    logger.info("等待 5s 再请求 API，降低 429 概率")
    time.sleep(5)
    # 1) 商品列表：/products.json?limit=250&page=1, 2, ...
    try:
        for page in range(1, max_product_pages + 1):
            r = _get_with_429_retry(f"{base}/products.json", params={"limit": 250, "page": page})
            if r is None or not r.ok:
                if r is not None and r.status_code == 429:
                    logger.warning("Shopify products.json 仍 429，跳过后续商品页")
                break
            data = r.json()
            products = data.get("products") or []
            if not products:
                break
            for p in products:
                h = (p.get("handle") or "").strip()
                if h:
                    out.append(f"{base}/products/{h}")
            time.sleep(REQUEST_DELAY)
    except Exception as e:
        logger.warning("Shopify products.json 跳过: %s", e)
    # 2) 分类列表：/collections.json
    try:
        time.sleep(REQUEST_DELAY)
        r = _get_with_429_retry(f"{base}/collections.json", params={"limit": 250})
        if r is not None and r.ok:
            data = r.json()
            for c in data.get("collections") or []:
                h = (c.get("handle") or "").strip()
                if h:
                    out.append(f"{base}/collections/{h}")
    except Exception as e:
        logger.warning("Shopify collections.json 跳过: %s", e)
    return list(dict.fromkeys(out))


def url_priority(url: str) -> int:
    """用于优先级队列：0=商品/分类优先，1=博客，2=其他。数值越小越先爬。"""
    path = (urlparse(url).path or "").lower()
    if "/products/" in path or "/collections/" in path:
        return 0
    if "/blogs/" in path or "/blog/" in path.rstrip("/"):
        return 1
    return 2


def get_links(soup: BeautifulSoup, current_url: str) -> List[str]:
    """从页面中提取本站可爬取的链接（保留查询串，便于深度遍历分页等）。"""
    links = set()
    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        full = urljoin(current_url, href)
        full = full.split("#")[0]  # 只去掉锚点，保留查询串
        if is_same_domain(full):
            links.add(normalize_url(full))
    return list(links)


def _wait_429_retry(r: requests.Response) -> int:
    """根据 429 响应决定等待秒数：优先用 Retry-After 头，否则用默认。"""
    retry_after = r.headers.get("Retry-After", "").strip()
    if retry_after.isdigit():
        return min(300, int(retry_after))
    return RETRY_429_DELAY


def fetch_page(url: str) -> Tuple[Optional[str], Optional[str], List[str]]:
    """
    请求单页，返回 (title, 最终 URL, 本站链接列表)。
    先经全局速率限制再发请求；遇 429 按 Retry-After 或默认等待后重试，并自动加大后续间隔。
    """
    global _rate_limiter
    for attempt in range(2):
        if _rate_limiter:
            _rate_limiter.wait()
        try:
            r, original_final = _do_get(url, allow_redirects=True)
            if r.status_code == 429 and attempt == 0:
                wait_s = _wait_429_retry(r)
                logger.warning("429 限流: 等待 %ds 后重试（优先使用服务端 Retry-After）", wait_s)
                time.sleep(wait_s)
                if _rate_limiter:
                    _rate_limiter.increase_delay()
                continue
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")
            title_tag = soup.find("title")
            title = (title_tag.get_text(strip=True) if title_tag else "") or ""
            final_url = normalize_url(original_final if original_final else r.url)
            links = get_links(soup, final_url)
            return (title, final_url, links)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429 and attempt == 0:
                wait_s = _wait_429_retry(e.response)
                logger.warning("429 限流: 等待 %ds 后重试", wait_s)
                time.sleep(wait_s)
                if _rate_limiter:
                    _rate_limiter.increase_delay()
                continue
            logger.warning("跳过 %s: %s", url, e)
            return (None, None, [])
        except Exception as e:
            logger.warning("跳过 %s: %s", url, e)
            return (None, None, [])
    return (None, None, [])


def _worker(q: queue.PriorityQueue, visited: set, lock: threading.Lock, on_page=None, pages_list=None, pages_lock=None):
    """单个工作线程：从优先级队列取 URL（先商品/分类，再博客，再其他），抓取并解析，新链接入队。"""
    while True:
        try:
            pri, url = q.get()
        except (ValueError, TypeError):
            url = None
        if url is None:
            q.task_done()
            return
        with lock:
            if url in visited:
                q.task_done()
                continue
            visited.add(url)

        logger.info("爬取: %s", url)
        title, final_url, links = fetch_page(url)

        if title is not None and final_url is not None:
            rec = {"title": title, "url": final_url}
            if on_page:
                try:
                    on_page(rec)
                except Exception as e:
                    logger.exception("on_page 回调异常: %s", e)
            if pages_list is not None and pages_lock is not None:
                with pages_lock:
                    pages_list.append(rec)
        with lock:
            for link in links:
                if link not in visited:
                    visited.add(link)
                    q.put((url_priority(link), link))
        q.task_done()


def crawl(base_url: str = None, on_page=None, use_sitemap: bool = True, use_shopify_api: bool = False, use_google_site: bool = False, use_wayback: bool = True, use_feeds: bool = True):
    """
    深度爬取全站（蜘蛛式遍历 + 多种种子来源 + 优先级队列：先爬商品/分类）。
    base_url: 目标站首页 URL；默认使用模块内 BASE_URL。
    on_page: 可选回调，每抓到一页调用 on_page({"title": "...", "url": "..."})。
    use_sitemap: 是否从 sitemap/robots.txt 发现 URL。
    use_shopify_api: 是否从 Shopify 的 products.json/collections.json 拉取种子（易触发 429，默认关闭）。
    use_google_site: 是否用 Google Custom Search API 的 site: 查询补全种子（需配置 GOOGLE_CSE_API_KEY、GOOGLE_CSE_CX）。
    use_wayback: 是否从 archive.org CDX 拉取历史 URL 作为种子（免费，默认开）。
    use_feeds: 是否尝试 RSS/Atom 路径发现博客链接（默认开）。
    """
    global _rate_limiter, _current_domain
    url_base = (base_url or BASE_URL).strip().rstrip("/") + "/"
    parsed = urlparse(url_base)
    _current_domain = (parsed.netloc or "").lower()
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        _rate_limiter = RateLimiter(initial_delay=RATE_LIMIT_INITIAL, max_delay=RATE_LIMIT_MAX)
        if SCRAPER_API_KEY:
            logger.info("已启用多 IP：ScraperAPI（环境变量 SCRAPER_API_KEY）")
        elif EGR_PROXY_URL:
            logger.info("已启用多 IP：代理（环境变量 EGR_PROXY_URL / HTTP_PROXY）")

        visited = set()
        lock = threading.Lock()
        q = queue.PriorityQueue()
        pages_list = [] if on_page is None else None
        pages_lock = threading.Lock() if on_page is None else None

        start_url = normalize_url(url_base)
        seed_urls = [start_url]

        if use_sitemap:
            logger.info("正在从 sitemap/robots.txt 发现 URL")
            sitemap_urls = fetch_sitemap_urls(url_base)
            for u in sitemap_urls:
                if u not in seed_urls:
                    seed_urls.append(u)
            n = len(seed_urls)
            logger.info("发现 %d 个种子 URL（含首页）", n)
            if n <= 1:
                logger.info("若仅 1 个，多为目标站 sitemap 不可用，将仅从首页跟链接")

        if use_wayback:
            logger.info("正在从 Wayback (archive.org) 发现 URL")
            wayback_urls = fetch_wayback_urls(url_base)
            added = 0
            for u in wayback_urls:
                if u not in seed_urls:
                    seed_urls.append(u)
                    added += 1
            logger.info("Wayback 新增 %d 个种子，当前共 %d 个", added, len(seed_urls))

        if use_google_site:
            logger.info("正在从 Google site 搜索发现 URL")
            google_urls = fetch_google_site_urls(url_base)
            added = 0
            for u in google_urls:
                if u not in seed_urls:
                    seed_urls.append(u)
                    added += 1
            logger.info("Google site 新增 %d 个种子，当前共 %d 个", added, len(seed_urls))

        if use_feeds:
            feed_urls = fetch_feed_urls(url_base)
            for u in feed_urls:
                if u not in seed_urls:
                    seed_urls.append(u)

        if use_shopify_api:
            logger.info("正在从 Shopify products.json / collections.json 发现商品与分类 URL")
            api_urls = fetch_shopify_api_urls(url_base)
            added = 0
            for u in api_urls:
                nu = normalize_url(u)
                if nu not in seed_urls:
                    seed_urls.append(nu)
                    added += 1
            logger.info("新增 %d 个种子（商品+分类），当前共 %d 个种子", added, len(seed_urls))

        for u in seed_urls:
            q.put((url_priority(u), u))

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            workers = [
                executor.submit(_worker, q, visited, lock, on_page, pages_list, pages_lock)
                for _ in range(WORKERS)
            ]
            q.join()
            for _ in range(WORKERS):
                q.put((999, None))
            q.join()
            for w in workers:
                w.result()

        # 仅在没有使用 on_page（如命令行直接跑）时写 JSON/CSV
        if on_page is None and pages_list:
            seen = set()
            unique = [p for p in pages_list if p["url"] not in seen and not seen.add(p["url"])]
            json_path = OUTPUT_DIR / "egr_pages.json"
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(unique, f, ensure_ascii=False, indent=2)
            logger.info("已保存 %d 条记录到: %s", len(unique), json_path)
            csv_path = OUTPUT_DIR / "egr_pages.csv"
            with open(csv_path, "w", encoding="utf-8-sig") as f:
                f.write("title,url\n")
                for p in unique:
                    title_esc = p["title"].replace('"', '""')
                    f.write(f'"{title_esc}","{p["url"]}"\n')
            logger.info("已保存 CSV 到: %s", csv_path)
        elif on_page is not None:
            logger.info("爬取结束，共 %d 个 URL", len(visited))

        return list(visited)
    finally:
        _current_domain = None


if __name__ == "__main__":
    crawl()
