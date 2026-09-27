import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import _llm_config_for_test, _make_llm, app
from llm.client import LLMClient
from settings import Settings


class LLMConfigTest(unittest.TestCase):
    def test_active_local_config_migrates_and_stays_after_save(self):
        for mode in ("local", "bridge"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                file = path / "settings.json"
                file.write_text(json.dumps({
                    "llm_mode": mode,
                    "llm_base_url": "https://example.com/v1",
                    "llm_api_key": "remote-key",
                    "llm_model": "remote-model",
                    "local_llm_base_url": "http://127.0.0.1:8080/v1",
                    "local_llm_api_key": "local-key",
                    "local_llm_model": "local-model",
                }), encoding="utf-8")
                with (patch("settings.DATA_DIR", path),
                      patch("settings.SETTINGS_FILE", file)):
                    settings = Settings()
                    with patch("app.settings", settings):
                        client = _make_llm()
                    self.assertEqual(client.base_url, "http://127.0.0.1:8080/v1")
                    self.assertEqual(client.api_key, "local-key")
                    self.assertEqual(client.model, "local-model")
                    settings.update({"llm_model": "new-model"})
                    self.assertEqual(Settings().get("llm_model"), "new-model")
                    self.assertNotIn("llm_mode", json.loads(file.read_text()))
                    self.assertNotIn("local_llm_model", json.loads(file.read_text()))

    def test_active_remote_config_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            file = path / "settings.json"
            file.write_text(json.dumps({
                "llm_mode": "direct",
                "llm_base_url": "https://example.com/v1",
                "llm_api_key": "remote-key",
                "llm_model": "remote-model",
                "local_llm_model": "local-model",
            }), encoding="utf-8")
            with (patch("settings.DATA_DIR", path),
                  patch("settings.SETTINGS_FILE", file)):
                settings = Settings()
                self.assertEqual(settings.get("llm_base_url"), "https://example.com/v1")
                self.assertEqual(settings.get("llm_api_key"), "remote-key")
                self.assertEqual(settings.get("llm_model"), "remote-model")

    def test_local_api_bypasses_proxy_and_remote_api_uses_normal_session(self):
        for url, local in (
            ("http://127.0.0.1:8080/v1", True),
            ("http://192.168.1.10:1234/v1", True),
            ("https://example.com/v1", False),
        ):
            with self.subTest(url=url):
                response = Mock(ok=True, status_code=200)
                response.json.return_value = {
                    "choices": [{"message": {"content": "connected"}}],
                }
                with (patch("llm.client.local_session") as local_session,
                      patch("llm.client.requests.post", return_value=response) as remote_post):
                    local_session.return_value.post.return_value = response
                    client = LLMClient({
                        "base_url": url, "api_key": "test-key", "model": "test-model",
                    })
                    self.assertEqual(client.chat([{"role": "user", "content": "ping"}]),
                                     "connected")
                    post = local_session.return_value.post if local else remote_post
                    self.assertEqual(post.call_args.args[0], url + "/chat/completions")
                    self.assertEqual(post.call_args.kwargs["json"]["model"], "test-model")
                    self.assertEqual(post.call_args.kwargs["headers"]["Authorization"],
                                     "Bearer test-key")
                    if local:
                        remote_post.assert_not_called()
                    else:
                        local_session.assert_not_called()

    def test_connection_test_uses_unsaved_fields_including_empty_key(self):
        with (patch("app.settings.get", side_effect={
                  "llm_base_url": "https://saved.example/v1",
                  "llm_api_key": "saved-key",
                  "llm_model": "saved-model",
              }.get),
              patch("llm.client.local_session") as local_session):
            client = _llm_config_for_test({
                "llm_base_url": "http://localhost:8080/v1",
                "llm_api_key": "",
                "llm_model": "current-model",
            })
        self.assertEqual(client.base_url, "http://localhost:8080/v1")
        self.assertEqual(client.api_key, "")
        self.assertEqual(client.model, "current-model")
        local_session.assert_called_once()

    def test_connection_endpoint_uses_the_single_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with (patch("settings.DATA_DIR", path),
                  patch("settings.SETTINGS_FILE", path / "settings.json")):
                settings = Settings()
                with (patch("app.settings", settings),
                      patch("llm.client.LLMClient.call",
                            return_value="连接正常") as call):
                    response = app.test_client().post("/api/test/llm", json={
                        "llm_base_url": "http://localhost:8080/v1",
                        "llm_api_key": "",
                        "llm_model": "local-model",
                    })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"success": True, "output": "连接正常"})
        call.assert_called_once()


if __name__ == "__main__":
    unittest.main()
