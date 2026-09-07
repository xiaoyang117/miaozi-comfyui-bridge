"""喵梓二号 - ComfyUI 生图桥接 Web 应用（优化版）

链路：用户输入/图片 -> (可选 VLM 识别) -> 角色搜索(本地库/浏览器)
     -> LLM 生成提示词 -> 替换工作流占位符+设置尺寸 -> 提交 ComfyUI -> 返回图片

优化点：
- workflow_path 支持相对名(自动定位到 workflows/)与绝对路径
- 对话框可随时选择分辨率预设，提交前自动改写 EmptyLatentImage
- 更清晰的 SSE 事件与错误提示
"""
import itertools
import json
import os
import re
import threading
import time
from pathlib import Path

import requests as _req
from flask import (Flask, Response, jsonify, render_template, request,
                   send_file, send_from_directory, stream_with_context)

from character_lookup.query import is_built, lookup as char_lookup
from character_lookup import resolver as char_resolver
from character_lookup import tag_vocab as char_tags
from comfyui.client import ComfyUIClient
from llm.client import (LLMClient, PROMPT_SYSTEM_SPECIFIC,
                        PROMPT_WITH_CONTEXT_SPECIFIC,
                        PROMPT_MULTI_ROLE_SPECIFIC,
                        SEARCH_SYSTEM_TINY, EXTRACT_CN_SYSTEM,
                        SIZE_DECIDE_SYSTEM)
from settings import Settings
from logger import get_logger, get_request_id, set_request_id

BASE_DIR = Path(__file__).parent
WORKFLOWS_DIR = BASE_DIR / "workflows"
OUTPUTS_DIR = BASE_DIR / "outputs"

log = get_logger("app")

settings = Settings()
app = Flask(__name__, static_folder=str(BASE_DIR / "static"))
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.secret_key = "comfyui-bridge-secret"

# 单个应用实例内同时只允许一次生成，避免排队混乱
_gen_lock = threading.Lock()

# --------------------------------------------------------------------- #
# 任务注册表：记录每次生成任务（含 API / OpenAI 触发）的实时状态，
# 供网页「任务」面板轮询展示。内存态，重启即清空。
# --------------------------------------------------------------------- #
_TASKS_LOCK = threading.Lock()
_TASKS: dict = {}
_TASK_SEQ = itertools.count(1)
_TASK_SOURCE_LABEL = {"web": "Web", "sync": "同步API", "openai": "OpenAI兼容"}


def task_begin(source: str) -> str:
    """登记一个新任务，返回 task_id。"""
    tid = f"T{next(_TASK_SEQ):04d}"
    with _TASKS_LOCK:
        _TASKS[tid] = {
            "id": tid,
            "source": source,
            "source_label": _TASK_SOURCE_LABEL.get(source, source),
            "status": "running",          # running / done / error
            "step": "",                    # 当前阶段名
            "step_index": -1,              # 对应前端 chip 序号
            "msg": "排队等待…",
            "image": None,
            "prompt": "",
            "started": time.time(),
            "finished": None,
            "cost": None,
        }
    return tid


def task_update(tid: str, **kw):
    """更新任务字段（失败静默）。"""
    if not tid:
        return
    with _TASKS_LOCK:
        t = _TASKS.get(tid)
        if not t:
            return
        t.update({k: v for k, v in kw.items() if v is not None})
        t["step_index"] = _STEP_CHIP.get(t.get("step", ""), -1)


def task_finish(tid: str, ok: bool, msg: str = "", **kw):
    """任务结束：done/error + 耗时。"""
    if not tid:
        return
    with _TASKS_LOCK:
        t = _TASKS.get(tid)
        if not t:
            return
        t["status"] = "done" if ok else "error"
        t["msg"] = msg or t.get("msg", "")
        t["finished"] = time.time()
        t["cost"] = round(t["finished"] - t.get("started", t["finished"]), 1)
        t.update({k: v for k, v in kw.items() if v is not None})


def api_tasks():
    """列出任务（新的在前）。"""
    with _TASKS_LOCK:
        items = sorted(_TASKS.values(),
                       key=lambda x: x.get("started", 0), reverse=True)
        return items[:100]


# SSE step -> 面板 chip 序号（与前端 STEP_MAP 对应）
_STEP_CHIP = {"vlm": 0, "search": 1, "llm": 2, "size": 3, "comfyui": 4}


@app.before_request
def _log_request():
    """每个 HTTP 请求打一条日志；SSE 生成请求会给线程设 request_id。"""
    rid = request.headers.get("X-Request-Id", "")
    set_request_id(rid)
    path = request.path
    if path.startswith("/outputs/"):
        log.debug("GET %s", path)
    else:
        log.info("→ %s %s", request.method, path)


# ====================================================================== #
# 工厂
# ====================================================================== #
def make_llm() -> LLMClient:
    return LLMClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        custom_system_prompt=settings.custom_system_prompt,
    )


_COMFY_CLIENTS: dict = {}
_COMFY_LOCK = threading.Lock()


def make_comfy(shared: bool = True) -> ComfyUIClient:
    """获取 ComfyUIClient。

    shared=True: 复用常驻实例（主生成流程用——主流程本就 _gen_lock 串行）。
    shared=False: 每次新建独立实例（图库二采用——避免与生成任务抢
    session/client_id，即使生成中二采也能提交）。

    模型驻留在 ComfyUI 服务端，与客户端实例无关；是否共享只影响
    本地 HTTP 连接与提交通道。
    """
    url = settings.comfyui_url or ""
    if not shared:
        return ComfyUIClient(server_url=url, output_dir=OUTPUTS_DIR)
    with _COMFY_LOCK:
        c = _COMFY_CLIENTS.get(url)
        if c is None or c.server_url != url.rstrip("/"):
            c = ComfyUIClient(server_url=url, output_dir=OUTPUTS_DIR)
            _COMFY_CLIENTS[url] = c
        return c


# ====================================================================== #
# VLM 图片识别
# ====================================================================== #
def vlm_analyze(image_list: list) -> str:
    """调用 VLM 逐张描述图片中的角色外貌。"""
    if not image_list:
        raise RuntimeError("没有图片数据")
    base_url, api_key, model = (settings.vlm_base_url,
                                settings.vlm_api_key, settings.vlm_model)
    if not base_url or not model:
        raise RuntimeError("VLM 未配置（请到『配置』页填写图片识别 API）")

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if len(image_list) == 1:
        prompt_text = "请详细描述这张图片中的角色外貌特征，包括发色、瞳色、体型、服装等。"
    else:
        prompt_text = ("请逐一描述每张图片中的角色外貌特征，按图片顺序标注"
                       "（图片1、图片2...），包括发色、瞳色、体型、服装等。")
    content_parts = [{"type": "text", "text": prompt_text}]
    for img in image_list:
        content_parts.append({"type": "image_url",
                              "image_url": {"url": img}})

    body = {
        "model": model,
        "messages": [{"role": "user", "content": content_parts}],
        "temperature": 0.1,
    }
    try:
        resp = _req.post(url, headers=headers, json=body, timeout=60)
    except _req.exceptions.ConnectionError:
        raise RuntimeError(f"无法连接到 VLM ({base_url})")
    except _req.exceptions.Timeout:
        raise RuntimeError("VLM 请求超时")

    if not resp.ok:
        raise RuntimeError(f"VLM API 错误 (HTTP {resp.status_code}): "
                           f"{resp.text[:300]}")
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"VLM 返回非 JSON:\n{resp.text[:300]}")

    if "error" in data:
        err = data["error"]
        err = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        raise RuntimeError(f"VLM 错误: {err}")

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("VLM 返回格式异常")


# ====================================================================== #
# 工作流
# ====================================================================== #
def load_workflow(path: str = None) -> dict:
    """加载工作流。path 可为绝对路径、相对 workflows 的文件名，或为空用默认。"""
    path = path or settings.workflow_path
    cand = Path(path)
    if not cand.is_absolute():
        cand = WORKFLOWS_DIR / cand.name
    if not cand.exists():
        raise FileNotFoundError(f"工作流文件不存在: {cand}")
    return ComfyUIClient.load_workflow(str(cand))


# ====================================================================== #
# 角色候选格式化 / LLM 翻译兜底
# ====================================================================== #
def _fmt_candidates(cands: list) -> str:
    """把候选角色 dict 列表格式化为 prompt 上下文文本。"""
    lines = []
    for i, d in enumerate(cands[:3], 1):
        lines.append(f"[候选{i}] 角色: {d['character']} | 作品: {d['copyright']} "
                     f"| 触发词: {d['trigger']}")
        core = d.get("core_tags") or ""
        tags = [t.strip() for t in core.split(",") if t.strip()]
        if tags:
            lines.append(f"   特征标签: {', '.join(tags[:60])}")
    return "\n".join(lines)


