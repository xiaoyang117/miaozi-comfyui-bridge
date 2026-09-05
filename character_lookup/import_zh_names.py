"""把现成的「Danbooru 标签 -> 中文名」数据集导入本地角色查询。

数据源（任选其一，均为现成开源数据集）：
  A) ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table (推荐)
     HuggingFace: https://huggingface.co/datasets/ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table
     国内镜像:     https://hf-mirror.com/datasets/ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table
     主数据为 SQLite（含 tags 表: name / category / post_count / cn_name，
     category: 4=角色, 3=作品/版权）。下载 .sqlite 或解压后的 .csv 均可。
  B) Aligadai/danbooru-10w-zh_cn （备选，无分类、全量标签含机翻）
     https://huggingface.co/datasets/Aligadai/danbooru-10w-zh_cn
     两列 CSV: danbooru标签,中文翻译 —— 无 category，导入时靠
     characters.db 角色集过滤，只保留本库真实存在的角色名映射。

用法:
    python character_lookup/import_zh_names.py <数据文件或目录> [--db characters.db]
    # <数据文件> 支持 .sqlite/.db/.csv，若给目录则自动找里面的 sqlite/csv

产出:
    character_lookup/zh_names.json    {中文名: [danbooru角色tag, ...]}
    character_lookup/zh_works.json    {作品中文名: [copyright tag, ...]} (源有作品类时)
    不影响 aliases.json（手工/自动学习条目优先级更高）。

设计:
    - join 过滤：只保留 characters.db 里真实存在的角色 tag，杜绝把普通
      标签/画师/机翻垃圾当角色导入。
    - 多义名保留多候选（如 尼禄 -> fate 与月姬两个角色），解析时逐个试。
    - 导入条目不做容量裁剪（aliases.json 的 3000 上限不适用独立文件）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).parent
DEFAULT_DB = HERE / "characters.db"
OUT_ZH = HERE / "zh_names.json"
OUT_WORK = HERE / "zh_works.json"

_CN_SEG = re.compile(r"[\u4e00-\u9fff]{2,12}")
_CN_LEN = re.compile(r"^[\u4e00-\u9fff]{2,12}$")
# 作品名常带 ASCII（东方Project/Fate系列/Love Live 等）：主体中文 + 前后可带 ASCII
_CN_ASCII_LEN = re.compile(
    r"^[A-Za-z0-9_\-\s]{0,12}[\u4e00-\u9fff]{2,10}[A-Za-z0-9_\-\s]{0,12}$")

# 中文名里的常见解释性前后缀，需剔除（数据集里常见 "原名xx；xx是社团" 之类）
_SKIP_WORDS = {
    "原名", "中文", "别名", "其他", "以上", "以下", "系列作",
    "作品", "角色", "社团", "组合", "团体", "画师", "作者", "声优",
    "歌手", "虚拟主播", "本名", "旧名", "简称", "无通用", "待定",
    "未知", "官方", "译名", "日文", "注",
}


def _load_characters(db_path: Path) -> set[str]:
    """载入角色库全部 character tag（小写），用于 join 过滤。"""
    if not db_path.exists():
        print(f"[警告] 角色库不存在: {db_path} —— 将无法过滤，全量导入")
        return set()
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT character FROM characters").fetchall()
    conn.close()
    return {r[0].lower() for r in rows}


def _clean_cn(raw: str, allow_ascii: bool = False) -> list[str]:
    """把 cn_name 拆成干净的中文名候选列表。

    allow_ascii=True 用于作品名（如 东方Project / Fate），
    角色名保持纯中文严格匹配以挡机翻乱码。
    """
    if not raw:
        return []
    out: list[str] = []
    pat = _CN_ASCII_LEN if allow_ascii else _CN_LEN
    for part in re.split(r"[、，,;；/|]", raw):
        part = part.strip()
        # 剔除解释段（含数字/字母混排或 "xx是yy" 长尾）
        if not part or not pat.match(part):
            continue
        if any(w in part for w in _SKIP_WORDS):
            continue
        out.append(part)
    return out


def _iter_from_sqlite(path: Path):
    conn = sqlite3.connect(str(path.resolve()))
    conn.row_factory = sqlite3.Row
    # 探测表结构：ffdkj 主表叫 tags；兼容 characters 等其它命名
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    cand = [t for t in ("tags", "character", "characters") if t in tables]
    if not cand:
        print(f"[警告] {path.name} 里没找到 tags/characters 表，跳过")
        conn.close()
        return
    table = cand[0]
    # 最朴素读取：SELECT * 整表取出，列位置在 Python 侧解析
    # （规避个别 sqlite 环境下"动态列名+AS别名"SELECT 的怪异行为）
    try:
        cur = conn.execute(f'SELECT * FROM "{table}"')
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    except Exception as e:
        print(f"[警告] 读取 {path.name} 失败: {e}")
        conn.close()
        return
    name_idx = next((i for i, c in enumerate(cols)
                     if c in ("name", "tag", "character")), None)
    cn_idx = next((i for i, c in enumerate(cols)
                   if c in ("cn_name", "zh", "zh_name", "chinese", "cn")), None)
    cat_idx = next((i for i, c in enumerate(cols)
                    if c in ("category", "type", "cat")), None)
    if name_idx is None or cn_idx is None:
        print(f"[警告] {path.name} 的 {table} 表缺 name/cn 列，跳过")
        conn.close()
        return
    for r in rows:
        yield {"name": (r[name_idx] or "").strip(),
               "cn": (r[cn_idx] or "").strip(),
               "cat": int(r[cat_idx]) if cat_idx is not None
               and r[cat_idx] is not None else None}
    conn.close()


def _iter_from_csv(path: Path):
    import csv as _csv
    with open(path, encoding="utf-8", newline="") as f:
        sample = f.read(8192)
        f.seek(0)
        # 表头判定：须能嗅探出表头，且表头含中英"名字/中文"标识词，否则按无表头两列读
        has_header = False
        try:
            if _csv.Sniffer().has_header(sample):
                f.seek(0)
                first = next(_csv.reader(f), []) or []
                if any(str(c).strip().lower() in
                       ("name", "tag", "character", "english", "cn", "zh",
                        "chinese", "cn_name", "zh_name", "中文", "翻译")
                       for c in first):
                    has_header = True
        except Exception:
            has_header = False
        f.seek(0)
        reader = _csv.DictReader(f) if has_header else None
        if reader is None:
            # 无表头：按两列 (name, cn) 读
            for row in _csv.reader(f):
                if len(row) >= 2 and (row[0] or "").strip():
                    yield {"name": (row[0] or "").strip(),
                           "cn": (row[1] or "").strip(), "cat": None}
            return
        cols = reader.fieldnames or []
        name_col = next((c for c in cols
                         if c.lower() in ("name", "tag", "character", "english")), None)
        cn_col = next((c for c in cols
                       if c.lower() in ("cn_name", "zh", "zh_name", "chinese",
                                        "cn", "翻译", "中文")), None)
        cat_col = next((c for c in cols
                        if c.lower() in ("category", "type", "cat")), None)
        if not name_col or not cn_col:
            print(f"[警告] {path.name} 列名无法识别 (列: {cols})，跳过")
            return
        for row in reader:
            yield {"name": (row.get(name_col) or "").strip(),
                   "cn": (row.get(cn_col) or "").strip(),
                   "cat": (int(row[cat_col]) if cat_col and row.get(cat_col) else None)}


def import_file(path: Path, db_path: Path,
                zh_out: dict, work_out: dict,
                characters: set[str], stats: dict) -> None:
    print(f"\n=== 处理 {path.name} ===")
    if path.suffix.lower() in (".sqlite", ".db", ".sqlite3"):
        it = _iter_from_sqlite(path)
    elif path.suffix.lower() in (".csv", ".txt"):
        it = _iter_from_csv(path)
    else:
        print(f"[跳过] 不支持的扩展名: {path.suffix}")
        return

    role_hits = work_hits = nojoin = 0
    for row in it:
        name, cn, cat = row["name"], row["cn"], row["cat"]
        if not name or not cn:
            continue
        cns = _clean_cn(cn, allow_ascii=(cat == 3))
        if not cns:
            continue
        low = name.lower()
        # 角色类：join 过滤到 characters.db
        is_work = cat == 3
        if cat is not None and cat not in (3, 4):
            continue  # 通用/画师/元标签 全部丢弃
        if is_work:
            for c in cns:
                lst = work_out.setdefault(c, [])
                if name not in lst:
                    lst.append(name)
            work_hits += 1
            continue
        if low not in characters:
            nojoin += 1
            continue
        for c in cns:
            lst = zh_out.setdefault(c, [])
            if name not in lst:
                lst.append(name)
        role_hits += 1

    stats["角色映射"] += role_hits
    stats["作品映射"] += work_hits
    stats["未入本库跳过"] += nojoin
    print(f"  角色映射 {role_hits} | 作品映射 {work_hits} | "
          f"未在本库被跳过 {nojoin}")


def main() -> None:
    ap = argparse.ArgumentParser(description="导入 Danbooru 中文名数据集")
    ap.add_argument("src", help="数据文件(.sqlite/.db/.csv) 或含数据的目录")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="characters.db 路径")
    args = ap.parse_args()

    src = Path(args.src)
    db_path = Path(args.db)
    if not src.exists():
        sys.exit(f"文件不存在: {src}")
    if db_path.exists():
        print(f"角色库: {db_path}（用于过滤）")
    characters = _load_characters(db_path)

    zh_out: dict[str, list[str]] = {}
    work_out: dict[str, list[str]] = {}
    stats = {"角色映射": 0, "作品映射": 0, "未入本库跳过": 0}

    files = [src] if src.is_file() else sorted(
        p for p in src.iterdir() if p.suffix.lower() in (".sqlite", ".db", ".csv", ".txt"))
    if not files:
        sys.exit(f"目录里没有 .sqlite/.db/.csv 文件: {src}")
    for p in files:
        import_file(p, db_path, zh_out, work_out, characters, stats)

    # 去重保留首次出现顺序，排序输出
    def dump(data: dict, path: Path) -> int:
        data = {k: list(dict.fromkeys(v)) for k, v in data.items()}
        data = dict(sorted(data.items(), key=lambda x: x[0]))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=0),
                        encoding="utf-8")
        return len(data)

    n_zh = dump(zh_out, OUT_ZH)
    n_wk = dump(work_out, OUT_WORK) if work_out else 0
    if not work_out:
        OUT_WORK.unlink(missing_ok=True)

    print("\n=== 导入完成 ===")
    print(f"新增中文名条目: {n_zh} 个（写入 {OUT_ZH.name}）")
    if n_wk:
        print(f"新增作品中文名条目: {n_wk} 个（写入 {OUT_WORK.name}）")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    size = OUT_ZH.stat().st_size / 1024 if OUT_ZH.exists() else 0
    print(f"zh_names.json 大小: {size:.0f} KB")


if __name__ == "__main__":
    main()
