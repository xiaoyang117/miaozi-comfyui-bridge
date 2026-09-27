"""喵梓 · 本机版服务端

单进程跑完所有事：网站界面、会话历史、提示词调度、MCP 驱动 ComfyUI、
代理本机模型。没有独立桥接进程，也没有跨机通信。

启动：python app.py
"""

import base64
import glob as globmod
import json
import os
import threading
import uuid
from pathlib import Path

from flask import (Flask, Response, jsonify, render_template, request,
                   send_from_directory)
from werkzeug.exceptions import HTTPException

from character_lookup import is_built as char_db_built
from character_lookup import lookup as char_lookup
from engine import engine
from llm import LLMError
from llm.client import LLMClient
from llm.prompts import build_history_text
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
    if path in ("/api/auth",) or any(path.startswith(p) for p in _OPEN_PATHS):
        return None
    given = (request.headers.get("X-Access-Password")
             or request.args.get("pw") or "")
    if given != password:
        return jsonify({"error": "口令错误", "need_auth": True}), 401
    return None


# ---------------------------------------------------------------- 工具

def _make_llm(**overrides) -> LLMClient:
    cfg = {
        "mode": settings.llm_mode,
        "base_url": settings.get("llm_base_url"),
        "api_key": settings.get("llm_api_key"),
        "model": settings.get("llm_model"),
        "local_base_url": settings.get("local_llm_base_url"),
        "local_api_key": settings.get("local_llm_api_key"),
        "local_model": settings.get("local_llm_model"),
        "timeout": settings.llm_timeout,
    }
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    return LLMClient(cfg)


def _llm_config_for_test(data: dict) -> LLMClient:
    mode = data.get("llm_mode") or settings.llm_mode
    if mode == "bridge":
        mode = "local"
    return LLMClient({
        "mode": mode,
        "base_url": data.get("llm_base_url") or settings.get("llm_base_url"),
        "api_key": data.get("llm_api_key") or settings.get("llm_api_key"),
        "model": data.get("llm_model") or settings.get("llm_model"),
        "local_base_url": (data.get("local_llm_base_url")
                           or settings.get("local_llm_base_url")),
        "local_api_key": (data.get("local_llm_api_key")
                          or settings.get("local_llm_api_key")),
        "local_model": (data.get("local_llm_model")
                        or settings.get("local_llm_model")),
        "timeout": min(settings.llm_timeout, 120),
    })


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _public_settings() -> dict:
    """给前端看的配置，剥掉口令这类不该下发到浏览器的东西。

    访问口令只回一个布尔值 auth_required，前端据此决定是否弹口令框。
    """
    data = settings.get_all()
    data.pop("access_password", None)
    data["auth_required"] = bool(settings.access_password)
    return data


def _save_data_url(data_url: str, prefix: str = "") -> str:
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
    name = f"{prefix}{uuid.uuid4().hex[:20]}{ext}"
    try:
        (OUTPUT_DIR / name).write_bytes(base64.b64decode(b64))
    except Exception:
        return ""
    _prune_outputs()
    return f"/outputs/{name}"


_prune_lock = threading.Lock()


def _prune_outputs() -> None:
    """只保留最近 keep_outputs 张图，防止磁盘被慢慢填满。"""
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
                 if p.is_file() and p.suffix.lower() in ALLOWED_IMAGE_EXT]
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
    return send_from_directory(OUTPUT_DIR, filename)


@app.route("/api/auth", methods=["POST"])
def api_auth():
    data = request.get_json(silent=True) or {}
    password = settings.access_password
    if not password:
        return jsonify({"success": True, "need_auth": False})
    ok = data.get("password") == password
    return jsonify({"success": ok, "need_auth": True}), (200 if ok else 401)


# ---------------------------------------------------------------- 启动数据

@app.route("/api/bootstrap")
def api_bootstrap():
    """一次性把前端需要的所有状态取回去，减少来回请求。"""
    return jsonify({
        "success": True,
        "settings": _public_settings(),
        "engine": engine.status(),
        "character_db": char_db_built(),
        "sessions": store.list_sessions(limit=30),
    })


# ---------------------------------------------------------------- 配置

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify({"success": True, "settings": _public_settings()})
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "无效的配置数据"}), 400
    settings.update(data)
    return jsonify({"success": True, "settings": _public_settings()})


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
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        return jsonify({"success": True,
                        "session": store.create_session(data.get("title", ""))})
    return jsonify({"success": True, "sessions": store.list_sessions(limit=50)})


@app.route("/api/sessions/<session_id>", methods=["GET", "DELETE", "PATCH"])
def api_session(session_id):
    if request.method == "DELETE":
        store.delete_session(session_id)
        return jsonify({"success": True})
    if request.method == "PATCH":
        data = request.get_json(silent=True) or {}
        store.rename_session(session_id, data.get("title", "新对话"))
        return jsonify({"success": True})
    return jsonify({"success": True,
                    "messages": store.list_messages(session_id)})


@app.route("/api/sessions/<session_id>/clear", methods=["POST"])
def api_session_clear(session_id):
    store.clear_messages(session_id)
    return jsonify({"success": True})


