"""喵梓 · 本机版服务端

单进程跑完所有事：网站界面、会话历史、提示词调度、MCP 驱动 ComfyUI、
代理本机模型。没有独立桥接进程，也没有跨机通信。

启动：python app.py
"""

import base64
import glob as globmod
import hashlib
import ipaddress
import json
import os
import re
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from flask import (Flask, Response, abort, g, jsonify, render_template, request,
                   send_from_directory)
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.exceptions import HTTPException

from character_lookup import (direct_candidates, find_candidates,
                              format_character, get_character,
                              is_built as char_db_built, lookup as char_lookup)
from engine import engine
from llm import LLMError
from llm.client import LLMClient, parse_generated_prompt
from llm.prompts import build_history_text
from plugins import PluginError, plugins
from settings import settings
from store import store
from vlm import VLMError, describe_images

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 工作流的兜底目录：没配 workflow_path 时用项目自带的 workflows/
DEFAULT_WORKFLOW_DIR = BASE_DIR.parent / "workflows"

app = Flask(__name__, static_folder=str(BASE_DIR / "static"))
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["JSON_AS_ASCII"] = False

# 同一时间只跑一个生成任务，避免把本地显卡打满
_gen_lock = threading.Lock()
MAX_CHARACTERS = 4

# 不需要口令校验的路径
_OPEN_PATHS = ("/static/", "/favicon.ico")

ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


# ---------------------------------------------------------------- 口令

@app.before_request
def _guard():
    if request.method == "OPTIONS":
        return None
    password = settings.access_password
    if not password:
        return None
    path = request.path
    if path in ("/", "/api/auth") or any(path.startswith(p) for p in _OPEN_PATHS):
        return None
    given = (request.headers.get("X-Access-Password")
             or request.args.get("pw") or "")
    if given == password:
        return None
    try:
        valid = URLSafeTimedSerializer(password, salt="miaozi-access").loads(
            request.cookies.get("mz_access", ""), max_age=7 * 24 * 3600) is True
    except BadSignature:
        valid = False
    if not valid:
        return jsonify({"error": "口令错误", "need_auth": True}), 401
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("Origin")
        if ((origin and urlsplit(origin).netloc != request.host)
                or request.headers.get("Sec-Fetch-Site") == "cross-site"):
            return jsonify({"error": "不允许跨站修改"}), 403
    return None


@app.after_request
def _access_cookie(response):
    if settings.access_password and g.get("refresh_access_cookie", False):
        token = URLSafeTimedSerializer(
            settings.access_password, salt="miaozi-access").dumps(True)
        response.set_cookie("mz_access", token, max_age=7 * 24 * 3600,
                            httponly=True, samesite="Strict", secure=request.is_secure)
    if request.path.startswith(("/api/", "/outputs/")):
        response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------- 工具

def _visitor_ip() -> str:
    try:
        address = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        abort(400, description="无法识别访问 IP")
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return "127.0.0.1" if address.is_loopback else address.compressed


def _require_session(session_id: str, owner_ip: str) -> None:
    if not store.session_exists(session_id, owner_ip):
        abort(404, description="会话不存在")


def _make_llm(**overrides) -> LLMClient:
    cfg = {
        "base_url": settings.get("llm_base_url"),
        "api_key": settings.get("llm_api_key"),
        "model": settings.get("llm_model"),
        "timeout": settings.llm_timeout,
    }
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    return LLMClient(cfg)


def _llm_config_for_test(data: dict) -> LLMClient:
    return LLMClient({
        "base_url": data.get("llm_base_url", settings.get("llm_base_url")),
        "api_key": data.get("llm_api_key", settings.get("llm_api_key")),
        "model": data.get("llm_model", settings.get("llm_model")),
        "timeout": min(settings.llm_timeout, 120),
    })


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _public_settings(owner_ip: str = "127.0.0.1") -> dict:
    """给前端看的配置，剥掉口令这类不该下发到浏览器的东西。

    访问口令只回一个布尔值 auth_required，前端据此决定是否弹口令框。
    """
    data = settings.get_all()
    data.pop("access_password", None)
    controls = data.get("workflow_controls") or {}
    data["workflow_controls"] = {
        **(controls if isinstance(controls, dict) else {}),
        **store.get_preferences(owner_ip)["workflow_controls"],
    }
    data["auth_required"] = bool(settings.access_password)
    return data


def _output_prefix(owner_ip: str) -> str:
    return hashlib.sha256(owner_ip.encode("utf-8")).hexdigest()[:32] + "_"


