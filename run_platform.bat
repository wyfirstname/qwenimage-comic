@echo off
setlocal
cd /d "%~dp0"

REM ============================================
REM   Qwen-Image workbench (platform mode, default)
REM   portal + apps + engine in ONE process
REM   entry page: http://127.0.0.1:<PORT>/
REM   single app: run_app.bat <imagegen|comic|portrait>
REM ============================================

set PORT=8000
if exist ".env" (
    for /f "tokens=1,* delims==" %%a in ('findstr /b /i "PORT=" ".env" 2^>nul') do set PORT=%%b
)
if not defined PORT set PORT=8000

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

echo [platform] starting ... open http://127.0.0.1:%PORT%
"%PY%" -m uvicorn portal.backend.main:app --host 127.0.0.1 --port %PORT%

echo.
echo Server stopped.
pause
endlocal
