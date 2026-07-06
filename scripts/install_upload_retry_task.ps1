$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ScriptPath = Join-Path $ProjectRoot "scripts\retry_pending_uploads.bat"
$TaskName = "KidsLearningBotRetryUploads"

schtasks /Create /TN $TaskName /TR "`"$ScriptPath`"" /SC MINUTE /MO 30 /F

Write-Host "Installed task: $TaskName"
Write-Host "Runs every 30 minutes while the PC is on/logged in."
Write-Host "Retries rendered/upload-failed videos and thumbnails."
