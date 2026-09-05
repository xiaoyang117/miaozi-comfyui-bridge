"""OpenAI 兼容接口层：让标准 OpenAI SDK / 客户端可直接调用本服务生图。

用法（任意支持 OpenAI Images API 的客户端）：
    from openai import OpenAI
    client = OpenAI(base_url="http://127.0.0.1:5000/v1", api_key="miaozi")
    resp = client.images.generate(
        model="miaozi-image-xl",
        prompt="shiroko, blue archive, swimsuit",
        size="1024x1024",
        n=1,
        response_format="b64_json",
    )

提供端点：
    GET  /v1/models                    模型列表
    POST /v1/images/generations        文生图（OpenAI Images API 兼容）

可选鉴权：设置环境变量 MIAOZI_API_KEY 后，客户端须带
Authorization: Bearer <key>；未设置则放行（本地工具默认）。
"""
from __future__ import annotations

import base64
import os
import time

from flask import Blueprint, jsonify, request

openai_bp = Blueprint("openai", __name__)

# 可选鉴权：配置 MIAOZI_API_KEY 后启用
_REQUIRED_KEY = os.environ.get("MIAOZI_API_KEY", "").strip()

# 本服务提供的模型 ID（宽容：请求任意 model 都会接受，便于客户端直连）
_MODELS = [
    {"id": "miaozi-image-xl", "object": "model",
     "created": 1757000000, "owned_by": "miaozi",
     "description": "Miaozi ComfyUI bridge (animagine-xl 底模)"},
    {"id": "dall-e-3", "object": "model",
     "created": 1696016800, "owned_by": "openai-compat",
     "description": "兼容别名，实际由本服务的 ComfyUI 工作流出图"},
]


def _check_auth():
    """校验 Bearer key（若已配置）。返回错误响应或 None。"""
    if not _REQUIRED_KEY:
        return None
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth else ""
    if token and token == _REQUIRED_KEY:
        return None
    return _openai_error("Incorrect API key provided",
                         "invalid_request_error", code="invalid_api_key",
                         status=401)


def _openai_error(message, err_type="invalid_request_error",
                  code=None, status=400, param=None):
    payload = {"error": {"message": message, "type": err_type,
                         "param": param, "code": code}}
    return jsonify(payload), status


def _parse_size(size: str):
    """解析 OpenAI size（'1024x1024' / 'auto'），返回 (w,h) 或 None=自动。"""
    if not size or size.lower() == "auto":
        return None
    try:
        w, h = size.lower().split("x")
        w, h = int(w), int(h)
        # 规整到 64 倍数，限制范围
        w = max(256, min(round(w / 64) * 64, 4096))
        h = max(256, min(round(h / 64) * 64, 4096))
        return w, h
    except Exception:
        return None


def _load_image_b64(image_url: str, outputs_dir) -> str:
    """把 /outputs/xxx.png 转成 OpenAI b64_json。"""
    name = image_url.rsplit("/", 1)[-1]
    p = outputs_dir / name
    if not p.exists():
        raise FileNotFoundError(f"图片文件不存在: {p}")
    return base64.b64encode(p.read_bytes()).decode("ascii")


def register_openai(app, run_generation, settings_obj, gen_lock, outputs_dir):
    """把 OpenAI 兼容端点注册进 app。

    run_generation: app._run_generation 事件生成器
    settings_obj:   app.settings
    gen_lock:       app._gen_lock
    outputs_dir:    app.OUTPUTS_DIR (Path)
    """
    log_mod = app.extensions.get("miaozi_log")
    if log_mod is None:
        from logger import get_logger
        log_mod = get_logger("openai")
        app.extensions["miaozi_log"] = log_mod

    # ---------------- /v1/models ----------------
    @openai_bp.route("/models", methods=["GET"])
    def models_list():
        err = _check_auth()
        if err:
            return err
        return jsonify({"object": "list", "data": [
            {k: v for k, v in m.items() if k != "description"} for m in _MODELS
        ]})

    # ---------------- /v1/images/generations ----------------
    @openai_bp.route("/images/generations", methods=["POST"])
    def images_generations():
        err = _check_auth()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return _openai_error("prompt is required",
                                 param="prompt")
        model = body.get("model") or _MODELS[0]["id"]
        n = int(body.get("n") or 1)
        n = max(1, min(n, 4))
        response_format = (body.get("response_format") or "url").lower()
        if response_format not in ("url", "b64_json"):
            response_format = "url"

        size = body.get("size") or ""
        wh = _parse_size(size)
        # 私有扩展字段（OpenAI 客户端不会发，但脚本可用来控制角色识别）
        role = (body.get("role") or "").strip()
        # OpenAI 的 prompt 通常是完整英文标签，默认不做角色搜索（避免误匹配）；
        # 传入 role 或 use_search=true 时才启用角色库增强。
        use_search = bool(body.get("use_search", False)) or bool(role)

        data = {
            "prompt": prompt,
            "use_search": use_search,
            "role": role,
            "workflow_path": body.get("workflow_path")
                             or settings_obj.workflow_path,
        }
        if wh:
            data["auto_resolution"] = False
            data["width"] = wh[0]
            data["height"] = wh[1]
        else:
            # 未指定尺寸：交给分辨率自动决策（规则/LLM/默认）
            data["auto_resolution"] = True
            data["width"] = None
            data["height"] = None

        # 参考图（可选，OpenAI images/edits 风格：body["image"] = [b64])
        imgs = body.get("image") or body.get("images") or []
        if isinstance(imgs, str):
            imgs = [imgs]
        if imgs:
            data["image"] = imgs

        log_mod.info("openai images/generations model=%s size=%r n=%s "
                     "search=%s", model, size, n, use_search)

        created = int(time.time())
        data_items = []
        for i in range(n):
            if not gen_lock.acquire(blocking=False):
                return _openai_error(
                    "服务器正忙（已有生图任务进行中），请稍后重试",
                    err_type="server_error", code="server_busy",
                    status=429)
            result = None
            err_msg = None
            try:
                for ev in run_generation(data):
                    if ev.get("step") == "done":
                        result = ev
                    elif ev.get("step") == "error":
                        err_msg = ev.get("error")
                        break
            finally:
                gen_lock.release()

            if err_msg:
                return _openai_error(err_msg, err_type="server_error",
                                     status=500)
            if not result or not result.get("image"):
                return _openai_error("生图失败，未返回图片",
                                     err_type="server_error", status=500)

            if response_format == "b64_json":
                try:
                    b64 = _load_image_b64(result["image"], outputs_dir)
                except Exception as e:
                    return _openai_error(str(e), err_type="server_error",
                                         status=500)
                data_items.append({"b64_json": b64})
            else:
                url = request.host_url.rstrip("/") + result["image"]
                data_items.append({"url": url})

            log_mod.info("openai done #%s -> %s", i + 1, result["image"])

        return jsonify({"created": created, "data": data_items, "model": model})

    app.register_blueprint(openai_bp, url_prefix="/v1")
    return openai_bp
