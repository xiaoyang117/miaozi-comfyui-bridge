import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from app import app, _gen_lock
from backends import ComfyBackend
from settings import Settings
from store import Store


class RegenerationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name)
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.store = Store(path / "sessions.db")
        stack.enter_context(patch("settings.DATA_DIR", path))
        stack.enter_context(patch("settings.SETTINGS_FILE", path / "settings.json"))
        self.settings = Settings()
        self.settings.update({
            "access_password": "",
            "positive_prompt_prefix": "new quality words",
            "prompt_placeholder": "PROMPT_PH",
        })
        for name, value in (("store", self.store), ("settings", self.settings)):
            stack.enter_context(patch("app." + name, value))
        self.llm = stack.enter_context(patch("app._make_llm"))
        self.vlm = stack.enter_context(patch("app.describe_images"))
        self.lookup = stack.enter_context(patch("app._resolve_character"))
        self.context = stack.enter_context(patch.object(self.store, "context_pairs"))
        self.generate = stack.enter_context(patch(
            "app.engine.generate", return_value={"image": "image-data", "elapsed": 2}))
        stack.enter_context(patch("app._save_data_url", return_value="/outputs/new.png"))
        self.session = self.store.create_session()["id"]
        self.store.add_message(self.session, "user", content="初音未来")
        self.prompt = "masterpiece, hatsune_miku, blue eyes"
        self.source_id = self.store.add_message(
            self.session, "assistant", content=self.prompt,
            image="/outputs/original.png", character="Miku",
            meta={"width": 768, "height": 1024, "workflow_path": "original.json",
                  "character_ids": ["hatsune_miku"]})
        self.client = app.test_client()

    def regenerate(self, **overrides):
        payload = {"session_id": self.session,
                   "regenerate_message_id": self.source_id, **overrides}
        response = self.client.post("/api/generate", json=payload)
        self.assertIn("text/event-stream", response.content_type)
        return [json.loads(line.removeprefix("data: "))
                for line in response.get_data(as_text=True).splitlines()
                if line.startswith("data: ")]

    def test_saved_prompt_is_reused_without_any_model_or_prefix_changes(self):
        events = self.regenerate(
            prompt="different prompt", workflow_path="different.json",
            images=["reference-image"], use_search=True, use_history=True)
        self.assertEqual([event["step"] for event in events], ["comfyui", "done"])
        job = self.generate.call_args.args[0]
        self.assertEqual(job["prompt"], self.prompt)
        self.assertEqual(job["replacements"], {"PROMPT_PH": self.prompt})
        self.assertEqual(job["workflow_path"], "original.json")
        self.assertEqual((job["width"], job["height"]), (768, 1024))
        self.assertTrue(job["randomize_seed"])
        for mock in (self.llm, self.vlm, self.lookup, self.context):
            mock.assert_not_called()

        messages = self.store.list_messages(self.session)
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[1]["image"], "/outputs/original.png")
        new = messages[-1]
        self.assertEqual(new["content"], self.prompt)
        self.assertEqual(new["meta"]["regenerated_from"], self.source_id)
        self.assertEqual(new["character"], "Miku")
        self.assertEqual(self.store.last_assistant_character_ids(self.session),
                         ["hatsune_miku"])
        self.regenerate(regenerate_message_id=new["id"])
        self.assertEqual(self.generate.call_args.args[0]["prompt"], self.prompt)
        self.assertEqual(len(self.store.list_messages(self.session)), 4)

    def test_old_history_uses_current_workflow_and_resolution_fallback(self):
        source_id = self.store.add_message(
            self.session, "assistant", content=self.prompt, image="/outputs/old.png")
        events = self.regenerate(
            regenerate_message_id=source_id, workflow_path="selected.json", preset_index=0)
        self.assertEqual(events[-1]["step"], "done")
        job = self.generate.call_args.args[0]
        self.assertEqual(job["workflow_path"], "selected.json")
        self.assertEqual((job["width"], job["height"]), self.settings.resolve_size(0))

    def test_invalid_or_cross_session_sources_cannot_generate(self):
        user_id = self.store.list_messages(self.session)[0]["id"]
        empty_id = self.store.add_message(
            self.session, "assistant", content="", image="/outputs/old.png")
        missing_image_id = self.store.add_message(
            self.session, "assistant", content=self.prompt)
        other_session = self.store.create_session()["id"]
        for overrides in (
            {"session_id": other_session},
            {"regenerate_message_id": 99999},
            {"regenerate_message_id": True},
            {"regenerate_message_id": user_id},
            {"regenerate_message_id": empty_id},
            {"regenerate_message_id": missing_image_id},
        ):
            with self.subTest(overrides=overrides):
                before = self.store.list_messages(self.session)
                self.assertEqual(self.regenerate(**overrides)[-1]["step"], "error")
                self.generate.assert_not_called()
                self.assertEqual(self.store.list_messages(self.session), before)

    def test_invalid_history_metadata_uses_current_workflow(self):
        for meta in ("broken JSON", "[]", "null"):
            with self.subTest(meta=meta):
                with self.store._conn() as conn:
                    conn.execute("UPDATE messages SET meta = ? WHERE id = ?",
                                 (meta, self.source_id))
                self.assertEqual(self.regenerate(workflow_path="current.json")[-1]["step"],
                                 "done")
                self.assertEqual(self.generate.call_args.args[0]["workflow_path"],
                                 "current.json")

    def test_failure_keeps_history_and_releases_generation_lock(self):
        for result, error in ((None, RuntimeError("offline")), ({}, None)):
            with self.subTest(result=result):
                self.generate.side_effect = error
                self.generate.return_value = result
                self.assertEqual(self.regenerate()[-1]["step"], "error")
                self.assertEqual(len(self.store.list_messages(self.session)), 2)
        self.generate.side_effect = None
        self.generate.return_value = {"image": "image-data"}
        self.assertEqual(self.regenerate()[-1]["step"], "done")

    def test_regeneration_shares_the_existing_generation_queue(self):
        _gen_lock.acquire()
        try:
            response = self.client.post("/api/generate", json={
                "session_id": self.session, "regenerate_message_id": self.source_id})
            self.assertEqual(response.json, {"queue": True})
            self.generate.assert_not_called()
        finally:
            _gen_lock.release()


