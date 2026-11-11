# Registers a Windows Task Scheduler job that downloads NSE end-of-day files (bhavcopies,
# delivery, participant OI, index closes incl. India VIX) every weekday evening and refreshes
# the IV history. NOT run automatically — the owner runs it once:
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

$cmd = "/c cd /d `"$Repo`" && `"$uv`" run python tasks.py eod >> logs\eod_task.log 2>&1 && `"$uv`" run python tasks.py iv-history >> logs\eod_task.log 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName "QuantEngine-EOD" -Action $action -Trigger $trigger `
    -Settings $settings -Force `
    -Description "NSE end-of-day downloads + IV history. Data only. Repo: $Repo" | Out-Null

Write-Host "Registered QuantEngine-EOD on weekdays at $Time (repo $Repo)."
