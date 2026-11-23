#!/usr/bin/env bash
# Run one tasks.py task from the repo, appending its output to logs/<log>_task.log.
#   qe-run.sh <log> <task> [KEY=VALUE ...]      e.g.  qe-run.sh recorder record SEND=1
# Used by the systemd units next to this file; exec keeps the PID, so systemd's SIGINT reaches
# Python and the task stops the way Ctrl+C stops it (the recorder flushes and runs QC).
set -euo pipefail
repo="$(cd "$(dirname "$(readlink -f "$0")")/../../.." && pwd)"
log="$1"
shift
cd "$repo"
mkdir -p logs
printf '\n=== %s  %s ===\n' "$(TZ=Asia/Kolkata date '+%F %T IST')" "$*" >>"logs/${log}_task.log"
if [ -x .venv/bin/python ]; then
    exec .venv/bin/python tasks.py "$@" >>"logs/${log}_task.log" 2>&1
fi
# no .venv yet: systemd's PATH lacks mise's shims, so look for uv there too
uv="$(command -v uv || true)"
[ -n "$uv" ] || uv="$HOME/.local/share/mise/shims/uv"
exec "$uv" run python tasks.py "$@" >>"logs/${log}_task.log" 2>&1
