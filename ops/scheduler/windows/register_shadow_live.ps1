# Registers a Windows Task Scheduler job that starts the V1 live shadow scanner (watch-only) every
# weekday morning. NOT run automatically — the owner runs it once, after the first manual
# session has worked:
#
#   powershell -ExecutionPolicy Bypass -File ops\scheduler\windows\register_shadow_live.ps1
#
# Remove with:  Unregister-ScheduledTask -TaskName QuantEngine-ShadowLive -Confirm:$false
#
# Needs: the account switched to Upstox Plus (30-level depth), an Upstox token in .env
# (UPSTOX_ANALYTICS_TOKEN, or `uv run python tasks.py upstox-login` each morning), and the
# recorder task running too (the scanner reads its OI walls and previous-day levels).
# It exits by itself on holidays and after the session; with SEND=1, A+ signals go to Telegram
# labelled as unvalidated hypotheses. It never places orders.

param(
    [string]$Time = "08:46",
    [string]$Repo = (Resolve-Path "$PSScriptRoot\..\..\..").Path
)

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:APPDATA "Python\Python312\Scripts\uv.exe" }
if (-not (Test-Path $uv)) { throw "uv not found; install it with: pip install --user uv" }

New-Item -ItemType Directory -Force (Join-Path $Repo "logs") | Out-Null

$cmd = "/c cd /d `"$Repo`" && `"$uv`" run python tasks.py shadow-live SEND=1 >> logs\shadow_live_task.log 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 8)

Register-ScheduledTask -TaskName "QuantEngine-ShadowLive" -Action $action -Trigger $trigger `
    -Settings $settings -Force `
    -Description "V1 shadow scanner (watch-only, 30-level depth). Never trades. Repo: $Repo" | Out-Null

Write-Host "Registered QuantEngine-ShadowLive on weekdays at $Time (repo $Repo)."
Write-Host "Needs UPSTOX_ANALYTICS_TOKEN in .env (or 'uv run python tasks.py upstox-login' each morning)."
