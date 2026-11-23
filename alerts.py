#!/usr/bin/env python3
"""Phone notifications via Pushover (run by carcount-alerts.service).

Every 30 s checks for problems, power events and highlights, and sends a daily
summary. Each problem is sent once when it starts and once when it clears.
Which alerts are on, and the thresholds, are on the web page's Alerts tab;
Pushover keys are stored in ~/.config/carcount/pushover.json (never in the repo).

  --test   send a test notification and exit
  --once   run the checks once and print what would be sent (sends nothing)
"""

import argparse
import json
import logging
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import carcount_data as cd

API = 'https://api.pushover.net/1/messages.json'
STATE_PATH = Path('/home/carcount/.local/share/carcount/alerts-state.json')
CHECK_EVERY_S = 30
SAVE_EVERY_S = 300          # also how precisely "last seen" is known after a power cut
RETRY_FOR_S = 6 * 3600      # keep retrying unsent messages (network down) this long
DASHBOARD = 'http://carcounter:8080/'
DAYTIME = ('07:00', '20:00')  # "no cars" alert only in this window
MIN_HISTORY_DAYS = 7        # days of data needed before record / busy-hour highlights
BUSY_MIN_CARS = 10

PROBLEM, OK, POWER, HIGHLIGHT = '⚠️ Car counter problem', '✅ Car counter', '⚡ Car counter power', '⭐ Car counter'

log = logging.getLogger('alerts')


# ====================================================================== sending
def _post(title, message, priority=0, ts=None):
    """Returns (error or None, worth retrying)."""
    keys = cd.pushover_keys()
    if not keys:
        return 'Pushover keys not set (web page > Alerts)', False
    data = urllib.parse.urlencode({
        'token': keys[1], 'user': keys[0], 'title': title, 'message': message, 'priority': priority,
        'url': DASHBOARD, 'url_title': 'Open Car Counter', 'timestamp': int(ts or time.time())}).encode()
    try:
        with urllib.request.urlopen(API, data, timeout=15) as r:
            resp = json.loads(r.read() or b'{}')
        return (None, False) if resp.get('status') == 1 else (str(resp.get('errors') or resp), False)
    except urllib.error.HTTPError as e:
        try:
            err = '; '.join(json.loads(e.read()).get('errors', [])) or str(e)
        except ValueError:
            err = str(e)
        return f'Pushover refused: {err}', e.code >= 500 or e.code == 429
    except (OSError, ValueError) as e:
        return f'Network error: {e}', True


def record(title, message, err=None):
    try:
        cd.ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(cd.ALERT_LOG, 'a') as fh:
            fh.write(json.dumps({'time': datetime.now().isoformat(timespec='seconds'), 'title': title,
                                 'message': message, 'error': err}) + '\n')
    except OSError:
        pass


def send(title, message, priority=0, log_it=True):
    """Send one notification now. Returns None on success, else the error."""
    err, _ = _post(title, message, priority)
    if log_it:
        record(title, message, err)
    (log.warning if err else log.info)('Notify %s: %s | %s', 'FAILED (' + err + ')' if err else 'sent', title, message)
    return err


def recent(n=30):
    try:
        lines = cd.ALERT_LOG.read_text().splitlines()[-n:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def trim_log(keep=300):
    try:
        lines = cd.ALERT_LOG.read_text().splitlines()
        if len(lines) > 2 * keep:
            cd.ALERT_LOG.write_text('\n'.join(lines[-keep:]) + '\n')
    except OSError:
        pass


# ====================================================================== checks
def service_state(unit):
    try:
        return subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True,
                              timeout=5).stdout.strip() or 'unknown'
    except (OSError, subprocess.SubprocessError):
        return 'unknown'


def clock_synced():
    try:
        return subprocess.run(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value'], capture_output=True,
                              text=True, timeout=5).stdout.strip() == 'yes'
    except (OSError, subprocess.SubprocessError):
        return False


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def at(day, hhmm):
    return datetime.combine(day, datetime.strptime(hhmm, '%H:%M').time())


