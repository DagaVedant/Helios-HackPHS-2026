#!/usr/bin/env python3
"""
Servo test on GPIO26 + GPIO13.

  brown -> MB102 GND (common with Pi GND)
  red   -> MB102 5V (NOT Pi 5V)
  orange pan  -> GPIO26 = physical pin 37
  orange tilt -> GPIO13 = physical pin 33
"""

import time
import threading
import lgpio

PAN_GPIO = 26   # pin 37
TILT_GPIO = 13  # pin 33
CHIP = 0


class BitBang:
    def __init__(self, h, gpio, name):
        self.h = h
        self.gpio = gpio
        self.name = name
        self.us = 1500.0
        self.run = True
        lgpio.gpio_claim_output(h, gpio, 0)
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while self.run:
            hi = self.us / 1_000_000.0
            lo = max(0.020 - hi, 0.0)
            lgpio.gpio_write(self.h, self.gpio, 1)
            time.sleep(hi)
            lgpio.gpio_write(self.h, self.gpio, 0)
            time.sleep(lo)

    def stop(self):
        self.run = False
        lgpio.gpio_write(self.h, self.gpio, 0)


def main() -> None:
    print(f"Pan  GPIO{PAN_GPIO} (pin 37)")
    print(f"Tilt GPIO{TILT_GPIO} (pin 33)")
    print("Red=MB102 5V  Brown=GND  Common Pi<->MB102 GND")
    input("Enter to start...")

    h = lgpio.gpiochip_open(CHIP)
    pan = BitBang(h, PAN_GPIO, "pan")
    tilt = BitBang(h, TILT_GPIO, "tilt")
    try:
        for us in (1500, 1000, 1500, 2000, 1500, 1200, 1800, 1500):
            print(f"both -> {us} us")
            pan.us = float(us)
            tilt.us = float(us)
            time.sleep(1.5)
    finally:
        pan.stop()
        tilt.stop()
        time.sleep(0.05)
        lgpio.gpiochip_close(h)
    print("done")


if __name__ == "__main__":
    main()
