@echo off
setlocal
cd /d "%~dp0"

REM ============================================
REM   Run ONE app standalone (no portal)
REM   Usage:  run_app.bat imagegen ^| comic ^| portrait
REM   engine is loaded in-process of that app
REM ============================================

set APPID=%~1
if "%APPID%"=="" (
    echo Usage: run_app.bat ^<imagegen^|comic^|portrait^>
    pause
    exit /b 1
)
if not exist "apps\%APPID%\manifest.json" (
    echo [ERROR] unknown app: %APPID%
    pause
    exit /b 1
)

set PORT=8101
if /i "%APPID%"=="comic"    set PORT=8102
if /i "%APPID%"=="portrait" set PORT=8103

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

echo [app:%APPID%] starting ... open http://127.0.0.1:%PORT%
"%PY%" -m uvicorn apps.%APPID%.backend.routes:create_app --factory --host 127.0.0.1 --port %PORT%

echo.
echo Server stopped.
pause
endlocal
