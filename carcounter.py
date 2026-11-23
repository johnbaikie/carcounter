#!/usr/bin/env python3
"""Car counter: TF-Luna break-beam across the road + Picamera2 photo + CSV log.

Detection
  * Reads TF-Luna frames at 100 Hz.
  * At startup the beam is calibrated: the median distance to the far-side target
    becomes the baseline. If there is no reliable return (no target yet / out of
    range) it runs in "no-target" mode and counts any return closer than
    NO_TARGET_TRIGGER_CM instead.
  * A frame is "blocked" if the return is shorter than baseline - MARGIN_CM, or the
    signal is lost (amp < MIN_AMP). TRIGGER_FRAMES consecutive blocked frames = one
    event. The event ends after REARM_S of clear frames, so a long blockage is
    still only one event.
  * Baseline tracks slow drift while clear, and re-baselines if the scene changes
    (target moved, parked car) - see REBASELINE_S / STUCK_S.

Output (per day): <root>/YYYY-MM-DD/events.csv, health.csv (every 60 s) and photos/*.jpg
  root = /mnt/carcount (USB) if mounted and writable, else ~/carcount_data (SD).
"""

import argparse
import csv
import json
import logging
import os
import queue
import signal
import socket
import statistics
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

import serial

import carcount_data as cd
from carcount_data import SD_ROOT, USB_ROOT, storage_root

# ---------------------------------------------------------------- fixed settings
PORT = '/dev/ttyAMA0'
BAUD = 115200
RATE_HZ = 100
SENSOR_TIMEOUT_S = 2.0      # no valid frames this long -> reopen the port
MIN_VALID_FRACTION = 0.8    # fraction of valid returns needed to accept a baseline
BASELINE_ALPHA = 0.002      # slow drift tracking while clear (~5 s time constant)
STUCK_SPREAD_CM = 10        # "steady" = last 10 s of readings within this spread
WATCHDOG_PING_S = 5         # systemd watchdog keep-alive (service has WatchdogSec=60)
WRITER_STALL_S = 45         # a photo/CSV job running longer than this = hung -> stop pinging
PHOTO_WAIT_S = 8            # a car's CSV row waits at most this long for its photo
CAMERA_TEST_S = 6           # startup test frame must arrive within this
LIVE_EVERY_S = 0.5          # live reading for the web Settings page

# ---------------------------------------------------------------- tunable settings
# COUNT_START, COUNT_STOP, PHOTO_START, PHOTO_STOP, MARGIN_CM, TRIGGER_FRAMES, REARM_S, MIN_AMP, NO_TARGET_TRIGGER_CM, CALIBRATE_S, REBASELINE_S,
# STUCK_S, CAMERA_ENABLED, PHOTO_SIZE, LENS_POSITION, SHORT_EXPOSURE, HEALTH_EVERY_S, STATUS_EVERY_S
# Defaults/limits: carcount_data.SETTINGS. Overrides: ~/.config/carcount/config.json (web Settings page).
# Reloaded on SIGHUP; SIGUSR1 = recalibrate the baseline.
globals().update(cd.load_settings())

log = logging.getLogger('carcounter')
sensor = {'temp': None}     # latest TF-Luna chip temperature


# ====================================================================== storage
def sd_notify(msg):
    """Minimal systemd notify (READY=1 / WATCHDOG=1) without python3-systemd."""
    addr = os.environ.get('NOTIFY_SOCKET')
    if not addr:
        return
    if addr[0] == '@':
        addr = '\0' + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except OSError as e:
        log.warning('sd_notify failed: %s', e)


def throttled():
    try:
        out = subprocess.run(['vcgencmd', 'get_throttled'], capture_output=True, text=True, timeout=2).stdout
        return out.strip().split('=')[-1]
    except (OSError, subprocess.SubprocessError):
        return ''


def day_dir(root, ts):
    return root / datetime.fromtimestamp(ts).strftime('%Y-%m-%d')


