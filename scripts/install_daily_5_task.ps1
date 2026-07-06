$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ScriptPath = Join-Path $ProjectRoot "scripts\run_daily_5.bat"
$TaskName = "KidsLearningBotDaily5"

schtasks /Create /TN $TaskName /TR "`"$ScriptPath`"" /SC DAILY /ST 08:00 /F

Write-Host "Installed task: $TaskName"
Write-Host "Runs daily at 08:00 local time, which is 20:00 Pakistan time while this PC is on PDT."
Write-Host "Creates/uploads 5 different-topic videos."
