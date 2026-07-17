$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ScriptPath = Join-Path $ProjectRoot "scripts\run_daily_2.bat"
$TaskName = "KidsLearningBotDaily2"

schtasks /Create /TN $TaskName /TR "`"$ScriptPath`"" /SC DAILY /ST 08:00 /F

Write-Host "Installed task: $TaskName"
Write-Host "Runs daily at 08:00 local time, which is 20:00 Pakistan time while this PC is on PDT."
Write-Host "Creates/uploads 2 higher-quality, interactive Kids Shorts."

$LongScriptPath = Join-Path $ProjectRoot "scripts\run_daily_long_all.bat"
$LongTaskName = "StoryBotDailyLongAll"
schtasks /Create /TN $LongTaskName /TR "`"$LongScriptPath`"" /SC DAILY /ST 09:00 /F

Write-Host "Installed task: $LongTaskName"
Write-Host "Runs daily at 09:00 local time."
Write-Host "Creates/uploads one ~5-minute 16:9 video for every connected channel."
