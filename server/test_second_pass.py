import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app
from backends import BackendError, ComfyBackend
from settings import Settings
from store import Store
from test_workflow_controls import example_workflow
from workflow_controls import apply_loras, apply_second_pass, describe_workflow


def sampling_workflow(second_pass=False):
    workflow = example_workflow()
    workflow["19"]["inputs"].update(
        seed=123, steps=30, cfg=4, denoise=1, sampler_name="euler", scheduler="simple")
    if second_pass:
        workflow["200"] = {
            "class_type": "LatentUpscaleBy",
            "inputs": {"samples": ["19", 0], "scale_by": 2, "upscale_method": "bislerp"},
        }
        workflow["201"] = copy.deepcopy(workflow["19"])
        workflow["201"]["inputs"].update(latent_image=["200", 0], steps=15, denoise=0.3)
        workflow["8"]["inputs"]["samples"] = ["201", 0]
    return workflow


class SecondPassControlsTest(unittest.TestCase):
    def test_only_active_second_pass_and_its_upscaler_are_described(self):
        workflow = sampling_workflow(True)
        workflow["202"] = copy.deepcopy(workflow["201"])
        details = describe_workflow(workflow, "66")["second_pass"]
        self.assertEqual([node["id"] for node in details["samplers"]], ["201"])
        self.assertEqual([node["id"] for node in details["upscalers"]], ["200"])
        self.assertEqual(details["add_targets"], [])
        first_pass = describe_workflow(sampling_workflow(), "66")["second_pass"]
        self.assertEqual(first_pass["samplers"], [])
        self.assertEqual(first_pass["add_targets"][0]["decoder_ids"], ["8"])

    def test_image_decode_upscale_encode_chain_is_recognized(self):
        workflow = sampling_workflow(True)
        workflow["203"] = {"class_type": "VAEDecode", "inputs": {"samples": ["19", 0]}}
        workflow["204"] = {"class_type": "ImageScale",
                           "inputs": {"image": ["203", 0], "width": 2048, "height": 1536,
                                      "upscale_method": "lanczos", "crop": "disabled"}}
        workflow["205"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["204", 0]}}
        workflow["201"]["inputs"]["latent_image"] = ["205", 0]
        description = describe_workflow(workflow, "66")
        self.assertEqual([node["id"] for node in description["second_pass"]["upscalers"]],
                         ["204"])
        before = copy.deepcopy(workflow["19"])
        apply_second_pass(workflow, description, {
            "mode": "existing", "nodes": {"204": {"width": "2560", "height": "1920"},
                                          "201": {"steps": "25", "denoise": "0.45"}},
        })
        self.assertEqual(workflow["204"]["inputs"]["width"], 2560)
        self.assertEqual(workflow["201"]["inputs"]["denoise"], 0.45)
        self.assertEqual(workflow["19"], before)

    def test_later_passes_are_included_without_traversing_conditioning_as_latent(self):
        workflow = sampling_workflow(True)
        workflow["203"] = copy.deepcopy(workflow["201"])
        workflow["203"]["inputs"]["latent_image"] = ["201", 0]
        workflow["8"]["inputs"]["samples"] = ["203", 0]
        workflow["210"] = copy.deepcopy(workflow["19"])
        workflow["210"]["inputs"]["latent_image"] = ["28", 0]
        workflow["211"] = {"class_type": "ReferenceLatent",
                           "inputs": {"conditioning": ["12", 0], "latent": ["210", 0]}}
        workflow["19"]["inputs"]["negative"] = ["211", 0]
        details = describe_workflow(workflow, "66")["second_pass"]
        self.assertEqual([node["id"] for node in details["samplers"]], ["201", "203"])

    def test_original_mode_does_not_change_workflow(self):
        workflow = sampling_workflow(True)
        before = copy.deepcopy(workflow)
        apply_second_pass(workflow, describe_workflow(workflow, "66"), {
            "mode": "original", "nodes": {"201": {"steps": 100}},
        })
        self.assertEqual(workflow, before)

    def test_linked_inputs_and_first_pass_cannot_be_overwritten(self):
        for node_id, values in (("19", {"steps": 10}), ("100", {"width": 640}),
                                ("201", {"cfg": 5})):
            with self.subTest(node_id=node_id):
                workflow = sampling_workflow(True)
                workflow["300"] = {"class_type": "PrimitiveFloat", "inputs": {"value": 4}}
                workflow["201"]["inputs"]["cfg"] = ["300", 0]
                before = copy.deepcopy(workflow)
                with self.assertRaises(ValueError):
                    apply_second_pass(workflow, describe_workflow(workflow, "66"), {
                        "mode": "existing", "nodes": {node_id: values},
                    })
                self.assertEqual(workflow, before)

    def test_invalid_values_fail_atomically(self):
        for key, value in (("steps", 1.5), ("steps", True), ("steps", 0),
                           ("denoise", 1.1), ("cfg", "nan"), ("cfg", float("inf")),
                           ("scheduler", ""), ("seed", 10)):
            with self.subTest(key=key, value=value):
                workflow = sampling_workflow(True)
                before = copy.deepcopy(workflow)
                with self.assertRaises(ValueError):
                    apply_second_pass(workflow, describe_workflow(workflow, "66"), {
                        "mode": "existing", "nodes": {"200": {"scale_by": 1.5},
                                                      "201": {key: value}},
                    })
                self.assertEqual(workflow, before)

    def test_advanced_sampler_preserves_noise_and_step_controls(self):
        workflow = sampling_workflow(True)
        sampler = workflow["201"]
        sampler["class_type"] = "KSamplerAdvanced"
        sampler["inputs"].pop("seed")
        sampler["inputs"].pop("denoise")
        sampler["inputs"].update(noise_seed=123, start_at_step=5, end_at_step=30,
                                 add_noise="enable", return_with_leftover_noise="disable")
        description = describe_workflow(workflow, "66")
        apply_second_pass(workflow, description, {
            "mode": "existing", "nodes": {"201": {"start_at_step": 8,
                                                 "end_at_step": 25, "add_noise": "disable"}},
        })
        self.assertEqual(sampler["inputs"]["start_at_step"], 8)
        self.assertEqual(sampler["inputs"]["noise_seed"], 123)
        self.assertNotIn("denoise", sampler["inputs"])
        with self.assertRaisesRegex(ValueError, "开始步数"):
            apply_second_pass(workflow, description, {
                "mode": "existing", "nodes": {"201": {"start_at_step": 26}},
            })

    def test_added_pass_inherits_loras_conditioning_and_only_changes_active_decoder(self):
        workflow = sampling_workflow()
        workflow["88"] = copy.deepcopy(workflow["8"])
        workflow["89"] = {"class_type": "SaveImage", "inputs": {"images": ["88", 0]}}
        description = describe_workflow(workflow, "66")
        apply_loras(workflow, description, {}, [
            {"lora_name": "a.safetensors", "strength_model": 0.8},
            {"lora_name": "b.safetensors", "strength_model": 0.6},
        ], "19")
        first = copy.deepcopy(workflow["19"])
        apply_second_pass(workflow, description, {
            "mode": "add", "source_sampler_id": "19",
            "parameters": {"scale_by": 2, "steps": 18, "cfg": 3.5, "denoise": 0.4,
                           "sampler_name": "dpmpp_2m", "scheduler": "karras"},
        })
        second = workflow[workflow["8"]["inputs"]["samples"][0]]
        upscale = workflow[second["inputs"]["latent_image"][0]]
        self.assertEqual(second["class_type"], "KSampler")
        for key in ("model", "positive", "negative", "seed"):
            self.assertEqual(second["inputs"][key], first["inputs"][key])
        self.assertEqual(second["inputs"]["denoise"], 0.4)
        self.assertEqual(upscale["inputs"]["samples"], ["19", 0])
        self.assertEqual(upscale["inputs"]["scale_by"], 2)
        self.assertEqual(workflow["19"], first)
        self.assertEqual(workflow["88"]["inputs"]["samples"], ["19", 0])

    def test_invalid_addition_and_unsupported_graph_do_not_change_workflow(self):
        for controls in (
            {"mode": "unknown"}, {"mode": "add", "source_sampler_id": "28"},
            {"mode": "add", "source_sampler_id": "19", "parameters": {"scale_by": 9}},
            {"mode": "add", "source_sampler_id": "19", "parameters": {"upscale_method": "bad"}},
            {"mode": "add", "source_sampler_id": "19", "parameters": []},
        ):
            with self.subTest(controls=controls):
                workflow = sampling_workflow()
                before = copy.deepcopy(workflow)
                with self.assertRaises(ValueError):
                    apply_second_pass(workflow, describe_workflow(workflow, "66"), controls)
                self.assertEqual(workflow, before)
        workflow = sampling_workflow(True)
        with self.assertRaises(ValueError):
            apply_second_pass(workflow, describe_workflow(workflow, "66"), {
                "mode": "add", "source_sampler_id": "19",
            })
        workflow = sampling_workflow()
        workflow["8"]["inputs"]["samples"] = ["19", 1]
        self.assertEqual(describe_workflow(workflow, "66")["second_pass"]["add_targets"], [])


