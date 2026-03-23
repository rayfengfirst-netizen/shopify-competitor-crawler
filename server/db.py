"""
本地 SQLite 数据库：按站点存储爬取到的页面 title 与 url，以及 SEO 分析结果。
每个站点对应 data/sites/<site_id>/db.sqlite。
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SITES_DIR = PROJECT_ROOT / "data" / "sites"


def get_db_path(site_id: str) -> Path:
    """某站点的数据库文件路径。"""
    return SITES_DIR / site_id / "db.sqlite"


def get_connection(site_id: str):
    """获取该站点的数据库连接。"""
    path = get_db_path(site_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)


def init_db(site_id: str) -> None:
    """创建该站点的表（若不存在）。"""
    conn = get_connection(site_id)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pages_classified (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                created_at TEXT,
                page_type TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seo_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                page_type TEXT NOT NULL,
                score INTEGER,
                suggestions TEXT,
                alternatives TEXT,
                error TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS product_details (
                url TEXT PRIMARY KEY,
                title_seo TEXT,
                title_product TEXT,
                description TEXT,
                price TEXT,
                image_url TEXT,
                rating_count INTEGER,
                rating_value TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def add_page(site_id: str, title: str, url: str) -> bool:
    """插入一条页面记录，若 url 已存在则忽略。返回是否插入了新行。"""
    conn = get_connection(site_id)
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO pages (title, url) VALUES (?, ?)", (title, url)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_pages(site_id: str, limit: int = 5000, offset: int = 0):
    """按入库顺序返回列表，最新先。返回 [{"id", "title", "url", "created_at"}, ...]"""
    conn = get_connection(site_id)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, url, created_at FROM pages ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_count(site_id: str) -> int:
    """总条数。"""
    conn = get_connection(site_id)
    try:
        return conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    finally:
        conn.close()


def replace_classified(site_id: str, rows: list) -> None:
    """用新的分类结果全量替换 pages_classified 表。"""
    conn = get_connection(site_id)
    try:
        conn.execute("DELETE FROM pages_classified")
        conn.executemany(
            "INSERT INTO pages_classified (title, url, created_at, page_type) VALUES (?, ?, ?, ?)",
            [(r["title"], r["url"], r.get("created_at"), r["page_type"]) for r in rows],
        )
        conn.commit()
    finally:
        conn.close()


def get_pages_classified(site_id: str, limit: int = 5000, offset: int = 0, page_type: str = None):
    """返回分类后的列表。"""
    conn = None
    try:
        conn = get_connection(site_id)
        conn.row_factory = sqlite3.Row
        if page_type:
            rows = conn.execute(
                """SELECT id, title, url, created_at, page_type FROM pages_classified
                   WHERE page_type = ? ORDER BY page_type, id LIMIT ? OFFSET ?""",
                (page_type, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, title, url, created_at, page_type FROM pages_classified
                   ORDER BY page_type, id LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        if conn:
            conn.close()


def get_classified_counts(site_id: str) -> dict:
    """按 page_type 统计条数。"""
    conn = None
    try:
        conn = get_connection(site_id)
        rows = conn.execute(
            "SELECT page_type, COUNT(*) AS cnt FROM pages_classified GROUP BY page_type"
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}
    finally:
        if conn:
            conn.close()


def clear_seo_analysis(site_id: str) -> None:
    """清空 seo_analysis 表。"""
    conn = get_connection(site_id)
    try:
        conn.execute("DELETE FROM seo_analysis")
        conn.commit()
    finally:
        conn.close()


def insert_seo_analysis_batch(site_id: str, results: list) -> None:
    """追加写入一批 SEO 分析结果。"""
    if not results:
        return
    conn = get_connection(site_id)
    try:
        for r in results:
            suggestions = json.dumps(r.get("suggestions") or [], ensure_ascii=False)
            alternatives = json.dumps(r.get("alternatives") or [], ensure_ascii=False)
            conn.execute(
                """INSERT INTO seo_analysis (title, url, page_type, score, suggestions, alternatives, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    r.get("title") or "",
                    r.get("url") or "",
                    r.get("page_type") or "",
                    r.get("score"),
                    suggestions,
                    alternatives,
                    r.get("error"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def replace_seo_analysis(site_id: str, results: list) -> None:
    """用本次 SEO 分析结果全量覆盖 seo_analysis 表。"""
    conn = get_connection(site_id)
    try:
        conn.execute("DELETE FROM seo_analysis")
        for r in results:
            suggestions = json.dumps(r.get("suggestions") or [], ensure_ascii=False)
            alternatives = json.dumps(r.get("alternatives") or [], ensure_ascii=False)
            conn.execute(
                """INSERT INTO seo_analysis (title, url, page_type, score, suggestions, alternatives, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    r.get("title") or "",
                    r.get("url") or "",
                    r.get("page_type") or "",
                    r.get("score"),
                    suggestions,
                    alternatives,
                    r.get("error"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def get_seo_analysis(site_id: str, limit: int = 50000, page_type: str = None):
    """返回已保存的 SEO 分析列表。"""
    conn = None
    try:
        conn = get_connection(site_id)
        conn.row_factory = sqlite3.Row
        if page_type:
            rows = conn.execute(
                """SELECT id, title, url, page_type, score, suggestions, alternatives, error, created_at
                   FROM seo_analysis WHERE page_type = ? ORDER BY id LIMIT ?""",
                (page_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, title, url, page_type, score, suggestions, alternatives, error, created_at
                   FROM seo_analysis ORDER BY id LIMIT ?""",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            row = dict(r)
            try:
                row["suggestions"] = json.loads(r["suggestions"] or "[]")
            except (TypeError, json.JSONDecodeError):
                row["suggestions"] = []
            try:
                row["alternatives"] = json.loads(r["alternatives"] or "[]")
            except (TypeError, json.JSONDecodeError):
                row["alternatives"] = []
            out.append(row)
        return out
    except Exception:
        return []
    finally:
        if conn:
            conn.close()


def upsert_product_detail(site_id: str, url: str, title_seo: str = None, title_product: str = None,
                          description: str = None, price: str = None, image_url: str = None,
                          rating_count: int = None, rating_value: str = None) -> None:
    """插入或更新一条商品详情。"""
    conn = get_connection(site_id)
    try:
        conn.execute(
            """
            INSERT INTO product_details (url, title_seo, title_product, description, price, image_url, rating_count, rating_value, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
            ON CONFLICT(url) DO UPDATE SET
                title_seo=excluded.title_seo,
                title_product=excluded.title_product,
                description=excluded.description,
                price=excluded.price,
                image_url=excluded.image_url,
                rating_count=excluded.rating_count,
                rating_value=excluded.rating_value,
                updated_at=datetime('now','localtime')
            """,
            (url, title_seo or "", title_product or "", description or "", price or "", image_url or "",
             rating_count, rating_value or ""),
        )
        conn.commit()
    finally:
        conn.close()


def get_product_detail(site_id: str, url: str) -> Optional[dict]:
    """按 url 取一条商品详情，不存在返回 None。"""
    conn = get_connection(site_id)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM product_details WHERE url = ?", (url,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None
    finally:
        conn.close()


def get_all_product_details(site_id: str) -> list:
    """返回该站点所有已补充的商品详情列表。"""
    conn = get_connection(site_id)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT url, title_seo, title_product, description, price, image_url, rating_count, rating_value, created_at, updated_at FROM product_details ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()
