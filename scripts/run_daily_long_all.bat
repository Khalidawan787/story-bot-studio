@echo off
cd /d "%~dp0\.."
if not exist logs mkdir logs
echo ==== %date% %time% Daily long-form all channels start ====>> logs\daily_long_all.log
".venv\Scripts\python.exe" -m src.cli retry-uploads --limit 20 >> logs\daily_long_all.log 2>&1
".venv\Scripts\python.exe" -m src.cli daily-long-all --scenes 20 --upload true >> logs\daily_long_all.log 2>&1
".venv\Scripts\python.exe" -m src.cli retry-uploads --limit 20 >> logs\daily_long_all.log 2>&1
echo ==== %date% %time% Daily long-form all channels end ====>> logs\daily_long_all.log
