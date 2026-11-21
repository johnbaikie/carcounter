"""SQLite copy test: initial build, incremental append, SD->USB move, corrupt DB, schema change.
Run: python3 tests/test_db.py   (uses temporary folders; does not touch real data)"""
import os, sys, csv, shutil, logging, sqlite3, tempfile, time
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
logging.basicConfig(level=logging.WARNING)
import carcount_data as cd, carcount_db as db, maintenance as mt

tmp = Path(tempfile.mkdtemp(prefix='carcount-test-'))
usb, sd, dbp = tmp / 'usb', tmp / 'sd', tmp / 'test.db'
usb.mkdir(); sd.mkdir()
cd.USB_ROOT, cd.SD_ROOT = usb, sd
cd.usb_ok = lambda: True
db.DB_PATH = mt.carcount_db.DB_PATH = dbp

def write(root, day, n, start=0):
    for i in range(start, start + n):
        cd.append_csv(root / day / 'events.csv', cd.EVENT_FIELDS,
                      {'timestamp': f'{day}T08:{i:02d}:00.000', 'count_today': i + 1, 'distance_cm': 300,
                       'strength': 2000, 'duration_s': '0.5', 'photo': ''})
def q(sql):
    con = sqlite3.connect(dbp); r = con.execute(sql).fetchall(); con.close(); return r

write(usb, '2026-01-01', 5); write(sd, '2026-01-02', 3)
assert db.update(path=dbp)['events'] == 8
assert q('SELECT * FROM daily ORDER BY day') == [('2026-01-01', 5), ('2026-01-02', 3)]
assert q('SELECT typeof(distance_cm), typeof(duration_s) FROM events LIMIT 1') == [('integer', 'real')]

time.sleep(0.01); write(usb, '2026-01-01', 2, start=5)              # append -> only that file re-imported
assert db.update(path=dbp)['events'] == 10
assert q("SELECT rows FROM sources WHERE path LIKE '%2026-01-01%'") == [(7,)]

shutil.copytree(sd / '2026-01-02', usb / '2026-01-02')               # SD day moved to USB
shutil.rmtree(sd / '2026-01-02')
assert db.update(path=dbp)['events'] == 10
assert q("SELECT count(*) FROM events WHERE source LIKE '%/sd/%'") == [(0,)]

q("UPDATE meta SET value='old' WHERE key='schema'")                  # CSV columns changed -> rebuild
assert db.update(path=dbp)['events'] == 10

for p in (dbp, Path(f'{dbp}-wal'), Path(f'{dbp}-shm')):              # corrupt file -> maintenance rebuilds
    p.unlink(missing_ok=True)
dbp.write_bytes(b'this is not a database' * 100)
mt.update_db(dry=False)
assert q('SELECT count(*) FROM events') == [(10,)]

shutil.rmtree(tmp)
print('DB OK')
