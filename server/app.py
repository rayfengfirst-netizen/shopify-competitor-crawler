"""
通用多站点爬虫服务：添加/删除站点，按站点爬取、查看数据、分类、SEO 分析。
运行方式（在项目根目录下）：python3 -m server.app
浏览器访问 http://127.0.0.1:5001
"""

import logging
import shutil
import threading
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from server import db
from server import sites as sites_module
from server.build_classified import run_and_return_counts
from server import lm_client
from server import product_parser
from crawler.crawl_egr import crawl

app = Flask(__name__, static_folder="static")

# 爬取状态（全局一次只能爬一个站点）
_crawl_running = False
_crawl_site_id = None
_crawl_error = None

# SEO 分析状态
_seo_running = False
_seo_site_id = None
_seo_total = 0
_seo_done = 0
_seo_error = None

SEO_BATCH_SIZE = 10

# 商品详情补充状态（后台任务）
_enrich_running = False
_enrich_site_id = None
_enrich_done = 0
_enrich_total = 0
_enrich_error = None
ENRICH_DELAY = 1.5  # 每请求间隔秒数，避免 429


def _run_crawl(site_id: str, use_google_site: bool = False):
    global _crawl_running, _crawl_site_id, _crawl_error
    _crawl_running = True
    _crawl_site_id = site_id
    _crawl_error = None
    try:
        site = sites_module.get_site(site_id)
        if not site:
            _crawl_error = "站点不存在"
            return
        base_url = site["base_url"]
        db.init_db(site_id)

        def on_page(rec):
            db.add_page(site_id, rec["title"], rec["url"])

        crawl(base_url=base_url, on_page=on_page, use_google_site=use_google_site)
    except Exception as e:
        _crawl_error = str(e)
    finally:
        _crawl_running = False
        _crawl_site_id = None


def _run_seo_analyze(site_id: str, page_type: str, limit: int, pages: list):
    global _seo_running, _seo_site_id, _seo_total, _seo_done, _seo_error
    _seo_running = True
    _seo_site_id = site_id
    _seo_total = len(pages)
    _seo_done = 0
    _seo_error = None
    try:
        db.clear_seo_analysis(site_id)
        for i in range(0, len(pages), SEO_BATCH_SIZE):
            batch = pages[i : i + SEO_BATCH_SIZE]
            results = []
            for p in batch:
                out = lm_client.analyze_seo_title(p.get("title") or "")
                results.append({
                    "title": p.get("title"),
                    "url": p.get("url"),
                    "page_type": p.get("page_type"),
                    "score": out.get("score"),
                    "suggestions": out.get("suggestions") or [],
                    "alternatives": out.get("alternatives") or [],
                    "error": out.get("error"),
                })
            db.insert_seo_analysis_batch(site_id, results)
            _seo_done += len(results)
    except Exception as e:
        _seo_error = str(e)
    finally:
        _seo_running = False
        _seo_site_id = None


def _run_enrich(site_id: str):
    """后台：拉取该站点产品页 URL，逐条解析并写入 product_details；已落库的 URL 直接跳过，不重复请求。"""
    global _enrich_running, _enrich_site_id, _enrich_done, _enrich_total, _enrich_error
    import time
    _enrich_running = True
    _enrich_site_id = site_id
    _enrich_done = 0
    _enrich_total = 0
    _enrich_error = None
    try:
        db.init_db(site_id)
        rows = db.get_pages_classified(site_id, limit=10000, page_type="product")
        _enrich_total = len(rows)
        for i, row in enumerate(rows):
            url = (row.get("url") or "").strip()
            if not url:
                continue
            # 已落库有数据的直接跳过，不重复请求
            if db.get_product_detail(site_id, url) is not None:
                _enrich_done = i + 1
                continue
            try:
                parsed = product_parser.parse_product_page(url)
                db.upsert_product_detail(
                    site_id,
                    url=parsed.get("url") or url,
                    title_seo=parsed.get("title_seo"),
                    title_product=parsed.get("title_product"),
                    description=parsed.get("description"),
                    price=parsed.get("price"),
                    image_url=parsed.get("image_url"),
                    rating_count=parsed.get("rating_count"),
                    rating_value=parsed.get("rating_value"),
                )
            except Exception as e:
                logging.getLogger(__name__).warning("补充商品详情失败 %s: %s", url, e)
            _enrich_done = i + 1
            time.sleep(ENRICH_DELAY)
    except Exception as e:
        _enrich_error = str(e)
    finally:
        _enrich_running = False
        _enrich_site_id = None


def _get_site_id():
    """从 query 或 JSON body 取 site_id，缺则返回 None。"""
    sid = request.args.get("site_id", "").strip()
    if not sid and request.is_json:
        sid = (request.get_json(silent=True) or {}).get("site_id", "").strip()
    return sid or None


# ----- 页面 -----
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/classified.html")
def classified_page():
    return send_from_directory(app.static_folder, "classified.html")


@app.route("/seo.html")
def seo_page():
    return send_from_directory(app.static_folder, "seo.html")


@app.route("/products.html")
def products_page():
    return send_from_directory(app.static_folder, "products.html")


