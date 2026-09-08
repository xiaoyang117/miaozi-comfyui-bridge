"""ComfyUI 客户端：负责提交工作流、轮询历史、下载生成图片。

新增能力：
- 尺寸覆盖：可传入 (width, height)，自动改写所有 EmptyLatentImage 节点，
  也支持用 width_placeholder / height_placeholder 做字符串占位替换。
"""
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import requests

from logger import get_logger

log = get_logger("comfyui")

# 尺寸相关的 ComfyUI 节点类型（它们的 inputs 里是整数宽高，非字符串）
_LATENT_CLASSES = {"EmptyLatentImage", "EmptySD3LatentImage", "EmptyFluxLatentImage"}
# 尺寸节点在 inputs 中的字段名
_WIDTH_FIELD = "width"
_HEIGHT_FIELD = "height"


class ComfyUIClient:
    def __init__(self, server_url: str = "", output_dir: Optional[Path] = None):
        self.server_url = server_url.rstrip("/")
        self.client_id = str(uuid.uuid4())
        self.output_dir = output_dir or (Path(__file__).parent.parent / "outputs")
        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=1, pool_maxsize=1, max_retries=0)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)
        # 本机回环地址不走系统代理（全局代理会把 localhost 回环流量转发而挂起）
        host = self.server_url.split("//", 1)[-1].split("/", 1)[0].split(":")[0] \
            if "//" in self.server_url else ""
        if host in ("127.0.0.1", "localhost", "::1", ""):
            self._session.trust_env = False

    # ------------------------------------------------------------------ #
    # 工作流处理
    # ------------------------------------------------------------------ #
    @staticmethod
    def load_workflow(path: str) -> dict:
        """读取工作流 JSON，返回可用于 /prompt 提交的 prompt 字典。

        兼容三种格式：
        1. {"prompt": {...}}              -> API 导出格式
        2. {"nodes": [...], "links": ...}  -> Canvas 格式（抛错提示）
        3. {...}                           -> 直接就是 prompt 格式
        """
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict) and "prompt" in data:
            return data["prompt"]

        if isinstance(data, dict) and "nodes" in data:
            raise RuntimeError(
                "工作流文件是 Canvas 格式，请使用 ComfyUI 的 'Save (API Format)' "
                "导出 API 格式后再试")

        if not isinstance(data, dict):
            raise RuntimeError("工作流文件格式不正确")
        return data

    def apply_prompt(self, workflow: dict, prompt_text: str,
                     placeholder: str = "") -> dict:
        """把提示词文本安全替换进工作流的占位符，返回新的工作流副本。"""
        wf = json.loads(json.dumps(workflow, ensure_ascii=False))
        if placeholder:
            # 遍历所有节点文本字段做替换
            wf_str = json.dumps(wf, ensure_ascii=False)
            escaped = json.dumps(prompt_text, ensure_ascii=False)[1:-1]
            wf_str = wf_str.replace(placeholder, escaped)
            wf = json.loads(wf_str)
        return wf

    @staticmethod
    def _ph_sub(text: str, ph: str, value) -> str:
        """按「独立 token」替换占位符，避免子串误伤。

        例：占位符 chang 不应命中提示词里的 changing。
        """
        if not ph:
            return text
        return re.sub(
            r"(?<![A-Za-z0-9_])" + re.escape(ph) + r"(?![A-Za-z0-9_])",
            str(value), text)

    @staticmethod
    def _ph_hit(text: str, ph: str) -> bool:
        """占位符是否作为独立 token 出现在文本里。"""
        if not ph:
            return False
        return re.search(
            r"(?<![A-Za-z0-9_])" + re.escape(ph) + r"(?![A-Za-z0-9_])",
            text) is not None

    def apply_size(self, workflow: dict, width: Optional[int],
                   height: Optional[int],
                   width_ph: str = "", height_ph: str = "") -> dict:
        """按需改写尺寸。

        优先级：
        1. 若存在 width_placeholder/height_placeholder 且节点文本里含这些占位词，
           则按字符串占位替换（针对把尺寸写进正向提示词文本的工作流）。
        2. 否则改写所有 EmptyLatentImage 系列节点的 width/height。
        返回新副本；不修改传入对象。
        """
        wf = json.loads(json.dumps(workflow, ensure_ascii=False))

        # 方式 1：字符串占位替换。
        # 注意：调用方通常先做了 apply_prompt，此时 LLM 提示词已进入工作流 JSON，
        # 因此必须用「独立 token」匹配，否则 chang 会命中 changing、gao 命中 gaokao，
        # 把提示词改烂（且会误判为"工作流用了占位符"从而跳过方式 2）。
        if width_ph or height_ph:
            wf_str = json.dumps(wf, ensure_ascii=False)
            ph_used = self._ph_hit(wf_str, width_ph) or \
                self._ph_hit(wf_str, height_ph)
            if ph_used and (width or height):
                if width and width_ph:
                    wf_str = self._ph_sub(wf_str, width_ph, int(width))
                if height and height_ph:
                    wf_str = self._ph_sub(wf_str, height_ph, int(height))
                return json.loads(wf_str)

        # 方式 2：改写 EmptyLatentImage 系列节点
        if width or height:
            for node in wf.values():
                if not isinstance(node, dict):
                    continue
                cls = node.get("class_type")
                inputs = node.get("inputs")
                if cls in _LATENT_CLASSES and isinstance(inputs, dict):
                    if width:
                        inputs[_WIDTH_FIELD] = int(width)
                    if height:
                        inputs[_HEIGHT_FIELD] = int(height)
        return wf

    # ------------------------------------------------------------------ #
    # 提交与轮询
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # 图片上传 / img2img 二采
    # ------------------------------------------------------------------ #
    def upload_image(self, file_bytes: bytes, filename: str) -> bool:
        """把图片上传到 ComfyUI input 目录（POST /upload/image）。

        filename 仅取 basename，需为安全文件名（app 层负责生成）。
        返回是否成功。
        """
        import os as _os
        safe = _os.path.basename(filename or "")
        if not safe:
            return False
        try:
            r = self._session.post(
                f"{self.server_url}/upload/image",
                files={"image": (safe, file_bytes,
                                 "image/png")},
                data={"overwrite": "true"},
                timeout=60)
            return r.status_code == 200
        except requests.RequestException as e:
            log.warning("上传图片到 ComfyUI 失败: %s", e)
            return False

    def generate_img2img(self, workflow: dict, prompt: str,
                         input_filename: str,
                         scale: float = 2.0, denoise: float = 0.5,
                         seed: Optional[int] = None,
                         placeholder: str = "UPSCALE_PROMPT_PH",
                         progress_cb=None) -> Optional[Path]:
        """img2img 二采：替换模板占位符 -> 设置放大/denoise/输入图 -> 提交下载。

        模板约定：
          - LoadImage.image  = "INPUT_IMAGE_PH"
          - ImageScaleBy.scale_by = 数值 2.0
          - KSampler.denoise = 数值 0.5
          - KSampler.seed    = 数值（未传则随机，保证重复二采结果不同）

        提示词注入：
          - 若模板里存在 placeholder（默认 UPSCALE_PROMPT_PH）则替换之；
          - 否则若传入了 prompt，则追加到 KSampler.positive 指向的正向
            CLIPTextEncode 文本末尾（避免 prompt 被静默丢弃）。
        """
        import copy
        import random as _rnd
        # seed 未指定时随机：ComfyUI 的 seed=0 并不是"随机"，固定 0 会导致
        # 同一张图用同样参数反复二采得到完全相同的结果。
        real_seed = int(seed) if seed is not None else _rnd.randint(1, 2**63 - 1)
        wf = copy.deepcopy(workflow)
        ph_hit = False
        for nid, node in wf.items():
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            ct = node.get("class_type", "")
            for k, v in inputs.items():
                if isinstance(v, str) and placeholder in v:
                    inputs[k] = v.replace(placeholder, prompt or "")
                    ph_hit = True
                elif k == "image" and v == "INPUT_IMAGE_PH":
                    inputs[k] = input_filename
                elif ct == "ImageScaleBy" and k == "scale_by":
                    inputs[k] = float(scale)
                elif ct == "LatentUpscaleBy" and k == "scale_by":
                    inputs[k] = float(scale)
                elif ct == "KSampler" and k == "denoise":
                    inputs[k] = float(denoise)
                elif ct == "KSampler" and k == "seed":
                    inputs[k] = real_seed
        if prompt and not ph_hit:
            self._append_positive_prompt(wf, prompt)
        prompt_id = self.submit(wf)
        node_ids = self._detect_image_node_ids(wf)
        return self._wait_and_download(prompt_id, node_ids,
                                       progress_cb=progress_cb)

    @staticmethod
    def _append_positive_prompt(wf: dict, prompt: str) -> bool:
        """把 prompt 追加到 KSampler.positive 指向的正向文本节点末尾。

        模板没有占位符时使用，保证额外提示词不会被静默丢弃。
        返回是否成功注入。
        """
        if not prompt:
            return False
        for node in wf.values():
            if not isinstance(node, dict):
                continue
            if not str(node.get("class_type") or "").startswith("KSampler"):
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            pos = inputs.get("positive")
            # positive 形如 ["11", 0]
            if not (isinstance(pos, list) and pos):
                continue
            tgt = wf.get(str(pos[0]))
            if not isinstance(tgt, dict):
                continue
            tin = tgt.get("inputs")
            if not (isinstance(tin, dict) and isinstance(tin.get("text"), str)):
                continue
            old = tin["text"].rstrip()
            tin["text"] = (old + ", " + prompt) if old else prompt
            log.info("二采模板无占位符，已把额外提示词追加到节点 %s", pos[0])
            return True
        return False

    def submit(self, workflow: dict) -> str:
        resp = self._session.post(
            f"{self.server_url}/prompt",
            json={"prompt": workflow, "client_id": self.client_id},
            timeout=30,
        )
        try:
            body = resp.json()
        except Exception:
            body = {}

        if "error" in body:
            err = body["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            detail = ""
            if isinstance(err, dict) and err.get("node_errors"):
                detail = "\n节点错误:\n" + json.dumps(
                    err["node_errors"], ensure_ascii=False, indent=2)[:1500]
                log.error("ComfyUI 提交失败，节点错误详情: %s", detail)
            raise RuntimeError(f"ComfyUI 提交工作流失败: {msg}{detail}")

        resp.raise_for_status()
        prompt_id = body.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(
                f"ComfyUI 未返回 prompt_id:\n"
                f"{json.dumps(body, ensure_ascii=False)[:500]}")
        return prompt_id

    def generate(self, workflow: dict, prompt: str,
                 placeholder: str = "114514.1919810",
                 save_node_id: str = "",
                 width: Optional[int] = None,
                 height: Optional[int] = None,
                 width_placeholder: str = "",
                 height_placeholder: str = "",
                 progress_cb=None,
                 cancel: Optional[threading.Event] = None) -> Optional[Path]:
        """替换占位符 + 设置尺寸 + 提交 + 轮询 + 下载图片。

        save_node_id 已无需手动配置：自动探测工作流里所有会输出图片的节点
        （SaveImage / SaveAnimatedWEBP / PreviewImage 等）作为下载候选，
        逐个尝试、谁有图用谁。save_node_id 仅作为附加的优先候选保留兼容。

        progress_cb: 可选回调 progress_cb(str)，等待期间周期回报进度文案。
        cancel: 可选 threading.Event，置位后中止等待轮询。
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        wf = self.apply_prompt(workflow, prompt, placeholder)
        wf = self.apply_size(wf, width, height, width_placeholder, height_placeholder)
        # 随机 KSampler seed：工作流通常带固定 seed，若不替换则同样 prompt 每次
        # 出图完全一样，"再次生成/换一张"会失效。
        wf = self.randomize_seed(wf)

        prompt_id = self.submit(wf)
        node_ids = self._detect_image_node_ids(wf, save_node_id)
        log.info("输出节点候选: %s (配置: %r)", node_ids, save_node_id)
        return self._wait_and_download(prompt_id, node_ids,
                                       progress_cb=progress_cb,
                                       cancel=cancel)

    @staticmethod
    def randomize_seed(workflow: dict, seed: Optional[int] = None) -> dict:
        """把工作流里所有 KSampler 类节点的 seed 随机化。

        返回新副本；不修改传入对象。seed=None 时用随机大整数。
        """
        import random as _rnd
        wf = json.loads(json.dumps(workflow, ensure_ascii=False))
        real_seed = seed if seed is not None else _rnd.randint(1, 2**63 - 1)
        _SAMPLE_CLS = ("KSampler", "KSamplerAdvanced", "SamplerCustom",
                       "SamplerCustomAdvanced")
        for node in wf.values():
            if not isinstance(node, dict):
                continue
            ct = node.get("class_type", "")
            inputs = node.get("inputs")
            if ct in _SAMPLE_CLS and isinstance(inputs, dict) \
                    and "seed" in inputs:
                inputs["seed"] = int(real_seed)
        return wf

    def _detect_image_node_ids(self, wf: dict,
                               configured: str = "") -> list:
        """自动探测工作流里所有可能输出图片的节点 ID。

        判定：class_type 以 Save/Preview 开头（SaveImage、SaveAnimatedWEBP、
        PreviewImage 等），或 inputs 里含 filename_prefix（保存类节点的标志）。
        返回有序去重候选：配置值优先（兼容旧行为），随后是探测到的节点。
        """
        cands: list[str] = []
        seen = set()
        def _add(nid):
            s = str(nid)
            if s and s not in seen:
                seen.add(s)
                cands.append(s)
        if configured:
            _add(configured)
        for nid, node in (wf or {}).items():
            if not isinstance(node, dict):
                continue
            ct = str(node.get("class_type") or "")
            inputs = node.get("inputs")
            if (ct.startswith("Save") or ct.startswith("Preview")
                    or (isinstance(inputs, dict)
                        and "filename_prefix" in inputs)):
                _add(nid)
        return cands

    def _wait_and_download(self, prompt_id: str,
                           node_ids: list,
                           progress_cb=None,
                           timeout: float = 600,
                           cancel: Optional[threading.Event] = None) -> Optional[Path]:
        """等待 ComfyUI 执行完成并下载图片。

        timeout 默认 600 秒：img2img 放大 + 重采样在图较大时偏慢，
        300 秒容易误杀；普通文生图通常 1~2 分钟内完成。

        cancel: 可选 threading.Event，置位后立即中止等待（用于客户端断开时
        及时释放，避免后台线程继续空转轮询）。
        """
        start = time.time()
        if progress_cb:
            progress_cb(f"已提交 ComfyUI，排队等待执行…")
        while time.time() - start < timeout:
            if cancel is not None and cancel.is_set():
                log.info("等待被取消，停止轮询 prompt_id=%s", prompt_id)
                return None
            time.sleep(1)
            # 周期回报：执行中已等待秒数
            if progress_cb and int(time.time() - start) % 2 == 0:
                progress_cb(f"ComfyUI 生成中… 已等待 {int(time.time()-start)}s")
            try:
                r = self._session.get(
                    f"{self.server_url}/history/{prompt_id}", timeout=10)
            except requests.RequestException:
                if progress_cb:
                    progress_cb("查询 ComfyUI 状态失败，正在重试…")
                continue
            if r.status_code != 200:
                continue
            try:
                history = r.json()
            except Exception:
                continue
            if prompt_id not in history:
                continue

            entry = history[prompt_id]
            status = entry.get("status", {})
            for msg_type, msg_data in status.get("messages", []):
                if msg_type == "execution_error":
                    err_msg = msg_data.get("message", str(msg_data))
                    nid = msg_data.get("node_id", "?")
                    ntype = msg_data.get("node_type", "?")
                    log.error("ComfyUI 节点 #%s (%s) 执行错误: %s",
                              nid, ntype, err_msg)
                    raise RuntimeError(
                        f"ComfyUI 节点 #{nid} ({ntype}) 执行错误:\n{err_msg}")

            path = self._download_images(entry, node_ids)
            if path:
                if progress_cb:
                    progress_cb("生成完成，正在保存图片…")
                return path
            # 若已执行完成但候选节点都没图，退化为遍历全部输出
            if status.get("completed") or status.get("status_str") == "success":
                log.warning("ComfyUI 任务已完成但候选输出节点 %s 无图，"
                            "尝试遍历全部输出", node_ids)
                path = self._download_images(entry, node_ids, fallback=True)
                if path:
                    if progress_cb:
                        progress_cb("生成完成，正在保存图片…")
                    return path
                # 任务已完成但找不到任何图片 => 直接失败而非空转
                raise RuntimeError(
                    "ComfyUI 任务已完成但未返回图片，请检查工作流中是否有"
                    "SaveImage 等图片输出节点")

        log.error("ComfyUI 生图超时（%s 秒），prompt_id=%s", timeout, prompt_id)
        raise RuntimeError(f"ComfyUI 生图超时（{timeout} 秒），请检查工作流节点配置")

    def _download_images(self, entry: dict, node_ids: list,
                         fallback: bool = False) -> Optional[Path]:
        outputs = entry.get("outputs", {})
        if not isinstance(outputs, dict):
            return None

        if fallback:
            node_ids = list(outputs.keys())
        if not node_ids:
            node_ids = list(outputs.keys())

        for nid in node_ids:
            node_out = outputs.get(str(nid)) or outputs.get(nid)
            if not node_out:
                continue
            for img in node_out.get("images", []) or []:
                try:
                    ir = self._session.get(
                        f"{self.server_url}/view",
                        params={
                            "filename": img["filename"],
                            "subfolder": img.get("subfolder", ""),
                            "type": img.get("type", "output"),
                        },
                        timeout=30,
                    )
                    ir.raise_for_status()
                except requests.RequestException:
                    continue
                # 用 时间戳_文件名 保存，避免并发覆盖
                path = self.output_dir / f"{int(time.time()*1000)}_{img['filename']}"
                path.write_bytes(ir.content)
                return path
        return None

    def test_connection(self) -> str:
        try:
            r = self._session.get(f"{self.server_url}/object_info", timeout=10)
            if r.ok:
                return "ok"
            return f"HTTP {r.status_code}"
        except requests.exceptions.ConnectionError:
            return "连接失败"
        except Exception as e:
            return str(e)
