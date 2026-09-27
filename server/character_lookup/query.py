"""本地角色库查询。

数据来自 noob-wiki 的 danbooru_character.csv，用 build_db.py 建成
SQLite。查询纯本地、无网络、无 API Key，几毫秒出结果。

只读打开（query_only），多个请求并发查也不会互相干扰。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB = os.environ.get(
    "CHARACTER_DB", str(Path(__file__).with_name("characters.db")))


def _connect(db_path: str = "") -> sqlite3.Connection:
    path = db_path or DEFAULT_DB
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _clean(row: sqlite3.Row) -> dict:
    return {
        "character": row["character"],
        "copyright": row["copyright"],
        "name": row["name"],
        "copyright_name": row["copyright_name"],
        "trigger": row["trigger"],
        "core_tags": row["core_tags"],
        "count": row["count"],
        "url": row["url"],
    }


def _candidate(row: sqlite3.Row) -> str:
    return f"{row['name']} ({row['copyright_name']})"


def is_built(db_path: str = "") -> bool:
    return Path(db_path or DEFAULT_DB).exists()


def best_match(query: str, limit: int = 20,
               db_path: str = "") -> tuple:
    """返回 (最佳匹配 dict | None, 候选列表)。

    匹配顺序：角色 slug 精确 -> trigger 精确 -> search_blob 模糊（按热度排）。
    """
    q = (query or "").strip().strip(",.，。 ")
    if not q:
        return None, []
    if not is_built(db_path):
        return None, []

    conn = _connect(db_path)
    try:
        slug = q.replace(" ", "_")
        row = conn.execute(
            "SELECT * FROM characters WHERE character = ?", (slug,)).fetchone()
        if not row:
            row = conn.execute(
                "SELECT * FROM characters WHERE trigger = ?", (q,)).fetchone()

        if row:
            return _clean(row), []

        cleaned = " ".join(q.replace(",", " ").replace("(", " ")
                           .replace(")", " ").split())
        if not cleaned:
            return None, []
        like = "%" + "%".join(cleaned.split()) + "%"
        rows = conn.execute(
            "SELECT * FROM characters WHERE search_blob LIKE ? "
            "ORDER BY count DESC, name_lower LIMIT ?",
            (like, limit)).fetchall()
        if not rows:
            return None, []
        return _clean(rows[0]), [_candidate(r) for r in rows[1:]]
    finally:
        conn.close()


def lookup(query: str, max_tags: int = 80, db_path: str = "") -> str:
    """返回格式化好的角色参考文本，找不到返回空串。"""
    try:
        best, alts = best_match(query, db_path=db_path)
    except Exception:
        return ""
    if not best:
        return ""

    lines = [
        f"角色: {best['character']}",
        f"作品: {best['copyright']}",
        f"触发词: {best['trigger']}",
    ]
    core = best.get("core_tags") or ""
    if core:
        tags = [t.strip() for t in core.split(",") if t.strip()]
        lines.append("特征标签: " + ", ".join(tags[:max_tags]))
    if alts:
        lines.append("其他候选: " + " / ".join(alts))
    return "\n".join(lines)
