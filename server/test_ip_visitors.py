import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from app import _resolve_character, _save_data_url, app
from llm import prompts
from llm.client import GeneratedPrompt, LLMClient
from settings import Settings
from store import Store


IP_A = "192.0.2.10"
IP_B = "192.0.2.20"
IMAGE = "data:image/png;base64,aGVsbG8="


class VisitorIsolationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("settings.DATA_DIR", self.path))
        stack.enter_context(patch("settings.SETTINGS_FILE", self.path / "settings.json"))
        self.settings = Settings()
        self.settings.update({
            "access_password": "", "use_character_db": False,
            "workflow_controls": {}, "keep_outputs": 100,
        })
        self.store = Store(self.path / "sessions.db")
        stack.enter_context(patch("app.settings", self.settings))
        stack.enter_context(patch("app.store", self.store))
        stack.enter_context(patch("app.OUTPUT_DIR", self.path))
        stack.enter_context(patch("app.engine.status", return_value={}))
        stack.enter_context(patch("app._resolve_workflow_path", return_value="workflow.json"))
        stack.enter_context(patch("app.plugins.run", side_effect=lambda hook, data: data))
        self.llm = Mock(spec=LLMClient)
        self.llm.generate_prompt.return_value = GeneratedPrompt("cat, sunny day", "阳光下的猫")
        self.make_llm = stack.enter_context(patch("app._make_llm", return_value=self.llm))
        self.generate = stack.enter_context(patch(
            "app.engine.generate", return_value={"image": IMAGE, "elapsed": 1}))
        self.client = app.test_client()

    def request(self, path, ip=IP_A, method="GET", **kwargs):
        response = self.client.open(
            path, method=method, environ_overrides={"REMOTE_ADDR": ip},
            buffered=True, **kwargs)
        self.addCleanup(response.close)
        return response

    def generation(self, ip=IP_A, **payload):
        response = self.request("/api/generate", ip, "POST", json={
            "prompt": "一只猫", "use_search": False, "use_history": True, **payload})
        return [json.loads(line.removeprefix("data: "))
                for line in response.get_data(as_text=True).splitlines()
                if line.startswith("data: ")]

    def test_session_mutations_and_history_are_isolated(self):
        session = self.request("/api/sessions", method="POST",
                               json={"title": "A 的会话", "owner_ip": IP_B}).json["session"]["id"]
        message_id = self.store.add_message(
            session, "assistant", content="cat", image="/outputs/example.png",
            owner_ip=IP_A, meta={"character_ids": ["miku"]})
        self.assertEqual(len(self.request("/api/sessions").json["sessions"]), 1)
        self.assertEqual(self.request("/api/sessions", IP_B).json["sessions"], [])
        paths = (
            (f"/api/sessions/{session}", "GET", {}),
            (f"/api/sessions/{session}", "DELETE", {}),
            (f"/api/sessions/{session}", "PATCH", {"json": {"title": "changed"}}),
            (f"/api/sessions/{session}/clear", "POST", {}),
            (f"/api/messages/{message_id}", "DELETE", {}),
            ("/api/characters/resolve", "POST",
             {"json": {"prompt": "她换红色外套", "session_id": session}}),
        )
        for path, method, kwargs in paths:
            with self.subTest(path=path, method=method):
                self.assertEqual(self.request(path, IP_B, method, **kwargs).status_code, 404)
        self.assertIsNone(self.store.get_message(session, message_id, IP_B))
        self.assertEqual(self.store.list_messages(session, owner_ip=IP_B), [])
        self.assertEqual(self.store.context_pairs(session, 2, IP_B), [])
        self.assertEqual(self.store.last_assistant_character_ids(session, IP_B), [])
        self.assertFalse(self.store.delete_message(message_id, IP_B))
        self.store.rename_session(session, "changed", IP_B)
        self.store.clear_messages(session, IP_B)
        self.store.delete_session(session, IP_B)
        with self.assertRaises(ValueError):
            self.store.ensure_session(session, IP_B)
        with self.assertRaises(ValueError):
            self.store.add_message(session, "user", "forbidden", owner_ip=IP_B)
        self.assertEqual(self.store.list_sessions(owner_ip=IP_A)[0]["title"], "A 的会话")
        self.assertEqual(len(self.store.list_messages(session, owner_ip=IP_A)), 1)
        self.assertEqual(self.request(f"/api/messages/{message_id}", method="DELETE").status_code, 200)
        self.assertEqual(self.store.list_messages(session, owner_ip=IP_A), [])

    def test_foreign_session_cannot_generate_or_regenerate(self):
        result = self.generation()[-1]
        self.assertEqual(result["step"], "done")
        session = result["session_id"]
        source = self.store.list_messages(session, owner_ip=IP_A)[-1]
        before = self.store.list_messages(session, owner_ip=IP_A)
        self.generate.reset_mock()
        self.make_llm.reset_mock()
        for payload in ({"session_id": session},
                        {"session_id": session, "regenerate_message_id": source["id"]}):
            with self.subTest(payload=payload):
                self.assertEqual(self.generation(IP_B, **payload)[-1]["step"], "error")
                self.generate.assert_not_called()
                self.make_llm.assert_not_called()
        self.assertEqual(self.store.list_messages(session, owner_ip=IP_A), before)
        self.assertEqual(self.generation(session_id=session)[-1]["step"], "done")

    def test_role_inheritance_never_reads_another_ip(self):
        session = self.store.create_session(owner_ip=IP_A)["id"]
        self.store.add_message(
            session, "assistant", "cat", owner_ip=IP_A, meta={"character_ids": ["miku"]})
        character = {"character": "miku", "name": "Miku", "copyright_name": "Vocaloid"}
        self.llm.extract_tags.return_value = "无"
        with (patch("app.direct_candidates", return_value=[]),
              patch("app.get_character", return_value=character) as lookup):
            result = _resolve_character("她换红色衣服", session, True, IP_B)
            self.assertEqual(result["status"], "not_found")
            lookup.assert_not_called()
            result = _resolve_character("她换红色衣服", session, True, IP_A)
            self.assertEqual(result["source"], "history")
            self.assertEqual(result["candidates"][0]["character"], "miku")

    def test_preferences_persist_per_ip_and_connections_remain_shared(self):
        self.assertFalse(self.request("/api/bootstrap").json["preferences"]["nsfw_enabled"])
        controls = {"workflow.json": {"extra_loras": [{"name": "a.safetensors"}]}}
        response = self.request("/api/preferences", method="POST", json={
            "nsfw_enabled": True, "workflow_controls": controls, "owner_ip": IP_B})
        self.assertEqual(response.status_code, 200)
        self.request("/api/preferences", method="POST", json={"nsfw_enabled": False})
        self.assertEqual(self.request("/api/bootstrap").json["settings"]["workflow_controls"], controls)
        self.assertEqual(self.request("/api/bootstrap", IP_B).json["settings"]["workflow_controls"], {})
        self.assertFalse(self.request("/api/preferences", IP_B).json["preferences"]["nsfw_enabled"])
        reopened = Store(self.path / "sessions.db")
        self.assertEqual(reopened.get_preferences(IP_A)["workflow_controls"], controls)
        self.assertEqual(reopened.get_preferences(IP_B)["workflow_controls"], {})
        self.settings.update({"llm_model": "shared-model"})
        self.assertEqual(self.request("/api/bootstrap", IP_B).json["settings"]["llm_model"], "shared-model")
        for data in ([], {"nsfw_enabled": "false"}, {"workflow_controls": []},
                     {"workflow_controls": {"workflow.json": []}}):
            with self.subTest(data=data):
                self.assertEqual(self.request("/api/preferences", method="POST", json=data).status_code, 400)

    def test_workflow_generation_uses_only_own_saved_controls(self):
        controls_a = {"extra_loras": [{"name": "a.safetensors"}]}
        controls_b = {"extra_loras": [{"name": "b.safetensors"}]}
        for ip, controls in ((IP_A, controls_a), (IP_B, controls_b)):
            self.store.update_preferences(ip, {"workflow_controls": {"workflow.json": controls}})
        for ip, controls in ((IP_A, controls_a), (IP_B, controls_b)):
            with self.subTest(ip=ip):
                self.assertEqual(self.generation(ip)[-1]["step"], "done")
                self.assertEqual(self.generate.call_args.args[0]["workflow_controls"], controls)

    def test_ip_normalization_and_forwarded_headers(self):
        session = self.request("/api/sessions", ip="127.0.0.1", method="POST",
                               json={}).json["session"]["id"]
        bootstrap = self.request("/api/bootstrap", "::1").json
        self.assertEqual(bootstrap["client_ip"], "127.0.0.1")
        self.assertEqual(bootstrap["sessions"][0]["id"], session)
        session = self.request("/api/sessions", method="POST", json={}).json["session"]["id"]
        self.assertEqual(self.request("/api/sessions", "::ffff:192.0.2.10").json["sessions"][0]["id"],
                         session)
        response = self.request("/api/bootstrap", IP_B,
                                headers={"X-Forwarded-For": IP_A, "Forwarded": f"for={IP_A}"})
        self.assertEqual(response.json["client_ip"], IP_B)
        self.assertEqual(response.json["sessions"], [])
        self.assertEqual(self.request("/api/bootstrap", "invalid").status_code, 400)

    def test_nsfw_metadata_history_filter_and_regeneration(self):
        session = self.store.create_session(owner_ip=IP_A)["id"]
        for user, prompt, enabled in (("白猫", "white cat", False), ("成人主题", "adult context", True)):
            self.store.add_message(session, "user", user, owner_ip=IP_A)
            self.store.add_message(session, "assistant", prompt, owner_ip=IP_A,
                                   meta={"nsfw_enabled": enabled})
        self.assertEqual(self.store.context_pairs(session, 2, IP_A, include_nsfw=False),
                         [("白猫", "white cat")])
        self.assertEqual(self.generation(session_id=session)[-1]["step"], "done")
        arguments = self.llm.generate_prompt.call_args.kwargs
        self.assertFalse(arguments["nsfw_enabled"])
        self.assertNotIn("adult context", arguments["history_text"])
        self.assertIn("white cat", arguments["history_text"])
        self.store.update_preferences(IP_A, {"nsfw_enabled": True})
        result = self.generation(session_id=session)[-1]
        self.assertTrue(self.llm.generate_prompt.call_args.kwargs["nsfw_enabled"])
        self.assertTrue(result["nsfw_enabled"])
        messages = self.store.list_messages(session, owner_ip=IP_A)
        self.assertTrue(messages[-1]["meta"]["nsfw_enabled"])
        self.assertTrue(messages[-2]["meta"]["nsfw_enabled"])
        source = messages[-1]["id"]
        self.make_llm.reset_mock()
        regenerated = self.generation(session_id=session, regenerate_message_id=source)[-1]
        self.assertEqual(regenerated["step"], "done")
        self.assertTrue(regenerated["nsfw_enabled"])
        self.make_llm.assert_not_called()
        self.store.update_preferences(IP_A, {"nsfw_enabled": False})
        self.generate.reset_mock()
        events = self.generation(session_id=session, regenerate_message_id=source)
        self.assertEqual(events[-1]["step"], "error")
        self.assertIn("NSFW", events[-1]["error"])
        self.generate.assert_not_called()
        self.assertEqual(self.generation(IP_B)[-1]["step"], "done")
        self.assertFalse(self.llm.generate_prompt.call_args.kwargs["nsfw_enabled"])

    def test_image_ownership_cache_and_legacy_compatibility(self):
        image = self.generation()[-1]["image"]
        self.assertEqual(self.request(image).get_data(), b"hello")
        response = self.request(image, IP_B)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.request(image, IP_B, headers={"X-Forwarded-For": IP_A}).status_code, 404)
        legacy = self.path / "gen_legacy.png"
        legacy.write_bytes(b"legacy")
        self.assertEqual(self.request("/outputs/gen_legacy.png", "127.0.0.1").status_code, 200)
        self.assertEqual(self.request("/outputs/gen_legacy.png", IP_A).status_code, 404)
        self.assertTrue(legacy.exists())
        self.assertEqual(self.request("/outputs/../sessions.db").status_code, 404)

    def test_pruning_is_scoped_to_owner(self):
        self.settings.update({"keep_outputs": 2})
        outputs_b = [_save_data_url(IMAGE, owner_ip=IP_B) for _ in range(2)]
        for _ in range(4):
            _save_data_url(IMAGE, owner_ip=IP_A)
        for url in outputs_b:
            self.assertEqual(self.request(url, IP_B).status_code, 200)
        self.assertEqual(len(list(self.path.glob("*.png"))), 4)

    def test_request_modes_validate_and_preserve_regeneration_source(self):
        self.assertEqual(self.generation(nsfw_enabled="false")[-1]["step"], "error")
        self.make_llm.assert_not_called()
        self.generate.assert_not_called()
        result = self.generation(nsfw_enabled=False)[-1]
        source = self.store.list_messages(result["session_id"], owner_ip=IP_A)[-1]
        self.store.update_preferences(IP_A, {"nsfw_enabled": True})
        self.make_llm.reset_mock()
        result = self.generation(
            session_id=result["session_id"], regenerate_message_id=source["id"])[-1]
        self.assertEqual(result["step"], "done")
        self.assertFalse(result["nsfw_enabled"])
        self.assertFalse(self.store.list_messages(result["session_id"], owner_ip=IP_A)[-1]
                         ["meta"]["nsfw_enabled"])
        self.make_llm.assert_not_called()

    def test_access_password_allows_own_images_after_login(self):
        image = _save_data_url(IMAGE, owner_ip=IP_A)
        self.settings.update({"access_password": "test-password"})
        self.assertEqual(self.request("/").status_code, 200)
        self.assertEqual(self.request(image).status_code, 401)
        response = self.request("/api/auth", method="POST", json={"password": "test-password"})
        self.assertTrue(response.json["success"])
        cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(self.request(image).status_code, 200)
        self.assertEqual(self.request(image, IP_B).status_code, 404)
        self.assertEqual(self.request("/api/preferences", method="POST",
                                     headers={"Origin": "https://other.example"},
                                     json={"nsfw_enabled": True}).status_code, 403)
        self.assertEqual(self.request("/api/preferences", method="POST",
                                     headers={"Origin": "http://localhost"},
                                     json={"nsfw_enabled": True}).status_code, 200)
        self.settings.update({"access_password": "new-password"})
        self.assertEqual(self.request(image).status_code, 401)

    def test_setting_access_password_keeps_own_images_accessible(self):
        image = _save_data_url(IMAGE, owner_ip=IP_A)
        for password in ("first-password", "new-password"):
            response = self.request("/api/settings", method="POST",
                                    json={"access_password": password})
            self.assertTrue(response.json["success"])
            self.assertIn("mz_access=", response.headers["Set-Cookie"])
            self.assertEqual(self.request(image).status_code, 200)
            self.assertEqual(self.request(image, IP_B).status_code, 404)


