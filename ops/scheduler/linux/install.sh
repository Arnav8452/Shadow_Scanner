#!/usr/bin/env bash
# Install the Quant Engine systemd *user* units (Linux; tested target: Omarchy / Arch).
# Run it as yourself, NOT with sudo, from anywhere:
#     ops/scheduler/linux/install.sh            install, enable and start the timers
#     ops/scheduler/linux/install.sh --uninstall
# It never changes system settings; README.md lists the optional ones (panic reboot, watchdog,
# timezone) for you to apply yourself.
set -euo pipefail
here="$(dirname "$(readlink -f "$0")")"
repo="$(cd "$here/../../.." && pwd)"
dest="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
units=(qe-recorder qe-shadow-live qe-cockpit qe-evening qe-event-arb)

if [ "${1:-}" = "--uninstall" ]; then
    for u in "${units[@]}"; do
        systemctl --user disable --now "$u.timer" "$u.service" 2>/dev/null || true
        rm -f "$dest/$u.service" "$dest/$u.timer"
    done
    systemctl --user daemon-reload
    echo "Removed the qe-* units. Data, logs and .env are untouched."
    exit 0
fi

[ "$(id -u)" -ne 0 ] || { echo "Run as your own user, not root." >&2; exit 1; }
[ -f "$repo/tasks.py" ] || { echo "tasks.py not found under $repo" >&2; exit 1; }
[ -f "$repo/.env" ] || echo "WARNING: $repo/.env is missing; copy it from the backup (tokens)."
if [ -d "$repo/.venv/Scripts" ] && [ ! -d "$repo/.venv/bin" ]; then
    echo "$repo/.venv was made on Windows. Remove it (rm -rf .venv) and run this again." >&2
    exit 1
fi
command -v uv >/dev/null || { echo "uv not found: sudo pacman -S uv" >&2; exit 1; }

echo "== uv sync (creates .venv with Python 3.12+)"
(cd "$repo" && uv sync)

chmod +x "$here"/*.sh
mkdir -p "$dest" "$repo/logs"
for u in "${units[@]}"; do
    for kind in service timer; do
        sed "s|@REPO@|$repo|g" "$here/units/$u.$kind" >"$dest/$u.$kind"
    done
done
systemctl --user daemon-reload
systemd-analyze --user verify "$dest"/qe-*.service "$dest"/qe-*.timer || true
for u in "${units[@]}"; do systemctl --user enable "$u.timer"; done
# recorder, engine and cockpit also start at boot (guarded to market hours, except the cockpit)
systemctl --user enable qe-recorder.service qe-shadow-live.service qe-cockpit.service
systemctl --user start qe-recorder.timer qe-shadow-live.timer qe-cockpit.timer \
    qe-evening.timer qe-event-arb.timer

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
    echo
    echo "NEXT: let the units run without a login (and after a reboot):"
    echo "    loginctl enable-linger $USER"
fi
echo
echo "Installed for $repo. Check with:  systemctl --user list-timers 'qe-*'"
echo "Start today's services now (if in market hours):"
echo "    systemctl --user start qe-cockpit qe-recorder qe-shadow-live"
