# Registers a Windows Task Scheduler job that starts the market-data recorder (M2) every
# weekday morning. NOT run automatically — the owner runs it once, after the first manual
# session has worked:
#
#   powershell -ExecutionPolicy Bypass -File ops\scheduler\windows\register_recorder.ps1
#
# Remove with:  Unregister-ScheduledTask -TaskName QuantEngine-Recorder -Confirm:$false
#
# Before it can record real data each day:
#   1. An Upstox token in .env: UPSTOX_ANALYTICS_TOKEN (1 year), or run
#      `uv run python tasks.py upstox-login` each morning if the feed refuses it.
#   2. The PC must stay awake 08:45–15:50 (sleep and Windows Update restarts cut the feed;
#      see docs/RUNBOOK.md).
# The recorder exits by itself on exchange holidays (config/rules/holidays.csv) and after the
# session window; QC runs at the end and, with SEND=1, the verdict goes to Telegram.

param(
    [string]$Time = "08:45",
    [string]$Repo = (Resolve-Path "$PSScriptRoot\..\..\..").Path
)

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:APPDATA "Python\Python312\Scripts\uv.exe" }
if (-not (Test-Path $uv)) { throw "uv not found; install it with: pip install --user uv" }

New-Item -ItemType Directory -Force (Join-Path $Repo "logs") | Out-Null

$cmd = "/c cd /d `"$Repo`" && `"$uv`" run python tasks.py record SEND=1 >> logs\recorder_task.log 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 8)

Register-ScheduledTask -TaskName "QuantEngine-Recorder" -Action $action -Trigger $trigger `
    -Settings $settings -Force `
    -Description "Market-data recorder (M2). Data only; never trades. Repo: $Repo" | Out-Null

Write-Host "Registered QuantEngine-Recorder on weekdays at $Time (repo $Repo)."
Write-Host "Needs UPSTOX_ANALYTICS_TOKEN in .env (or 'uv run python tasks.py upstox-login' each morning)."
