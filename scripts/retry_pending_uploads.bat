@echo off
cd /d "%~dp0\.."
if not exist logs mkdir logs
echo ==== %date% %time% Retry uploads start ====>> logs\upload_retry.log
".venv\Scripts\python.exe" -m src.cli catch-up-daily --target 5 --upload true --start-hour 8 >> logs\upload_retry.log 2>&1
".venv\Scripts\python.exe" -m src.cli retry-uploads --limit 20 >> logs\upload_retry.log 2>&1
echo ==== %date% %time% Retry uploads end ====>> logs\upload_retry.log
