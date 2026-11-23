#!/usr/bin/env bash
# The evening chain, as on Windows (register_eod.ps1):
#   eod && iv-history   (logs/eod_task.log),  then  evening  (logs/evening_task.log)
# eod runs without DATE even on a catch-up run (the laptop was off at 20:00): today's files then
# come back "missing" (404, not an error), its catch-up step fetches the previous 7 days, and
# only an undated run saves the current-only files (F&O ban list, bulk/block deals, FII/DII) and
# the screener summary. `evening` picks "today after 19:30, else the last trading day" itself.
set -u
here="$(dirname "$(readlink -f "$0")")"
"$here/qe-run.sh" eod eod && "$here/qe-run.sh" eod iv-history
"$here/qe-run.sh" evening evening
