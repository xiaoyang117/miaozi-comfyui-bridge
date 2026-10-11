import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from app import app
from llm import prompts
from llm.client import GeneratedPrompt, LLMClient, LLMError, parse_generated_prompt
from settings import Settings
from store import Store


class GeneratedPromptTest(unittest.TestCase):
    def test_json_fences_and_extra_text_keep_fields_separate(self):
        description = '她穿着“红色外套”，站在雨中的街道。\n背景带有 "暖色灯光"。'
        payload = {"prompt": "1girl, red coat, rain", "description": description}
        compact = json.dumps(payload, ensure_ascii=False)
        pretty = json.dumps(payload, ensure_ascii=False, indent=2)
        for raw in (compact, pretty, f"```json\n{pretty}\n```",
                    f"以下是结果：\n{compact}\n生成完毕。"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_generated_prompt(raw),
                                 GeneratedPrompt(payload["prompt"], description))

    def test_legacy_text_and_missing_description_are_compatible(self):
        for raw in ('Prompt: "1girl, red coat"', "```text\n1girl, red coat\n```",
                    '{"prompt": "1girl, red coat"}'):
            with self.subTest(raw=raw):
                self.assertEqual(parse_generated_prompt(raw),
                                 GeneratedPrompt("1girl, red coat"))

    def test_invalid_structured_output_is_never_used_as_a_prompt(self):
        for raw in (
            '{"prompt": "1girl", "description": ',
            '{"description": "红衣女孩"}',
            '{"prompt": 123, "description": "红衣女孩"}',
            '{"prompt": "1girl", "description": ["红衣女孩"]}',
            '{"prompt": "1girl", "description": null}',
            '[{"prompt": "1girl", "description": "红衣女孩"}]',
            '"prompt": "1girl", "description": "红衣女孩"',
            'Prompt: 1girl\n说明：红衣女孩',
        ):
            with self.subTest(raw=raw), self.assertRaises(LLMError):
                parse_generated_prompt(raw)

    def test_all_generation_templates_request_description_in_one_call(self):
        client = LLMClient({})
        output = '{"prompt": "red coat", "description": "穿红色外套。"}'
        for context, specific, custom in (
            ("", False, ""), ("", True, ""),
            ("角色资料", False, ""), ("角色资料", True, ""),
            ("", False, "使用水彩风格。"),
        ):
            with self.subTest(context=context, specific=specific, custom=custom):
                with patch.object(client, "call", return_value=output) as call:
                    result = client.generate_prompt(
                        "改穿红色外套", history_text="上轮画面", context=context,
                        specific=specific, custom_system=custom,
                        include_description=True)
                self.assertEqual(result, GeneratedPrompt("red coat", "穿红色外套。"))
                call.assert_called_once()
                system, user = call.call_args.args
                self.assertIn(prompts.PROMPT_WITH_DESCRIPTION_OUTPUT, system)
                self.assertNotIn(prompts.PROMPT_ONLY_OUTPUT, system)
                self.assertIn("改穿红色外套", user)
                self.assertIn("上轮画面", user)
                if context:
                    self.assertIn(context, user)
                if custom:
                    self.assertIn(custom, system)

    def test_default_client_contract_still_returns_plain_prompt(self):
        client = LLMClient({})
        with patch.object(client, "call", return_value='Prompt: "red coat"') as call:
            self.assertEqual(client.generate_prompt("红色外套"), "red coat")
        call.assert_called_once_with(prompts.PROMPT_SYSTEM, "红色外套")


