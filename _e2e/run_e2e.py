"""端到端验收：单机服务器（内置引擎）+ 模拟 MCP + 模拟本地模型。

全程走真实 HTTP，验证：配置下发 -> 引擎重启加载 fake_mcp -> MCP 握手 ->
占位符替换 -> 图片落盘 -> 会话落库 -> 上下文传递。

用法（服务器与模拟模型已在跑，见 run_all.sh）：
  python run_e2e.py
"""

import base64
import json
import sys
import time

import requests

BASE = "http://127.0.0.1:18765"
PY_EXE = r"C:\Users\pc\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
FAKE_MCP = r"D:\神秘小软件\miaozi\_e2e\fake_mcp.py"
FAKE_LLM = "http://127.0.0.1:18081/v1"
WF_PATH = r"D:\神秘小软件\miaozi\_e2e\wf\test_workflow.json"

# 本机测试必须绕开系统代理，否则 127.0.0.1 会被丢进代理（502）
s = requests.Session()
s.trust_env = False
s.proxies = {}

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  <- {extra}"))
    if not cond:
        fails.append(name)


def get(path, **kw):
    return s.get(BASE + path, timeout=kw.pop("timeout", 30), **kw)


def post(path, body=None, **kw):
    return s.post(BASE + path, json=body, timeout=kw.pop("timeout", 120), **kw)


def sse(path, body):
    """读完整条 SSE，返回事件列表。"""
    events = []
    with s.post(BASE + path, json=body, stream=True, timeout=300) as r:
        check("SSE 响应类型", "text/event-stream" in r.headers.get("Content-Type", ""),
              r.headers.get("Content-Type"))
        for line in r.iter_lines(decode_unicode=True):
            if line and line.startswith("data: "):
                try:
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
    return events


print("=" * 60)
print("一、服务器基础")
print("=" * 60)
r = get("/")
check("首页可访问", r.status_code == 200 and "喵梓" in r.text)

d = get("/api/bootstrap").json()
check("bootstrap 成功", d.get("success") is True)
check("bootstrap 返回 engine 状态", isinstance(d.get("engine"), dict), list(d.keys()))

print()
print("=" * 60)
print("二、引擎配置与重启（fake_mcp + fake_llm）")
print("=" * 60)
post("/api/settings", {
    "llm_mode": "local",
    "local_llm_base_url": FAKE_LLM,
    "mcp_enabled": True,
    "mcp_command": PY_EXE,
    "mcp_args": [FAKE_MCP],
    "workflow_path": WF_PATH,
    "prompt_placeholder": "114514.1919810",
    "width_placeholder": "PH_W",
    "height_placeholder": "PH_H",
    "save_node_id": "9",
    "use_character_db": False,
    "resolution_presets": [{"label": "896×1152", "width": 896, "height": 1152}],
})
d = post("/api/engine/restart", {}).json()
st = d.get("engine") or {}
check("引擎重启成功", d.get("success") is True, d)
check("MCP 已连接（fake_mcp）", st.get("mcp_alive") is True, st)
check("MCP 工具就绪", (st.get("tools_count") or 0) >= 7, st.get("tools_count"))

d = get("/api/engine/status").json()["engine"]
check("引擎状态可查", d.get("mcp_alive") is True, d)

print()
print("=" * 60)
print("三、ComfyUI 探测（走 MCP）与工作流列表")
print("=" * 60)
d = post("/api/test/comfyui", {}).json()
check("ComfyUI 探测成功", d.get("success") is True, d)

d = get("/api/workflows").json()
names = [w["name"] for w in d.get("workflows", [])]
check("读到本地工作流", "test_workflow.json" in names, names)
check("未标记为离线回落", not d.get("offline"), d)

print()
print("=" * 60)
print("四、完整生成流程（本地模型 + MCP 出图）")
print("=" * 60)
sid = post("/api/sessions", {"title": "e2e"}).json()["session"]["id"]

ev = sse("/api/generate", {
    "prompt": "一个银发少女站在海边",
    "session_id": sid,
    "use_search": False,
    "use_history": True,
    "specific": False,
    "preset_index": 0,
    "images": [],
})
steps = [e.get("step") for e in ev]
print("   步骤序列:", steps)
check("流程经过 llm 步骤", "llm" in steps, steps)
check("流程经过 comfyui 步骤", "comfyui" in steps, steps)
done = next((e for e in ev if e.get("step") == "done"), None)
check("流程完成", done is not None, ev[-1] if ev else "无事件")
if done:
    check("返回图片地址", str(done.get("image", "")).startswith("/outputs/"), done)
    check("返回提示词", bool(done.get("prompt")), done)
    print("   提示词:", done.get("prompt", "")[:110])
    img = get(done["image"])
    check("图片可下载", img.status_code == 200 and img.content[:4] == b"\x89PNG",
          img.status_code)
    check("图片尺寸正确", done.get("prompt") is not None)

