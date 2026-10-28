@echo off
rem Double-click to open Rank Checker. The first run sets everything up.
cd /d "%~dp0"
title Rank Checker

set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul && set "PY=python"
if not defined PY (
  echo Rank Checker needs Python 3.9 or newer.
  echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH" during setup.
  echo Then double-click this file again.
  pause
  exit /b 1
)

if exist ".venv\.ready" goto start
echo First run: setting up Rank Checker. This takes a minute or two and only happens once.
if exist ".venv" rmdir /s /q ".venv"
%PY% -m venv .venv || goto fail
".venv\Scripts\python" -m pip install --quiet --upgrade pip
".venv\Scripts\python" -m pip install --quiet -r requirements.txt || goto fail
".venv\Scripts\python" -m playwright install chromium || goto fail
type nul > ".venv\.ready"
echo Setup complete.
echo.

:start
".venv\Scripts\python" app.py
pause
exit /b 0

:fail
echo.
echo Setup didn't finish. Check your internet connection and double-click this file to try again.
pause
exit /b 1
