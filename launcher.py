import importlib.util
import os
import socket
import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "server"
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def port_open(port):
    with socket.socket() as connection:
        connection.settimeout(2)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def check_services():
    print("[检查] 以下只检测本机默认端口，不影响网站启动。")
    print("[提示] 使用远程服务或其他端口时，请在网页配置中测试连接。")
    for name, port in (("ComfyUI", 8188), ("LLM", 8080)):
        if port_open(port):
            print(f"[检查] {name} 默认端口有响应：127.0.0.1:{port}")
        else:
            print(f"[提示] {name} 默认端口未检测到服务：127.0.0.1:{port}")
    print()


def install_dependencies():
    if all(importlib.util.find_spec(name)
           for name in ("flask", "waitress", "requests")):
        return True
    print("[准备] 缺少网站依赖，正在通过清华源安装...")
    result = subprocess.run([
        sys.executable, "-m", "pip", "install",
        "-r", str(SERVER / "requirements.txt"), "-i", MIRROR,
    ])
    if result.returncode:
        print("[错误] 依赖安装失败，请查看上方报错，检查网络或 Python 环境。")
        return False
    print()
    return True


def main():
    print()
    print(" ==== 喵梓 · ComfyUI 创作工作台 ====")
    print()
    check_services()
    if not install_dependencies():
        return 1
    port = os.environ.get("PORT", "5000")
    address = f"http://127.0.0.1:{port}"
    print(f"[启动] 即将自动打开网页：{address}")
    print("[提示] 如果页面暂时打不开，请稍等片刻后刷新。")
    print("[提示] 关闭此窗口或按 Ctrl+C 可以停止网站。")
    print()
    webbrowser.open(address)
    try:
        result = subprocess.run([sys.executable, "app.py"], cwd=SERVER)
    except KeyboardInterrupt:
        result = None
    print()
    print("[提示] 网站已停止；如果是意外退出，请查看上方报错。")
    return result.returncode if result else 0


if __name__ == "__main__":
    sys.exit(main())
