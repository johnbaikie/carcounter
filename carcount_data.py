"""Shared storage paths and readers for the car counter (used by counter, maintenance, web)."""

import csv
import glob
import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path

USB_ROOT = Path('/mnt/carcount')
SD_ROOT = Path('/home/carcount/carcount_data')
ROOTS = {'usb': USB_ROOT, 'sd': SD_ROOT}
UPS_STATE = Path('/run/carcount-ups.json')   # written by ups_monitor.py
DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

EVENT_FIELDS = ['timestamp', 'count_today', 'distance_cm', 'strength', 'min_distance_cm',
                'duration_s', 'baseline_cm', 'photo']
HEALTH_FIELDS = ['timestamp', 'frames_per_s', 'valid_pct', 'baseline_cm', 'events', 'sensor_errors',
                 'lidar_temp_c', 'cpu_temp_c', 'fan_rpm', 'throttled', 'disk_free_gb', 'storage',
                 'camera', 'ac_power', 'battery_v', 'battery_pct']


def usb_ok():
    try:
        return os.path.ismount(USB_ROOT) and os.access(USB_ROOT, os.W_OK)
    except OSError:
        return False


def storage_root():
    """USB drive if mounted and writable, else the SD card."""
    return USB_ROOT if usb_ok() else SD_ROOT


def read_roots():
    return [r for r in (USB_ROOT if usb_ok() else None, SD_ROOT) if r and r.is_dir()]