def _fmt_best_candidate(d: dict) -> str:
    """只格式化最匹配的那个角色（供 LLM/复选参考，避免多候选互相干扰）。"""
    lines = [f"角色: {d['character']} | 作品: {d['copyright']} "
             f"| 触发词: {d.get('trigger', '')}"]
    core = d.get("core_tags") or ""
    tags = [t.strip() for t in core.split(",") if t.strip()]
    if tags:
        lines.append(f"特征标签: {', '.join(tags[:80])}")
    return "\n".join(lines)


# 互斥特征组：同一组内不同值冲突（如不同发色/瞳色/体型），强制补回时只取第一个
_CONFLICT_GROUPS = [
    {"black_hair", "white_hair", "blonde_hair", "brown_hair", "red_hair",
     "blue_hair", "green_hair", "purple_hair", "pink_hair", "grey_hair",
     "silver_hair", "orange_hair", "multicolored_hair", "two-tone_hair"},
    {"red_eyes", "blue_eyes", "green_eyes", "yellow_eyes", "purple_eyes",
     "brown_eyes", "black_eyes", "pink_eyes", "grey_eyes", "heterochromia"},
    {"small_breasts", "medium_breasts", "large_breasts", "huge_breasts"},
    {"short_hair", "long_hair", "very_long_hair", "medium_hair"},
]


def _core_tags_conflict(tag: str, chosen: list[str]) -> bool:
    """tag 与已选标签是否同组冲突（强制补回时避免把两个发色/瞳色都塞进去）。"""
    for grp in _CONFLICT_GROUPS:
        if tag in grp:
            return any(c in grp for c in chosen)
    return False


def _slugify_tag(t: str) -> str:
    """规范化标签为词库 slug（小写下划线）。"""
    return t.strip().lower().replace(" ", "_").strip("_")


def _arrange_multirole(tag_list: list[str],
                       roles_meta: list[dict],
                       pos_hint: str = "") -> str:
    """多角色时按角色分块重排提示词，让特征贴着自己的角色名，
    减少 SD/Qwen 系模型跨角色串特征。

    结构： 角色1名+版权, 角色1专属特征,
           角色2名+版权, 角色2专属特征,
           公共特征(≥2角色共有),
           其余画面标签,
           位置锚定(左/右)。
    单角色时不重排，返回原顺序逗号串。
    """
    if not tag_list:
        return ""
    if len(roles_meta) < 2:
        return ", ".join(tag_list)

    # 每个角色的特征集（规范化 slug）
    role_sets = []
    for rm in roles_meta:
        s = set()
        for t in re.split(r"[,\n]", rm.get("core_tags", "")):
            t2 = _slugify_tag(t)
            if t2:
                s.add(t2)
        role_sets.append(s)

    # 归类 tag_list 中每个元素
    uniq_head = []   # 唯一特征(仅1角色有)
    shared = []      # 公共特征(多角色共有)
    rest = []        # 不属于任何角色的(画面/姿态等)
    for tg in tag_list:
        s2 = _slugify_tag(tg)
        owners = [i for i, s in enumerate(role_sets) if s2 in s]
        if len(owners) >= 2:
            shared.append(tg)
        elif len(owners) == 1:
            uniq_head.append((owners[0], tg))
        else:
            rest.append(tg)

    # 组装：按角色顺序收集"该角色的特征"
    blocks = []
    for i, rm in enumerate(roles_meta):
        parts = [rm.get("query", "")]
        cp = rm.get("copyright", "")
        if cp and cp.lower() not in (rm.get("query", "").lower()):
            parts.append(cp)
        # 该角色的专属特征（保持词库/输出顺序）
        own = [tg for (oi, tg) in uniq_head if oi == i]
        # 若该角色没有任何专属特征残留（全被当公共），至少保证角色名在
        blocks.append(", ".join(parts + own))

    # 公共特征、画面标签
    tail = shared + rest
    if tail:
        blocks.append(", ".join(tail))
    text = ", ".join(blocks)

    # 位置/互动描述：优先 LLM 在草稿写的构图说明；没有再程序兜底左右
    if len(roles_meta) == 2:
        if pos_hint:
            text = f"{text}, {pos_hint}"
        else:
            r1 = roles_meta[0].get("query", "").split("(")[0].strip("_")
            r2 = roles_meta[1].get("query", "").split("(")[0].strip("_")
            if r1 and r2 and r1 != r2:
                text = f"{text}, {r1} on the left, {r2} on the right"
    elif pos_hint:
        text = f"{text}, {pos_hint}"
    return text


_COMP_BLOCK = re.compile(
    r"\[composition\](.*?)\[/composition\]", re.IGNORECASE | re.S)


def _extract_position_hint(draft: str,
                           roles_meta: list[dict]) -> str:
    """从草稿提取 LLM 写的 [composition] 构图说明（自然语言，不校验）。

    返回规范化的位置/互动短句（如 "nagato on the left, akagi on the right"），
    空串表示 LLM 没写（调用方决定兜底）。
    """
    if not draft:
        return ""
    m = _COMP_BLOCK.search(draft)
    if not m:
        return ""
    body = m.group(1).strip().strip(",。 ")
    if not body:
        return ""
    # 去掉可能混入的其它标签性杂质：只要含位置/互动词才保留
    low = body.lower()
    pos_words = ("left", "right", "behind", "front", "next to", "beside",
                 "holding", "looking at", "embracing", "leaning",
                 "standing", "sitting", "behind")
    if any(w in low for w in pos_words):
        return body[:160]
    return ""