def _owns_output(filename: str, owner_ip: str) -> bool:
    if "/" in filename or "\\" in filename:
        return False
    if re.match(r"^[0-9a-f]{32}_", filename):
        return filename.startswith(_output_prefix(owner_ip))
    return owner_ip == "127.0.0.1"


def _save_data_url(data_url: str, prefix: str = "",
                   owner_ip: str = "127.0.0.1") -> str:
    """把引擎回传的图片存到 outputs/，返回对外路径。"""
    if not data_url:
        return ""
    if data_url.startswith("http://") or data_url.startswith("https://"):
        return data_url
    try:
        header, b64 = data_url.split(",", 1)
    except ValueError:
        return ""
    ext = ".png"
    if "image/" in header:
        sub = header.split("image/", 1)[1].split(";")[0].strip().lower()
        ext = "." + (sub if sub in ("jpeg", "jpg", "webp", "gif", "bmp")
                     else "png")
    name = f"{_output_prefix(owner_ip)}{prefix}{uuid.uuid4().hex[:20]}{ext}"
    try:
        (OUTPUT_DIR / name).write_bytes(base64.b64decode(b64))
    except Exception:
        return ""
    _prune_outputs(owner_ip)
    return f"/outputs/{name}"


_prune_lock = threading.Lock()


def _prune_outputs(owner_ip: str = "127.0.0.1") -> None:
    """每个 IP 只保留最近 keep_outputs 张图。"""
    keep = settings.get("keep_outputs")
    try:
        keep = int(keep)
    except (TypeError, ValueError):
        keep = 0
    if keep <= 0:
        return
    if not _prune_lock.acquire(blocking=False):
        return
    try:
        files = [p for p in OUTPUT_DIR.iterdir()
                 if p.is_file() and p.suffix.lower() in ALLOWED_IMAGE_EXT
                 and _owns_output(p.name, owner_ip)]
        if len(files) <= keep:
            return
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[keep:]:
            try:
                p.unlink()
            except OSError:
                pass
    finally:
        _prune_lock.release()


def _resolve_workflow_path(requested: str) -> str:
    path = (requested or settings.get("workflow_path") or "").strip()
    if path:
        return path
    # 没配工作流路径：用项目 workflows/ 里第一个 json
    if DEFAULT_WORKFLOW_DIR.is_dir():
        for f in sorted(globmod.glob(str(DEFAULT_WORKFLOW_DIR / "*.json"))):
            return os.path.abspath(f)
    return path


# ---------------------------------------------------------------- 页面

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/outputs/<path:filename>")
def serve_output(filename):
    if not _owns_output(filename, _visitor_ip()):
        abort(404)
    return send_from_directory(OUTPUT_DIR, filename)


@app.route("/api/auth", methods=["POST"])
def api_auth():
    data = request.get_json(silent=True) or {}
    password = settings.access_password
    if not password:
        return jsonify({"success": True, "need_auth": False})
    ok = data.get("password") == password
    if ok:
        g.refresh_access_cookie = True
    return jsonify({"success": ok, "need_auth": True}), (200 if ok else 401)


# ---------------------------------------------------------------- 启动数据

@app.route("/api/bootstrap")
def api_bootstrap():
    """一次性把前端需要的所有状态取回去，减少来回请求。"""
    owner_ip = _visitor_ip()
    return jsonify({
        "success": True,
        "settings": _public_settings(owner_ip),
        "engine": engine.status(),
        "character_db": char_db_built(),
        "client_ip": owner_ip,
        "preferences": store.get_preferences(owner_ip),
        "sessions": store.list_sessions(limit=30, owner_ip=owner_ip),
    })


# ---------------------------------------------------------------- 配置

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify({"success": True, "settings": _public_settings(_visitor_ip())})
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "无效的配置数据"}), 400
    settings.update(data)
    if data.get("access_password"):
        g.refresh_access_cookie = True
    return jsonify({"success": True, "settings": _public_settings(_visitor_ip())})


@app.route("/api/preferences", methods=["GET", "POST"])
def api_preferences():
    owner_ip = _visitor_ip()
    if request.method == "GET":
        return jsonify({"success": True, "preferences": store.get_preferences(owner_ip)})
    try:
        preferences = store.update_preferences(owner_ip, request.get_json(silent=True))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"success": True, "preferences": preferences,
                    "settings": _public_settings(owner_ip)})


