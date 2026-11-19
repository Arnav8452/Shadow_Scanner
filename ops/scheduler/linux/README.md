# Running the engine on Linux (Omarchy / Arch) with systemd

These units replace the Windows Task Scheduler jobs in `ops/scheduler/windows/`. Everything is
the same as on Windows: paper only, local only, and live trading stays locked. They are
**systemd user units**. They run as you, need no root, and keep running without a login
once linger is on.

| Unit | When | What | Log |
|---|---|---|---|
| `qe-recorder` | weekdays 08:45 IST, and at boot during market hours | `tasks.py record SEND=1` | `logs/recorder_task.log` |
| `qe-shadow-live` | weekdays 08:46 IST, and at boot during market hours | `tasks.py shadow-live SEND=1` | `logs/shadow_live_task.log` |
| `qe-cockpit` | weekdays 08:45 IST, and at boot | `tasks.py cockpit OPEN=0` → http://127.0.0.1:8765 | `logs/cockpit_task.log` |
| `qe-evening` | weekdays 20:00 IST | `eod && iv-history`, then `evening` | `logs/eod_task.log`, `logs/evening_task.log` |
| `qe-event-arb` | daily 23:30 IST | `tasks.py event-arb SEND=1` (buyback paper account) | `logs/event_arb_task.log` |

The Python tasks also write their own logs under `logs/`, as before.

## Behaviour after a crash

These are the parts that differ from Windows.

- **Crash and reboot during market hours.** With linger on, the recorder, engine and cockpit
  start again at boot. The recorder and engine only start between 08:40 and 15:50 IST on
  weekdays (`in_session.sh`). Exchange holidays are left to the tasks, which read
  `config/rules/holidays.csv` and exit.
- **A task dies.** `Restart=on-failure` restarts it after 60 s, up to 6 times in 30 min.
  A broken token or similar fault then stops the restarts instead of looping. Check it with
  `journalctl --user -u qe-recorder`.
- **Stop.** systemd sends SIGINT, which is the same as Ctrl+C. The recorder flushes its files
  and runs QC, just as it does when you stop it by hand.
- **Missed evening run.** `Persistent=true` runs a missed 20:00 or 23:30 job at the next boot.
  The chain picks its dates as follows:
  - If the catch-up starts before 19:30, `eod` gets `DATE=` the previous weekday. Without a
    date it would fetch today's files, which NSE hasn't published yet.
  - If the catch-up falls in market hours, it waits until 15:50 (`after_session.sh`). This
    keeps the downloads and rebuilds off the recorder's CPU.
- **Timers use IST** (`Asia/Kolkata` in `OnCalendar=`), whatever the PC's timezone is.

## Install

1. **Copy the repo and data from the backup.** Restore `Trading.tar`, for example to
   `~/Trading`. Check that `.env`, `cockpit.key`, `data/` and `argon/` came across. Then
   delete the Windows virtualenv: `rm -rf ~/Trading/.venv` (it holds `.exe` files).
2. **Install the tools:** `sudo pacman -S uv git`. uv downloads Python 3.12 itself if needed.
3. **Get a first manual run working before installing anything**, the same rule as on
   Windows:
   ```sh
   cd ~/Trading && uv sync && uv run pytest -q
   uv run python tasks.py cockpit            # opens http://127.0.0.1:8765; Ctrl+C to stop
   ```
4. **Install the units:**
   ```sh
   ~/Trading/ops/scheduler/linux/install.sh
   loginctl enable-linger "$USER"            # run without a login and after reboots
   systemctl --user list-timers 'qe-*'
   ```
5. **On Windows, disable the old jobs**, so a dual-boot never records twice into the same
   folders: `Get-ScheduledTask QuantEngine-* | Disable-ScheduledTask`.

Remove everything with `ops/scheduler/linux/install.sh --uninstall`. This leaves the data,
logs and `.env` untouched.

### Everyday commands

```sh
systemctl --user status qe-recorder qe-shadow-live qe-cockpit
systemctl --user restart qe-shadow-live qe-cockpit      # e.g. after pulling new code
systemctl --user start qe-evening                       # run the evening chain now
journalctl --user -u qe-recorder --since today
tail -f ~/Trading/logs/recorder_task.log
```

## System settings: yours to apply (not done by `install.sh`)

**Time sync and timezone.** The lag figures need an accurate clock:
```sh
sudo timedatectl set-timezone Asia/Kolkata
sudo timedatectl set-ntp true
timedatectl timesync-status
```
`timesync-status` should show an offset of a few ms. For tighter sync, `chrony` can replace
`systemd-timesyncd`.

**Reboot after a kernel crash instead of hanging.** Add `panic=10` to the kernel command line.
Omarchy boots with Limine: add it to the `KERNEL_CMDLINE` line in `/etc/default/limine`, then
run `sudo limine-update`. Check the Omarchy manual if your install differs. With linger and
the boot start above, a crash mid-session costs about a minute of data, not the whole session.

**Reboot after a hard freeze, where no panic happens.** Use the hardware watchdog: set
`RuntimeWatchdogSec=30s` in `/etc/systemd/system.conf`, then reboot.

**Stop sleep from cutting the feed:** `systemctl mask sleep.target suspend.target`, or set
the lid and idle actions in `/etc/systemd/logind.conf`. This applies only if the PC
sleeps on its own.

## The RAM problem (0x1A / 0x154 crashes on Windows)

Software can soften the damage, but it can't make faulty RAM reliable:

1. **Find out which stick is at fault.** This is the real fix. Run MemTest86+ (Arch package
   `memtest86+-efi`, added as a Limine entry) for a few passes with **each 8 GB stick alone**
   in the first slot. Then run the PC on the good stick. 8 GB is enough for the recorder,
   engine and cockpit. Avoid heavy research runs during market hours.
2. **Mixed sticks:** the SK Hynix and Crucial sticks are a mixed pair. If both pass alone but
   fail together, turn XMP/EXPO off in the BIOS, or set a lower common speed. Mismatched
   timings are a common cause.
3. **A fixed bad address range:** if MemTest86+ always reports errors at the same addresses,
   you can exclude that range with the kernel option `memmap=<size>$<address>`, for example
   `memmap=64M$0x1F0000000`. Limine needs no escaping of `$`. Alternatively, `memtest=4`
   makes the kernel test and reserve bad memory at boot. Use it only if your kernel has
   `CONFIG_MEMTEST` (`zgrep MEMTEST /proc/config.gz`).
4. **Limit the damage until then:** `panic=10`, the watchdog and the restart-at-boot units
   above. The recorder flushes to disk every 30 s (`recorder.flush_interval_s`), so a crash
   loses about the last 30 s plus the reboot time.
