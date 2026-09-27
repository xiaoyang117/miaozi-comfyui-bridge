"""真机验证：宽高占位符替换成「数字」而非「字符串」。

这是之前修过的关键 bug：纯文本替换会把 "width":"PH_W" 换成
"width":"896"（带引号），ComfyUI 直接拒收。修复后应替换成裸数字。

验证方式：把 MIAOMIAO.json 的节点 28（EmptyLatentImage）宽高埋成
PH_W / PH_H，替换成 512x768 后真机出图，再解析 PNG 头确认实际尺寸。
"""

import json
import os
import struct
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bridge"))

from backends import ComfyBackend, BackendError  # noqa
from mcp_client import MCPClient  # noqa

VENV = r"C:\Users\pc\.workbuddy\binaries\python\envs\miaomcp"
PROJECT = (r"H:\ComfyUI_windows_portable_nvidia(2)"
           r"\ComfyUI_windows_portable\ComfyUI")
SRC_WF = os.path.join(HERE, "..", "workflows", "MIAOMIAO.json")
PROMPT = "1girl, solo, cherry blossoms, wind"


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def png_size(path):
    """读 PNG IHDR，返回 (宽, 高)。"""
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != bytes([137, 80, 78, 71, 13, 10, 26, 10]):
        raise RuntimeError("不是合法 PNG")
    w, h = struct.unpack(">II", head[16:24])
    return w, h


def main():
    # 1. 造一个埋了宽高占位符的工作流（不动用户的原文件）
    wf = json.load(open(SRC_WF, encoding="utf-8"))
    # 注意：MIAOMIAO.json 里 KSampler(19) 实际用的是节点 100（竖图），
    # 节点 28 是没有连线引用的孤儿节点。占位符必须埋在生效节点上。
    latent = wf["100"]["inputs"]         # EmptyLatentImage（KSampler 实际使用）
    latent["width"] = "PH_W"
    latent["height"] = "PH_H"
    tmp = os.path.join(tempfile.gettempdir(), "miaozi_ph_test.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(wf, f, ensure_ascii=False)
    log(f"已生成带占位符的工作流：{tmp}")

    # 2. 真机跑
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env["COMFY_BIN"] = VENV + r"\Scripts\comfy.exe"
    env["COMFY_PROJECT"] = PROJECT

    cfg = {
        "comfyui_url": "http://127.0.0.1:8188",
        "save_node_id": "66",
        "run_timeout": 600,
        "poll_interval": 1.5,
        "image_max_dim": 0,
    }
    mcp = MCPClient(VENV + r"\Scripts\comfy-mcp.exe", env=env)
    mcp.start()
    comfy = ComfyBackend(cfg, mcp=mcp, log=log)

    try:
        out = comfy.generate({
            "workflow_path": tmp,
            "replacements": {"114514.1919810": PROMPT,
                             "PH_W": 512, "PH_H": 768},
            "timeout": 420,
        })
    except BackendError as e:
        log(f"失败：{e}")
        mcp.stop()
        return 1
    finally:
        pass

    import base64
    raw = base64.b64decode(out["image"].split(",", 1)[1])
    dst = os.path.join(HERE, "live_ph_out.png")
    open(dst, "wb").write(raw)
    w, h = png_size(dst)
    log(f"图片已存：{dst}")
    log(f"实际尺寸：{w} x {h}（期望 512 x 768）")

    mcp.stop()
    if (w, h) == (512, 768):
        log("=== 数字占位符真机验证通过 ===")
        return 0
    log("=== 尺寸不对，说明宽高替换有问题 ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
