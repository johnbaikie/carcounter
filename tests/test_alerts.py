"""Alert logic with a fake Pushover and fake sensor state (nothing is sent).
Run: python3 tests/test_alerts.py"""
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import alerts
import carcount_data as cd

tmp = Path(tempfile.mkdtemp())
alerts.STATE_PATH = tmp / 'state.json'
cd.ALERT_LOG = tmp / 'log.jsonl'
cd.PUSHOVER_PATH = tmp / 'pushover.json'

sent = []
net_down = [False]


def fake_post(title, message, priority=0, ts=None):
    if net_down[0]:
        return 'Network error: down', True
    sent.append((title, message, priority))
    return None, False


alerts._post = fake_post
NOW = [datetime(2026, 11, 20, 12, 0)]


class FakeDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW[0]


alerts.datetime = FakeDT
cd.datetime = FakeDT
alerts.service_state = lambda unit: 'failed'

# fake world
world = {'live': {'event_s': 0}, 'health': [], 'events': {}, 'usb': True, 'free': 100.0, 'temp': 55.0, 'ups': None}
defaults = {k: s['default'] for k, s in cd.SETTING.items()}
settings = dict(defaults)
settings['ALERT_QUIET_H'] = 0      # switched on in the quiet-day section
alerts.cd.load_settings = lambda: dict(settings)
alerts.cd.live_state = lambda max_age_s=3: world['live']
alerts.cd.health = lambda day: world['health']
alerts.cd.events = lambda day: world['events'].get(day, [])
alerts.cd.all_days = lambda: sorted(world['events'])
alerts.cd.usb_ok = lambda: world['usb']
alerts.cd.disk_free_gb = lambda p: world['free']
alerts.cd.cpu_temp = lambda: world['temp']
alerts.cd.fan_rpm = lambda: 0
alerts.cd.ups_state = lambda max_age_s=30: world['ups']


def health(**kw):
    row = {'timestamp': NOW[0].isoformat(timespec='seconds'), 'frames_per_s': '100', 'camera': 'ok',
           'throttled': '0x0', 'ac_power': '', 'battery_v': '', 'battery_pct': ''}
    row.update(kw)
    world['health'] = [row]


def ev(day, hh, mm=0):
    return {'timestamp': f'{day}T{hh:02d}:{mm:02d}:00.000'}


def take():
    out = list(sent)
    sent.clear()
    return out


a = alerts.Alerts()
health()
a.check()
_m = take(); assert _m == [], _m

# counter stops: needs 90 s before alerting, then one message, then "running again"
world['live'] = None
a.check()
assert take() == []
a.state['active']['counter'] -= 100
a.check()
m = take()
assert len(m) == 1 and 'not running' in m[0][1] and 'failed' in m[0][1], m
a.check()
assert take() == [], 'sent once only'
world['live'] = {'event_s': 0}
a.check()
assert [t for t, *_ in take()] == [alerts.OK]

# lidar dead, camera dead, under-voltage (health row based, immediate)
health(frames_per_s='0', camera='none', throttled='0x50005')
a.check()
msgs = ' | '.join(x[1] for x in take())
assert 'lidar' in msgs and 'camera' in msgs and 'Under-voltage' in msgs, msgs
health()
a.check()
assert len(take()) == 3, 'three recoveries'

# beam blocked a long time
world['live'] = {'event_s': 16 * 60}
a.check()
assert 'blocked for 16 min' in take()[0][1]
world['live'] = {'event_s': 0}
a.check()
assert 'clear again' in take()[0][1]

# USB missing (2 min delay), hot CPU (2 min delay)
world['usb'], world['temp'] = False, 83.0
a.check()
assert take() == []
for k in ('usb', 'hot'):
    a.state['active'][k] -= 200
a.check()
msgs = ' | '.join(x[1] for x in take())
assert 'USB drive' in msgs and '83 °C' in msgs, msgs
world['usb'], world['temp'] = True, 60.0
a.check()
assert len(take()) == 2

# problems switched off -> silent, and no "recovered" for a condition that was active
world['live'] = None
a.check()
a.state['active']['counter'] -= 100
settings['ALERT_PROBLEMS'] = False
a.check()
world['live'] = {'event_s': 0}
a.check()
assert take() == []
settings['ALERT_PROBLEMS'] = True

