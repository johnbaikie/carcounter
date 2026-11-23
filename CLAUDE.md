# Car counter project

## Goal
Count cars passing the bottom of a cul-de-sac with a TF-Luna lidar beam, photograph each car, log counts.

## Hardware
- Raspberry Pi 5 (hostname carcounter, user carcount), Camera Module 3 (imx708)
- TF-Luna lidar on Pi UART: /dev/ttyAMA0, 115200 baud, frames 0x59 0x59 + dist/amp/temp + checksum
  - Wiring: TF-Luna 1(5V)->Pi pin 2, 2(RXD)->pin 8 (GPIO14 TX), 3(TXD)->pin 10 (GPIO15 RX), 4(GND)->pin 6, pin 5 unconnected
  - config.txt: enable_uart=1, dtparam=uart0=on; serial console disabled
- Geekworm X1202 UPS (4x18650). Fuel gauge I2C 0x36 (confirmed), power-loss on GPIO6, charge control GPIO16
  - EEPROM: PSU_MAX_CURRENT=5000, POWER_OFF_ON_HALT=1
  - Scripts cloned from https://github.com/suptronics/x120x (bat.py, pld.py, merged.py / *-trixie variants)
  - BATTERIES NOT INSTALLED YET (arriving soon). Do not enable the x120x-ups systemd service until they are in and bat.py/pld.py are tested.
- Fan: 80 mm 5 V fan on GPIO 5V/GND (always on, no tach -> fan_rpm 0 is normal). Optional: MOSFET on GPIO12 +
  dtoverlay=gpio-fan,gpiopin=12 (not done). Mount: hardware/pi_fan_mount.scad. Original fan connector is faulty
- Storage: 123 GB USB drive (vfat, UUID 344F-1B4F) fstab-mounted at /mnt/carcount (nofail,flush; fstab backup /etc/fstab.bak-20261120)
- Remote access: Tailscale (tailscaled enabled, operator=carcount); web at http://carcounter:8080 from tailnet devices. Never port-forward (no auth)
- Power: mains via outdoor extension cord -> 5V/5A USB-C into X1202; batteries are backup only (~5-9 h runtime)

