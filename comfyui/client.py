"""ComfyUI 客户端：负责提交工作流、轮询历史、下载生成图片。

新增能力：
- 尺寸覆盖：可传入 (width, height)，自动改写所有 EmptyLatentImage 节点，
  也支持用 width_placeholder / height_placeholder 做字符串占位替换。
"""
import json
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

        # 方式 1：字符串占位（如果 LLM 生成的文本里带 chang/gao 之类词）仅在占位符有意义时处理
        if width_ph or height_ph:
            ph_used = False
            if width_ph and str(width_ph) in json.dumps(wf):
                ph_used = True
            elif height_ph and str(height_ph) in json.dumps(wf):
                ph_used = True
            if ph_used and (width or height):
                wf_str = json.dumps(wf, ensure_ascii=False)
                if width_ph:
                    wf_str = wf_str.replace(width_ph, str(int(width)))
                if height_ph:
                    wf_str = wf_str.replace(height_ph, str(int(height)))
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
                 save_node_id: str = "9",
                 width: Optional[int] = None,
                 height: Optional[int] = None,
                 width_placeholder: str = "",
                 height_placeholder: str = "",
                 progress_cb=None) -> Optional[Path]:
        """替换占位符 + 设置尺寸 + 提交 + 轮询 + 下载图片。

        progress_cb: 可选回调 progress_cb(str)，等待期间周期回报进度文案。
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        wf = self.apply_prompt(workflow, prompt, placeholder)
        wf = self.apply_size(wf, width, height, width_placeholder, height_placeholder)

        prompt_id = self.submit(wf)
        return self._wait_and_download(prompt_id, save_node_id,
                                       progress_cb=progress_cb)

    def _wait_and_download(self, prompt_id: str,
                           save_node_id: str,
                           progress_cb=None) -> Optional[Path]:
        start = time.time()
        timeout = 300
        if progress_cb:
            progress_cb(f"已提交 ComfyUI，排队等待执行…")
        while time.time() - start < timeout:
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

            path = self._download_images(entry, save_node_id)
            if path:
                if progress_cb:
                    progress_cb("生成完成，正在保存图片…")
                return path
            # 若已执行完成但没匹配到图，退化为遍历所有输出
            if status.get("completed") or status.get("status_str") == "success":
                log.warning("ComfyUI 任务已完成但 save_node_id=%s 无图，"
                            "尝试遍历全部输出", save_node_id)
                path = self._download_images(entry, save_node_id, fallback=True)
                if path:
                    if progress_cb:
                        progress_cb("生成完成，正在保存图片…")
                    return path
                # 任务已完成但找不到任何图片 => 直接失败而非空转
                raise RuntimeError(
                    "ComfyUI 任务已完成但未返回图片，请检查 save_node_id 是否与"
                    "工作流中的 SaveImage 节点一致")

        log.error("ComfyUI 生图超时（%s 秒），prompt_id=%s", timeout, prompt_id)
        raise RuntimeError(f"ComfyUI 生图超时（{timeout} 秒），请检查工作流节点配置")

    def _download_images(self, entry: dict, save_node_id: str,
                         fallback: bool = False) -> Optional[Path]:
        outputs = entry.get("outputs", {})
        if not isinstance(outputs, dict):
            return None

        node_ids = []
        if save_node_id:
            node_ids.append(save_node_id)
        if fallback:
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