# ----- 站点管理 -----
@app.route("/api/sites", methods=["GET"])
def api_sites_list():
    """获取所有站点列表。"""
    try:
        return jsonify({"ok": True, "sites": sites_module.get_sites()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "sites": []}), 200


@app.route("/api/sites", methods=["POST"])
def api_sites_add():
    """添加站点。body: { "base_url": "https://www.example.com/", "name": "可选名称" }"""
    try:
        body = request.get_json(silent=True) or {}
        base_url = (body.get("base_url") or "").strip()
        name = (body.get("name") or "").strip()
        if not base_url:
            return jsonify({"ok": False, "error": "请填写 base_url"}), 400
        site = sites_module.add_site(base_url, name=name or None)
        return jsonify({"ok": True, "site": site})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200


@app.route("/api/sites/<site_id>", methods=["DELETE"])
def api_sites_delete(site_id: str):
    """删除站点及其数据。"""
    try:
        sites_module.delete_site(site_id)
        return jsonify({"ok": True, "message": "已删除"})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200


# ----- 爬取 -----
@app.route("/api/crawl/start", methods=["POST"])
def start_crawl():
    """启动爬取。body: { "site_id": "spelabautoparts.com", "use_google_site": true }"""
    site_id = _get_site_id()
    if not site_id:
        return jsonify({"ok": False, "message": "请提供 site_id"}), 400
    if sites_module.get_site(site_id) is None:
        return jsonify({"ok": False, "message": "站点不存在"}), 404
    global _crawl_running
    if _crawl_running:
        return jsonify({"ok": False, "message": "爬取已在运行中"}), 409
    data = request.get_json(silent=True) or {}
    use_google_site = bool(data.get("use_google_site"))
    threading.Thread(target=_run_crawl, args=(site_id,), kwargs={"use_google_site": use_google_site}, daemon=True).start()
    return jsonify({"ok": True, "message": "已开始爬取", "site_id": site_id})


@app.route("/api/crawl/status")
def crawl_status():
    """爬取状态。可选 ?site_id=xxx，若正在爬取则返回该站点的 count。"""
    site_id = _get_site_id() or _crawl_site_id
    count = db.get_count(site_id) if site_id else 0
    return jsonify({
        "running": _crawl_running,
        "site_id": _crawl_site_id,
        "count": count,
        "error": _crawl_error,
    })


@app.route("/api/pages")
def list_pages():
    """页面列表。必须 ?site_id=xxx"""
    site_id = _get_site_id()
    if not site_id:
        return jsonify({"ok": False, "error": "请提供 site_id", "pages": [], "total": 0}), 400
    try:
        db.init_db(site_id)
        limit = 5000
        try:
            limit = min(int(request.args.get("limit", limit)), 10000)
        except Exception:
            pass
        rows = db.get_pages(site_id, limit=limit)
        return jsonify({"pages": rows, "total": db.get_count(site_id)})
    except Exception as e:
        return jsonify({"pages": [], "total": 0}), 200


# ----- 分类 -----
@app.route("/api/classified/counts")
def classified_counts():
    """按页面类型统计。必须 ?site_id=xxx"""
    site_id = _get_site_id()
    if not site_id:
        return jsonify({}), 200
    try:
        db.init_db(site_id)
        return jsonify(db.get_classified_counts(site_id))
    except Exception:
        return jsonify({}), 200


@app.route("/api/classified/pages")
def classified_pages():
    """分类列表。必须 ?site_id=xxx，可选 page_type"""
    site_id = _get_site_id()
    if not site_id:
        return jsonify({"pages": [], "counts": {}}), 200
    try:
        db.init_db(site_id)
        limit = min(int(request.args.get("limit", 5000)), 10000) if request.args.get("limit") else 5000
        page_type = request.args.get("page_type", "").strip() or None
        rows = db.get_pages_classified(site_id, limit=limit, page_type=page_type)
        counts = db.get_classified_counts(site_id)
        return jsonify({"pages": rows, "counts": counts})
    except Exception:
        return jsonify({"pages": [], "counts": {}}), 200


@app.route("/api/classified/rebuild", methods=["POST"])
def classified_rebuild():
    """重新整理分类。body: { "site_id": "xxx" }"""
    site_id = _get_site_id()
    if not site_id:
        return jsonify({"ok": False, "error": "请提供 site_id"}), 400
    try:
        db.init_db(site_id)
        counts = run_and_return_counts(site_id)
        return jsonify({"ok": True, "counts": counts})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200


# ----- SEO -----
@app.route("/api/seo/analyze", methods=["POST"])
def seo_analyze():
    """启动 SEO 分析。body: { "site_id", "page_type", "limit" }"""
    global _seo_running
    site_id = _get_site_id()
    if not site_id:
        return jsonify({"ok": False, "error": "请提供 site_id"}), 400
    if _seo_running:
        return jsonify({"ok": False, "error": "分析已在运行中，请稍候再试"}), 409
    body = request.get_json(silent=True) or {}
    page_type = (body.get("page_type") or "").strip()
    if not page_type:
        return jsonify({"ok": False, "error": "请提供 page_type"}), 400
    try:
        limit = int(body.get("limit", 10))
        limit = 50000 if limit <= 0 else min(limit, 50000)
    except (TypeError, ValueError):
        limit = 50000
    try:
        db.init_db(site_id)
        pages = db.get_pages_classified(site_id, limit=limit, page_type=page_type)
        if not pages:
            return jsonify({"ok": True, "results": [], "total": 0, "message": "该类型暂无数据"}), 200
        threading.Thread(target=_run_seo_analyze, args=(site_id, page_type, limit, pages), daemon=True).start()
        return jsonify({"ok": True, "message": "分析已启动", "total": len(pages), "results": []})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200


