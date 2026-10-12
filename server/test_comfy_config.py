import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import app
from backends import ComfyBackend
from engine import Engine
from settings import Settings


class ComfyConfigTest(unittest.TestCase):
    def test_workflow_discovery_scans_all_roots_and_classifies_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / "bridge" / "workflows"
            custom = root / "exported"
            project = root / "ComfyUI"
            paths = [
                local / "nested" / "image.JSON",
                custom / "saved.json",
                custom / "sibling.json",
                project / "workflows" / "wrapped.json",
                project / "user" / "default" / "workflows" / "canvas.json",
                project / "user" / "another" / "workflows" / "nested" / "second.json",
            ]
            workflow = {"1": {"class_type": "SaveImage", "inputs": {}}}
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                data = ({"nodes": [], "links": []} if path.name == "canvas.json"
                        else {"prompt": workflow} if path.name == "wrapped.json"
                        else workflow)
                path.write_text(json.dumps(data), encoding="utf-8")
            (custom / "broken.json").write_text("{broken", encoding="utf-8")
            (custom / "settings.json").write_text('{"theme": "dark"}', encoding="utf-8")
            (custom / "notes.txt").write_text("not a workflow", encoding="utf-8")
            backend = ComfyBackend({
                "workflow_path": str(custom / "saved.json"),
                "comfy_project": str(project),
                "workflow_dirs": [str(custom), str(local), str(custom / "missing")],
            })
            with patch("backends.DEFAULT_WORKFLOW_DIR", local):
                result = backend.list_workflows(str(custom / "sibling.json"))
            items = {item["path"]: item for item in result["workflows"]}
            self.assertEqual(len(result["workflows"]), 8)
            self.assertEqual(len(items), 8)
            for path in paths:
                self.assertEqual(items[str(path)]["format"],
                                 "canvas" if path.name == "canvas.json" else "api")
            self.assertEqual(items[str(paths[0])]["name"], "nested/image.JSON")
            self.assertEqual(items[str(custom / "broken.json")]["format"], "invalid")
            self.assertEqual(items[str(custom / "settings.json")]["format"], "invalid")
            self.assertEqual(result["dir"].split(" | ").count(str(custom)), 1)

    def test_workflow_endpoint_refreshes_without_starting_mcp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "ComfyUI"
            workflows = project / "user" / "default" / "workflows"
            workflows.mkdir(parents=True)
            first = workflows / "first.json"
            first.write_text('{"1": {"class_type": "SaveImage", "inputs": {}}}',
                             encoding="utf-8")
            cfg = {"mcp_enabled": True, "comfy_project": str(project)}
            with (patch("engine.settings", cfg),
                  patch("backends.DEFAULT_WORKFLOW_DIR", root / "missing")):
                engine = Engine()
                with (patch("app.engine", engine),
                      patch.object(engine, "ensure_mcp") as ensure_mcp,
                      patch("settings.Settings.access_password", property(lambda self: ""))):
                    client = app.test_client()
                    response = client.get("/api/workflows")
                    self.assertTrue(response.json["success"])
                    self.assertEqual([item["path"] for item in response.json["workflows"]],
                                     [str(first)])
                    second = workflows / "second.json"
                    second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
                    response = client.get("/api/workflows")
                    self.assertEqual(len(response.json["workflows"]), 2)
                    extra = root / "exports"
                    extra.mkdir()
                    exported = extra / "third.json"
                    exported.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
                    response = client.get("/api/workflows",
                                          query_string={"path": str(exported)})
                    self.assertEqual(len(response.json["workflows"]), 3)
                    ensure_mcp.assert_not_called()

    def test_connection_test_uses_saved_url_without_mcp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with (patch("settings.DATA_DIR", path),
                  patch("settings.SETTINGS_FILE", path / "settings.json")):
                settings = Settings()
                settings.update({"mcp_enabled": False,
                                 "comfyui_url": "http://127.0.0.1:8188"})
                session = Mock()
                session.get.return_value.ok = True
                session.get.return_value.json.return_value = {"system": "ready"}
                with (patch("engine.settings", settings),
                      patch("engine.local_session", return_value=session),
                      patch("backends.local_session", return_value=session)):
                    engine = Engine()
                    with patch("app.engine", engine):
                        response = app.test_client().post("/api/test/comfyui", json={})
                        self.assertTrue(response.json["success"])
                        self.assertEqual(response.json["source"], "http")
                        self.assertEqual(session.get.call_args.args[0],
                                         "http://127.0.0.1:8188/system_stats")

                        settings.update({"comfyui_url": "http://127.0.0.1:8288"})
                        response = app.test_client().post("/api/test/comfyui", json={})
                        self.assertTrue(response.json["success"])
                        self.assertEqual(session.get.call_args.args[0],
                                         "http://127.0.0.1:8288/system_stats")

    def test_generation_uses_current_url_and_save_node(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with (patch("settings.DATA_DIR", path),
                  patch("settings.SETTINGS_FILE", path / "settings.json")):
                settings = Settings()
                settings.update({"mcp_enabled": False,
                                 "comfyui_url": "http://127.0.0.1:8188",
                                 "save_node_id": "42"})
                with (patch("engine.settings", settings),
                      patch.object(ComfyBackend, "generate", autospec=True,
                                   side_effect=lambda backend, job: {
                                       "url": backend.http_url,
                                       "save_node_id": backend.save_node_id,
                                   })):
                    engine = Engine()
                    self.assertEqual(engine.generate({}), {
                        "url": "http://127.0.0.1:8188",
                        "save_node_id": "42",
                    })
                    settings.update({"comfyui_url": "http://127.0.0.1:8288",
                                     "save_node_id": "66"})
                    self.assertEqual(engine.generate({}), {
                        "url": "http://127.0.0.1:8288",
                        "save_node_id": "66",
                    })


if __name__ == "__main__":
    unittest.main()
