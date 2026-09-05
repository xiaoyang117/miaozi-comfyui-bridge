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
import threading
import time
from pathlib import Path

import requests as _req
from flask import (Flask, Response, jsonify, render_template, request,
                   send_from_directory, stream_with_context)

from character_lookup.query import is_built, lookup as char_lookup
from character_lookup import resolver as char_resolver
from comfyui.client import ComfyUIClient
from llm.client import (LLMClient, PROMPT_SYSTEM_SPECIFIC,
                        PROMPT_WITH_CONTEXT_SPECIFIC,
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


def make_comfy() -> ComfyUIClient:
    return ComfyUIClient(server_url=settings.comfyui_url,
                         output_dir=OUTPUTS_DIR)


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
        search_info = None
        raw = None
        role_hint = (data.get("role") or "").strip()
        if use_search:
            yield {"step": "search", "msg": "🔎 正在识别角色…"}
            cands: list = []
            via = ""
            role_missed = False
            try:
                # (0) 手动指定角色（最高优先级，完全绕过识别）
                if role_hint:
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
                if not cands:
                    cands = char_resolver.resolve_from_text(user_input)
                    if cands:
                        via = "本地库直查(无LLM)"

                # (2) LLM 兜底层：仅当规则层没命中时才调用
                if not cands:
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

                # (3) 组装结果
                if cands:
                    raw = _fmt_candidates(cands)
                    best = cands[0]
                    search_info = {
                        "query": best.get("character", ""),
                        "via": via,
                        "role_missed": role_missed,
                        "candidates": [c.get("character", "")
                                       for c in cands[:5]],
                        "results": (raw or "")[:800],
                    }
                    log.info("[%s] search hit via=%s -> %s",
                             gid, via, best.get("character"))
                    yield {"step": "search",
                           "msg": f"✓ 角色识别：{best.get('character')} ({via})"}
                    # 仅当用户显式给出中文角色（role框/描述直名）时学习别名，
                    # 避免把 LLM 猜错的结果或整句描述写进别名表。
                    # 学习统一由 _translate_and_lookup 以精确中文名触发。
                else:
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
                if search_info and raw:
                    ctx = f"角色参考资料:\n{raw}\n\n用户需求: {user_input}"
                    prompt = llm._call(PROMPT_WITH_CONTEXT_SPECIFIC,
                                       ctx, history)
                else:
                    sp = PROMPT_SYSTEM_SPECIFIC if use_search \
                        else llm._prompt_system
                    prompt = llm._call(sp, user_input, history)
                if not prompt:
                    raise RuntimeError("LLM 返回了空提示词")
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
            yield {
                "step": "done",
                "image": f"/outputs/{path.name}",
                "prompt": prompt,
                "search": search_info,
                "size": {"width": int(width) if width else None,
                         "height": int(height) if height else None,
                         "via": size_via},
                "vlm": vlm_description,
            }
        else:
            log.error("[%s] comfyui 未返回图片路径", gid)
            yield {"step": "error", "error": "生图失败，ComfyUI 未返回图片"}
    except Exception as e:
        log.error("[%s] 生成异常: %s", get_request_id(), e, exc_info=True)
        yield {"step": "error", "error": str(e)}
    finally:
        set_request_id("")


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
    import waitress
    from waitress.server import create_server
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

    # 多端口：waitress 每个端口一个 serve 线程
    servers = [create_server(app, host=host, port=port, threads=8)]
    for ep in extra_ports:
        servers.append(create_server(app, host=host, port=ep, threads=8))
    import threading as _th
    for srv in servers[1:]:
        _th.Thread(target=srv.run, daemon=True).start()
    servers[0].run()
