"""
站点注册：添加/删除/列表站点，每个站点对应 data/sites/<site_id>/ 目录与独立数据库。
"""

import json
import re
import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from server import db

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SITES_DIR = PROJECT_ROOT / "data" / "sites"
SITES_JSON = PROJECT_ROOT / "data" / "sites.json"


def _normalize_site_id(base_url: str) -> str:
    """从 base_url 得到唯一 site_id（用于目录名），不含 www。"""
    parsed = urlparse(base_url.strip())
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        return host[4:]
    return host or "unknown"


def _load_sites_list() -> list:
    """读取 sites.json，返回 sites 数组。"""
    SITES_JSON.parent.mkdir(parents=True, exist_ok=True)
    if not SITES_JSON.exists():
        return []
    try:
        with open(SITES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("sites") or []
    except Exception:
        return []


def _save_sites_list(sites: list) -> None:
    with open(SITES_JSON, "w", encoding="utf-8") as f:
        json.dump({"sites": sites}, f, ensure_ascii=False, indent=2)


def get_sites() -> list:
    """
    返回所有站点列表。
    每项: {"id", "base_url", "name", "created_at", "page_count"}（page_count 可能为 0）
    """
    sites = _load_sites_list()
    out = []
    for s in sites:
        sid = s.get("id") or ""
        row = {
            "id": sid,
            "base_url": s.get("base_url") or "",
            "name": s.get("name") or sid,
            "created_at": s.get("created_at") or "",
        }
        try:
            row["page_count"] = db.get_count(sid)
        except Exception:
            row["page_count"] = 0
        out.append(row)
    return out


def add_site(base_url: str, name: str = None) -> dict:
    """
    添加站点：创建 data/sites/<site_id>/ 并初始化数据库，写入 sites.json。
    base_url 如 https://www.example.com/
    返回 {"id", "base_url", "name", "created_at"}，若 id 已存在则抛出 ValueError。
    """
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("请填写站点 URL")
    if not base_url.startswith("http://") and not base_url.startswith("https://"):
        base_url = "https://" + base_url
    base_url = base_url.rstrip("/") + "/"  # 保证末尾有 /

    site_id = _normalize_site_id(base_url)
    if not re.match(r"^[a-z0-9.-]+$", site_id):
        raise ValueError("无效的站点域名")

    sites = _load_sites_list()
    if any(s.get("id") == site_id for s in sites):
        raise ValueError("该站点已存在")

    import datetime
    created_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    site_dir = SITES_DIR / site_id
    site_dir.mkdir(parents=True, exist_ok=True)
    db.init_db(site_id)

    entry = {
        "id": site_id,
        "base_url": base_url,
        "name": (name or "").strip() or site_id,
        "created_at": created_at,
    }
    sites.append(entry)
    _save_sites_list(sites)
    return {**entry, "page_count": 0}


def delete_site(site_id: str) -> None:
    """删除站点：从 sites.json 移除并删除 data/sites/<site_id>/ 目录。"""
    site_id = (site_id or "").strip()
    if not site_id:
        raise ValueError("站点 id 不能为空")

    sites = _load_sites_list()
    sites = [s for s in sites if s.get("id") != site_id]
    if len(sites) == _load_sites_list():
        raise ValueError("站点不存在")
    _save_sites_list(sites)

    site_dir = SITES_DIR / site_id
    if site_dir.exists():
        shutil.rmtree(site_dir)


def get_site(site_id: str) -> Optional[dict]:
    """根据 id 返回站点信息，不存在返回 None。"""
    for s in get_sites():
        if s.get("id") == site_id:
            return s
    return None
