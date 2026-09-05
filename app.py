"""喵梓二号 - ComfyUI 生图桥接 Web 应用（优化版）

链路：用户输入/图片 -> (可选 VLM 识别) -> 角色搜索(本地库/浏览器)
     -> LLM 生成提示词 -> 替换工作流占位符+设置尺寸 -> 提交 ComfyUI -> 返回图片

优化点：
- workflow_path 支持相对名(自动定位到 workflows/)与绝对路径
- 对话框可随时选择分辨率预设，提交前自动改写 EmptyLatentImage
- 更清晰的 SSE 事件与错误提示
"""
import json
import os
import threading
from pathlib import Path

import requests as _req
from flask import (Flask, Response, jsonify, render_template, request,
                   send_from_directory, stream_with_context)

from character_lookup.query import is_built, lookup as char_lookup
from character_lookup import resolver as char_resolver
from comfyui.client import ComfyUIClient
from llm.client import (LLMClient, PROMPT_SYSTEM_SPECIFIC,
                        PROMPT_WITH_CONTEXT_SPECIFIC,
                        SEARCH_SYSTEM, SEARCH_SYSTEM_TINY, EXTRACT_CN_SYSTEM,
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
def make_llm(search_url_idx: int = 0) -> LLMClient:
    sources = settings.search_sources
    url = sources[search_url_idx]["url"] if 0 <= search_url_idx < len(sources) \
        else sources[0]["url"]
    return LLMClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        custom_system_prompt=settings.custom_system_prompt,
        tavily_key=settings.tavily_key,
        tavily_max_results=settings.tavily_max_results,
        use_browser_search=settings.use_browser_search,
        search_url=url,
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
        result, _ = llm.generate_prompt("a cat sitting on a windowsill",
                                        use_search=False)
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


@app.route("/api/test/search", methods=["POST"])
def test_search():
    data = request.get_json() or {}
    tavily_key = data.get("tavily_key") or settings.tavily_key
    if not tavily_key:
        return jsonify({"success": False, "error": "未配置 Tavily Key"})
    try:
        from llm.search import tavily_search
        results = tavily_search(tavily_key, "test", max_results=2)
        if results:
            return jsonify({"success": True, "results": results[:500]})
        return jsonify({"success": False,
                        "error": "搜索无结果或 Key 无效"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


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
# 生成主流程（SSE）
# ====================================================================== #
@app.route("/api/generate", methods=["POST"])
def generate():
    if not _gen_lock.acquire(blocking=False):
        return jsonify({"queue": True}), 200

    def event_stream():
        import uuid as _uuid
        set_request_id(_uuid.uuid4().hex[:12])  # 本次生成全程共享此 id
        gid = get_request_id()
        try:
            data = request.get_json() or {}
            user_input = (data.get("prompt") or "").strip()
            log.info("[%s] input: %s", gid, user_input[:100])
            if not user_input:
                yield _sse({"step": "error", "error": "请输入图片描述"})
                return

            # ---------- 解析参数 ----------
            wf_path = data.get("workflow_path") or settings.workflow_path
            use_search = bool(data.get("use_search", True))
            history = data.get("history") or []
            search_url_idx = int(data.get("search_url_idx") or 0)
            auto_resolution = bool(data.get("auto_resolution", False))
            width = data.get("width") or settings.gen_width
            height = data.get("height") or settings.gen_height
            size_via = "manual"
            log.info("[%s] params: wf=%s search=%s auto_size=%s size=%sx%s",
                     gid, Path(wf_path).name, use_search, auto_resolution,
                     width, height)

            # ---------- 加载工作流 ----------
            try:
                wf = load_workflow(wf_path)
                log.info("[%s] workflow loaded: %s 节点数=%s", gid,
                         Path(wf_path).name, len(wf))
            except FileNotFoundError as e:
                log.error("[%s] workflow not found: %s", gid, e)
                yield _sse({"step": "error", "error": str(e)})
                return
            except Exception as e:
                log.error("[%s] workflow parse error: %s", gid, e,
                          exc_info=True)
                yield _sse({"step": "error", "error": f"工作流解析失败: {e}"})
                return

            llm = make_llm(search_url_idx=search_url_idx)
            comfy = make_comfy()

            # ---------- Step 0: VLM ----------
            image_list = data.get("image") or []
            if isinstance(image_list, str):
                image_list = [image_list] if image_list else []
            vlm_description = None
            if image_list:
                yield _sse({"step": "vlm"})
                log.info("[%s] vlm: %d 张图", gid, len(image_list))
                try:
                    vlm_description = vlm_analyze(image_list)
                    log.info("[%s] vlm done: %s 字", gid,
                             len(vlm_description or ""))
                except RuntimeError as e:
                    log.error("[%s] vlm failed: %s", gid, e)
                    yield _sse({"step": "error",
                                "error": f"图片识别失败: {e}"})
                    return
                user_input = (f"用户上传了一张图片，以下是图片识别结果：\n"
                              f"{vlm_description}\n\n用户需求：{user_input}")

            # ---------- Step 1: 角色识别（规则优先，LLM 兜底） ----------
            search_info = None
            raw = None
            role_hint = (data.get("role") or "").strip()
            if use_search:
                yield _sse({"step": "search"})
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
                        # 浏览器搜索辅助（可选，给 LLM 提供线索）
                        if not cands and settings.use_browser_search \
                                and llm.search_url:
                            try:
                                from llm.browser_search import browser_search
                                web_raw = browser_search(user_input, 5,
                                                         llm.search_url)
                                if web_raw:
                                    q2 = llm._call(SEARCH_SYSTEM,
                                                   f"用户需求: {user_input}"
                                                   f"\n网络搜索:\n{web_raw[:800]}",
                                                   history)
                                    cands = char_resolver.resolve_from_text(q2)
                                    via = "浏览器搜索"
                            except Exception as e:
                                log.warning("[web search error] %s", e)

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
                        # 仅当用户显式给出中文角色（role框/描述直名）时学习别名，
                        # 避免把 LLM 猜错的结果或整句描述写进别名表。
                        # 学习统一由 _translate_and_lookup 以精确中文名触发。
                    else:
                        log.warning("[search] 未找到角色: %s", user_input[:60])
                        search_info = {"query": "", "via": via,
                                       "results": "",
                                       "error": "未找到匹配角色，已按无角色参考继续"}
                except Exception as e:
                    log.error("[search error] %s", e)
                    search_info = {"query": "", "results": "",
                                   "error": str(e)}

            # ---------- Step 2: LLM 生成提示词 ----------
            yield _sse({"step": "llm"})
            prompt = ""
            try:
                if search_info and raw:
                    ctx = f"角色参考资料:\n{raw}\n\n用户需求: {user_input}"
                    prompt = llm._call(PROMPT_WITH_CONTEXT_SPECIFIC, ctx, history)
                else:
                    sp = PROMPT_SYSTEM_SPECIFIC if use_search \
                        else llm._prompt_system
                    prompt = llm._call(sp, user_input, history)
                if not prompt:
                    raise RuntimeError("LLM 返回了空提示词")
                log.info("[%s] llm prompt ok (%d 字符)", gid, len(prompt))
            except Exception as e:
                log.error("[%s] llm prompt failed: %s", gid, e, exc_info=True)
                yield _sse({"step": "error",
                            "error": f"提示词生成失败: {e}"})
                return

            # ---------- Step 2.5: 分辨率自动决策 ----------
            if auto_resolution:
                width, height, size_via = decide_resolution(user_input, llm,
                                                            history)
                log.info("[%s] size auto -> %sx%s (via %s)",
                         gid, width, height, size_via)

            # ---------- Step 3: ComfyUI ----------
            yield _sse({"step": "comfyui"})
            log.info("[%s] comfyui submit, size=%sx%s", gid, width, height)
            try:
                path = comfy.generate(
                    wf, prompt,
                    placeholder=settings.prompt_placeholder,
                    save_node_id=settings.save_node_id,
                    width=int(width) if width else None,
                    height=int(height) if height else None,
                    width_placeholder=settings.width_placeholder,
                    height_placeholder=settings.height_placeholder,
                )
                log.info("[%s] comfyui done -> %s", gid, path.name if path else None)
            except Exception as e:
                log.error("[%s] comfyui failed: %s", gid, e, exc_info=True)
                yield _sse({"step": "error",
                            "error": f"ComfyUI 生图失败: {e}"})
                return

            if path and path.exists():
                log.info("[%s] done, 图片已保存", gid)
                yield _sse({
                    "step": "done",
                    "image": f"/outputs/{path.name}",
                    "prompt": prompt,
                    "search": search_info,
                    "size": {"width": int(width) if width else None,
                             "height": int(height) if height else None,
                             "via": size_via},
                    "vlm": vlm_description,
                })
            else:
                log.error("[%s] comfyui 未返回图片路径", gid)
                yield _sse({"step": "error",
                            "error": "生图失败，ComfyUI 未返回图片"})
        except Exception as e:
            log.error("[%s] 生成异常: %s", get_request_id(), e, exc_info=True)
            yield _sse({"step": "error", "error": str(e)})
        finally:
            set_request_id("")
            _gen_lock.release()

    return Response(stream_with_context(event_stream()),
                    mimetype="text/event-stream")


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


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


if __name__ == "__main__":
    import os as _os
    import waitress
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    host = _os.getenv("HOST", "0.0.0.0")
    port = int(_os.getenv("PORT", "5000"))
    log.info("喵梓二号 启动，日志文件: %s", "logs/app.log")
    print("=" * 46)
    print("  喵梓二号 已启动")
    print(f"  本机访问:  http://127.0.0.1:{port}")
    print(f"  局域网:    http://<your-ip>:{port}")
    print("=" * 46)
    waitress.serve(app, host=host, port=port, threads=8)
