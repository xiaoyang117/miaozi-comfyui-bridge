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


# 中文名大表里的噪音 key：通用画面/职业/动作/状态词被数据集误当角色名
# （如 泳装->泳装角色、女仆->maid、夕阳->sunset_porforever）。
# 这些词出现在用户输入里时绝不作为角色名参与解析，避免整句被假候选污染。
# 注意只影响 import 的大容量表 zh_names；手工 aliases.json 不受限（人工条目可信）。
_ZH_NOISE_KEYS = {
    # ---- 服装/物品 ----
    "泳装", "泳衣", "比基尼", "女仆", "校服", "制服", "军装", "军服",
    "和服", "旗袍", "婚纱", "西装", "礼服", "裙", "裙子", "短裙", "长裙",
    "围裙", "兜帽", "披风", "斗篷", "帽子", "头饰", "王冠", "皇冠",
    "眼镜", "口罩", "面纱", "手套", "丝袜", "过膝袜", "袜子", "靴子",
    "鞋子", "凉鞋", "拖鞋", "盔甲", "铠甲", "防具", "武器", "剑", "刀",
    "枪", "盾", "弓", "法杖", "杖", "书本", "书", "杯子", "伞", "扇子",
    "领带", "领结", "蝴蝶结", "发带", "发夹", "项链", "耳环", "戒指",
    "手镯", "胸针", "锁链", "镣铐", "绷带", "创可贴", "项链",
    # ---- 颜色/外貌特征 ----
    "白发", "银发", "金发", "黑发", "红发", "橙发", "棕发", "蓝发",
    "绿发", "紫发", "粉发", "灰发", "茶发", "亚麻发", "长发", "短发",
    "中长发", "卷发", "直发", "双马尾", "单马尾", "马尾", "麻花辫",
    "辫子", "呆毛", "刘海", "猫耳", "狐耳", "兔耳", "兽耳", "恶魔角",
    "天使光环", "光环", "翅膀", "尾巴", "虎牙", "獠牙", "雀斑", "痣",
    "泪痣", "红瞳", "蓝瞳", "绿瞳", "金瞳", "紫瞳", "粉瞳", "异色瞳",
    "眼罩", "单眼罩", "伤疤",
    # ---- 场景/背景 ----
    "天空", "蓝天", "星空", "夜空", "大海", "海洋", "海边", "海滩",
    "沙滩", "夕阳", "日落", "黄昏", "晚霞", "早晨", "日出", "夜晚",
    "阳光", "日光", "日光浴", "光线", "光照", "逆光", "顺光", "灯光",
    "月亮", "满月", "星星", "云", "云朵", "彩虹", "闪电", "打雷",
    "下雨", "雨天", "下雪", "雪天", "雪地", "雪", "冰", "火焰", "火",
    "岩浆", "水", "瀑布", "河流", "湖", "湖泊", "森林", "树林", "草地",
    "花园", "花田", "花", "樱花", "玫瑰", "向日葵", "麦田", "沙漠",
    "山", "山脉", "洞穴", "悬崖", "城市", "街道", "小镇", "村庄",
    "房间", "卧室", "教室", "学校", "图书馆", "咖啡馆", "咖啡厅",
    "餐厅", "商店", "超市", "车站", "列车", "地铁", "飞机", "船",
    "汽车", "摩托车", "自行车", "战场", "城堡", "塔", "教堂", "神殿",
    "神社", "寺庙", "废墟", "监狱", "牢房", "桥", "屋顶", "天台",
    "阳台", "窗户", "门口", "走廊", "走廊", "泳池", "温泉", "浴室",
    "浴缸", "厨房", "实验室", "医院", "办公室", "工厂",
    # ---- 动作/状态/表情 ----
    "坐", "坐姿", "坐着", "站", "站立", "站着", "躺", "躺卧", "躺着",
    "跪", "跪坐", "蹲", "蹲着", "跳", "跳跃", "跑", "跑步", "奔跑",
    "走", "走路", "散步", "飞", "飞行", "游泳", "游", "睡觉", "睡",
    "醒来", "眨眼", "闭眼", "睁眼", "微笑", "笑", "大笑", "张嘴",
    "闭嘴", "脸红", "害羞", "哭", "哭泣", "流泪", "眼泪", "泪",
    "生气", "愤怒", "发怒", "惊讶", "震惊", "害怕", "恐惧", "开心",
    "高兴", "伤心", "难过", "尴尬", "委屈", "撒娇", "卖萌", "嘟嘴",
    "吐舌", "舔", "咬", "嚼", "喝", "吃", "做饭", "工作", "学习",
    "看书", "画画", "唱歌", "跳舞", "弹琴", "演奏", "打电话", "发短信",
    "自拍", "拍照", "回头", "回首", "转身", "弯腰", "举手", "挥手",
    "招手", "拥抱", "牵手", "战斗", "打斗", "打架", "施法", "施魔法",
    "瞄准", "射击", "狩猎", "钓鱼", "浇花", "撑伞", "穿", "戴",
    "脱", "换衣", "更衣", "洗澡", "泡澡", "刷牙", "照镜子",
    # ---- 人物关系/身份（通用） ----
    "老师", "教师", "学生", "同学", "同班", "校长", "班长", "委员",
    "会长", "社长", "部长", "店员", "服务员", "女仆装", "管家",
    "骑士", "武士", "忍者", "魔法师", "法师", "术士", "弓箭手",
    "战士", "剑士", "枪手", "狙击手", "医生", "护士", "警察", "刑警",
    "侦探", "消防员", "宇航员", "飞行员", "船长", "士兵", "军人",
    "军官", "公主", "王子", "女王", "国王", "女神", "天使", "恶魔",
    "猫娘", "狐娘", "兔娘", "狼娘", "龙娘", "机械娘", "魅魔", "吸血鬼",
    "偶像", "歌手", "舞者", "演员", "模特", "主播", "vtuber",
    # ---- 其他常见描述 ----
    "可爱", "漂亮", "美丽", "帅气", "酷", "性感", "萌", "幼", "成熟",
    "巨大", "微小", "强壮", "瘦", "胖", "高", "矮", "干净", "脏",
    "新", "旧", "破旧", "受伤", "流血", "晕", "死亡", "尸体",
    "背景", "场景", "画面", "图片", "照片", "插图", "立绘", "差分",
    "表情", "姿势", "动作", "姿态", "服装", "衣服", "装扮", "造型",
    "形象", "版本", "限定", "泳装ver", "新年", "圣诞", "万圣", "泳装版本",
}