@app.route("/api/plugins")
def api_plugins():
    return jsonify({"success": True, "plugins": plugins.list_plugins()})


@app.route("/api/plugins/<plugin_id>", methods=["PUT"])
def api_plugin_configure(plugin_id):
    try:
        plugins.configure(plugin_id, request.get_json(silent=True))
        return jsonify({"success": True, "plugins": plugins.list_plugins()})
    except PluginError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"无法启用插件：{exc}"}), 400


@app.route("/api/plugins/reload", methods=["POST"])
def api_plugins_reload():
    return jsonify({"success": True, "plugins": plugins.reload()})


@app.route("/api/plugins/<plugin_id>/call/<action>", methods=["POST"])
def api_plugin_call(plugin_id, action):
    try:
        result = plugins.call_api(plugin_id, action, request.get_json(silent=True))
        return jsonify(result)
    except PluginError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/plugins/<plugin_id>/assets/<name>")
def api_plugin_asset(plugin_id, name):
    try:
        return send_from_directory(plugins.directory / plugin_id / "assets",
                                   plugins.asset(plugin_id, name).name)
    except PluginError as exc:
        return jsonify({"error": str(exc)}), 404


# ---------------------------------------------------------------- 引擎

@app.route("/api/engine/status")
def api_engine_status():
    return jsonify({"success": True, "engine": engine.status()})


@app.route("/api/engine/restart", methods=["POST"])
def api_engine_restart():
    """改完 MCP 相关配置后重建连接。"""
    return jsonify({"success": True, "engine": engine.restart()})


# ---------------------------------------------------------------- 会话历史

@app.route("/api/sessions", methods=["GET", "POST"])
def api_sessions():
    owner_ip = _visitor_ip()
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        return jsonify({"success": True,
                        "session": store.create_session(data.get("title", ""), owner_ip)})
    return jsonify({"success": True,
                    "sessions": store.list_sessions(limit=50, owner_ip=owner_ip)})


@app.route("/api/sessions/<session_id>", methods=["GET", "DELETE", "PATCH"])
def api_session(session_id):
    owner_ip = _visitor_ip()
    _require_session(session_id, owner_ip)
    if request.method == "DELETE":
        store.delete_session(session_id, owner_ip)
        return jsonify({"success": True})
    if request.method == "PATCH":
        data = request.get_json(silent=True) or {}
        store.rename_session(session_id, data.get("title", "新对话"), owner_ip)
        return jsonify({"success": True})
    return jsonify({"success": True,
                    "messages": store.list_messages(session_id, owner_ip=owner_ip)})


@app.route("/api/sessions/<session_id>/clear", methods=["POST"])
def api_session_clear(session_id):
    owner_ip = _visitor_ip()
    _require_session(session_id, owner_ip)
    store.clear_messages(session_id, owner_ip)
    return jsonify({"success": True})


@app.route("/api/messages/<int:message_id>", methods=["DELETE"])
def api_message_delete(message_id):
    if not store.delete_message(message_id, _visitor_ip()):
        abort(404, description="消息不存在")
    return jsonify({"success": True})


# ---------------------------------------------------------------- 测试

@app.route("/api/test/llm", methods=["POST"])
def test_llm():
    data = request.get_json(silent=True) or {}
    try:
        client = _llm_config_for_test(data)
        out = client.call(
            "你是一个测试助手。只回复：连接正常",
            "ping")
        return jsonify({"success": True, "output": out[:500]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/test/vlm", methods=["POST"])
def test_vlm():
    data = request.get_json(silent=True) or {}
    cfg = {
        "base_url": data.get("vlm_base_url") or settings.get("vlm_base_url"),
        "api_key": data.get("vlm_api_key") or settings.get("vlm_api_key"),
        "model": data.get("vlm_model") or settings.get("vlm_model"),
        "timeout": min(settings.vlm_timeout, 60),
    }
    url = str(cfg["base_url"] or "").rstrip("/")
    if not url or not cfg["model"]:
        return jsonify({"success": False, "error": "未配置 VLM 地址或模型名"})
    import requests
    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    try:
        r = requests.post(
            f"{url}/chat/completions", headers=headers, timeout=cfg["timeout"],
            json={"model": cfg["model"],
                  "messages": [{"role": "user", "content": "只回复：ok"}],
                  "temperature": 0.1})
        return jsonify({"success": r.ok, "status": r.status_code,
                        "body": r.text[:600]})
    except Exception as e:
        return jsonify({"success": False, "status": 0, "body": str(e)})


@app.route("/api/test/characters", methods=["POST"])
def test_characters():
    data = request.get_json(silent=True) or {}
    if not char_db_built():
        return jsonify({"success": False,
                        "error": "本地角色库未建立，先运行 "
                                 "python character_lookup/build_db.py"})
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"success": False, "error": "请输入要查询的角色名"})
    result = char_lookup(query)
    if result:
        return jsonify({"success": True, "result": result[:800]})
    candidates = find_candidates(query)
    if candidates:
        names = " / ".join(f"{c['name']} ({c['copyright_name']})"
                           for c in candidates)
        return jsonify({"success": False, "error": f"找到多个角色，请加上作品名：{names}"})
    return jsonify({"success": False,
                    "error": f"未找到「{query}」，角色名请用英文或罗马音"})