def count_existing_today():
    """Resume today's count after a restart by counting rows already logged."""
    n = 0
    for root in {USB_ROOT, SD_ROOT}:
        f = day_dir(root, time.time()) / 'events.csv'
        try:
            with open(f, newline='') as fh:
                n += sum(1 for _ in csv.DictReader(fh))
        except (OSError, csv.Error):
            pass
    return n


class Writer:
    """Background threads for slow work, so the 100 Hz sensor loop never stalls:
    a camera thread takes photos, a writer thread appends CSV rows. A car's row waits
    for its photo for at most PHOTO_WAIT_S, so a hung camera can't hold up (or, when
    the watchdog restarts us, lose) the counts."""

    def __init__(self, camera_enabled):
        self.q = queue.Queue(maxsize=50)
        self.cq = queue.Queue(maxsize=20)
        self.cam = None
        self.busy = {'writer': None, 'camera': None}
        if camera_enabled:
            try:
                self.cam = self._open_camera()
            except Exception:
                log.exception('Camera failed to start - counting without photos')
            if self.cam and not self._test_capture():
                log.error('Camera started but is not delivering frames - counting without photos '
                          '(check the ribbon cable; restart the counter once fixed)')
                self.cam = None     # a hung camera can't be closed cleanly; just stop using it
        self.thread = threading.Thread(target=self._run, args=(self.q, 'writer'), name='writer', daemon=True)
        self.thread.start()
        self.cam_thread = None
        if self.cam:
            self.cam_thread = threading.Thread(target=self._run, args=(self.cq, 'camera'), name='camera', daemon=True)
            self.cam_thread.start()

    @staticmethod
    def _open_camera():
        from picamera2 import Picamera2
        cam = Picamera2()
        size = tuple(int(x) for x in PHOTO_SIZE.split('x'))
        cam.configure(cam.create_still_configuration(main={'size': size}, buffer_count=2))
        cam.set_controls(Writer._controls())
        cam.start()
        time.sleep(2)  # let exposure / white balance settle
        log.info('Camera ready %s, manual focus LensPosition=%.2f', PHOTO_SIZE, LENS_POSITION)
        return cam

    def _test_capture(self, timeout=None):
        """One frame within `timeout` s? Stops a dead camera from hanging every photo (and the
        watchdog restarting us every couple of minutes)."""
        ok = []

        def grab():
            try:
                self.cam.capture_request().release()
                ok.append(True)
            except Exception as e:
                log.error('Camera test capture failed: %s', e)
        t = threading.Thread(target=grab, name='camera-test', daemon=True)
        t.start()
        t.join(timeout or CAMERA_TEST_S)
        return bool(ok)

    @staticmethod
    def _controls():
        from libcamera import controls
        return {'AfMode': controls.AfModeEnum.Manual, 'LensPosition': float(LENS_POSITION),
                'AeExposureMode': controls.AeExposureModeEnum.Short if SHORT_EXPOSURE
                else controls.AeExposureModeEnum.Normal}

    def update_controls(self):
        self.submit(self._update_controls, q=self.cq)

    def _update_controls(self):
        if self.cam:
            self.cam.set_controls(self._controls())
            log.info('Camera controls updated: LensPosition=%.2f short exposure=%s', LENS_POSITION, SHORT_EXPOSURE)

    def submit(self, job, *args, q=None):
        try:
            (q or self.q).put_nowait((job, args))
            return True
        except queue.Full:
            log.warning('%s queue full, dropping %s', 'Camera' if q is self.cq else 'Writer', job.__name__)
            return False

    def busy_for(self, name):
        b = self.busy[name]
        return 0 if b is None else time.monotonic() - b

    def photo(self, ev):
        if not self.cam:
            return
        ev['photo_done'] = threading.Event()
        if self.busy_for('camera') > PHOTO_WAIT_S:
            log.warning('Camera busy for %.0f s (hung?) - no photo for this car', self.busy_for('camera'))
            ev['photo_done'].set()
        elif not self.submit(self._photo, ev, q=self.cq):
            ev['photo_done'].set()

    def row(self, ev):
        self.submit(self._row, ev)

    def close(self):
        self.q.put((None, ()))
        self.thread.join(timeout=10)
        if self.cam_thread:
            try:
                self.cq.put_nowait((None, ()))
            except queue.Full:
                pass
            self.cam_thread.join(timeout=5)
        if self.cam:
            try:
                self.cam.stop()
                self.cam.close()
            except Exception:
                pass

    def healthy(self):
        """False -> stop pinging the watchdog so systemd restarts us (reopens a hung camera).
        Rows don't wait on a hung camera, so they are all written by then."""
        if not self.thread.is_alive() or self.busy_for('writer') >= WRITER_STALL_S:
            return False
        return self.cam_thread is None or (self.cam_thread.is_alive() and self.busy_for('camera') < WRITER_STALL_S)

    def health(self, row):
        self.submit(self._health, row)

    def _run(self, q, name):
        while True:
            job, args = q.get()
            if job is None:
                return
            self.busy[name] = time.monotonic()
            try:
                job(*args)
            except Exception:
                log.exception('%s job %s failed', name.title(), job.__name__)
            finally:
                self.busy[name] = None

    def _health(self, row):
        root = storage_root()
        row.update(storage='usb' if root == USB_ROOT else 'sd',
                   disk_free_gb=_r(cd.disk_free_gb(root), 1),
                   cpu_temp_c=_r(cd.cpu_temp(), 1), fan_rpm=cd.fan_rpm(), throttled=throttled(),
                   camera='none' if not self.cam else 'hung' if self.busy_for('camera') > PHOTO_WAIT_S else 'ok')
        ups = cd.ups_state()
        if ups:
            row.update(ac_power=int(ups['ac']), battery_v=ups.get('volts'), battery_pct=ups.get('pct'))
        for r in dict.fromkeys([root, SD_ROOT]):
            try:
                cd.append_csv(day_dir(r, time.time()) / 'health.csv', cd.HEALTH_FIELDS, row)
                return
            except OSError as e:
                log.warning('Health write to %s failed: %s', r, e)

    def _photo(self, ev):
        try:
            self._take_photo(ev)
        finally:
            ev['photo_done'].set()

    def _take_photo(self, ev):
        name = datetime.fromtimestamp(ev['t']).strftime('%Y%m%d_%H%M%S_%f')[:-3] + '.jpg'
        req = self.cam.capture_request()      # grab the frame first, then try where to save it
        try:
            for root in dict.fromkeys([storage_root(), SD_ROOT]):
                path = day_dir(root, ev['t']) / 'photos' / name
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    req.save('main', str(path))
                    ev['photo'] = str(path)
                    return
                except OSError as e:
                    log.warning('Photo save to %s failed: %s', path, e)
        finally:
            req.release()

    def _row(self, ev):
        done = ev.get('photo_done')
        if done and not done.wait(PHOTO_WAIT_S):
            log.warning('Photo not ready after %d s (camera hung?) - logging car #%s without it',
                        PHOTO_WAIT_S, ev['count'])
        row = {
            'timestamp': datetime.fromtimestamp(ev['t']).isoformat(timespec='milliseconds'),
            'count_today': ev['count'],
            'distance_cm': ev['dist'],
            'strength': ev['amp'],
            'min_distance_cm': ev['min_dist'] if ev['min_dist'] is not None else '',
            'duration_s': f"{ev['t_last'] - ev['t']:.2f}",
            'baseline_cm': round(ev['baseline']) if ev['baseline'] else '',
            'photo': ev.get('photo', ''),
        }
        for root in dict.fromkeys([storage_root(), SD_ROOT]):
            f = day_dir(root, ev['t']) / 'events.csv'
            try:
                cd.append_csv(f, cd.EVENT_FIELDS, row)
                return
            except OSError as e:
                log.warning('CSV write to %s failed: %s', f, e)
        log.error('Event not logged anywhere: %s', row)


