@echo off
title Story Bot Dashboard
cd /d "%~dp0"
echo ================================================
echo   Starting Story Bot Dashboard...
echo   The browser will open automatically.
echo   Keep this window open while you work.
echo ================================================
echo.
".venv\Scripts\python.exe" web_dashboard.py
pause
