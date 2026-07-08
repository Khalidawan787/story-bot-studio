param(
    [switch]$InstallDailyTask,
    [switch]$SkipFfmpegInstall
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

function Write-Step($Message) {
    Write-Host ""
    Write-Host "== $Message ==" -ForegroundColor Cyan
}

function Set-EnvValue($Path, $Key, $Value) {
    $Line = "$Key=$Value"
    if (Test-Path $Path) {
        $Lines = Get-Content $Path
    } else {
        $Lines = @()
    }

    $Found = $false
    $Updated = foreach ($ExistingLine in $Lines) {
        if ($ExistingLine -like "$Key=*") {
            $Found = $true
            $Line
        } else {
            $ExistingLine
        }
    }

    if (-not $Found) {
        $Updated += $Line
    }

    Set-Content -Path $Path -Value $Updated -Encoding UTF8
}

Write-Step "Checking Python"
$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) {
    throw "Python is not installed. Install Python 3.11+ from https://www.python.org/downloads/ then run this script again."
}
python --version

Write-Step "Creating virtual environment"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
}

Write-Step "Installing Python packages"
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-optional.txt

Write-Step "Checking FFmpeg"
$Ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
$Ffprobe = Get-Command ffprobe -ErrorAction SilentlyContinue

if ((-not $Ffmpeg -or -not $Ffprobe) -and -not $SkipFfmpegInstall) {
    $Winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($Winget) {
        Write-Host "FFmpeg not found. Installing FFmpeg with winget..."
        winget install --id Gyan.FFmpeg --source winget --accept-package-agreements --accept-source-agreements
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
        $Ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
        $Ffprobe = Get-Command ffprobe -ErrorAction SilentlyContinue
    }
}

if (-not $Ffmpeg -or -not $Ffprobe) {
    throw "FFmpeg was not found. Install FFmpeg, then run this script again."
}

Write-Step "Preparing .env"
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
    } else {
        New-Item -ItemType File ".env" | Out-Null
    }
}

Set-EnvValue ".env" "FFMPEG_BIN" $Ffmpeg.Source
Set-EnvValue ".env" "FFPROBE_BIN" $Ffprobe.Source
Set-EnvValue ".env" "YOUTUBE_CLIENT_SECRET_FILE" "client_secret.json"
Set-EnvValue ".env" "YOUTUBE_TOKEN_FILE" "token.json"
Set-EnvValue ".env" "YOUTUBE_PRIVACY_STATUS" "public"
Set-EnvValue ".env" "ENABLE_BACKGROUND_MUSIC" "true"
Set-EnvValue ".env" "AUTO_GENERATE_MISSING_IMAGES" "true"
# Spread uploads out (private + scheduled publishAt) so a daily batch does not
# go public all at once. First video ~1h out, then one every 3h.
Set-EnvValue ".env" "YOUTUBE_SCHEDULE_UPLOADS" "true"
Set-EnvValue ".env" "YOUTUBE_SCHEDULE_INTERVAL_HOURS" "3"
Set-EnvValue ".env" "YOUTUBE_SCHEDULE_FIRST_DELAY_HOURS" "1"

Write-Step "Checking YouTube files"
if (Test-Path "client_secret.json") {
    Write-Host "client_secret.json found."
} else {
    Write-Host "client_secret.json missing. Copy it into this folder before uploading to YouTube." -ForegroundColor Yellow
}

if (Test-Path "token.json") {
    Write-Host "token.json found. If upload fails on this PC, delete token.json and login again."
} else {
    Write-Host "token.json missing. First upload will open Google login." -ForegroundColor Yellow
}

Write-Step "Running health check"
.\.venv\Scripts\python.exe -m compileall src web_dashboard.py
.\.venv\Scripts\python.exe -m src.cli daily --count 5 --dry-run true

if ($InstallDailyTask) {
    Write-Step "Installing daily 5-video task"
    powershell.exe -ExecutionPolicy Bypass -File ".\scripts\install_daily_5_task.ps1"
    powershell.exe -ExecutionPolicy Bypass -File ".\scripts\install_upload_retry_task.ps1"
}

Write-Step "Setup complete"
Write-Host "Dashboard command:"
Write-Host "  .\.venv\Scripts\python.exe web_dashboard.py"
Write-Host ""
Write-Host "Dashboard URL:"
Write-Host "  http://127.0.0.1:8000"
Write-Host ""
Write-Host "Daily manual test command:"
Write-Host "  .\.venv\Scripts\python.exe -m src.cli daily --count 5 --upload true"
