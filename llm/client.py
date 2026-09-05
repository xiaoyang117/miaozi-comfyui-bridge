import json
import re

import requests

from logger import get_logger as _get_logger

logger = _get_logger("llm")

PROMPT_SYSTEM = (
    "You are a prompt generator. "
    "Output ONLY the prompt. No greetings, no notes, no labels, no markdown, no JSON, no quotes. "
    "Do NOT include phrases like 'Here is', 'I created', 'Prompt:', or any explanation. "
    "Just the prompt text, nothing before, nothing after. "
    "Include subject, style, lighting, composition, quality keywords. "
    "Character features: hair color, eye color, body type."
)

# 小模型专用：更短、示例更少、只要求一个名字
SEARCH_SYSTEM_TINY = (
    "你是角色名翻译器。把用户的话翻译成英文角色标签。\n"
    "只输出：角色英文名, 作品英文名（没有作品就只输出角色名）\n"
    "不要中文，不要解释。\n"
    "例子：白子 -> shiroko\n"
    "例子：碧蓝档案的白子 -> shiroko, blue_archive\n"
)

# 从中文描述抽取角色中文名（用于别名学习 / 翻译兜底）
EXTRACT_CN_SYSTEM = (
    "从用户描述中提取角色名。"
    "只输出角色中文名（2-6个字），不要作品名、不要解释。"
    "如果不知道是什么角色，输出：未知"
)

# 让 LLM 判断输出方向（横/竖/方），小模型友好：只输出一个词
SIZE_DECIDE_SYSTEM = (
    "你是图片方向判断器。根据用户的描述，判断最适合的输出方向。\n"
    "只输出一个词，不要解释，不要多余字：\n"
    "portrait  —— 竖图（头像、单人立绘、全身、手机壁纸）\n"
    "landscape —— 横图（风景、多人场景、桌面壁纸、宽场景）\n"
    "square    —— 方图（默认，无法判断时用这个）\n"
    "例子：\n"
    "输入：一个人的头像 -> portrait\n"
    "输入：海边日落风景 -> landscape\n"
    "输入：一只猫 -> square\n"
)

_IMG_REF = re.compile(
    r'(?:data:image/\w+;base64[^ ]*|https?://\S+\.(?:png|jpg|jpeg|gif|webp|bmp|svg)\b|\b\w+\.(?:png|jpg|jpeg|gif|webp|bmp|svg))\b',
    re.IGNORECASE)
_IMG_CLEAN = re.compile(r'\bimage\.(?:png|jpg|jpeg|gif|webp|bmp|svg)\b', re.IGNORECASE)
_REPLACE_IMG = re.compile(r'\[img\]', re.IGNORECASE)

PROMPT_SYSTEM_SPECIFIC = (
    "You are a prompt generator. "
    "Must include: character full name, source/work name, "
    "hair color, eye color, body type, "
    "and detailed visual features (clothing, accessories). "
    "Output ONLY the prompt. No greetings, no notes, no labels, no markdown, no JSON, no quotes. "
    "Do NOT include phrases like 'Here is', 'I created', 'Prompt:', or any explanation."
)

PROMPT_WITH_CONTEXT_SPECIFIC = (
    "You are a prompt generator. "
    "Below is reference info about a specific character's appearance from web search. "
    "Must include: character full name, source/work name, "
    "hair color, eye color, body type, "
    "and detailed visual features (clothing, accessories). "
    "Output ONLY the prompt. No greetings, no notes, no labels, no markdown, no JSON, no quotes. "
    "Do NOT include phrases like 'Here is', 'I created', 'Prompt:', or any explanation."
)


class LLMClient:
    def __init__(self, base_url: str = "", api_key: str = "", model: str = "",
                 custom_system_prompt: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._prompt_system = custom_system_prompt.strip() or PROMPT_SYSTEM

    def _call(self, system: str, user: str, history: list = None) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        def clean_text(t):
            t = _IMG_REF.sub('', t or '')
            t = _IMG_CLEAN.sub('', t)
            t = t.replace('[img]', '').replace('[IMG]', '')
            return t.strip()

        user_clean = clean_text(user)
        system_clean = clean_text(system)

        messages = [{"role": "system", "content": system_clean}]
        if history:
            msgs = []
            for m in history:
                c = m.get("content", "")
                if isinstance(c, str):
                    c = clean_text(c)
                elif isinstance(c, list):
                    c = [{"type": x.get("type","text"), "text": clean_text(x.get("text",""))} for x in c]
                msgs.append({**m, "content": c})
            messages.extend(msgs)
        messages.append({"role": "user", "content": user_clean})

        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
        }

        # Sanity check: strip any remaining image refs
        body_str = json.dumps(body)
        if re.search(r'image\.(png|jpg|jpeg|gif|webp)', body_str, re.IGNORECASE) or '[img]' in body_str.lower():
            for msg in messages:
                c = msg.get("content", "")
                if isinstance(c, str):
                    c = re.sub(r'\b\w*image\w*\.(?:png|jpg|jpeg|gif|webp|bmp|svg)\b', '', c, flags=re.IGNORECASE)
                    c = c.replace('[img]', '').replace('[IMG]', '')
                    msg["content"] = c.strip()

        try:
            resp = requests.post(url, headers=headers, json=body, timeout=120)
        except requests.exceptions.ConnectionError:
            raise RuntimeError(f"无法连接到 LLM API ({self.base_url})")
        except requests.exceptions.Timeout:
            raise RuntimeError("LLM API 请求超时")

        raw = resp.text
        if resp.status_code in (401, 403):
            raise RuntimeError(f"LLM API 认证失败 (HTTP {resp.status_code})")
        if resp.status_code == 404:
            issue = "模型不存在" if "model" in raw.lower() else "地址不存在"
            raise RuntimeError(f"LLM API {issue}")
        if not resp.ok:
            err_body = raw[:500]
            if "image" in err_body.lower() and ("not support" in err_body.lower() or "cannot read" in err_body.lower()):
                raise RuntimeError(f"模型 '{self.model}' 不支持图片输入，请使用纯文本模型")
            raise RuntimeError(f"LLM API 错误 (HTTP {resp.status_code}): {err_body}")

        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(f"LLM API 返回了非 JSON 内容:\n{raw[:500]}")

        if "error" in data:
            err_msg = data["error"]
            if isinstance(err_msg, dict):
                err_msg = err_msg.get("message", str(err_msg))
            err_lower = str(err_msg).lower()
            if "image" in err_lower and ("not support" in err_lower or "cannot read" in err_lower):
                raise RuntimeError(f"模型 '{self.model}' 不支持图片输入，请使用纯文本模型")
            raise RuntimeError(f"LLM API 返回错误: {err_msg}")

        try:
            text = data["choices"][0]["message"]["content"].strip()
            if "cannot read" in text.lower() and "image" in text.lower() and "not support" in text.lower():
                raise RuntimeError(f"模型 '{self.model}' 不支持图片输入，请使用纯文本模型")
            # Post-process: strip markdown code blocks, JSON wrapping, quotes
            text = re.sub(r'^```[\w]*\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
            text = re.sub(r'^["\'`]|["\'`]$', '', text)
            text = text.strip()
            return text
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(
                f"LLM API 返回格式异常:\n{json.dumps(data, ensure_ascii=False)[:500]}"
            )
