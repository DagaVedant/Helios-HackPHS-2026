#!/usr/bin/env python3
"""
Jog both servos with your keyboard arrow keys (works over SSH).

  pan  orange -> GPIO26 = pin 37
  tilt orange -> GPIO13 = pin 33
  red -> MB102 5V   brown -> common GND

Keys:
  ← →   pan
  ↑ ↓   tilt
  space center (pan 0 / tilt 45)
  q     quit
"""

from __future__ import annotations

import select
import sys
import termios
import threading
import time
import tty

import lgpio

PAN_GPIO = 26
TILT_GPIO = 13
CHIP = 0
STEP = 5.0
PAN_MIN, PAN_MAX = -80.0, 80.0
TILT_MIN, TILT_MAX = 15.0, 85.0
MID_US = 1500.0
US_PER_DEG = 1000.0 / 180.0


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def pan_us(deg: float) -> float:
    return MID_US + deg * US_PER_DEG


def tilt_us(deg: float) -> float:
    return MID_US + (deg - 45.0) * US_PER_DEG


class BitBang:
    def __init__(self, h, gpio):
        self.h = h
        self.gpio = gpio
        self.us = MID_US
        self.run = True
        lgpio.gpio_claim_output(h, gpio, 0)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.run:
            hi = max(self.us, 500.0) / 1_000_000.0
            lo = max(0.020 - hi, 0.0)
            lgpio.gpio_write(self.h, self.gpio, 1)
            time.sleep(hi)
            lgpio.gpio_write(self.h, self.gpio, 0)
            time.sleep(lo)

    def stop(self):
        self.run = False
        try:
            lgpio.gpio_write(self.h, self.gpio, 0)
        except Exception:
            pass


def read_key():
    """Return 'up'/'down'/'left'/'right'/char, or None."""
    r, _, _ = select.select([sys.stdin], [], [], 0.05)
    if not r:
        return None
    ch = sys.stdin.read(1)
    if ch != "\x1b":
        return ch
    # arrow: ESC [ A/B/C/D
    r2, _, _ = select.select([sys.stdin], [], [], 0.05)
    if not r2 or sys.stdin.read(1) != "[":
        return None
    r3, _, _ = select.select([sys.stdin], [], [], 0.05)
    if not r3:
        return None
    code = sys.stdin.read(1)
    return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(code)


def main() -> int:
    if not sys.stdin.isatty():
        print("Need an interactive SSH/terminal session.")
        return 1

    print("Arrow keys jog servos. Space=center  q=quit")
    print(f"Pan GPIO{PAN_GPIO} (pin 37)   Tilt GPIO{TILT_GPIO} (pin 33)")
    print("Hold the tray. 5V ON.\n")

    h = lgpio.gpiochip_open(CHIP)
    pan_s = BitBang(h, PAN_GPIO)
    tilt_s = BitBang(h, TILT_GPIO)
    pan, tilt = 0.0, 45.0
    pan_s.us = pan_us(pan)
    tilt_s.us = tilt_us(tilt)

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        while True:
            k = read_key()
            if k is None:
                continue
            if k in ("q", "Q", "\x03"):
                break
            if k in (" ", "c"):
                pan, tilt = 0.0, 45.0
            elif k == "left":
                pan = clamp(pan - STEP, PAN_MIN, PAN_MAX)
            elif k == "right":
                pan = clamp(pan + STEP, PAN_MIN, PAN_MAX)
            elif k == "up":
                tilt = clamp(tilt + STEP, TILT_MIN, TILT_MAX)
            elif k == "down":
                tilt = clamp(tilt - STEP, TILT_MIN, TILT_MAX)
            else:
                continue
            pan_s.us = pan_us(pan)
            tilt_s.us = tilt_us(tilt)
            print(f"\r  pan {pan:+6.1f}°   tilt {tilt:5.1f}°   ", end="", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        pan_s.stop()
        tilt_s.stop()
        time.sleep(0.05)
        lgpio.gpiochip_close(h)
        print("\nPWM off.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