def _references_previous_character(prompt: str) -> bool:
    return bool(
        re.search(r"(?<!其)[她他](?:们)?|这个角色|该角色|这名角色|上述角色|前面的角色",
                  prompt)
        or re.search(
            r"\b(?:she|her|hers|he|him|his|they|them|their|both|"
            r"that character|the character)\b", prompt, re.IGNORECASE)
    )


def _references_multiple_previous_characters(prompt: str) -> bool:
    return bool(
        re.search(r"[她他]们|两人|两位|两名角色|她和他|他和她|两个人", prompt)
        or re.search(r"\b(?:they|them|their|both)\b", prompt, re.IGNORECASE)
    )


def _previous_characters(prompt: str, session_id: str,
                         use_history: bool, owner_ip: str = "127.0.0.1") -> list[dict]:
    if not use_history or not session_id or not _references_previous_character(prompt):
        return []
    ids = store.last_assistant_character_ids(session_id, owner_ip)
    if not ids:
        single = (store.last_assistant_character_id(session_id)
                  if owner_ip == "127.0.0.1"
                  else store.last_assistant_character_id(session_id, owner_ip))
        ids = [single] if single else []
    if len(ids) > 1 and not _references_multiple_previous_characters(prompt):
        return []
    characters = []
    for character_id in dict.fromkeys(ids[:MAX_CHARACTERS]):
        character = get_character(character_id)
        if character:
            characters.append(character)
    return characters


def _character_group(query: str, candidates: list,
                     source: str = "database") -> dict:
    public = [{"character": c["character"], "name": c["name"],
               "copyright": c["copyright_name"]} for c in candidates]
    if not public:
        status, message = "not_found", f"未找到：{query}"
    elif len(public) > 1:
        status, message = "ambiguous", f"{query} 有多个同名角色，请选择作品"
    else:
        status = "matched"
        label = "沿用上一轮角色" if source == "history" else "已匹配"
        message = f"{label}：{public[0]['name']}（{public[0]['copyright']}）"
    return {"query": query, "status": status, "source": source,
            "message": message, "candidates": public}


def _resolve_character(prompt: str, session_id: str = "",
                       use_history: bool = True, owner_ip: str = "127.0.0.1") -> dict:
    direct = direct_candidates(prompt, limit=20)
    if direct:
        queries = [(prompt, direct)]
    else:
        tags = _make_llm().extract_tags(prompt)
        queries = []
        seen = set()
        for line in tags.splitlines():
            query = re.sub(r"^\s*(?:\d+[.)]|[-•])\s*", "", line).strip()
            if not query or query.lower() in ("无", "none", "no character"):
                continue
            query = re.sub(r"\s*[|｜]\s*", ", ", query, count=1)
            key = query.lower()
            if key not in seen:
                candidates = find_candidates(query, limit=20)
                if not candidates and query.count(",") == 1:
                    first, second = (part.strip() for part in query.split(",", 1))
                    first_matches = find_candidates(first, limit=20)
                    second_matches = find_candidates(second, limit=20)
                    if (first_matches and second_matches
                            and len(queries) <= MAX_CHARACTERS - 2):
                        queries.extend([(first, first_matches), (second, second_matches)])
                        seen.update((first.lower(), second.lower()))
                        continue
                queries.append((query, candidates))
                seen.add(key)
            if len(queries) >= MAX_CHARACTERS:
                break
    groups = [_character_group(query, candidates) for query, candidates in queries]
    if not groups or re.search(r"和|与|跟|一起|旁边|身旁|alongside|together with",
                               prompt, re.IGNORECASE):
        existing = {c["character"] for group in groups for c in group["candidates"]}
        for previous in _previous_characters(prompt, session_id, use_history, owner_ip):
            if previous["character"] not in existing and len(groups) < MAX_CHARACTERS:
                groups.append(_character_group(previous["name"], [previous], "history"))
                existing.add(previous["character"])
    if not groups:
        if (use_history and session_id and _references_previous_character(prompt)
                and not _references_multiple_previous_characters(prompt)
                and len(store.last_assistant_character_ids(session_id, owner_ip)) > 1):
            return {"status": "ambiguous_reference",
                    "message": "上一轮有多位角色，请写明要修改哪位角色",
                    "groups": [], "candidates": []}
        return {"status": "not_found", "message": "未识别到明确的角色名",
                "groups": [], "candidates": []}
    status = ("ambiguous" if any(g["status"] == "ambiguous" for g in groups)
              else "matched" if any(g["status"] == "matched" for g in groups)
              else "not_found")
    return {"status": status,
            "message": "；".join(g["message"] for g in groups),
            "groups": groups,
            "source": ("history" if all(g["source"] == "history" for g in groups)
                       else "database"),
            "candidates": groups[0]["candidates"] if len(groups) == 1 else []}