@app.route("/api/messages/<int:message_id>", methods=["DELETE"])
def api_message_delete(message_id):
    store.delete_message(message_id)
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
        label = "本机模型" if client.mode == "local" else "远程 API"
        return jsonify({"success": True, "output": out[:500],
                        "mode": client.mode, "mode_label": label})
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
    return jsonify({"success": False,
                    "error": f"未找到「{query}」，角色名请用英文或罗马音"})


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
        items = result.get("workflows") or []
        # 兜底：把项目自带的 workflows/ 目录也列进去
        if DEFAULT_WORKFLOW_DIR.is_dir():
            seen = {i.get("path") for i in items}
            for f in sorted(globmod.glob(str(DEFAULT_WORKFLOW_DIR / "*.json"))):
                ap = os.path.abspath(f)
                if ap not in seen:
                    items.append({"path": ap, "name": os.path.basename(f)})
        return jsonify({"success": True, "workflows": items,
                        "dir": result.get("dir", "")})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 200


# ---------------------------------------------------------------- 生成主流程

@app.route("/api/generate", methods=["POST"])
def api_generate():
    if not _gen_lock.acquire(blocking=False):
        return jsonify({"queue": True}), 200

    payload = request.get_json(silent=True) or {}

    def stream():
        try:
            yield from _run_generation(payload)
        except Exception as e:  # noqa: BLE001  任何异常都要能吐回前端
            yield _sse({"step": "error", "error": str(e)})
        finally:
            _gen_lock.release()

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


def _run_generation(payload: dict):
    user_input = (payload.get("prompt") or "").strip()
    if not user_input:
        yield _sse({"step": "error", "error": "请输入图片描述"})
        return

    session_id = store.ensure_session(payload.get("session_id") or "")
    use_character = bool(payload.get("use_search", True))
    specific = bool(payload.get("specific", False))
    use_history = bool(payload.get("use_history", True))
    workflow_path = _resolve_workflow_path(payload.get("workflow_path"))
    if not workflow_path:
        yield _sse({"step": "error",
                    "error": "没有找到工作流文件。请在配置页设置工作流路径"})
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
    character_name = ""
    if use_character and char_db_built():
        yield _sse({"step": "search"})
        try:
            tags = client.extract_tags(user_input)
            if tags:
                character_info = char_lookup(tags)
                character_name = tags
                if not character_info:
                    # 只拿角色名再试一次（作品名可能拼错）
                    first = tags.split(",")[0].strip()
                    if first and first != tags:
                        character_info = char_lookup(first)
                        if character_info:
                            character_name = first
        except Exception as e:
            # 角色库只是增强，失败不该中断生图
            print(f"[character lookup] {e}")

    # ---------- 2. 生成提示词 ----------
    yield _sse({"step": "llm"})
    history_text = ""
    if use_history and settings.history_rounds > 0:
        history_text = build_history_text(
            store.context_pairs(session_id, settings.history_rounds))
    try:
        prompt = client.generate_prompt(
            user_input_full,
            history_text=history_text,
            context=character_info,
            specific=specific,
            custom_system=settings.get("custom_system_prompt") or "",
        )
    except LLMError as e:
        yield _sse({"step": "error", "error": f"提示词生成失败：{e}"})
        return
    if not prompt:
        yield _sse({"step": "error", "error": "模型没有返回提示词，请重试"})
        return

    # ---------- 3. 引擎驱动 ComfyUI 出图 ----------
    yield _sse({"step": "comfyui"})
    replacements = settings.build_replacements(prompt, width, height)
    try:
        result = engine.generate({
            "workflow_path": workflow_path,
            "prompt": prompt,
            "replacements": replacements,
            "width": width,
            "height": height,
            "timeout": settings.comfy_timeout,
        })
    except Exception as e:
        yield _sse({"step": "error", "error": f"ComfyUI 执行失败：{e}"})
        return

    image_url = ""
    if result.get("image"):
        image_url = _save_data_url(result["image"], prefix="gen_")
    if not image_url:
        yield _sse({"step": "error",
                    "error": result.get("error")
                    or "ComfyUI 没有返回图片，请检查工作流的保存节点 ID"})
        return

    # ---------- 4. 落库 ----------
    store.add_message(session_id, "user", content=user_input,
                      vlm=vlm_text,
                      meta={"has_image": bool(images),
                            "image_count": len(images)})
    store.add_message(session_id, "assistant", content=prompt,
                      image=image_url, character=character_info,
                      meta={"width": width, "height": height,
                            "elapsed": result.get("elapsed")})

    yield _sse({"step": "done", "image": image_url, "prompt": prompt,
                "character": character_info, "vlm": vlm_text,
                "session_id": session_id})


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
    print(f"喵梓服务已启动： http://127.0.0.1:{port}")
    if not settings.access_password:
        print("提示：当前未设置访问口令。只在自己电脑用可以不设；"
              "局域网/手机访问建议在配置页里设置一个。")
    waitress.serve(app, host=host, port=port, threads=16)
