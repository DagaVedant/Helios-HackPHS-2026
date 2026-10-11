#!/usr/bin/env python3
"""
Follow the brightest light using the 4 LDRs on the ADS1115 (v2).

  pan  servo signal -> GPIO26 (pin 37)   (override: --pan-gpio)
  tilt servo signal -> GPIO13 (pin 33)   (override: --tilt-gpio, e.g. 19 for pin 35)
  servo red -> MB102 5V, brown -> common GND

What is new in v2
  - per-LDR balance scaling (press b while the panel faces the light) so a
    weaker LDR does not bias the error toward one side
  - hysteresis + minimum step so it settles instead of hunting, and still
    creeps for small errors
  - pan/tilt trims and limits in config.json
  - "AT LIMIT" warnings so you can see when an axis is pinned at its end stop

Keys while running
  space   pause / resume following
  b       balance LDRs now (panel must be facing the light)
  r       reset balance to 1.0
  f / g   flip pan / tilt direction (saved)
  [ / ]   gain down / up        - / =   deadband down / up
  c       center (pan 0, tilt 45)
  q       quit

Run
  sudo .venv/bin/python follow_light.py
  sudo .venv/bin/python follow_light.py --tilt-gpio 19
"""

from __future__ import annotations

import argparse
import json
import select
import sys
import termios
import threading
import time
import tty
from pathlib import Path

import board
import busio
import lgpio
from adafruit_ads1x15.ads1115 import ADS1115
from adafruit_ads1x15.analog_in import AnalogIn

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"

MID_US = 1500.0
US_PER_DEG = 1000.0 / 180.0
EMA_ALPHA = 0.35
STATUS_PERIOD = 0.25
LIMIT_WARN_S = 2.0
SIDES = ("left", "right", "top", "bottom")

DEFAULTS = {
    "pan_sign": 1,
    "tilt_sign": 1,
    "gain": 8.0,
    "deadband": 0.03,
    "max_step_deg": 3.0,
    "min_step_deg": 0.4,
    "loop_hz": 20,
    "dark_threshold": 2000,
    "pan_min": -80.0,
    "pan_max": 80.0,
    # tilt has no degree limits; only the servo pulse range below bounds it
    "servo_us_min": 600.0,
    "servo_us_max": 2400.0,
    "pan_trim": 0.0,
    "tilt_trim": 0.0,
    "ldr_scale": {"left": 1.0, "right": 1.0, "top": 1.0, "bottom": 1.0},
}


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text())
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v if not isinstance(v, dict) else dict(v))
    for s in SIDES:
        cfg["ldr_scale"].setdefault(s, 1.0)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Servo (bit-banged 50 Hz pulse)
# ---------------------------------------------------------------------------