class Alerts:
    def __init__(self, dry=False):
        self.dry = dry
        try:
            self.state = json.loads(STATE_PATH.read_text())
        except (OSError, ValueError):
            self.state = {}
        for k, v in (('active', {}), ('sent', {}), ('outbox', [])):
            self.state.setdefault(k, v)
        self.saved = (0.0, None)
        self.prev_seen = self.state.get('last_seen')   # before check() overwrites it
        self.hist = (None, {}, {})   # (for day, {day: cars}, {day: hourly list}) - previous days, cached daily

    # ---------------------------------------------------------- plumbing
    def notify(self, title, message, priority=0):
        if self.dry:
            print(f'WOULD SEND [{title}] {message}')
            return
        err, retry = _post(title, message, priority)
        record(title, message, err + (' - will retry' if retry else '') if err else None)
        (log.warning if err else log.info)('Notify %s: %s | %s', f'FAILED ({err})' if err else 'sent', title, message)
        if err and retry:
            self.state['outbox'].append({'title': title, 'message': message, 'priority': priority, 'ts': time.time()})

    def retry_outbox(self):
        pending, self.state['outbox'] = self.state['outbox'], []
        for i, m in enumerate(pending):
            if time.time() - m['ts'] > RETRY_FOR_S:
                log.warning('Giving up on: %s', m['message'])
                continue
            err, retry = _post(m['title'], m['message'], m['priority'], m['ts'])
            if err and retry:                           # still offline - keep this and the rest
                self.state['outbox'].extend(pending[i:])
                return
            record(m['title'], m['message'], err or None)
            log.info('Sent late: %s', m['message']) if not err else log.warning('Dropped (%s): %s', err, m['message'])

    def condition(self, key, enabled, active, delay_s, title, msg_on, msg_off=None, priority=0):
        """Send msg_on once `active` has held for delay_s, and msg_off when it clears."""
        act, sent = self.state['active'], self.state['sent']
        if not enabled:
            act.pop(key, None)
            sent.pop(key, None)
            return
        now = datetime.now().timestamp()
        if active:
            act.setdefault(key, now)
            if key not in sent and now - act[key] >= delay_s:
                self.notify(title, msg_on() if callable(msg_on) else msg_on, priority)
                sent[key] = now
        else:
            since = act.pop(key, None)
            if sent.pop(key, None) is not None:
                off = msg_off(since) if callable(msg_off) else msg_off
                if off:
                    self.notify(OK if title == PROBLEM else title, off)

    def save(self, force=False):
        """Write the state when it changes, and every SAVE_EVERY_S for last_seen (spares the SD card)."""
        if self.dry:
            return
        core = json.dumps({k: v for k, v in self.state.items() if k != 'last_seen'}, sort_keys=True)
        if force or core != self.saved[1] or time.time() - self.saved[0] >= SAVE_EVERY_S:
            try:
                STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
                tmp = STATE_PATH.with_suffix('.tmp')
                tmp.write_text(json.dumps(self.state, sort_keys=True))
                tmp.replace(STATE_PATH)
                self.saved = (time.time(), core)
            except OSError as e:
                log.warning('Cannot save state: %s', e)

    def history(self, today):
        if self.hist[0] != today:
            cars, hourly = {}, {}
            for d in cd.all_days():
                if d < today.isoformat():
                    evs = cd.events(d)
                    cars[d], hourly[d] = len(evs), cd.hourly(evs)
            self.hist = (today, cars, hourly)
        return self.hist[1], self.hist[2]

    # ---------------------------------------------------------- startup
    def startup(self, cfg):
        """Pi just booted and we were running before -> report the gap (likely a power cut).
        Waits for the clock to sync first: right after power-on the Pi's clock still shows the
        time it was switched off, so the gap would look like a couple of minutes.
        Returns True when done, False to be called again."""
        try:
            uptime = float(Path('/proc/uptime').read_text().split()[0])
        except (OSError, ValueError, IndexError):
            return True
        last = self.prev_seen
        if uptime > 900 or not last or not cfg['ALERT_POWER']:
            return True
        if not clock_synced():
            return False                                    # try again next check (gives up after 15 min uptime)
        boot = datetime.now() - timedelta(seconds=uptime)
        seen = datetime.fromtimestamp(last)
        if seen >= boot - timedelta(seconds=60):
            return True                                     # we already ran this boot (service restarted)
        mins = (boot - seen).total_seconds() / 60
        self.notify(POWER, f'The Pi restarted at {boot:%H:%M}. Last seen running around {seen:%a %H:%M} '
                           f'(down about {mins:.0f} min){" - power cut?" if mins >= 5 else ""}.')
        return True

    # ---------------------------------------------------------- every 30 s
    def check(self):
        cfg = cd.load_settings()
        now = datetime.now()
        today = now.date()
        live = cd.live_state(max_age_s=15)
        rows = cd.health(today.isoformat())
        h = rows[-1] if rows else None
        fresh = h if h and (now - datetime.fromisoformat(h['timestamp'])).total_seconds() < 180 else None
        evs = cd.events(today.isoformat())

        # ---- problems
        P = cfg['ALERT_PROBLEMS']
        self.condition('counter', P, live is None, 90, PROBLEM,
                       lambda: f'The counter is not running (service {service_state("carcount")}). Nothing is being counted.',
                       'The counter is running again.')
        fps = num(fresh and fresh.get('frames_per_s'))
        self.condition('lidar', P, live is not None and fps is not None and fps < 50, 0, PROBLEM,
                       lambda: f'The lidar is not sending data ({fps:.0f} readings/s, expected 100). Check the wiring.',
                       'Lidar data is back to normal.')
        self.condition('camera', P, bool(cfg['CAMERA_ENABLED'] and fresh and fresh.get('camera') != 'ok'), 0, PROBLEM,
                       'The camera is not working - counting continues without photos.', 'The camera is working again.')
        blocked = (live or {}).get('event_s') or 0
        self.condition('blocked', P, blocked >= cfg['ALERT_BLOCKED_MIN'] * 60, 0, PROBLEM,
                       lambda: f'The beam has been blocked for {blocked / 60:.0f} min and the signal is unsteady '
                               '(parked car, fallen target, dirty sensor?). Nothing new is counted until it clears.',
                       'The beam is clear again.')
        usb = cd.usb_ok()
        self.condition('usb', P, not usb, 120, PROBLEM,
                       'The USB drive is not available - saving to the SD card instead. '
                       'The data is moved back when the drive returns.', 'The USB drive is back.')
        root = cd.storage_root()
        free = cd.disk_free_gb(root)
        min_free = cfg['MIN_FREE_GB_USB'] if usb else 10.0
        self.condition('disk', P, free is not None and free < min_free, 2 * 3600, PROBLEM,
                       lambda: f'Only {free:.1f} GB free on the {"USB drive" if usb else "SD card"}, '
                               'even after deleting old photos.', 'Disk space is OK again.')
        temp = cd.cpu_temp()
        # 5 °C hysteresis so a reading hovering at the limit doesn't flip hot/normal every check
        hot_limit = cfg['ALERT_CPU_TEMP'] - (5 if 'hot' in self.state['active'] else 0)
        rpm = cd.fan_rpm()
        self.condition('hot', P, temp is not None and temp >= hot_limit, 120, PROBLEM,
                       lambda: f'The Pi is hot: CPU {temp:.0f} °C{f" (fan {rpm} rpm)" if rpm else ""}. '
                               'It slows itself down from about 85 °C.',
                       lambda _: f'Temperature is back to normal ({temp:.0f} °C).')
        try:
            thr = int((fresh or {}).get('throttled') or '0', 16)
        except ValueError:
            thr = 0
        self.condition('undervolt', P, bool(thr & 0x1), 0, PROBLEM,
                       'Under-voltage: the power supply is not delivering enough. Check the cable and adapter.',
                       'Supply voltage is OK again.')

        # ---- power (UPS monitor if running, else the counter's health log)
        W = cfg['ALERT_POWER']
        ups = cd.ups_state()
        if ups:
            ac, volts, pct = ups.get('ac'), num(ups.get('volts')), num(ups.get('pct'))
        else:
            ac = num(fresh.get('ac_power')) if fresh else None
            volts, pct = (num(fresh.get('battery_v')), num(fresh.get('battery_pct'))) if fresh else (None, None)
        on_batt = ac is not None and not ac
        batt = ' '.join(x for x in (f'{volts:.2f} V' if volts else '', f'{pct:.0f}%' if pct is not None else '') if x)
        self.condition('mains', W, on_batt, 0, POWER, f'Mains power lost - running on battery ({batt or "level unknown"}).',
                       'Mains power is back.', priority=1)
        self.condition('battery', W, on_batt and pct is not None and pct <= cfg['ALERT_BATTERY_PCT'], 0, POWER,
                       f'Battery low: {batt}. The Pi shuts down safely when it is nearly empty.', None, priority=1)

        # ---- highlights
        H = cfg['ALERT_HIGHLIGHTS']
        hist, hist_hourly = self.history(today)
        if H and len(hist) >= MIN_HISTORY_DAYS and self.state.get('record_day') != today.isoformat():
            best_day = max(hist, key=hist.get)
            if len(evs) > hist[best_day] > 0:
                self.state['record_day'] = today.isoformat()
                self.notify(HIGHLIGHT, f'New record: {len(evs)} cars today, beating {hist[best_day]} on '
                                       f'{date.fromisoformat(best_day):%a %d %b}.')
        prev = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        key = prev.strftime('%Y-%m-%d %H')
        if self.state.get('busy_hour') != key:
            self.state['busy_hour'] = key
            recent_days = sorted(d for d in hist_hourly if d < prev.date().isoformat())[-14:]
            if H and len(recent_days) >= MIN_HISTORY_DAYS:
                day_evs = evs if prev.date() == today else cd.events(prev.date().isoformat())
                n = cd.hourly(day_evs)[prev.hour]
                usual = sum(hist_hourly[d][prev.hour] for d in recent_days) / len(recent_days)
                if n >= BUSY_MIN_CARS and n >= cfg['ALERT_BUSY_FACTOR'] * max(usual, 1):
                    self.notify(HIGHLIGHT, f'Busy hour: {n} cars between {prev:%H}:00 and {prev.hour + 1:02d}:00 '
                                           f'(usually about {usual:.0f}).')
        q = cfg['ALERT_QUIET_H']
        quiet_on = bool(q) and live is not None and cd.in_window(*DAYTIME) and \
            cd.in_window(cfg['COUNT_START'], cfg['COUNT_STOP'])
        ref = at(today, DAYTIME[0])
        if cfg['COUNT_START'] < cfg['COUNT_STOP']:
            ref = max(ref, at(today, cfg['COUNT_START']))
        last_car = datetime.fromisoformat(evs[-1]['timestamp']) if evs else None
        if last_car:
            ref = max(ref, last_car)
        gap_h = (now - ref).total_seconds() / 3600

        def quiet_off(since):
            new = last_car and since and last_car.timestamp() > since
            return f'Cars are being counted again (car at {last_car:%H:%M}).' if new else None
        self.condition('quiet', H, quiet_on and gap_h >= q, 0, HIGHLIGHT,
                       lambda: f'No cars counted for {gap_h:.1f} h'
                               f'{f" (last at {last_car:%H:%M})" if last_car else " today"}. '
                               'Quiet day, or is the beam misaligned / target moved?', quiet_off)

        # ---- daily summary
        if cfg['ALERT_SUMMARY'] and self.state.get('summary_day') != today.isoformat() \
                and now.strftime('%H:%M') >= cfg['ALERT_SUMMARY_TIME']:
            self.state['summary_day'] = today.isoformat()
            self.notify(f'🚗 {now:%a %d %b}: {len(evs)} cars', self.summary(now, evs, hist))
        elif not cfg['ALERT_SUMMARY'] and self.state.get('summary_day') != today.isoformat() \
                and now.strftime('%H:%M') >= cfg['ALERT_SUMMARY_TIME']:
            self.state['summary_day'] = today.isoformat()      # don't send it late if switched on afterwards

        self.state['last_seen'] = time.time()

    @staticmethod
    def summary(now, evs, hist):
        today = now.date()
        lines = [f'{len(evs)} cars counted by {now:%H:%M}.']
        hourly = cd.hourly(evs)
        if evs:
            h = max(range(24), key=hourly.__getitem__)
            lines.append(f'Busiest hour: {h:02d}:00-{h + 1:02d}:00 ({hourly[h]} cars).')
            lines.append(f'First car {evs[0]["timestamp"][11:16]}, last {evs[-1]["timestamp"][11:16]}.')
        cmp = []
        for label, d in (('Yesterday', today - timedelta(days=1)), (f'Last {today:%A}', today - timedelta(days=7))):
            if d.isoformat() in hist:
                cmp.append(f'{label}: {hist[d.isoformat()]}')
        if cmp:
            lines.append(' · '.join(cmp) + ' (full day)')
        return '\n'.join(lines)


# ====================================================================== main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--test', action='store_true')
    ap.add_argument('--once', action='store_true')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    if args.test:
        raise SystemExit(1 if send(OK, 'Test notification from the car counter.') else 0)
    a = Alerts(dry=args.once)
    if args.once:
        a.state['sent'] = {}
        a.check()
        return
    if not cd.pushover_keys():
        log.warning('Pushover keys not set - add them on the web page (Alerts). Checks run, nothing is sent.')
    trim_log()
    boot_pending = True
    a.save(force=True)
    while True:
        t0 = time.monotonic()
        try:
            if boot_pending:
                boot_pending = not a.startup(cd.load_settings())
            a.retry_outbox()
            a.check()
        except Exception:
            log.exception('Check failed')
        a.save()
        time.sleep(max(1.0, CHECK_EVERY_S - (time.monotonic() - t0)))


if __name__ == '__main__':
    main()
