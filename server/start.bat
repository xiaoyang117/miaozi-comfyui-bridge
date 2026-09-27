@echo off
cd /d "%~dp0"
echo ==== 喵梓 服务端 ====
if not exist data mkdir data
echo 正在安装依赖...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo 依赖安装失败
    pause
    exit /b 1
)
echo.
echo 启动后访问 http://127.0.0.1:5000
echo 前置条件：本机 ComfyUI 已启动（默认 http://127.0.0.1:8188）
echo 首次使用：进「配置」页填 ComfyUI 工作区目录和 comfy-mcp 命令
echo.
python app.py
pause
