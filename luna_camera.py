#!/usr/bin/env python3

"""TF-Luna distance trigger: take a photo when an object comes within TRIGGER_CM."""

import serial, time

from datetime import datetime

from pathlib import Path

from picamera2 import Picamera2



PORT        = '/dev/ttyAMA0'

RATE_HZ     = 10    # sensor frames/sec; higher = faster reaction

TRIGGER_CM  = 25    # take a photo below this distance

REARM_CM    = 30    # must move back past this to trigger again

COOLDOWN_S  = 2     # minimum seconds between photos

MIN_AMP     = 100   # ignore weak returns below this signal strength

OUT_DIR     = Path.home() / 'captures'





def set_rate(s, hz):

    cmd = bytes([0x5A, 0x06, 0x03, hz & 0xFF, hz >> 8])

    s.write(cmd + bytes([sum(cmd) & 0xFF]))

    time.sleep(0.1)

    s.reset_input_buffer()





def frames(s):

    while True:

        if s.read(1) != b'\x59' or s.read(1) != b'\x59':

            continue

        f = s.read(7)

        if len(f) != 7:

            continue

        b = b'\x59\x59' + f

        if sum(b[:8]) & 0xFF != b[8]:

            continue

        yield b[2] | b[3] << 8, b[4] | b[5] << 8





def main():

    OUT_DIR.mkdir(exist_ok=True)



    cam = Picamera2()

    cam.configure(cam.create_still_configuration())

    cam.start()

    time.sleep(2)   # let exposure/white balance settle



    s = serial.Serial(PORT, 115200, timeout=2)

    set_rate(s, RATE_HZ)



    armed, last_shot = True, 0.0

    print(f'Ready: trigger < {TRIGGER_CM} cm, re-arm > {REARM_CM} cm. Ctrl+C to stop.')

    try:

        for dist, amp in frames(s):

            if amp < MIN_AMP:

                continue

            print(f'{dist:4d} cm  amp {amp:5d}{"" if armed else "  (waiting to re-arm)"}')



            if armed and dist < TRIGGER_CM and time.time() - last_shot >= COOLDOWN_S:

                path = OUT_DIR / f'{datetime.now():%Y%m%d_%H%M%S}.jpg'

                cam.capture_file(str(path))

                print(f'  -> captured {path}')

                armed, last_shot = False, time.time()

            elif not armed and dist > REARM_CM:

                armed = True

    except KeyboardInterrupt:

        pass

    finally:

        cam.stop()

        s.close()





if __name__ == '__main__':

    main()