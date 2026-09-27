"""真机联调：用真实的 comfy-mcp + 真实的 ComfyUI 跑通一次出图。

验证点：
  1. 我的 MCP 客户端能不能驱动官方 comfy-mcp
  2. COMFY_PROJECT 指向便携版后，run_workflow 能不能work
  3. _apply_replacements 替换占位符是否被 ComfyUI 接受
  4. 新版 job(action="wait") 等待逻辑是否正确
  5. fetch_outputs 能不能取到图

不依赖服务器，直接调 ComfyBackend。
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bridge"))

from backends import ComfyBackend, BackendError  # noqa
from mcp_client import MCPClient  # noqa

VENV = r"C:\Users\pc\.workbuddy\binaries\python\envs\miaomcp"
COMFY_MCP = VENV + r"\Scripts\comfy-mcp.exe"
COMFY_BIN = VENV + r"\Scripts\comfy.exe"
PROJECT = (r"H:\ComfyUI_windows_portable_nvidia(2)"
           r"\ComfyUI_windows_portable\ComfyUI")
WORKFLOW = os.path.join(os.path.dirname(__file__), "..", "workflows",
                        "MIAOMIAO.json")
PROMPT = ("1girl, solo, long hair, cat ears, smile, looking at viewer, "
          "cherry blossoms, spring")


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def main():
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env["COMFY_BIN"] = COMFY_BIN
    env["COMFY_PROJECT"] = PROJECT

    cfg = {
        "comfyui_url": "http://127.0.0.1:8188",
        "save_node_id": "66",
        "prompt_placeholder": "114514.1919810",
        "run_timeout": 600,
        "poll_interval": 1.5,
        "image_max_dim": 0,
        "image_format": "png",
    }

    mcp = MCPClient(COMFY_MCP, env=env, verbose=False)
    mcp.start()

    comfy = ComfyBackend(cfg, mcp=mcp, log=log)

    log("--- probe ---")
    p = comfy.probe()
    log(f"probe ok={p.get('ok')} source={p.get('source')}")

    log("--- 开始出图 ---")
    t0 = time.time()
    try:
        out = comfy.generate({
            "workflow_path": os.path.abspath(WORKFLOW),
            "prompt": PROMPT,
            "timeout": 420,
        })
    except BackendError as e:
        log(f"出图失败（BackendError）：{e}")
        mcp.stop()
        return 1
    except Exception as e:
        log(f"出图异常 {type(e).__name__}：{e}")
        mcp.stop()
        return 1

    img = out.get("image") or ""
    log(f"用时 {out.get('elapsed')}s  总耗时 {time.time()-t0:.1f}s")
    log(f"prompt_id = {out.get('prompt_id')}")
    log(f"图片 data URL 长度 = {len(img)}")
    if img.startswith("data:image/"):
        head = img[:40]
        log(f"图片头 = {head}")
        import base64
        raw = base64.b64decode(img.split(",", 1)[1])
        out_png = os.path.join(os.path.dirname(__file__), "live_out.png")
        with open(out_png, "wb") as f:
            f.write(raw)
        log(f"已存图：{out_png}（{len(raw)} 字节）")
        # 校验是真 PNG
        log(f"PNG 魔数正确：{raw[:8] == bytes([137, 80, 78, 71, 13, 10, 26, 10])}")
    else:
        log("没有拿到图片！")
        mcp.stop()
        return 1

    mcp.stop()
    log("=== 真机链路全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
