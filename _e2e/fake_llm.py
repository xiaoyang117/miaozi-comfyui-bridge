"""模拟本地模型服务，返回一个固定的提示词。"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        b = json.dumps({"data":[{"id":"qwen2.5:7b"},{"id":"other"}]}).encode()
        self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8")
        body = json.loads(raw or "{}")
        msgs = body.get("messages") or []
        # 回显 system 前 40 字，方便断言走了哪条分支
        sys_txt = next((m["content"] for m in msgs if m.get("role")=="system"), "")
        text = "1girl, solo, silver hair, blue_archive_(series), white dress, " + sys_txt[:40]
        out = {"choices":[{"message":{"role":"assistant","content":text}}]}
        b = json.dumps(out, ensure_ascii=False).encode()
        self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass

HTTPServer(("127.0.0.1", 18081), H).serve_forever()
