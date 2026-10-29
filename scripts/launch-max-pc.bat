@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo MAX is not installed. Follow docs/PC-WINDOWS.md first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m max pc run
if errorlevel 1 pause
endlocal
