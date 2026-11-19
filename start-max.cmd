@echo off
setlocal EnableExtensions
pushd "%~dp0"
if errorlevel 1 exit /b 2

if not exist ".venv\Scripts\python.exe" (
    echo [MAX] Creating a local Python environment...
    py -3.13 -m venv ".venv" >nul 2>&1
    if errorlevel 1 py -3.11 -m venv ".venv"
    if errorlevel 1 (
        echo [MAX] Python 3.11 or 3.13 is required. Install Python first.
        popd
        exit /b 2
    )
)

if not exist ".venv\.max-desktop-installed" (
    echo [MAX] Installing desktop dependencies inside .venv...
    ".venv\Scripts\python.exe" -m pip install -e ".[desktop]"
    if errorlevel 1 (
        echo [MAX] Installation failed. Check Python and internet access.
        popd
        exit /b 3
    )
    >".venv\.max-desktop-installed" echo MAX desktop environment installed
)

echo [MAX] Launching your native MAX desktop preview...
".venv\Scripts\python.exe" -m max_core.native_desktop
set "exitCode=%errorlevel%"
if not "%exitCode%"=="0" (
    echo [MAX] App exited with code %exitCode%.
    pause
)
popd
exit /b %exitCode%
