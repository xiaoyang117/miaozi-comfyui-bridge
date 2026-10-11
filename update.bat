@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
echo 请先关闭正在运行的喵梓网站，再继续更新。
echo 本机代理请填写 HTTP 端口，例如 http://127.0.0.1:7890
set "MIAOZI_UPDATE_PROXY="
set /p "MIAOZI_UPDATE_PROXY=代理地址（留空使用系统代理或直连）: "
python "%~dp0update.py"
if errorlevel 1 (
    echo 更新失败；请检查上方错误信息。
    pause
    exit /b 1
)
echo 更新完成。请重新运行 start.bat。
pause
