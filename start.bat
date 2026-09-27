@echo off
cd /d "%~dp0"
title MiaoZi - Local Launcher

echo.
echo  ==== MiaoZi (local edition) ====
echo.

rem ---------- 1. Env check (hints only, will not start them for you) ----------
set COMFY_OK=0
set LLM_OK=0

python -c "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',8188))==0 else 1)" >nul 2>&1
if not errorlevel 1 set COMFY_OK=1

python -c "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',8080))==0 else 1)" >nul 2>&1
if not errorlevel 1 set LLM_OK=1

if "%COMFY_OK%"=="1" (echo  [OK] ComfyUI is running    127.0.0.1:8188
) else echo  [!!] ComfyUI is NOT running - image gen unavailable. Start run_nvidia_gpu.bat first.

if "%LLM_OK%"=="1" (echo  [OK] llama-server running  127.0.0.1:8080
) else echo  [!!] llama-server NOT running - local LLM unavailable. Use remote API or start llama-server.

echo.

rem ---------- 2. First-run dependency install ----------
python -c "import flask,waitress,requests" >nul 2>&1
if errorlevel 1 (
    echo  First run: installing dependencies...
    python -m pip install -r "%~dp0server\requirements.txt"
    if errorlevel 1 (
        echo  Dependency install failed. Check your python environment.
        pause
        exit /b 1
    )
    echo.
)

rem ---------- 3. Start server + open browser ----------
echo  Starting server... browser will open http://127.0.0.1:5000
echo  Close this window to stop the server.
echo.
start "" http://127.0.0.1:5000
cd server
python app.py
pause
