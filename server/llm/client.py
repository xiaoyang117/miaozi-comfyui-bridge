"""通过 OpenAI 兼容接口调用本机或远程 LLM。"""

import ipaddress
import json
import re
from urllib.parse import urlsplit

import requests

from backends import local_session
from llm import prompts as P

# 模型有时爱把提示词包在 markdown 代码块或引号里，统一剥掉
_FENCE_HEAD = re.compile(r"^```[\w\-]*\s*\n?")
_FENCE_TAIL = re.compile(r"\n?```\s*$")


def clean_prompt(text: str) -> str:
    """去掉代码块、包裹引号、前缀标签，只留提示词本体。"""
    t = (text or "").strip()
    t = _FENCE_HEAD.sub("", t)
    t = _FENCE_TAIL.sub("", t)
    t = t.strip()
    # 去掉「提示词：」「Prompt:」这类前缀
    t = re.sub(r"^(提示词|Prompt|Positive Prompt)\s*[:：]\s*", "", t,
               flags=re.IGNORECASE)
    # 单个整体引号包裹
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'`“”":
        t = t[1:-1].strip()
    return t.strip()


class LLMError(RuntimeError):
    pass


class LLMClient:
    """一次生成流程复用一个实例，避免重复读配置。"""

    def __init__(self, config: dict) -> None:
        self.base_url = str(config.get("base_url") or "").rstrip("/")
        self.api_key = config.get("api_key") or ""
        self.model = config.get("model") or ""
        self.timeout = int(config.get("timeout") or 180)
        self.temperature = float(config.get("temperature", 0.1))
        host = urlsplit(self.base_url).hostname
        try:
            private_host = bool(host and ipaddress.ip_address(host).is_private)
        except ValueError:
            private_host = host == "localhost" or bool(host and host.endswith(".local"))
        self._session = local_session() if private_host else requests

    # ---------- 底层调用 ----------

    def chat(self, messages: list) -> str:
        if not self.base_url:
            raise LLMError("未配置 LLM API 地址")
        if not self.model:
            raise LLMError("未配置模型名称")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        data = self._post_chat(self._session, self.base_url, headers, body,
                               "LLM API")
        return self._extract_text(data)

    def _post_chat(self, session, url: str, headers: dict, body: dict,
                   label: str):
        try:
            resp = session.post(f"{url}/chat/completions",
                                headers=headers, json=body,
                                timeout=self.timeout)
        except requests.exceptions.ConnectionError:
            raise LLMError(f"无法连接{label}（{url}）")
        except requests.exceptions.Timeout:
            raise LLMError(f"{label}请求超时（{self.timeout}s）")

        if resp.status_code in (401, 403):
            raise LLMError(f"{label}认证失败（HTTP {resp.status_code}），"
                           f"请检查 API Key")
        if resp.status_code == 404:
            hint = "模型不存在" if "model" in resp.text.lower() \
                else "接口地址不存在"
            raise LLMError(f"{label}{hint}")
        if not resp.ok:
            raise LLMError(f"{label}错误（HTTP {resp.status_code}）："
                           f"{resp.text[:300]}")

        try:
            data = resp.json()
        except Exception:
            raise LLMError(f"{label}返回非 JSON 内容：{resp.text[:300]}")

        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            if isinstance(err, dict):
                err = err.get("message", str(err))
            raise LLMError(f"{label}返回错误：{err}")
        return data

    @staticmethod
    def _extract_text(data) -> str:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(
                "LLM 返回格式异常：" +
                json.dumps(data, ensure_ascii=False)[:300])
        if isinstance(content, list):
            # 有些接口把内容拆成数组
            content = "".join(
                part.get("text", "") for part in content
                if isinstance(part, dict))
        return (content or "").strip()

    # ---------- 业务封装 ----------

    def call(self, system: str, user: str) -> str:
        """system + 单条 user，返回原始文本。"""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        return self.chat(messages)

    def generate_prompt(self, user_input: str, history_text: str = "",
                        context: str = "", specific: bool = False,
                        custom_system: str = "") -> str:
        """生成最终提示词。context 是角色库参考资料。"""
        if context:
            template = P.PROMPT_WITH_CONTEXT_SPECIFIC if specific else P.PROMPT_WITH_CONTEXT
            body = f"角色参考资料：\n{context}\n\n用户需求：{user_input}"
            if history_text:
                body = P.HISTORY_WRAPPER.format(
                    history=history_text, current=body)
            raw = self.call(template, body)
        else:
            template = (custom_system.strip()
                        or (P.PROMPT_SYSTEM_SPECIFIC if specific else P.PROMPT_SYSTEM))
            body = user_input
            if history_text:
                body = P.HISTORY_WRAPPER.format(
                    history=history_text, current=user_input)
            raw = self.call(template, body)
        return clean_prompt(raw)

    def extract_tags(self, user_input: str) -> str:
        """把中文角色名转成 Danbooru 标签，失败返回空串。"""
        try:
            return clean_prompt(self.call(P.TO_DANBOORU_TAG, user_input))
        except Exception:
            return ""
