#!/usr/bin/env bash
# Exit 0 when it is a weekday between FROM and TO (IST), else 1. systemd's ExecCondition= uses
# it so a boot or a restart outside market hours skips the unit instead of starting it.
#   in_session.sh [FROM] [TO]          defaults 08:40 15:50
# Exchange holidays are not checked here: the tasks read config/rules/holidays.csv and exit.
set -eu
from="${1:-08:40}"
to="${2:-15:50}"
dow="$(TZ=Asia/Kolkata date +%u)"   # 1 = Monday ... 7 = Sunday
now="$(TZ=Asia/Kolkata date +%H:%M)"
[ "$dow" -le 5 ] || exit 1
[[ "$now" > "$from" || "$now" == "$from" ]] || exit 1
[[ "$now" < "$to" ]] || exit 1
exit 0
