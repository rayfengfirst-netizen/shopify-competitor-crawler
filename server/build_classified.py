"""
从 pages 表读取所有记录，按 URL 规则分类，写入 pages_classified 并导出。
支持按站点：run_and_return_counts(site_id)。
"""

import json
from pathlib import Path

from server import db
from server.classify import classify_page_type

OUTPUT_BASE = Path(__file__).resolve().parent.parent / "output"


def run_and_return_counts(site_id: str):
    """执行分类并写入表 + 导出文件，返回各类型数量。供 API 调用。"""
    db.init_db(site_id)
    rows = db.get_pages(site_id, limit=99999, offset=0)
    classified = []
    for r in rows:
        page_type = classify_page_type(r["url"])
        classified.append({
            "title": r["title"],
            "url": r["url"],
            "created_at": r.get("created_at") or "",
            "page_type": page_type,
        })
    db.replace_classified(site_id, classified)
    out_dir = OUTPUT_BASE / "sites" / site_id
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "egr_pages_classified.json", "w", encoding="utf-8") as f:
        json.dump(classified, f, ensure_ascii=False, indent=2)
    with open(out_dir / "egr_pages_classified.csv", "w", encoding="utf-8-sig") as f:
        f.write("title,url,created_at,page_type\n")
        for r in classified:
            title_esc = (r["title"] or "").replace('"', '""')
            f.write(f'"{title_esc}","{r["url"]}","{r["created_at"]}","{r["page_type"]}"\n')
    return db.get_classified_counts(site_id)


def main():
    """命令行：默认对 spelabautoparts.com 执行（可改）。"""
    import sys
    site_id = (sys.argv[1] if len(sys.argv) > 1 else "spelabautoparts.com").strip()
    counts = run_and_return_counts(site_id)
    total = sum(counts.values())
    print(f"已分类 {total} 条，站点 {site_id}")
    for t, n in sorted(counts.items()):
        print(f"  {t}: {n}")


if __name__ == "__main__":
    main()