@app.route("/api/seo/status")
def seo_status():
    return jsonify({
        "running": _seo_running,
        "site_id": _seo_site_id,
        "total_expected": _seo_total,
        "total_done": _seo_done,
        "error": _seo_error,
    })


# ----- 商品列表（按站点 + 补充详情） -----
@app.route("/api/products")
def api_products():
    """按站点返回商品列表（产品页 URL + 已补充的详情）。必须 ?site_id=xxx"""
    site_id = _get_site_id()
    if not site_id:
        return jsonify({"ok": False, "error": "请提供 site_id", "products": []}), 400
    try:
        db.init_db(site_id)
        rows = db.get_pages_classified(site_id, limit=10000, page_type="product")
        details_map = {d["url"]: d for d in db.get_all_product_details(site_id)}
        products = []
        for r in rows:
            url = r.get("url") or ""
            d = details_map.get(url)
            products.append({
                "url": url,
                "title_seo": (d.get("title_seo") if d else None) or r.get("title") or "",
                "title_product": (d.get("title_product") if d else None) or "",
                "description": (d.get("description") if d else None) or "",
                "price": (d.get("price") if d else None) or "",
                "image_url": (d.get("image_url") if d else None) or "",
                "rating_count": (d.get("rating_count") if d else None),
                "rating_value": (d.get("rating_value") if d else None) or "",
            })
        return jsonify({"ok": True, "products": products})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "products": []}), 200


@app.route("/api/products/enrich", methods=["POST"])
def api_products_enrich():
    """后台补充当前站点商品详情（解析每条产品页）。body: { "site_id": "xxx" }"""
    site_id = _get_site_id()
    if not site_id:
        return jsonify({"ok": False, "error": "请提供 site_id"}), 400
    global _enrich_running
    if _enrich_running:
        return jsonify({"ok": False, "error": "正在补充其他站点，请稍后再试"}), 409
    try:
        db.init_db(site_id)
        n = len(db.get_pages_classified(site_id, limit=1, page_type="product"))
        if n == 0:
            return jsonify({"ok": False, "error": "该站点暂无产品页数据，请先爬取并整理分类"}), 400
        threading.Thread(target=_run_enrich, args=(site_id,), daemon=True).start()
        return jsonify({"ok": True, "message": "已开始补充商品详情，请稍候刷新列表查看"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200


@app.route("/api/products/enrich/status")
def api_products_enrich_status():
    """补充任务进度，供前端轮询。"""
    return jsonify({
        "running": _enrich_running,
        "site_id": _enrich_site_id,
        "done": _enrich_done,
        "total": _enrich_total,
        "error": _enrich_error,
    })


@app.route("/api/seo/results")
def seo_results():
    """必须 ?site_id=xxx"""
    site_id = _get_site_id()
    if not site_id:
        return jsonify({"ok": False, "error": "请提供 site_id", "results": []}), 400
    try:
        db.init_db(site_id)
        limit = min(max(1, int(request.args.get("limit", 50000))), 50000) if request.args.get("limit") else 50000
        page_type = (request.args.get("page_type") or "").strip() or None
        rows = db.get_seo_analysis(site_id, limit=limit, page_type=page_type)
        return jsonify({"ok": True, "results": rows})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "results": []}), 200


def migrate_legacy_data():
    """若存在旧版 data/egr.db，迁移到 data/sites/spelabautoparts.com/ 并登记，不删除原文件。"""
    legacy_db = Path(__file__).resolve().parent.parent / "data" / "egr.db"
    site_id = "spelabautoparts.com"
    site_dir = Path(__file__).resolve().parent.parent / "data" / "sites" / site_id
    site_db = site_dir / "db.sqlite"
    if not legacy_db.exists():
        return
    if site_db.exists():
        return  # 已迁移过
    sites_list = sites_module._load_sites_list()
    if any(s.get("id") == site_id for s in sites_list):
        return
    site_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(legacy_db, site_db)
    import datetime
    created_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sites_list.append({
        "id": site_id,
        "base_url": "https://www.spelabautoparts.com/",
        "name": "SPELAB Auto Parts",
        "created_at": created_at,
    })
    sites_module._save_sites_list(sites_list)


def main():
    log_file = Path(__file__).resolve().parent.parent / "server.log"
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    # 同时输出到终端，便于查看「已爬取 xxx」等日志
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    migrate_legacy_data()
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)


# 被 gunicorn 等加载时也执行一次迁移（仅首次部署或新机有用）
migrate_legacy_data()

if __name__ == "__main__":
    main()
