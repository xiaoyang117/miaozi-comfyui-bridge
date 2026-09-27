"""运行时可编辑的配置，持久化到 data/settings.json。

设计目标：改配置不需要重启、不需要改代码。所有字段都带默认值，
settings.json 里缺失的键自动回落到默认值。
"""

import json
import os
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# 数据目录支持环境变量覆盖，方便部署到别的位置或跑多实例
DATA_DIR = Path(os.environ.get("MIAOZI_DATA_DIR") or (BASE_DIR / "data"))
SETTINGS_FILE = DATA_DIR / "settings.json"

DEFAULT_SETTINGS = {
    # ---- 访问保护 ----
    # 留空 = 不校验。局域网/手机访问建议设置一个。
    "access_password": "",

    # ---- LLM ----
    # local = 直连本机模型（Ollama / LM Studio）；direct = 调远程 API
    "llm_mode": "direct",
    "llm_base_url": "https://api.openai.com/v1",
    "llm_api_key": "",
    "llm_model": "gpt-4o-mini",
    # local 模式下要请求的本地服务与模型名
    # 默认 llama.cpp 的 llama-server（OpenAI 兼容，默认端口 8080）；
    # Ollama / LM Studio 也能用，把地址改掉即可
    "local_llm_base_url": "http://127.0.0.1:8080/v1",
    "local_llm_api_key": "",
    # llama-server 忽略模型名，但接口要求非空；Ollama 用户请填真实模型名
    "local_llm_model": "local-model",
    # 单次 LLM 请求超时（秒），本地小模型慢，给宽一点
    "llm_timeout": 180,

    # ---- VLM 图片识别（走远程 API）----
    "vlm_base_url": "",
    "vlm_api_key": "",
    "vlm_model": "",
    "vlm_timeout": 90,

    # ---- 本机引擎：MCP + ComfyUI ----
    "mcp_enabled": True,
    # comfy-mcp 命令。装在虚拟环境里时填绝对路径，例如
    #   C:/Users/you/venv/Scripts/comfy-mcp.exe
    "mcp_command": "comfy-mcp",
    "mcp_args": [],
    "mcp_cwd": "",
    "mcp_env": {},
    # 你的 ComfyUI 工作区目录。便携版必填，否则 comfy-cli 会用它
    # 默认的空目录，模型列表是空的、自定义节点认不到。
    "comfy_project": "",
    # comfy 命令（comfy-cli）的绝对路径，只有装在 venv 里才需要
    "comfy_bin": "",
    "comfyui_url": "http://127.0.0.1:8188",
    # 轮询 ComfyUI 的间隔（秒）
    "poll_interval": 1.5,
    # 图片回传压缩：0 = 原图不压缩
    "image_max_dim": 0,
    "image_format": "png",
    "image_quality": 92,

    # ---- 生图总超时（秒）----
    "comfy_timeout": 600,

    # ---- 工作流与占位符替换 ----
    # 留空 = 自动用项目 workflows/ 目录里第一个 json
    "workflow_path": "",
    # 工作流检索目录，多个用列表
    "workflow_dirs": [],
    "prompt_placeholder": "114514.1919810",
    "save_node_id": "66",
    # 分辨率：在工作流里预留了宽高占位符时才会生效
    "width_placeholder": "",
    "height_placeholder": "",
    "gen_width": 896,
    "gen_height": 1152,
    "resolution_presets": [
        {"label": "896×1152", "width": 896, "height": 1152},
        {"label": "1152×896", "width": 1152, "height": 896},
        {"label": "1024×1024", "width": 1024, "height": 1024},
    ],

    # ---- 提示词风格 ----
    "custom_system_prompt": "",
    "positive_prompt_prefix": "",
    "prompt_instructions": "",

    # ---- 对话 ----
    "history_rounds": 2,

    # ---- 角色库 ----
    "use_character_db": True,

    # ---- 输出图片清理 ----
    # 每次生图都会留一张图。设成大于 0 就会自动只保留最近这么多张。
    # 0 表示不清理。
    "keep_outputs": 300,
}


