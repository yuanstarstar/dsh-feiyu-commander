@echo off
chcp 936 >nul
setlocal
set PYTHONIOENCODING=cp936
set "FEIYU_HOME=%~dp0"
if "%FEIYU_HOME:~-1%"=="\" set "FEIYU_HOME=%FEIYU_HOME:~0,-1%"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
if not exist "%~dp0tools\boot.py" (
  echo [x] 没找到 tools\boot.py，先跑 安装.bat。
  pause
  exit /b 1
)
"%PY%" "%~dp0tools\boot.py" %*
echo.
pause
