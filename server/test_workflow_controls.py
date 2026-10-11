import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import app
from backends import BackendError, ComfyBackend
from settings import Settings
from store import Store
from workflow_controls import apply_loras, apply_resolution, describe_workflow


def example_workflow():
    return {
        "28": {"class_type": "EmptyLatentImage",
               "inputs": {"width": 1216, "height": 832}},
        "100": {"class_type": "EmptyLatentImage",
                "inputs": {"width": 832, "height": 1216}},
        "44": {"class_type": "UNETLoader", "inputs": {"unet_name": "base.safetensors"}},
        "45": {"class_type": "CLIPLoader", "inputs": {"clip_name": "clip.safetensors"}},
        "11": {"class_type": "CLIPTextEncode",
               "inputs": {"text": "positive", "clip": ["45", 0]}},
        "12": {"class_type": "CLIPTextEncode",
               "inputs": {"text": "negative", "clip": ["45", 0]}},
        "19": {"class_type": "KSampler",
               "inputs": {"model": ["44", 0], "positive": ["11", 0],
                          "negative": ["12", 0], "latent_image": ["100", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["19", 0]}},
        "66": {"class_type": "SaveImage", "inputs": {"images": ["8", 0]}},
    }


class ResolutionControlsTest(unittest.TestCase):
    def test_only_connected_latent_is_recommended_and_changed(self):
        workflow = example_workflow()
        description = describe_workflow(workflow, "66")
        self.assertEqual([node["id"] for node in description["resolution_nodes"]
                          if node["recommended"]], ["100"])
        apply_resolution(workflow, description, 1024, 768)
        self.assertEqual((workflow["100"]["inputs"]["width"],
                          workflow["100"]["inputs"]["height"]), (1024, 768))
        self.assertEqual((workflow["28"]["inputs"]["width"],
                          workflow["28"]["inputs"]["height"]), (1216, 832))
        with self.assertRaisesRegex(ValueError, "找不到出图支路"):
            apply_resolution(workflow, description, 800, 800, "28")

    def test_primitive_dimensions_keep_links_and_change_integer_sources(self):
        workflow = example_workflow()
        workflow["101"] = {"class_type": "PrimitiveInt", "inputs": {"value": 832}}
        workflow["102"] = {"class_type": "Reroute", "inputs": {"input": ["101", 0]}}
        workflow["100"]["inputs"]["width"] = ["102", 0]
        description = describe_workflow(workflow, "66")
        self.assertTrue(next(node for node in description["resolution_nodes"]
                             if node["id"] == "100")["editable"])
        apply_resolution(workflow, description, 640, 384)
        self.assertEqual(workflow["101"]["inputs"]["value"], 640)
        self.assertEqual(workflow["100"]["inputs"]["width"], ["102", 0])

    def test_shared_width_and_height_primitive_is_rejected_without_changes(self):
        workflow = example_workflow()
        workflow["101"] = {"class_type": "PrimitiveInt", "inputs": {"value": 832}}
        workflow["100"]["inputs"].update(width=["101", 0], height=["101", 0])
        before = copy.deepcopy(workflow)
        with self.assertRaisesRegex(ValueError, "共用"):
            apply_resolution(workflow, describe_workflow(workflow, "66"), 640, 384)
        self.assertEqual(workflow, before)

    def test_node_computing_unknown_dimensions_is_not_rewritten(self):
        workflow = example_workflow()
        workflow["101"] = {"class_type": "MathExpression", "inputs": {"a": 832}}
        workflow["100"]["inputs"]["width"] = ["101", 0]
        with self.assertRaisesRegex(ValueError, "无法识别"):
            apply_resolution(workflow, describe_workflow(workflow, "66"), 640, 384)
        self.assertEqual(workflow["100"]["inputs"]["width"], ["101", 0])

    def test_sd3_latent_and_other_image_size_inputs_are_discovered(self):
        workflow = example_workflow()
        workflow["100"] = {"class_type": "EmptySD3LatentImage",
                           "inputs": {"width": 512, "height": 640}}
        description = describe_workflow(workflow, "66")
        apply_resolution(workflow, description, 768, 896)
        self.assertEqual(workflow["100"]["inputs"], {"width": 768, "height": 896})
        workflow["100"]["inputs"] = {"image_width": 512, "image_height": 640}
        apply_resolution(workflow, describe_workflow(workflow, "66"), 640, 384)
        self.assertEqual(workflow["100"]["inputs"]["image_width"], 640)

    def test_unrecognized_workflow_requires_explicit_choice(self):
        workflow = example_workflow()
        workflow["100"]["inputs"].pop("width")
        self.assertFalse(any(n["recommended"] for n in
                             describe_workflow(workflow, "66")["resolution_nodes"]))
        with self.assertRaisesRegex(ValueError, "找不到"):
            apply_resolution(workflow, describe_workflow(workflow, "66"), 640, 384)

    def test_second_pass_upscale_size_is_preserved(self):
        workflow = example_workflow()
        workflow["200"] = {"class_type": "LatentUpscale",
                           "inputs": {"samples": ["19", 0], "width": 2048, "height": 2048}}
        workflow["201"] = {"class_type": "KSampler",
                           "inputs": {**workflow["19"]["inputs"],
                                      "latent_image": ["200", 0]}}
        workflow["8"]["inputs"]["samples"] = ["201", 0]
        description = describe_workflow(workflow, "66")
        self.assertEqual([node["id"] for node in description["resolution_nodes"]
                          if node["recommended"]], ["100"])
        apply_resolution(workflow, description, 640, 384)
        self.assertEqual(workflow["100"]["inputs"]["width"], 640)
        self.assertEqual(workflow["200"]["inputs"]["width"], 2048)


class LoraControlsTest(unittest.TestCase):
    def test_existing_loader_values_change_without_touching_inactive_loader(self):
        workflow = example_workflow()
        workflow["10"] = {"class_type": "LoraLoader",
                          "inputs": {"model": ["44", 0], "clip": ["45", 0],
                                     "lora_name": "old.safetensors",
                                     "strength_model": 0.5, "strength_clip": 0.5}}
        workflow["19"]["inputs"]["model"] = ["10", 0]
        workflow["11"]["inputs"]["clip"] = ["10", 1]
        workflow["12"]["inputs"]["clip"] = ["10", 1]
        workflow["9"] = {"class_type": "LoraLoaderModelOnly",
                         "inputs": {"model": ["44", 0], "lora_name": "unused.safetensors",
                                    "strength_model": 1}}
        description = describe_workflow(workflow, "66")
        self.assertFalse(next(n for n in description["lora_nodes"] if n["id"] == "9")["active"])
        apply_loras(workflow, description, {"10": {"lora_name": "new.safetensors",
                                                  "strength_model": 0.8,
                                                  "strength_clip": 0.3}}, [], "")
        self.assertEqual(workflow["10"]["inputs"]["strength_model"], 0.8)
        self.assertEqual(workflow["10"]["inputs"]["lora_name"], "new.safetensors")
        with self.assertRaisesRegex(ValueError, "不可用"):
            apply_loras(workflow, description, {"9": {"strength_model": 1}}, [], "")

    def test_multiple_loras_form_model_and_clip_chains_without_mutating_original(self):
        workflow = example_workflow()
        before = copy.deepcopy(workflow)
        controls = describe_workflow(workflow, "66")
        apply_loras(workflow, controls, {}, [
            {"lora_name": "a.safetensors", "strength_model": 0.8,
             "strength_clip": 0.2},
            {"lora_name": "b.safetensors", "strength_model": 0.6,
             "strength_clip": 0.4},
        ], "19")
        model_id = workflow["19"]["inputs"]["model"][0]
        second = workflow[model_id]
        self.assertEqual(second["inputs"]["lora_name"], "b.safetensors")
        first_id = second["inputs"]["model"][0]
        self.assertEqual(workflow[first_id]["inputs"]["lora_name"], "a.safetensors")
        self.assertEqual(second["inputs"]["clip"], [first_id, 1])
        for field in ("positive", "negative"):
            new_encode = workflow[workflow["19"]["inputs"][field][0]]
            self.assertEqual(new_encode["inputs"]["clip"], [model_id, 1])
        self.assertEqual(workflow["11"], before["11"])
        self.assertEqual(workflow["12"], before["12"])

    def test_model_only_supports_workflows_without_clip_connections(self):
        workflow = example_workflow()
        workflow["11"]["inputs"].pop("clip")
        workflow["12"]["inputs"].pop("clip")
        controls = describe_workflow(workflow, "66")
        apply_loras(workflow, controls, {}, [
            {"lora_name": "model-only.safetensors", "strength_model": 1,
             "model_only": True}], "19")
        node = workflow[workflow["19"]["inputs"]["model"][0]]
        self.assertEqual(node["class_type"], "LoraLoaderModelOnly")
        self.assertNotIn("clip", node["inputs"])
        with self.assertRaisesRegex(ValueError, "CLIP 支路"):
            apply_loras(workflow, controls, {}, [{"lora_name": "full.safetensors"}], "19")

    def test_custom_loader_with_single_strength_is_editable(self):
        workflow = example_workflow()
        workflow["20"] = {"class_type": "CustomLoraLoader",
                          "inputs": {"model": ["44", 0], "lora_name": "a.safetensors",
                                     "strength": 0.4}}
        workflow["19"]["inputs"]["model"] = ["20", 0]
        description = describe_workflow(workflow, "66")
        self.assertEqual(description["lora_nodes"][0]["strength"], 0.4)
        apply_loras(workflow, description,
                    {"20": {"lora_name": "b.safetensors", "strength": "0.7"}}, [], "")
        self.assertEqual(workflow["20"]["inputs"]["strength"], 0.7)

    def test_full_lora_only_rewires_selected_sampler_when_clip_is_shared(self):
        workflow = example_workflow()
        workflow["21"] = {"class_type": "KSampler",
                          "inputs": {**workflow["19"]["inputs"],
                                     "latent_image": ["28", 0]}}
        workflow["67"] = {"class_type": "SaveImage", "inputs": {"images": ["21", 0]}}
        workflow["66"]["inputs"]["other_image"] = ["67", 0]
        controls = describe_workflow(workflow, "66")
        apply_loras(workflow, controls, {},
                    [{"lora_name": "foo.safetensors", "strength_model": 0.8}], "19")
        self.assertEqual(workflow["21"]["inputs"]["positive"], ["11", 0])
        self.assertEqual(workflow["21"]["inputs"]["model"], ["44", 0])
        self.assertEqual(workflow["11"]["inputs"]["clip"], ["45", 0])

    def test_bad_weights_and_missing_sampler_fail_before_submission(self):
        workflow = example_workflow()
        controls = describe_workflow(workflow, "66")
        with self.assertRaisesRegex(ValueError, "权重必须是数字"):
            apply_loras(workflow, controls, {},
                        [{"lora_name": "a.safetensors", "strength_model": "broken",
                          "model_only": True}], "19")
        with self.assertRaisesRegex(ValueError, "请选择"):
            apply_loras(workflow, controls, {},
                        [{"lora_name": "a.safetensors", "model_only": True}], "28")

    def test_shared_clip_layer_setting_is_preserved_before_lora(self):
        workflow = example_workflow()
        workflow["55"] = {"class_type": "CLIPSetLastLayer",
                          "inputs": {"clip": ["45", 0], "stop_at_clip_layer": -2}}
        workflow["11"]["inputs"]["clip"] = ["55", 0]
        workflow["12"]["inputs"]["clip"] = ["55", 0]
        apply_loras(workflow, describe_workflow(workflow, "66"), {},
                    [{"lora_name": "a.safetensors", "strength_clip": 0.4}], "19")
        new_id = workflow["19"]["inputs"]["model"][0]
        self.assertEqual(workflow[new_id]["inputs"]["clip"], ["55", 0])
        self.assertEqual(workflow["55"]["inputs"]["clip"], ["45", 0])


class WorkflowBackendTest(unittest.TestCase):
    def test_submission_combines_placeholder_resolution_and_lora_with_original_intact(self):
        original = example_workflow()
        original["100"]["inputs"].update(width="PH_W", height="PH_H")
        original["11"]["inputs"]["text"] = "PROMPT"
        backend = ComfyBackend({
            "comfyui_url": "http://127.0.0.1:8188",
            "save_node_id": "66",
            "width_placeholder": "PH_W",
            "height_placeholder": "PH_H",
        }, log=lambda _: None)
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "image.png"
            image.touch()
            with (patch.object(backend, "_load_workflow",
                               side_effect=lambda _: copy.deepcopy(original)),
                  patch.object(backend, "_submit_http", return_value="job") as submit,
                  patch.object(backend, "_fetch_via_http", return_value=str(image)),
                  patch.object(backend, "_encode_image", return_value="image-data")):
                job = {
                    "workflow_path": "workflow.json", "width": 640, "height": 384,
                    "resize": True,
                    "replacements": {"PH_W": 640, "PH_H": 384, "PROMPT": "new prompt"},
                    "workflow_controls": {
                        "resolution_node_id": "auto",
                        "lora_target_id": "19",
                        "extra_loras": [{"lora_name": "style.safetensors",
                                         "strength_model": 0.6,
                                         "strength_clip": 0.3}],
                    },
                }
                result = backend.generate(job)
                sent = submit.call_args.args[0]
                self.assertEqual((sent["100"]["inputs"]["width"],
                                  sent["100"]["inputs"]["height"]), (640, 384))
                self.assertEqual(sent["28"]["inputs"]["width"], 1216)
                self.assertEqual(sent["11"]["inputs"]["text"], "new prompt")
                self.assertEqual(result["base_size"], [640, 384])
                self.assertEqual(original["100"]["inputs"]["width"], "PH_W")
                with self.assertRaisesRegex(BackendError, "请选择分辨率预设"):
                    backend.generate({**job, "resize": False, "replacements": {}})
                with patch.object(backend, "_load_workflow",
                                  side_effect=lambda _: copy.deepcopy(example_workflow())):
                    native = backend.generate({**job, "resize": False, "replacements": {}})
                self.assertEqual(submit.call_args.args[0]["100"]["inputs"]["width"], 832)
                self.assertEqual(native["base_size"], [832, 1216])

    def test_comfy_lora_catalog_and_inspection_endpoints(self):
        backend = ComfyBackend({"comfyui_url": "http://127.0.0.1:8188"})
        response = Mock()
        response.json.return_value = ["subdir\\a.safetensors", "b.safetensors"]
        with patch.object(backend.session, "get", return_value=response) as get:
            self.assertEqual(backend.list_loras(),
                             ["b.safetensors", "subdir\\a.safetensors"])
            get.assert_called_once_with("http://127.0.0.1:8188/models/loras", timeout=8)
        with (patch("app.engine.inspect_workflow",
                    return_value=describe_workflow(example_workflow(), "66")),
              patch("app.engine.list_loras", return_value=["a.safetensors"])):
            client = app.test_client()
            with patch("settings.Settings.access_password", property(lambda self: "")):
                found = client.get("/api/workflows/inspect?path=workflows/MIAOMIAO.json")
                self.assertTrue(found.json["success"])
                self.assertEqual(client.get("/api/loras").json["models"], ["a.safetensors"])
                with patch("app.engine.list_loras", side_effect=BackendError("离线")):
                    offline = client.get("/api/loras").json
                    self.assertFalse(offline["success"])
                    self.assertEqual(offline["models"], [])
                    self.assertEqual(offline["error"], "离线")

    def test_complex_size_calculation_keeps_manual_placeholder_support(self):
        workflow = example_workflow()
        workflow["100"]["inputs"].update(width=["101", 0], height=["101", 1])
        workflow["101"] = {"class_type": "SizeCalculation",
                           "inputs": {"width_value": "PH_W", "height_value": "PH_H"}}
        backend = ComfyBackend({
            "comfyui_url": "http://127.0.0.1:8188", "save_node_id": "66",
            "width_placeholder": "PH_W", "height_placeholder": "PH_H",
        }, log=lambda _: None)
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "image.png"
            image.touch()
            with (patch.object(backend, "_load_workflow",
                               side_effect=lambda _: copy.deepcopy(workflow)),
                  patch.object(backend, "_submit_http", return_value="job") as submit,
                  patch.object(backend, "_fetch_via_http", return_value=str(image)),
                  patch.object(backend, "_encode_image", return_value="image-data")):
                backend.generate({
                    "workflow_path": "workflow.json", "width": 640, "height": 384,
                    "resize": True, "replacements": {"PH_W": 640, "PH_H": 384},
                })
                sent = submit.call_args.args[0]
                self.assertEqual(sent["100"]["inputs"]["width"], ["101", 0])
                self.assertEqual(sent["101"]["inputs"]["width_value"], 640)
                self.assertEqual(sent["101"]["inputs"]["height_value"], 384)
                with self.assertRaisesRegex(BackendError, "无法自动设置"):
                    backend.generate({
                        "workflow_path": "workflow.json", "width": 640, "height": 384,
                        "resize": True,
                        "workflow_controls": {"resolution_node_id": "100"},
                        "replacements": {"PH_W": 640, "PH_H": 384},
                    })


class GenerationControlsTest(unittest.TestCase):
    def test_normal_generation_saves_controls_and_regeneration_reuses_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (patch("settings.DATA_DIR", root),
                  patch("settings.SETTINGS_FILE", root / "settings.json")):
                settings = Settings()
                settings.update({"access_password": "", "use_character_db": False,
                                 "positive_prompt_prefix": "", "prompt_placeholder": "PROMPT"})
                store = Store(root / "history.db")
                session_id = store.create_session()["id"]
                controls = {
                    "resolution_node_id": "100", "lora_target_id": "19",
                    "extra_loras": [{"lora_name": "style.safetensors",
                                     "strength_model": "0.8",
                                     "strength_clip": "0.2"}],
                }
                workflow_path = "workflow.json"
                with (patch("app.settings", settings),
                      patch("app.store", store),
                      patch("app._make_llm") as make_llm,
                      patch("app.engine.generate",
                            return_value={"image": "fake", "elapsed": 1,
                                          "base_size": [640, 384]}) as generate,
                      patch("app._save_data_url", return_value="/outputs/fake.png")):
                    make_llm.return_value.generate_prompt.return_value = "draw me"
                    client = app.test_client()
                    save = client.post("/api/settings", json={
                        "workflow_controls": {workflow_path: controls},
                    })
                    self.assertTrue(save.json["success"])
                    self.assertEqual(save.json["settings"]["workflow_controls"][workflow_path],
                                     controls)
                    response = client.post("/api/generate", json={
                        "session_id": session_id,
                        "workflow_path": workflow_path,
                        "prompt": "测试",
                        "use_search": False, "use_history": False,
                        "preset_index": 0,
                        "workflow_controls": controls,
                    })
                    self.assertIn('"step": "done"', response.get_data(as_text=True))
                    job = generate.call_args.args[0]
                    self.assertTrue(job["resize"])
                    self.assertEqual(job["workflow_controls"], controls)
                    history = store.list_messages(session_id)
                    source = history[-1]
                    self.assertEqual(source["meta"]["workflow_controls"], controls)
                    self.assertEqual((source["meta"]["width"], source["meta"]["height"]),
                                     (640, 384))
                    settings.update({"workflow_controls": {workflow_path: {
                        "extra_loras": [{"lora_name": "changed.safetensors"}]}}})
                    regenerated = client.post("/api/generate", json={
                        "session_id": session_id,
                        "regenerate_message_id": source["id"],
                        "workflow_path": "different.json",
                        "preset_index": 1,
                    })
                    self.assertIn('"step": "done"', regenerated.get_data(as_text=True))
                    job = generate.call_args.args[0]
                    self.assertEqual(job["workflow_path"], workflow_path)
                    self.assertEqual(job["workflow_controls"], controls)
                    self.assertEqual((job["width"], job["height"]), (640, 384))
                    self.assertTrue(job["randomize_seed"])
                    make_llm.return_value.generate_prompt.assert_called_once()
                    self.assertEqual(store.list_messages(session_id)[-1]["meta"]
                                     ["workflow_controls"], controls)


if __name__ == "__main__":
    unittest.main()
