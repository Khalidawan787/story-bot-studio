$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Command = "-m src.cli ai-make --category animals --upload true"
$Times = @("09:00", "13:00", "17:00", "21:00", "23:00")

foreach ($Time in $Times) {
    $Name = "KidsLearningBot_$($Time.Replace(':', ''))"
    $Action = New-ScheduledTaskAction -Execute $Python -Argument $Command -WorkingDirectory $ProjectRoot
    $Trigger = New-ScheduledTaskTrigger -Daily -At $Time
    Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Description "Generate and upload kids learning YouTube Short" -Force
}

Write-Host "Installed Kids Learning Bot scheduled tasks."

