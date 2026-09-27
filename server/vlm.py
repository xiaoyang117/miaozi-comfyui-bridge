"""VLM 图片识别：把用户上传的图变成文字描述，再喂给提示词生成。

放在服务器侧直调远程 API，本地机器不用额外装视觉模型。
"""

import requests


class VLMError(RuntimeError):
    pass


def describe_images(images: list, config: dict) -> str:
    """images 是 data URL 或 http 图片地址列表，返回描述文本。"""
    if not images:
        raise VLMError("没有图片数据")

    base_url = str(config.get("base_url") or "").rstrip("/")
    api_key = config.get("api_key") or ""
    model = config.get("model") or ""
    timeout = int(config.get("timeout") or 90)

    if not base_url or not model:
        raise VLMError("VLM 未配置（需要在配置里填 VLM 地址和模型名）")

    if len(images) == 1:
        prompt_text = ("请详细描述这张图片中的角色外貌特征，"
                       "包括发色、瞳色、发型、体型、服装、配饰等。"
                       "描述要具体，便于转成绘图提示词。")
    else:
        prompt_text = ("请按顺序逐一描述每张图片中的角色外貌特征"
                       "（图片1、图片2……），包括发色、瞳色、发型、"
                       "体型、服装、配饰等。")

    content = [{"type": "text", "text": prompt_text}]
    for img in images:
        content.append({"type": "image_url", "image_url": {"url": img}})

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.1,
    }

    try:
        resp = requests.post(f"{base_url}/chat/completions",
                             headers=headers, json=body, timeout=timeout)
    except requests.exceptions.ConnectionError:
        raise VLMError(f"无法连接到 VLM（{base_url}）")
    except requests.exceptions.Timeout:
        raise VLMError(f"VLM 请求超时（{timeout}s）")

    if not resp.ok:
        raise VLMError(f"VLM API 错误（HTTP {resp.status_code}）：{resp.text[:300]}")

    try:
        data = resp.json()
    except Exception:
        raise VLMError(f"VLM 返回非 JSON：{resp.text[:200]}")

    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        if isinstance(err, dict):
            err = err.get("message", str(err))
        raise VLMError(f"VLM 错误：{err}")

    try:
        content_out = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise VLMError("VLM 返回格式异常")

    if isinstance(content_out, list):
        content_out = "".join(p.get("text", "") for p in content_out
                              if isinstance(p, dict))
    return (content_out or "").strip()