class Servo:
    def __init__(self, handle: int, gpio: int):
        self.h = handle
        self.gpio = gpio
        self.us = MID_US
        self._run = False
        self._t: threading.Thread | None = None
        lgpio.gpio_claim_output(handle, gpio, 0)

    def set_us(self, us: float) -> None:
        self.us = clamp(us, 500.0, 2500.0)

    def start(self) -> None:
        self._run = True
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self) -> None:
        while self._run:
            try:
                hi = self.us / 1_000_000.0
                lgpio.gpio_write(self.h, self.gpio, 1)
                time.sleep(hi)
                lgpio.gpio_write(self.h, self.gpio, 0)
                time.sleep(max(0.020 - hi, 0.0))
            except Exception:
                break

    def stop(self) -> None:
        self._run = False
        if self._t is not None:
            self._t.join(timeout=0.5)
        try:
            lgpio.gpio_write(self.h, self.gpio, 0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# One axis of the controller
# ---------------------------------------------------------------------------

class Axis:
    """Proportional step with hysteresis, min/max step, optional limits and trim.

    lo/hi may be None (no degree limit). The servo pulse range always applies.
    """

    def __init__(self, name: str, center: float, lo: float | None, hi: float | None,
                 trim: float, servo: Servo, us_min: float, us_max: float):
        self.name = name
        self.center = center
        self.lo = lo
        self.hi = hi
        self.trim = trim
        self.servo = servo
        self.us_min = us_min
        self.us_max = us_max
        self.pos = center
        self.moving = False
        self.limit_since: float | None = None
        self.limit_warned = False
        self.write()

    def _limit_deg(self, deg: float) -> float:
        if self.lo is not None:
            deg = max(deg, self.lo)
        if self.hi is not None:
            deg = min(deg, self.hi)
        return deg

    def write(self) -> bool:
        """Send the pulse. Returns True if the pulse range clipped the move."""
        us = MID_US + (self.pos - self.center + self.trim) * US_PER_DEG
        cu = clamp(us, self.us_min, self.us_max)
        if cu != us:
            # pull pos back to what the servo can actually do
            self.pos = self.center - self.trim + (cu - MID_US) / US_PER_DEG
        self.servo.set_us(cu)
        return cu != us

    def goto(self, deg: float) -> None:
        self.pos = self._limit_deg(deg)
        self.write()

    def update(self, err: float, sign: int, gain: float, dead: float,
               cap: float, min_step: float, now: float) -> float:
        a = abs(err)
        if self.moving:
            if a < dead:
                self.moving = False
        elif a >= dead * 1.5:
            self.moving = True
        if not self.moving:
            self.limit_since = None
            self.limit_warned = False
            return 0.0

        step = sign * gain * err
        if abs(step) < min_step:
            step = min_step if step > 0 else -min_step
        step = clamp(step, -cap, cap)

        before = self.pos
        self.pos = self._limit_deg(self.pos + step)
        clipped = self.write()
        pinned = clipped or abs(self.pos - before) < 1e-6
        if pinned:
            if self.limit_since is None:
                self.limit_since = now
            elif not self.limit_warned and now - self.limit_since > LIMIT_WARN_S:
                self.limit_warned = True
                end = "MIN" if step < 0 else "MAX"
                why = "servo pulse range" if clipped else f"{self.name}_{end.lower()}"
                print(f"\n{self.name.upper()} AT {end} ({self.pos:.0f} deg, {why}) but still "
                      f"wants to go further. Check {self.name}_sign, {self.name}_trim, "
                      f"or press b to balance the LDRs.")
        else:
            self.limit_since = None
            self.limit_warned = False
        return step


# ---------------------------------------------------------------------------
# Keyboard / I2C helpers
# ---------------------------------------------------------------------------

class Keys:
    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)

    def getch(self) -> str | None:
        r, _, _ = select.select([sys.stdin], [], [], 0)
        return sys.stdin.read(1) if r else None

    def close(self) -> None:
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)


def scan(i2c: busio.I2C) -> set[int]:
    while not i2c.try_lock():
        pass
    try:
        return set(i2c.scan())
    finally:
        i2c.unlock()


def read_all(chans: list[AnalogIn]) -> list[float]:
    last: Exception | None = None
    for _ in range(4):
        try:
            return [float(ch.value) for ch in chans]
        except OSError as e:
            last = e
            time.sleep(0.02)
    raise OSError(f"ADS1115 read failed: {last}")


def open_oled(i2c: busio.I2C, found: set[int]):
    addr = 0x3C if 0x3C in found else (0x3D if 0x3D in found else None)
    if addr is None:
        return None
    try:
        import adafruit_ssd1306
        from PIL import Image, ImageDraw, ImageFont

        oled = adafruit_ssd1306.SSD1306_I2C(128, 64, i2c, addr=addr)
        font = ImageFont.load_default()

        def draw(lines: list[str]) -> None:
            img = Image.new("1", (128, 64))
            d = ImageDraw.Draw(img)
            for i, line in enumerate(lines):
                d.text((0, i * 13), line, font=font, fill=255)
            try:
                oled.image(img)
                oled.show()
            except OSError:
                pass

        def clear() -> None:
            try:
                oled.fill(0)
                oled.show()
            except OSError:
                pass

        return draw, clear
    except Exception as e:
        print(f"OLED disabled: {e}")
        return None


