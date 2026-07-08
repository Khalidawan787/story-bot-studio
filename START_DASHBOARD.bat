@echo off
setlocal enabledelayedexpansion
title Story Bot Dashboard
cd /d "%~dp0"
echo ================================================
echo   Starting Story Bot Dashboard...
echo   The browser will open automatically.
echo   Keep this window open while you work.
echo ================================================
echo.

rem First run on a new PC: .venv is not there yet, so build it once.
if not exist ".venv\Scripts\python.exe" (
    call :setup
    if not exist ".venv\Scripts\python.exe" goto :setup_failed
)

rem A .venv copied from another PC is NOT portable and will crash on launch.
rem Make sure this one actually works BEFORE we open the dashboard; if the
rem core packages can't import, rebuild the environment from scratch.
".venv\Scripts\python.exe" -c "import flask, src.config" >nul 2>nul
if errorlevel 1 (
    echo This PC's setup looks incomplete or was copied from another computer.
    echo Rebuilding the environment. This can take a few minutes...
    echo.
    if exist ".venv" rmdir /s /q ".venv"
    call :setup
    if not exist ".venv\Scripts\python.exe" goto :setup_failed
)

:run
echo.
echo Dashboard URL:  http://127.0.0.1:8000
echo (A browser tab opens automatically. Close this window to stop the bot.)
echo.
".venv\Scripts\python.exe" web_dashboard.py
set "EXITCODE=%ERRORLEVEL%"

echo.
echo ------------------------------------------------
if "%EXITCODE%"=="0" (
    echo The dashboard was closed.
) else (
    echo The dashboard stopped unexpectedly ^(code %EXITCODE%^).
    echo The error message is shown above this line.
    echo.
    echo Common fixes:
    echo   - Make sure the dashboard is not already open in another window.
    echo   - If YouTube login is broken, delete token.json and try again.
    echo   - Re-run this file; a broken setup rebuilds automatically.
)
echo ------------------------------------------------
echo.
echo Type R then Enter to restart the dashboard, or just press Enter to close.
set "CHOICE="
set /p "CHOICE=> "
if /i "%CHOICE%"=="R" goto :run
exit /b %EXITCODE%

:setup
echo First time setup on this PC. Installing Python packages...
echo This can take a few minutes. Please wait.
echo.
powershell.exe -ExecutionPolicy Bypass -File ".\scripts\setup_new_pc.ps1"
goto :eof

:setup_failed
echo.
echo Setup did not finish. Make sure Python 3.11+ is installed:
echo   https://www.python.org/downloads/
echo Then run this file again.
echo.
echo Press any key to close.
pause >nul
exit /b 1
