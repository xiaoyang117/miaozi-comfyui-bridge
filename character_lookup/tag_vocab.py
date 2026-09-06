"""Danbooru 标签词库检索：为「画面标签选择」提供候选召回与校验。

数据源: tag_full.sqlite（ffdkj 中文名 + qdlabs 英文补全合并，37.5万标签）
  tags(name, category, cn_name, post_count)
  category: 0=通用(画面) 1=画师 3=版权 4=角色 5=meta

职责:
  - recall_candidates(text, extra_tags, limit)
      从一段(中文/混合)文本里召回候选标签，供 LLM 从中挑选。
  - validate_tags(text)
      把 LLM 输出切词，仅保留词库中真实存在的标签（防编造）。

设计（多路召回，本地零 LLM）:
  R1 额外标签直入  —— 角色 core_tags / 手动指定等高质量来源
  R2 中文点名      —— 文本里出现的中文词 -> cn_name 匹配
  R3 英文词直查    —— 文本里的英文词 -> name 精确匹配
  R4 高频画面兜底  —— 命中不足时用高热度通用标签补齐
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import threading
from pathlib import Path

TAG_DB = os.environ.get(
    "TAG_VOCAB_DB", str(Path(__file__).with_name("tag_full.sqlite")))

_CACHE: dict = {"rows": None, "mtime": 0.0, "cn_map": None,
                "vocab": None, "name2idx": None, "pool": None}
_LOCK = threading.RLock()

# 画面标签核心类别：0=通用(画面主体) 3=版权(作品)；角色4 由角色库负责
_SCENE_CATS = (0, 3)

_CN_CHUNK = re.compile(r"[\u4e00-\u9fff]+")
_EN_TERM = re.compile(r"[a-z][a-z0-9_]{2,30}")

# 口语/常见说法 -> danbooru 标准中文名（点名时扩展，覆盖高频画面场景词）。
# 值可能对应多个候选，均尝试查 cn_map。
_CN_SYNONYMS = {
    "海边": "海洋", "大海": "海洋", "海": "海洋",
    "沙滩": "海滩",
    "夕阳": "日落", "晚霞": "日落",
    "夜晚": "夜空", "星空": "夜空", "夜景": "城市夜景",
    "坐": "坐姿", "坐着": "坐姿", "坐在": "坐姿",
    "站": "站立", "站着": "站立",
    "躺": "躺卧", "躺着": "躺卧",
    "草地": "草地", "花田": "花田", "花园": "花园",
    "室内": "室内", "户外": "户外", "室外": "户外",
    "背景": "简单背景", "纯色背景": "纯色背景",
    "街道": "城市夜景", "城市": "城市",
    "教室": "教室", "学校": "学校建筑",
    "下雨": "下雨", "雨天": "下雨",
    "下雪": "下雪", "雪天": "下雪", "雪地": "下雪",
    "打斗": "战斗", "打架": "战斗",
    "挥手": "挥手", "招手": "挥手",
    "大笑": "张嘴大笑", "笑颜": "微笑",
    "震惊": "惊讶",
    "愤怒": "生气", "发怒": "生气",
    "害羞": "脸红", "不好意思": "脸红",
    "哭泣": "流泪", "哭": "流泪",
    "wink": "眨眼", "眨眼睛": "眨眼",
    "闭上眼睛": "闭眼",
    "女孩": "少女", "男生": "男孩", "男生": "少年",
    "双马尾": "双马尾", "单马尾": "单马尾",
    "猫耳": "猫耳", "狐耳": "狐耳", "兔耳": "兽耳",
    "比基尼": "比基尼",
    "军服": "军装",
    "校服": "校服", "女仆": "女仆装",
    "短裙": "短裙",
    "长筒袜": "过膝袜",
    "刀剑": "剑", "手枪": "枪械",
    "樱花": "樱花", "玫瑰": "玫瑰",
    "满月": "满月", "月光": "月亮",
    "阳光": "阳光", "逆光": "逆光",
    "水面": "水面", "水花": "水花", "水滴": "水滴",
    "着火": "火焰",
    "云朵": "云",
    "特写": "特写", "全身": "全身", "半身": "半身", "上半身": "上半身",
}


def _load() -> bool:
    """读 tag_full.sqlite 进内存（mtime 缓存，带锁）。"""
    global _CACHE
    try:
        mtime = os.path.getmtime(TAG_DB)
    except OSError:
        return False
    with _LOCK:
        if _CACHE["rows"] is not None and _CACHE["mtime"] == mtime:
            return True
        if not os.path.exists(TAG_DB):
            return False
        rows = []
        try:
            conn = sqlite3.connect(TAG_DB)
            conn.execute("PRAGMA query_only = ON")
            rows = [{"name": r[0], "category": r[1],
                     "cn_name": r[2] or "", "post_count": r[3] or 0}
                    for r in conn.execute(
                        "SELECT name, category, cn_name, post_count FROM tags")]
            conn.close()
        except Exception:
            return False
        # 中文点名索引：中文主体词(2-6字, 去括号注释) -> 候选行下标列表
        cn_map: dict[str, list[int]] = {}
        for i, r in enumerate(rows):
            cn = r["cn_name"]
            if not cn:
                continue
            body = re.split(r"[（(【\[，,;；]", cn, maxsplit=1)[0].strip()
            body = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff ・·]", "", body)
            if not (2 <= len(body) <= 6) or body.isascii() and len(body) < 2:
                continue
            cn_map.setdefault(body, []).append(i)
        name2idx = {r["name"]: i for i, r in enumerate(rows)}
        # 高频画面池（分类0/3按热度降序）
        scene = [(i, rows[i]["post_count"]) for i, r in enumerate(rows)
                 if r["category"] in _SCENE_CATS]
        scene.sort(key=lambda x: x[1], reverse=True)
        pool = [i for i, _ in scene]
        _CACHE = {"rows": rows, "mtime": mtime, "cn_map": cn_map,
                  "vocab": set(name2idx), "name2idx": name2idx, "pool": pool}
        return True


def _available() -> bool:
    return _load()


def is_available() -> bool:
    """词库是否可用（tag_full.sqlite 存在且已加载）。"""
    return _available()


def recall_candidates(text: str, extra_tags: list[str] | None = None,
                      limit: int = 200) -> list[dict]:
    """多路召回候选标签。返回 [{name, cn_name, post_count, via}]。"""
    if not _available():
        return []
    text = text or ""
    C = _CACHE
    rows, cn_map, name2idx, vocab = (C["rows"], C["cn_map"],
                                     C["name2idx"], C["vocab"])
    picked: dict[str, dict] = {}
    via_of: dict[str, str] = {}

    def _add(idx: int, via: str):
        r = rows[idx]
        n = r["name"]
        if n not in picked:
            picked[n] = r
            via_of[n] = via

    # R1: 额外标签（角色 core_tags 等 CSV 串）
    for t in (extra_tags or []):
        for piece in re.split(r"[,\n]", t or ""):
            piece = piece.strip().lower().replace(" ", "_").strip("_")
            if piece and piece in name2idx:
                _add(name2idx[piece], "角色特征")

    # R2: 中文点名 —— 文本中文块滑窗查 cn_map（原词 + 同义词扩展）
    low = text.lower()
    cn_map_hit = set()   # 已命中过的 cn key，避免重复点名同一词

    def _hit_cn(cn_key: str):
        """cn_key 在 cn_map 存在且类别符合则召回其候选。"""
        if cn_key in cn_map_hit:
            return
        idxs = cn_map.get(cn_key)
        if not idxs:
            return
        cn_map_hit.add(cn_key)
        for idx in idxs[:6]:
            r = rows[idx]
            if r["category"] not in _SCENE_CATS:
                continue
            # 剔除低热度/明显噪音（拼写错误标签常挂在同中文名下）
            if r["post_count"] < 50:
                continue
            _add(idx, "中文")

    text_cn = "".join(_CN_CHUNK.findall(low))  # 拼接中文便于同义词子串检测
    for chunk in _CN_CHUNK.findall(low):
        n = len(chunk)
        for L in (2, 3, 4):
            if n < L:
                continue
            for s in range(n - L + 1):
                w = chunk[s:s + L]
                _hit_cn(w)
    # 同义词扩展：口语词(可能含单字/跨标点)在文本中出现 -> 查对应标准中文名
    for spoken, std in _CN_SYNONYMS.items():
        if spoken.lower() in low and std not in cn_map_hit:
            _hit_cn(std)

    # R3: 英文词直查
    for t in _EN_TERM.findall(low):
        if t in name2idx:
            _add(name2idx[t], "英文")

    # R4: 高频画面兜底 —— 仅当候选不足时补齐。
    # 控制高频占比：中文点名充分时只补少量（约 1/3 上限），避免噪音淹没点名结果。
    cn_count = sum(1 for v in via_of.values() if v == "中文")
    if cn_count >= 8:
        r4_cap = max(10, limit // 3)          # 点名充分 → 高频仅作补充
    else:
        r4_cap = limit                        # 点名少 → 高频兜底
    for i in C["pool"]:
        if len(picked) >= r4_cap:
            break
        if rows[i]["name"] not in picked:
            _add(i, "高频")

    # 排序：角色特征 > 中文 > 英文 > 高频；组内热度降序
    via_rank = {"角色特征": 0, "中文": 1, "英文": 2, "高频": 3}
    ordered = sorted(picked.values(),
                     key=lambda r: (via_rank.get(via_of[r["name"]], 9),
                                    -r["post_count"]))
    out = []
    for r in ordered[:limit]:
        out.append({"name": r["name"], "cn_name": r["cn_name"],
                    "post_count": r["post_count"], "via": via_of[r["name"]]})
    return out


def validate_tags(text: str) -> list[str]:
    """把 LLM 输出的文本切词，仅保留词库真实存在的标签（去重保序）。"""
    if not _available() or not text:
        return []
    vocab = _CACHE["vocab"]
    out: list[str] = []
    seen = set()
    for piece in re.split(r"[\s,，、;；\n]+", text):
        w = piece.strip().lower().replace(" ", "_").strip("_")
        if not w:
            continue
        if w in vocab and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def format_candidates(cands: list[dict], max_n: int = 200) -> str:
    """把候选格式化为 LLM 可见文本：'name 中文名'，逗号分隔。"""
    parts = []
    for c in cands[:max_n]:
        label = c["name"]
        if c.get("cn_name"):
            label += f" {c['cn_name']}"
        parts.append(label)
    return ", ".join(parts)


def stats() -> str:
    if not _available():
        return "词库不可用"
    C = _CACHE
    return (f"{len(C['rows']):,} 标签 | 中文索引 {len(C['cn_map']):,} 词 "
            f"| 画面池 {len(C['pool']):,}")


if __name__ == "__main__":
    if not _available():
        sys.exit("tag_full.sqlite 不存在，请先运行 character_lookup/merge_tags.py")
    print("词库:", stats())
    q = sys.argv[1] if len(sys.argv) > 1 else "碧蓝档案的白子 穿泳装坐在海边"
    print(f"\n输入: {q}")
    cands = recall_candidates(q, extra_tags=["white_hair, blue_archive"],
                              limit=60)
    print(f"召回 {len(cands)} 候选，前 40:")
    for c in cands[:40]:
        print(f"  {c['name']:<32} {c['cn_name'] or '':<10} via={c['via']} "
              f"{c['post_count']:,}")
    print("\n校验 '1girl, silver_hair, white_hair, swimsuit, made_up_tag':",
          validate_tags("1girl, silver_hair, white_hair, swimsuit, made_up_tag"))