# ====================================================================== sensor
class SensorTimeout(Exception):
    pass


def set_rate(s, hz):
    cmd = bytes([0x5A, 0x06, 0x03, hz & 0xFF, hz >> 8])
    s.write(cmd + bytes([sum(cmd) & 0xFF]))
    time.sleep(0.1)
    s.reset_input_buffer()


def frames(s):
    """Yield (time, dist_cm, amp) for every valid-checksum frame."""
    buf = b''
    last = time.monotonic()
    while True:
        chunk = s.read(s.in_waiting or 1)
        now = time.time()
        if chunk:
            buf += chunk
        elif time.monotonic() - last > SENSOR_TIMEOUT_S:
            raise SensorTimeout
        while len(buf) >= 9:
            i = buf.find(b'\x59\x59')
            if i < 0:
                buf = buf[-1:]
                break
            if len(buf) - i < 9:
                buf = buf[i:]
                break
            f = buf[i:i + 9]
            if sum(f[:8]) & 0xFF != f[8]:
                buf = buf[i + 1:]
                continue
            buf = buf[i + 9:]
            last = time.monotonic()
            sensor['temp'] = (f[6] | f[7] << 8) / 8 - 256
            yield now, f[2] | f[3] << 8, f[4] | f[5] << 8
        if time.monotonic() - last > SENSOR_TIMEOUT_S:
            raise SensorTimeout


