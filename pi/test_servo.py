#!/usr/bin/env python3
"""
Servo-only smoke test. Hold the tray. 5V jumper ON before running.

Wiring:
  brown -> GND (common with Pi)
  red   -> MB102 5V
  pan orange  -> pin 32 (GPIO12)
  tilt orange -> pin 33 (GPIO13)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from rpi_hardware_pwm import HardwarePWM

HZ = 50
PERIOD_US = 1_000_000 / HZ


def duty(us: float) -> float:
    return (us / PERIOD_US) * 100.0


def chips() -> list[int]:
    root = Path("/sys/class/pwm")
    out = []
    for p in sorted(root.glob("pwmchip*")):
        out.append(int(p.name.replace("pwmchip", "")))
    return out


def sweep(chip: int) -> None:
    print(f"\n=== Trying pwmchip{chip} ===")
    pan = HardwarePWM(pwm_channel=0, hz=HZ, chip=chip)
    tilt = HardwarePWM(pwm_channel=1, hz=HZ, chip=chip)
    # center
    pan.start(duty(1500))
    tilt.start(duty(1500))
    print("center 1500us — both should twitch/hold")
    time.sleep(1.5)
    for us in (1000, 1500, 2000, 1500):
        print(f"  pulse {us}us  duty={duty(us):.2f}%")
        pan.change_duty_cycle(duty(us))
        tilt.change_duty_cycle(duty(us))
        time.sleep(1.0)
    pan.stop()
    tilt.stop()
    print(f"pwmchip{chip} done")


def main() -> int:
    found = chips()
    print("PWM chips:", found)
    if not found:
        print("No pwmchip* — add to /boot/firmware/config.txt then reboot:")
        print("  dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4")
        return 1

    print("Hold the tray. Confirm MB102 5V jumper is ON and servo reds are on 5V.")
    input("Press Enter to start sweep...")

    # Prefer 0 then 1 on Pi 5
    order = [c for c in (0, 1, 2) if c in found] + [c for c in found if c not in (0, 1, 2)]
    for c in order:
        try:
            sweep(c)
        except Exception as e:
            print(f"pwmchip{c} error: {e}")

    print("\nWhich chip made the servos move? Put that number in config.json as pwm_chip.")
    print("If NONE moved: check 5V on servo red, common GND, orange on pins 32/33.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
