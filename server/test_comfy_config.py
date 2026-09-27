import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import app
from backends import ComfyBackend
from engine import Engine
from settings import Settings


class ComfyConfigTest(unittest.TestCase):
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
