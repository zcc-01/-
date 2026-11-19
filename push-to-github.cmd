@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ================================================
echo  Push feat/identity-ops  ->  github.com/zcc-01/-
echo ================================================
echo.

git rev-parse --abbrev-ref HEAD
git log --oneline -1
echo.

echo Pushing... (a credential window may pop up; use the zcc-01 account
echo or paste a Personal Access Token as the password)
echo.

git push -u origin feat/identity-ops --force
if errorlevel 1 goto failed

echo.
echo [OK] Pushed.
echo      https://github.com/zcc-01/-/tree/feat/identity-ops
goto end

:failed
echo.
echo [FAILED] Nothing was overwritten - the server rejected the push.
echo   * 403 / Permission denied  -> this account has no write access
echo   * Authentication failed    -> wrong password, paste a PAT instead
echo   * Could not resolve host   -> no network to github.com
echo.

:end
echo.
pause