## Site geometry
- Sensor near curb, 3-5 ft setback; road is 25 ft curb to curb (far curb ~8.5-9 m, at/over TF-Luna's 8 m rating)
- Traffic crosses left to right
- Plan: break-beam with a target at the far side (try retroreflective target; fallback: TF02-Pro/TFmini Plus, same protocol)
- Still measuring: exact distances, mount height (~50-60 cm), camera placement

## Current code
- Repo: https://github.com/johnbaikie/carcounter (PUBLIC - never commit photos, logs, config.json or secrets)
- carcount_data.py: shared paths (USB_ROOT /mnt/carcount, SD_ROOT ~/carcount_data), CSV fields, readers,
  and SETTINGS (tunable defaults/limits). Overrides in ~/.config/carcount/config.json (only non-defaults stored)
- carcounter.py: v1 counter. Schedule settings COUNT_START/STOP, PHOTO_START/STOP (HH:MM, equal = all day, wraps midnight;
  outside counting hours blockages are tracked but not logged). Tunables from settings; SIGHUP = reload (camera size/enable -> self-exit, systemd restarts),
  SIGUSR1 = recalibrate. Writes /dev/shm/carcount-live.json 2x/s. `-v` prints frames, `--no-camera`.
  Output: <root>/YYYY-MM-DD/{events.csv,health.csv,photos/,thumbs/}. Photos on a camera thread, CSV/health on a writer thread;
  a row waits <= PHOTO_WAIT_S for its photo (hung camera can't lose counts); startup test frame, else runs without camera.
  health.csv every 60 s (fps, valid%, baseline, lidar/CPU temp, fan rpm, throttled, disk, camera, UPS). sd_notify watchdog.
- alerts.py (carcount-alerts.service, user carcount): Pushover notifications, checks every 30 s. Problems (counter down,
  lidar/camera, beam blocked, USB missing, disk, CPU hot, under-voltage), power (mains/battery, Pi restarted = power cut),
  highlights (record day, busy hour, no cars in daytime), daily summary. ALERT_* settings on web /alerts.
  Keys in ~/.config/carcount/pushover.json (600, never commit). State ~/.local/share/carcount/alerts-state.json,
  sent log alerts-log.jsonl. Unsent messages retried 6 h. `--once` dry run, `--test`. ups_monitor sends before shutdown.
- ups_monitor.py: PLD + fuel gauge; shuts down only when on battery AND (low for N reads OR max time on battery);
  sets RTC wakealarm first. UPS_* settings re-read from config.json when it changes (runs as root, no signal needed).
  Writes /run/carcount-ups.json (state, pending shutdown, grace). `--once`, `--dry-run`
  Runs as ROOT from a root-owned copy in /opt/carcount (with alerts.py, carcount_data.py): `sudo sh scripts/install-ups.sh`
  installs/updates code + unit (hardened: ProtectHome=read-only etc.). Re-run after changing any of those 3 files;
  the Power page warns when the copy differs from ~/src. Never point the unit back at ~/src (carcount-writable -> root).
- maintenance.py (hourly timer): SD fallback -> USB merge, photo retention (365 d / min free GB), summary_daily.csv, SQLite update
- carcount_db.py: derived SQLite copy at ~/.local/share/carcount/carcount.db (ext4, WAL), incremental by file size/mtime,
  auto-rebuild on schema change or corruption. CSVs stay the source of truth. Web does not use it yet.
- web.py: LAN page on port 8080 (no auth): today (counts + one-line status, charts, photos), /day/<date>, /days,
  /view/<day>/<photo> (photo viewer: earlier/later buttons, arrow keys, swipe, crosses days),
  /settings (full status card, live beam reading, edit settings -> signals counter, recalibrate button),
  WEB_THEME Auto/Christmas/Plain (Auto = 15 Nov-6 Jan): lights, snow, emoji, wagon (static/wagon.svg, original art) drives a road per new car,
  /power (UPS status - direct GPIO/I2C read if monitor not running, input voltage, battery chart, power events from journal, UPS_* settings)
- Deploy code changes: Settings page > System card Restart buttons (import-check first), or
  kill $(systemctl show -p MainPID --value carcount[-web]) (Restart=always)
- systemd/: carcount.service (Type=notify, WatchdogSec=60), carcount-web.service, carcount-maintenance.{service,timer},
  carcount-alerts.service, carcount-ups.service (NOT enabled until batteries tested). Logs: journalctl -u carcount -f
- Hardware watchdog already active by default (systemd RuntimeWatchdogUSec=1min)
- sudo needs a password (since 2026-11-20 reboot) - Claude can't install units; user runs sudo commands
- luna_camera.py: original prototype (superseded)

## v1 features (DONE)
- 100 Hz reads; auto-calibrated baseline; blocked = shorter than baseline - margin OR signal lost for N frames; re-arm after stable target; long blockage = one event
- Camera: manual focus, ms filenames, capture on writer thread (no serial stall)
- CSV per event in date folders on USB with SD fallback; maintenance merges SD days back to USB (tests/test_fallback.py)
- systemd: counter, web, maintenance timer ENABLED. carcount-ups installed, NOT enabled (batteries)
- Still to verify on hardware: RTC wake after shutdown (Pi 5 + POWER_OFF_ON_HALT=1); real mains-loss test once batteries are in
- Tests: python3 tests/test_detector.py; tests/test_fallback.py; tests/test_db.py; tests/test_alerts.py; tests/test_writer.py
  (tests must point USB_ROOT/SD_ROOT in BOTH carcounter and carcount_data at a temp dir - never the real drive)

## v2 / later
- DONE: retention, daily/hourly summaries, health logging, watchdog, web page
- DONE: alerts via Pushover (alerts.py)
- TODO: night imaging, direction/speed, vehicle classification
- Open questions: crossings vs visits (cul-de-sac in/out double counting), filtering people/bikes/dogs
