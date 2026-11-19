@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ================================================
echo  Agri trace platform - local dev server
echo ================================================
echo.

echo [1/3] Checking Python and dependencies...
python -V
python -c "import fastapi, sqlalchemy, jwt, bcrypt, jinja2, uvicorn" >nul 2>&1
if errorlevel 1 goto INSTALL
echo      dependencies OK
goto SEED

:INSTALL
echo      missing packages, installing (needs network)...
python -m pip install -r requirements-dev.txt
if errorlevel 1 goto INSTALL_FAILED

:SEED
echo.
echo [2/3] Creating database and seeding demo data...
python -m app.db.seed_cli
if errorlevel 1 goto SEED_FAILED

echo.
echo [3/3] Starting server...
echo.
echo   Home        http://127.0.0.1:8000
echo   Ops         http://127.0.0.1:8000/ops
echo   Regulator   http://127.0.0.1:8000/regulator
echo   Farmer      http://127.0.0.1:8000/farmer
echo   API docs    http://127.0.0.1:8000/docs
echo.
echo   Accounts, password is Passw0rd! for all:
echo     operator01    platform ops / cert review / alerts
echo     regulator01   regulator / traceback / CSV export
echo     farmer01      farmer / apply cert / farm dashboard
echo     consumer01    consumer / used for 403 demo
echo.
echo   Stop: Ctrl+C or close this window
echo ================================================
echo.

start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8000/login"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

echo.
echo Server stopped.
pause
exit /b 0

:INSTALL_FAILED
echo.
echo [FAILED] pip install failed.
echo          run manually: python -m pip install -r requirements-dev.txt
pause
exit /b 1

:SEED_FAILED
echo.
echo [FAILED] database seeding failed, see the error above.
pause
exit /b 1