# 画面标签选取数量随草稿丰富度变化：草稿越长（需求越复杂），画面区需补的越多。
# 以草稿的"标签段数"为指标：按逗号/换行切段，纯英文自然语言按单词粗算。
def _scene_pick_range(draft: str | None) -> tuple[int, int]:
    """根据草稿长度估算画面区应选标签数区间 (lo, hi)。

    映射（标签段数 n）：
      n<=2   -> 2~5   极简需求(只有角色名), 少量画面点缀
      n<=8   -> 4~8   简单需求
      n<=16  -> 7~12  中等
      其它    -> 10~16 复杂需求
    """
    if not draft or not draft.strip():
        return 8, 15
    segs = [s.strip() for s in re.split(r"[,\n]", draft) if s.strip()]
    # 若切出来的段数很少但草稿很长（自然语言长句），按单词数粗估
    n = len(segs)
    total_chars = len(draft)
    if n <= 3 and total_chars > 60:
        n = min(20, max(n, total_chars // 12))
    if n <= 2:
        return 2, 5
    if n <= 8:
        return 4, 8
    if n <= 16:
        return 7, 12
    return 10, 16


# --------------------------------------------------------------------- #
# 提示词渲染风格（anima 空格 / danbooru 下划线）
# Anima/Qwen 系底模训练用小写+空格标签，danbooru 系吃下划线标签。
# --------------------------------------------------------------------- #
_KEEP_UNDERSCORE = re.compile(r"^(?:score|quality|year)_[0-9]+$|^year_\d{4}$", re.I)


def _anima_segment(seg: str) -> str:
    """把单个标签段转 Anima 空格格式。

    - 角色名 nagato_(azur_lane) -> nagato (azur lane)
    - 普通标签 white_hair -> white hair
    - score_7 / year_2025 等保留下划线
    """
    seg = (seg or "").strip()
    if not seg:
        return seg
    # 角色名/版权名形态 xxx_(yyy)[_(zzz)]
    m = re.fullmatch(
        r"([a-z0-9][a-z0-9_\-']*?)_\(([a-z0-9_\-]+)\)", seg, re.I)
    if m:
        core = m.group(1).replace("_", " ")
        paren = m.group(2).replace("_", " ")
        return f"{core} ({paren})"
    if _KEEP_UNDERSCORE.fullmatch(seg):
        return seg
    # 一般标签：下划线 -> 空格（已含空格的保持不变）
    return seg.replace("_", " ")


def _to_render_style(prompt: str, wf_path: str = "") -> str:
    """内部提示词 -> 最终渲染风格。

    settings.prompt_style:
      danbooru -> 原样(下划线)
      anima    -> 强制空格
      auto     -> 工作流名含 anima/qwen/miao/harem 才转空格
    """
    if not prompt:
        return prompt
    style = settings.prompt_style
    if style == "danbooru":
        return prompt
    if style == "auto":
        name = (wf_path or settings.workflow_path or "").lower()
        if not any(k in name for k in ("anima", "qwen", "miao", "harem")):
            return prompt
    # 按 逗号/分号+可选空格 拆分（保留分隔符与后续空格），每段转空格后拼回
    parts = re.split(r"(,|;)", prompt)
    out = []
    for i, p in enumerate(parts):
        if p in (",", ";"):
            out.append(p)
        elif p.strip():
            # 保留段首空格(分隔符后的排版空格)，用段内容的原始前导空格
            lead = p[:len(p) - len(p.lstrip())]
            out.append(lead + _anima_segment(p))
        else:
            out.append(p)
    return "".join(out)


def _looks_like_tag(s: str) -> bool:
    """判断字符串是否为纯 danbooru 标签样式（英文/数字/下划线/括号/逗号）。"""
    import re as _re
    return bool(_re.fullmatch(r"[A-Za-z0-9_\-(),.\s]+", s or ""))


def _translate_and_lookup(cn_name: str, llm: LLMClient,
                          history: list) -> list:
    """把中文角色名翻译成 danbooru 标签并查库；成功则记住别名。"""
    try:
        retry = (f"角色中文名: {cn_name}\n"
                 f"请给出这个角色的 danbooru 英文标签（罗马音），"
                 f"只输出：角色标签名, 作品标签名。不知道作品就只输出角色名。")
        q2 = llm._call("你是一个角色名翻译工具，只输出英文标签。", retry, history)
        q2 = (q2 or "").strip()
        if not q2:
            return []
        cands = char_resolver.resolve_from_text(q2)
        if cands:
            # 记住：下次遇到这个中文名直接命中
            char_resolver.save_alias(cn_name, cands[0].get("character", ""))
        return cands
    except Exception as e:
        log.error("[translate error] %s", e)
        return []


# ====================================================================== #
# 分辨率自动决策（规则优先，LLM 兜底）
# ====================================================================== #
# 常见比例 -> 方向（用于从用户输入中提取）
_RATIO_PATTERNS = [
    # (正则, 方向)  方向: portrait / landscape / square
    (r"\b(\d{1,2})\s*[:：]\s*(\d{1,2})\b", None),  # 动态判断比例
    (r"\b(\d{2,4})\s*[x×*]\s*(\d{2,4})\b", None),  # 显式 1024x768
]

_DIRECTION_WORDS = {
    "portrait": ["竖图", "竖版", "竖屏", "头像", "立绘", "全身", "半身",
                 "portrait", "vertical", "mobile wallpaper", "手机壁纸",
                 "single", "solo", "1girl", "1boy"],
    "landscape": ["横图", "横版", "横屏", "桌面壁纸", "风景", "多人",
                  "landscape", "horizontal", "desktop wallpaper", "wallpaper",
                  "wide", "poster", "海报", "全景"],
    "square": ["方图", "正方形", "方形", "square"],
}

# 方向 -> 默认候选尺寸（会被预设覆盖，此处是兜底）
_FALLBACK_SIZES = {
    "portrait": (832, 1216),
    "landscape": (1216, 832),
    "square": (1024, 1024),
}


def _extract_explicit_size(text: str):
    """从用户输入中提取显式宽高或比例，返回 (w, h) 或 None。"""
    import re as _re
    # 显式 "1024x768" / "1024×768"（避免 \b 因中文是 \w 而失效）
    m = _re.search(r"(?<!\d)(\d{2,4})\s*[x×*]\s*(\d{2,4})(?!\d)", text, _re.I)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        return _sanitize_size(w, h)
    # 比例 "16:9" / "3:4"
    m = _re.search(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{1,2})(?!\d)", text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a and b:
            # 依据比例推断方向，用 1024 基准
            base = 1024
            if a > b:
                h = base
                w = round(base * a / b / 64) * 64
            elif b > a:
                w = base
                h = round(base * b / a / 64) * 64
            else:
                w = h = base
            return _sanitize_size(w, h)
    return None


def _sanitize_size(w: int, h: int):
    """把尺寸规整到 64 的倍数，限制在合理范围。"""
    w = max(256, min(round(w / 64) * 64, 4096))
    h = max(256, min(round(h / 64) * 64, 4096))
    return w, h


def _direction_from_text(text: str):
    """规则层：从文本中判断方向，返回 portrait/landscape/square 或 None。"""
    t = (text or "").lower()
    for direction, words in _DIRECTION_WORDS.items():
        for w in words:
            if w.lower() in t:
                return direction
    return None


def _pick_size_for_direction(direction: str):
    """依据方向从预设里挑一个合适尺寸，否则用兜底。"""
    presets = settings.resolution_presets
    # 从预设里找最贴合方向的
    for p in presets:
        w, h = int(p.get("width", 0)), int(p.get("height", 0))
        if not w or not h:
            continue
        if direction == "portrait" and h > w:
            return w, h
        if direction == "landscape" and w > h:
            return w, h
        if direction == "square" and w == h:
            return w, h
    # 预设里没有贴合方向 → 用默认 gen 尺寸
    dw, dh = settings.gen_width, settings.gen_height
    if direction == "portrait" and dw >= dh:
        dw, dh = dh, dw
    elif direction == "landscape" and dh >= dw:
        dw, dh = dh, dw
    elif direction == "square":
        s = max(dw, dh)
        dw = dh = s
    return _sanitize_size(dw, dh)


def decide_resolution(user_input: str, llm: LLMClient,
                      history: list = None):
    """决定输出分辨率，返回 (w, h, via)。规则优先，LLM 兜底。

    via: explicit(用户显式写尺寸) / rule(规则词) / llm(LLM判断) / fallback
    """
    # 1) 显式尺寸/比例
    explicit = _extract_explicit_size(user_input)
    if explicit:
        return explicit[0], explicit[1], "explicit"

    # 2) 规则词（横/竖/方）
    direction = _direction_from_text(user_input)
    if direction:
        w, h = _pick_size_for_direction(direction)
        return w, h, f"rule:{direction}"

    # 3) LLM 判断方向（兜底，只问一个词）
    try:
        ans = (llm._call(SIZE_DECIDE_SYSTEM, user_input, history) or "").lower()
        if "portrait" in ans:
            w, h = _pick_size_for_direction("portrait")
            return w, h, "llm:portrait"
        if "landscape" in ans:
            w, h = _pick_size_for_direction("landscape")
            return w, h, "llm:landscape"
        if "square" in ans:
            w, h = _pick_size_for_direction("square")
            return w, h, "llm:square"
    except Exception as e:
        log.error("[size-decide error] %s", e)

    # 4) 全部失败 → 默认
    w, h = settings.gen_width, settings.gen_height
    w, h = _sanitize_size(w, h)
    return w, h, "fallback"




# ====================================================================== #
# 页面
# ====================================================================== #
@app.route("/")
def index():
    return render_template("index.html")


# ====================================================================== #
# 配置 / 测试接口
# ====================================================================== #
@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify({"success": True, "settings": settings.get_all()})
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({"error": "无效的配置数据"}), 400
    settings.update(data)
    return jsonify({"success": True, "settings": settings.get_all()})


@app.route("/api/workflows")
def api_workflows():
    items = []
    if WORKFLOWS_DIR.exists():
        for f in sorted(WORKFLOWS_DIR.glob("*.json")):
            items.append({"path": str(f), "name": f.name})
    return jsonify({"success": True, "workflows": items})


def _safe_wf_name(name: str):
    """校验工作流文件名：仅允许 .json 结尾的合法文件名，防路径穿越。返回文件名或 None。"""
    name = (name or "").strip()
    if not name or not name.endswith(".json"):
        return None
    if name != name.replace("\\", "/").split("/")[-1]:
        return None  # 含路径分隔符
    if name in (".", "..", "") or any(ch in name for ch in '<>:"|?*'):
        return None
    return name


@app.route("/api/workflows/<path:name>", methods=["GET"])
def api_workflow_get(name):
    """读取单个工作流文件内容。"""
    fname = _safe_wf_name(name)
    if not fname:
        return jsonify({"success": False, "error": "非法文件名"}), 400
    p = WORKFLOWS_DIR / fname
    if not p.exists():
        return jsonify({"success": False, "error": f"工作流文件不存在: {fname}"}), 404
    try:
        content = p.read_text(encoding="utf-8")
        return jsonify({"success": True, "name": fname, "content": content})
    except Exception as e:
        return jsonify({"success": False, "error": f"读取失败: {e}"}), 500


@app.route("/api/workflows/save", methods=["POST"])
def api_workflow_save():
    """保存工作流。body: {name: 现有文件名, content: JSON 文本,
    new_name?: 另存为的新文件名（此时不覆盖原文件）}。"""
    data = request.get_json() or {}
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        return jsonify({"success": False, "error": "内容不能为空"}), 400
    # 校验是合法 JSON（工作流必须是 JSON）
    try:
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return jsonify({"success": False, "error": "工作流内容必须是 JSON 对象"}), 400
    except json.JSONDecodeError as e:
        return jsonify({"success": False, "error": f"JSON 格式错误: {e}"}), 400

    new_name = _safe_wf_name(data.get("new_name"))
    name = _safe_wf_name(data.get("name"))
    # 另存：以 new_name 为准；覆盖：name 必须存在
    if new_name:
        target = new_name
    elif name:
        if not (WORKFLOWS_DIR / name).exists():
            return jsonify({"success": False, "error": f"原文件不存在: {name}"}), 404
        target = name
    else:
        return jsonify({"success": False, "error": "缺少文件名"}), 400

    try:
        WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
        p = WORKFLOWS_DIR / target
        # 用 json.dumps 规范格式写回（保留 ensure_ascii=False）
        p.write_text(json.dumps(parsed, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return jsonify({"success": True, "name": target,
                        "message": "已保存 ✅" if target == name
                                   else f"已另存为 {target}"})
    except Exception as e:
        return jsonify({"success": False, "error": f"保存失败: {e}"}), 500


@app.route("/api/test/llm", methods=["POST"])
def test_llm():
    data = request.get_json() or {}
    try:
        llm = LLMClient(
            base_url=data.get("llm_base_url") or settings.llm_base_url,
            api_key=data.get("llm_api_key") or settings.llm_api_key,
            model=data.get("llm_model") or settings.llm_model,
            custom_system_prompt=(data.get("custom_system_prompt")
                                  or settings.custom_system_prompt))
        result = llm._call(llm._prompt_system,
                           "a cat sitting on a windowsill", [])
        return jsonify({"success": True, "prompt": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/test/llm/raw", methods=["POST"])
def test_llm_raw():
    data = request.get_json() or {}
    base_url = (data.get("llm_base_url") or settings.llm_base_url).rstrip("/")
    api_key = data.get("llm_api_key") or settings.llm_api_key
    model = data.get("llm_model") or settings.llm_model
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Reply with: test ok"},
            {"role": "user", "content": "ping"},
        ],
        "temperature": 0.1,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        resp = _req.post(f"{base_url}/chat/completions", headers=headers,
                         json=body, timeout=30)
        return jsonify({"success": resp.ok, "status": resp.status_code,
                        "body": resp.text[:2000]})
    except Exception as e:
        return jsonify({"success": False, "status": 0,
                        "body": f"请求失败: {e}"})


@app.route("/api/test/comfyui", methods=["POST"])
def test_comfyui():
    data = request.get_json() or {}
    url = data.get("comfyui_url") or settings.comfyui_url
    try:
        client = ComfyUIClient(server_url=url)
        if client.test_connection() == "ok":
            return jsonify({"success": True})
        return jsonify({"success": False, "error": client.test_connection()})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/test/characters", methods=["POST"])
def test_characters():
    data = request.get_json() or {}
    try:
        if not is_built():
            return jsonify({
                "success": False,
                "error": "本地角色库未建立，请先运行 "
                         "python character_lookup/build_db.py 构建数据库"})
        q = (data.get("query") or "shiroko").strip()
        # 1) 直接单名查询（兼容旧接口）
        result = char_lookup(q)
        # 2) 走智能解析：英文直查/中文别名/多候选
        cands = char_resolver.resolve_from_text(q)
        if cands:
            text = char_resolver.role_candidates_text(q)
            return jsonify({"success": True,
                            "result": (result[:300] if result else text[:300]),
                            "candidates": [c["character"] for c in cands[:5]]})
        return jsonify({
            "success": False,
            "error": "未找到该角色。可尝试：\n"
                     "1. 英文/罗马音：shiroko\n"
                     "2. 中文名（内置常见角色）：白子\n"
                     "3. 中文名+系列：碧蓝档案的白子"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/test/resolve", methods=["POST"])
def test_resolve():
    """诊断接口：返回智能解析器每一步的结果，便于排查角色识别问题。"""
    data = request.get_json() or {}
    q = (data.get("query") or "").strip()
    if not q:
        return jsonify({"success": False, "error": "请输入查询内容"})
    try:
        report = char_resolver.debug_report(q)
        return jsonify({"success": True, "report": report})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ====================================================================== #
# 生成主流程：核心生成器（SSE 与同步接口共用）
# ====================================================================== #
def _run_generation(data: dict):
    """核心生图生成器：逐条 yield 事件字典。

    事件 step: vlm / search / llm / comfyui / done / error
    由 /api/generate（SSE 流）与 /api/generate/sync（同步 JSON）共同消费。
    锁由调用方管理（两个路由各自 acquire/release）。
    """
    import uuid as _uuid
    set_request_id(_uuid.uuid4().hex[:12])  # 本次生成全程共享此 id
    gid = get_request_id()
    try:
        user_input = (data.get("prompt") or "").strip()
        log.info("[%s] input: %s", gid, user_input[:100])
        if not user_input:
            yield {"step": "error", "error": "请输入图片描述"}
            return

        # ---------- 解析参数 ----------
        wf_path = data.get("workflow_path") or settings.workflow_path
        use_search = bool(data.get("use_search", True))
        history = data.get("history") or []
        auto_resolution = bool(data.get("auto_resolution", False))
        # 直通模式：外部已给标准标签，跳过 LLM 改写与角色搜索，原样提交 ComfyUI
        raw_prompt = bool(data.get("raw_prompt", False))
        if raw_prompt:
            use_search = False
        width = data.get("width") or settings.gen_width
        height = data.get("height") or settings.gen_height
        size_via = "manual"
        log.info("[%s] params: wf=%s search=%s auto_size=%s raw=%s size=%sx%s",
                 gid, Path(wf_path).name, use_search, auto_resolution,
                 raw_prompt, width, height)

        # ---------- 加载工作流 ----------
        try:
            wf = load_workflow(wf_path)
            log.info("[%s] workflow loaded: %s 节点数=%s", gid,
                     Path(wf_path).name, len(wf))
        except FileNotFoundError as e:
            log.error("[%s] workflow not found: %s", gid, e)
            yield {"step": "error", "error": str(e)}
            return
        except Exception as e:
            log.error("[%s] workflow parse error: %s", gid, e,
                      exc_info=True)
            yield {"step": "error", "error": f"工作流解析失败: {e}"}
            return

        llm = make_llm()
        comfy = make_comfy()

        # ---------- Step 0: VLM ----------
        image_list = data.get("image") or []
        if isinstance(image_list, str):
            image_list = [image_list] if image_list else []
        vlm_description = None
        if image_list:
            yield {"step": "vlm", "msg": f"🖼️ 识别 {len(image_list)} 张参考图…"}
            log.info("[%s] vlm: %d 张图", gid, len(image_list))
            try:
                vlm_description = vlm_analyze(image_list)
                log.info("[%s] vlm done: %s 字", gid,
                         len(vlm_description or ""))
                yield {"step": "vlm",
                       "msg": f"✓ 图片识别完成（{len(vlm_description or '')} 字）"}
            except RuntimeError as e:
                log.error("[%s] vlm failed: %s", gid, e)
                yield {"step": "error", "error": f"图片识别失败: {e}"}
                return
            user_input = (f"用户上传了一张图片，以下是图片识别结果：\n"
                          f"{vlm_description}\n\n用户需求：{user_input}")

        # ---------- Step 1: 角色识别（规则优先，LLM 兜底） ----------
        # roles_meta: 本次命中的角色（1~N 个），每个含 best 单角色信息
        search_info = None
        raw = None
        best_ref = None   # 最匹配角色的参考文本（喂模型用，避免多候选干扰）
        roles_meta: list[dict] = []   # [{query, copyright, core_tags, ref}]
        char_core_tags = ""   # 命中角色的 core_tags 汇总（逗号拼接，供标签选词）
        role_hint = (data.get("role") or "").strip()
        if use_search:
            yield {"step": "search", "msg": "🔎 正在识别角色…"}
            cands: list = []
            multi_cands: list[list] = []   # 每个角色一组候选（多角色）
            via = ""
            role_missed = False
            try:
                # (0) 手动指定角色（最高优先级，完全绕过识别）
                if role_hint:
                    if "," in role_hint or "，" in role_hint:
                        # 逗号分隔的多个角色 -> 各自解析
                        for seg in re.split(r"[，,]+", role_hint):
                            sub = char_resolver.resolve_from_text(
                                seg.strip(), strict=True)
                            if sub:
                                multi_cands.append(sub)
                                via = "手动指定(多角色)"
                            else:
                                role_missed = True
                        cands = multi_cands[0] if multi_cands else []
                    else:
                        cands = char_resolver.resolve_from_text(role_hint,
                                                                strict=True)
                        if cands:
                            via = "手动指定"
                            # 用户明确给的中文称呼 -> 记住别名
                            char_resolver.learn(role_hint, cands[0], via)
                        else:
                            role_missed = True
                            # 若输入是自由文本（非纯标签样式），允许结合描述再查一次
                            if not _looks_like_tag(role_hint):
                                cands = char_resolver.resolve_from_text(
                                    f"{role_hint} {user_input}")
                                via = "手动指定(结合描述)"
                # (1) 规则层：英文直查 / 中文别名表 / 多候选 —— 无需 LLM
                if not cands and not multi_cands:
                    if role_hint and role_missed and not _looks_like_tag(
                            role_hint):
                        # 手动中文名 strict 未中：结合描述再查一次
                        sub = char_resolver.resolve_from_text(
                            f"{role_hint} {user_input}")
                        if sub:
                            multi_cands = [sub]
                            cands = sub
                            via = "手动指定(结合描述)"
                    if not cands and not multi_cands:
                        multi_cands = char_resolver.resolve_multi(user_input)
                        if multi_cands:
                            cands = multi_cands[0]
                            via = "本地库直查(无LLM)"

                # (2) LLM 兜底层：仅当规则层没命中时才调用
                if not cands and not multi_cands:
                    # 先让 LLM 给英文标签（提示词已对小模型简化）
                    q = ""
                    try:
                        q = llm._call(SEARCH_SYSTEM_TINY, user_input, [])
                    except Exception as e:
                        log.warning("[llm-extract error] %s", e)
                    if q:
                        cands = char_resolver.resolve_from_text(q)
                        via = "LLM英文标签"
                    # 若仍未命中：抽取中文角色名，走别名/翻译
                    if not cands:
                        cn = ""
                        try:
                            cn = llm._call(EXTRACT_CN_SYSTEM,
                                           user_input, []) or ""
                        except Exception as e:
                            log.warning("[llm-cn error] %s", e)
                        cn = cn.strip()
                        if cn and cn != "未知":
                            cands = char_resolver.resolve_from_text(cn)
                            via = "中文名→别名"
                        if not cands and cn and cn != "未知":
                            cands = _translate_and_lookup(cn, llm, history)
                            via = "LLM翻译"
                    if cands:
                        multi_cands = [cands]

                # (3) 组装结果（多角色）
                if multi_cands:
                    roles_meta = []
                    raw_parts = []
                    ref_parts = []
                    for grp in multi_cands:
                        if not grp:
                            continue
                        best = grp[0]
                        roles_meta.append({
                            "query": best.get("character", ""),
                            "copyright": best.get("copyright", ""),
                            "core_tags": best.get("core_tags", "") or "",
                            "ref": _fmt_best_candidate(best),
                        })
                        raw_parts.append(_fmt_candidates(grp))
                        ref_parts.append(best.get("character", ""))
                    if not roles_meta:
                        multi_cands = []
                        cands = []
                    else:
                        raw = "\n".join(raw_parts)
                        best_ref = "\n\n".join(r["ref"] for r in roles_meta)
                        char_core_tags = ",".join(
                            r["core_tags"] for r in roles_meta)
                        main_role = roles_meta[0]
                        search_info = {
                            "query": main_role["query"],
                            "copyright": main_role["copyright"],
                            "via": via,
                            "role_missed": role_missed,
                            "candidates": [g[0].get("character", "")
                                           for g in multi_cands],
                            "results": (raw or "")[:800],
                            "role_count": len(roles_meta),
                        }
                        names = " + ".join(ref_parts)
                        log.info("[%s] search hit via=%s -> %s (%d 角色)",
                                 gid, via, names, len(roles_meta))
                        yield {"step": "search",
                               "msg": f"✓ 角色识别：{names} ({via})"}
                        # 仅当用户显式给出中文角色（role框/描述直名）时学习别名，
                        # 避免把 LLM 猜错的结果或整句描述写进别名表。
                        # 学习统一由 _translate_and_lookup 以精确中文名触发。
                if not multi_cands:
                    log.warning("[search] 未找到角色: %s", user_input[:60])
                    search_info = {"query": "", "via": via,
                                   "results": "",
                                   "error": "未找到匹配角色，已按无角色参考继续"}
                    yield {"step": "search",
                           "msg": "⚠️ 未匹配到角色，按无角色参考继续"}
            except Exception as e:
                log.error("[search error] %s", e)
                search_info = {"query": "", "results": "",
                               "error": str(e)}

        # ---------- Step 2: LLM 生成提示词（直通模式则跳过） ----------
        prompt = ""
        if raw_prompt:
            # 直通：外部已给标准标签，原样作为正向提示词
            prompt = user_input
            log.info("[%s] raw prompt passthrough (%d 字符)", gid,
                     len(prompt))
        else:
            yield {"step": "llm",
                   "msg": "🧠 生成提示词中（本地模型较慢，通常 30~60 秒，请耐心）…"}
            try:
                # 第一步：先自由生成"草稿"（带标签风格引导）
                draft = None
                draft_pos_hint = ""   # 草稿中的角色位置/互动描述（单角色多候选用）
                multi_role_direct = False  # 多角色结构化块直通(不走词库过滤)
                if search_info and best_ref:
                    ctx = f"角色参考资料:\n{best_ref}\n\n用户需求: {user_input}"
                    if len(roles_meta) >= 2:
                        # 方案A：多角色结构化块。每个角色资料已含在 best_ref，
                        # 要求 LLM 按角色独立分块(外貌+服装+动作+位置)。
                        ctx = (f"角色资料(每个角色一段, 必须全部保留并展开成独立块):\n"
                               f"{best_ref}\n\n"
                               f"用户需求: {user_input}\n\n"
                               f"把上面的每个角色分别写成完整独立的角色块——"
                               f"含该角色的外貌/服装/动作/位置。"
                               f"同一画面内每个角色出现一次, 绝不互相串特征。")
                        draft = llm._call(PROMPT_MULTI_ROLE_SPECIFIC,
                                          ctx, history)
                        multi_role_direct = bool(draft)
                    else:
                        # 单角色：自由草稿
                        draft = llm._call(PROMPT_WITH_CONTEXT_SPECIFIC,
                                          ctx, history)
                        draft_pos_hint = _extract_position_hint(
                            draft, roles_meta)
                else:
                    sp = PROMPT_SYSTEM_SPECIFIC if use_search \
                        else llm._prompt_system
                    draft = llm._call(sp, user_input, history)
                # 剥掉草稿里的 [composition] 块（位置句已单独提取，
                # 避免它污染词库校验/复选/直通 prompt）
                if draft and not multi_role_direct:
                    draft = _COMP_BLOCK.sub("", draft or "").strip(
                        " ,，、\n")

                # 标签化处理：词库可用 + 总开关开启
                # 多角色结构化块直通时不进词库(草稿即最终 prompt)
                tag_prompt = None
                if not multi_role_direct and \
                        settings.tag_selection and char_tags.is_available():
                    if settings.tag_reselect:
                        # 复选开：每个角色的特征单独成组(尽量多选保本体)，
                        # 画面标签一个池（数量随草稿长度），一次调用挑完。
                        trait_groups: list[list[str]] = []
                        if roles_meta:
                            for rm in roles_meta:
                                grp = []
                                for t in re.split(r"[,\n]",
                                                  rm.get("core_tags", "")):
                                    t2 = t.strip().replace(" ", "_")
                                    if t2 and t2 not in grp:
                                        grp.append(t2)
                                if grp:
                                    trait_groups.append(grp)
                        else:
                            # 无角色库命中：从草稿/输入取不了官方特征，
                            # 角色特征区退化为空（纯画面复选）
                            trait_groups = []
                        scene_cands = char_tags.recall_candidates(
                            draft or user_input, limit=180)
                        n_roles = len(trait_groups)
                        if (n_roles or len(scene_cands) >= 20):
                            ctx_parts = [f"用户需求：{user_input}"]
                            if draft:
                                ctx_parts.append(f"参考草稿：{draft}")
                            if n_roles:
                                zone_parts = []
                                for gi, grp in enumerate(trait_groups, 1):
                                    label = (f"角色{gi}特征区（角色{gi}的"
                                             "形象特征，尽量多选其中与角色"
                                             "相符的；区内若冲突(如不同发色)"
                                             "只选一个）")
                                    zone_parts.append(label + "：\n"
                                                      + ", ".join(grp))
                                zone_text = "\n\n".join(zone_parts)
                                if n_roles > 1:
                                    zone_text = (
                                        "画面包含多个角色，每个角色的特征区"
                                        "都要尽量多选，保证两人形象完整。\n"
                                        + zone_text)
                                ctx_parts.append(zone_text)
                            if scene_cands:
                                s_lo, s_hi = _scene_pick_range(draft)
                                # 候选不足时收窄上限
                                s_hi = min(s_hi, len(scene_cands))
                                ctx_parts.append(
                                    f"画面区（从画面区选 {s_lo}~{s_hi} 个标签，"
                                    "覆盖姿态/场景/服饰/氛围；"
                                    "多角色时补充 2girls/multiple_girls/"
                                    "siblings 等人数标签；草稿已合适的画面"
                                    "标签可保留并计入此数）：\n"
                                    + char_tags.format_candidates(scene_cands))
                            ctx = "\n\n".join(ctx_parts)
                            try:
                                picked = llm._call(TAG_SELECT_SYSTEM, ctx, [])
                            except Exception:
                                picked = ""
                            if picked:
                                tag_prompt = char_tags.validate_tags(picked)
                                # 角色特征高优先级兜底：每组内补回未冲突特征
                                if trait_groups:
                                    kept = set(tag_prompt)
                                    for grp in trait_groups:
                                        for t in grp:
                                            if t in kept:
                                                continue
                                            # 组内已有同维度冲突特征则跳过
                                            conflict = any(
                                                c in kept and c in grp
                                                and _core_tags_conflict(t, [c])
                                                for c in grp)
                                            if not conflict:
                                                tag_prompt.append(t)
                                                kept.add(t)
                                log.info(
                                    "[%s] 标签复选: %d角色特征%d 画面%d -> %d",
                                    gid, len(trait_groups),
                                    sum(len(g) for g in trait_groups),
                                    len(scene_cands), len(tag_prompt))
                    if not tag_prompt:
                        # 复选关（或复选失败）：直接校验草稿，剔除不在词库的词
                        tag_prompt = char_tags.validate_tags(draft or "")
                        log.info("[%s] 标签校验(无复选): %d 字 -> %d 标签",
                                 gid, len(draft or ""), len(tag_prompt))

                if tag_prompt:
                    # 校验/复选出的真实标签作为提示词主体
                    # 多角色时按角色分块（特征贴角色名）防串
                    if len(roles_meta) >= 2:
                        prompt = _arrange_multirole(
                            tag_prompt, roles_meta, draft_pos_hint)
                    else:
                        prompt = ", ".join(tag_prompt)
                else:
                    # 回退：草稿原样使用（词库不可用/总开关关/校验全被剔）
                    prompt = draft or ""
                    if prompt and len(roles_meta) >= 2:
                        if draft_pos_hint:
                            prompt = f"{prompt}, {draft_pos_hint}"
                if not prompt:
                    raise RuntimeError("LLM 返回了空提示词")
                # 角色已命中但 LLM 漏输出角色标签时，前置注入（确定性兜底，
                # 保证每个角色名与作品名一定进入提示词）
                if roles_meta:
                    pl = prompt.lower()
                    missing = []
                    for rm in roles_meta:
                        q = (rm.get("query") or "").lower()
                        main = q.split("(")[0].strip("_")
                        if not main:
                            continue
                        # 角色名或其主词出现在前部即视为已含
                        if not (q in pl[:250] or main in pl[:250]):
                            missing.append(rm)
                    if missing:
                        prefix_parts = []
                        for rm in missing:
                            seg = rm["query"]
                            cp = rm.get("copyright", "")
                            if cp:
                                seg = f"{seg}, {cp}"
                            prefix_parts.append(seg)
                        if prefix_parts:
                            prompt = ", ".join(prefix_parts) + ", " + prompt
                            log.info(
                                "[%s] 角色标签缺失，已注入前缀 %d 个: %s",
                                gid, len(missing),
                                [m["query"] for m in missing])
                log.info("[%s] llm prompt ok (%d 字符)", gid, len(prompt))
                yield {"step": "llm", "msg": "✓ 提示词已生成"}
            except Exception as e:
                log.error("[%s] llm prompt failed: %s", gid, e,
                          exc_info=True)
                yield {"step": "error", "error": f"提示词生成失败: {e}"}
                return

        # ---------- Step 2.5: 分辨率自动决策 ----------
        if auto_resolution:
            if raw_prompt and not _extract_explicit_size(user_input):
                # 直通模式：prompt 是英文标签不含方向意图，除非显式尺寸，
                # 否则不调 LLM 判断，直接用默认尺寸（LLM 可能不可用）
                width, height = settings.gen_width, settings.gen_height
                size_via = "default(raw)"
                log.info("[%s] size raw-default -> %sx%s",
                         gid, width, height)
            else:
                width, height, size_via = decide_resolution(user_input, llm,
                                                            history)
                log.info("[%s] size auto -> %sx%s (via %s)",
                         gid, width, height, size_via)

        # ---------- Step 3: ComfyUI（工作线程 + 进度转发） ----------
        # 提交前统一按底模渲染风格转换提示词(anima 空格/danbooru 下划线)
        prompt = _to_render_style(prompt, wf_path)
        if auto_resolution:
            yield {"step": "size", "msg": f"📐 尺寸：{width}×{height} ({size_via})"}
        else:
            yield {"step": "size", "msg": f"📐 尺寸：{width}×{height}"}
        yield {"step": "comfyui", "msg": "⚙️ 正在提交 ComfyUI…"}
        log.info("[%s] comfyui submit, size=%sx%s", gid, width, height)
        import queue as _queue
        import threading as _th
        import time as _time
        _cq = _queue.Queue()
        _t0 = _time.time()

        def _run_comfy():
            try:
                p = comfy.generate(
                    wf, prompt,
                    placeholder=settings.prompt_placeholder,
                    save_node_id=settings.save_node_id,
                    width=int(width) if width else None,
                    height=int(height) if height else None,
                    width_placeholder=settings.width_placeholder,
                    height_placeholder=settings.height_placeholder,
                    progress_cb=lambda msg: _cq.put(("progress", msg)),
                )
                _cq.put(("ok", p))
            except Exception as e:
                log.error("[%s] comfyui failed: %s", gid, e, exc_info=True)
                _cq.put(("err", e))

        _th.Thread(target=_run_comfy, daemon=True).start()
        path = None
        comfy_err = None
        while True:
            try:
                kind, payload = _cq.get(timeout=5)
            except _queue.Empty:
                # 心跳：ComfyUI 仍未返回，周期汇报防止连接空闲
                yield {"step": "comfyui",
                       "msg": f"⏳ 等待 ComfyUI… {int(_time.time()-_t0)}s"}
                continue
            if kind == "progress":
                yield {"step": "comfyui", "msg": f"🎨 {payload}"}
            elif kind == "ok":
                path = payload
                log.info("[%s] comfyui done -> %s",
                         gid, path.name if path else None)
                break
            else:  # err
                comfy_err = payload
                break

        if comfy_err:
            yield {"step": "error",
                   "error": f"ComfyUI 生图失败: {comfy_err}"}
            return

        if path and path.exists():
            log.info("[%s] done, 图片已保存", gid)
            ctx_usage = None
            try:
                u = getattr(llm, "last_usage", None)
                if isinstance(u, dict):
                    pt = u.get("prompt_tokens") or 0
                    ctx_usage = {"prompt_tokens": pt,
                                 "total_tokens": (u.get("total_tokens") or pt)}
            except Exception:
                ctx_usage = None
            yield {
                "step": "done",
                "image": f"/outputs/{path.name}",
                "prompt": prompt,
                "search": search_info,
                "size": {"width": int(width) if width else None,
                         "height": int(height) if height else None,
                         "via": size_via},
                "vlm": vlm_description,
                "ctx_usage": ctx_usage,   # 本次生成最后一次 LLM 请求的真实 token
            }
        else:
            log.error("[%s] comfyui 未返回图片路径", gid)
            yield {"step": "error", "error": "生图失败，ComfyUI 未返回图片"}
    except Exception as e:
        log.error("[%s] 生成异常: %s", get_request_id(), e, exc_info=True)
        yield {"step": "error", "error": str(e)}
    finally:
        set_request_id("")


# 标签复选系统提示词：要求 LLM 参考草稿、从【角色特征区】尽量多选、
# 从【画面区】挑选画面标签，不得新增区外词。
TAG_SELECT_SYSTEM = (
    "你是一个 danbooru 标签选择器。用户给出画面需求、参考草稿和多个候选区。\n"
    "请从中挑选标签组成最终提示词。\n"
    "要求：\n"
    "1. 只输出选中标签的英文名，逗号+空格分隔，不要序号不要解释\n"
    "2. 有多个【角色N特征区】时：先输出 角色名标签, 再紧跟该角色的"
    "特征标签，然后下一个角色，依此类推——每个角色的特征必须紧跟自己的角色名，"
    "绝不能混在别的角色后面（这决定画面角色特征是否正确不串）\n"
    "3. 每个特征区内尽量多选，冲突项(如不同发色)只选最贴合的一个\n"
    "4. 【画面区】按提示选取数量，覆盖姿态/场景/服饰/氛围，"
    "多角色时补充 2girls/multiple_girls 等人数标签\n"
    "5. 全部标签必须来自候选区，禁止输出区外任何词\n"
    "6. 若草稿来自历史对话延续（历史是泳装、本次加海边），"
    "沿用仍适用的标签并补入新场景标签"
)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _generation_with_task(data: dict, source: str):
    """包一层任务登记：随事件流更新任务注册表，结束自动登记结果。

    三个入口（Web SSE / 同步 API / OpenAI 兼容）统一走这里，
    因此被 API 触发或 OpenAI 触发的生成，也会出现在网页「任务」面板。
    """
    tid = task_begin(source)
    try:
        for ev in _run_generation(data):
            step = ev.get("step")
            if step in ("vlm", "search", "llm", "size", "comfyui"):
                task_update(tid, step=step, msg=ev.get("msg") or "")
            elif step == "done":
                task_finish(tid, True, msg="✅ 生成完成",
                            image=ev.get("image"),
                            prompt=(ev.get("prompt") or "")[:80])
                yield ev
            elif step == "error":
                task_finish(tid, False, msg=f"❌ {ev.get('error', '失败')}")
                yield ev
            else:
                yield ev
    except GeneratorExit:
        # 客户端断开：任务标记中断（不覆盖已 done）
        with _TASKS_LOCK:
            t = _TASKS.get(tid)
            if t and t.get("status") == "running":
                t["status"] = "error"
                t["msg"] = "⏹ 已中断（客户端断开）"
                t["finished"] = time.time()
        raise



# ====================================================================== #
# 生成接口：SSE 流（供前端） + 同步 JSON（供外部程序）
# ====================================================================== #
@app.route("/api/resolve/role", methods=["POST"])
def api_resolve_role():
    """发送前角色预检：仅规则层识别（不调 LLM），支持多角色。

    body: {prompt, role?}
    返回 {success, roles: [{label, candidates, need_choice}],
          need_choice: 任一角色需选择}
    每个角色一组候选；candidates 跨作品>1 则该角色 need_choice=True。
    """
    data = request.get_json() or {}
    text = (data.get("prompt") or "").strip()
    role = (data.get("role") or "").strip()
    if not text and not role:
        return jsonify({"success": False, "error": "缺少文本"}), 400

    def _item(d: dict) -> dict:
        core = (d.get("core_tags") or "")
        return {
            "character": d.get("character", ""),
            "copyright": d.get("copyright", ""),
            "name": d.get("name", ""),
            "copyright_name": d.get("copyright_name", ""),
            "copyright_cn": char_resolver.copyright_to_cn(
                d.get("copyright", "")),   # 中文作品名(弹卡展示)
            "trigger": (d.get("trigger") or "")[:120],
            "core_head": ", ".join(
                [t.strip() for t in core.split(",") if t.strip()][:12]),
        }

    try:
        if role:
            # 手动指定：逗号分隔多个角色
            groups = []
            for seg in re.split(r"[，,]+", role):
                seg = seg.strip()
                if not seg:
                    continue
                cands = char_resolver.resolve_from_text(seg, strict=True)
                if cands:
                    groups.append(cands)
        else:
            groups = char_resolver.resolve_multi(text)

        roles_out = []
        any_choice = False
        for i, cands in enumerate(groups or [], 1):
            if not cands:
                continue
            items = [_item(d) for d in cands[:5]]
            unique_cps = {d.get("copyright") for d in cands[:5]}
            need = len(unique_cps) > 1
            any_choice = any_choice or need
            roles_out.append({
                "label": f"角色{i}",
                "need_choice": need,
                "candidates": items,
            })
        return jsonify({"success": True, "prompt": text,
                        "roles": roles_out,
                        "need_choice": any_choice})
    except Exception as e:
        log.error("[resolve/role] %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/generate", methods=["POST"])
def generate():
    if not _gen_lock.acquire(blocking=False):
        return jsonify({"queue": True}), 200

    def event_stream():
        data = request.get_json() or {}
        try:
            for ev in _generation_with_task(data, "web"):
                yield _sse(ev)
        finally:
            _gen_lock.release()

    return Response(stream_with_context(event_stream()),
                    mimetype="text/event-stream")


@app.route("/api/generate/sync", methods=["POST"])
def generate_sync():
    """同步生图接口：外部程序一次 POST，等待出图后直接返回 JSON。

    请求体与 /api/generate 相同：
        {
          "prompt": "...",            # 必填
          "role": "shiroko",          # 可选，手动指定角色
          "use_search": true,         # 可选，默认 true
          "auto_resolution": true,    # 可选，AI 决定尺寸
          "width": 896, "height": 1152,
          "workflow_path": "...",
          "image": ["data:..."]       # 可选，参考图 base64
        }
    响应 200: {"success": true, "image": "/outputs/x.png",
               "url": "http://host/outputs/x.png", "prompt": "...", "size": {...}}
    响应 429: {"success": false, "error": "...", "queue": true}  # 已有任务在跑
    响应 500: {"success": false, "error": "..."}                 # 生成失败
    """
    if not _gen_lock.acquire(blocking=False):
        return jsonify({"success": False, "queue": True,
                        "error": "已有生图任务进行中，请稍后重试"}), 429
    data = request.get_json() or {}
    result = None
    try:
        for ev in _generation_with_task(data, "sync"):
            if ev.get("step") == "done":
                result = ev
            elif ev.get("step") == "error":
                return jsonify({"success": False, "error": ev.get("error")}), 500
    finally:
        _gen_lock.release()

    if not result or not result.get("image"):
        return jsonify({"success": False, "error": "生图失败，未返回图片"}), 500
    image_url = result["image"]
    return jsonify({
        "success": True,
        "image": image_url,
        "url": request.host_url.rstrip("/") + image_url,
        "prompt": result.get("prompt", ""),
        "search": result.get("search"),
        "size": result.get("size"),
        "vlm": result.get("vlm"),
    })


# ====================================================================== #
# 图库 / 二采（超分）
# ====================================================================== #
_GALLERY_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


@app.route("/api/gallery", methods=["GET"])
def api_gallery():
    """列出工具本地 outputs 目录的历史图片（全历史图库）。

    query: ?offset=&limit=&sub=&q=   sub=搜索子目录;
    返回 {files: [{name, url, mtime}], total, offset, limit, root}
    """
    root = OUTPUTS_DIR
    if not root.is_dir():
        return jsonify({"success": False,
                        "error": "输出目录不存在",
                        "files": [], "total": 0}), 200
    offset = max(0, int(request.args.get("offset", 0) or 0))
    limit = min(200, max(1, int(request.args.get("limit", 60) or 60)))
    sub = (request.args.get("sub") or "").strip()
    q = (request.args.get("q") or "").strip().lower()
    base = root / sub if sub else root
    files = []
    if base.is_dir():
        for p in base.iterdir():
            if p.suffix.lower() in _GALLERY_EXTS:
                nm = p.name
                if q and q not in nm.lower():
                    continue
                rel = (p.relative_to(root)).as_posix() if sub else nm
                files.append({
                    "name": nm,
                    "path": rel,
                    "sub": sub,
                    "mtime": p.stat().st_mtime,
                    "url": f"/api/gallery/img?f={rel}",
                })
    files.sort(key=lambda x: x["mtime"], reverse=True)
    total = len(files)
    return jsonify({
        "success": True,
        "files": files[offset:offset + limit],
        "total": total, "offset": offset, "limit": limit, "root": str(root),
    })


@app.route("/api/gallery/img")
def api_gallery_img():
    """从工具本地 outputs 目录读图（按相对路径，防目录穿越）。

    支持 ?w=<px> 服务端缩略（等比、最长边限宽），用于下拉/网格预览，
    大幅减少加载体积；不带 w 返回原图。
    """
    f = (request.args.get("f") or "").strip()
    root = OUTPUTS_DIR
    if not root.is_dir() or not f:
        return "not found", 404
    try:
        full = (root / f).resolve()
        if not str(full).startswith(str(root.resolve())) or not full.is_file():
            return "not found", 404
    except Exception:
        return "not found", 404
    w = request.args.get("w")
    if w and w.isdigit() and 16 <= int(w) <= 400:
        try:
            from PIL import Image
            with Image.open(full) as im:
                im.load()
                if im.mode in ("RGBA", "P", "LA"):
                    im = im.convert("RGB")
                maxw = int(w)
                if max(im.size) > maxw:
                    im.thumbnail((maxw, maxw), Image.LANCZOS)
                import io as _io
                buf = _io.BytesIO()
                im.save(buf, format="JPEG", quality=80)
                buf.seek(0)
                resp = Response(buf.read(), mimetype="image/jpeg")
                resp.headers["Cache-Control"] = "public, max-age=86400"
                return resp
        except Exception as e:
            log.warning("[gallery/img] 缩略失败 %s: %s", f, e)
            return send_file(full)
    return send_file(full)


@app.route("/api/upscale", methods=["POST"])
def api_upscale():
    """对工具 outputs 目录的一张已生成图做二采（img2img 放大重采样）。

    body: {file: "相对路径/xxx.png", prompt?: "额外提示",
           scale?: float, denoise?: float, seed?: int}
    返回 {success, image: 本地 outputs 下新图 URL}
    """
    data = request.get_json(force=True, silent=True) or {}
    rel = (data.get("file") or "").strip()
    if not rel:
        return jsonify({"success": False, "error": "缺少图片路径"}), 400
    root = OUTPUTS_DIR
    try:
        src = (root / rel).resolve()
        if not str(src).startswith(str(root.resolve())) or not src.is_file():
            return jsonify({"success": False,
                            "error": "图片不在输出目录"}), 400
    except Exception:
        return jsonify({"success": False,
                        "error": "图片路径无效"}), 400

    scale = float(data.get("scale", 2.0) or 2.0)
    denoise = float(data.get("denoise", 0.5) or 0.5)
    seed = data.get("seed")
    extra_prompt = (data.get("prompt") or "").strip()
    scale = min(4.0, max(1.0, scale))
    denoise = min(1.0, max(0.1, denoise))

    # 上传到 ComfyUI input（安全文件名）
    import time as _t
    safe = f"up_{int(_t.time()*1000)}_{src.name}"
    cli = make_comfy(shared=False)   # 独立实例，避免与生成任务抢会话
    try:
        ok = cli.upload_image(src.read_bytes(), safe)
    except Exception as e:
        log.error("[upscale] 读图失败: %s", e)
        return jsonify({"success": False, "error": f"读图失败: {e}"}), 500
    if not ok:
        return jsonify({"success": False,
                        "error": "上传图片到 ComfyUI 失败（检查服务）"}), 500

    try:
        wf = load_workflow("MIAOMIAO 单二采.json")
    except Exception as e:
        log.error("[upscale] 模板加载失败: %s", e)
        return jsonify({"success": False,
                        "error": f"img2img 模板加载失败: {e}"}), 500

    try:
        path = cli.generate_img2img(
            wf, extra_prompt, safe, scale=scale, denoise=denoise,
            seed=int(seed) if seed else None,
            progress_cb=None)
    except Exception as e:
        log.error("[upscale] 二采执行失败: %s", e)
        return jsonify({"success": False,
                        "error": f"二采执行失败: {e}"}), 500
    if not path:
        return jsonify({"success": False,
                        "error": "二采完成但未获取到图片"}), 500
    return jsonify({"success": True,
                    "image": f"/outputs/{path.name}",
                    "scale": scale, "denoise": denoise})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """全局关闭按钮：停止喵梓服务进程。

    页面按钮调用；立即返回响应，后台线程延迟 0.6s 后强制退出进程，
    保证浏览器收到响应后再关。
    """
    log.info("收到关闭服务请求，正在退出…")
    import os as _os

    def _bye():
        import time as _t
        _t.sleep(0.6)
        _os._exit(0)
    threading.Thread(target=_bye, daemon=True).start()
    return jsonify({"success": True, "msg": "服务已关闭"})


@app.route("/api/health", methods=["GET"])
def api_health():
    """健康检查：确认服务与 ComfyUI 可达状态。"""
    import requests as _rq
    comfy_ok = False
    comfy_err = ""
    try:
        r = _rq.get(f"{settings.comfyui_url}/system_stats", timeout=5)
        comfy_ok = r.ok
        if not r.ok:
            comfy_err = f"HTTP {r.status_code}"
    except Exception as e:
        comfy_err = str(e)[:120]
    return jsonify({
        "success": True,
        "status": "ok",
        "comfyui": {"url": settings.comfyui_url,
                    "reachable": comfy_ok,
                    "error": comfy_err or None},
        "llm": {"base_url": settings.llm_base_url,
                "model": settings.llm_model},
        "busy": _gen_lock.locked(),
    })


# ====================================================================== #
# 静态输出
# ====================================================================== #
@app.route("/outputs/<path:filename>")
def serve_output(filename):
    return send_from_directory(str(OUTPUTS_DIR), filename)


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "接口不存在"}), 404


@app.errorhandler(Exception)
def handle_all_errors(e):
    log.error("服务器错误: %s", e, exc_info=True)
    return jsonify({"error": f"服务器错误: {e}"}), 500


# ====================================================================== #
# 日志读取 / 前端错误上报
# ====================================================================== #
@app.route("/api/logs", methods=["GET"])
def api_logs():
    """返回日志文件尾部内容，供页面日志查看器轮询展示。

    参数: ?lines=200 (默认 200, 上限 2000)
    """
    from logger import LOG_FILE
    lines = request.args.get("lines", default=200, type=int)
    lines = max(10, min(lines, 2000))
    if not LOG_FILE.exists():
        return jsonify({"success": True, "log": "", "file": str(LOG_FILE),
                        "lines": 0})
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:]
        # 文件长度可能很大，仅回传尾部，避免前端卡顿
        return jsonify({"success": True, "log": "".join(tail),
                        "file": str(LOG_FILE),
                        "total": len(all_lines),
                        "lines": len(tail)})
    except Exception as e:
        log.error("读取日志失败: %s", e, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/logs/frontend", methods=["POST"])
def api_logs_frontend():
    """接收前端上报的 JS 错误（window.onerror / unhandledrejection）。"""
    data = request.get_json() or {}
    msg = data.get("message") or data.get("reason") or ""
    src = data.get("source") or data.get("stack") or ""
    loc = data.get("location") or ""
    f_log = get_logger("frontend")
    f_log.error("前端错误: %s | %s | %s", msg, loc, src[:1000])
    return jsonify({"success": True})


@app.route("/api/tasks", methods=["GET"])
def api_task_list():
    """任务面板数据：返回全部生成任务（含 API/OpenAI 触发）的实时状态。"""
    return jsonify({"success": True, "tasks": api_tasks()})


@app.route("/api/tasks", methods=["DELETE"])
def api_task_clear():
    """清空任务记录。"""
    with _TASKS_LOCK:
        _TASKS.clear()
    return jsonify({"success": True})


# ====================================================================== #
# OpenAI 兼容接口注册
# ====================================================================== #
try:
    from openai_api import register_openai
    register_openai(app, lambda data: _generation_with_task(data, "openai"),
                    settings, _gen_lock, OUTPUTS_DIR)
    log.info("OpenAI 兼容接口已启用: /v1/models, /v1/images/generations")
except Exception as e:
    log.error("OpenAI 兼容接口注册失败: %s", e, exc_info=True)


if __name__ == "__main__":
    import os as _os
    import threading as _th
    # 使用 werkzeug 服务器：waitress 对 SSE 长响应整体缓冲（进度事件无法
    # 实时到达，表现为"中间内容消失"），werkzeug 逐块实时推送。
    from werkzeug.serving import make_server
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    host = _os.getenv("HOST", "0.0.0.0")
    port = int(_os.getenv("PORT", "5000"))
    # 额外监听端口（逗号分隔），例如 EXTRA_PORTS=5001,5002
    extra_ports = []
    for p in str(_os.getenv("EXTRA_PORTS", "")).split(","):
        p = p.strip()
        if p.isdigit():
            extra_ports.append(int(p))

    log.info("喵梓二号 启动，日志文件: %s", "logs/app.log")
    print("=" * 46)
    print("  喵梓二号 已启动")
    print(f"  主端口:    http://127.0.0.1:{port}")
    for ep in extra_ports:
        print(f"  附加端口:  http://127.0.0.1:{ep}")
    print(f"  局域网:    http://<your-ip>:{port}")
    print("  健康检查:  /api/health")
    print("  同步生图:  POST /api/generate/sync")
    print("  OpenAI兼容: /v1/images/generations (base_url: "
          f"http://127.0.0.1:{port}/v1)")
    print("=" * 46)

    # 多端口：每个端口一个 make_server 线程（threaded=True 并发处理请求）
    servers = [make_server(host, port, app, threaded=True)]
    for ep in extra_ports:
        servers.append(make_server(host, ep, app, threaded=True))
    for srv in servers[1:]:
        _th.Thread(target=srv.serve_forever, daemon=True).start()
    servers[0].serve_forever()
