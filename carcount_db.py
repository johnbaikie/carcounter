#!/usr/bin/env python3
"""SQLite copy of the CSV logs, for fast queries. The CSVs remain the record:
this database is derived from them and can be deleted and rebuilt at any time.

Built incrementally by maintenance.py (hourly): only CSV files whose size or
modification time changed are re-imported; rows from CSVs that no longer exist
(e.g. SD fallback days moved to USB) are removed. If the CSV columns change the
database is rebuilt from scratch.

  python3 carcount_db.py            update now
  python3 carcount_db.py --rebuild  delete and rebuild from all CSVs

Tables: events, health (one row per CSV row, plus `day` and `source` columns),
        sources (imported files). Views: daily (day, cars), hourly (day, hour, cars).
Example:  SELECT hour, avg(cars) FROM hourly GROUP BY hour;
"""

import argparse
import csv
import hashlib
import logging
import sqlite3
import time
from pathlib import Path

import carcount_data as cd

DB_PATH = Path('/home/carcount/.local/share/carcount/carcount.db')

log = logging.getLogger('carcount_db')

# column types; anything not listed is TEXT
INTEGER = {'count_today', 'distance_cm', 'strength', 'min_distance_cm', 'baseline_cm', 'events',
           'sensor_errors', 'fan_rpm', 'ac_power'}
REAL = {'duration_s', 'frames_per_s', 'valid_pct', 'lidar_temp_c', 'cpu_temp_c', 'disk_free_gb',
        'battery_v', 'battery_pct'}
TABLES = {'events': ('events.csv', cd.EVENT_FIELDS), 'health': ('health.csv', cd.HEALTH_FIELDS)}
SCHEMA_ID = hashlib.sha1(repr(sorted((t, f) for t, (_, f) in TABLES.items())).encode()).hexdigest()[:12]


def col_type(name):
    return 'INTEGER' if name in INTEGER else 'REAL' if name in REAL else 'TEXT'


def convert(name, value):
    if value is None or value == '':
        return None
    try:
        if name in INTEGER:
            return int(float(value))
        if name in REAL:
            return float(value)
    except ValueError:
        return None
    return value


def connect(path=None):
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute('PRAGMA journal_mode=WAL')   # readers (e.g. web page) don't block the hourly update
    con.execute('PRAGMA synchronous=NORMAL')
    return con


def create_schema(con):
    for table, (_, fields) in TABLES.items():
        cols = ', '.join(f'{f} {col_type(f)}' for f in fields)
        con.execute(f'CREATE TABLE IF NOT EXISTS {table} (day TEXT NOT NULL, {cols}, source TEXT NOT NULL)')
        con.execute(f'CREATE INDEX IF NOT EXISTS {table}_ts ON {table} (timestamp)')
        con.execute(f'CREATE INDEX IF NOT EXISTS {table}_source ON {table} (source)')
    con.execute('CREATE TABLE IF NOT EXISTS sources (path TEXT PRIMARY KEY, tbl TEXT, day TEXT, '
                'size INTEGER, mtime REAL, rows INTEGER, imported REAL)')
    con.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
    con.execute('CREATE VIEW IF NOT EXISTS daily AS SELECT day, count(*) AS cars FROM events GROUP BY day')
    con.execute("CREATE VIEW IF NOT EXISTS hourly AS SELECT day, CAST(substr(timestamp, 12, 2) AS INTEGER) AS hour, "
                "count(*) AS cars FROM events GROUP BY day, hour")
    con.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (SCHEMA_ID,))


def csv_files():
    """{path: (table, day)} for every events/health CSV under the USB and SD roots."""
    found = {}
    for root in cd.read_roots():
        for daydir in root.iterdir():
            if not (daydir.is_dir() and cd.DAY_RE.match(daydir.name)):
                continue
            for table, (name, _) in TABLES.items():
                f = daydir / name
                if f.is_file():
                    found[str(f)] = (table, daydir.name)
    return found


def update(rebuild=False, path=None):
    path = path or DB_PATH
    t0 = time.monotonic()
    if rebuild and path.exists():
        for p in (path, Path(f'{path}-wal'), Path(f'{path}-shm')):
            p.unlink(missing_ok=True)
    con = connect(path)
    try:
        row = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone() \
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone() else None
    except sqlite3.DatabaseError:
        row = None
    if row and row[0] != SCHEMA_ID:
        log.info('CSV columns changed - rebuilding database')
        con.close()
        return update(rebuild=True, path=path)
    with con:
        create_schema(con)
        known = {p: (s, m) for p, s, m in con.execute('SELECT path, size, mtime FROM sources')}
        files = csv_files()
        removed = [p for p in known if p not in files]
        for p in removed:
            tbl = con.execute('SELECT tbl FROM sources WHERE path=?', (p,)).fetchone()[0]
            con.execute(f'DELETE FROM {tbl} WHERE source=?', (p,))
            con.execute('DELETE FROM sources WHERE path=?', (p,))
        imported = 0
        for p, (table, day) in sorted(files.items()):
            try:
                st = Path(p).stat()
            except OSError:
                continue
            if known.get(p) == (st.st_size, st.st_mtime):
                continue
            fields = TABLES[table][1]
            con.execute(f'DELETE FROM {table} WHERE source=?', (p,))
            try:
                with open(p, newline='') as fh:
                    rows = [[day] + [convert(f, r.get(f)) for f in fields] + [p] for r in csv.DictReader(fh)]
            except (OSError, csv.Error) as e:
                log.warning('Skipping %s: %s', p, e)
                continue
            con.executemany(f'INSERT INTO {table} VALUES ({", ".join("?" * (len(fields) + 2))})', rows)
            con.execute('INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (p, table, day, st.st_size, st.st_mtime, len(rows), time.time()))
            imported += 1
    counts = {t: con.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in TABLES}
    con.close()
    log.info('Database: %d files imported, %d removed, %s rows, %.1f s -> %s',
             imported, len(removed), counts, time.monotonic() - t0, path)
    return counts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--rebuild', action='store_true', help='delete the database and rebuild from all CSVs')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    try:
        update(rebuild=args.rebuild)
    except sqlite3.DatabaseError as e:
        log.error('Database error (%s) - rebuilding from CSVs', e)
        update(rebuild=True)


if __name__ == '__main__':
    main()