def append_csv(path, fields, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with open(path, 'a', newline='') as fh:
        w = csv.DictWriter(fh, fields, extrasaction='ignore')
        if new:
            w.writeheader()
        w.writerow(row)
        fh.flush()
        os.fsync(fh.fileno())


def read_csv(name, day):
    """Rows of <root>/<day>/<name> from USB and SD fallback, merged and time-sorted."""
    rows = []
    for root in read_roots():
        try:
            with open(root / str(day) / name, newline='') as fh:
                rows.extend(csv.DictReader(fh))
        except (OSError, csv.Error):
            pass
    rows.sort(key=lambda r: r.get('timestamp') or '')
    return rows


def events(day):
    return read_csv('events.csv', day)


def health(day):
    return read_csv('health.csv', day)


def all_days():
    days = set()
    for root in read_roots():
        days.update(p.name for p in root.iterdir() if p.is_dir() and DAY_RE.match(p.name))
    return sorted(days)


def hourly(rows):
    h = [0] * 24
    for r in rows:
        try:
            h[int(r['timestamp'][11:13])] += 1
        except (KeyError, ValueError, IndexError):
            pass
    return h


def ups_state(max_age_s=30):
    """Latest UPS reading from ups_monitor.py, or None if it isn't running."""
    try:
        if time.time() - UPS_STATE.stat().st_mtime > max_age_s:
            return None
        return json.loads(UPS_STATE.read_text())
    except (OSError, ValueError):
        return None


def read_sys(path, scale=1.0):
    try:
        return int(open(path).read().strip()) / scale
    except (OSError, ValueError):
        return None


def cpu_temp():
    return read_sys('/sys/class/thermal/thermal_zone0/temp', 1000)


def fan_rpm():
    for p in glob.glob('/sys/class/hwmon/hwmon*/fan1_input'):
        v = read_sys(p)
        if v is not None:
            return int(v)
    return None


def disk_free_gb(path):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize / 1e9
    except OSError:
        return None


def today():
    return date.today().isoformat()


# ====================================================================== tunable settings
# Single source of truth for defaults and limits. Overrides live in CONFIG_PATH
# (edited from the web page's Settings tab). apply: 'live' = takes effect on reload,
# 'restart' = counter restarts itself (~5 s without counting), 'next run' = maintenance job.
CONFIG_PATH = Path('/home/carcount/.config/carcount/config.json')
LIVE_STATE = Path('/dev/shm/carcount-live.json')   # written by carcounter ~2x/s for the web page
PHOTO_SIZES = ['1536x864', '2304x1296', '4608x2592']

SETTINGS = [
    # key, group, label, type, default, min, max, apply, help
    ('COUNT_START', 'Schedule', 'Start counting at', 'time', '00:00', None, None, 'live',
     'Same start and stop time = count all day. Stop earlier than start = overnight (e.g. 22:00 to 06:00).'),
    ('COUNT_STOP', 'Schedule', 'Stop counting at', 'time', '00:00', None, None, 'live',
     'Outside these hours the beam is still watched (baseline kept up to date) but nothing is counted.'),
    ('PHOTO_START', 'Schedule', 'Take photos from', 'time', '00:00', None, None, 'live',
     'Photos are only taken for counted cars, so this applies within the counting hours.'),
    ('PHOTO_STOP', 'Schedule', 'Take photos until', 'time', '00:00', None, None, 'live',
     'Same start and stop time = photos all day (while "Take photos" is on).'),
    ('MARGIN_CM', 'Detection', 'Trigger margin (cm)', 'int', 30, 5, 400, 'live',
     'Beam counts as blocked when the distance is this much shorter than the baseline.'),
    ('TRIGGER_FRAMES', 'Detection', 'Frames to trigger', 'int', 3, 1, 50, 'live',
     'Consecutive blocked readings needed to count (100 per second).'),
    ('REARM_S', 'Detection', 'Re-arm time (s)', 'float', 0.3, 0.05, 10, 'live',
     'Beam must be clear this long before the next car can be counted.'),
    ('MIN_AMP', 'Detection', 'Minimum signal strength', 'int', 100, 10, 5000, 'live',
     'Readings weaker than this count as "signal lost".'),
    ('NO_TARGET_TRIGGER_CM', 'Detection', 'No-target trigger distance (cm)', 'int', 700, 30, 1200, 'live',
     'Only used when there is no far-side target: count anything closer than this.'),
    ('CALIBRATE_S', 'Detection', 'Calibration time (s)', 'float', 3.0, 1, 30, 'live',
     'How long to sample the clear beam when calibrating.'),
    ('REBASELINE_S', 'Detection', 'Re-baseline after (s)', 'float', 10.0, 2, 600, 'live',
     'Clear but steadily farther than the baseline this long -> adopt the new distance.'),
    ('STUCK_S', 'Detection', 'Adopt blocking object after (s)', 'float', 600.0, 30, 7200, 'live',
     'A steady object (parked car) blocking this long becomes the new baseline.'),
    ('CAMERA_ENABLED', 'Camera', 'Take photos', 'bool', True, None, None, 'restart', ''),
    ('PHOTO_SIZE', 'Camera', 'Photo size', 'choice', '2304x1296', None, None, 'restart',
     'Larger = more detail, bigger files, slightly slower capture.'),
    ('LENS_POSITION', 'Camera', 'Focus (dioptres)', 'float', 0.4, 0.0, 10.0, 'live',
     '1 / distance in metres: 0.4 = 2.5 m, 0.25 = 4 m, 0 = infinity.'),
    ('SHORT_EXPOSURE', 'Camera', 'Prefer short exposure', 'bool', True, None, None, 'live',
     'Less motion blur, more noise in low light.'),
    ('HEALTH_EVERY_S', 'Logging', 'Health log interval (s)', 'int', 60, 10, 3600, 'live', ''),
    ('STATUS_EVERY_S', 'Logging', 'Journal status interval (s)', 'int', 600, 60, 86400, 'live', ''),
    ('PHOTO_KEEP_DAYS', 'Storage', 'Keep photos for (days)', 'int', 365, 0, 3650, 'next run',
     '0 = keep until the disk is nearly full. CSV logs are always kept.'),
    ('MIN_FREE_GB_USB', 'Storage', 'Minimum free space on USB (GB)', 'float', 5.0, 1, 100, 'next run',
     'Oldest photos are deleted to keep at least this much free.'),
    # Power: read by ups_monitor.py (runs as root), which re-reads the config file within a few seconds.
    ('UPS_SHUTDOWN_ENABLED', 'Power', 'Shut down on low battery', 'bool', True, None, None, 'ups',
     'Off = only log what would happen (use while testing new batteries).'),
    ('UPS_LOW_VOLTAGE', 'Power', 'Low battery voltage (V per cell)', 'float', 3.20, 3.0, 3.7, 'ups',
     'On battery and below this voltage counts as low. 18650 cells are nearly empty around 3.2 V.'),
    ('UPS_LOW_PERCENT', 'Power', 'Low battery charge (%)', 'int', 10, 0, 90, 'ups',
     'On battery and below this charge counts as low. 0 = use voltage only.'),
    ('UPS_LOW_COUNT', 'Power', 'Low readings before shutdown', 'int', 5, 1, 60, 'ups',
     'Consecutive low readings (2 s apart) required, so a brief dip does not shut down.'),
    ('UPS_MAX_ON_BATTERY_MIN', 'Power', 'Shut down after on battery (min)', 'int', 0, 0, 1440, 'ups',
     'Shut down after this long without mains even if the battery is not low. 0 = off.'),
    ('UPS_BOOT_GRACE_S', 'Power', 'Grace period after boot (s)', 'int', 120, 30, 1800, 'ups',
     'Never shut down this soon after the monitor starts, so you can log in and fix things.'),
    ('UPS_WAKE_AFTER_MIN', 'Power', 'Wake up after shutdown (min)', 'int', 30, 0, 1440, 'ups',
     'RTC alarm to power the Pi back on; it keeps running if mains is back. 0 = stay off.'),
    # Notifications: read by alerts.py (carcount-alerts.service) every check, no restart needed.
    ('ALERT_PROBLEMS', 'Notifications', 'Problems', 'bool', True, None, None, 'alerts',
     'Counter stopped, lidar or camera failure, beam blocked, USB drive missing, disk full, overheating.'),
    ('ALERT_POWER', 'Notifications', 'Power', 'bool', True, None, None, 'alerts',
     'Pi restarted (power cut?), mains lost/restored, battery low, low-battery shutdown.'),
    ('ALERT_HIGHLIGHTS', 'Notifications', 'Highlights', 'bool', True, None, None, 'alerts',
     'Record day, unusually busy hour, no cars for a long time in the daytime.'),
    ('ALERT_SUMMARY', 'Notifications', 'Daily summary', 'bool', True, None, None, 'alerts', ''),
    ('ALERT_SUMMARY_TIME', 'Notifications', 'Send daily summary at', 'time', '21:00', None, None, 'alerts', ''),
    ('ALERT_QUIET_H', 'Notifications', 'No cars alert after (hours)', 'float', 3.0, 0, 24, 'alerts',
     'Only between 07:00 and 20:00 and within counting hours. 0 = off.'),
    ('ALERT_BUSY_FACTOR', 'Notifications', 'Busy hour = this many times usual', 'float', 2.0, 1.2, 10, 'alerts',
     'Compared with the same hour over the last 14 days (needs a week of data, and at least 10 cars).'),
    ('ALERT_BLOCKED_MIN', 'Notifications', 'Beam blocked alert after (min)', 'int', 15, 1, 1440, 'alerts', ''),
    ('ALERT_CPU_TEMP', 'Notifications', 'CPU temperature alert (°C)', 'int', 80, 60, 90, 'alerts',
     'The Pi 5 slows itself down from about 85 °C.'),
    ('ALERT_BATTERY_PCT', 'Notifications', 'Battery low alert (%)', 'int', 30, 5, 90, 'alerts',
     'On battery and at or below this charge.'),
]
SETTING = {s[0]: dict(zip(('key', 'group', 'label', 'type', 'default', 'min', 'max', 'apply', 'help'), s))
           for s in SETTINGS}


def parse_setting(key, raw):
    """Convert/validate one value. Returns (value, error_message_or_None)."""
    s = SETTING[key]
    t = s['type']
    if t == 'bool':
        return (raw if isinstance(raw, bool) else str(raw).lower() in ('1', 'true', 'on', 'yes')), None
    if t == 'choice':
        return (raw, None) if raw in PHOTO_SIZES else (s['default'], f'{s["label"]}: invalid choice')
    if t == 'time':
        m = re.fullmatch(r'\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*', str(raw))
        if m and int(m[1]) < 24 and int(m[2]) < 60:
            return f'{int(m[1]):02d}:{m[2]}', None
        return s['default'], f'{s["label"]}: "{raw}" is not a time (HH:MM)'
    try:
        v = int(raw) if t == 'int' and float(raw) == int(float(raw)) else float(raw)
        if t == 'int' and v != int(v):
            raise ValueError
        v = int(v) if t == 'int' else v
    except (TypeError, ValueError):
        return s['default'], f'{s["label"]}: "{raw}" is not a {"whole " if t == "int" else ""}number'
    if not s['min'] <= v <= s['max']:
        return s['default'], f'{s["label"]}: must be between {s["min"]} and {s["max"]}'
    return v, None


def load_settings():
    """Defaults overlaid with valid values from CONFIG_PATH."""
    try:
        saved = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        saved = {}
    out = {}
    for key, s in SETTING.items():
        v, err = parse_setting(key, saved[key]) if key in saved else (s['default'], None)
        out[key] = s['default'] if err else v
    return out


def save_settings(values):
    """Write only values that differ from the defaults (so code-default changes still apply)."""
    data = {k: v for k, v in values.items() if k in SETTING and v != SETTING[k]['default']}
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    tmp.replace(CONFIG_PATH)


def in_window(start, stop, when=None):
    """True if `when` (datetime, default now) is within start..stop ("HH:MM", stop exclusive).
    start == stop means always; stop < start wraps past midnight."""
    if start == stop:
        return True
    now = (when or datetime.now()).strftime('%H:%M')
    return start <= now < stop if start < stop else now >= start or now < stop


# Pushover credentials (secret - kept out of config.json and the repo)
PUSHOVER_PATH = Path('/home/carcount/.config/carcount/pushover.json')
ALERT_LOG = Path('/home/carcount/.local/share/carcount/alerts-log.jsonl')   # sent notifications


def pushover_keys():
    try:
        d = json.loads(PUSHOVER_PATH.read_text())
        return (d['user'], d['token']) if d.get('user') and d.get('token') else None
    except (OSError, ValueError, KeyError):
        return None


def save_pushover_keys(user, token):
    PUSHOVER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PUSHOVER_PATH.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as fh:
        json.dump({'user': user, 'token': token}, fh)
    tmp.replace(PUSHOVER_PATH)


def live_state(max_age_s=3):
    try:
        if time.time() - LIVE_STATE.stat().st_mtime > max_age_s:
            return None
        return json.loads(LIVE_STATE.read_text())
    except (OSError, ValueError):
        return None