def arrow(err: float, dead: float, neg: str, pos: str) -> str:
    if abs(err) < dead:
        return "="
    return pos if err > 0 else neg


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Follow the light with the LDRs (v2)")
    ap.add_argument("--pan-gpio", type=int, default=int(cfg.get("pan_gpio", 26)))
    ap.add_argument("--tilt-gpio", type=int, default=int(cfg.get("tilt_gpio", 13)))
    ap.add_argument("--dark", type=float, default=None, help="override dark_threshold")
    args = ap.parse_args()

    if not sys.stdin.isatty():
        print("Run this from an interactive SSH terminal.")
        return 1

    cfg["pan_gpio"] = args.pan_gpio
    cfg["tilt_gpio"] = args.tilt_gpio
    save_config(cfg)

    i2c = busio.I2C(board.SCL, board.SDA)
    time.sleep(0.1)
    found = scan(i2c)
    print("I2C devices:", [hex(a) for a in sorted(found)])
    if 0x48 not in found:
        print("ADS1115 not at 0x48. Fix the I2C bus first (i2cdetect -y 1).")
        return 1

    ads = ADS1115(i2c)
    ads.gain = 1
    chans = [AnalogIn(ads, i) for i in range(4)]
    oled = open_oled(i2c, found)

    m = cfg["ldr_channels"]
    scale = cfg["ldr_scale"]
    dark = args.dark if args.dark is not None else float(cfg["dark_threshold"])
    period = 1.0 / float(cfg["loop_hz"])

    print(f"LDR map {m}   scale {{ {', '.join(f'{s}:{scale[s]:.2f}' for s in SIDES)} }}")
    print(f"pan_sign={cfg['pan_sign']} tilt_sign={cfg['tilt_sign']} gain={cfg['gain']} "
          f"deadband={cfg['deadband']} step {cfg['min_step_deg']}..{cfg['max_step_deg']} deg")
    us_min = float(cfg["servo_us_min"])
    us_max = float(cfg["servo_us_max"])
    print(f"pan {cfg['pan_min']}..{cfg['pan_max']} deg trim {cfg['pan_trim']}   "
          f"tilt: no degree limit (pulse {us_min:.0f}..{us_max:.0f} us) trim {cfg['tilt_trim']}   "
          f"dark={dark:.0f}")
    print(f"Pan GPIO{args.pan_gpio}   Tilt GPIO{args.tilt_gpio}")

    h = lgpio.gpiochip_open(0)
    pan_servo = Servo(h, args.pan_gpio)
    tilt_servo = Servo(h, args.tilt_gpio)
    pan = Axis("pan", 0.0, float(cfg["pan_min"]), float(cfg["pan_max"]),
               float(cfg["pan_trim"]), pan_servo, us_min, us_max)
    tilt = Axis("tilt", 45.0, None, None,
                float(cfg["tilt_trim"]), tilt_servo, us_min, us_max)

    input("\nHold the tray, MB102 5V on, press Enter to arm (pan 0, tilt 45)... ")
    pan_servo.start()
    tilt_servo.start()

    print("Following.  space=pause  b=balance  r=reset  f/g=flip  [ ]=gain  - ==deadband  c=center  q=quit\n")
    keys = Keys()
    ema: list[float] | None = None
    paused = False
    last_status = 0.0

    try:
        while True:
            now = time.monotonic()

            k = keys.getch()
            if k in ("q", "Q"):
                break
            if k == " ":
                paused = not paused
            elif k == "c":
                pan.goto(0.0)
                tilt.goto(45.0)
            elif k == "f":
                cfg["pan_sign"] = -int(cfg["pan_sign"])
                save_config(cfg)
                print(f"\npan_sign -> {cfg['pan_sign']}")
            elif k == "g":
                cfg["tilt_sign"] = -int(cfg["tilt_sign"])
                save_config(cfg)
                print(f"\ntilt_sign -> {cfg['tilt_sign']}")
            elif k == "[":
                cfg["gain"] = max(1.0, float(cfg["gain"]) - 1.0)
                save_config(cfg)
                print(f"\ngain -> {cfg['gain']}")
            elif k == "]":
                cfg["gain"] = min(40.0, float(cfg["gain"]) + 1.0)
                save_config(cfg)
                print(f"\ngain -> {cfg['gain']}")
            elif k == "-":
                cfg["deadband"] = max(0.005, round(float(cfg["deadband"]) - 0.005, 3))
                save_config(cfg)
                print(f"\ndeadband -> {cfg['deadband']}")
            elif k == "=":
                cfg["deadband"] = min(0.2, round(float(cfg["deadband"]) + 0.005, 3))
                save_config(cfg)
                print(f"\ndeadband -> {cfg['deadband']}")
            elif k == "r":
                for s in SIDES:
                    scale[s] = 1.0
                save_config(cfg)
                print("\nbalance reset to 1.0")
            elif k == "b" and ema is not None:
                raw_by_side = {s: ema[m[s]] for s in SIDES}
                mean = sum(raw_by_side.values()) / 4.0
                if min(raw_by_side.values()) > 50:
                    for s in SIDES:
                        scale[s] = mean / raw_by_side[s]
                    save_config(cfg)
                    print("\nbalanced: " + ", ".join(f"{s}={scale[s]:.3f}" for s in SIDES))
                else:
                    print("\ntoo dark to balance")

            try:
                raw = read_all(chans)
            except OSError as e:
                print(f"\nI2C glitch: {e}")
                time.sleep(0.1)
                continue
            ema = raw if ema is None else [EMA_ALPHA * r + (1 - EMA_ALPHA) * e
                                           for r, e in zip(raw, ema)]

            v = {s: ema[m[s]] * scale[s] for s in SIDES}
            L, R, T, B = v["left"], v["right"], v["top"], v["bottom"]
            total_raw = sum(ema)
            ex = (R - L) / (R + L) if R + L > 1 else 0.0
            ez = (T - B) / (T + B) if T + B > 1 else 0.0

            gain = float(cfg["gain"])
            dead = float(cfg["deadband"])
            cap = float(cfg["max_step_deg"])
            mn = float(cfg["min_step_deg"])

            if paused:
                state = "PAUSED"
            elif total_raw < dark:
                state = "DARK"
            else:
                dp = pan.update(ex, int(cfg["pan_sign"]), gain, dead, cap, mn, now)
                dt_ = tilt.update(ez, int(cfg["tilt_sign"]), gain, dead, cap, mn, now)
                state = "LOCKED" if dp == 0.0 and dt_ == 0.0 else "TRACK"

            if now - last_status >= STATUS_PERIOD:
                last_status = now
                want = arrow(ex, dead, "<", ">") + arrow(ez, dead, "v", "^")
                print(f"\r{state:6} {want}  pan {pan.pos:+6.1f}  tilt {tilt.pos:5.1f}  "
                      f"ex {ex:+.3f} ez {ez:+.3f}  "
                      f"L{L:6.0f} R{R:6.0f} T{T:6.0f} B{B:6.0f}   ",
                      end="", flush=True)
                if oled:
                    oled[0]([
                        f"FOLLOW {state} {want}",
                        f"P{pan.pos:+5.1f}  T{tilt.pos:5.1f}",
                        f"ex{ex:+.2f} ez{ez:+.2f}",
                        f"L{L:5.0f} R{R:5.0f}",
                        f"T{T:5.0f} B{B:5.0f}",
                    ])

            dt = time.monotonic() - now
            if dt < period:
                time.sleep(period - dt)
    except KeyboardInterrupt:
        pass
    finally:
        keys.close()
        pan_servo.stop()
        tilt_servo.stop()
        try:
            lgpio.gpiochip_close(h)
        except Exception:
            pass
        if oled:
            oled[1]()
        print("\nServos off.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
