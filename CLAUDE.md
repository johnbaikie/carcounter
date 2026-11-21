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
- Fan: config.txt has dtparam=cooling_fan=on (cooling_device0 works); original fan connector is faulty, planning Active Cooler or GPIO fan
- Storage: 123 GB USB drive (vfat, UUID 344F-1B4F) fstab-mounted at /mnt/carcount (nofail,flush; fstab backup /etc/fstab.bak-20261120)
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
- carcounter.py: v1 counter. Tunables from settings; SIGHUP = reload (camera size/enable -> self-exit, systemd restarts),
  SIGUSR1 = recalibrate. Writes /dev/shm/carcount-live.json 2x/s. `-v` prints frames, `--no-camera`.
  Output: <root>/YYYY-MM-DD/{events.csv,health.csv,photos/,thumbs/}. Photos/CSV/health on a writer thread.
  health.csv every 60 s (fps, valid%, baseline, lidar/CPU temp, fan rpm, throttled, disk, camera, UPS). sd_notify watchdog.
- ups_monitor.py: PLD + fuel gauge; shuts down only when on battery AND low; sets RTC wakealarm (+30 min) first.
  Writes /run/carcount-ups.json for the health log. `--once`, `--dry-run`
- maintenance.py (hourly timer): SD fallback -> USB merge, photo retention (365 d / min free GB), summary_daily.csv
- web.py: LAN page on port 8080 (no auth): today, status, hourly/14-day line charts, photos, /day/<date>, /days,
  /settings (live beam reading, edit settings -> signals counter, recalibrate button)
- Deploy code changes without sudo: kill $(systemctl show -p MainPID --value carcount[-web]) (Restart=always)
- systemd/: carcount.service (Type=notify, WatchdogSec=60), carcount-web.service, carcount-maintenance.{service,timer},
  carcount-ups.service (NOT enabled until batteries tested). Logs: journalctl -u carcount -f
- Hardware watchdog already active by default (systemd RuntimeWatchdogUSec=1min)
- sudo needs a password (since 2026-11-20 reboot) - Claude can't install units; user runs sudo commands
- luna_camera.py: original prototype (superseded)

## v1 features to build
- 100 Hz reads; baseline auto-calibrated to target; count on "shorter than baseline - margin OR signal lost" for 2-3 frames; re-arm after ~0.3 s stable target; long blockages = one event
- Camera: lock focus (AfMode Manual, LensPosition ~0.4 for ~2.5 m); reset serial buffer after capture; ms-precision filenames
- CSV log per event: timestamp, distance, strength, photo; date folders on USB drive with SD fallback
- systemd services: counter + UPS monitor (low-battery safe shutdown, auto restart on power return)

## v2 / later
- DONE: retention, daily/hourly summaries, health logging, watchdog, web page
- TODO: alerts (needs a channel: ntfy/email/etc), night imaging, direction/speed, vehicle classification
- Open questions: crossings vs visits (cul-de-sac in/out double counting), filtering people/bikes/dogs
