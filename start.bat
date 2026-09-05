@echo off
setlocal
cd /d "%~dp0"
title Miaozi2 - ComfyUI Bridge

echo ============================================
echo   Miaozi2 ComfyUI Bridge  Launcher
echo ============================================

REM ============ locate a base Python ============
set "BASE_PY="
where python >nul 2>nul && set "BASE_PY=python"
if not defined BASE_PY (
    if exist "C:\Program Files\Python313\python.exe" set "BASE_PY=C:\Program Files\Python313\python.exe"
    if not defined BASE_PY if exist "C:\Python313\python.exe" set "BASE_PY=C:\Python313\python.exe"
)
if not defined BASE_PY (
    echo [ERROR] No Python found.
    echo Please install Python 3.10+ from https://www.python.org
    echo and tick "Add python.exe to PATH", then run this file again.
    echo.
    pause
    exit /b 1
)
echo Using base Python: %BASE_PY%

REM ============ ensure project venv ============
if exist "venv\Scripts\python.exe" (
    set "PY=venv\Scripts\python.exe"
    echo Reusing existing venv.
) else (
    echo Creating project venv (first run)...
    "%BASE_PY%" -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create venv.
        pause
        exit /b 1
    )
    set "PY=venv\Scripts\python.exe"
)
echo Using Python: %PY%

REM ============ ensure dependencies ============
"%PY%" -c "import flask, requests, waitress" >nul 2>nul
if errorlevel 1 (
    echo Installing dependencies (first run, please wait)...
    "%PY%" -m pip install --upgrade pip -q
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERROR] Dependency install failed.
        echo Check network, then run manually:
        echo     %PY% -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
) else (
    echo Dependencies OK.
)

REM ============ launch ============
echo.
echo ============================================
echo   Starting server...
echo   Open browser:  http://127.0.0.1:5000
echo   Close this window to stop the server.
echo ============================================
"%PY%" app.py

echo.
echo Server stopped.
pause
