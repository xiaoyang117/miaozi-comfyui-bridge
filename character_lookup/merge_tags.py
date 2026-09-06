"""合并两份 danbooru 标签源为统一词库 tag_full.sqlite。

数据源：
  A) ffdkj 中英对照表  character_lookup/tag.sqlite   (32.5万, 含中文名 cn_name, 全类别含画师)
  B) qdlabs 英文全量表  <外部下载 tags.parquet>       (19.4万, 纯 wiki 标签, 无画师, 非 deprecated)

合并策略：
  - 按 tag name 去重；重复时 cn_name 用 ffdkj 的（若 qdlabs 无中文）
  - post_count 取两者较大值（避免因采集时间不同导致热标签排名偏低）
  - category 以 ffdkj 为准，缺失则用 qdlabs
  - 产出 character_lookup/tag_full.sqlite: tags(name, category, cn_name, post_count)

用法:
  python merge_tags.py <tags.parquet 路径>
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pyarrow.parquet as pq

HERE = Path(__file__).parent
SRC_FFDKJ = HERE / "tag.sqlite"
OUT_DB = HERE / "tag_full.sqlite"


def load_ffdkj(db_path: Path) -> dict:
    """读 ffdkj: name -> (category, cn_name, post_count)。"""
    out: dict[str, tuple] = {}
    conn = sqlite3.connect(str(db_path))
    for name, category, post_count, cn_name in conn.execute(
            "SELECT name, category, post_count, cn_name FROM tags"):
        out[name] = (category, cn_name or "", post_count or 0)
    conn.close()
    return out


def load_qdlabs(parquet_path: Path) -> dict:
    """读 qdlabs parquet: name -> (category, post_count)。

    注意：该数据集存在重复行（同一 tag 多行完全相同），此处按 name 去重，
    同 name 多行时保留 post_count 最大的一条，避免误取低热度重复行。
    """
    df = pq.read_table(str(parquet_path)).to_pandas()
    df = df.sort_values("post_count", ascending=False)
    df = df.drop_duplicates(subset="name", keep="first")
    out: dict[str, tuple] = {}
    for _, r in df.iterrows():
        out[r["name"]] = (int(r["category"]), int(r["post_count"]))
    return out


def main():
    ap = argparse.ArgumentParser(description="merge danbooru tag sources")
    ap.add_argument("parquet", help="qdlabs tags.parquet 路径")
    args = ap.parse_args()
    pq_path = Path(args.parquet)
    if not pq_path.exists():
        sys.exit(f"文件不存在: {pq_path}")

    ffd = load_ffdkj(SRC_FFDKJ)
    print(f"ffdkj: {len(ffd):,} 条")
    qdl = load_qdlabs(pq_path)
    print(f"qdlabs: {len(qdl):,} 条")

    merged: dict[str, dict] = {}
    # 先放 ffdkj（保留中文名）
    for name, (cat, cn, cnt) in ffd.items():
        merged[name] = {"category": cat, "cn_name": cn, "post_count": cnt}
    # 补 qdlabs
    added = 0
    for name, (cat, cnt) in qdl.items():
        m = merged.get(name)
        if m is None:
            merged[name] = {"category": cat, "cn_name": "", "post_count": cnt}
            added += 1
        else:
            if cnt > m["post_count"]:
                m["post_count"] = cnt
    print(f"qdlabs 新增: {added:,} 条 | 合并后总数: {len(merged):,}")

    # 写库
    if OUT_DB.exists():
        OUT_DB.unlink()
    conn = sqlite3.connect(str(OUT_DB))
    conn.execute("""CREATE TABLE tags (
        name TEXT PRIMARY KEY,
        category INTEGER NOT NULL DEFAULT 0,
        cn_name TEXT NOT NULL DEFAULT '',
        post_count INTEGER NOT NULL DEFAULT 0
    )""")
    conn.execute(
        "CREATE INDEX idx_tags_category ON tags(category)")
    conn.execute(
        "CREATE INDEX idx_tags_post ON tags(post_count DESC)")
    rows = [(n, v["category"], v["cn_name"], v["post_count"])
            for n, v in merged.items()]
    conn.executemany("INSERT OR REPLACE INTO tags VALUES (?,?,?,?)", rows)
    conn.commit()

    # 统计
    print("\n=== 合并后分类分布 ===")
    for cat, cnt in conn.execute(
            "SELECT category, COUNT(*) FROM tags GROUP BY category ORDER BY category"):
        print(f"  cat={cat}: {cnt:,}")
    n_cn = conn.execute(
        "SELECT COUNT(*) FROM tags WHERE cn_name != ''").fetchone()[0]
    print(f"带中文名: {n_cn:,} | 纯英文: {len(merged)-n_cn:,}")
    conn.close()
    print(f"\n✅ 已生成: {OUT_DB}")


if __name__ == "__main__":
    main()
