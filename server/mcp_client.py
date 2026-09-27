"""MCP stdio 客户端。

MCP 的本地服务端是一个普通子进程，双方用「一行一个 JSON」的方式对话
（JSON-RPC 2.0）。这里实现最小可用子集：

    initialize -> notifications/initialized -> tools/list -> tools/call

为什么不直接用官方 SDK：少一个依赖，而且出问题时能一眼看清收发内容。
调试时打开 config.json 里的 verbose 就能看到全部原始报文。
"""

import json
import shutil
import subprocess
import sys
import threading
import time

DEFAULT_PROTOCOL = "2024-11-05"


class MCPError(RuntimeError):
    pass


class MCPClient:
    """把 comfy-mcp 之类的 stdio MCP 服务端包成一个可调用的对象。"""

    def __init__(self, command, args=None, env=None, verbose=False,
                 cwd=None):
        self.command = command
        self.args = list(args or [])
        self.env = env
        self.verbose = verbose
        self.cwd = cwd

        self._proc = None
        self._lock = threading.RLock()
        self._next_id = 1
        self._pending = {}
        self._tools = []
        self._stderr_lines = []
        self._closed = False

    # ---------------- 进程管理 ----------------

    def _log(self, msg: str) -> None:
        print(f"[mcp] {msg}", flush=True)

    def start(self) -> None:
        exe = shutil.which(self.command) or self.command
        cmd = [exe] + self.args
        self._log(f"启动 {cmd[0]} {' '.join(cmd[1:])}".rstrip())
        kwargs = {}
        if self.cwd:
            kwargs["cwd"] = self.cwd
        creationflags = 0
        if sys.platform == "win32":
            # 不要弹出黑窗口
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                text=True,
                encoding="utf-8",
                bufsize=1,
                creationflags=creationflags,
                **kwargs,
            )
        except FileNotFoundError:
            raise MCPError(
                f"找不到命令 {self.command}。请先安装：pip install comfy-mcp")
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self._handshake()

    def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            if self.verbose:
                self._log(f"<- {line[:600]}")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_id = msg.get("id")
            if msg_id is None:
                # 服务端通知，记录即可
                if self.verbose:
                    self._log(f"通知: {msg.get('method')}")
                continue
            with self._lock:
                slot = self._pending.pop(msg_id, None)
            if slot is not None:
                slot["message"] = msg
                slot["event"].set()

    def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            line = line.rstrip()
            if not line:
                continue
            self._stderr_lines.append(line)
            if len(self._stderr_lines) > 200:
                self._stderr_lines.pop(0)
            if self.verbose:
                self._log(f"[stderr] {line}")

    def _handshake(self) -> None:
        init = self.call("initialize", {
            "protocolVersion": DEFAULT_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "miaozi-bridge", "version": "1.0"},
        }, timeout=30)
        server = (init or {}).get("serverInfo") or {}
        self._log(f"已连接 MCP 服务端：{server.get('name', '?')} "
                  f"{server.get('version', '')}".rstrip())
        self.notify("notifications/initialized", {})
        tools = self.call("tools/list", {}, timeout=30)
        self._tools = (tools or {}).get("tools") or []
        self._log(f"可用工具 {len(self._tools)} 个：" +
                  ", ".join(t.get("name", "?") for t in self._tools))

    # ---------------- JSON-RPC ----------------

    def _send(self, payload: dict) -> None:
        if not self._proc or self._proc.poll() is not None:
            raise MCPError("MCP 服务端进程已退出" +
                           (f"：{self._stderr_lines[-1]}"
                            if self._stderr_lines else ""))
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        if self.verbose:
            self._log(f"-> {line[:600]}".rstrip())
        try:
            assert self._proc.stdin
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise MCPError(f"写入 MCP 服务端失败：{e}")

    def call(self, method: str, params: dict, timeout: float = 60.0) -> dict:
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            slot = {"event": threading.Event(), "message": None}
            self._pending[req_id] = slot
        self._send({"jsonrpc": "2.0", "id": req_id,
                    "method": method, "params": params or {}})

        if not slot["event"].wait(timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            raise MCPError(f"MCP 调用超时（{method}，{timeout:.0f}s）")

        msg = slot["message"] or {}
        if "error" in msg:
            err = msg["error"]
            if isinstance(err, dict):
                err = err.get("message", json.dumps(err, ensure_ascii=False))
            raise MCPError(f"MCP 返回错误（{method}）：{err}")
        return msg.get("result") or {}

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # ---------------- 工具调用 ----------------

    @property
    def tools(self) -> list:
        return list(self._tools)

    def tool_names(self) -> list:
        return [t.get("name", "") for t in self._tools]

    def has_tool(self, name: str) -> bool:
        return name in self.tool_names()

    def find_tool(self, candidates) -> str:
        """按候选名找第一个存在的工具，找不到抛异常。"""
        names = self.tool_names()
        for c in candidates:
            if c in names:
                return c
        # 退一步做子串匹配，兼容工具改名
        for c in candidates:
            for n in names:
                if c in n:
                    return n
        raise MCPError(
            f"服务端没有可用的工具（找过 {list(candidates)}，"
            f"实际有 {names}）")

    def call_tool(self, name: str, arguments: dict,
                  timeout: float = 300.0) -> dict:
        result = self.call("tools/call",
                           {"name": name, "arguments": arguments or {}},
                           timeout=timeout)
        return self._unwrap(result)

    @staticmethod
    def _unwrap(result: dict) -> dict:
        """把 MCP 的 content 数组拼成 {text, structured, raw}。"""
        texts = []
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(item.get("text", ""))
        structured = result.get("structuredContent")
        if structured is None:
            # 有些实现把 JSON 塞在文本里
            for t in texts:
                t2 = t.strip()
                if t2.startswith("{") or t2.startswith("["):
                    try:
                        structured = json.loads(t2)
                        break
                    except json.JSONDecodeError:
                        pass
        out = {
            "text": "\n".join(t for t in texts if t),
            "structured": structured,
            "raw": result,
            "is_error": bool(result.get("isError")),
        }
        if out["is_error"]:
            raise MCPError(out["text"] or "工具执行失败")
        return out

    # ---------------- 生命周期 ----------------

    def alive(self) -> bool:
        return (self._proc is not None and self._proc.poll() is None
                and not self._closed)

    def stop(self) -> None:
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def last_stderr(self, n: int = 5) -> str:
        return "\n".join(self._stderr_lines[-n:])

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False
