"""Storage fallback test: USB present/missing/failing -> SD, readers merge both, maintenance moves SD days to USB.
Run: python3 tests/test_fallback.py   (uses temporary folders; does not touch real data)"""
import sys, os, time, csv, shutil, logging, tempfile
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')
import carcount_data as cd, carcounter as cc, maintenance as mt
tmp = Path(tempfile.mkdtemp(prefix='carcount-test-'))
usb, sd = tmp/'usb', tmp/'sd'; usb.mkdir(); sd.mkdir()
state = {'usb_ok': True}
for m in (cd, cc, mt):
    for name, val in (('USB_ROOT', usb), ('SD_ROOT', sd)):
        if hasattr(m, name): setattr(m, name, val)
cd.usb_ok = lambda: state['usb_ok']
mt.MIN_FREE_GB = {usb: 0, sd: 0}
w = cc.Writer(camera_enabled=False)
def event(ts, n):
    return {'t': ts, 't_last': ts + 0.5, 'count': n, 'dist': 300, 'amp': 2000, 'min_dist': 280, 'baseline': 850, 'photo': ''}
def fake_photo(ev, root):
    p = cc.day_dir(root, ev['t']) / 'photos' / f"{int(ev['t'])}.jpg"; p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b'jpg'); ev['photo'] = str(p)
def drain():
    while not w.q.empty() or w.busy['writer']: time.sleep(0.05)
    time.sleep(0.1)
now, yesterday = time.time(), time.time() - 86400
today, yday = cd.today(), time.strftime('%Y-%m-%d', time.localtime(yesterday))

w.row(event(now, 1)); drain()                                   # 1) USB present
state['usb_ok'] = False                                         # 2) USB unplugged
for i in range(3):
    e = event(yesterday + i, i + 1); fake_photo(e, sd); w.row(e)
w.row(event(now + 1, 2)); drain()
state['usb_ok'] = True                                          # 3) USB back but write fails
os.chmod(usb / today / 'events.csv', 0o400); os.chmod(usb / today, 0o500)
w.row(event(now + 2, 3)); drain()
os.chmod(usb / today, 0o700); os.chmod(usb / today / 'events.csv', 0o600)
w.close()

def rows(root, day):
    f = root / day / 'events.csv'
    return list(csv.DictReader(open(f))) if f.exists() else []
print('USB today:', len(rows(usb, today)), '| SD today:', len(rows(sd, today)), '| SD yesterday:', len(rows(sd, yday)))
print('readers merge both roots, today total =', len(cd.events(today)), '| resumed count =', cc.count_existing_today())
mt.merge_sd_to_usb(dry=False)                                   # 4) maintenance moves past SD days to USB
r = rows(usb, yday)
print('after merge: USB yesterday', len(r), '| SD yesterday left:', (sd / yday).exists(), '| SD today kept:', (sd / today).exists())
print('photo paths rewritten to USB and exist:', all(x['photo'].startswith(str(usb)) and Path(x['photo']).exists() for x in r))
shutil.rmtree(tmp)