@app.route("/api/characters/resolve", methods=["POST"])
def api_resolve_character():
    data = request.get_json(silent=True) or {}
    owner_ip = _visitor_ip()
    session_id = str(data.get("session_id") or "")
    if session_id:
        _require_session(session_id, owner_ip)
    if not settings.get("use_character_db", True) or not data.get("use_search", True):
        return jsonify({"status": "disabled", "message": "角色库检索已关闭", "candidates": []})
    if not char_db_built():
        return jsonify({"status": "unavailable", "message": "角色库未建立", "candidates": []})
    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "请输入图片描述"}), 400
    try:
        return jsonify(_resolve_character(
            prompt, session_id, bool(data.get("use_history", True)), owner_ip))
    except Exception as e:
        return jsonify({"error": f"角色检索失败：{e}"}), 500


@app.route("/api/test/comfyui", methods=["POST"])
def test_comfyui():
    try:
        result = engine.probe()
        if result.get("ok"):
            return jsonify({"success": True, "body": result.get("body", ""),
                            "source": result.get("source", "")})
        return jsonify({"success": False, "error": result.get("error", "未知错误")})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/workflows")
def api_workflows():
    """列出可用的本机工作流文件。"""
    requested = request.args.get("path", "")
    try:
        result = engine.list_workflows(requested)
        return jsonify({"success": True, "workflows": result.get("workflows") or [],
                        "dir": result.get("dir", "")})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 200


