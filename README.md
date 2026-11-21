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
| `maintenance.py` | Hourly: move SD fallback data to USB, photo retention, `summary_daily.csv` |
| `ups_monitor.py` | X1202 UPS: logs power events, safe shutdown on low battery, RTC wake to restart |
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

## Install
```bash
sudo apt install python3-serial python3-picamera2 python3-smbus2 python3-libgpiod python3-pil
git clone <this repo> ~/src
# USB drive (example; use your drive's UUID from `lsblk -f`):
#   UUID=XXXX-XXXX /mnt/carcount vfat defaults,nofail,noatime,flush,uid=1000,gid=1000,umask=022,x-systemd.device-timeout=10s 0 0
sudo cp ~/src/systemd/carcount*.service ~/src/systemd/carcount-maintenance.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now carcount carcount-web carcount-maintenance.timer
# UPS monitor only once batteries are installed and tested:
#   sudo python3 ~/src/ups_monitor.py --dry-run
#   sudo systemctl enable --now carcount-ups
```
Paths assume user `carcount` with the code in `/home/carcount/src`.

## Use
- Web page: `http://<pi-address>:8080/` (no login - keep it on a private network)
- Logs: `journalctl -u carcount -f`
- Run by hand (stop the service first): `python3 carcounter.py -v` prints every reading
- Settings are edited on the web Settings page and stored in `~/.config/carcount/config.json`;
  the counter reloads on `SIGHUP` and recalibrates on `SIGUSR1`.

## License
MIT - see [LICENSE](LICENSE).
