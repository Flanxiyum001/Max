@echo off
setlocal EnableExtensions
pushd "%~dp0"
if errorlevel 1 exit /b 2
if /I "%~1"=="--tts-only" (
    echo [MAX] Qwen speech-only preview: uses model weights you have already downloaded.
    echo [MAX] Only speech output changes. Your current chat and microphone settings stay in use.
    echo [MAX] Requires Python 3.12 and sufficient disk space. Ollama is not required for TTS.
) else (
    echo [MAX] Full local preview downloads model weights if missing.
    echo [MAX] Requires Python 3.12, running local Ollama and sufficient disk space.
    echo [MAX] No paid speech or chat API is used in this profile.
)
choice /C YN /M "Continue with local setup"
if errorlevel 2 goto cancel
if not exist ".venv-local-voice\Scripts\python.exe" (
    py -3.12 -m venv ".venv-local-voice"
    if errorlevel 1 goto failed
)
if not exist ".venv-local-voice\.max-local-voice-v1" (
    ".venv-local-voice\Scripts\python.exe" -m pip install -e ".[desktop,local-voice]"
    if errorlevel 1 goto failed
    >".venv-local-voice\.max-local-voice-v1" echo ready
)
".venv-local-voice\Scripts\python.exe" -m max_core.voice.local_runtime launch %*
set "exitCode=%errorlevel%"
popd
exit /b %exitCode%
:failed
echo [MAX] Local setup failed. Standard start-max.cmd is unchanged.
popd
exit /b 1
:cancel
popd
exit /b 0
