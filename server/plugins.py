"""Local plugin registry and generation hooks."""

import base64
import binascii
import copy
import importlib.util
import json
import re
import sys
import threading
from pathlib import Path

from settings import DATA_DIR

HOOKS = ("before_prompt", "after_prompt", "before_workflow", "after_generate")
PLUGIN_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
ASSET_NAME = re.compile(r"[a-zA-Z0-9_-][a-zA-Z0-9_.-]*\Z")
API_VERSION = 1


class PluginError(ValueError):
    pass


class PluginManager:
    def __init__(self, directory: Path = DATA_DIR / "plugins",
                 state_file: Path = DATA_DIR / "plugins.json") -> None:
        self.directory = Path(directory)
        self.state_file = Path(state_file)
        self._lock = threading.RLock()
        self._loaded = {}
        self._errors = {}
        self._states = {}
        self._active = {}
        self._activating = set()
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.state_file.is_file():
            try:
                saved = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(saved, dict) and isinstance(saved.get("plugins"), dict):
                    self._states = saved["plugins"]
            except (OSError, ValueError):
                pass

    def _paths(self, plugin_id: str) -> tuple[Path, Path]:
        if not PLUGIN_ID.fullmatch(plugin_id):
            raise PluginError("插件 ID 只能包含小写字母、数字、横线和下划线")
        folder = self.directory / plugin_id
        manifest = folder / "plugin.json"
        entry = folder / "main.py"
        if (folder.is_symlink() or manifest.is_symlink() or entry.is_symlink()
                or not folder.is_dir() or not manifest.is_file() or not entry.is_file()):
            raise PluginError("插件目录需要包含普通文件 plugin.json 和 main.py")
        return manifest, entry

    def _manifest(self, plugin_id: str) -> dict:
        manifest_path, _ = self._paths(plugin_id)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PluginError("插件清单不是有效的 JSON") from exc
        if not isinstance(manifest, dict) or manifest.get("id") != plugin_id:
            raise PluginError("插件清单的 id 必须与目录名一致")
        if type(manifest.get("api_version")) is not int or manifest["api_version"] != API_VERSION:
            raise PluginError(f"插件 API 版本不兼容（当前版本 {API_VERSION}）")
        if (not isinstance(manifest.get("name"), str) or not manifest["name"].strip()
                or not isinstance(manifest.get("version"), str) or not manifest["version"].strip()
                or not isinstance(manifest.get("description", ""), str)):
            raise PluginError("插件名称、版本或介绍格式不正确")
        hooks = manifest.get("hooks")
        if (not isinstance(hooks, list)
                or len(hooks) != len(set(str(hook) for hook in hooks))
                or any(hook not in HOOKS for hook in hooks)):
            raise PluginError("插件清单需要声明有效的 hooks")
        if not isinstance(manifest.get("default_config", {}), dict):
            raise PluginError("default_config 必须是 JSON 对象")
        requires = manifest.get("requires", [])
        if (not isinstance(requires, list) or len(requires) != len(set(str(item) for item in requires))
                or any(not isinstance(item, str) or not PLUGIN_ID.fullmatch(item)
                       or item == plugin_id for item in requires)):
            raise PluginError("requires 必须是其他插件的 ID 列表")
        if type(manifest.get("api", False)) is not bool:
            raise PluginError("api 必须是布尔值")
        ui = manifest.get("ui", "")
        if not isinstance(ui, str) or (ui and not ASSET_NAME.fullmatch(ui)):
            raise PluginError("ui 必须是 assets 目录中的单个文件名")
        return manifest

    def _state(self, plugin_id: str) -> dict:
        saved = self._states.get(plugin_id, {})
        if (not isinstance(saved, dict)
                or type(saved.get("enabled", False)) is not bool
                or type(saved.get("order", 0)) is not int
                or not -1000 <= saved.get("order", 0) <= 1000
                or not isinstance(saved.get("config", {}), dict)):
            self._errors[plugin_id] = "保存的插件设置格式不正确，请重新保存"
            return {}
        return saved

    def _load(self, plugin_id: str):
        if plugin_id not in self._loaded:
            _, entry = self._paths(plugin_id)
            spec = importlib.util.spec_from_file_location(
                f"miaozi_plugin_{plugin_id}", entry,
                submodule_search_locations=[str(entry.parent)])
            if spec is None or spec.loader is None:
                raise PluginError("无法读取插件入口")
            module = importlib.util.module_from_spec(spec)
            source = entry.read_text(encoding="utf-8-sig")
            sys.modules[spec.name] = module
            try:
                exec(compile(source, str(entry), "exec"), vars(module))
                if not callable(vars(module).get("transform")):
                    raise PluginError("main.py 必须提供 transform(hook, data, config)")
            except Exception:
                sys.modules.pop(spec.name, None)
                raise
            self._loaded[plugin_id] = module
        return vars(self._loaded[plugin_id])["transform"]

    def _activate(self, plugin_id: str, config: dict) -> None:
        if plugin_id in self._active:
            return
        if plugin_id in self._activating:
            raise PluginError("插件依赖形成循环")
        self._activating.add(plugin_id)
        try:
            manifest = self._manifest(plugin_id)
            for dependency in manifest.get("requires", []):
                if not self._state(dependency).get("enabled") or dependency in self._errors:
                    raise PluginError(f"依赖插件不可用：{dependency}")
                try:
                    dependency_config = self._state(dependency).get(
                        "config", self._manifest(dependency).get("default_config", {}))
                    self._activate(dependency, dependency_config)
                except PluginError as exc:
                    raise PluginError(f"依赖插件不可用：{dependency}（{exc}）") from exc
            self._load(plugin_id)
            callback = vars(self._loaded[plugin_id]).get("on_enable")
            if callable(callback):
                callback(copy.deepcopy(config))
            self._active[plugin_id] = True
        except Exception as exc:
            self._errors[plugin_id] = f"插件启动失败：{exc}"
            raise PluginError(self._errors[plugin_id]) from exc
        finally:
            self._activating.discard(plugin_id)

    def _deactivate(self, plugin_id: str) -> None:
        if plugin_id not in self._active:
            return
        self._active.pop(plugin_id)
        callback = vars(self._loaded[plugin_id]).get("on_disable")
        if callable(callback):
            try:
                callback()
            except Exception as exc:
                self._errors[plugin_id] = f"插件停用失败：{exc}"
                print(f"[plugins] {plugin_id}: {self._errors[plugin_id]}", flush=True)

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_file.with_suffix(".json.tmp")
        temp.write_text(json.dumps({"plugins": self._states}, ensure_ascii=False,
                                   indent=2), encoding="utf-8")
        temp.replace(self.state_file)

    def list_plugins(self) -> list[dict]:
        with self._lock:
            found = []
            for folder in sorted(self.directory.iterdir()):
                plugin_id = folder.name
                if not PLUGIN_ID.fullmatch(plugin_id):
                    continue
                state = self._state(plugin_id)
                info = {
                    "id": plugin_id,
                    "name": plugin_id,
                    "version": "",
                    "description": "",
                    "hooks": [],
                    "requires": [],
                    "api": False,
                    "ui": "",
                    "enabled": state.get("enabled") is True,
                    "order": (state.get("order", 0)
                              if type(state.get("order", 0)) is int else 0),
                    "config": (state.get("config", {})
                               if isinstance(state.get("config", {}), dict) else {}),
                    "error": "",
                }
                try:
                    manifest = self._manifest(plugin_id)
                    info.update({key: manifest.get(key, "") for key in
                                 ("name", "version", "description", "hooks")})
                    info["requires"] = manifest.get("requires", [])
                    info["api"] = manifest.get("api", False)
                    info["ui"] = manifest.get("ui", "")
                    if "config" not in state:
                        info["config"] = manifest.get("default_config", {})
                    if info["enabled"] and plugin_id not in self._errors:
                        self._activate(plugin_id, info["config"])
                except Exception as exc:
                    info["error"] = str(exc)
                info["error"] = info["error"] or self._errors.get(plugin_id, "")
                found.append(info)
            return sorted(found, key=lambda item: (item["order"], item["id"]))

    def configure(self, plugin_id: str, data: dict) -> None:
        with self._lock:
            if not isinstance(data, dict) or set(data) - {"enabled", "order", "config"}:
                raise PluginError("插件配置字段不正确")
            previous = self._state(plugin_id)
            try:
                manifest = self._manifest(plugin_id)
            except PluginError:
                if data.get("enabled") is False and previous.get("enabled") is True:
                    self._states[plugin_id] = {**previous, "enabled": False}
                    self._errors.pop(plugin_id, None)
                    self._deactivate(plugin_id)
                    self._save()
                    return
                raise
            enabled = data.get("enabled", previous.get("enabled", False))
            order = data.get("order", previous.get("order", 0))
            default_config = previous.get("config", manifest.get("default_config", {}))
            config = data.get("config", default_config)
            if type(enabled) is not bool or type(order) is not int or not -1000 <= order <= 1000:
                raise PluginError("启用状态或顺序格式不正确")
            if not isinstance(config, dict):
                raise PluginError("插件配置必须是 JSON 对象")
            try:
                if len(json.dumps(config, ensure_ascii=False)) > 65536:
                    raise PluginError("插件配置过长")
            except (TypeError, ValueError) as exc:
                raise PluginError("插件配置必须是可保存的 JSON 对象") from exc
            if enabled:
                for dependency in manifest.get("requires", []):
                    self._manifest(dependency)
                    if not self._state(dependency).get("enabled"):
                        raise PluginError(f"请先启用依赖插件 {dependency}")
            else:
                for info in self.list_plugins():
                    if info["enabled"] and plugin_id in info["requires"]:
                        raise PluginError(f"请先停用依赖此插件的 {info['id']}")
            self._errors.pop(plugin_id, None)
            if enabled:
                self._activate(plugin_id, config)
            else:
                self._deactivate(plugin_id)
            self._states[plugin_id] = {
                "enabled": enabled, "order": order, "config": config}
            self._save()

    def reload(self) -> list[dict]:
        with self._lock:
            for plugin_id in reversed(tuple(self._active)):
                self._deactivate(plugin_id)
            for module in self._loaded.values():
                prefix = vars(module)["__name__"]
                for name in tuple(sys.modules):
                    if name == prefix or name.startswith(prefix + "."):
                        sys.modules.pop(name, None)
            self._loaded.clear()
            self._errors.clear()
            return self.list_plugins()

    def run(self, hook: str, data: dict) -> dict:
        if hook not in HOOKS:
            raise PluginError(f"未知的插件扩展点：{hook}")
        with self._lock:
            current = data
            available = {info["id"]: info for info in self.list_plugins()}
            ordered = []
            visited = set()

            def visit(plugin_id: str) -> None:
                if plugin_id in visited:
                    return
                visited.add(plugin_id)
                for dependency in available[plugin_id]["requires"]:
                    if dependency in available:
                        visit(dependency)
                ordered.append(available[plugin_id])

            for item in available.values():
                visit(item["id"])
            for info in ordered:
                if not info["enabled"] or hook not in info["hooks"] or info["error"]:
                    continue
                plugin_id = info["id"]
                if any(dependency not in available or not available[dependency]["enabled"]
                       or available[dependency]["error"] or dependency in self._errors
                       for dependency in info["requires"]):
                    self._errors[plugin_id] = "依赖插件不可用，请修复后重新扫描"
                    continue
                try:
                    result = self._load(plugin_id)(
                        hook, copy.deepcopy(current), copy.deepcopy(info["config"]))
                    self._validate(hook, result, current)
                    current = result
                    self._errors.pop(plugin_id, None)
                except Exception as exc:
                    self._errors[plugin_id] = f"{hook} 执行失败：{exc}"
                    print(f"[plugins] {plugin_id}: {self._errors[plugin_id]}", flush=True)
            return current

    def call_api(self, plugin_id: str, action: str, payload: dict) -> dict:
        if not ASSET_NAME.fullmatch(action) or not isinstance(payload, dict):
            raise PluginError("插件请求格式不正确")
        with self._lock:
            manifest = self._manifest(plugin_id)
            if not manifest.get("api") or not self._state(plugin_id).get("enabled"):
                raise PluginError("插件接口未启用")
            if plugin_id in self._errors:
                raise PluginError("插件出错，请重新扫描后重试")
            try:
                config = self._state(plugin_id).get(
                    "config", manifest.get("default_config", {}))
                self._activate(plugin_id, config)
                module = self._loaded[plugin_id]
                handler = vars(module).get("handle_api")
                if not callable(handler):
                    raise PluginError("main.py 必须提供 handle_api(action, payload, config)")
                result = handler(action, copy.deepcopy(payload), copy.deepcopy(config))
                if not isinstance(result, dict):
                    raise PluginError("插件接口必须返回 JSON 对象")
                json.dumps(result)
                return result
            except Exception as exc:
                self._errors[plugin_id] = f"API 执行失败：{exc}"
                print(f"[plugins] {plugin_id}: {self._errors[plugin_id]}", flush=True)
                raise PluginError(self._errors[plugin_id]) from exc

    def asset(self, plugin_id: str, name: str) -> Path:
        if not ASSET_NAME.fullmatch(name):
            raise PluginError("资源文件名不正确")
        with self._lock:
            self._manifest(plugin_id)
            if not self._state(plugin_id).get("enabled"):
                raise PluginError("插件尚未启用")
            folder = self.directory / plugin_id / "assets"
            file = folder / name
            if folder.is_symlink() or file.is_symlink() or not file.is_file():
                raise PluginError("插件资源不存在")
            return file

    @staticmethod
    def _validate(hook: str, result: dict, previous: dict) -> None:
        fields = {
            "before_prompt": {"user_input": str, "history_text": str,
                              "character_info": str, "specific": bool,
                              "custom_system": str},
            "after_prompt": {"prompt": str, "description": str},
            "before_workflow": {"workflow": dict},
            "after_generate": {"image": str, "description": str},
        }[hook]
        if (not isinstance(result, dict)
                or any(not isinstance(result.get(name), expected)
                       for name, expected in fields.items())
                or (hook == "before_prompt" and not result["user_input"].strip())
                or (hook == "after_prompt" and not result["prompt"].strip())
                or (hook == "after_generate" and not result["image"].strip())):
            raise PluginError("返回的插件数据格式不正确")
        if hook == "before_workflow":
            workflow = result["workflow"]
            if (not workflow or any(not isinstance(node_id, str)
                                    or not isinstance(node, dict)
                                    or not isinstance(node.get("class_type"), str)
                                    or not isinstance(node.get("inputs"), dict)
                                    for node_id, node in workflow.items())):
                raise PluginError("插件工作流节点格式不正确")
            try:
                json.dumps(workflow, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise PluginError("插件工作流不是有效的 JSON") from exc
        if hook == "after_generate" and result["image"] != previous["image"]:
            image = result["image"]
            if not image.startswith(("data:image/", "http://", "https://")):
                raise PluginError("插件图片必须是 data URL 或 HTTP(S) 地址")
            if image.startswith("data:image/"):
                header, separator, content = image.partition(",")
                if not separator or not header.endswith(";base64") or not content:
                    raise PluginError("插件图片 data URL 格式不正确")
                try:
                    base64.b64decode(content, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise PluginError("插件图片不是有效的 base64 数据") from exc


plugins = PluginManager()
