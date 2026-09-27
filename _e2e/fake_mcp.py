"""模拟一个 comfy-mcp 服务端：说 MCP stdio JSON-RPC，返回一张假图。"""
import base64, json, os, sys, tempfile

PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
       "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()

TOOLS = ["server_info", "run_workflow", "job", "fetch_outputs",
         "validate_workflow", "search_models", "nodes"]

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        send({"jsonrpc":"2.0","id":mid,"result":{
            "protocolVersion":"2024-11-05",
            "capabilities":{"tools":{}},
            "serverInfo":{"name":"fake-comfy-mcp","version":"0.0.1"}}})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        send({"jsonrpc":"2.0","id":mid,"result":{"tools":[
            {"name":n,"description":n,"inputSchema":{"type":"object"}} for n in TOOLS]}})
    elif method == "tools/call":
        name = (msg.get("params") or {}).get("name")
        args = (msg.get("params") or {}).get("arguments") or {}
        if name == "server_info":
            out = {"comfyui":"running","url":"http://127.0.0.1:8188","vram":"24GB"}
        elif name == "validate_workflow":
            assert os.path.exists(args.get("workflow_path","")), "workflow missing"
            out = {"valid": True}
        elif name == "run_workflow":
            # 校验占位符真的被替换过
            wf = json.load(open(args["workflow_path"], encoding="utf-8"))
            blob = json.dumps(wf, ensure_ascii=False)
            assert "114514.1919810" not in blob, "占位符没被替换!"
            assert "PH_X" not in blob, "占位符没被替换!"
            open(os.path.join(tempfile.gettempdir(),"miaozi_wf.json"),"w",encoding="utf-8").write(blob)
            out = {"prompt_id":"abcd-1234-ef56-7890","status":"submitted"}
        elif name == "job":
            # 新版 comfy-mcp 的形态：单个 job 工具 + action 参数。
            # 桥接应该用 action="wait"，并带上 prompt_id / timeout_seconds
            #（timeout_seconds 只对 wait/watch 有效，官方会拒绝错位传参）
            action = args.get("action")
            assert action == "wait", f"job action 应为 wait，实际 {action!r}"
            assert args.get("prompt_id"), "job 缺少 prompt_id"
            assert "timeout_seconds" in args, "job wait 应带 timeout_seconds"
            out = {"status": "completed", "prompt_id": args.get("prompt_id")}
        elif name == "fetch_outputs":
            d = args.get("out_dir") or tempfile.gettempdir()
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "fake_out_00001_.png")
            open(p,"wb").write(base64.b64decode(PNG))
            out = {"saved":[p]}
        else:
            out = {}
        send({"jsonrpc":"2.0","id":mid,"result":{
            "content":[{"type":"text","text":json.dumps(out,ensure_ascii=False)}],
            "structuredContent":out}})
    else:
        if mid is not None:
            send({"jsonrpc":"2.0","id":mid,"error":{"code":-32601,"message":"no method"}})