class ImageDescriptionIntegrationTest(unittest.TestCase):
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
            "llm_base_url": "https://example.com/v1", "llm_model": "test-model",
            "prompt_placeholder": "PROMPT_PH", "positive_prompt_prefix": "masterpiece",
        })
        self.store = Store(self.path / "sessions.db")
        stack.enter_context(patch("app.settings", self.settings))
        stack.enter_context(patch("app.store", self.store))
        stack.enter_context(patch("app._resolve_workflow_path", return_value="workflow.json"))
        self.generate = stack.enter_context(patch(
            "app.engine.generate", return_value={"image": "image-data", "elapsed": 1}))
        stack.enter_context(patch("app._save_data_url", return_value="/outputs/image.png"))
        self.description = '她穿着“红色外套”，站在雨中的街道。\n背景带有 "暖色灯光"。'
        self.output = {"prompt": "1girl, red coat, rain", "description": self.description}
        self.response = Mock(ok=True, status_code=200)
        self.set_model_output(json.dumps(self.output, ensure_ascii=False))
        self.post = stack.enter_context(patch(
            "llm.client.requests.post", return_value=self.response))
        self.session = self.store.create_session()["id"]
        self.client = app.test_client()

    def set_model_output(self, output):
        self.response.json.return_value = {"choices": [{"message": {"content": output}}]}

    def request_generation(self, **overrides):
        response = self.client.post("/api/generate", json={
            "prompt": "画个穿红衣服的女孩", "session_id": self.session,
            "use_search": False, "use_history": False, **overrides,
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.content_type)
        return [json.loads(line.removeprefix("data: "))
                for line in response.get_data(as_text=True).splitlines()
                if line.startswith("data: ")]

    def test_generation_sends_only_prompt_and_persists_description(self):
        events = self.request_generation()
        expected_prompt = "masterpiece, " + self.output["prompt"]
        self.assertEqual(events[-1]["step"], "done")
        self.assertEqual(events[-1]["description"], self.description)
        self.assertEqual(events[-1]["prompt"], expected_prompt)
        self.post.assert_called_once()
        body = self.post.call_args.kwargs["json"]
        self.assertEqual(set(body), {"model", "messages", "temperature"})
        self.assertIn(prompts.PROMPT_WITH_DESCRIPTION_OUTPUT,
                      body["messages"][0]["content"])
        job = self.generate.call_args.args[0]
        self.assertEqual(job["prompt"], expected_prompt)
        self.assertEqual(job["replacements"], {"PROMPT_PH": expected_prompt})
        messages = Store(self.path / "sessions.db").list_messages(self.session)
        self.assertEqual(messages[-1]["content"], expected_prompt)
        self.assertEqual(messages[-1]["meta"]["description"], self.description)

    def test_regeneration_reuses_description_without_any_llm_request(self):
        self.request_generation()
        self.post.reset_mock()
        source = self.store.list_messages(self.session)[-1]
        for _ in range(2):
            result = self.request_generation(regenerate_message_id=source["id"])[-1]
            self.assertEqual(result["step"], "done")
            self.assertEqual(result["description"], self.description)
            source = self.store.list_messages(self.session)[-1]
            self.assertEqual(source["meta"]["description"], self.description)
            self.assertEqual(self.generate.call_args.args[0]["prompt"],
                             "masterpiece, " + self.output["prompt"])
        self.post.assert_not_called()

    def test_old_or_invalid_history_description_is_safe(self):
        for meta in ({}, {"description": None}, {"description": ["invalid"]}):
            with self.subTest(meta=meta):
                source_id = self.store.add_message(
                    self.session, "assistant", content="1girl",
                    image="/outputs/old.png", meta=meta)
                result = self.request_generation(regenerate_message_id=source_id)[-1]
                self.assertEqual(result["step"], "done")
                self.assertEqual(result["description"], "")
                latest = self.store.list_messages(self.session)[-1]
                self.assertEqual(latest["meta"]["description"], "")
        self.post.assert_not_called()

    def test_legacy_model_response_still_generates_without_description(self):
        self.set_model_output("Prompt: 1girl, red coat")
        result = self.request_generation()[-1]
        self.assertEqual(result["step"], "done")
        self.assertEqual(result["description"], "")
        self.assertEqual(result["prompt"], "masterpiece, 1girl, red coat")

    def test_bad_json_or_empty_prompt_never_reaches_comfyui_or_history(self):
        for raw in ('{"prompt": "1girl", "description": ',
                    '{"description": "红衣女孩"}',
                    '{"prompt": "", "description": "红衣女孩"}'):
            with self.subTest(raw=raw):
                self.set_model_output(raw)
                self.assertEqual(self.request_generation()[-1]["step"], "error")
                self.generate.assert_not_called()
                self.assertEqual(self.store.list_messages(self.session), [])

    def test_image_failure_does_not_save_description(self):
        self.generate.side_effect = RuntimeError("offline")
        self.assertEqual(self.request_generation()[-1]["step"], "error")
        self.assertEqual(self.store.list_messages(self.session), [])

    def test_context_inherits_prompt_without_description_or_json(self):
        self.request_generation()
        self.request_generation(prompt="把她的外套改成蓝色", use_history=True)
        user = self.post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertIn("masterpiece, " + self.output["prompt"], user)
        self.assertIn("把她的外套改成蓝色", user)
        self.assertNotIn(self.description, user)
        self.assertNotIn('"description"', user)


if __name__ == "__main__":
    unittest.main()
