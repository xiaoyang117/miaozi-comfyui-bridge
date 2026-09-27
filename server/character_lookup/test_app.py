import json
import unittest
from unittest.mock import patch

from app import _run_generation, app


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


if __name__ == "__main__":
    unittest.main()
