# Registers a Windows Task Scheduler job that starts the cockpit gateway (the local web page at
# http://127.0.0.1:8765) every weekday morning. NOT run automatically: the owner runs it once,
# after a manual day with `uv run python tasks.py cockpit` has worked:
#
#   powershell -ExecutionPolicy Bypass -File ops\scheduler\windows\register_cockpit.ps1
#
# Remove with:  Unregister-ScheduledTask -TaskName QuantEngine-Cockpit -Confirm:$false
#
# The gateway only displays and relays: stopping or restarting it never affects the recorder
# (QuantEngine-Recorder) or the engine (QuantEngine-ShadowLive), which keep recording and paper
# trading on their own. It does not open a browser; bookmark http://127.0.0.1:8765.

param(
    [string]$Time = "08:45",
    [string]$Repo = (Resolve-Path "$PSScriptRoot\..\..\..").Path
)

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:APPDATA "Python\Python312\Scripts\uv.exe" }
if (-not (Test-Path $uv)) { throw "uv not found; install it with: pip install --user uv" }

New-Item -ItemType Directory -Force (Join-Path $Repo "logs") | Out-Null

$cmd = "/c cd /d `"$Repo`" && `"$uv`" run python tasks.py cockpit OPEN=0 >> logs\cockpit_task.log 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 16)

Register-ScheduledTask -TaskName "QuantEngine-Cockpit" -Action $action -Trigger $trigger `
    -Settings $settings -Force `
    -Description "Cockpit page (local only, paper only). Display and relay; never trades. Repo: $Repo" | Out-Null

Write-Host "Registered QuantEngine-Cockpit on weekdays at $Time (repo $Repo). Open http://127.0.0.1:8765"