class RegenerationSeedTest(unittest.TestCase):
    def test_seed_changes_reach_submission_only_when_requested(self):
        workflow = {
            "1": {"inputs": {"seed": 1, "steps": 20}},
            "2": {"inputs": {"noise_seed": 1}},
            "3": {"inputs": {"seed": ["1", 0], "noise_seed": True}},
        }
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "image.png"
            image.touch()
            backend = ComfyBackend({"comfyui_url": "http://127.0.0.1:8188"}, log=lambda _: None)
            with (patch.object(backend, "_load_workflow",
                               side_effect=lambda _: copy.deepcopy(workflow)),
                  patch.object(backend, "_submit_http", return_value="job") as submit,
                  patch.object(backend, "_fetch_via_http", return_value=str(image)),
                  patch.object(backend, "_encode_image", return_value="image-data"),
                  patch("backends.secrets.randbelow", return_value=7) as random):
                backend.generate({"workflow_path": "workflow.json"})
                self.assertEqual(submit.call_args.args[0], workflow)
                random.assert_not_called()
                backend.generate({"workflow_path": "workflow.json", "randomize_seed": True})
                submitted = submit.call_args.args[0]
                self.assertEqual(submitted["1"]["inputs"]["seed"], 7)
                self.assertEqual(submitted["2"]["inputs"]["noise_seed"], 7)
                self.assertEqual(submitted["3"], workflow["3"])
                self.assertEqual(submitted["1"]["inputs"]["steps"], 20)
                random.assert_called_once()
                self.assertEqual(workflow["1"]["inputs"]["seed"], 1)


if __name__ == "__main__":
    unittest.main()
