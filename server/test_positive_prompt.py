import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import _run_generation
from settings import Settings


class PositivePromptTest(unittest.TestCase):
    def test_positive_content_survives_settings_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with (patch("settings.DATA_DIR", path),
                  patch("settings.SETTINGS_FILE", path / "settings.json")):
                settings = Settings()
                self.assertEqual(settings.get("positive_prompt_prefix"), "")
                settings.update({
                    "positive_prompt_prefix": "masterpiece, cinematic lighting",
                })
                self.assertEqual(Settings().get("positive_prompt_prefix"),
                                 "masterpiece, cinematic lighting")

    def test_positive_content_is_sent_to_workflow_and_saved_in_history(self):
        for prefix, expected in (
            ("  masterpiece, cinematic lighting,  ",
             "masterpiece, cinematic lighting, Miku"),
            ("", "Miku"),
        ):
            with self.subTest(prefix=prefix):
                llm = Mock()
                llm.generate_prompt.return_value = "Miku"
                values = {
                    "use_character_db": False,
                    "positive_prompt_prefix": prefix,
                    "prompt_placeholder": "PROMPT_PH",
                }
                with (patch("app.settings.get",
                            side_effect=lambda key, default=None: values.get(key, default)),
                      patch("app.settings.resolve_size", return_value=(896, 1152)),
                      patch("app._resolve_workflow_path", return_value="workflow.json"),
                      patch("app.store.ensure_session", return_value="session"),
                      patch("app.store.add_message") as add_message,
                      patch("app._make_llm", return_value=llm),
                      patch("app.engine.generate",
                            return_value={"image": "data:image/png;base64,eA==",
                                          "elapsed": 1}) as generate,
                      patch("app._save_data_url", return_value="/outputs/image.png")):
                    events = list(_run_generation({
                        "prompt": "初音未来", "use_search": False, "use_history": False,
                    }))
                done = json.loads(events[-1].removeprefix("data: "))
                self.assertEqual(done["prompt"], expected)
                self.assertEqual(generate.call_args.args[0]["replacements"],
                                 {"PROMPT_PH": expected})
                self.assertEqual(add_message.call_args_list[1].kwargs["content"],
                                 expected)


if __name__ == "__main__":
    unittest.main()
