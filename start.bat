@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   Local Image Generator - Qwen-Image 2.1
echo ============================================
echo.

REM ---------- 1. locate python ----------
set PYLAUNCH=
where python >nul 2>nul && set PYLAUNCH=python
if not defined PYLAUNCH (
    where py >nul 2>nul && set PYLAUNCH=py
)
if not defined PYLAUNCH (
    echo [ERROR] Python not found in PATH.
    echo         Install Python 3.10 - 3.12 from https://www.python.org/downloads/
    echo         and make sure "Add python.exe to PATH" is checked.
    echo.
    pause
    exit /b 1
)

REM ---------- 2. virtual environment ----------
if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Creating virtual environment .venv ...
    %PYLAUNCH% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
) else (
    echo [1/4] Virtual environment found.
)

set PY=.venv\Scripts\python.exe

REM ---------- 3. dependencies ----------
if not exist ".venv\.deps_ok" (
    echo [2/4] Installing web dependencies ...
    "%PY%" -m pip install --upgrade pip -q
    "%PY%" -m pip install -r backend\requirements.txt -q
    if errorlevel 1 (
        echo [ERROR] Dependency install failed. Try a mirror:
        echo         "%PY%" -m pip install -r backend\requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
        pause
        exit /b 1
    )
    echo ok> ".venv\.deps_ok"
) else (
    echo [2/4] Dependencies already installed.
)

REM ---------- 4. config ----------
if not exist ".env" (
    echo [3/4] Creating .env from .env.example ...
    copy ".env.example" ".env" >nul
) else (
    echo [3/4] .env found.
)

REM ---------- 5. port check ----------
set PORT=8000
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /r /c:"TCP.*:%PORT% .*LISTENING"') do (
    echo [WARN] Port %PORT% is already in use by PID %%a.
    echo        Stop that process, or change PORT in .env
    echo.
    pause
    exit /b 1
)

echo [4/4] Starting server ...
echo.
echo   Open in browser:  http://127.0.0.1:%PORT%
echo   Stop the server:  press Ctrl+C in this window
echo.

cd backend
"..\%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%

echo.
echo Server stopped.
pause
endlocal
