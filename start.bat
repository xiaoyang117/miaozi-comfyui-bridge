@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 (
    echo Start failed: cannot open the project folder.
    pause
    exit /b 1
)
title MiaoZi
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 (
    echo Start failed: Python 3.10 or newer is required.
    echo Install Python and enable "Add Python to PATH", then try again.
    pause
    exit /b 1
)
python "%~dp0launcher.py"
pause
