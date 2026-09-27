"""本地执行后端：ComfyUI（走 MCP）与本地 LLM。

ComfyUI 这里优先用 comfy-mcp 的 tools/call 驱动；如果服务端版本不同、
拿不到输出文件，就退回直接读 ComfyUI 的 HTTP 接口（和旧版做法一样）。
两条路都不通才报错，尽量别让你卡在版本差异上。
"""

import base64
import glob
import json
import mimetypes
import os
import tempfile
import time
import uuid
from pathlib import Path

import requests

MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}


def local_session() -> requests.Session:
    """访问本机服务的会话：绕开系统代理。

    本机常驻 Clash / v2ray 时，环境里的 http_proxy 会让 requests
    把发往 127.0.0.1 的请求也送进代理，导致连不上自己的 ComfyUI /
    Ollama。这里显式关掉。
    """
    s = requests.Session()
    s.trust_env = False
    s.proxies = {}
    return s


class BackendError(RuntimeError):
    pass


# ============================================================ ComfyUI

class ComfyBackend:
    def __init__(self, cfg: dict, mcp=None, log=print) -> None:
        self.cfg = cfg or {}
        self.mcp = mcp
        self.log = log
        self.http_url = str(self.cfg.get("comfyui_url") or "").rstrip("/")
        self.save_node_id = str(self.cfg.get("save_node_id") or "")
        self.poll_interval = float(self.cfg.get("poll_interval") or 1.5)
        self.session = local_session()

    # ---------- 工具名解析 ----------

    def _tool(self, *candidates) -> str:
        if self.mcp is None:
            raise BackendError("MCP 未连接")
        return self.mcp.find_tool(candidates)

    # ---------- 探测 ----------

    def probe(self) -> dict:
        """返回 {ok, body, source}。用于「测试连接」。"""
        if self.mcp is not None:
            try:
                name = self._tool("server_info", "get_server_info",
                                  "comfy_server_info")
                res = self.mcp.call_tool(name, {}, timeout=30)
                return {"ok": True, "source": f"mcp:{name}",
                        "body": res.get("text", "")[:1500]}
            except Exception as e:
                self.log(f"[warn] MCP server_info 失败：{e}")

        # 退回 HTTP
        if self.http_url:
            try:
                r = self.session.get(f"{self.http_url}/system_stats", timeout=8)
                if r.ok:
                    try:
                        body = json.dumps(r.json(), ensure_ascii=False)[:1200]
                    except Exception:
                        body = r.text[:1200]
                    return {"ok": True, "source": "http", "body": body}
                return {"ok": False, "source": "http",
                        "error": f"ComfyUI 返回 HTTP {r.status_code}"}
            except requests.exceptions.ConnectionError:
                return {"ok": False, "source": "http",
                        "error": f"连不上 ComfyUI（{self.http_url}），"
                                 f"请确认它已经启动"}
            except Exception as e:
                return {"ok": False, "source": "http", "error": str(e)}
        return {"ok": False, "source": "none",
                "error": "既没有 MCP 也没有配置 ComfyUI 地址"}

    # ---------- 工作流 ----------

    def list_workflows(self, extra_dir: str = "") -> dict:
        dirs = []
        cfg_dirs = self.cfg.get("workflow_dirs") or []
        if isinstance(cfg_dirs, str):
            cfg_dirs = [cfg_dirs]
        for d in cfg_dirs:
            if d:
                dirs.append(os.path.expanduser(str(d)))
        if extra_dir:
            p = os.path.expanduser(str(extra_dir))
            dirs.append(p if os.path.isdir(p) else os.path.dirname(p))

        items = []
        seen = set()
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for f in sorted(glob.glob(os.path.join(d, "*.json"))):
                ap = os.path.abspath(f)
                if ap in seen:
                    continue
                seen.add(ap)
                items.append({"path": ap, "name": os.path.basename(f),
                              "dir": d})
        return {"workflows": items, "dir": " | ".join(dirs)}

    def _load_workflow(self, path: str) -> dict:
        p = os.path.expanduser(str(path or ""))
        if not p or not os.path.exists(p):
            raise BackendError(f"工作流文件不存在：{path}")
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise BackendError(
                f"工作流不是合法 JSON（{os.path.basename(p)}）："
                f"第 {e.lineno} 行第 {e.colno} 列。"
                f"常见原因：手动改了文件、或把占位符写成了裸值"
                f"（占位符要写成字符串，例如 \"width\":\"PH_W\"）")
        if isinstance(data, dict) and "prompt" in data:
            data = data["prompt"]
        if isinstance(data, dict) and "nodes" in data:
            raise BackendError(
                "工作流是画布格式。请在 ComfyUI 里用 "
                "「Save (API Format)」重新导出。")
        if not isinstance(data, dict):
            raise BackendError("工作流 JSON 格式不正确")
        return data

    @staticmethod
    def _json_token(value) -> str:
        return json.dumps(value, ensure_ascii=False)

    def _apply_replacements(self, workflow: dict, replacements: dict) -> dict:
        """按 JSON 令牌替换占位符，字符串/数字都正确处理。

        两种情况都要覆盖：
          文本节点  "text": "114514.1919810"   -> "text": "实际的提示词"
          宽高节点  "width": "PH_W"            -> "width": 896

        直接做纯文本替换的话，"PH_W" 的位置会被换成裸的 896，但外层
        引号还在，结果变成 "width": "896" —— ComfyUI 会拒收字符串。
        所以这里同时对「带引号」和「不带引号」两种写法做替换：
        带引号的替换成 JSON 字面量，不带引号的替换成字面量的裸值。
        """
        raw = json.dumps(workflow, ensure_ascii=False)
        for key, value in (replacements or {}).items():
            if not key:
                continue
            literal = self._json_token(value)
            if isinstance(value, str):
                # 文本占位符：只替换引号内的部分，保留外层引号
                raw = raw.replace('"' + key + '"', literal)
                raw = raw.replace(key, literal[1:-1])
            else:
                # 数字占位符：整体吃掉落引号，写入裸值
                raw = raw.replace('"' + key + '"', literal)
                raw = raw.replace(key, literal)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise BackendError(
                f"替换占位符后工作流 JSON 损坏：第 {e.lineno} 行"
                f"第 {e.colno} 列。请确认占位符写在了字符串里"
                f"（如 \"text\":\"占位符\"），且宽度/高度占位符"
                f"没有被写成裸值。")

    # ---------- 生成 ----------

    def generate(self, job: dict) -> dict:
        started = time.time()
        workflow_path = job.get("workflow_path") or self.cfg.get("workflow_path")
        workflow = self._load_workflow(workflow_path)
        prompt = job.get("prompt") or ""
        replacements = dict(job.get("replacements") or {})
        if not replacements and prompt:
            ph = str(self.cfg.get("prompt_placeholder") or "").strip()
            if ph:
                replacements[ph] = prompt
        workflow = self._apply_replacements(workflow, replacements)

        timeout = float(job.get("timeout") or self.cfg.get("run_timeout") or 600)
        tmp_root = Path(tempfile.mkdtemp(prefix="miaozi_"))
        wf_file = tmp_root / "workflow.json"
        with open(wf_file, "w", encoding="utf-8") as f:
            json.dump(workflow, f, ensure_ascii=False)

        prompt_id = None
        mcp_error = None

        # --- 优先 MCP ---
        if self.mcp is not None:
            try:
                probe = self.probe()
                if not probe.get("ok"):
                    raise BackendError(probe.get("error") or "ComfyUI 未就绪")
                vname = None
                if self.mcp.has_tool("validate_workflow"):
                    vname = "validate_workflow"
                elif self.mcp.tools:
                    for t in self.mcp.tool_names():
                        if "validate" in t:
                            vname = t
                            break
                if vname:
                    try:
                        self.mcp.call_tool(vname, {"workflow_path": str(wf_file)},
                                           timeout=60)
                    except Exception as e:
                        self.log(f"[warn] 工作流校验提示：{e}")

                rname = self._tool("run_workflow", "comfy_run_workflow",
                                   "submit_workflow")
                res = self.mcp.call_tool(
                    rname,
                    {"workflow_path": str(wf_file), "wait": False},
                    timeout=timeout)
                prompt_id = self._extract_prompt_id(res)
                if not prompt_id:
                    raise BackendError(
                        "MCP 没有返回 prompt_id，无法取结果："
                        + (res.get("text") or "")[:200])
                self.log(f"[comfy] 已提交 prompt_id={prompt_id}")
            except Exception as e:
                mcp_error = str(e)
                self.log(f"[warn] MCP 路径失败，尝试 HTTP 回退：{e}")
                prompt_id = None

        # --- 回退 HTTP ---
        if not prompt_id:
            if not self.http_url:
                raise BackendError(
                    f"ComfyUI 提交失败：{mcp_error or '未配置 comfyui_url'}")

        image_path = None
        outputs_dir = tmp_root / "out"

        if prompt_id and self.mcp is not None:
            try:
                image_path = self._fetch_via_mcp(prompt_id, outputs_dir,
                                                 timeout)
            except Exception as e:
                self.log(f"[warn] MCP 取结果失败：{e}")
                image_path = None

        if not image_path and self.http_url:
            # 没有 prompt_id 说明 MCP 没走通，用 HTTP 直接提交
            if not prompt_id:
                prompt_id = self._submit_http(workflow)
            image_path = self._fetch_via_http(prompt_id, outputs_dir, timeout)

        if not image_path or not os.path.exists(image_path):
            raise BackendError("ComfyUI 执行完成但没有找到输出图片")

        data_url = self._encode_image(image_path)
        elapsed = round(time.time() - started, 1)
        self.log(f"[comfy] 出图完成，用时 {elapsed}s")

        # 清理临时目录（图片已经读进内存）
        try:
            for p in sorted(tmp_root.rglob("*"), reverse=True):
                if p.is_file():
                    p.unlink()
                else:
                    p.rmdir()
            tmp_root.rmdir()
        except Exception:
            pass

        return {"image": data_url, "elapsed": elapsed,
                "prompt_id": prompt_id, "workflow": os.path.basename(str(workflow_path))}

    # ---------- 结果获取 ----------

    @staticmethod
    def _extract_prompt_id(res: dict) -> str:
        structured = res.get("structured")
        if isinstance(structured, dict):
            for key in ("prompt_id", "promptId", "id", "job_id"):
                if structured.get(key):
                    return str(structured[key])
            # 可能是 {"job": {"prompt_id": ...}}
            for v in structured.values():
                if isinstance(v, dict):
                    for key in ("prompt_id", "promptId", "id", "job_id"):
                        if v.get(key):
                            return str(v[key])
        text = res.get("text") or ""
        import re
        m = re.search(r'"(?:prompt_id|promptId|job_id)"\s*:\s*"([^"]+)"', text)
        if m:
            return m.group(1)
        # 有些实现直接把 id 单独输出
        m = re.search(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                      r"[0-9a-f]{4}-[0-9a-f]{12})\b", text)
        return m.group(1) if m else ""

    def _wait_job(self, prompt_id: str, timeout: float) -> None:
        """等任务跑完。

        这里要兼容两种 comfy-mcp：
          · 新版把 job_status / wait_for_job / watch_job 合并成了**单个
            `job` 工具**，用 action 区分（action="wait" 即原来的等待）。
          · 老版是三个独立工具。
        所以先找 `job`，没有才退回去找旧名字。
        """
        mcp = self.mcp
        if mcp is None:
            return

        if mcp.has_tool("job"):
            try:
                # 注意：timeout_seconds 只对 wait/watch 有效，
                # 传到别的 action 上会被服务端拒绝，所以只在这里带。
                mcp.call_tool("job", {
                    "action": "wait",
                    "prompt_id": prompt_id,
                    "timeout_seconds": float(timeout),
                }, timeout=timeout + 30)
            except Exception as e:
                # 有界轮询超时是正常返回，不用当失败；真失败也会被
                # 后面的 fetch_outputs 兜住。
                self.log(f"[warn] job wait 结束：{e}")
            return

        for name in ("wait_for_job", "watch_job", "job_status"):
            if mcp.has_tool(name):
                try:
                    mcp.call_tool(name, {"prompt_id": prompt_id},
                                  timeout=timeout)
                except Exception as e:
                    self.log(f"[warn] {name} 未成功：{e}")
                return

    def _fetch_via_mcp(self, prompt_id: str, out_dir: Path,
                       timeout: float) -> str:
        self._wait_job(prompt_id, timeout)

        fname = self._tool("fetch_outputs", "get_outputs", "collect_outputs")
        out_dir.mkdir(parents=True, exist_ok=True)
        self.mcp.call_tool(fname,
                           {"prompt_id": prompt_id, "out_dir": str(out_dir)},
                           timeout=timeout + 60)
        return self._pick_image(out_dir)

    def _submit_http(self, workflow: dict) -> str:
        client_id = uuid.uuid4().hex
        resp = self.session.post(
            f"{self.http_url}/prompt",
            json={"prompt": workflow, "client_id": client_id},
            timeout=30)
        try:
            body = resp.json()
        except Exception:
            body = {}
        if isinstance(body, dict) and body.get("error"):
            err = body["error"]
            if isinstance(err, dict):
                err = err.get("message", json.dumps(err, ensure_ascii=False))
            raise BackendError(f"ComfyUI 拒绝工作流：{err}")
        resp.raise_for_status()
        pid = (body or {}).get("prompt_id")
        if not pid:
            raise BackendError("ComfyUI 未返回 prompt_id")
        self.log(f"[comfy] HTTP 已提交 prompt_id={pid}")
        return pid

    def _fetch_via_http(self, prompt_id: str, out_dir: Path,
                        timeout: float) -> str:
        out_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(self.poll_interval)
            try:
                r = self.session.get(f"{self.http_url}/history/{prompt_id}",
                                     timeout=15)
            except Exception:
                continue
            if r.status_code != 200:
                continue
            try:
                history = r.json()
            except Exception:
                continue
            entry = (history or {}).get(prompt_id)
            if not entry:
                continue

            status = entry.get("status") or {}
            for msg_type, msg_data in (status.get("messages") or []):
                if msg_type == "execution_error":
                    if isinstance(msg_data, dict):
                        nid = msg_data.get("node_id", "?")
                        ntype = msg_data.get("node_type", "?")
                        msg = msg_data.get("exception_message") or \
                            msg_data.get("message") or str(msg_data)
                    else:
                        nid, ntype, msg = "?", "?", str(msg_data)
                    raise BackendError(
                        f"ComfyUI 节点 #{nid}（{ntype}）执行失败：{msg}")

            outputs = entry.get("outputs") or {}
            candidates = []
            if self.save_node_id and self.save_node_id in outputs:
                candidates.append(outputs[self.save_node_id])
            candidates.extend(outputs.values())
            for node_out in candidates:
                if not isinstance(node_out, dict):
                    continue
                for img in (node_out.get("images") or []):
                    dest = out_dir / img.get("filename", "out.png")
                    try:
                        ir = self.session.get(
                            f"{self.http_url}/view",
                            params={"filename": img.get("filename", ""),
                                    "subfolder": img.get("subfolder", ""),
                                    "type": img.get("type", "output")},
                            timeout=60)
                        ir.raise_for_status()
                        dest.write_bytes(ir.content)
                        return str(dest)
                    except Exception as e:
                        self.log(f"[warn] 下载图片失败：{e}")
        raise BackendError(f"等待 ComfyUI 出图超时（{timeout:.0f}s）")

    @staticmethod
    def _pick_image(directory: Path) -> str:
        files = [p for p in directory.rglob("*")
                 if p.is_file() and p.suffix.lower() in MIME_BY_EXT]
        if not files:
            return ""
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return str(files[0])

    # ---------- 编码 ----------

    def _encode_image(self, path: str) -> str:
        p = Path(path)
        data = p.read_bytes()
        mime = MIME_BY_EXT.get(p.suffix.lower()) or \
            mimetypes.guess_type(str(p))[0] or "image/png"

        max_dim = int(self.cfg.get("image_max_dim") or 0)
        if max_dim > 0:
            try:
                import io
                from PIL import Image
                im = Image.open(io.BytesIO(data))
                if max(im.size) > max_dim:
                    ratio = max_dim / max(im.size)
                    im = im.resize((int(im.width * ratio),
                                    int(im.height * ratio)))
                buf = io.BytesIO()
                if self.cfg.get("image_format") == "jpeg":
                    im.convert("RGB").save(
                        buf, "JPEG",
                        quality=int(self.cfg.get("image_quality") or 92))
                    mime = "image/jpeg"
                else:
                    im.save(buf, "PNG")
                    mime = "image/png"
                data = buf.getvalue()
            except Exception as e:
                self.log(f"[warn] 图片压缩跳过：{e}")

        return f"data:{mime};base64," + base64.b64encode(data).decode()


