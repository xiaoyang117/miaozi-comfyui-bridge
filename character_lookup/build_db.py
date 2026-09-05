"""Build a local SQLite character database from the Laxhar/noob-wiki
danbooru_character.csv dataset. The schema mirrors AnimaDex so the same
query logic applies, but this module is standalone (no Flask app needed).

Usage:
    python character_lookup/build_db.py [--csv path] [--db path]
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_char_name      ON characters(name_lower);
CREATE INDEX IF NOT EXISTS idx_char_copyright ON characters(copyright);
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
    "grey eyes", "heterochromia", "purple eyes", "red eyes",
    "yellow eyes",
}


def _titlecase(s: str) -> str:
    if not s:
        return s
    return s[:1].upper() + s[1:]


def _trait_label(tag: str) -> str:
    return _titlecase(tag).replace("_", " ")


def parse_row(row: dict) -> tuple[dict, list] | None:
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

    traits = []
    for t in (x.strip() for x in core.split(",")):
        if not t:
            continue
        if t in HAIR_COLOR_TAGS:
            traits.append(("hair_color", t, _trait_label(t)))
        elif t in HAIR_LENGTH_TAGS:
            traits.append(("hair_length", t, _trait_label(t)))
        elif t in EYE_COLOR_TAGS:
            traits.append(("eye_color", t, _trait_label(t)))
        elif t in ("1girl", "1boy", "1other", "no_humans"):
            traits.append(("gender", t, t))

    fields = {
        "character": character,
        "copyright": copyright_,
        "name": name,
        "name_lower": name.lower(),
        "copyright_name": copyright_name,
        "trigger": trigger,
        "core_tags": core,
        "count": count,
        "url": (row.get("url") or "").strip(),
        "search_blob": " ".join((character, copyright_, trigger,
                                 core)).lower(),
    }
    return fields, traits


def _upsert(conn, fields: dict, _traits: list = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO characters(character, copyright, name, "
        "name_lower, copyright_name, trigger, core_tags, count, url, "
        "search_blob) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [fields[c] for c in (
            "character", "copyright", "name", "name_lower",
            "copyright_name", "trigger", "core_tags", "count", "url",
            "search_blob")])


def download_csv(csv_path: Path, proxy_url: str = "") -> None:
    from curl_cffi import requests
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    print(f"下载 {DATASET_URL}")
    r = requests.get(DATASET_URL, proxies=proxies, impersonate="chrome",
                     stream=True, timeout=120)
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
    print(f"\n保存到 {csv_path}")


def build_db(csv_path: Path, db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    count = 0
    t0 = time.time()
    with conn:
        with open(csv_path, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                parsed = parse_row(row)
                if parsed is None:
                    continue
                fields, traits = parsed
                _upsert(conn, fields, traits)
                count += 1
                if count % 20000 == 0:
                    print(f"  {count} 角色... ({time.time() - t0:.0f}s)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_char_count "
                 "ON characters(count DESC, name_lower)")
    conn.commit()
    conn.close()
    return count


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="danbooru_character.csv",
                    help="path to save/download the CSV")
    ap.add_argument("--db", default="characters.db",
                    help="path of the output SQLite database")
    ap.add_argument("--proxy", default=os.environ.get("PROXY_URL", ""),
                    help="proxy url e.g. http://127.0.0.1:7897")
    ap.add_argument("--skip-download", action="store_true",
                    help="skip downloading if the CSV already exists")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    db_path = Path(args.db)
    if not csv_path.exists():
        download_csv(csv_path, args.proxy)
    else:
        print(f"CSV 已存在: {csv_path}")

    print(f"建库 {db_path} ...")
    count = build_db(csv_path, db_path)
    print(f"完成，共 {count} 个角色")


if __name__ == "__main__":
    main()
