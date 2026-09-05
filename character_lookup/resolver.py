"""多层角色解析器：把“找角色”从 LLM 必经之路变成规则优先。

对小模型友好的设计：
1. 英文直查   —— 用户输入里带英文/罗马音时直接命中，无需 LLM
2. 别名表     —— 中文名/常用称呼 -> danbooru 角色名（可自动学习积累）
3. LLM 提取   —— 上面都不中才用 LLM 抽取英文名（提示词 few-shot 化）
4. 翻译兜底   —— LLM 给出中文名时转为 danbooru 标签再查

别名表存于 character_lookup/aliases.json：
    {"白子": "shiroko, blue_archive", ...}
每次成功解析到角色后，会把 (中文输入 -> 角色) 学习进表，越用越准。
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from .query import exact_slug, extract_english_terms, lookup_multi

ALIASES_FILE = os.environ.get(
    "CHARACTER_ALIASES", str(Path(__file__).with_name("aliases.json")))

# 大容量中文名映射（由 import_zh_names.py 从开源数据集导入，数万条）。
# 与 aliases.json（手工 + 自动学习，3000 上限）分开存放，互不干扰。
ZH_FILE = os.environ.get(
    "CHARACTER_ZH", str(Path(__file__).with_name("zh_names.json")))
# 作品中文名 -> copyright tag（同一导入器产出，用于同名角色的作品消歧）
ZH_WORK_FILE = os.environ.get(
    "CHARACTER_ZH_WORK", str(Path(__file__).with_name("zh_works.json")))

_zh_cache: dict = {"data": None, "mtime": 0.0}
_zw_cache: dict = {"data": None, "mtime": 0.0}
_zh_lock = threading.RLock()

# 常见中文简称/系列词 -> 不需要单独成角色，但参与版权匹配时用
_SERIES_HINTS = {
    "碧蓝档案": "blue_archive", "ba": "blue_archive", "蔚蓝档案": "blue_archive",
    "碧蓝航线": "azur_lane", "blhx": "azur_lane",
    "原神": "genshin_impact", "genshin": "genshin_impact",
    "明日方舟": "arknights", "方舟": "arknights",
    "崩坏星穹铁道": "honkai_star_rail", "星穹铁道": "honkai_star_rail",
    "崩坏3": "honkai_3rd",
    "少女前线": "girls_frontline",
    "偶像大师": "idolmaster",
    "东方": "touhou", "东方project": "touhou",
    "vocaloid": "vocaloid", "初音未来": "vocaloid", "初音": "vocaloid",
    "hololive": "hololive", "holo": "hololive",
    "sao": "sword_art_online", "刀剑神域": "sword_art_online",
    "赛马娘": "umamusume", "马娘": "umamusume",
}

_alias_lock = threading.RLock()


# --------------------------------------------------------------------- #
# 别名表读写
# --------------------------------------------------------------------- #
def load_aliases() -> dict:
    with _alias_lock:
        try:
            with open(ALIASES_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def save_alias(key: str, value: str) -> None:
    """把 (中文称呼 -> 角色) 记入别名表，自动去重、限制大小。"""
    if not key or not value:
        return
    with _alias_lock:
        data = load_aliases()
        # 避免脏 key（太长/纯符号）
        key = key.strip()
        if len(key) < 2 or len(key) > 12:
            return
        data[key] = value
        # 防止无限膨胀：最多保留 3000 条
        if len(data) > 3000:
            data = dict(list(data.items())[-3000:])
        try:
            with open(ALIASES_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception:
            pass


def match_aliases(text: str) -> list[tuple[str, str]]:
    """在文本里找别名表命中项。返回 [(别名, 角色串)]，按别名长度倒序（最长优先）。"""
    if not text:
        return []
    aliases = load_aliases()
    hits = []
    for k, v in aliases.items():
        if k and v and k in text:
            hits.append((k, v))
    hits.sort(key=lambda x: len(x[0]), reverse=True)
    return hits


# --------------------------------------------------------------------- #
# 大容量中文名映射（导入数据集，独立于手工别名表）
# --------------------------------------------------------------------- #
def load_zh_names() -> dict:
    """读取 zh_names.json 并做 mtime 缓存（文件可能数 MB，避免每请求重读）。"""
    global _zh_cache
    try:
        mtime = os.path.getmtime(ZH_FILE)
    except OSError:
        return {}
    with _zh_lock:
        if _zh_cache["data"] is not None and _zh_cache["mtime"] == mtime:
            return _zh_cache["data"]
        try:
            with open(ZH_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        _zh_cache = {"data": data, "mtime": mtime}
        return data


def match_zh_names(text: str) -> list[tuple[str, list[str]]]:
    """在大容量中文名映射里找命中。返回 [(中文名, [角色tag...])]，最长优先。"""
    if not text or not re.search(r"[\u4e00-\u9fff]", text):
        return []
    zh = load_zh_names()
    if not zh:
        return []
    n = len(text)
    hits = []
    for k, roles in zh.items():
        # 字符串包含前提：key 不能比 text 长；同长度也需 k == text 才可能包含
        if len(k) > n or not roles:
            continue
        if k in text:
            hits.append((k, roles))
    hits.sort(key=lambda x: len(x[0]), reverse=True)
    return hits


def load_zh_works() -> dict:
    """读取 zh_works.json（作品中文名 -> [copyright tag]），mtime 缓存。"""
    global _zw_cache
    try:
        mtime = os.path.getmtime(ZH_WORK_FILE)
    except OSError:
        return {}
    with _zh_lock:
        if _zw_cache["data"] is not None and _zw_cache["mtime"] == mtime:
            return _zw_cache["data"]
        try:
            with open(ZH_WORK_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        _zw_cache = {"data": data, "mtime": mtime}
        return data


def _prefer_copyrights(text: str) -> set[str]:
    """从输入文本里找出『作品线索』对应的 copyright 标签集合。

    来源：作品中文名表(zh_works)、内置系列词、输入中的英文版权词。
    仅用于同名角色的排序偏好，找不到也无需回退。
    """
    if not text:
        return set()
    pref: set[str] = set()
    low = text.lower()
    # 1) 作品中文名表（碧蓝航线的长门 -> azur_lane）
    n = len(low)
    for wk, cps in load_zh_works().items():
        if len(wk) <= n and wk in low and cps:
            pref.update(cps)
    # 2) 内置系列词
    for cn, cp in _SERIES_HINTS.items():
        if cn and cn in low:
            pref.add(cp)
    # 3) 输入里直接出现的英文版权词（不过滤版权停用词；
    #    仅与候选 copyright 精确比对才生效，故无副作用）
    for t in re.findall(r"[a-z][a-z0-9_]{2,}", low):
        pref.add(t)
    return pref


def _disambiguate(cands: list[dict], text: str) -> list[dict]:
    """同名多作品角色消歧：作品线索命中者排前（如 长门 kancolle/azur_lane）。"""
    if len(cands) < 2 or not text:
        return cands
    pref = _prefer_copyrights(text)
    if not pref:
        return cands
    hit = [c for c in cands if (c.get("copyright") or "") in pref]
    rest = [c for c in cands if (c.get("copyright") or "") not in pref]
    if hit:
        return hit + rest
    return cands


# --------------------------------------------------------------------- #
# 从一段自由文本中智能查找角色（主入口）
# --------------------------------------------------------------------- #
_TAG_LIKE = re.compile(r"^[a-z0-9_]+(?:\([a-z0-9_]+\))?$")


def _lookup_role(role: str, n: int = 3) -> list[dict]:
    """按别名/映射里的角色值查库。

    - 纯标签形态（shiroko_(blue_archive)）：只做精确匹配；精确失败即视为
      死值/拼写错，直接跳过（不做全表模糊 OR —— 曾导致 600ms+ 与串味）。
    - 自由文本（含空格/描述）：交给模糊匹配。
    """
    role = (role or "").strip()
    if not role:
        return []
    slug = role.lower().replace(" ", "_")
    hit = exact_slug(slug)
    if hit:
        return [hit]
    if _TAG_LIKE.match(slug):
        return []
    return lookup_multi(role, n)


def resolve_from_text(text: str, n: int = 3, strict: bool = False) -> list[dict]:
    """尽力从文本中解析出角色，返回候选角色 dict 列表（可为空）。

    优先级（无需 LLM）：
      a) 精确 slug 命中（用户直接给 danbooru 角色名）
      b) 中文别名表命中
      c) 逐英文术语查库（多候选）
      d) 系列词 + 中文片段综合搜索（宽松）

    strict=True 时（手动指定角色框）：只做精确/别名/整串匹配，
    不做逐词宽松联想，避免把 "zzzz_nonexist" 猜成奇怪角色。
    """
    text = (text or "").strip()
    if not text:
        return []

    # a) 精确 slug 命中（用户直接给 danbooru 角色名）
    slug = re.sub(r"\s+", "_", text.strip()).lower()
    if re.fullmatch(r"[a-z0-9_\-]+(?:\([a-z0-9_\-]+\))?", slug):
        hit = exact_slug(slug)
        if hit:
            return [hit]
        # 整串标签直查（如 "shiroko, blue_archive" 已带版权词）
        if strict:
            loose = lookup_multi(text, n)
            if loose and _confidence_ok(text, loose[0]):
                return loose
            return []

    # b) 手工别名表（轻量、高可信，优先）
    for _alias, role in match_aliases(text):
        res = _lookup_role(role, n)
        if res:
            return _disambiguate(res, text)

    # b2) 大容量中文名映射（开源数据集导入，覆盖数万角色）
    for _zh, roles in match_zh_names(text):
        found: list[dict] = []
        for role in roles[:10]:
            res = _lookup_role(role, n)
            if res and res[0].get("character") not in {x.get("character") for x in found}:
                found.append(res[0])
        if found:
            return _disambiguate(found, text)[:n]

    # c) 英文术语直查
    terms = extract_english_terms(text)
    collected: list[dict] = []
    seen = set()
    for t in terms:
        for d in lookup_multi(t, n):
            if d["character"] not in seen:
                collected.append(d)
                seen.add(d["character"])
        if collected:
            break  # 第一个术语命中就足够，避免串味
    if collected:
        if strict and not _confidence_ok(" ".join(terms), collected[0]):
            return []
        return _disambiguate(collected[:n], text)

    # d) 中文 -> 系列词线索：把中文描述里的作品线索和相邻英文结合
    return []


def _confidence_ok(input_text: str, best: dict) -> bool:
    """判断宽松命中的置信度：输入里的有效词须出现在角色字段中。"""
    words = [w for w in re.split(r"[^a-z0-9]+", input_text.lower()) if len(w) >= 3]
    if not words:
        return False
    char = best.get("character", "").lower()
    name = best.get("name", "").lower()
    trigger = best.get("trigger", "").lower()
    blob = char + " " + name + " " + trigger
    return any(w in blob for w in words)


def role_candidates_text(text: str) -> str:
    """返回格式化的候选参考文本（用于 prompt 上下文），空串表示没找到。"""
    cands = resolve_from_text(text)
    if not cands:
        return ""
    lines = []
    for i, d in enumerate(cands, 1):
        lines.append(f"[候选{i}] 角色: {d['character']} | 作品: {d['copyright']} "
                     f"| 触发词: {d['trigger']}")
        core = d.get("core_tags") or ""
        tags = [t.strip() for t in core.split(",") if t.strip()]
        if tags:
            lines.append(f"   特征标签: {', '.join(tags[:60])}")
    return "\n".join(lines)


def debug_report(text: str) -> str:
    """诊断接口：输出解析每一步的结果。"""
    from .query import extract_english_terms
    lines = [f"输入: {text}"]
    slug = re.sub(r"\s+", "_", text.strip()).lower()
    if re.fullmatch(r"[a-z0-9_\-]+(?:\([a-z0-9_\-]+\))?", slug):
        hit = exact_slug(slug)
        lines.append(f"[a] 精确slug '{slug}': {'命中 ' + hit['character'] if hit else '未命中'}")
    else:
        lines.append(f"[a] 精确slug: 跳过(含非纯英文/下划线)")

    alias_hits = match_aliases(text)
    if alias_hits:
        lines.append("[b] 中文别名命中: " + ", ".join(f"{k}->{v}" for k, v in alias_hits))
    else:
        lines.append("[b] 中文别名: 未命中")

    zh_hits = match_zh_names(text)
    if zh_hits:
        lines.append("[b2] 中文名映射命中: "
                     + ", ".join(f"{k}->{v}" for k, v in zh_hits))
    else:
        lines.append("[b2] 中文名映射(数据集): 未命中")

    terms = extract_english_terms(text)
    lines.append(f"[c] 提取英文术语: {terms if terms else '(无)'}")
    if terms:
        for t in terms:
            cands = lookup_multi(t, 3)
            names = [d['character'] for d in cands]
            lines.append(f"    术语 '{t}' 命中: {names if names else '(无)'}")
    return "\n".join(lines)


def learn(input_text: str, resolved: dict | None, via: str = "") -> None:
    """自动学习：把『明确的中文角色称呼 -> danbooru 角色』写入别名表。

    适用场景（高置信）：
    - 用户在角色框手动指定且命中（via 含“手动”）
    - 中文名翻译成功后由调用方直接 save_alias（cn_name 精确）
    避免用整句自然语言描述当 key。
    """
    if not input_text or not resolved:
        return
    if "手动" not in (via or ""):
        return
    key = _pick_name_key(input_text)
    if not key:
        return
    role = resolved.get("character", "")
    if role:
        save_alias(key, role)


def _pick_name_key(text: str) -> str:
    """从文本中挑一个最像“角色称呼”的中文片段作为别名 key。

    规则：
    - 若整体为 2-6 个中文字符的纯称呼（如“白子”“小鸟游星野”），直接使用
    - 否则取最长连续中文段，但需剔除常见动词/描述前后缀
    """
    t = (text or "").strip()
    if not t or len(t) > 20:
        return ""
    pure = re.fullmatch(r"[\u4e00-\u9fff]{2,6}", t)
    if pure:
        return t
    segs = re.findall(r"[\u4e00-\u9fff]{2,6}", t)
    if not segs:
        return ""
    return max(segs, key=len)


if __name__ == "__main__":
    import sys
    term = sys.argv[1] if len(sys.argv) > 1 else ""
    if not term:
        sys.exit("usage: python resolver.py <任意描述>")
    print("候选:")
    for d in resolve_from_text(term):
        print(" -", d["character"], "|", d["copyright"], "| count", d["count"])
    print("\n参考文本:")
    print(role_candidates_text(term) or "(未找到)")