@app.route("/api/workflows/inspect")
def api_workflow_inspect():
    try:
        path = _resolve_workflow_path(request.args.get("path"))
        if not path:
            return jsonify({"error": "请先选择工作流"}), 400
        return jsonify({"success": True, **engine.inspect_workflow(path)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/loras")
def api_loras():
    try:
        return jsonify({"success": True, "models": engine.list_loras()})
    except Exception as e:
        return jsonify({"success": False, "models": [], "error": str(e)})


# ---------------------------------------------------------------- 生成主流程

@app.route("/api/generate", methods=["POST"])
def api_generate():
    owner_ip = _visitor_ip()
    if not _gen_lock.acquire(blocking=False):
        return jsonify({"queue": True}), 200

    payload = request.get_json(silent=True) or {}

    def stream():
        try:
            yield from _run_generation(payload, owner_ip)
        except Exception as e:  # noqa: BLE001  任何异常都要能吐回前端
            yield _sse({"step": "error", "error": str(e)})
        finally:
            _gen_lock.release()

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


def _run_generation(payload: dict, owner_ip: str = "127.0.0.1"):
    if "regenerate_message_id" in payload:
        yield from _run_regeneration(payload, owner_ip)
        return

    user_input = (payload.get("prompt") or "").strip()
    if not user_input:
        yield _sse({"step": "error", "error": "请输入图片描述"})
        return
    nsfw_enabled = _nsfw_mode(payload, owner_ip)

    session_id = store.ensure_session(payload.get("session_id") or "", owner_ip)
    use_character = bool(payload.get("use_search", True)) and settings.get("use_character_db", True)
    specific = bool(payload.get("specific", False))
    use_history = bool(payload.get("use_history", True))
    workflow_path = _resolve_workflow_path(payload.get("workflow_path"))
    if not workflow_path:
        yield _sse({"step": "error",
                    "error": "没有找到工作流文件。请在配置页设置工作流路径"})
        return
    try:
        controls = _workflow_controls(workflow_path, payload, owner_ip)
    except ValueError as e:
        yield _sse({"step": "error", "error": str(e)})
        return
    width, height = settings.resolve_size(payload.get("preset_index"))
    images = payload.get("images") or []
    if isinstance(images, str):
        images = [images] if images else []

    # ---------- 0. 图片识别 ----------
    vlm_text = ""
    if images:
        yield _sse({"step": "vlm"})
        try:
            vlm_text = describe_images(images, {
                "base_url": settings.get("vlm_base_url"),
                "api_key": settings.get("vlm_api_key"),
                "model": settings.get("vlm_model"),
                "timeout": settings.vlm_timeout,
            })
        except VLMError as e:
            yield _sse({"step": "error", "error": f"图片识别失败：{e}"})
            return
        user_input_full = (f"用户上传了参考图片，以下是图片识别结果：\n"
                           f"{vlm_text}\n\n用户需求：{user_input}")
    else:
        user_input_full = user_input

    client = _make_llm()

    # ---------- 1. 角色库检索 ----------
    character_info = ""
    characters = []
    inherited_ids = set()
    character_status = "角色库检索已关闭"
    if use_character and not char_db_built():
        character_status = "角色库未建立，已跳过检索"
    elif use_character:
        yield _sse({"step": "search"})
        try:
            if payload.get("character_checked") is True:
                selected = payload.get("character_ids")
                if not isinstance(selected, list):
                    selected = [payload.get("character_id")]
                seen = set()
                for character_id in selected[:MAX_CHARACTERS]:
                    if not isinstance(character_id, str) or character_id in seen:
                        continue
                    seen.add(character_id)
                    character = get_character(character_id)
                    if character:
                        characters.append(character)
                if characters:
                    prefix = ("沿用上一轮角色"
                              if payload.get("character_reason") == "history"
                              else "已匹配")
                    names = "、".join(
                        f"{c['name']}（{c['copyright_name']}）" for c in characters)
                    character_status = f"{prefix}：{names}"
                    missing = payload.get("character_missing")
                    if isinstance(missing, int) and 0 < missing <= MAX_CHARACTERS:
                        character_status += f"；另有 {missing} 位角色未命中"
                    inherited = payload.get("inherited_character_ids")
                    if use_history and isinstance(inherited, list):
                        previous_ids = set(store.last_assistant_character_ids(session_id, owner_ip))
                        inherited_ids = {
                            value for value in inherited
                            if isinstance(value, str) and value in previous_ids
                        }
                else:
                    character_status = {
                        "not_found": "未识别到角色或角色库中没有匹配项",
                        "ambiguous_reference": "上一轮有多位角色，请写明要修改哪位角色",
                        "ambiguous": "多个候选角色，未选择",
                        "cancelled": "已跳过角色选择",
                        "error": "角色检索失败，已跳过",
                    }.get(payload.get("character_reason"), "未找到可用的角色特征")
            else:
                resolved = _resolve_character(user_input, session_id, use_history, owner_ip)
                character_status = resolved["message"]
                seen = set()
                for group in resolved["groups"]:
                    if group["status"] == "matched":
                        character = get_character(group["candidates"][0]["character"])
                        if character and character["character"] not in seen:
                            characters.append(character)
                            seen.add(character["character"])
                            if group["source"] == "history":
                                inherited_ids.add(character["character"])
            if len(characters) == 1:
                character_info = format_character(characters[0])
            elif characters:
                character_info = "\n\n".join(
                    f"角色 {index}（{c['name']}，{c['copyright_name']}"
                    f"{'，上一轮沿用角色' if c['character'] in inherited_ids else ''}）：\n"
                    f"{format_character(c, max_tags=40)}"
                    for index, c in enumerate(characters, start=1))
        except Exception as e:
            characters = []
            character_info = ""
            character_status = f"角色检索失败：{e}"
    yield _sse({"step": "character", "message": character_status})

    # ---------- 2. 生成提示词 ----------
    yield _sse({"step": "llm"})
    history_text = ""
    if use_history and settings.history_rounds > 0:
        history_text = build_history_text(
            store.context_pairs(session_id, settings.history_rounds, owner_ip,
                                include_nsfw=nsfw_enabled))
    prompt_data = plugins.run("before_prompt", {
        "user_input": user_input_full,
        "history_text": history_text,
        "character_info": character_info,
        "specific": specific,
        "custom_system": settings.get("custom_system_prompt") or "",
        "workflow_path": workflow_path,
    })
    try:
        generated_prompt = client.generate_prompt(
            prompt_data["user_input"],
            history_text=prompt_data["history_text"],
            context=prompt_data["character_info"],
            specific=prompt_data["specific"],
            custom_system=prompt_data["custom_system"],
            include_description=True,
            nsfw_enabled=nsfw_enabled,
        )
        if isinstance(generated_prompt, str):
            generated_prompt = parse_generated_prompt(generated_prompt)
    except LLMError as e:
        yield _sse({"step": "error", "error": f"提示词生成失败：{e}"})
        return
    prompt = generated_prompt.prompt
    description = generated_prompt.description
    if not prompt:
        yield _sse({"step": "error", "error": "模型没有返回提示词，请重试"})
        return

    positive_prefix = (str(settings.get("positive_prompt_prefix") or "")
                       .strip().rstrip(",").rstrip())
    if positive_prefix:
        prompt = f"{positive_prefix}, {prompt}"

    output = plugins.run("after_prompt", {
        "prompt": prompt, "description": description,
        "workflow_path": workflow_path,
    })
    prompt, description = output["prompt"], output["description"]
    resize = payload.get("preset_index") != -1
    generated = yield from _generate_image(prompt, workflow_path, width, height,
                                           controls=controls, resize=resize,
                                           description=description, owner_ip=owner_ip)
    if generated is None:
        return
    result, image_url, description = generated
    width, height = result.get("base_size") or (width, height)

    # ---------- 4. 落库 ----------
    store.add_message(session_id, "user", content=user_input,
                      vlm=vlm_text, owner_ip=owner_ip,
                      meta={"has_image": bool(images),
                            "image_count": len(images),
                            "nsfw_enabled": nsfw_enabled})
    store.add_message(session_id, "assistant", content=prompt,
                      image=image_url, character=character_info, owner_ip=owner_ip,
                      meta={"width": width, "height": height,
                            "nsfw_enabled": nsfw_enabled,
                            "description": description,
                            "workflow_path": workflow_path,
                            "workflow_controls": controls,
                            "resize": resize,
                            "elapsed": result.get("elapsed"),
                            "character_status": character_status,
                            "character_id": (characters[0]["character"]
                                             if characters else ""),
                            "character_ids": [c["character"] for c in characters]})

    yield _sse({"step": "done", "image": image_url, "prompt": prompt,
                "description": description,
                "nsfw_enabled": nsfw_enabled,
                "character": character_info, "vlm": vlm_text,
                "session_id": session_id})


def _run_regeneration(payload: dict, owner_ip: str = "127.0.0.1"):
    session_id = payload.get("session_id")
    message_id = payload.get("regenerate_message_id")
    if (not isinstance(session_id, str) or not session_id
            or type(message_id) is not int):
        yield _sse({"step": "error", "error": "请选择要再次生成的图片"})
        return
    source = store.get_message(session_id, message_id, owner_ip)
    if (source is None or source["role"] != "assistant"
            or not source["image"] or not source["content"].strip()):
        yield _sse({"step": "error", "error": "原生成记录不存在或没有可用提示词"})
        return

    prompt = source["content"]
    meta = source["meta"]
    nsfw_enabled = _nsfw_mode(payload, owner_ip)
    if meta.get("nsfw_enabled") is True and not nsfw_enabled:
        yield _sse({"step": "error", "error": "原图使用 NSFW 模式，请先开启 NSFW 再次生成"})
        return
    description = meta.get("description", "")
    if not isinstance(description, str):
        description = ""
    workflow_path = _resolve_workflow_path(
        meta.get("workflow_path") or payload.get("workflow_path"))
    if not workflow_path:
        yield _sse({"step": "error",
                    "error": "没有找到工作流文件。请在配置页设置工作流路径"})
        return
    default_width, default_height = settings.resolve_size(payload.get("preset_index"))
    width = meta.get("width") or default_width
    height = meta.get("height") or default_height
    try:
        controls = (meta.get("workflow_controls") if isinstance(meta.get("workflow_controls"), dict)
                    else _workflow_controls(workflow_path, payload, owner_ip))
    except ValueError as e:
        yield _sse({"step": "error", "error": str(e)})
        return
    resize = meta.get("resize", True)
    generated = yield from _generate_image(
        prompt, workflow_path, width, height, randomize_seed=True,
        controls=controls, resize=resize, description=description, owner_ip=owner_ip)
    if generated is None:
        return
    result, image_url, description = generated
    width, height = result.get("base_size") or (width, height)
    store.add_message(session_id, "assistant", content=prompt,
                      image=image_url, character=source["character"], owner_ip=owner_ip,
                      vlm=source["vlm"],
                      meta={**meta, "width": width, "height": height,
                            "description": description,
                            "nsfw_enabled": meta.get("nsfw_enabled") is True,
                            "workflow_path": workflow_path,
                            "workflow_controls": controls, "resize": resize,
                            "elapsed": result.get("elapsed"),
                            "regenerated_from": source["id"],
                            "character_status": "再次生成：复用提示词，未调用 LLM"})
    yield _sse({"step": "done", "image": image_url, "prompt": prompt,
                "description": description,
                "nsfw_enabled": meta.get("nsfw_enabled") is True,
                "session_id": session_id})


def _nsfw_mode(payload: dict, owner_ip: str) -> bool:
    if "nsfw_enabled" in payload:
        if type(payload["nsfw_enabled"]) is not bool:
            raise ValueError("NSFW 开关必须为布尔值")
        return payload["nsfw_enabled"]
    return store.get_preferences(owner_ip)["nsfw_enabled"]


def _workflow_controls(workflow_path: str, payload: dict,
                       owner_ip: str = "127.0.0.1") -> dict:
    saved = settings.get("workflow_controls") or {}
    if not isinstance(saved, dict):
        saved = {}
    saved = {**saved, **store.get_preferences(owner_ip)["workflow_controls"]}
    controls = payload.get("workflow_controls", saved.get(workflow_path) or {})
    if not isinstance(controls, dict):
        raise ValueError("工作流参数格式不正确")
    return controls


def _generate_image(prompt: str, workflow_path: str, width: int, height: int,
                    randomize_seed: bool = False, controls: dict = None,
                    resize: bool = True, description: str = "",
                    owner_ip: str = "127.0.0.1"):
    yield _sse({"step": "comfyui"})
    try:
        replacements = settings.build_replacements(prompt, width, height)
        if not resize:
            placeholder = str(settings.get("prompt_placeholder") or "").strip()
            replacements = {placeholder: prompt} if placeholder else {}
        result = engine.generate({
            "workflow_path": workflow_path,
            "prompt": prompt,
            "replacements": replacements,
            "width": width,
            "height": height,
            "timeout": settings.comfy_timeout,
            "randomize_seed": randomize_seed,
            "resize": resize,
            "workflow_controls": controls or {},
        })
    except Exception as e:
        yield _sse({"step": "error", "error": f"ComfyUI 执行失败：{e}"})
        return
    if result.get("image"):
        output = plugins.run("after_generate", {
            "image": result["image"], "description": description,
            "prompt": prompt, "workflow_path": workflow_path,
            "is_regeneration": randomize_seed,
        })
        result["image"] = output["image"]
        description = output["description"]
    image_url = (_save_data_url(result["image"], prefix="gen_", owner_ip=owner_ip)
                 if result.get("image") else "")
    if not image_url:
        yield _sse({"step": "error",
                    "error": result.get("error")
                    or "ComfyUI 没有返回图片，请检查工作流的保存节点 ID"})
        return
    return result, image_url, description


# ---------------------------------------------------------------- 错误兜底

@app.errorhandler(404)
def _not_found(e):
    if request.path.startswith("/api/") or request.path.startswith("/outputs/"):
        return jsonify({"error": "资源不存在"}), 404
    return render_template("index.html"), 200


@app.errorhandler(Exception)
def _handle_all(e):
    # 参数校验类错误不该被报成 500，否则前端会把「方法不对/参数缺失」
    # 显示成「服务器错误」，排查时很误导。
    if isinstance(e, HTTPException):
        code = e.code or 500
        if code == 405:
            return jsonify({"error": f"请求方法不允许（{request.method} {request.path}）"}), 405
        return jsonify({"error": e.description or "请求错误"}), code
    return jsonify({"error": f"服务器错误：{e}"}), 500


if __name__ == "__main__":
    import waitress

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    print("正在启动喵梓网页服务...")
    print(f"本机访问：http://127.0.0.1:{port}")
    print(f"监听地址：{host}，端口：{port}")
    print("首次使用请在网页「配置」中填写 ComfyUI 和 LLM 连接信息。")
    if not settings.access_password:
        print("提示：当前未设置访问口令，可在网页「配置」中设置。")
    waitress.serve(app, host=host, port=port, threads=16)
