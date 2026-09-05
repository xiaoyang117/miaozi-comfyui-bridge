@echo off
echo %date% %time% launcher executed > "%~dp0start.log"
cd /d "%~dp0"

echo ============================================
echo   Miaozi2 ComfyUI Bridge  Launcher
echo ============================================
echo.

set "PY=%CD%\venv\Scripts\python.exe"

if exist "%PY%" goto :hasvenv

echo [1/3] Python venv not found, creating...
python -m venv venv
if errorlevel 1 (
  echo.
  echo [ERROR] Cannot create venv. Install Python 3.10+ first.
  pause
  exit /b 1
)
set "PY=%CD%\venv\Scripts\python.exe"

:hasvenv
echo [1/3] Python: %PY%

echo [2/3] Checking dependencies...
"%PY%" -c "import flask,requests,waitress" 2>nul
if errorlevel 1 (
  echo Installing dependencies, please wait...
  "%PY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo [ERROR] pip install failed. Check network.
    pause
    exit /b 1
  )
)

echo [3/3] Starting server at http://127.0.0.1:5000
echo.
echo ============================================
echo   Server running. Close this window to stop.
echo ============================================
"%PY%" app.py

echo.
echo Server stopped.
pause
