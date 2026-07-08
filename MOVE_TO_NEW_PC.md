# Move To Another PC

Copy this full project folder to the new PC:

`kids_learning_youtube_bot`

Then open PowerShell inside that folder and run:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\setup_new_pc.ps1
```

To also install the daily 5-video Windows Task Scheduler job and the 30-minute upload retry job:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\setup_new_pc.ps1 -InstallDailyTask
```

After setup, open the dashboard:

```powershell
.\.venv\Scripts\python.exe web_dashboard.py
```

Or just double-click `START_DASHBOARD.bat` — it installs everything the first
time, rebuilds automatically if the copied setup is broken, and keeps the
window open (with a "type R to restart" prompt) if anything goes wrong so you
can read the error instead of it closing on you.

Dashboard URL:

`http://127.0.0.1:8000`

Notes:

- VS Code does not need to stay open.
- For daily automation, the new PC must be on, logged in, awake, and connected to internet.
- If YouTube upload fails on the new PC, delete `token.json` and run one upload again so Google login opens fresh.
- Keep `.env`, `client_secret.json`, and `token.json` private.
