# Registers a daily Windows Task Scheduler job that runs the event-arbitrage scan and sends
# Telegram alerts. NOT run automatically — the owner runs it once:
#
#   powershell -ExecutionPolicy Bypass -File ops\scheduler\windows\register_event_arb.ps1
#
# Remove with:  Unregister-ScheduledTask -TaskName QuantEngine-EventArb -Confirm:$false
#
# Why 23:30 daily: the NSE announcements RSS holds roughly the current day only, and filings
# arrive late into the evening and on weekends. Running once late every day keeps the
# candidate history (data/event_arb/candidates.jsonl) gap-free. The PC must be on (or able to
# wake) at that time.

param(
    [string]$Time = "23:30",
    [string]$Repo = (Resolve-Path "$PSScriptRoot\..\..\..").Path
)

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:APPDATA "Python\Python312\Scripts\uv.exe" }
if (-not (Test-Path $uv)) { throw "uv not found; install it with: pip install --user uv" }

New-Item -ItemType Directory -Force (Join-Path $Repo "logs") | Out-Null

$cmd = "/c cd /d `"$Repo`" && `"$uv`" run python tasks.py event-arb SEND=1 >> logs\event_arb_task.log 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName "QuantEngine-EventArb" -Action $action -Trigger $trigger `
    -Settings $settings -Force `
    -Description "Event-arbitrage scan (information only; never trades). Repo: $Repo" | Out-Null

Write-Host "Registered QuantEngine-EventArb daily at $Time (repo $Repo)."
