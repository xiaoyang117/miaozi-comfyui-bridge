@echo off
setlocal
if /i "%~1"=="--run" goto run

set "MIAOZI_UPDATE_ROOT=%~dp0"
set "MIAOZI_UPDATE_RUNNER=%TEMP%\miaozi-update-%RANDOM%-%RANDOM%.bat"
copy /y "%~f0" "%MIAOZI_UPDATE_RUNNER%" >nul
if errorlevel 1 (
    echo Failed to prepare the updater. Check access to your temporary folder.
    pause
    exit /b 1
)
call "%MIAOZI_UPDATE_RUNNER%" --run
set "MIAOZI_UPDATE_RESULT=%ERRORLEVEL%"
del /q "%MIAOZI_UPDATE_RUNNER%" >nul 2>&1
exit /b %MIAOZI_UPDATE_RESULT%

:run
cd /d "%MIAOZI_UPDATE_ROOT%"
if errorlevel 1 (
    echo Update failed: cannot open the project folder.
    pause
    exit /b 1
)
chcp 65001 >nul
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 (
    echo Update failed: Python 3.10 or newer is required.
    echo Install Python or add it to PATH, then try again.
    pause
    exit /b 1
)
echo Close the MiaoZi website before updating.
echo Enter a local HTTP proxy, such as http://127.0.0.1:7890
set "MIAOZI_UPDATE_PROXY="
set /p "MIAOZI_UPDATE_PROXY=Proxy URL (blank for system proxy or direct): "
python "%MIAOZI_UPDATE_ROOT%update.py"
set "MIAOZI_UPDATE_RESULT=%ERRORLEVEL%"
if not "%MIAOZI_UPDATE_RESULT%"=="0" (
    echo Update failed. See the error message above.
) else (
    echo Update complete. Restart start.bat.
)
pause
exit /b %MIAOZI_UPDATE_RESULT%
