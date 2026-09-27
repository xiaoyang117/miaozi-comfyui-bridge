"""本地角色库查询。

数据来自 noob-wiki 的 danbooru_character.csv，用 build_db.py 建成
SQLite。查询纯本地、无网络、无 API Key，几毫秒出结果。

只读打开（query_only），多个请求并发查也不会互相干扰。
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
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


def is_built(db_path: str = "") -> bool:
    return Path(db_path or DEFAULT_DB).exists()


def get_character(character: str, db_path: str = "") -> dict | None:
    if not is_built(db_path):
        return None
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM characters WHERE character = ?", (character,)).fetchone()
        return _clean(row) if row else None


def _normalize(value: str) -> str:
    return " ".join(value.lower().replace("_", " ").split())


def find_candidates(query: str, limit: int = 5,
                    db_path: str = "") -> list[dict]:
    """先按角色名精确检索；作品名用于排除同名角色和错误作品。"""
    if not is_built(db_path):
        return []
    name, _, series = (query or "").strip().partition(",")
    name = _normalize(name.strip(" .，。"))
    series = _normalize(series.strip(" .，。"))
    if not name or len(name) > 90:
        return []
    slug = name.replace(" ", "_")
    series_filter = (" AND (copyright = ? COLLATE NOCASE "
                     "OR copyright_name = ? COLLATE NOCASE)") if series else ""
    series_params = (series.replace(" ", "_"), series) if series else ()
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM characters WHERE (character = ? COLLATE NOCASE "
            "OR name_lower = ?)" + series_filter +
            " ORDER BY count DESC LIMIT ?",
            (slug, name, *series_params, limit)).fetchall()
        if not rows and len(name) >= 3:
            escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = conn.execute(
                "SELECT * FROM characters WHERE name_lower LIKE ? ESCAPE '\\'" +
                series_filter + " ORDER BY count DESC LIMIT ?",
                (f"%{escaped}%", *series_params, limit)).fetchall()
    return [_clean(row) for row in rows]


def direct_candidates(description: str, db_path: str = "") -> list[dict]:
    text = (description or "").strip()
    if re.fullmatch(r"[a-zA-Z0-9_ ()'\-]+(?:,\s*[a-zA-Z0-9_ ()'\-]+)?", text):
        return find_candidates(text, db_path=db_path)
    return []


def best_match(query: str, limit: int = 20,
               db_path: str = "") -> tuple:
    matches = find_candidates(query, limit=limit, db_path=db_path)
    if len(matches) == 1:
        return matches[0], []
    return None, [f"{m['name']} ({m['copyright_name']})" for m in matches]


def format_character(best: dict, max_tags: int = 80) -> str:
    lines = [
        f"角色: {best['character']}",
        f"作品: {best['copyright']}",
        f"触发词: {best['trigger']}",
    ]
    core = best.get("core_tags") or ""
    if core:
        tags = [t.strip() for t in core.split(",") if t.strip()]
        lines.append("特征标签: " + ", ".join(tags[:max_tags]))
    return "\n".join(lines)


def lookup(query: str, max_tags: int = 80, db_path: str = "") -> str:
    """返回格式化好的角色参考文本，找不到返回空串。"""
    try:
        best, _ = best_match(query, db_path=db_path)
    except Exception:
        return ""
    if not best:
        return ""

    return format_character(best, max_tags)
