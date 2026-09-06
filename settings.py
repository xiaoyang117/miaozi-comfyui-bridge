"""配置中心：负责 settings.json 的读写与统一访问。

所有键都以 DEFAULT_SETTINGS 为白名单与默认值来源，
保证前端能保存、后端能读取的配置项完全一致。
"""
import json
import threading
from pathlib import Path

BASE_DIR = Path(__file__).parent
SETTINGS_FILE = BASE_DIR / "settings.json"

DEFAULT_SETTINGS = {
    # ---- LLM（提示词生成）----
    "llm_base_url": "http://localhost:11434/v1",
    "llm_api_key": "",
    "llm_model": "qwen2.5:7b",
    "custom_system_prompt": "",

    # ---- VLM（图片识别）----
    "vlm_base_url": "",
    "vlm_api_key": "",
    "vlm_model": "",

    # ---- ComfyUI ----
    "comfyui_url": "http://127.0.0.1:8188",

    # ---- 工作流 ----
    # 注意：此字段为相对于 workflows 目录的文件名（如 "default_workflow.json"），
    # 也兼容绝对路径。若留空，默认取 workflows/default_workflow.json。
    "workflow_path": "default_workflow.json",
    # 提示词占位符：出现在工作流 positive 节点文本里的字符串，会被替换为 LLM 生成的提示词
    "prompt_placeholder": "114514.1919810",
    # 保存图片的节点 ID（SaveImage）。默认 66 对应 MIAOMIAO 工作流；
    # 若用 default_workflow 需在配置页改成 9。找不到时后端会自动遍历兜底。
    "save_node_id": "66",

    # ---- 尺寸占位符（用于提示词内联指定宽高，可选）----
    "width_placeholder": "chang",
    "height_placeholder": "gao",
    # 生成默认宽高
    "gen_width": 896,
    "gen_height": 1152,
    # 预设分辨率（对话框工具栏可快速选择）
    "resolution_presets": [
        {"label": "896×1152", "width": 896, "height": 1152},
        {"label": "1152×896", "width": 1152, "height": 896},
    ],

    # ---- 提示词生成附加指令 ----
    "prompt_instructions": "",

    # ---- 搜索（legacy，主流程已本地化，保留键位兼容旧 settings.json）----
    "search_sources": [
        {"name": "Bing", "url": "https://www.bing.com/search?q={query}&count={count}"}
    ],
    # 网络代理（角色库在线重建/下载用）
    "proxy_url": "http://127.0.0.1:7897",
    "danbooru_api_key": "",
    "danbooru_username": "",

    # ---- 对话 ----
    # 历史上下文轮数：每轮=1条用户消息+1条助手回复；0 表示每次全新对话
    "history_rounds": 2,
}

_settings_lock = threading.RLock()


class Settings:
    def __init__(self):
        self._data = dict(DEFAULT_SETTINGS)
        self._load()

    # ---------- 内部：读写 ----------
    def _load(self):
        if not SETTINGS_FILE.exists():
            self._save()  # 首次运行生成默认配置文件
            return
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                loaded = {}
            with _settings_lock:
                for k, v in loaded.items():
                    if k in DEFAULT_SETTINGS:
                        self._data[k] = v
        except Exception:
            pass

    def _save(self):
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def get_all(self) -> dict:
        with _settings_lock:
            # 深拷贝：避免把内部可变对象（如 resolution_presets 的 dict/list）
            # 直接暴露给调用方，防止其改值污染内存中的真实配置。
            return json.loads(json.dumps(self._data, ensure_ascii=False))

    def update(self, data: dict):
        """仅允许白名单内的键，超范围的请求被丢弃。"""
        if not isinstance(data, dict):
            return
        with _settings_lock:
            for k, v in data.items():
                if k in DEFAULT_SETTINGS:
                    self._data[k] = v if not isinstance(v, str) else v.strip()
            self._save()

    # ---------- 便捷访问器 ----------
    @property
    def llm_base_url(self) -> str:
        return str(self._data.get("llm_base_url", ""))

    @property
    def llm_api_key(self) -> str:
        return str(self._data.get("llm_api_key", ""))

    @property
    def llm_model(self) -> str:
        return str(self._data.get("llm_model", ""))

    @property
    def custom_system_prompt(self) -> str:
        return str(self._data.get("custom_system_prompt", ""))

    @property
    def vlm_base_url(self) -> str:
        return str(self._data.get("vlm_base_url", ""))

    @property
    def vlm_api_key(self) -> str:
        return str(self._data.get("vlm_api_key", ""))

    @property
    def vlm_model(self) -> str:
        return str(self._data.get("vlm_model", ""))

    @property
    def comfyui_url(self) -> str:
        return str(self._data.get("comfyui_url", ""))

    @property
    def workflow_path(self) -> str:
        """返回可用的工作流路径；若为相对名则定位到 workflows 目录。"""
        p = str(self._data.get("workflow_path", "") or "").strip()
        if not p:
            p = "default_workflow.json"
        cand = Path(p)
        if not cand.is_absolute():
            cand = BASE_DIR / "workflows" / p
        return str(cand)

    @property
    def workflow_name(self) -> str:
        """仅返回文件名部分，便于界面展示。"""
        return Path(self.workflow_path).name

    @property
    def prompt_placeholder(self) -> str:
        return str(self._data.get("prompt_placeholder", ""))

    @property
    def save_node_id(self) -> str:
        return str(self._data.get("save_node_id", ""))

    @property
    def width_placeholder(self) -> str:
        return str(self._data.get("width_placeholder", ""))

    @property
    def height_placeholder(self) -> str:
        return str(self._data.get("height_placeholder", ""))

    @property
    def gen_width(self) -> int:
        return _to_int(self._data.get("gen_width", 896), 896)

    @property
    def gen_height(self) -> int:
        return _to_int(self._data.get("gen_height", 1152), 1152)

    @property
    def resolution_presets(self) -> list:
        presets = self._data.get("resolution_presets")
        if isinstance(presets, list) and presets:
            return presets
        return list(DEFAULT_SETTINGS["resolution_presets"])

    @property
    def prompt_instructions(self) -> str:
        return str(self._data.get("prompt_instructions", ""))

    @property
    def proxy_url(self) -> str:
        return str(self._data.get("proxy_url", ""))

    @property
    def danbooru_api_key(self) -> str:
        return str(self._data.get("danbooru_api_key", ""))

    @property
    def danbooru_username(self) -> str:
        return str(self._data.get("danbooru_username", ""))

    @property
    def search_sources(self) -> list:
        raw = self._data.get("search_sources")
        if isinstance(raw, list) and raw:
            return raw
        return list(DEFAULT_SETTINGS["search_sources"])

    @property
    def history_rounds(self) -> int:
        return _to_int(self._data.get("history_rounds", 2), 2)


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
