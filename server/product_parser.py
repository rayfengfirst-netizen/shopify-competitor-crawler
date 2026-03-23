"""
解析商品页 HTML，提取：首图、SEO 标题、商品标题、描述、价格、评分、链接。
兼容 Shopify 常见结构（JSON-LD、og 标签、常见 class）。
"""

import json
import logging
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 15


def _first_image_from_json_ld(soup: BeautifulSoup) -> Optional[str]:
    """从页面中的 Product JSON-LD 取第一张图。"""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
            if isinstance(data, dict) and data.get("@type") == "Product":
                img = data.get("image")
                if isinstance(img, str) and img.startswith("http"):
                    return img
                if isinstance(img, list) and len(img) > 0:
                    return img[0] if isinstance(img[0], str) else img[0].get("url")
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        img = item.get("image")
                        if isinstance(img, str) and img.startswith("http"):
                            return img
                        if isinstance(img, list) and len(img) > 0:
                            return img[0] if isinstance(img[0], str) else img[0].get("url")
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _price_from_json_ld(soup: BeautifulSoup) -> Optional[str]:
    """从 Product JSON-LD 的 offers 取价格。"""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
            if isinstance(data, dict) and data.get("@type") == "Product":
                offers = data.get("offers")
                if isinstance(offers, dict) and offers.get("price"):
                    return str(offers.get("price", ""))
                if isinstance(offers, list) and len(offers) > 0 and isinstance(offers[0], dict):
                    return str(offers[0].get("price", ""))
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        offers = item.get("offers")
                        if isinstance(offers, dict) and offers.get("price"):
                            return str(offers.get("price", ""))
                        if isinstance(offers, list) and len(offers) > 0:
                            return str(offers[0].get("price", ""))
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _rating_from_json_ld(soup: BeautifulSoup) -> tuple:
    """从 JSON-LD 的 AggregateRating 取 (rating_value, rating_count)。"""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
            if isinstance(data, dict):
                agg = data.get("aggregateRating") or (data.get("review") and data["review"][0].get("author", {}).get("aggregateRating"))
                if not agg and data.get("@type") == "Product":
                    agg = data.get("aggregateRating")
                if isinstance(agg, dict):
                    val = agg.get("ratingValue")
                    cnt = agg.get("reviewCount") or agg.get("ratingCount")
                    return (str(val) if val is not None else None, int(cnt) if isinstance(cnt, (int, float)) else None)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        agg = item.get("aggregateRating")
                        if isinstance(agg, dict):
                            val = agg.get("ratingValue")
                            cnt = agg.get("reviewCount") or agg.get("ratingCount")
                            return (str(val) if val is not None else None, int(cnt) if isinstance(cnt, (int, float)) else None)
        except (json.JSONDecodeError, TypeError):
            continue
    return (None, None)


def _name_desc_from_json_ld(soup: BeautifulSoup) -> tuple:
    """从 Product JSON-LD 取 (name, description)。"""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
            if isinstance(data, dict) and data.get("@type") == "Product":
                return (data.get("name") or "", data.get("description") or "")
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        return (item.get("name") or "", item.get("description") or "")
        except (json.JSONDecodeError, TypeError):
            continue
    return ("", "")


def parse_product_page(url: str, html: Optional[str] = None) -> dict:
    """
    解析商品页，返回 dict：title_seo, title_product, description, price, image_url, rating_value, rating_count, url。
    若传 html 则不再请求；否则 requests 请求 url。
    """
    out = {
        "url": url,
        "title_seo": "",
        "title_product": "",
        "description": "",
        "price": "",
        "image_url": "",
        "rating_value": "",
        "rating_count": None,
    }
    if html is None:
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            html = r.text
        except Exception as e:
            logger.warning("请求商品页失败 %s: %s", url, e)
            return out
    if not html or not html.strip():
        return out

    soup = BeautifulSoup(html, "html.parser")

    # SEO 标题
    title_tag = soup.find("title")
    out["title_seo"] = (title_tag.get_text(strip=True) if title_tag else "") or ""

    # 首图：JSON-LD -> og:image -> 第一张产品图
    img = _first_image_from_json_ld(soup)
    if not img:
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            img = og.get("content", "").strip()
    if not img:
        for sel in ["meta[property='og:image']", ".product__media img", ".product-single__photo img", "[data-product-image]", ".product-gallery__image img"]:
            tag = soup.select_one(sel)
            if tag:
                if tag.name == "meta":
                    img = tag.get("content", "").strip()
                else:
                    img = tag.get("src") or tag.get("data-src")
                if img and img.startswith("http"):
                    break
    if img and not img.startswith("http") and img.startswith("//"):
        img = "https:" + img
    out["image_url"] = img or ""

    # 商品标题、描述：JSON-LD 优先，再 h1 / 描述区
    name_ld, desc_ld = _name_desc_from_json_ld(soup)
    out["title_product"] = name_ld
    out["description"] = desc_ld
    if not out["title_product"]:
        h1 = soup.find("h1", class_=re.compile(r"product|title", re.I)) or soup.find("h1")
        out["title_product"] = (h1.get_text(strip=True) if h1 else "") or out["title_seo"]
    if not out["description"]:
        desc_node = soup.select_one(".product__description, .product-single__description, [data-product-description], .product-description")
        if desc_node:
            out["description"] = desc_node.get_text(separator=" ", strip=True)[:2000]

    # 价格：JSON-LD -> .price / [data-product-price]，若得到整段文案则只保留金额
    price = _price_from_json_ld(soup)
    if not price:
        price_el = soup.select_one(".price, [data-product-price], .product__price, .product-single__price")
        if price_el:
            price = price_el.get_text(strip=True)
    # 若价格是一大段（如 "Regular price From $699.00 Regular price $995.90 Sale price..."），只取第一个 $ 金额
    if price and len(price) > 50:
        first_price = re.search(r"\$[\d,]+\.?\d*", price)
        if first_price:
            price = first_price.group(0)
        else:
            first_num = re.search(r"[\d,]+\.?\d+", price)
            if first_num:
                price = first_num.group(0)
    out["price"] = (price or "").strip()

    # 评分
    rv, rc = _rating_from_json_ld(soup)
    out["rating_value"] = rv or ""
    out["rating_count"] = rc
    if not out["rating_value"] and not out["rating_count"]:
        rating_el = soup.select_one("[data-rating], .rating, .review-count, .product-reviews-count")
        if rating_el:
            text = rating_el.get_text(strip=True)
            nums = re.findall(r"[\d.]+", text)
            if nums:
                out["rating_value"] = nums[0]
            cnt = re.search(r"(\d+)\s*reviews?", text, re.I)
            if cnt:
                out["rating_count"] = int(cnt.group(1))

    return out
