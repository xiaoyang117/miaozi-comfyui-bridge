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

    def test_resolve_endpoint_returns_separate_roles(self):
        client = Mock()
        client.extract_tags.return_value = "shiroko\nhatsune_miku"
        matches = {
            "shiroko": [{"character": "shiroko", "name": "Shiroko",
                         "copyright_name": "Blue Archive"}],
            "hatsune_miku": [{"character": "miku", "name": "Miku",
                              "copyright_name": "Vocaloid"}],
        }
        with (patch("app.settings.get",
                    side_effect=lambda key, default=None:
                    True if key == "use_character_db" else ""),
              patch("app.char_db_built", return_value=True),
              patch("app.direct_candidates", return_value=[]),
              patch("app._make_llm", return_value=client),
              patch("app.find_candidates", side_effect=lambda q, limit: matches[q])):
            response = app.test_client().post("/api/characters/resolve", json={
                "prompt": "Shiroko and Miku", "use_history": False,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual([g["candidates"][0]["character"]
                          for g in response.json["groups"]], ["shiroko", "miku"])

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

    def test_multiple_named_characters_have_independent_candidates(self):
        client = Mock()
        client.extract_tags.return_value = ("shiroko | blue_archive\n"
                                            "hatsune_miku | vocaloid")

        def candidates(query, limit):
            if query == "shiroko, blue_archive":
                return [{"character": "shiroko", "name": "Shiroko",
                         "copyright_name": "Blue Archive"}]
            return [{"character": "miku", "name": "Hatsune Miku",
                     "copyright_name": "Vocaloid"}]

        with (patch("app.direct_candidates", return_value=[]),
              patch("app._make_llm", return_value=client),
              patch("app.find_candidates", side_effect=candidates) as find,
              patch("app.store.last_assistant_character_id") as previous):
            result = _resolve_character("白子和初音未来一起合影", "session")
        self.assertEqual(result["status"], "matched")
        self.assertEqual([g["candidates"][0]["character"] for g in result["groups"]],
                         ["shiroko", "miku"])
        self.assertEqual(find.call_count, 2)
        previous.assert_not_called()

    def test_ambiguous_character_does_not_hide_other_match(self):
        client = Mock()
        client.extract_tags.return_value = "shiroko\nhatsune_miku"
        with (patch("app.direct_candidates", return_value=[]),
              patch("app._make_llm", return_value=client),
              patch("app.find_candidates", side_effect=[
                  [{"character": "shiroko_a", "name": "Shiroko",
                    "copyright_name": "A"},
                   {"character": "shiroko_b", "name": "Shiroko",
                    "copyright_name": "B"}],
                  [{"character": "miku", "name": "Miku",
                    "copyright_name": "Vocaloid"}],
              ])):
            result = _resolve_character("白子和初音未来", "session")
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual([g["status"] for g in result["groups"]],
                         ["ambiguous", "matched"])

    def test_legacy_comma_output_can_represent_two_known_characters(self):
        client = Mock()
        client.extract_tags.return_value = "shiroko, hatsune_miku"
        matches = {
            "shiroko": [{"character": "shiroko", "name": "Shiroko",
                         "copyright_name": "Blue Archive"}],
            "hatsune_miku": [{"character": "miku", "name": "Miku",
                              "copyright_name": "Vocaloid"}],
        }
        with (patch("app.direct_candidates", return_value=[]),
              patch("app._make_llm", return_value=client),
              patch("app.find_candidates",
                    side_effect=lambda q, limit: matches.get(q, []))):
            result = _resolve_character("白子和初音未来合影")
        self.assertEqual([g["candidates"][0]["character"] for g in result["groups"]],
                         ["shiroko", "miku"])

    def test_plural_pronoun_inherits_both_verified_characters(self):
        client = Mock()
        client.extract_tags.return_value = "无"
        characters = {
            "shiroko": {"character": "shiroko", "name": "Shiroko",
                        "copyright_name": "Blue Archive"},
            "miku": {"character": "miku", "name": "Miku",
                     "copyright_name": "Vocaloid"},
        }
        with (patch("app.direct_candidates", return_value=[]),
              patch("app._make_llm", return_value=client),
              patch("app.store.last_assistant_character_ids",
                    return_value=["shiroko", "miku"]),
              patch("app.get_character", side_effect=characters.get)):
            result = _resolve_character("让她们一起换上冬装", "session")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["source"], "history")
        self.assertEqual([g["candidates"][0]["character"] for g in result["groups"]],
                         ["shiroko", "miku"])

    def test_singular_pronoun_does_not_guess_among_previous_characters(self):
        client = Mock()
        client.extract_tags.return_value = "无"
        with (patch("app.direct_candidates", return_value=[]),
              patch("app._make_llm", return_value=client),
              patch("app.store.last_assistant_character_ids",
                    return_value=["shiroko", "miku"]),
              patch("app.get_character") as lookup):
            result = _resolve_character("把她的裙子换成蓝色", "session")
        self.assertEqual(result["status"], "ambiguous_reference")
        self.assertIn("写明", result["message"])
        lookup.assert_not_called()

    def test_new_explicit_role_replaces_old_unless_both_are_requested(self):
        client = Mock()
        client.extract_tags.return_value = "hatsune_miku"
        characters = {"character": "shiroko", "name": "Shiroko",
                      "copyright_name": "Blue Archive"}
        with (patch("app.direct_candidates", return_value=[]),
              patch("app._make_llm", return_value=client),
              patch("app.find_candidates",
                    return_value=[{"character": "miku", "name": "Miku",
                                   "copyright_name": "Vocaloid"}]),
              patch("app.store.last_assistant_character_ids",
                    return_value=["shiroko"]),
              patch("app.get_character", return_value=characters)):
            replacement = _resolve_character("把她换成初音未来", "session")
            together = _resolve_character("让她和初音未来一起合影", "session")
        self.assertEqual([g["candidates"][0]["character"] for g in replacement["groups"]],
                         ["miku"])
        self.assertEqual({g["candidates"][0]["character"] for g in together["groups"]},
                         {"miku", "shiroko"})

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

    def test_multiple_selected_characters_keep_features_and_history_separate(self):
        client = Mock()
        client.generate_prompt.return_value = "generated prompt"
        characters = {
            "shiroko": {"character": "shiroko", "name": "Shiroko",
                        "copyright_name": "Blue Archive", "tag": "white hair"},
            "miku": {"character": "miku", "name": "Miku",
                     "copyright_name": "Vocaloid", "tag": "teal hair"},
        }
        with (patch("app.settings.get",
                    side_effect=lambda key, default=None:
                    True if key == "use_character_db" else ""),
              patch("app.settings.resolve_size", return_value=(896, 1152)),
              patch("app.char_db_built", return_value=True),
              patch("app._resolve_workflow_path", return_value="workflow.json"),
              patch("app.store.ensure_session", return_value="session"),
              patch("app.store.context_pairs", return_value=[]),
              patch("app.store.last_assistant_character_ids",
                    return_value=["shiroko"]),
              patch("app.store.add_message") as add_message,
              patch("app._make_llm", return_value=client),
              patch("app.get_character", side_effect=characters.get),
              patch("app.format_character",
                    side_effect=lambda c, max_tags=80:
                    f"角色: {c['character']}\n特征标签: {c['tag']}"),
              patch("app.engine.generate",
                    return_value={"image": "data:image/png;base64,eA=="}),
              patch("app._save_data_url", return_value="/outputs/image.png")):
            events = list(_run_generation({
                "prompt": "Shiroko and Miku", "use_history": True,
                "character_checked": True,
                "character_ids": ["shiroko", "miku", "missing", "shiroko"],
                "inherited_character_ids": ["shiroko"],
            }))
        self.assertIn('"step": "done"', events[-1])
        context = client.generate_prompt.call_args.kwargs["context"]
        self.assertIn("角色 1（Shiroko，Blue Archive，上一轮沿用角色）：\n角色: shiroko\n特征标签: white hair",
                      context)
        self.assertIn("角色 2（Miku，Vocaloid）：\n角色: miku\n特征标签: teal hair",
                      context)
        self.assertEqual(add_message.call_args_list[1].kwargs["meta"]["character_ids"],
                         ["shiroko", "miku"])


if __name__ == "__main__":
    unittest.main()
