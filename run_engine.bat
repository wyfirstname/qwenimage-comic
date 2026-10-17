@echo off
setlocal
cd /d "%~dp0"

REM ============================================
REM   Run the engine as its own process (optional)
REM   clients must set ENGINE_TRANSPORT=http and
REM   ENGINE_URL=http://127.0.0.1:8189
REM ============================================

set PORT=8189

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

echo [engine] starting ... status at http://127.0.0.1:%PORT%/engine/v1/status
"%PY%" -m uvicorn engine.serve:app --host 127.0.0.1 --port %PORT%

echo.
echo Engine stopped.
pause
endlocal
