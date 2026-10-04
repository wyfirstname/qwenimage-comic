@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   Download Qwen-Image 2.1 models (~14.6 GB)
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Run start.bat once first to create it.
  pause
  exit /b 1
)

set "HF_ENDPOINT=https://hf-mirror.com"
".venv\Scripts\python.exe" scripts\download_models.py

echo.
echo Done. If there were errors above, run this again (resumable).
pause
