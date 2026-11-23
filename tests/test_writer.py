"""A hung camera must not hold up or lose counts: the CSV row is written without the photo
after PHOTO_WAIT_S, later cars skip the photo, and healthy() turns False so the watchdog
restarts the counter. Run: python3 tests/test_writer.py   (temporary folders, fake camera)"""
import csv
import os
import sys
import tempfile
import threading
import time
import logging
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
logging.basicConfig(level=logging.ERROR)
import carcount_data as cd
import carcounter as cc

tmp = Path(tempfile.mkdtemp(prefix='carcount-test-'))
cc.USB_ROOT = cc.SD_ROOT = cd.USB_ROOT = cd.SD_ROOT = tmp   # never the real drive
cd.usb_ok = lambda: True
cc.PHOTO_WAIT_S = 0.5
cc.WRITER_STALL_S = 1.5


class Req:
    def save(self, stream, path):
        Path(path).write_bytes(b'jpg')

    def release(self):
        pass


class FakeCam:
    def __init__(self):
        self.hang = threading.Event()   # set -> capture blocks until released
        self.release = threading.Event()

    def capture_request(self):
        if self.hang.is_set():
            self.release.wait()
        return Req()

    def stop(self):
        pass

    def close(self):
        pass


cam = FakeCam()
cc.Writer._open_camera = staticmethod(lambda: cam)
w = cc.Writer(camera_enabled=True)


def car(n):
    t = time.time()
    return {'t': t, 't_last': t + 0.3, 'count': n, 'dist': 300, 'amp': 2000, 'min_dist': 280, 'baseline': 850}


def rows():
    f = tmp / cd.today() / 'events.csv'
    return list(csv.DictReader(f.open())) if f.exists() else []


def wait_rows(n, timeout=5):
    end = time.time() + timeout
    while len(rows()) < n and time.time() < end:
        time.sleep(0.05)
    return rows()


# normal: photo then row with the photo path
e = car(1); w.photo(e); w.row(e)
r = wait_rows(1)
assert len(r) == 1 and r[0]['photo'].endswith('.jpg'), r
assert w.healthy()

# camera hangs: row still written (without photo) within ~PHOTO_WAIT_S
cam.hang.set()
e = car(2); w.photo(e); w.row(e)
t0 = time.time()
r = wait_rows(2)
assert len(r) == 2 and r[1]['photo'] == '' and time.time() - t0 < 2, (r, time.time() - t0)

# next car while still hung: skips the photo straight away
time.sleep(0.6)
e = car(3); w.photo(e); w.row(e)
t0 = time.time()
r = wait_rows(3)
assert len(r) == 3 and r[2]['photo'] == '' and time.time() - t0 < 0.5, time.time() - t0

# still hung past WRITER_STALL_S -> unhealthy (watchdog restarts us), all rows already safe
time.sleep(1.0)
assert not w.healthy() and len(rows()) == 3
cam.release.set()
w.close()

# camera that never delivers a frame at startup -> run without it (no hang, no restart loop)
cam2 = FakeCam(); cam2.hang.set()
cc.Writer._open_camera = staticmethod(lambda: cam2)
cc.CAMERA_TEST_S = 0.3
w2 = cc.Writer(camera_enabled=True)
assert w2.cam is None and w2.cam_thread is None and w2.healthy()
e = car(4); w2.photo(e); w2.row(e)
assert len(wait_rows(4)) == 4
cam2.release.set()
w2.close()
print('WRITER OK')