# 中文名大表里 1~3 字且落在黑名单的词，跳过（不参与角色解析）
def _zh_noise(k: str) -> bool:
    return k in _ZH_NOISE_KEYS


def match_zh_names(text: str) -> list[tuple[str, list[str]]]:
    """在大容量中文名映射里找命中。返回 [(中文名, [角色tag...])]，最长优先。

    过滤噪音 key（画面词/职业词等被数据集误当角色名的条目），
    避免"长门 穿泳装"里的"泳装"命中无关泳装角色。
    """
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
        if k in text and not _zh_noise(k):
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


# 作品英文版权 -> 中文名 的倒排缓存（供界面展示中文作品名）
_cn_rev_cache: dict = {"data": None, "mtime": 0.0}


def copyright_cn_map() -> dict:
    """返回 {copyright(小写): [中文名...]} 倒排索引（按 zh_works mtime 缓存）。"""
    global _cn_rev_cache
    try:
        mtime = os.path.getmtime(ZH_WORK_FILE)
    except OSError:
        return {}
    with _zh_lock:
        if (_cn_rev_cache["data"] is not None
                and _cn_rev_cache["mtime"] == mtime):
            return _cn_rev_cache["data"]
        rev: dict = {}
        for cn, cps in load_zh_works().items():
            for cp in cps:
                if cp:
                    rev.setdefault(cp.lower(), []).append(cn)
        _cn_rev_cache = {"data": rev, "mtime": mtime}
        return rev


def copyright_to_cn(cp: str) -> str:
    """copyright 标签 -> 中文作品名；无则返回空串。"""
    if not cp:
        return ""
    names = copyright_cn_map().get(cp.lower())
    return names[0] if names else ""


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


