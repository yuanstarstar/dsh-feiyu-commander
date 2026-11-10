@echo off
chcp 936 >nul
setlocal
set PYTHONIOENCODING=utf-8
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo 即将卸载肥鱼指挥官（DSH 里的插件条目 + 安装目录）。
choice /c YN /m "确定继续"
if errorlevel 2 exit /b 0
"%PY%" "%~dp0install.py" --uninstall
echo.
pause