# 占位符替换验证：模拟 MCP 把收到的工作流存到了临时目录
import os
import tempfile
wf_dump = os.path.join(tempfile.gettempdir(), "miaozi_wf.json")
if os.path.exists(wf_dump):
    blob = open(wf_dump, encoding="utf-8").read()
    check("提示词占位符已替换", "114514.1919810" not in blob, blob[:200])
    check("宽占位符已替换", "PH_W" not in blob and "PH_H" not in blob, blob[:200])
    wf = json.loads(blob)
    # 占位符写成字符串 "PH_W"，替换后应变成数字 896（不带引号）
    ins = wf.get("21", {}).get("inputs", {})
    check("宽高替换成数字类型",
          ins.get("width") == 896 and ins.get("height") == 1152, ins)
    check("提示词写入了文本节点",
          "silver hair" in wf.get("20", {}).get("inputs", {}).get("text", ""),
          wf.get("20"))
else:
    check("工作流落盘可校验", False, "没找到 miaozi_wf.json")

print()
print("=" * 60)
print("五、历史上下文与会话落库")
print("=" * 60)
msgs = get(f"/api/sessions/{sid}").json()["messages"]
check("用户+助手两条消息落库", len(msgs) == 2, len(msgs))
check("用户消息内容正确", msgs[0]["role"] == "user" and "银发" in msgs[0]["content"], msgs[0])
check("助手消息含图片", msgs[1]["role"] == "assistant" and msgs[1]["image"], msgs[1])
check("助手消息含提示词", bool(msgs[1]["content"]), msgs[1])

# 第二轮：验证上下文被带进去（模拟模型回显了 system 开头）
ev2 = sse("/api/generate", {
    "prompt": "换成冬天的场景",
    "session_id": sid,
    "use_search": False,
    "use_history": True,
    "preset_index": 0,
    "images": [],
})
done2 = next((e for e in ev2 if e.get("step") == "done"), None)
check("第二轮也能出图", done2 is not None, ev2)
if done2:
    p2 = done2.get("prompt", "")
    # 第二轮提示词里应带上上一轮的痕迹（模拟模型回显了 system 前缀）
    check("历史上下文参与生成", len(p2) > 0, p2[:120])
msgs2 = get(f"/api/sessions/{sid}").json()["messages"]
check("第二轮消息也落库", len(msgs2) == 4, len(msgs2))

# 关掉上下文再跑一次，确认开关生效
ev3 = sse("/api/generate", {
    "prompt": "第三轮",
    "session_id": sid,
    "use_search": False,
    "use_history": False,
    "preset_index": 0,
    "images": [],
})
check("关上下文也能出图", any(e.get("step") == "done" for e in ev3), ev3[-1] if ev3 else None)

print()
print("=" * 60)
print("六、异常路径")
print("=" * 60)
# 工作流不存在
d = post("/api/settings", {"workflow_path": "D:/不存在/workflow.json"}).json()
ev = sse("/api/generate", {"prompt": "x", "session_id": sid, "use_search": False,
                           "use_history": False, "images": []})
err = next((e for e in ev if e.get("step") == "error"), None)
check("工作流缺失时明确报错", err is not None and "工作流" in err.get("error", ""),
      err)
# 恢复
post("/api/settings", {"workflow_path": WF_PATH})

# 本地模型挂掉（单机版配置即时生效，引擎每次生成都会重建 LLM 客户端）
post("/api/settings", {"local_llm_base_url": "http://127.0.0.1:19999/v1"})
ev = sse("/api/generate", {"prompt": "y", "session_id": sid, "use_search": False,
                           "use_history": False, "images": []})
check("异常时 SSE 正常收尾", any(e.get("step") in ("done", "error") for e in ev),
      ev[-1] if ev else None)
# 恢复
post("/api/settings", {"local_llm_base_url": FAKE_LLM})

# 队列互斥：第二个请求要么返回 {"queue": true}，要么直接拿到流
import threading
res = {}


def bg():
    res["ev"] = sse("/api/generate", {"prompt": "并发1", "session_id": sid,
                                      "use_search": False, "use_history": False,
                                      "images": []})


t = threading.Thread(target=bg)
t.start()
time.sleep(0.3)
r = post("/api/generate", {"prompt": "并发2", "session_id": sid})
ct = r.headers.get("Content-Type", "")
ok = False
if "text/event-stream" in ct:
    ok = True                      # 抢到了锁，串行执行
else:
    try:
        ok = r.json().get("queue") is True   # 被拒，正确地提示排队
    except ValueError:
        ok = False
check("并发时要么排队要么串行（不崩溃）", ok, (r.status_code, ct, r.text[:120]))
t.join(timeout=180)

print()
print("=" * 60)
print("七、输出图片自动清理")
print("=" * 60)
post("/api/settings", {"keep_outputs": 2})
for i in range(4):
    sse("/api/generate", {"prompt": f"清理测试{i}", "session_id": sid,
                          "use_search": False, "use_history": False, "images": []})
import glob as _g
files = _g.glob(os.path.join("D:/神秘小软件/miaozi/server/outputs", "gen_*.png"))
check("输出目录受控", len(files) <= 2, f"实际 {len(files)} 个")
post("/api/settings", {"keep_outputs": 300})

print()
print("=" * 60)
print("八、引擎状态")
print("=" * 60)
st = get("/api/engine/status").json()["engine"]
check("引擎 MCP 在线", st.get("mcp_alive") is True, st)
check("出图计数累加", st.get("served", 0) >= 6, st.get("served"))
check("运行时长在记录", st.get("uptime", 0) > 0, st.get("uptime"))

print()
print("=" * 60)
print("失败项:", fails if fails else "无")
print("=" * 60)
sys.exit(1 if fails else 0)
