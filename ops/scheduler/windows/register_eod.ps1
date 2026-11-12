# Registers a Windows Task Scheduler job that downloads NSE end-of-day files (bhavcopies,
# delivery, participant OI, index closes incl. India VIX) every weekday evening and refreshes
# the IV history, then the evening job (delivery watchlist, results dates, filings). NOT run
# automatically — the owner runs it once:
#
#   powershell -ExecutionPolicy Bypass -File ops\scheduler\windows\register_eod.ps1
#
# Remove with:  Unregister-ScheduledTask -TaskName QuantEngine-EOD -Confirm:$false
#
# NSE publishes these around 18:00-19:30 IST; 20:00 leaves a margin. Holidays simply show as
# "missing". No login or token is needed (public NSE archives).

param(
    [string]$Time = "20:00",
    [string]$Repo = (Resolve-Path "$PSScriptRoot\..\..\..").Path
)

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:APPDATA "Python\Python312\Scripts\uv.exe" }
if (-not (Test-Path $uv)) { throw "uv not found; install it with: pip install --user uv" }

New-Item -ItemType Directory -Force (Join-Path $Repo "logs") | Out-Null

$cmd = "/c cd /d `"$Repo`" && `"$uv`" run python tasks.py eod >> logs\eod_task.log 2>&1 && `"$uv`" run python tasks.py iv-history >> logs\eod_task.log 2>&1 & `"$uv`" run python tasks.py evening >> logs\evening_task.log 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Time
# Run on battery too: on 12 Nov 2026 the default (no start on batteries) skipped the run on a
# laptop that was unplugged at 20:00. The 60-minute limit covers the evening job's filings.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 60)

Register-ScheduledTask -TaskName "QuantEngine-EOD" -Action $action -Trigger $trigger `
    -Settings $settings -Force `
    -Description "NSE end-of-day downloads + IV history + evening job. Data only. Repo: $Repo" | Out-Null

Write-Host "Registered QuantEngine-EOD on weekdays at $Time (repo $Repo)."
