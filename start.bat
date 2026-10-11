@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 (
    echo [错误] 无法进入项目目录，请检查文件夹是否存在。
    pause
    exit /b 1
)
title 喵梓 - 启动窗口

echo.
echo  ==== 喵梓 · ComfyUI 创作工作台 ====
echo.

python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到可用的 Python 3.10 或以上版本。
    echo 请安装 Python，并勾选 Add Python to PATH 后重新启动。
    pause
    exit /b 1
)

rem ---------- 1. Env check (hints only, will not start them for you) ----------
set COMFY_OK=0
set LLM_OK=0

echo [检查] 以下只检测本机默认端口，不影响网站启动。
echo [提示] 使用远程服务或其他端口时，请在网页配置中测试连接。

python -c "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',8188))==0 else 1)" >nul 2>&1
if not errorlevel 1 set COMFY_OK=1

python -c "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',8080))==0 else 1)" >nul 2>&1
if not errorlevel 1 set LLM_OK=1

if "%COMFY_OK%"=="1" (echo [检查] ComfyUI 默认端口有响应：127.0.0.1:8188
) else echo [提示] ComfyUI 默认端口未检测到服务：127.0.0.1:8188

if "%LLM_OK%"=="1" (echo [检查] LLM 默认端口有响应：127.0.0.1:8080
) else echo [提示] LLM 默认端口未检测到服务：127.0.0.1:8080

echo.

rem ---------- 2. First-run dependency install ----------
python -c "import flask,waitress,requests" >nul 2>&1
if errorlevel 1 (
    echo [准备] 缺少网站依赖，正在通过清华源安装...
    python -m pip install -r "%~dp0server\requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请查看上方报错，检查网络或 Python 环境。
        pause
        exit /b 1
    )
    echo.
)

rem ---------- 3. Start server + open browser ----------
if not defined PORT set "PORT=5000"
echo [启动] 即将自动打开网页：http://127.0.0.1:%PORT%
echo [提示] 如果页面暂时打不开，请稍等片刻后刷新。
echo [提示] 关闭此窗口或按 Ctrl+C 可以停止网站。
echo.
start "" "http://127.0.0.1:%PORT%"
cd /d "%~dp0server"
python app.py
echo.
echo [提示] 网站已停止；如果是意外退出，请查看上方报错。
pause
