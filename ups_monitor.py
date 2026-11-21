#!/usr/bin/env python3
"""X1202 UPS monitor: log power events, safe shutdown on low battery, auto-restart.

* Power-loss detect (PLD) on GPIO6: 1 = AC OK, 0 = running on battery.
* Fuel gauge (MAX17040-style) at I2C 0x36: reg 2 = voltage, reg 4 = state of charge.
* Shutdown only happens while on battery: when the battery is low for UPS_LOW_COUNT
  reads in a row, or (optionally) after UPS_MAX_ON_BATTERY_MIN on battery. On AC the
  monitor never shuts down, so a missing battery or a bogus gauge reading can't
  power the Pi off while mains is present.
* Auto restart: before powering off it sets the Pi 5 RTC wake alarm. With
  POWER_OFF_ON_HALT=1 the Pi wakes after UPS_WAKE_AFTER_MIN. If mains is back it
  keeps running; if still on a low battery it shuts down again (after the boot
  grace period) and sets another alarm. If the batteries run completely flat, the
  Pi boots normally once mains returns and 5 V reappears.

Thresholds are the UPS_* settings (carcount_data.SETTINGS), edited on the web Power
page. The config file is re-checked every few seconds, so no restart is needed.

Run as root (needs poweroff and /sys/class/rtc/rtc0/wakealarm).
  --once     print one reading and exit
  --dry-run  log what it would do but never shut down
"""

import argparse
import json
import os
import logging
import struct
import subprocess
import time

import gpiod
import smbus2

import alerts
import carcount_data as cd

PLD_PIN = 6
GPIOCHIP = '/dev/gpiochip0'
I2C_BUS = 1
GAUGE_ADDR = 0x36

POLL_S = 2
STATUS_EVERY_S = 600
ON_BATTERY_STATUS_S = 60  # more frequent status while on battery
STATE_FILE = str(cd.UPS_STATE)   # read by carcounter health log / web page

log = logging.getLogger('ups')


def read_gauge(bus):
    def word(reg):
        w = bus.read_word_data(GAUGE_ADDR, reg)
        return struct.unpack('<H', struct.pack('>H', w))[0]
    return word(2) * 1.25 / 1000 / 16, word(4) / 256


def write_state(**state):
    tmp = STATE_FILE + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump({**state, 'time': time.time()}, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


class Settings:
    """UPS_* settings, re-read whenever the config file changes."""

    def __init__(self):
        self.mtime = None
        self.v = {}
        self.check()

    def check(self):
        try:
            m = cd.CONFIG_PATH.stat().st_mtime
        except OSError:
            m = 0
        if m == self.mtime:
            return
        self.mtime = m
        new = {k: v for k, v in cd.load_settings().items() if k.startswith('UPS_')}
        if self.v:
            for k, v in new.items():
                if self.v.get(k) != v:
                    log.info('Setting %s: %s -> %s', k, self.v.get(k), v)
        else:
            log.info('Settings: %s', ', '.join(f'{k}={v}' for k, v in new.items()))
        self.v = new

    def __getitem__(self, k):
        return self.v[k]


def set_wake_alarm(seconds):
    path = '/sys/class/rtc/rtc0/wakealarm'
    with open(path, 'w') as f:
        f.write('0')               # clear any existing alarm
    with open(path, 'w') as f:
        f.write(f'+{seconds}')


def shutdown(reason, wake_min):
    log.critical('SHUTDOWN: %s.%s', reason,
                 f' Waking again in {wake_min} min via RTC alarm.' if wake_min else ' No wake alarm set.')
    if cd.load_settings()['ALERT_POWER']:
        alerts.send(alerts.POWER, f'Shutting down safely: {reason}. '
                    + (f'Will wake up again in {wake_min} min.' if wake_min else 'It will stay off until power returns.'),
                    priority=1, log_it=False)        # root: don't create a root-owned log file
    if wake_min:
        try:
            set_wake_alarm(wake_min * 60)
        except OSError as e:
            log.error('Could not set RTC wake alarm: %s', e)
    subprocess.run(['systemctl', 'poweroff'], check=False)
    time.sleep(60)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    bus = smbus2.SMBus(I2C_BUS)
    cfg = Settings() if not args.once else None
    start = time.monotonic()
    with gpiod.request_lines(GPIOCHIP, consumer='ups-monitor',
                             config={PLD_PIN: gpiod.LineSettings(direction=gpiod.line.Direction.INPUT)}) as pld:
        ac_prev, low_n, t_status, t_ac_lost, warned = None, 0, 0.0, None, False
        while True:
            ac = pld.get_value(PLD_PIN) == gpiod.line.Value.ACTIVE
            try:
                volts, pct = read_gauge(bus)
            except OSError as e:
                volts = pct = None
                log.warning('Fuel gauge read failed: %s', e)
            desc = f'{"AC" if ac else "BATTERY"}  {volts:.2f} V  {pct:.0f}%' if volts is not None \
                else f'{"AC" if ac else "BATTERY"}  gauge n/a'

            if args.once:
                print(desc)
                return

            cfg.check()
            now = time.monotonic()
            if ac != ac_prev:
                if ac_prev is None and ac:
                    log.info('Started: %s', desc)
                else:
                    log.warning('Power %s: %s', 'RESTORED' if ac else 'LOST - on battery', desc)
                t_ac_lost = None if ac else now
                ac_prev = ac
                t_status = now
            on_batt_s = now - t_ac_lost if t_ac_lost is not None else 0

            low = (not ac and volts is not None
                   and (volts < cfg['UPS_LOW_VOLTAGE']
                        or (cfg['UPS_LOW_PERCENT'] > 0 and pct < cfg['UPS_LOW_PERCENT'])))
            low_n = low_n + 1 if low else 0
            if low_n == 1:
                log.warning('Battery low: %s', desc)

            reason = None
            if low_n >= cfg['UPS_LOW_COUNT']:
                reason = f'battery low on battery power ({desc}, {on_batt_s / 60:.0f} min since AC loss)'
            elif not ac and cfg['UPS_MAX_ON_BATTERY_MIN'] and on_batt_s >= cfg['UPS_MAX_ON_BATTERY_MIN'] * 60:
                reason = f'on battery for {on_batt_s / 60:.0f} min ({desc})'
            in_grace = now - start < cfg['UPS_BOOT_GRACE_S']
            enabled = cfg['UPS_SHUTDOWN_ENABLED'] and not args.dry_run
            if reason and not in_grace and enabled:
                shutdown(reason, cfg['UPS_WAKE_AFTER_MIN'])
                low_n = 0
            elif reason and not warned:
                log.warning('Would shut down (%s): %s', 'boot grace period' if in_grace else
                            'dry run' if args.dry_run else 'shutdown disabled in settings', reason)
                warned = True
            elif not reason:
                warned = False

            write_state(ac=ac, volts=None if volts is None else round(volts, 3),
                        pct=None if pct is None else round(pct, 1), on_battery_s=round(on_batt_s),
                        low=low, low_n=low_n, pending=reason, shutdown_enabled=enabled,
                        grace_left_s=max(0, round(cfg['UPS_BOOT_GRACE_S'] - (now - start))))

            if now - t_status >= (STATUS_EVERY_S if ac else ON_BATTERY_STATUS_S):
                log.info('Status: %s', desc)
                t_status = now
            time.sleep(POLL_S)


if __name__ == '__main__':
    main()
