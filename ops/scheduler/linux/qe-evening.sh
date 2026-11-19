#!/usr/bin/env bash
# The evening chain, as on Windows (register_eod.ps1):
#   eod && iv-history   (logs/eod_task.log),  then  evening  (logs/evening_task.log)
# On a catch-up run before 19:30 IST (the PC was off at 20:00) eod gets DATE= the previous
# weekday, because without DATE it downloads *today's* files, which NSE has not published yet.
# `evening` picks "today after 19:30, else the last trading day" by itself.
set -u
here="$(dirname "$(readlink -f "$0")")"
args=()
if [[ "$(TZ=Asia/Kolkata date +%H:%M)" < "19:30" ]]; then
    d="$(TZ=Asia/Kolkata date -d yesterday +%F)"
    while [ "$(date -d "$d" +%u)" -gt 5 ]; do d="$(date -d "$d - 1 day" +%F)"; done
    args=("DATE=$d")
fi
"$here/qe-run.sh" eod eod "${args[@]}" && "$here/qe-run.sh" eod iv-history
"$here/qe-run.sh" evening evening