class SecondPassIntegrationTest(unittest.TestCase):
    def test_backend_addition_resizes_first_pass_and_preserves_source_file(self):
        original = sampling_workflow()
        before = copy.deepcopy(original)
        backend = ComfyBackend({"comfyui_url": "http://127.0.0.1:8188", "save_node_id": "66"})
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
                    "resize": True, "randomize_seed": True,
                    "workflow_controls": {"second_pass": {
                        "mode": "add", "source_sampler_id": "19",
                        "parameters": {"scale_by": 2, "denoise": 0.4},
                    }},
                }
                result = backend.generate(job)
                sent = submit.call_args.args[0]
                second = sent[sent["8"]["inputs"]["samples"][0]]
                self.assertEqual(sent["100"]["inputs"]["width"], 640)
                self.assertEqual(second["inputs"]["denoise"], 0.4)
                self.assertNotEqual(second["inputs"]["seed"], 123)
                self.assertEqual(result["base_size"], [640, 384])
                self.assertEqual(original, before)
                submit.reset_mock()
                with self.assertRaises(BackendError):
                    backend.generate({**job, "workflow_controls": {"second_pass": []}})
                submit.assert_not_called()

    def test_inspection_settings_and_regeneration_keep_second_pass_snapshot(self):
        with (tempfile.TemporaryDirectory() as directory,
              patch("settings.DATA_DIR", Path(directory)),
              patch("settings.SETTINGS_FILE", Path(directory) / "settings.json")):
            settings = Settings()
            store = Store(Path(directory) / "history.db")
            workflow_file = Path(directory) / "workflow.json"
            workflow_file.write_text("{}", encoding="utf-8")
            path = str(workflow_file)
            controls = {"second_pass": {
                "mode": "add", "source_sampler_id": "19",
                "parameters": {"scale_by": 1.5, "denoise": 0.4, "steps": 18},
            }}
            settings.update({"access_password": "", "workflow_path": path,
                             "use_character_db": False, "positive_prompt_prefix": "",
                             "workflow_controls": {path: controls},
                             "resolution_presets": [{"width": 640, "height": 384}]})
            with (patch("app.settings", settings), patch("app.store", store),
                  patch("app.engine.inspect_workflow",
                        return_value=describe_workflow(sampling_workflow(), "66")),
                  patch("app._make_llm") as llm,
                  patch("app.engine.generate", return_value={"image": "image-data"}) as generate,
                  patch("app._save_data_url", return_value="/outputs/test.png")):
                llm.return_value.generate_prompt.return_value = "test prompt"
                client = app.test_client()
                inspected = client.get("/api/workflows/inspect", query_string={"path": path})
                self.assertEqual(inspected.json["second_pass"]["add_targets"][0]["id"], "19")
                saved = client.post("/api/settings", json={"workflow_controls": {path: controls}})
                self.assertTrue(saved.json["success"])
                session_id = store.create_session("二采")["id"]
                response = client.post("/api/generate", json={
                    "session_id": session_id, "prompt": "测试", "use_search": False,
                    "use_history": False, "preset_index": 0, "workflow_controls": controls,
                })
                self.assertIn('"step": "done"', response.get_data(as_text=True))
                self.assertEqual(generate.call_args.args[0]["workflow_controls"], controls)
                source = store.list_messages(session_id)[-1]
                settings.update({"workflow_controls": {path: {"second_pass": {"mode": "original"}}}})
                regenerated = client.post("/api/generate", json={
                    "session_id": session_id, "regenerate_message_id": source["id"],
                })
                self.assertIn('"step": "done"', regenerated.get_data(as_text=True))
                self.assertEqual(generate.call_args.args[0]["workflow_controls"], controls)
                llm.return_value.generate_prompt.assert_called_once()


if __name__ == "__main__":
    unittest.main()
