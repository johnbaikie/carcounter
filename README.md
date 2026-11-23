# Car counter

Counts cars crossing a road with a TF-Luna lidar break-beam on a Raspberry Pi 5, photographs each one
with a Camera Module 3, and logs everything to CSV. Includes a LAN web page with counts, charts,
photos, health status and live-tunable settings.

## Hardware
- Raspberry Pi 5, Camera Module 3 (imx708)
- Benewake TF-Luna lidar on the Pi UART (`/dev/ttyAMA0`, 115200 baud)
  - TF-Luna 1 (5V) → pin 2, 2 (RXD) → pin 8 (GPIO14 TX), 3 (TXD) → pin 10 (GPIO15 RX), 4 (GND) → pin 6
  - `config.txt`: `enable_uart=1`, `dtparam=uart0=on`; serial console disabled
- Geekworm X1202 UPS (optional): fuel gauge on I2C 0x36, power-loss signal on GPIO6
- USB drive for data (falls back to the SD card if missing)

The lidar points across the road at a target on the far side. A car breaks the beam (shorter
distance or lost signal) and is counted once, however long it blocks the beam.

## Files
| File | Purpose |
|---|---|
| `carcounter.py` | Counter: 100 Hz lidar reads, auto-calibrated baseline, event detection, photos, CSV + health log |
| `carcount_data.py` | Shared paths, CSV readers, and the tunable settings (defaults + limits) |
| `web.py` | Web page on port 8080: today, status, charts, photos, per-day pages, Settings |
| `maintenance.py` | Hourly: move SD fallback data to USB, photo retention, `summary_daily.csv`, SQLite update |
| `carcount_db.py` | SQLite copy of the CSVs for fast queries (derived; safe to delete and rebuild) |
| `alerts.py` | Phone notifications via Pushover: problems, power, highlights, daily summary |
| `ups_monitor.py` | X1202 UPS: logs power events, safe shutdown on low battery, RTC wake to restart |
| `tests/` | `python3 tests/test_detector.py`, `test_fallback.py`, `test_db.py`, `test_alerts.py` |
| `systemd/` | Service and timer units |
| `luna_camera.py` | Original prototype (superseded) |

## Data layout
```
/mnt/carcount/                    (or ~/carcount_data if the USB drive is missing)
  YYYY-MM-DD/events.csv           timestamp, count_today, distance_cm, strength, min_distance_cm,
                                  duration_s, baseline_cm, photo
  YYYY-MM-DD/health.csv           every 60 s: lidar fps/valid %, baseline, temps, fan, throttling,
                                  disk, camera, UPS
  YYYY-MM-DD/photos/*.jpg         YYYYMMDD_HHMMSS_mmm.jpg
  summary_daily.csv               date, total, h00..h23
```

## SQLite copy
The CSVs are the record. Every hour `maintenance.py` also updates `~/.local/share/carcount/carcount.db`
(only changed CSVs are re-imported). Tables `events` and `health`; views `daily` and `hourly`.
```bash
sudo apt install sqlite3        # optional command-line client
sqlite3 ~/.local/share/carcount/carcount.db "SELECT hour, round(avg(cars),1) FROM hourly GROUP BY hour"
python3 carcount_db.py --rebuild   # rebuild from all CSVs
```

## Install
```bash
sudo apt install python3-serial python3-picamera2 python3-smbus2 python3-libgpiod python3-pil
git clone <this repo> ~/src
# USB drive (example; use your drive's UUID from `lsblk -f`):
#   UUID=XXXX-XXXX /mnt/carcount vfat defaults,nofail,noatime,flush,uid=1000,gid=1000,umask=022,x-systemd.device-timeout=10s 0 0
sudo cp ~/src/systemd/carcount*.service ~/src/systemd/carcount-maintenance.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now carcount carcount-web carcount-alerts carcount-maintenance.timer
# UPS monitor only once batteries are installed and tested:
#   sudo python3 ~/src/ups_monitor.py --dry-run
#   sudo sh ~/src/scripts/install-ups.sh        # root-owned copy in /opt/carcount + unit
#   sudo systemctl enable --now carcount-ups
# The monitor runs as root, so it runs from /opt/carcount, not ~/src. Re-run install-ups.sh
# after changing ups_monitor.py, alerts.py or carcount_data.py (the Power page reminds you).
```
Paths assume user `carcount` with the code in `/home/carcount/src`.

## Use
- Web page: `http://<pi-address>:8080/` (no login - keep it on a private network)
- Logs: `journalctl -u carcount -f`
- Run by hand (stop the service first): `python3 carcounter.py -v` prints every reading
- Settings are edited on the web Settings page and stored in `~/.config/carcount/config.json`;
  the counter reloads on `SIGHUP` and recalibrates on `SIGUSR1`.
- Notifications: install the Pushover app, then enter your user key and an application API token on the
  web Alerts page (stored in `~/.config/carcount/pushover.json`, mode 600). `python3 alerts.py --once`
  shows what would be sent right now.

## License
MIT - see [LICENSE](LICENSE).