# power: mains lost via UPS monitor, battery low, restored
world['ups'] = {'ac': False, 'volts': 3.9, 'pct': 80}
a.check()
m = take()
assert len(m) == 1 and 'Mains power lost' in m[0][1] and m[0][2] == 1, m
world['ups'] = {'ac': False, 'volts': 3.4, 'pct': 25}
a.check()
assert 'Battery low' in take()[0][1]
world['ups'] = {'ac': True, 'volts': 4.1, 'pct': 30}
a.check()
assert [x[1] for x in take()] == ['Mains power is back.']
world['ups'] = None
# power from the counter's health log when the UPS monitor isn't running
health(ac_power='0', battery_v='3.8', battery_pct='70')
a.check()
assert 'running on battery (3.80 V 70%)' in take()[0][1]
health()
a.check()
take()

# restart after a power cut
a.state['last_seen'] = (NOW[0] - timedelta(minutes=45)).timestamp()
alerts.Path = lambda p: type('P', (), {'read_text': lambda self: '120.0 0'})() if p == '/proc/uptime' else Path(p)
a.startup(settings)
m = take()
assert len(m) == 1 and 'power cut?' in m[0][1], m
alerts.Path = Path

# history: 10 days, ~5 cars/day at 17:00
base = NOW[0].date()
for i in range(1, 11):
    d = (base - timedelta(days=i)).isoformat()
    world['events'][d] = [ev(d, 17, j) for j in range(5)]
today = base.isoformat()
a.hist = (None, {}, {})      # history is cached once a day; it was built before these days existed

# quiet: no cars since 07:00 -> at 12:00 that's 5 h >= 3 h
settings['ALERT_QUIET_H'] = 3.0
a.state['busy_hour'] = NOW[0].strftime('%Y-%m-%d ') + '11'
a.check()
m = take()
assert len(m) == 1 and 'No cars counted for 5.0 h today' in m[0][1], m
world['events'][today] = [ev(today, 12, 1)]
NOW[0] = NOW[0].replace(minute=1, second=30)
a.check()
assert 'counted again' in take()[0][1]

# busy hour: 12 cars 16:00-17:00 when usually 0 -> alert at 17:00; record day when > 5
NOW[0] = datetime(2026, 11, 20, 17, 0, 30)
world['events'][today] = [ev(today, 16, j) for j in range(12)]
a.check()
msgs = [x[1] for x in take()]
assert any('Busy hour: 12 cars between 16:00 and 17:00' in m for m in msgs), msgs
assert any('New record: 12 cars' in m for m in msgs), msgs
a.check()
assert take() == [], 'record and busy hour sent once'

# daily summary at 21:00, once
NOW[0] = datetime(2026, 11, 20, 20, 59)
a.check()
assert take() == []
NOW[0] = datetime(2026, 11, 20, 21, 0, 5)
a.check()
m = take()
assert len(m) == 1 and m[0][0].endswith('12 cars') and 'Busiest hour: 16:00-17:00' in m[0][1] \
    and 'Yesterday: 5' in m[0][1] and 'Last Friday: 5' in m[0][1], m
a.check()
assert take() == []

# network down -> queued and retried later with the original text
NOW[0] = datetime(2026, 11, 20, 21, 5)
net_down[0] = True
world['usb'] = False
a.check()
a.state['active']['usb'] -= 200
a.check()
assert take() == [] and len(a.state['outbox']) == 1
net_down[0] = False
a.retry_outbox()
assert 'USB' in take()[0][1] and a.state['outbox'] == []
world['usb'] = True

# state survives a restart; log written
a.save(force=True)
b = alerts.Alerts()
assert b.state['summary_day'] == today
assert len(alerts.recent(100)) > 10

# web: key validation
import web
assert 'Not saved' in web.save_keys({'user': ['short'], 'token': ['x' * 30]})
assert 'saved' in web.save_keys({'user': ['u' * 30], 'token': ['t' * 30]})
assert cd.pushover_keys() == ('u' * 30, 't' * 30) and oct(cd.PUSHOVER_PATH.stat().st_mode)[-3:] == '600'
assert 'saved' in web.save_keys({'user': [''], 'token': ['T' * 30]}) and cd.pushover_keys() == ('u' * 30, 'T' * 30)
print('ALERTS OK')
