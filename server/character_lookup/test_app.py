import json
import unittest
from unittest.mock import Mock, patch

from app import _resolve_character, _run_generation, app


class CharacterFlowTest(unittest.TestCase):
    def test_disabled_setting_skips_lookup(self):
        with (patch("app.settings.get", return_value=False),
              patch("app._resolve_workflow_path", return_value="workflow.json"),
              patch("app.store.ensure_session", return_value="session"),
              patch("app._make_llm"),
              patch("app._resolve_character") as resolve):
            stream = _run_generation({"prompt": "Shiroko", "use_search": True})
            event = json.loads(next(stream).removeprefix("data: "))
            stream.close()
        self.assertEqual(event["step"], "character")
        self.assertEqual(event["message"], "角色库检索已关闭")
        resolve.assert_not_called()

    def test_missing_database_is_visible(self):
        with (patch("app.settings.get", return_value=True),
              patch("app.char_db_built", return_value=False),
              patch("app._resolve_workflow_path", return_value="workflow.json"),
              patch("app.store.ensure_session", return_value="session"),
              patch("app._make_llm")):
            stream = _run_generation({"prompt": "Shiroko"})
            event = json.loads(next(stream).removeprefix("data: "))
            stream.close()
        self.assertIn("角色库未建立", event["message"])

    def test_resolve_endpoint_obeys_global_switch(self):
        with patch("app.settings.get", return_value=False):
            response = app.test_client().post(
                "/api/characters/resolve", json={"prompt": "Shiroko"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "disabled")

    def test_selected_character_is_verified_without_another_llm_lookup(self):
        character = {"character": "shiroko", "name": "Shiroko",
                     "copyright_name": "Blue Archive"}
        with (patch("app.settings.get", return_value=True),
              patch("app.char_db_built", return_value=True),
              patch("app._resolve_workflow_path", return_value="workflow.json"),
              patch("app.store.ensure_session", return_value="session"),
              patch("app._make_llm"),
              patch("app.get_character", return_value=character) as get_character,
              patch("app.format_character", return_value="role context"),
              patch("app._resolve_character") as resolve):
            stream = _run_generation({
                "prompt": "Shiroko", "character_checked": True,
                "character_id": "shiroko",
            })
            self.assertEqual(json.loads(next(stream).removeprefix("data: "))["step"],
                             "search")
            event = json.loads(next(stream).removeprefix("data: "))
            stream.close()
        self.assertIn("Shiroko", event["message"])
        get_character.assert_called_once_with("shiroko")
        resolve.assert_not_called()

    def test_pronoun_reuses_last_character_when_history_is_enabled(self):
        client = Mock()
        client.extract_tags.return_value = "无"
        character = {"character": "shiroko", "name": "Shiroko",
                     "copyright_name": "Blue Archive"}
        with (patch("app.direct_candidates", return_value=[]),
              patch("app._make_llm", return_value=client),
              patch("app.store.last_assistant_character_id",
                    return_value="shiroko") as last_character,
              patch("app.get_character", return_value=character)):
            result = _resolve_character("把她的衣服换成红色", "session", True)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["source"], "history")
        self.assertEqual(result["candidates"][0]["character"], "shiroko")
        last_character.assert_called_once_with("session")

    def test_pronoun_does_not_reuse_character_when_history_is_disabled(self):
        client = Mock()
        client.extract_tags.return_value = "无"
        with (patch("app.direct_candidates", return_value=[]),
              patch("app._make_llm", return_value=client),
              patch("app.store.last_assistant_character_id") as last_character):
            result = _resolve_character("把她的衣服换成红色", "session", False)
        self.assertEqual(result["status"], "not_found")
        last_character.assert_not_called()

    def test_successful_generation_records_selected_character_id(self):
        client = Mock()
        client.generate_prompt.return_value = "generated prompt"
        character = {"character": "shiroko", "name": "Shiroko",
                     "copyright_name": "Blue Archive"}
        with (patch("app.settings.get",
                    side_effect=lambda key, default=None:
                    True if key == "use_character_db" else ""),
              patch("app.settings.resolve_size", return_value=(896, 1152)),
              patch("app.char_db_built", return_value=True),
              patch("app._resolve_workflow_path", return_value="workflow.json"),
              patch("app.store.ensure_session", return_value="session"),
              patch("app.store.add_message") as add_message,
              patch("app._make_llm", return_value=client),
              patch("app.get_character", return_value=character),
              patch("app.format_character", return_value="role context"),
              patch("app.engine.generate",
                    return_value={"image": "data:image/png;base64,eA==",
                                  "elapsed": 1}),
              patch("app._save_data_url", return_value="/outputs/image.png")):
            events = list(_run_generation({
                "prompt": "Shiroko", "use_history": False,
                "character_checked": True, "character_id": "shiroko",
            }))
        self.assertIn('"step": "done"', events[-1])
        assistant_meta = add_message.call_args_list[1].kwargs["meta"]
        self.assertEqual(assistant_meta["character_id"], "shiroko")


if __name__ == "__main__":
    unittest.main()
