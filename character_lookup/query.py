"""Local character feature lookup from a noob-wiki SQLite database.

The DB is built by build_db.py. Schema mirrors AnimaDex:
    characters (character, copyright, name, trigger, core_tags, count,
                search_blob, ...)

Provides:
    lookup(query)                 -> formatted text for prompt context
    best_match(query)             -> (dict | None, [candidate strings])
    lookup_multi(query, n)        -> up to n best matching characters (robust)
    extract_english_terms(text)   -> pull plausible English role-name terms
    exact_slug(name)              -> exact match on the character slug column
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

DEFAULT_DB = os.environ.get(
    "CHARACTER_DB", str(Path(__file__).with_name("characters.db")))

# 常见英文功能词/质量词，抽取英文候选时排除
_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "in", "on", "with", "at", "for",
    "from", "by", "to", "is", "are", "was", "be", "this", "that", "image",
    "picture", "photo", "character", "girl", "boy", "1girl", "1boy", "solo",
    "best", "quality", "masterpiece", "ultra", "detailed", "high", "res",
    "resolution", "hair", "eyes", "eye", "blue", "white", "black", "red",
    "pink", "purple", "green", "yellow", "brown", "orange", "silver", "blonde",
    "school", "uniform", "swimsuit", "wearing", "dress", "outfit", "clothes",
    "looking", "viewer", "smile", "smiling", "cute", "beautiful", "pretty",
    "long", "short", "medium", "large", "small", "big", "full", "body",
    "portrait", "upper", "lower", "close", "face", "head", "hand", "hands",
    "arm", "arms", "leg", "legs", "background", "simple", "standing",
    "sitting", "lying", "holding", "playing", "etc", "please", "draw",
    "generate", "make", "create", "want", "need", "like", "my", "very",
    "more", "some", "any", "all", "also", "just", "only", "no", "not",
}

# 常见“系列/版权”后缀词（copyright），防止把作品名误当角色名
_COPYRIGHT_WORDS = {
    "blue", "archive", "genshin", "impact", "original", "vocaloid",
    "touhou", "fate", "grand", "order", "arknights", "azur", "lane",
    "kantai", "collection", "nijisanji", "hololive", "idolmaster",
}


def _connect(db_path: str = "") -> sqlite3.Connection:
    path = db_path or DEFAULT_DB
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only = ON")
    return c


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


# --------------------------------------------------------------------- #
# 精确 / 模糊匹配
# --------------------------------------------------------------------- #
def exact_slug(name: str, db_path: str = "") -> dict | None:
    """按 character slug 精确匹配，如 'shiroko_(blue_archive)'。"""
    q = (name or "").strip().lower().replace(" ", "_")
    if not q:
        return None
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT * FROM characters WHERE character = ?", (q,)).fetchone()
    conn.close()
    return _clean(row) if row else None


def best_match(query: str, limit: int = 20,
               db_path: str = "") -> tuple[dict | None, list[str]]:
    """Return (best matching character, list of candidate descriptions).

    Lookup order: exact character slug -> exact trigger -> fuzzy on
    search_blob (sorted by popularity). If multiple fuzzy hits share the
    same name, the most popular one wins.
    """
    q = (query or "").strip().strip(",.，。 ")
    if not q:
        return None, []

    conn = _connect(db_path)
    slug = q.replace(" ", "_")

    row = conn.execute(
        "SELECT * FROM characters WHERE character = ?", (slug,)).fetchone()
    if not row:
        row = conn.execute(
            "SELECT * FROM characters WHERE trigger = ?", (q,)).fetchone()
    if not row:
        clean = " ".join(q.replace(",", " ").replace("(", " ")
                          .replace(")", " ").split())
        like = "%" + "%".join(clean.split()) + "%"
        rows = conn.execute(
            "SELECT * FROM characters WHERE search_blob LIKE ? ESCAPE '\\' "
            "ORDER BY count DESC, name_lower LIMIT ?", (like, limit)).fetchall()
        if not rows:
            conn.close()
            return None, []
        row = rows[0]
        alts = [_candidate(a) for a in rows[1:]]
        conn.close()
        return _clean(row), alts

    conn.close()
    return _clean(row), []


def lookup_multi(query: str, n: int = 3, db_path: str = "") -> list[dict]:
    """返回最多 n 个不同的候选角色（宽容匹配）。

    对小模型友好的关键改进：
    - 先尝试整串精确/触发匹配
    - 失败后改为“逐词 OR + 词频评分”的宽松匹配，
      任何含用户词的常见角色都会浮出，避免因 LLM 输出多词/少词而失败。
    - 候选按 (命中词数 desc, count desc) 排序，避免只看热度。
    """
    q = (query or "").strip().strip(",.，。 ")
    if not q:
        return []
    words = [w for w in re.split(r"[,()_ \t]+", q.lower()) if w and len(w) > 1]
    if not words:
        return []

    conn = _connect(db_path)

    def _fetch(sql: str, params: tuple) -> list:
        rows = conn.execute(sql, params).fetchall()
        return [_clean(r) for r in rows]

    # 1) 整串精确（slug / trigger）
    slug = q.replace(" ", "_")
    hit = _fetch("SELECT * FROM characters WHERE character = ?", (slug,))
    if hit:
        conn.close()
        return hit[:n]
    hit = _fetch("SELECT * FROM characters WHERE trigger = ?", (q,))
    if hit:
        conn.close()
        return hit[:n]

    # 2) 全词 AND（与原逻辑等价，但多候选）
    like_all = "%" + "%".join(words) + "%"
    rows = conn.execute(
        "SELECT * FROM characters WHERE search_blob LIKE ? ESCAPE '\\' "
        "ORDER BY count DESC LIMIT ?", (like_all, n)).fetchall()
    results = [_clean(r) for r in rows]

    # 3) 词级 OR + 评分（重点新增）
    if len(results) < n and len(words) >= 1:
        # 逐个词匹配 search_blob / name / character，汇总计分
        scores: dict[int, dict] = {}
        for w in words:
            like = f"%{w}%"
            found = conn.execute(
                "SELECT * FROM characters WHERE search_blob LIKE ? ESCAPE '\\' "
                "ORDER BY count DESC LIMIT 30", (like,)).fetchall()
            for r in found:
                rid = r["character"]
                s = scores.setdefault(rid, {"row": r, "score": 0})
                # 词出现在 name/character 开头权重更高
                if w in r["character"].split("_") or \
                   w in r["name"].lower().split():
                    s["score"] += 3
                else:
                    s["score"] += 1
        ranked = sorted(scores.values(),
                        key=lambda x: (x["score"], x["row"]["count"]),
                        reverse=True)
        seen = {r["character"] for r in results}
        for item in ranked:
            if len(results) >= n:
                break
            if item["row"]["character"] not in seen:
                results.append(_clean(item["row"]))
                seen.add(item["row"]["character"])

    conn.close()
    return results[:n]


# --------------------------------------------------------------------- #
# 英文候选抽取（纯规则，不依赖 LLM）
# --------------------------------------------------------------------- #
def extract_english_terms(text: str, max_terms: int = 6) -> list[str]:
    """从一段文本里抽取疑似角色名的英文术语。

    规则：
    - 抽取连续字母串（含 空格/下划线/括号 连接的词组）
    - 去停用词、纯作品词
    - 保序去重，最多 max_terms 个
    """
    if not text:
        return []
    # 1) 括号里的英文优先（用户常写 “白子(shiroko)”）
    bracket_hits = re.findall(
        r"[（(]\s*([A-Za-z][A-Za-z0-9 _\-]{1,40}?)\s*[)）]", text)
    # 2) 其他位置的连续英文词
    general_hits = re.findall(r"[A-Za-z][A-Za-z0-9 _\-]{1,40}", text)

    terms: list[str] = []
    def _add(t: str):
        t = t.strip().strip("_- ")
        tl = t.lower()
        # 单个词长度<2 或 停用词 丢弃
        if len(t) < 2 or tl in _STOPWORDS:
            return
        # 全由停用词构成也丢弃
        toks = re.split(r"[\s_\-]+", tl)
        if toks and all(x in _STOPWORDS for x in toks):
            return
        # 纯版权词丢弃（如 blue archive 仅作作品线索）
        if toks and all(x in _COPYRIGHT_WORDS for x in toks):
            return
        if t not in terms:
            terms.append(t)

    for h in bracket_hits:
        _add(h)
    for h in general_hits:
        _add(h)
    return terms[:max_terms]


def lookup(query: str, max_tags: int = 80, db_path: str = "") -> str:
    """Return formatted character reference text, or "" when not found."""
    best, alts = best_match(query, db_path=db_path)
    if not best:
        return ""
    lines = [f"角色: {best['character']}",
             f"作品: {best['copyright']}",
             f"触发词: {best['trigger']}"]
    core = best["core_tags"]
    if core:
        tags = [t.strip() for t in core.split(",") if t.strip()]
        shown = ", ".join(tags[:max_tags])
        lines.append(f"特征标签: {shown}")
    if alts:
        lines.append("其他候选: " + " / ".join(alts))
    return "\n".join(lines)


def is_built(db_path: str = "") -> bool:
    return Path(db_path or DEFAULT_DB).exists()


if __name__ == "__main__":
    import sys
    term = sys.argv[1] if len(sys.argv) > 1 else ""
    if not term:
        sys.exit("usage: python query.py <角色名>")
    print(lookup(term) or "未找到")
    print("\n-- multi --")
    for d in lookup_multi(term):
        print(" ", d["character"], "|", d["copyright"], "| count=", d["count"])