def open_sensor():
    s = serial.Serial(PORT, BAUD, timeout=0.5)
    set_rate(s, RATE_HZ)
    return s


# ====================================================================== detector
class Detector:
    def __init__(self, writer):
        self.writer = writer
        self.baseline = None
        self.armed = True
        self.blocked_run = 0
        self.clear_since = None
        self.event = None
        self.pending = []            # frames of the current blocked run, before it becomes an event
        self.rebase = deque()        # candidate frames for re-baselining while clear
        self.recent = deque()        # last 10 s while blocked (stuck detection)
        self.stuck_warned = False
        self.count_day = datetime.now().date()
        self.count = count_existing_today()
        # cumulative stats; status() and health_row() report differences
        self.n_frames = self.n_valid = self.n_events = 0
        self.snaps = {}
        self.last = (0, 0)           # latest (dist, amp) for the live view
        self.cal = []                # samples while calibrating, None when calibrated
        self.sched = None            # (counting, photos) last logged, for change messages

    # ---------------------------------------------------------- calibration
    def recalibrate(self):
        self.flush()
        self.blocked_run = 0
        self.pending.clear()
        self.rebase.clear()
        self.cal = []

    def calibrate(self, samples):
        self.cal = None
        valid = [d for _, d, a in samples if a >= MIN_AMP]
        frac = len(valid) / max(len(samples), 1)
        if frac >= MIN_VALID_FRACTION:
            self.baseline = float(statistics.median(valid))
            log.info('Baseline %.0f cm (%.0f%% valid returns, %d frames). Blocked below %.0f cm or on signal loss.',
                     self.baseline, frac * 100, len(samples), self.baseline - MARGIN_CM)
        else:
            self.baseline = None
            log.warning('No reliable target (%.0f%% valid returns) - NO-TARGET mode: counting returns closer than %d cm',
                        frac * 100, NO_TARGET_TRIGGER_CM)

    def is_blocked(self, dist, amp):
        if self.baseline is None:
            return amp >= MIN_AMP and dist < NO_TARGET_TRIGGER_CM
        return amp < MIN_AMP or dist < self.baseline - MARGIN_CM

    # ---------------------------------------------------------- per frame
    def feed(self, t, dist, amp):
        self.n_frames += 1
        valid = amp >= MIN_AMP
        self.n_valid += valid
        self.last = (dist, amp)
        if self.cal is not None:
            if not self.cal:
                log.info('Calibrating for %.0f s - keep the beam clear...', CALIBRATE_S)
            self.cal.append((t, dist, amp))
            if t - self.cal[0][0] >= CALIBRATE_S:
                self.calibrate(self.cal)
            return
        blocked = self.is_blocked(dist, amp)

        if blocked:
            self.clear_since = None
            self.rebase.clear()
            if self.armed:
                self.blocked_run += 1
                self.pending.append((t, dist, amp))
                if self.blocked_run >= TRIGGER_FRAMES:
                    self._start_event()
            else:
                self._extend_event(t, dist, amp)
                self._check_stuck(t, dist, amp)
        else:
            self.blocked_run = 0
            self.pending.clear()
            if not self.armed:
                if self.clear_since is None:
                    self.clear_since = t
                elif t - self.clear_since >= REARM_S:
                    self._end_event()
            else:
                self._track_baseline(t, dist, valid)

    # ---------------------------------------------------------- events
    def _start_event(self):
        t0, d0, a0 = self.pending[0]
        when = datetime.fromtimestamp(t0)
        if not cd.in_window(COUNT_START, COUNT_STOP, when):
            # outside counting hours: track the blockage (so it isn't counted when the
            # window opens mid-car, and stuck detection still works) but log nothing
            self.event = {'t': t0, 't_last': self.pending[-1][0], 'min_dist': None, 'skip': True}
            self._armed_off()
            return
        today = when.date()
        if today != self.count_day:
            self.count_day, self.count = today, 0
        self.count += 1
        self.n_events += 1
        dists = [d for _, d, a in self.pending if a >= MIN_AMP]
        # trigger distance/strength: first frame with a valid return (car body), else the first frame
        td, ta = next(((d, a) for _, d, a in self.pending if a >= MIN_AMP), (d0, a0))
        self.event = {'t': t0, 't_last': self.pending[-1][0], 'count': self.count,
                      'dist': td, 'amp': ta, 'min_dist': min(dists) if dists else None,
                      'baseline': self.baseline}
        if cd.in_window(PHOTO_START, PHOTO_STOP, when):
            self.writer.photo(self.event)
        log.info('Car #%d today  dist=%s cm amp=%s', self.count, td, ta)
        self._armed_off()

    def _armed_off(self):
        self.armed = False
        self.pending.clear()
        self.blocked_run = 0
        self.recent.clear()
        self.stuck_warned = False

    def _extend_event(self, t, dist, amp):
        ev = self.event
        ev['t_last'] = t
        if amp >= MIN_AMP and (ev['min_dist'] is None or dist < ev['min_dist']):
            ev['min_dist'] = dist

    def _end_event(self):
        if self.event and not self.event.get('skip'):
            self.writer.row(self.event)
            dur = self.event['t_last'] - self.event['t']
            if dur > 5:
                log.info('Long blockage ended after %.1f s (counted once)', dur)
        self.event = None
        self.armed = True
        self.clear_since = None
        self.recent.clear()

    def flush(self):
        """Log an in-progress event (on shutdown)."""
        if self.event:
            self._end_event()

    # ---------------------------------------------------------- baseline upkeep
    def _track_baseline(self, t, dist, valid):
        if not valid:
            self.rebase.clear()
            return
        if self.baseline is not None and abs(dist - self.baseline) < MARGIN_CM / 2:
            self.baseline += BASELINE_ALPHA * (dist - self.baseline)
            self.rebase.clear()
            return
        # clear but not near the baseline: farther than it, or a new target in no-target mode
        self.rebase.append((t, dist))
        if t - self.rebase[0][0] >= REBASELINE_S:
            ds = [d for _, d in self.rebase]
            if max(ds) - min(ds) <= STUCK_SPREAD_CM * 2:
                old = self.baseline
                self.baseline = float(statistics.median(ds))
                log.warning('Re-baselined %s -> %.0f cm (scene changed while clear)',
                            f'{old:.0f} cm' if old else 'no-target', self.baseline)
            self.rebase.clear()

    def _check_stuck(self, t, dist, amp):
        self.recent.append((t, dist, amp))
        while self.recent and t - self.recent[0][0] > 10:
            self.recent.popleft()
        dur = t - self.event['t']
        if dur < STUCK_S:
            return
        ds = [d for _, d, a in self.recent if a >= MIN_AMP]
        if len(ds) > 0.8 * len(self.recent) and max(ds) - min(ds) <= STUCK_SPREAD_CM:
            new = float(statistics.median(ds))
            log.warning('Beam blocked %.0f s by a steady object at %.0f cm - adopting it as baseline', dur, new)
            self._end_event()
            self.baseline = new
        elif not self.stuck_warned:
            log.warning('Beam blocked for %.0f s (signal lost / unsteady) - check sensor and target', dur)
            self.stuck_warned = True

    # ---------------------------------------------------------- schedule
    def schedule(self):
        """(counting, photos) right now per the Schedule settings."""
        counting = cd.in_window(COUNT_START, COUNT_STOP)
        return counting, counting and CAMERA_ENABLED and cd.in_window(PHOTO_START, PHOTO_STOP)

    def log_schedule(self):
        now = self.schedule()
        if now == self.sched:
            return
        if self.sched is None or now[0] != self.sched[0]:
            log.info('Counting %s (counting hours %s)', 'ON' if now[0] else 'PAUSED', hours(COUNT_START, COUNT_STOP))
        if now[0] and CAMERA_ENABLED and (self.sched is None or now[1] != self.sched[1]):
            log.info('Photos %s (photo hours %s)', 'ON' if now[1] else 'OFF', hours(PHOTO_START, PHOTO_STOP))
        self.sched = now

    def delta(self, key):
        """(seconds, frames, valid, events) since the last call with this key."""
        now = time.monotonic()
        cur = (now, self.n_frames, self.n_valid, self.n_events)
        prev = self.snaps.get(key, cur)
        self.snaps[key] = cur
        return tuple(c - p for c, p in zip(cur, prev))

    def status(self):
        secs, fr, va, ev = self.delta('status')
        b = f'{self.baseline:.0f} cm' if self.baseline is not None else 'none (no-target mode)'
        log.info('Status: %.0f frames/s, %.0f%% valid, baseline %s, %d events in last %.0f min, %d today',
                 fr / max(secs, 1), 100 * va / max(fr, 1), b, ev, secs / 60, self.count)

    def live(self):
        d, a = self.last
        thr = (self.baseline - MARGIN_CM) if self.baseline is not None else NO_TARGET_TRIGGER_CM
        counting, photos = self.schedule()
        return {'time': time.time(), 'dist': d, 'amp': a, 'counting': counting, 'photos': photos,
                'count_hours': hours(COUNT_START, COUNT_STOP), 'photo_hours': hours(PHOTO_START, PHOTO_STOP),
                'baseline': None if self.baseline is None else round(self.baseline, 1),
                'threshold': round(thr), 'blocked': self.is_blocked(d, a) if self.n_frames else None,
                'state': 'calibrating' if self.cal is not None else ('armed' if counting else 'paused') if self.armed
                else 'in event' if not self.event or not self.event.get('skip') else 'blocked (not counted)',
                'count': self.count, 'min_amp': MIN_AMP,
                'event_s': round(time.time() - self.event['t']) if self.event else 0}

    def health_row(self, sensor_errors):
        secs, fr, va, ev = self.delta('health')
        return {'timestamp': datetime.now().isoformat(timespec='seconds'),
                'frames_per_s': _r(fr / max(secs, 1), 1), 'valid_pct': _r(100 * va / max(fr, 1), 1),
                'baseline_cm': _r(self.baseline, 0), 'events': ev, 'sensor_errors': sensor_errors,
                'lidar_temp_c': _r(sensor['temp'], 1) if fr else ''}