# ============================================================ 本地 LLM

class LocalLLM:
    """把本机的 OpenAI 兼容服务（Ollama / LM Studio / llama.cpp）代理给服务器。

    注意：服务器只发提示词文本过来，模型和算力都在你本机。
    """

    def __init__(self, cfg: dict, log=print) -> None:
        self.cfg = cfg or {}
        self.log = log
        self.base_url = str(self.cfg.get("base_url")
                            or "http://127.0.0.1:8080/v1").rstrip("/")
        self.api_key = self.cfg.get("api_key") or ""
        self.session = local_session()

    def chat(self, payload: dict, timeout: float = 300) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": payload.get("model") or self.cfg.get("model") or "",
            "messages": payload.get("messages") or [],
            "temperature": payload.get("temperature", 0.1),
            "stream": False,
        }
        if not body["model"]:
            return {"error": "配置里没有指定本机模型名"}
        try:
            r = self.session.post(f"{self.base_url}/chat/completions",
                                 headers=headers, json=body,
                                 timeout=timeout)
        except requests.exceptions.ConnectionError:
            return {"error": f"无法连接本地模型服务（{self.base_url}），"
                             f"请确认 llama-server / Ollama / LM Studio 已启动"}
        except requests.exceptions.Timeout:
            return {"error": f"本地模型响应超时（{timeout:.0f}s）"}
        if not r.ok:
            return {"error": f"本地模型返回 HTTP {r.status_code}：{r.text[:300]}"}
        try:
            return r.json()
        except Exception:
            return {"error": f"本地模型返回非 JSON：{r.text[:200]}"}

    def health(self) -> dict:
        try:
            r = self.session.get(f"{self.base_url}/models", timeout=8)
            if r.ok:
                try:
                    models = [m.get("id") for m in
                              (r.json().get("data") or [])]
                except Exception:
                    models = []
                return {"ok": True, "models": models[:40]}
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
