@echo off
cd /d "%~dp0\.."
if not exist logs mkdir logs
echo ==== %date% %time% Daily 2 start ====>> logs\daily_runner.log
".venv\Scripts\python.exe" -m src.cli retry-uploads --limit 20 >> logs\daily_runner.log 2>&1
".venv\Scripts\python.exe" -m src.cli daily --count 2 --upload true >> logs\daily_runner.log 2>&1
".venv\Scripts\python.exe" -m src.cli retry-uploads --limit 20 >> logs\daily_runner.log 2>&1
echo ==== %date% %time% Daily 2 end ====>> logs\daily_runner.log