def resolve_multi(text: str, max_roles: int = 3) -> list[list[dict]]:
    """从一段文本里解析出【多个不同角色】（用于多角色同框）。

    返回 list[list[dict]]：每个内层列表 = 该角色的一组候选（首项为最佳）。
    例： resolve_multi("碧蓝航线的长门和赤城")
      -> [ [nagato_(azur_lane)...], [akagi_(azur_lane)...] ]

    策略（本地规则，无 LLM）：
      1) 若文本本身是逗号分隔的英文标签/别名 → 逐段解析
      2) 中文：match_zh_names 得全部命中，按【位置不重叠】贪心取
         （避免"长门酱"和"长门"重叠双计；作品线索只服务其所在角色）
      3) 每个选中词交给 _resolve_one 返回候选
      4) 别名表(aliases.json)命中也能多角色
    按文本出现顺序返回；同一 character 去重。
    """
    text = (text or "").strip()
    if not text:
        return []
    out: list[list[dict]] = []
    seen_char: set[str] = set()

    def _push(cands: list[dict]):
        if not cands:
            return
        top = cands[0].get("character", "")
        if top in seen_char:
            return
        seen_char.add(top)
        out.append(cands)

    def _resolve_one(seg: str) -> list[dict]:
        """单段解析：整段作为一个角色（含消歧），不递归展开多角色。

        用原始 text 做作品线索消歧（seg 可能是剥离上下文的孤立角色词，
        而"碧蓝航线的长门"里的 azur_lane 线索在 text 中）。
        """
        seg = seg.strip().strip("，,、+和与跟及")
        if not seg:
            return []
        cands = resolve_from_text(seg, strict=True)
        return _disambiguate(cands, text) if cands else []

    # 1) 英文逗号分隔段（如 "shiroko_(blue_archive), hoshino_(blue_archive)"，
    #    或简短的 "长门, 赤城" 角色列表）：按逗号拆段逐个解析。
    #    注意：只有每段都是"角色名形态"(纯英文标签 或 简短无描述词的中文角色名)
    #    才走这里；含描述的自然语言句(如 "长门和拉菲，两人站在沙滩上")必须交给
    #    下方中文名映射分支，否则会把整句当角色、导致漏识别/误判场景词。
    parts = [p for p in re.split(r"[，,、]+", text) if p.strip()]
    if len(parts) > 1:
        tag_like = re.compile(r"^[a-z0-9_\-() ]+$")
        _CN_DESC = re.compile(r"[\u4e00-\u9fff]*[的在穿站着戴和与跟及了里面]")
        def _is_role_seg(p: str) -> bool:
            p = p.strip()
            if tag_like.match(p):
                return True
            # 中文段：仅当是简短角色名(≤4字且无描述/虚词)才按角色处理
            return (re.search(r"[\u4e00-\u9fff]", p)
                    and len(p) <= 4 and not _CN_DESC.search(p))
        if all(_is_role_seg(p) for p in parts):
            for p in parts:
                _push(_resolve_one(p))
            return out[:max_roles]

    # 2) 中文名映射（多命中，按位置不重叠贪心）
    zh_hits = match_zh_names(text)   # [(中文名, [角色tag...])] 长->短
    used_spans: list[tuple[int, int]] = []

    def _overlaps(s0: int, e0: int) -> bool:
        for s1, e1 in used_spans:
            if s0 < e1 and s1 < e0:
                return True
        return False

    zh_grouped: dict[str, list[tuple[int, int]]] = {}
    for k, roles in zh_hits:
        # 所有出现位置
        pos = 0
        while True:
            i = text.find(k, pos)
            if i < 0:
                break
            zh_grouped.setdefault(k, []).append((i, i + len(k)))
            pos = i + 1
    # 按【首次出现位置】升序（保持文本中角色顺序）；同位置按长度降序
    zh_order = sorted(
        zh_grouped.items(),
        key=lambda kv: (min(s for s, _ in kv[1]), -len(kv[0])))
    for k, positions in zh_order:
        for (s, e) in positions:
            if _overlaps(s, e):
                continue
            used_spans.append((s, e))
            _push(_resolve_one(k))
            if len(out) >= max_roles:
                return out

    # 3) 别名表命中（可能多条）
    alias_hits = match_aliases(text)
    if alias_hits and len(out) < max_roles:
        for _alias, role in alias_hits:
            if len(out) >= max_roles:
                break
            res = _lookup_role(role)
            if res:
                _push(_disambiguate(res, text))
    return out[:max_roles]


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
