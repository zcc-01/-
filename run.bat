@echo off
chcp 65001 >nul
setlocal

REM ============================================================
REM  Agri Trace - one click launcher for Windows
REM  Double click this file. No need to open a terminal.
REM ============================================================

cd /d "%~dp0"

set "PORT=8000"
set "PY="

REM --- 1. locate a real python interpreter --------------------
REM Priority: bundled runtime > py launcher > python on PATH
if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
    set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
)
if not defined PY (
    for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
        if exist "%%D\python.exe" set "PY=%%D\python.exe"
    )
)
if not defined PY (
    for /f "delims=" %%P in ('where py 2^>nul') do (
        if not defined PY set "PY=%%P"
    )
)

if not defined PY (
    echo.
    echo  [ERROR] Python interpreter not found.
    echo.
    echo  Please install Python 3.11+ from https://www.python.org/downloads/
    echo  Remember to tick "Add python.exe to PATH" during installation.
    echo.
    echo  If Python is installed but still not found, try running this
    echo  script from the WorkBuddy terminal instead.
    echo.
    pause
    exit /b 1
)

echo.
echo  ============================================
echo    Agri Trace Platform - Local Launcher
echo  ============================================
echo.
echo  Python : %PY%
echo  Project: %CD%
echo.

"%PY%" -c "import sys;print('  Version:', sys.version.split()[0])"

REM --- 2. ensure dependencies --------------------------------
"%PY%" -c "import fastapi, uvicorn, sqlalchemy, jwt, qrcode, jinja2" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [1/3] Installing dependencies, this may take a few minutes...
    "%PY%" -m pip install -q -r requirements.txt
    if errorlevel 1 (
        echo.
        echo  [ERROR] Dependency installation failed. Check your network.
        echo          You can retry with a mirror:
        echo          pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
        echo.
        pause
        exit /b 1
    )
    echo  [1/3] Dependencies installed.
) else (
    echo  [1/3] Dependencies already satisfied.
)

REM --- 3. seed database if missing ---------------------------
if not exist "data\app.db" (
    echo  [2/3] Creating database with demo data...
    "%PY%" scripts\seed.py
    if errorlevel 1 (
        echo.
        echo  [ERROR] Database initialization failed. See messages above.
        echo.
        pause
        exit /b 1
    )
) else (
    echo  [2/3] Database already exists, keeping current data.
)

REM --- 4. start server ---------------------------------------
echo  [3/3] Starting server on http://127.0.0.1:%PORT%
echo.
echo  --------------------------------------------------------
echo    Home page      : http://127.0.0.1:%PORT%/
echo    Scan page      : http://127.0.0.1:%PORT%/trace/PC-20251101-DEMO-ITM0001
echo    API docs       : http://127.0.0.1:%PORT%/docs
echo.
echo    Demo account   : farmer01 / Passw0rd!
echo.
echo    Press Ctrl+C to stop the server.
echo  --------------------------------------------------------
echo.

REM open browser after a short delay, using ping as a portable sleep
start "" /b cmd /c "ping -n 5 127.0.0.1 >nul 2>&1 & start http://127.0.0.1:%PORT%/trace/PC-20251101-DEMO-ITM0001"

"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% --reload

echo.
echo  Server stopped.
pause
