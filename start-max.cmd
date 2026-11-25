@echo off
setlocal EnableExtensions
pushd "%~dp0"
if errorlevel 1 exit /b 2

set "MAX_ARG1=%~1"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if errorlevel 1 (
        echo [MAX] The existing .venv cannot run a supported Python.
        echo [MAX] Rename .venv to .venv-old, then run start-max.cmd again to recreate it.
        if /i not "%MAX_ARG1%"=="--smoke-test" if not defined MAX_NO_PAUSE pause
        popd
        exit /b 2
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo [MAX] Creating a local Python environment...
    py -3.13 -m venv ".venv" >nul 2>&1
    if errorlevel 1 py -3.12 -m venv ".venv" >nul 2>&1
    if errorlevel 1 py -3.11 -m venv ".venv" >nul 2>&1
    if errorlevel 1 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
        if not errorlevel 1 python -m venv ".venv" >nul 2>&1
    )
    if not exist ".venv\Scripts\python.exe" (
        echo [MAX] Python 3.11 or newer is required. Install Python 3.11+ and try again.
        if /i not "%MAX_ARG1%"=="--smoke-test" if not defined MAX_NO_PAUSE pause
        popd
        exit /b 2
    )
)

".venv\Scripts\python.exe" "scripts\prepare_native_desktop.py"
if errorlevel 1 (
    echo [MAX] Installation failed. Check Python and internet access, then relaunch.
    if /i not "%MAX_ARG1%"=="--smoke-test" if not defined MAX_NO_PAUSE pause
    popd
    exit /b 3
)

echo [MAX] Launching MAX Desktop...
".venv\Scripts\python.exe" -m max_core.native_desktop %*
set "exitCode=%errorlevel%"
if not "%exitCode%"=="0" (
    echo [MAX] App exited with code %exitCode%.
    if /i not "%MAX_ARG1%"=="--smoke-test" if not defined MAX_NO_PAUSE pause
)
popd
exit /b %exitCode%
