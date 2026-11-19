#!/usr/bin/env bash
# If it is now a weekday between FROM and TO (IST), sleep until TO; then exit 0. Evening jobs
# that systemd catches up after a missed run (Persistent=true) use it so their downloads and
# rebuilds never compete with the live recorder during market hours.
#   after_session.sh [FROM] [TO]       defaults 08:40 15:50
set -eu
here="$(dirname "$(readlink -f "$0")")"
to="${2:-15:50}"
if "$here/in_session.sh" "${1:-08:40}" "$to"; then
    wait_s=$(( $(TZ=Asia/Kolkata date -d "today $to" +%s) - $(date +%s) ))
    echo "market hours: waiting ${wait_s}s until $to IST"
    sleep "$wait_s"
fi
