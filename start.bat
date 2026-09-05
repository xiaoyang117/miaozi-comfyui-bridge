@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 喵梓二号 - ComfyUI 生图助手

echo ============================================
echo   喵梓二号  启动器
echo ============================================

REM ---- 使用本地 venv（若存在） ----
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"

echo [1/2] 检查并安装依赖...
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo 依赖安装失败，请检查网络或 Python 环境
    pause
    exit /b 1
)

echo [2/2] 安装 Chromium（浏览器搜索用，可跳过）...
"%PY%" -m playwright install chromium 2>nul

echo.
echo 启动中... 访问 http://localhost:5000
echo 关闭本窗口即停止服务
echo ============================================
"%PY%" app.py
pause
