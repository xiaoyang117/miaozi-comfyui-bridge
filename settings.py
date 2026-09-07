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
    # 注意：此字段为相对于 workflows 目录的文件名（如 "MIAOMIAO 1.json"），
    # 也兼容绝对路径。若留空，默认取 workflows/MIAOMIAO 1.json。
    "workflow_path": "MIAOMIAO 1.json",
    # 提示词占位符：出现在工作流 positive 节点文本里的字符串，会被替换为 LLM 生成的提示词
    "prompt_placeholder": "114514.1919810",
    # 保存图片的节点 ID（SaveImage）。留空=自动探测工作流中的图片输出节点，
    # 一般无需手动配置；仅在自动探测失效时手动指定。
    "save_node_id": "",

    # 提示词渲染风格：danbooru 系底模吃下划线标签；Qwen/Anima 系吃空格标签。
    # auto=按工作流文件名猜测(含 anima/miao/qwen 转空格)；anima=强制空格；danbooru=下划线
    "prompt_style": "auto",

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

    # ---- 图库/二采 ----
    # ComfyUI output 目录（图库历史图来源）；空=尝试从 comfyui_url 无法推断，
    # 需在配置页手动填（如 H:/ComfyUI.../ComfyUI/output）
    "comfyui_output_dir": "",

    # ---- 搜索（legacy，主流程已本地化，保留键位兼容旧 settings.json）----
    "search_sources": [
        {"name": "Bing", "url": "https://www.bing.com/search?q={query}&count={count}"}
    ],
    # 网络代理（角色库在线重建/下载用）
    "proxy_url": "http://127.0.0.1:7897",
    "danbooru_api_key": "",
    "danbooru_username": "",

    # ---- 对话 ----
    # 是否携带上下文历史（前端按 token 预算自适应裁剪轮数）
    "use_history": True,
    # LLM 上下文窗口大小(token)：用于前端估算上下文使用量进度条
    "llm_context_tokens": 32768,

    # ---- 提示词标签化 ----
    # 从 danbooru 标签词库召回候选，让 LLM 从中挑选（保证输出为真实标签）
    "tag_selection": True,
    # 草稿合法率低于阈值时，是否再召回候选让 LLM 复选补正；关=只剔非法词不补位
    "tag_reselect": True,
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
    def comfyui_output_dir(self) -> str:
        """ComfyUI output 目录（图库历史图）；空表示未配置。"""
        return str(self._data.get("comfyui_output_dir", "") or "").strip()

    @property
    def workflow_path(self) -> str:
        """返回可用的工作流路径；若为相对名则定位到 workflows 目录。"""
        p = str(self._data.get("workflow_path", "") or "").strip()
        if not p:
            p = "MIAOMIAO 1.json"
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
    def prompt_style(self) -> str:
        """danbooru(下划线) / anima(空格) / auto(按工作流猜)。"""
        return str(self._data.get("prompt_style", "auto")).lower()

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
    def use_history(self) -> bool:
        """前端是否携带上下文历史（预算自适应）。"""
        return bool(self._data.get("use_history", True))

    @property
    def llm_context_tokens(self) -> int:
        return _to_int(self._data.get("llm_context_tokens", 32768), 32768)

    @property
    def tag_selection(self) -> bool:
        return bool(self._data.get("tag_selection", True))

    @property
    def tag_reselect(self) -> bool:
        return bool(self._data.get("tag_reselect", True))


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
