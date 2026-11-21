#!/usr/bin/env python3
"""X1202 UPS monitor: log power events, safe shutdown on low battery, auto-restart.

* Power-loss detect (PLD) on GPIO6: 1 = AC OK, 0 = running on battery.
* Fuel gauge (MAX17040-style) at I2C 0x36: reg 2 = voltage, reg 4 = state of charge.
* Shutdown only happens while on battery AND the battery is low for LOW_COUNT reads
  in a row. On AC the monitor never shuts down (so a missing/bad battery or a
  bogus gauge reading can't power the Pi off while mains is present).
* Auto restart: before powering off it sets the Pi 5 RTC wake alarm. With
  POWER_OFF_ON_HALT=1 the Pi wakes after WAKE_AFTER_S. If mains is back it just
  keeps running; if still on a low battery it shuts down again (after the boot
  grace period) and sets another alarm. If the batteries run completely flat,
  the Pi boots normally once mains returns and 5 V reappears.

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

PLD_PIN = 6
GPIOCHIP = '/dev/gpiochip0'
I2C_BUS = 1
GAUGE_ADDR = 0x36

POLL_S = 2
LOW_VOLTAGE = 3.20        # per-cell volts (same threshold as Geekworm bat.py)
LOW_PERCENT = 10          # state of charge %
LOW_COUNT = 5             # consecutive low readings (10 s) before shutdown
BOOT_GRACE_S = 120        # never shut down in the first 2 min after start (time to SSH in)
WAKE_AFTER_S = 1800       # RTC wake 30 min after a low-battery shutdown
STATUS_EVERY_S = 600
STATE_FILE = '/run/carcount-ups.json'   # read by carcounter health log / web page
ON_BATTERY_STATUS_S = 60  # more frequent status while on battery

log = logging.getLogger('ups')


def read_gauge(bus):
    def word(reg):
        w = bus.read_word_data(GAUGE_ADDR, reg)
        return struct.unpack('<H', struct.pack('>H', w))[0]
    return word(2) * 1.25 / 1000 / 16, word(4) / 256


def write_state(ac, volts, pct):
    tmp = STATE_FILE + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump({'ac': ac, 'volts': None if volts is None else round(volts, 3),
                       'pct': None if pct is None else round(pct, 1), 'time': time.time()}, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def set_wake_alarm(seconds):
    path = '/sys/class/rtc/rtc0/wakealarm'
    with open(path, 'w') as f:
        f.write('0')               # clear any existing alarm
    with open(path, 'w') as f:
        f.write(f'+{seconds}')


def shutdown(dry_run, reason):
    log.critical('SHUTDOWN: %s. Waking again in %d min via RTC alarm.', reason, WAKE_AFTER_S // 60)
    if dry_run:
        log.critical('(dry run - not shutting down)')
        return
    try:
        set_wake_alarm(WAKE_AFTER_S)
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
    start = time.monotonic()
    with gpiod.request_lines(GPIOCHIP, consumer='ups-monitor',
                             config={PLD_PIN: gpiod.LineSettings(direction=gpiod.line.Direction.INPUT)}) as pld:
        ac_prev, low_n, t_status, t_ac_lost = None, 0, 0.0, None
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

            write_state(ac, volts, pct)
            now = time.monotonic()
            if ac != ac_prev:
                if ac_prev is None and ac:
                    log.info('Started: %s', desc)
                else:
                    log.warning('Power %s: %s', 'RESTORED' if ac else 'LOST - on battery', desc)
                t_ac_lost = None if ac else now
                ac_prev = ac
                t_status = now

            low = (not ac and volts is not None
                   and (volts < LOW_VOLTAGE or pct < LOW_PERCENT))
            low_n = low_n + 1 if low else 0
            if low_n == 1:
                log.warning('Battery low: %s', desc)
            if low_n >= LOW_COUNT and now - start >= BOOT_GRACE_S:
                mins = (now - t_ac_lost) / 60 if t_ac_lost else 0
                shutdown(args.dry_run, f'battery low on battery power ({desc}, {mins:.0f} min since AC loss)')
                low_n = 0

            if now - t_status >= (STATUS_EVERY_S if ac else ON_BATTERY_STATUS_S):
                log.info('Status: %s', desc)
                t_status = now
            time.sleep(POLL_S)


if __name__ == '__main__':
    main()
