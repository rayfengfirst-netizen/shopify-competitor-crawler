"""
根据 URL 路径规则判断页面类型（适用于 Shopify 等：产品、分类、博客、单页等）。
产品页与分类页严格区分：仅 /products/ 为产品页，/collections/、/collection/、/categories/ 等为分类页。
"""

from urllib.parse import urlparse

# 支持的页面类型（用于展示与筛选）
PAGE_TYPES = [
    "homepage",   # 首页
    "product",    # 产品页 /products/xxx（仅商品详情）
    "collection", # 分类/集合页 /collections/、/collection/、/categories/ 等
    "blog",       # 博客列表或文章 /blogs/xxx
    "page",       # 静态单页 /pages/xxx
    "other",      # 其他（cart、search、政策页等）
]


def classify_page_type(url: str) -> str:
    """
    根据 URL 返回页面类型。
    产品页与分类页分开：只有 /products/ 判为 product；/collections/、/collection/、/categories/、/category/ 等判为 collection。
    """
    if not url or not url.strip():
        return "other"
    parsed = urlparse(url.strip())
    path = (parsed.path or "").strip().lower()
    path = path.rstrip("/") or "/"
    if path == "/":
        return "homepage"
    # 先判分类页（多种常见路径），避免被误判为 product 或 other
    if "/collections/" in path or path.startswith("collections/"):
        return "collection"
    if "/collection/" in path or path.startswith("collection/"):
        return "collection"
    if "/categories/" in path or path.startswith("categories/"):
        return "collection"
    if "/category/" in path or path.startswith("category/"):
        return "collection"
    # 产品页：仅 /products/ 或 products/
    if "/products/" in path or path.startswith("products/"):
        return "product"
    if "/blogs/" in path or path.startswith("blogs/"):
        return "blog"
    if "/pages/" in path or path.startswith("pages/"):
        return "page"
    return "other"
