"""Detector simulation: glitches, merged cars, long blockages, parked car, no-target mode, calibration.
Run: python3 tests/test_detector.py"""
import os
import sys, logging; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import carcounter as cc
logging.basicConfig(level=logging.WARNING)
cc.count_existing_today = lambda: 0
class FW:
    def __init__(s): s.rows=[]
    def photo(s, ev): pass
    def row(s, ev): s.rows.append(dict(ev))
def sim(seq, base):
    w=FW(); d=cc.Detector(w); t=0.0
    d.calibrate([(i*.01, *base) for i in range(300)])
    for n, dist, amp in seq:
        for _ in range(n): t+=0.01; d.feed(t, dist, amp)
    d.flush(); return d, w
B=(850,3000)
d,w=sim([(100,850,3000),(100,400,2000),(200,850,3000),(2,300,500),(200,850,3000),(50,0,20),(100,850,3000),(30,350,900),(20,850,3000),(30,360,900),(100,850,3000)],B); assert len(w.rows)==3
d,w=sim([(100,850,3000),(3000,300,2000),(100,850,3000)],B); assert len(w.rows)==1
cc.STUCK_S=60
d,w=sim([(100,850,3000),(7000,400,2000),(1500,850,3000),(50,300,2000),(100,850,3000)],B); assert len(w.rows)==2 and abs(d.baseline-850)<5
d,w=sim([(100,0,10),(80,250,4000),(100,0,10),(1200,820,600),(50,400,3000),(100,820,600)],(0,10)); assert len(w.rows)==2 and d.baseline==820

print('ALL OK')
# calibration via feed() and recalibrate()
d = cc.Detector(FW()); t = 0
for _ in range(400): t += .01; d.feed(t, 850, 3000)
assert d.baseline == 850
for _ in range(50): t += .01; d.feed(t, 400, 2000)
d.recalibrate(); assert d.cal == []
for _ in range(400): t += .01; d.feed(t, 600, 3000)
assert d.baseline == 600
print('calibration OK')

# schedule: sim timestamps start at the epoch; build windows around that local time
from datetime import datetime, timedelta
t0 = datetime.fromtimestamp(0)
hm = lambda dt: dt.strftime('%H:%M')
cars = [(100,850,3000),(100,400,2000),(200,850,3000),(100,400,2000),(200,850,3000)]
class FWP(FW):
    def __init__(s): super().__init__(); s.photos = 0
    def photo(s, ev): s.photos += 1
def simp(seq):
    w=FWP(); d=cc.Detector(w); t=0.0
    d.calibrate([(i*.01, *B) for i in range(300)])
    for n, dist, amp in seq:
        for _ in range(n): t+=0.01; d.feed(t, dist, amp)
    d.flush(); return d, w
off = (hm(t0 + timedelta(hours=1)), hm(t0 + timedelta(hours=2)))   # window that excludes the sim
on = (hm(t0 - timedelta(hours=1)), hm(t0 + timedelta(hours=1)))   # window that includes it
cc.COUNT_START, cc.COUNT_STOP = off
d, w = simp(cars); assert len(w.rows) == 0 and d.count == 0 and w.photos == 0
# a car blocking when the window opens is not counted half-way through; the next car is
w=FWP(); d=cc.Detector(w); t=0.0
d.calibrate([(i*.01, *B) for i in range(300)])
for i in range(300):
    t+=0.01; d.feed(t, 400, 2000)
    if i == 100: cc.COUNT_START, cc.COUNT_STOP = on
for dist in [850]*100 + [400]*100 + [850]*100: t+=0.01; d.feed(t, dist, 2000 if dist == 400 else 3000)
d.flush(); assert len(w.rows) == 1 and d.count == 1
cc.COUNT_START, cc.COUNT_STOP = on
cc.PHOTO_START, cc.PHOTO_STOP = off
d, w = simp(cars); assert len(w.rows) == 2 and w.photos == 0
cc.PHOTO_START, cc.PHOTO_STOP = on
d, w = simp(cars); assert len(w.rows) == 2 and w.photos == 2
cc.COUNT_START = cc.COUNT_STOP = cc.PHOTO_START = cc.PHOTO_STOP = '00:00'
d, w = simp(cars); assert len(w.rows) == 2 and w.photos == 2
print('schedule OK')