class Settings:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data = dict(DEFAULT_SETTINGS)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        if not SETTINGS_FILE.exists():
            return
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                with self._lock:
                    # 只接受已知键，顺手丢掉历史版本遗留的字段
                    for key, value in loaded.items():
                        if key in DEFAULT_SETTINGS:
                            self._data[key] = value
        except Exception:
            # 配置坏了不应该让服务起不来
            pass

    def _save(self) -> None:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        tmp.replace(SETTINGS_FILE)

    # ---------- 读写 ----------

    def get_all(self) -> dict:
        with self._lock:
            return dict(self._data)

    def update(self, data: dict) -> dict:
        with self._lock:
            for key, value in (data or {}).items():
                if key not in DEFAULT_SETTINGS:
                    continue
                if isinstance(value, str):
                    self._data[key] = value.strip()
                else:
                    self._data[key] = value
            self._save()
            return dict(self._data)

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, DEFAULT_SETTINGS.get(key, default))

    # ---------- 便捷属性 ----------

    @property
    def access_password(self) -> str:
        return str(self.get("access_password") or "")

    @property
    def llm_mode(self) -> str:
        mode = str(self.get("llm_mode") or "direct")
        if mode == "bridge":
            mode = "local"        # 兼容历史配置
        return mode if mode in ("direct", "local") else "direct"

    def _int(self, key: str, fallback: int) -> int:
        try:
            return int(self.get(key))
        except (TypeError, ValueError):
            return fallback

    @property
    def llm_timeout(self) -> int:
        return self._int("llm_timeout", 180)

    @property
    def vlm_timeout(self) -> int:
        return self._int("vlm_timeout", 90)

    @property
    def comfy_timeout(self) -> int:
        return self._int("comfy_timeout", 600)

    @property
    def history_rounds(self) -> int:
        return max(0, self._int("history_rounds", 2))

    @property
    def gen_width(self) -> int:
        return self._int("gen_width", 896)

    @property
    def gen_height(self) -> int:
        return self._int("gen_height", 1152)

    @property
    def resolution_presets(self) -> list:
        raw = self.get("resolution_presets")
        if isinstance(raw, list) and raw:
            out = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                try:
                    out.append({
                        "label": str(item.get("label") or
                                     f"{item['width']}×{item['height']}"),
                        "width": int(item["width"]),
                        "height": int(item["height"]),
                    })
                except (KeyError, TypeError, ValueError):
                    continue
            if out:
                return out
        return [dict(p) for p in DEFAULT_SETTINGS["resolution_presets"]]

    def resolve_size(self, preset_index) -> tuple:
        """把前端传来的预设序号/自定义宽高解析成 (width, height)。"""
        presets = self.resolution_presets
        try:
            idx = int(preset_index)
        except (TypeError, ValueError):
            idx = -1
        if 0 <= idx < len(presets):
            return presets[idx]["width"], presets[idx]["height"]
        return self.gen_width, self.gen_height

    def build_replacements(self, prompt: str, width: int, height: int) -> dict:
        """生成工作流里的占位符替换表。

        值保留原始类型（提示词是字符串，宽高是数字）。后端按 JSON 令牌
        整体替换，所以工作流里写 "width":"PH_W" 会变成 "width":896
        —— 数字不带引号，正好是 ComfyUI 期望的写法。
        空占位符自动跳过。
        """
        repl = {}
        ph = str(self.get("prompt_placeholder") or "").strip()
        if ph:
            repl[ph] = prompt
        wph = str(self.get("width_placeholder") or "").strip()
        if wph:
            repl[wph] = int(width)
        hph = str(self.get("height_placeholder") or "").strip()
        if hph:
            repl[hph] = int(height)
        return repl


settings = Settings()