def hours(start, stop):
    return 'all day' if start == stop else f'{start}-{stop}'


def _r(x, nd):
    return '' if x is None else round(x, nd) if nd else round(x)


# ====================================================================== main
flags = {'reload': False, 'recal': False}


def reload_settings(writer):
    """Re-read config.json. Returns True if the counter must restart to apply it."""
    old = {k: globals()[k] for k in cd.SETTING}
    new = cd.load_settings()
    changed = {k: (old[k], v) for k, v in new.items() if old[k] != v}
    globals().update(new)
    if not changed:
        log.info('Settings reloaded - no changes')
        return False
    for k, (a, b) in changed.items():
        log.info('Setting %s: %s -> %s', k, a, b)
    if any(cd.SETTING[k]['apply'] == 'restart' for k in changed):
        log.info('Restarting to apply camera settings')
        return True
    if {'LENS_POSITION', 'SHORT_EXPOSURE'} & changed.keys():
        writer.update_controls()
    return False


def write_live(det):
    tmp = cd.LIVE_STATE.with_suffix('.tmp')
    try:
        tmp.write_text(json.dumps(det.live()))
        tmp.replace(cd.LIVE_STATE)
    except OSError:
        pass


def run(args):
    writer = Writer(CAMERA_ENABLED and not args.no_camera)
    det = Detector(writer)
    log.info('Storage: %s; %d events already logged today', storage_root(), det.count)
    sd_notify('READY=1')
    det.delta('status'), det.delta('health')
    now = time.monotonic()
    timers = {'ping': 0.0, 'health': now, 'status': now, 'live': 0.0}
    errors = [0]   # sensor errors since the last health row

    def tick():
        now = time.monotonic()
        if flags['reload']:
            flags['reload'] = False
            if reload_settings(writer):
                raise SystemExit(0)          # systemd (Restart=always) starts us again
        if flags['recal']:
            flags['recal'] = False
            log.info('Recalibration requested')
            det.recalibrate()
        if now - timers['live'] >= LIVE_EVERY_S:
            timers['live'] = now
            det.log_schedule()
            write_live(det)
        if now - timers['ping'] >= WATCHDOG_PING_S:
            timers['ping'] = now
            if writer.healthy():
                sd_notify('WATCHDOG=1')
            else:
                log.error('Writer thread stalled (camera hung?) - withholding watchdog ping')
        if now - timers['health'] >= HEALTH_EVERY_S:
            timers['health'] = now
            writer.health(det.health_row(errors[0]))
            errors[0] = 0
        if now - timers['status'] >= STATUS_EVERY_S:
            timers['status'] = now
            det.status()

    try:
        while True:
            try:
                s = open_sensor()
            except serial.SerialException as e:
                log.error('Cannot open %s: %s - retrying in 5 s', PORT, e)
                errors[0] += 1
                tick()
                time.sleep(5)
                continue
            try:
                for t, dist, amp in frames(s):
                    det.feed(t, dist, amp)
                    if args.verbose:
                        print(f'{dist:5d} cm  amp {amp:5d}  {"BLOCKED" if det.is_blocked(dist, amp) else ""}')
                    tick()
            except SensorTimeout:
                log.error('No data from TF-Luna for %.0f s - reopening port', SENSOR_TIMEOUT_S)
                errors[0] += 1
            except serial.SerialException as e:
                log.error('Serial error: %s - reopening port', e)
                errors[0] += 1
            finally:
                s.close()
            tick()
            time.sleep(1)
    finally:
        det.flush()
        writer.close()
        try:
            cd.LIVE_STATE.unlink()
        except OSError:
            pass
        log.info('Stopped. %d counted today.', det.count)


def _flag(name):
    def handler(*_):
        flags[name] = True
    return handler


def _sigterm(*_):
    raise SystemExit(0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('-v', '--verbose', action='store_true', help='print every frame')
    ap.add_argument('--no-camera', action='store_true', help='count and log without photos')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGHUP, _flag('reload'))
    signal.signal(signal.SIGUSR1, _flag('recal'))
    try:
        run(args)
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == '__main__':
    main()
