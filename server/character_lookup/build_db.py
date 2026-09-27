"""从 Laxhar/noob-wiki 的 danbooru_character.csv 构建本地角色库。

这一步只做一次，产物是一个约 140MB 的 SQLite。之后所有查询都是
纯本地的，不联网、不消耗任何 API 额度。

用法：
    python character_lookup/build_db.py                  # 下载并建库
    python character_lookup/build_db.py --skip-download  # 复用已有 CSV
    python character_lookup/build_db.py --proxy http://127.0.0.1:7897
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
import time
from pathlib import Path

DATASET_URL = ("https://huggingface.co/datasets/Laxhar/noob-wiki/"
               "resolve/main/danbooru_character.csv?download=true")

SCHEMA = """
CREATE TABLE IF NOT EXISTS characters (
    character      TEXT PRIMARY KEY,
    copyright      TEXT NOT NULL,
    name           TEXT NOT NULL,
    name_lower     TEXT NOT NULL,
    copyright_name TEXT NOT NULL,
    trigger        TEXT NOT NULL,
    core_tags      TEXT NOT NULL,
    count          INTEGER NOT NULL DEFAULT 0,
    url            TEXT NOT NULL DEFAULT '',
    search_blob    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_char_name ON characters(name_lower);
CREATE INDEX IF NOT EXISTS idx_char_copyright ON characters(copyright);
CREATE INDEX IF NOT EXISTS idx_char_count ON characters(count DESC, name_lower);
"""

HAIR_COLOR_TAGS = {
    "aqua hair", "black hair", "blonde hair", "blue hair", "brown hair",
    "green hair", "grey hair", "light brown hair", "multicolored hair",
    "orange hair", "pink hair", "purple hair", "red hair",
    "silver hair", "white hair", "yellow hair",
}
HAIR_LENGTH_TAGS = {
    "very short hair", "short hair", "medium hair", "long hair",
    "very long hair", "absurdly long hair",
}
EYE_COLOR_TAGS = {
    "aqua eyes", "black eyes", "blue eyes", "brown eyes", "green eyes",
    "grey eyes", "heterochromia", "purple eyes", "red eyes", "yellow eyes",
}


def _titlecase(s: str) -> str:
    if not s:
        return s
    return s[:1].upper() + s[1:]


def parse_row(row: dict) -> dict | None:
    character = (row.get("character") or "").strip()
    if not character:
        return None
    copyright_ = (row.get("copyright") or "").strip()
    trigger = (row.get("trigger") or "").strip()
    core = (row.get("core_tags") or "").strip()

    if ", " in trigger:
        nm, cp = trigger.split(", ", 1)
    else:
        nm, cp = trigger, ""
    name = _titlecase(nm) if nm else _titlecase(character.replace("_", " "))
    copyright_name = (_titlecase(cp) if cp
                      else _titlecase(copyright_.replace("_", " ")))
    try:
        count = int(row.get("count") or 0)
    except ValueError:
        count = 0

    return {
        "character": character,
        "copyright": copyright_,
        "name": name,
        "name_lower": name.lower(),
        "copyright_name": copyright_name,
        "trigger": trigger,
        "core_tags": core,
        "count": count,
        "url": (row.get("url") or "").strip(),
        "search_blob": " ".join(
            (character, copyright_, trigger, core)).lower(),
    }


COLS = ("character", "copyright", "name", "name_lower", "copyright_name",
        "trigger", "core_tags", "count", "url", "search_blob")


def download_csv(csv_path: Path, proxy_url: str = "") -> None:
    import requests
    proxies = ({"http": proxy_url, "https": proxy_url}
               if proxy_url else None)
    print(f"下载 {DATASET_URL}")
    with requests.get(DATASET_URL, proxies=proxies, stream=True,
                      timeout=180) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(csv_path, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // max(total, 1)
                    sys.stdout.write(f"\r  {pct}% ({done // 1048576}MB)")
                    sys.stdout.flush()
    print(f"\n已保存到 {csv_path}")


def build_db(csv_path: Path, db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    placeholders = ",".join("?" * len(COLS))
    count = 0
    t0 = time.time()
    with conn:
        with open(csv_path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                fields = parse_row(row)
                if fields is None:
                    continue
                conn.execute(
                    f"INSERT OR REPLACE INTO characters({','.join(COLS)}) "
                    f"VALUES ({placeholders})",
                    [fields[c] for c in COLS])
                count += 1
                if count % 20000 == 0:
                    print(f"  {count} 角色... ({time.time() - t0:.0f}s)")
    conn.commit()
    conn.close()
    return count


def main() -> None:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(here / "danbooru_character.csv"))
    ap.add_argument("--db", default=str(here / "characters.db"))
    ap.add_argument("--proxy", default=os.environ.get("PROXY_URL", ""),
                    help="例如 http://127.0.0.1:7897")
    ap.add_argument("--skip-download", action="store_true",
                    help="CSV 已存在时跳过下载")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    db_path = Path(args.db)
    if not csv_path.exists():
        download_csv(csv_path, args.proxy)
    else:
        print(f"复用已有 CSV：{csv_path}")

    print(f"开始建库：{db_path}")
    count = build_db(csv_path, db_path)
    print(f"完成，共 {count} 个角色")


if __name__ == "__main__":
    main()
