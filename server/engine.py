"""本机引擎：MCP 客户端 + ComfyUI 后端 + 本地模型代理。

单机版没有「桥接进程」这一层了——服务端进程自己直接持有 MCP 连接、
直接调 ComfyUI 和本机模型。原来 bridge/bridge.py 里那套「长轮询、
令牌、跨机任务派发」全部不需要。

配置从 settings 动态读，用户在网页改完配置点保存立即生效；
MCP 相关配置改完后点「重启引擎」重建连接。
"""

import threading
import time

from backends import ComfyBackend, local_session
from mcp_client import MCPClient
from settings import settings


def _log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} [engine] {msg}", flush=True)


class Engine:
    def __init__(self) -> None:
        self.mcp = None
        self.mcp_error = ""
        self._tried = False          # 本次进程只自动尝试一次，失败靠手动重启
        self._lock = threading.RLock()
        self.served = 0
        self.started_at = time.time()
        self._http = local_session()

    # ---------- 配置映射 ----------

    def _backend_cfg(self) -> dict:
        """把 settings 翻译成 ComfyBackend 认识的 cfg。"""
        return {
            "comfyui_url": settings.get("comfyui_url"),
            "save_node_id": settings.get("save_node_id"),
            "workflow_dirs": settings.get("workflow_dirs"),
            "workflow_path": settings.get("workflow_path"),
            "prompt_placeholder": settings.get("prompt_placeholder"),
            "poll_interval": settings.get("poll_interval"),
            "image_max_dim": settings.get("image_max_dim"),
            "image_format": settings.get("image_format"),
            "image_quality": settings.get("image_quality"),
        }

    def _backend(self) -> ComfyBackend:
        return ComfyBackend(self._backend_cfg(), mcp=self.mcp, log=_log)

    # ---------- MCP 生命周期 ----------

    def ensure_mcp(self) -> None:
        """懒加载：第一次用到 MCP 时才启动，失败不阻塞网站。"""
        with self._lock:
            if self._tried or not settings.get("mcp_enabled", True):
                return
            self._tried = True
            self.start_mcp()

    def start_mcp(self) -> None:
        import os

        self.stop_mcp()
        if not settings.get("mcp_enabled", True):
            self.mcp = None
            self.mcp_error = "已在配置里关闭 MCP"
            _log("MCP 已按配置关闭，ComfyUI 将走 HTTP 直连")
            return

        env = dict(os.environ)
        # comfy-mcp 内部要连本机 ComfyUI，带上代理变量会被拦掉
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "http_proxy", "https_proxy", "all_proxy"):
            env.pop(key, None)
        env["NO_PROXY"] = "*"
        env["no_proxy"] = "*"

        # 便携版 ComfyUI 必须指对工作区，否则 comfy-cli 会用它的默认空目录
        project = str(settings.get("comfy_project") or "").strip()
        if project:
            env["COMFY_PROJECT"] = os.path.expanduser(project)

        # comfy-cli 装在虚拟环境里时，MCP 客户端环境里没有它，需要显式指
        comfy_bin = str(settings.get("comfy_bin") or "").strip()
        if comfy_bin:
            env["COMFY_BIN"] = os.path.expanduser(comfy_bin)

        for k, v in (settings.get("mcp_env") or {}).items():
            env[str(k)] = str(v)

        command = str(settings.get("mcp_command") or "comfy-mcp")
        cwd = str(settings.get("mcp_cwd") or "").strip()
        if not cwd and project:
            cwd = project

        try:
            client = MCPClient(
                command,
                args=settings.get("mcp_args") or [],
                env=env,
                verbose=False,
                cwd=cwd or None,
            )
            client.start()
            self.mcp = client
            self.mcp_error = ""
            _log(f"MCP 就绪（{len(client.tool_names())} 个工具）")
        except Exception as e:
            self.mcp = None
            self.mcp_error = str(e)
            _log(f"[warn] MCP 启动失败：{e}（自动改走 HTTP 直连）")

    def stop_mcp(self) -> None:
        if self.mcp is not None:
            try:
                self.mcp.stop()
            except Exception:
                pass
        self.mcp = None
        self.mcp_error = ""

    def restart(self) -> dict:
        """配置改完后手动重建 MCP 连接。"""
        self._tried = True
        self.start_mcp()
        return self.status()

    # ---------- 对外能力（app.py 调这些） ----------

    def probe(self) -> dict:
        """测试 ComfyUI 连接。MCP 优先，失败回退 HTTP /system_stats。"""
        self.ensure_mcp()
        result = self._backend().probe()
        if self.mcp_error and not result.get("ok"):
            result["body"] = (result.get("body") or "") + \
                f"\n（MCP 状态：{self.mcp_error}）"
        return result

    def list_workflows(self, extra_dir: str = "") -> dict:
        self.ensure_mcp()
        return self._backend().list_workflows(extra_dir)

    def generate(self, job: dict) -> dict:
        self.ensure_mcp()
        result = self._backend().generate(job)
        self.served += 1
        return result

    def comfy_http_alive(self, timeout: float = 4.0) -> bool:
        url = str(settings.get("comfyui_url") or "").rstrip("/")
        if not url:
            return False
        try:
            r = self._http.get(f"{url}/system_stats", timeout=timeout)
            return r.ok
        except Exception:
            return False

    # ---------- 状态 ----------

    def status(self) -> dict:
        mcp_alive = bool(self.mcp and self.mcp.alive())
        comfy_ok = self.comfy_http_alive()
        if mcp_alive and comfy_ok:
            state = "mcp"
        elif comfy_ok:
            state = "http"
        else:
            state = "offline"
        return {
            "state": state,                # mcp | http | offline
            "mcp_alive": mcp_alive,
            "mcp_error": self.mcp_error,
            "mcp_enabled": bool(settings.get("mcp_enabled", True)),
            "tools_count": len(self.mcp.tool_names()) if mcp_alive else 0,
            "comfyui_url": settings.get("comfyui_url"),
            "comfy_project": settings.get("comfy_project"),
            "served": self.served,
            "uptime": int(time.time() - self.started_at),
        }


engine = Engine()