class LegacyMigrationTest(unittest.TestCase):
    def test_old_sessions_are_preserved_for_localhost(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.db"
            with sqlite3.connect(path) as connection:
                connection.executescript("""
                    CREATE TABLE sessions (
                        id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL, updated_at REAL NOT NULL);
                    CREATE TABLE messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                        role TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
                        image TEXT NOT NULL DEFAULT '', character TEXT NOT NULL DEFAULT '',
                        vlm TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL);
                    INSERT INTO sessions VALUES ('legacy', '旧会话', 1, 1);
                    INSERT INTO messages(session_id, role, content, image, created_at)
                        VALUES ('legacy', 'assistant', 'cat', '/outputs/old.png', 1);
                """)
            store = Store(path)
            self.assertEqual(store.list_sessions()[0]["title"], "旧会话")
            self.assertEqual(store.list_messages("legacy")[0]["image"], "/outputs/old.png")
            self.assertEqual(store.list_sessions(owner_ip=IP_A), [])
            self.assertFalse(store.get_preferences()["nsfw_enabled"])
            reopened = Store(path)
            self.assertEqual(len(reopened.list_messages("legacy")), 1)


class NsfwPromptTest(unittest.TestCase):
    def test_mode_rules_apply_to_all_prompt_templates(self):
        client = LLMClient({"base_url": "http://localhost/v1", "api_key": "", "model": "test"})
        for context, specific, custom in (("", False, ""), ("", True, ""),
                                          ("role data", False, ""), ("role data", True, ""),
                                          ("", False, "custom rules")):
            for enabled in (False, True):
                with self.subTest(context=context, specific=specific, custom=custom, enabled=enabled):
                    with patch.object(client, "call", return_value="cat") as call:
                        client.generate_prompt(
                            "猫", context=context, specific=specific, custom_system=custom,
                            include_description=True, nsfw_enabled=enabled)
                    system = call.call_args.args[0]
                    self.assertIn(prompts.NSFW_ENABLED if enabled else prompts.NSFW_DISABLED, system)
                    self.assertIn(prompts.PROMPT_WITH_DESCRIPTION_OUTPUT, system)
                    if custom:
                        self.assertIn(custom, system)


if __name__ == "__main__":
    unittest.main()
