#!/usr/bin/env python3
"""Hourly housekeeping for the car counter (run by carcount-maintenance.timer).

1. Move SD-card fallback data (past days) onto the USB drive once it is back.
2. Disk retention: delete the photos of the oldest days (CSV logs are always kept)
   when photos are older than PHOTO_KEEP_DAYS or free space drops below MIN_FREE_GB.
3. Rebuild <storage root>/summary_daily.csv: date, total, h00..h23.

  --dry-run   report what would be done without changing anything
"""

import argparse
import csv
import logging
import shutil
from datetime import date, timedelta

import carcount_data as cd

_cfg = cd.load_settings()                       # editable on the web Settings page
PHOTO_KEEP_DAYS = _cfg['PHOTO_KEEP_DAYS'] or None  # 0 -> keep until disk space runs low
MIN_FREE_GB = {cd.USB_ROOT: _cfg['MIN_FREE_GB_USB'], cd.SD_ROOT: 10.0}   # SD shares the OS disk

log = logging.getLogger('maintenance')


def merge_sd_to_usb(dry):
    if not cd.usb_ok() or not cd.SD_ROOT.is_dir():
        return
    today = cd.today()
    for day in sorted(p for p in cd.SD_ROOT.iterdir() if cd.DAY_RE.match(p.name) and p.name < today):
        dst = cd.USB_ROOT / day.name
        log.info('Moving SD fallback %s -> %s', day, dst)
        if dry:
            continue
        moved = {}
        photos = day / 'photos'
        if photos.is_dir():
            (dst / 'photos').mkdir(parents=True, exist_ok=True)
            for f in photos.iterdir():
                target = dst / 'photos' / f.name
                if not target.exists():
                    shutil.copy2(f, target)
                moved[str(f)] = str(target)
        for name, fields in (('events.csv', cd.EVENT_FIELDS), ('health.csv', cd.HEALTH_FIELDS)):
            src = day / name
            if not src.exists():
                continue
            with open(src, newline='') as fh:
                for row in csv.DictReader(fh):
                    if row.get('photo') in moved:
                        row['photo'] = moved[row['photo']]
                    cd.append_csv(dst / name, fields, row)
        shutil.rmtree(day)


def photo_days(root):
    return sorted(p for p in root.iterdir()
                  if cd.DAY_RE.match(p.name) and ((p / 'photos').is_dir() or (p / 'thumbs').is_dir()))


def retention(root, dry):
    if not root.is_dir() or (root == cd.USB_ROOT and not cd.usb_ok()):
        return
    today = cd.today()
    cutoff = (date.today() - timedelta(days=PHOTO_KEEP_DAYS)).isoformat() if PHOTO_KEEP_DAYS else ''
    min_free = MIN_FREE_GB[root]
    for day in photo_days(root):
        if day.name >= today:
            break
        free = cd.disk_free_gb(root) or 0
        too_old = day.name < cutoff
        if not too_old and free >= min_free:
            break
        why = f'older than {PHOTO_KEEP_DAYS} days' if too_old else f'only {free:.1f} GB free'
        log.info('Deleting photos of %s (%s)', day, why)
        if not dry:
            for sub in ('photos', 'thumbs'):
                shutil.rmtree(day / sub, ignore_errors=True)
    free = cd.disk_free_gb(root) or 0
    if free < min_free:
        log.warning('%s still has only %.1f GB free after retention', root, free)


def summaries(dry):
    out = cd.storage_root() / 'summary_daily.csv'
    rows = []
    for d in cd.all_days():
        h = cd.hourly(cd.events(d))
        rows.append({'date': d, 'total': sum(h), **{f'h{i:02d}': n for i, n in enumerate(h)}})
    log.info('Summary: %d days -> %s', len(rows), out)
    if dry:
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix('.tmp')
    with open(tmp, 'w', newline='') as fh:
        w = csv.DictWriter(fh, ['date', 'total'] + [f'h{i:02d}' for i in range(24)])
        w.writeheader()
        w.writerows(rows)
    tmp.replace(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    for step in (lambda: merge_sd_to_usb(args.dry_run),
                 lambda: retention(cd.USB_ROOT, args.dry_run),
                 lambda: retention(cd.SD_ROOT, args.dry_run),
                 lambda: summaries(args.dry_run)):
        try:
            step()
        except Exception:
            log.exception('Maintenance step failed')


if __name__ == '__main__':
    main()
