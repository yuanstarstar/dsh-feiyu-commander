@echo off
chcp 936 >nul
setlocal
set PYTHONIOENCODING=utf-8
set "PY="
py -3 -V >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  python -V >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [x] 没找到 Python 3.10+。
  echo     请先安装：https://www.python.org/downloads/
  echo     安装时记得勾选 "Add python.exe to PATH"。
  pause
  exit /b 1
)
echo 正在安装肥鱼指挥官...
echo.
%PY% "%~dp0install.py" %*
echo.
pause
