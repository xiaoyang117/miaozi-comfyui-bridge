import copy
import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from app import app
from backends import ComfyBackend
from llm.client import GeneratedPrompt
from plugins import PluginError, PluginManager
from settings import Settings
from store import Store
from test_workflow_controls import example_workflow


class PluginFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.manager = PluginManager(self.root / "plugins", self.root / "state.json")

    def install(self, plugin_id, hooks, code, **extra):
        folder = self.manager.directory / plugin_id
        folder.mkdir()
        manifest = {
            "api_version": 1, "id": plugin_id,
            "name": plugin_id, "version": "1.0", "hooks": hooks, **extra,
        }
        (folder / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        (folder / "main.py").write_text(code, encoding="utf-8")
        return folder


class PluginTest(PluginFixture):
    def test_example_is_disabled_until_enabled_and_keeps_config_on_restart(self):
        example = Path(__file__).resolve().parent.parent / "examples/plugins/prompt_suffix"
        shutil.copytree(example, self.manager.directory / "prompt_suffix")
        self.assertFalse(self.manager.list_plugins()[0]["enabled"])
        original = {"prompt": "cat", "description": ""}
        self.assertEqual(self.manager.run("after_prompt", original), original)
        self.manager.configure("prompt_suffix", {
            "enabled": True, "config": {"suffix": "warm light"}})
        self.assertEqual(self.manager.run("after_prompt", original)["prompt"],
                         "cat, warm light")
        self.assertEqual(original["prompt"], "cat")
        restarted = PluginManager(self.manager.directory, self.manager.state_file)
        self.assertEqual(restarted.run("after_prompt", original)["prompt"],
                         "cat, warm light")
        self.assertEqual(restarted.list_plugins()[0]["config"], {"suffix": "warm light"})
        restarted.configure("prompt_suffix", {"enabled": False})
        self.assertEqual(restarted.run("after_prompt", original), original)
        self.assertFalse(PluginManager(self.manager.directory,
                                       self.manager.state_file).list_plugins()[0]["enabled"])

    def test_version_entry_and_symlink_fail_without_startup_failure(self):
        folder = self.install("old", ["after_prompt"],
                              "def transform(hook, data, config):\n    return data\n",
                              api_version=2)
        self.assertIn("版本不兼容", self.manager.list_plugins()[0]["error"])
        with self.assertRaises(PluginError):
            self.manager.configure("old", {"enabled": True})
        manifest = json.loads((folder / "plugin.json").read_text(encoding="utf-8"))
        manifest["api_version"] = 1
        (folder / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        outside = self.root / "outside.py"
        outside.write_text("def transform(hook, data, config): return data",
                           encoding="utf-8")
        (folder / "main.py").unlink()
        (folder / "main.py").symlink_to(outside)
        self.assertIn("普通文件", self.manager.list_plugins()[0]["error"])
        with self.assertRaises(PluginError):
            self.manager.configure("../old", {"enabled": True})

    def test_failure_is_isolated_and_bad_manifest_can_still_be_disabled(self):
        self.install("broken", ["after_prompt"],
                     "def transform(hook, data, config):\n"
                     "    data['prompt'] = 'corrupted'\n    raise ValueError('broken')\n")
        self.install("good", ["after_prompt"],
                     "def transform(hook, data, config):\n"
                     "    data['prompt'] += ', good'\n    return data\n")
        self.manager.configure("broken", {"enabled": True})
        self.manager.configure("good", {"enabled": True})
        original = {"prompt": "cat", "description": ""}
        self.assertEqual(self.manager.run("after_prompt", original)["prompt"], "cat, good")
        self.assertEqual(original["prompt"], "cat")
        self.assertIn("broken", self.manager.list_plugins()[0]["error"])
        broken = self.manager.directory / "broken" / "plugin.json"
        broken.write_text("{bad", encoding="utf-8")
        self.manager.configure("broken", {"enabled": False})
        self.assertFalse(self.manager.list_plugins()[0]["enabled"])
        self.assertEqual(self.manager.run("after_prompt", original)["prompt"], "cat, good")

    def test_invalid_results_cannot_replace_workflow_or_image(self):
        self.install("wrong", ["before_workflow", "after_generate"],
                     "def transform(hook, data, config):\n"
                     "    if hook == 'before_workflow':\n"
                     "        data['workflow']['1']['inputs']['seed'] = 99\n"
                     "    else:\n        data['image'] = 123\n"
                     "    return None if hook == 'before_workflow' else data\n")
        self.manager.configure("wrong", {"enabled": True})
        workflow = {"1": {"inputs": {"seed": 1}}}
        incoming = {"workflow": workflow}
        self.assertEqual(self.manager.run("before_workflow", incoming), incoming)
        self.assertEqual(workflow["1"]["inputs"]["seed"], 1)
        self.manager.reload()
        image = {"image": "data:image/png;base64,AA==", "description": "a"}
        self.assertEqual(self.manager.run("after_generate", image), image)

    def test_malformed_image_data_is_rejected_before_saving(self):
        self.install("broken_image", ["after_generate"],
                     "def transform(hook, data, config):\n"
                     "    data['image'] = 'data:image/png;base64,@@'\n    return data\n")
        self.manager.configure("broken_image", {"enabled": True})
        original = {"image": "data:image/png;base64,aGk=", "description": ""}
        self.assertEqual(self.manager.run("after_generate", original), original)
        self.assertIn("base64", self.manager.list_plugins()[0]["error"])

    def test_dependencies_execute_first_and_block_invalid_disabling(self):
        self.install("consumer", ["after_prompt"],
                     "def transform(hook, data, config):\n"
                     "    data['prompt'] += '2'\n    return data\n",
                     requires=["base"])
        self.install("base", ["after_prompt"],
                     "def transform(hook, data, config):\n"
                     "    data['prompt'] += '1'\n    return data\n")
        with self.assertRaisesRegex(PluginError, "先启用"):
            self.manager.configure("consumer", {"enabled": True})
        self.manager.configure("base", {"enabled": True, "order": 10})
        self.manager.configure("consumer", {"enabled": True, "order": -10})
        self.assertEqual(self.manager.run("after_prompt", {
            "prompt": "cat", "description": ""})["prompt"], "cat12")
        with self.assertRaisesRegex(PluginError, "先停用"):
            self.manager.configure("base", {"enabled": False})
        (self.manager.directory / "base" / "main.py").unlink()
        self.manager.reload()
        original = {"prompt": "cat", "description": ""}
        self.assertEqual(self.manager.run("after_prompt", original), original)
        self.assertIn("依赖插件不可用", self.manager.list_plugins()[0]["error"])

    def test_reload_uses_new_source_and_invalid_config_is_rejected(self):
        folder = self.install("reloadable", ["after_prompt"],
                              "def transform(hook, data, config):\n"
                              "    data['prompt'] += 'A'\n    return data\n")
        self.manager.configure("reloadable", {"enabled": True})
        original = {"prompt": "cat", "description": ""}
        self.assertEqual(self.manager.run("after_prompt", original)["prompt"], "catA")
        (folder / "main.py").write_text(
            "def transform(hook, data, config):\n"
            "    data['prompt'] += 'B'\n    return data\n", encoding="utf-8")
        self.manager.reload()
        self.assertEqual(self.manager.run("after_prompt", original)["prompt"], "catB")
        for value in ({"enabled": 1}, {"config": []}, {"order": 1001}, {"order": 1.5}):
            with self.subTest(value=value), self.assertRaises(PluginError):
                self.manager.configure("reloadable", value)

    def test_plugin_can_import_its_own_helper_module(self):
        folder = self.install("package", ["after_prompt"],
                              "from .helpers import SUFFIX\n"
                              "def transform(hook, data, config):\n"
                              "    data['prompt'] += SUFFIX\n    return data\n")
        (folder / "helpers.py").write_text("SUFFIX = ', imported'\n", encoding="utf-8")
        self.manager.configure("package", {"enabled": True})
        self.assertEqual(self.manager.run("after_prompt", {
            "prompt": "cat", "description": ""})["prompt"], "cat, imported")

    def test_enable_disable_and_reload_call_lifecycle_hooks(self):
        self.install("lifecycle", ["after_prompt"],
                     "events = []\n"
                     "def on_enable(config):\n    events.append('enabled')\n"
                     "def on_disable():\n    events.append('disabled')\n"
                     "def transform(hook, data, config):\n    return data\n")
        self.manager.configure("lifecycle", {"enabled": True})
        module = self.manager._loaded["lifecycle"]
        self.assertEqual(vars(module)["events"], ["enabled"])
        self.manager.list_plugins()
        self.assertEqual(vars(module)["events"], ["enabled"])
        self.manager.configure("lifecycle", {"enabled": False})
        self.assertEqual(vars(module)["events"], ["enabled", "disabled"])
        self.manager.configure("lifecycle", {"enabled": True})
        self.manager.reload()
        self.assertEqual(vars(module)["events"], ["enabled", "disabled", "enabled", "disabled"])
        self.assertEqual(vars(self.manager._loaded["lifecycle"])["events"], ["enabled"])

    def test_dependency_cycle_discovered_on_reload_is_isolated(self):
        first = self.install("first", ["after_prompt"],
                             "def transform(hook, data, config):\n    return data\n")
        self.install("second", ["after_prompt"],
                     "def transform(hook, data, config):\n    return data\n",
                     requires=["first"])
        self.manager.configure("first", {"enabled": True})
        self.manager.configure("second", {"enabled": True})
        manifest = json.loads((first / "plugin.json").read_text(encoding="utf-8"))
        manifest["requires"] = ["second"]
        (first / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertTrue(all("循环" in item["error"] for item in self.manager.reload()))
        original = {"prompt": "cat", "description": ""}
        self.assertEqual(self.manager.run("after_prompt", original), original)

    def test_namespaced_api_assets_and_password_guard(self):
        example = Path(__file__).resolve().parent.parent / "examples/plugins/prompt_suffix"
        shutil.copytree(example, self.manager.directory / "prompt_suffix")
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("app.plugins", self.manager))
        stack.enter_context(patch("settings.DATA_DIR", self.root))
        stack.enter_context(patch("settings.SETTINGS_FILE", self.root / "settings.json"))
        settings = Settings()
        settings.update({"access_password": "secret"})
        stack.enter_context(patch("app.settings", settings))
        client = app.test_client()
        self.assertEqual(client.get("/api/plugins").status_code, 401)
        self.assertEqual(client.put("/api/plugins/prompt_suffix",
                                    json={"enabled": True}).status_code, 401)
        headers = {"X-Access-Password": "secret"}
        self.assertEqual(client.put("/api/plugins/prompt_suffix",
                                    json={"enabled": True}, headers=headers).status_code, 200)
        self.assertEqual(client.get("/api/plugins", headers=headers).json["plugins"][0]["id"],
                         "prompt_suffix")
        self.assertEqual(client.post("/api/plugins/prompt_suffix/call/preview",
                                     json={"prompt": "cat"}).status_code, 401)
        response = client.post("/api/plugins/prompt_suffix/call/preview",
                               json={"prompt": "cat"}, headers=headers)
        self.assertEqual(response.json["prompt"], "cat, cinematic lighting")
        self.assertEqual(client.get("/api/plugins/prompt_suffix/assets/panel.js").status_code,
                         401)
        asset = client.get("/api/plugins/prompt_suffix/assets/panel.js",
                           headers=headers)
        self.assertEqual(asset.status_code, 200)
        self.assertIn(b"miaoziPluginPanels", asset.get_data())
        asset.close()
        self.assertEqual(client.get("/api/plugins/prompt_suffix/assets/../main.py",
                                    headers=headers).status_code, 404)
        self.assertEqual(client.post("/api/plugins/reload", headers=headers).status_code, 200)
        self.assertEqual(client.put("/api/plugins/prompt_suffix",
                                    json={"enabled": False}, headers=headers).status_code, 200)
        self.assertEqual(client.post("/api/plugins/prompt_suffix/call/preview",
                                     json={}, headers=headers).status_code, 400)

    def test_invalid_persisted_state_remains_manageable(self):
        self.install("state", ["after_prompt"],
                     "def transform(hook, data, config):\n    return data\n")
        self.manager.state_file.write_text(json.dumps({"plugins": {
            "state": {"enabled": True, "order": "broken", "config": []}}}), encoding="utf-8")
        manager = PluginManager(self.manager.directory, self.manager.state_file)
        listed = manager.list_plugins()[0]
        self.assertFalse(listed["enabled"])
        self.assertIn("设置格式不正确", listed["error"])
        manager.configure("state", {"enabled": True, "config": {}})
        self.assertFalse(manager.list_plugins()[0]["error"])


class PluginGenerationTest(PluginFixture):
    def test_new_generation_and_regeneration_use_different_hooks(self):
        self.install("hooked", ["before_prompt", "after_prompt", "after_generate"],
                     "def transform(hook, data, config):\n"
                     "    if hook == 'before_prompt':\n"
                     "        data['user_input'] += ' with a hat'\n"
                     "    elif hook == 'after_prompt':\n"
                     "        data['prompt'] += ', watercolor'\n"
                     "    elif hook == 'after_generate':\n"
                     "        data['description'] += ' 插件已处理。'\n"
                     "    return data\n")
        self.manager.configure("hooked", {"enabled": True})
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("app.plugins", self.manager))
        stack.enter_context(patch("settings.DATA_DIR", self.root))
        stack.enter_context(patch("settings.SETTINGS_FILE", self.root / "settings.json"))
        settings = Settings()
        settings.update({"access_password": "", "use_character_db": False,
                         "positive_prompt_prefix": "quality"})
        store = Store(self.root / "sessions.db")
        stack.enter_context(patch("app.settings", settings))
        stack.enter_context(patch("app.store", store))
        stack.enter_context(patch("app._resolve_workflow_path", return_value="w.json"))
        llm = stack.enter_context(patch("app._make_llm"))
        llm.return_value.generate_prompt.return_value = GeneratedPrompt("cat", "画面。")
        engine = stack.enter_context(patch(
            "app.engine.generate", return_value={"image": "image-data", "elapsed": 1}))
        stack.enter_context(patch("app._save_data_url", return_value="/outputs/new.png"))
        client = app.test_client()
        session = store.create_session()["id"]
        response = client.post("/api/generate", json={
            "prompt": "画只猫", "session_id": session, "use_search": False,
            "use_history": False})
        events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines()
                  if line.startswith("data: ")]
        self.assertEqual(events[-1]["step"], "done")
        self.assertEqual(events[-1]["description"], "画面。 插件已处理。")
        self.assertEqual(llm.return_value.generate_prompt.call_args.args[0],
                         "画只猫 with a hat")
        self.assertEqual(engine.call_args.args[0]["prompt"], "quality, cat, watercolor")
        first = store.list_messages(session)[-1]
        response = client.post("/api/generate", json={
            "session_id": session, "regenerate_message_id": first["id"]})
        events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines()
                  if line.startswith("data: ")]
        self.assertEqual(events[-1]["step"], "done")
        self.assertEqual(events[-1]["description"], "画面。 插件已处理。 插件已处理。")
        llm.return_value.generate_prompt.assert_called_once()
        self.assertEqual(engine.call_args.args[0]["prompt"], "quality, cat, watercolor")
        self.assertTrue(engine.call_args.args[0]["randomize_seed"])

    def test_workflow_hook_runs_after_seed_and_built_in_edits(self):
        self.install("workflow", ["before_workflow"],
                     "def transform(hook, data, config):\n"
                     "    data['workflow']['100']['inputs']['batch_size'] = 2\n"
                     "    return data\n")
        self.manager.configure("workflow", {"enabled": True})
        workflow = example_workflow()
        backend = ComfyBackend({"comfyui_url": "http://127.0.0.1:8188"},
                               log=lambda _: None)
        image = self.root / "image.png"
        image.touch()
        with (patch("backends.plugins", self.manager),
              patch.object(backend, "_load_workflow", side_effect=lambda _: copy.deepcopy(workflow)),
              patch.object(backend, "_submit_http", return_value="job") as submit,
              patch.object(backend, "_fetch_via_http", return_value=str(image)),
              patch.object(backend, "_encode_image", return_value="image-data")):
            backend.generate({"workflow_path": "workflow.json", "prompt": "cat",
                              "width": 640, "height": 768, "resize": True})
        submitted = submit.call_args.args[0]
        self.assertEqual(submitted["100"]["inputs"]["width"], 640)
        self.assertEqual(submitted["100"]["inputs"]["batch_size"], 2)
        self.assertNotIn("batch_size", workflow["100"]["inputs"])


if __name__ == "__main__":
    unittest.main()
