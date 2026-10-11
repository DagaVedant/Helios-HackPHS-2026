#!/usr/bin/env python3
"""
Micro-Tracker (merged): INA219 power, 4 LDRs on an ADS1115, SSD1306 OLED,
hardware-PWM servos, closed-loop light following, night return-to-home,
CSV logging and the A/B gain test.

This program never touches the network. It appends one JSON line per second to
outbox.jsonl; run sender.py in a second SSH session to push those lines to Vercel.

Servos: pan GPIO12 (PWM0 ch0), tilt GPIO13 (PWM0 ch1). Hardware PWM, no threads.
Servo red = MB102 5V only. Common ground to a Pi GND pin.

Keys (SSH terminal):
  a        arm servos (first pulse = electrical center = pan 0, tilt 45)
  j / l    pan left / right 5 deg         (FIXED only)
  i / k    tilt up / down 5 deg           (FIXED only)
  t        toggle TRACK / FIXED
  x        run the A/B gain test (10 s fixed at home, then 10 s tracking)
  c        cover test + dark threshold calibration
  b / r    balance LDR gains (panel facing the light) / reset balance
  f / g    flip pan / tilt direction (saved)
  [ / ]    gain down / up       - / =   deadband down / up
  q        quit (PWM off)

Boot (once, then reboot):
  sudo raspi-config -> Interface Options -> I2C -> Enable
  /boot/firmware/config.txt:
    dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4
  Check: i2cdetect -y 1   (0x40 INA219, 0x48 ADS1115, 0x3C OLED)

Install and run:
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  python tracker.py               # --no-servo for sensors/OLED only
                                  # --calibrate for the cover test then exit

"""

from __future__ import annotations

import argparse
import csv
import json
import select
import sys
import termios
import time
import tty
from datetime import datetime
from pathlib import Path

import board
import busio
from adafruit_ads1x15.ads1115 import ADS1115
from adafruit_ads1x15.analog_in import AnalogIn
import adafruit_ssd1306
from PIL import Image, ImageDraw, ImageFont

try:
    from adafruit_ina219 import INA219
except ImportError:
    INA219 = None  # type: ignore

try:
    from rpi_hardware_pwm import HardwarePWM
except ImportError:
    HardwarePWM = None  # type: ignore


HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
LOG_DIR = HERE / "logs"
OUTBOX_PATH = HERE / "outbox.jsonl"

SIDES = ("left", "right", "top", "bottom")
JOG_STEP = 5.0
US_PER_DEG = 1000.0 / 180.0          # 1000-2000 us over 180 deg
MID_US = 1500.0
SERVO_HZ = 50.0
PERIOD_US = 1_000_000.0 / SERVO_HZ
EMA_ALPHA = 0.35
HOME_PAN, HOME_TILT = 0.0, 45.0
HOME_STEP = 1.0                      # deg per cycle when parking
LIMIT_WARN_S = 2.0

DEFAULTS = {
    "pan_sign": 1,
    "tilt_sign": 1,
    "one_axis": False,
    "gain": 8.0,
    "deadband": 0.03,
    "max_step_deg": 3.0,
    "min_step_deg": 0.4,
    "loop_hz": 20,
    "dark_threshold": 2000,
    "oled_address": 60,
    "shunt_ohms": 0.1,
    "pwm_chip": 2,                   # Pi 5 header PWM is pwmchip2
    "pan_channel": 0,                # GPIO12
    "tilt_channel": 1,               # GPIO13
    "pan_min": -80.0,
    "pan_max": 80.0,
    "tilt_min": 15.0,
    "tilt_max": 85.0,
    "home_after_s": 300.0,           # DARK this long -> park at home
    "log_hz": 1.0,
    "outbox_s": 1.0,
    "ab_settle_s": 3.0,
    "ab_measure_s": 7.0,
    "ldr_channels": {"left": 0, "right": 1, "top": 2, "bottom": 3},
    "ldr_scale": {"left": 1.0, "right": 1.0, "top": 1.0, "bottom": 1.0},
}


# ---------------------------------------------------------------------------
# Config / helpers
# ---------------------------------------------------------------------------

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    for k, v in DEFAULTS.items():
        if isinstance(v, dict):
            d = cfg.setdefault(k, {})
            for kk, vv in v.items():
                d.setdefault(kk, vv)
        else:
            cfg.setdefault(k, v)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


class Keyboard:
    def __init__(self):
        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd) if sys.stdin.isatty() else None
        self._raw = False
        self.enable_raw()

    def enable_raw(self) -> None:
        if self._old is not None and not self._raw:
            tty.setcbreak(self._fd)
            self._raw = True

    def disable_raw(self) -> None:
        if self._old is not None and self._raw:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
            self._raw = False

    def getch(self) -> str | None:
        if not self._raw:
            return None
        r, _, _ = select.select([sys.stdin], [], [], 0)
        return sys.stdin.read(1) if r else None

    def close(self) -> None:
        self.disable_raw()


# ---------------------------------------------------------------------------
# Servos (hardware PWM)
# ---------------------------------------------------------------------------

class HwServo:
    """One servo on a hardware PWM channel. Pulse width in microseconds."""

    def __init__(self, chip: int, channel: int):
        if HardwarePWM is None:
            raise RuntimeError("rpi-hardware-pwm missing: pip install rpi-hardware-pwm")
        self.pwm = HardwarePWM(pwm_channel=channel, hz=SERVO_HZ, chip=chip)
        self.running = False
        self.us = MID_US

    def _duty(self) -> float:
        return self.us / PERIOD_US * 100.0

    def start(self, us: float) -> None:
        self.us = clamp(us, 500.0, 2500.0)
        if self.running:
            self.pwm.change_duty_cycle(self._duty())
        else:
            self.pwm.start(self._duty())
            self.running = True

    def set_us(self, us: float) -> None:
        self.us = clamp(us, 500.0, 2500.0)
        if self.running:
            self.pwm.change_duty_cycle(self._duty())

    def stop(self) -> None:
        if self.running:
            try:
                self.pwm.stop()
            except Exception:
                pass
            self.running = False


class Axis:
    """One axis: proportional step with hysteresis, min/max step and hard limits."""

    def __init__(self, name: str, home: float, lo: float, hi: float,
                 center_deg: float, servo: HwServo | None):
        self.name = name
        self.home = home
        self.lo, self.hi = lo, hi
        self.center_deg = center_deg      # angle that maps to 1500 us
        self.servo = servo
        self.pos = home
        self.moving = False
        self.limit_since: float | None = None
        self.limit_warned = False

    def us(self) -> float:
        return MID_US + (self.pos - self.center_deg) * US_PER_DEG

    def write(self) -> None:
        if self.servo is not None:
            self.servo.set_us(self.us())

    def goto(self, deg: float) -> None:
        self.pos = clamp(deg, self.lo, self.hi)
        self.write()

    def jog(self, delta: float) -> None:
        self.goto(self.pos + delta)

    def toward(self, target: float, max_step: float) -> bool:
        """Slew toward target. Returns True once there."""
        d = target - self.pos
        if abs(d) <= max_step:
            self.goto(target)
            return True
        self.goto(self.pos + max_step * (1 if d > 0 else -1))
        return False

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
        self.goto(self.pos + step)
        if abs(self.pos - before) < 1e-6:
            if self.limit_since is None:
                self.limit_since = now
            elif not self.limit_warned and now - self.limit_since > LIMIT_WARN_S:
                self.limit_warned = True
                print(f"\n{self.name.upper()} pinned at {self.pos:.0f} deg but still wants "
                      f"to move. Check {self.name}_sign or press b to balance.")
        else:
            self.limit_since = None
            self.limit_warned = False
        return step


# ---------------------------------------------------------------------------
# Sensors + OLED
# ---------------------------------------------------------------------------

class Sensors:
    def __init__(self, cfg: dict):
        self.i2c = busio.I2C(board.SCL, board.SDA)
        time.sleep(0.05)
        while not self.i2c.try_lock():
            pass
        try:
            found = set(self.i2c.scan())
        finally:
            self.i2c.unlock()
        print("I2C devices:", [hex(a) for a in sorted(found)])
        if 0x48 not in found:
            raise RuntimeError("ADS1115 not found at 0x48 (check wiring / ADDR to GND)")

        self.ads = ADS1115(self.i2c)
        self.ads.gain = 1  # +/-4.096 V
        self.chans = [AnalogIn(self.ads, i) for i in range(4)]

        addr = int(cfg["oled_address"])
        if addr not in found and 0x3D in found:
            addr = 0x3D
        if addr not in found:
            raise RuntimeError("OLED not found at 0x3C/0x3D")
        self.oled = adafruit_ssd1306.SSD1306_I2C(128, 64, self.i2c, addr=addr)
        self.oled.fill(0)
        self.oled.show()
        self.font = ImageFont.load_default()

        self.ina = None
        self.ina_scale = 0.1 / float(cfg["shunt_ohms"])
        if INA219 is not None and 0x40 in found:
            self.ina = INA219(self.i2c)
            print("INA219 found, power readout on")
        else:
            print("INA219 not found: OLED shows the LDR sum, no power logged")

    def read_raw(self) -> list[float]:
        last: Exception | None = None
        for _ in range(4):
            try:
                return [float(c.value) for c in self.chans]
            except OSError as e:
                last = e
                time.sleep(0.02)
        raise OSError(f"ADS1115 read failed after retries: {last}")

    def read_power(self) -> tuple[float | None, float | None, float | None]:
        """Returns (volts, amps, watts) or Nones."""
        if self.ina is None:
            return None, None, None
        try:
            v = float(self.ina.bus_voltage)
            a = float(self.ina.current) / 1000.0 * self.ina_scale
            return v, a, v * a
        except Exception:
            return None, None, None

    def draw(self, mode: str, pan: float | None, tilt: float | None, ldr_sum: float,
             volts, amps, watts) -> None:
        img = Image.new("1", (128, 64))
        d = ImageDraw.Draw(img)
        d.text((0, 0), f"MODE {mode}", font=self.font, fill=255)
        if watts is not None:
            d.text((0, 14), f"{watts * 1000:7.2f} mW", font=self.font, fill=255)
            d.text((0, 26), f"{volts:5.2f}V {amps * 1000:6.1f}mA", font=self.font, fill=255)
        else:
            d.text((0, 14), f"LDR sum {ldr_sum:7.0f}", font=self.font, fill=255)
        if pan is None:
            d.text((0, 40), "SERVO OFF", font=self.font, fill=255)
            d.text((0, 52), "press a to arm", font=self.font, fill=255)
        else:
            d.text((0, 40), f"PAN {pan:+6.1f}", font=self.font, fill=255)
            d.text((0, 52), f"TILT {tilt:5.1f}", font=self.font, fill=255)
        try:
            self.oled.image(img)
            self.oled.show()
        except OSError:
            pass  # drop one bad frame


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def cover_test(sensors: Sensors, cfg: dict, kb: Keyboard | None = None) -> None:
    if kb is not None:
        kb.disable_raw()
    print("\nCover test: cover ONE LDR at a time. The channel that drops is assigned.\n")
    try:
        mapping: dict[str, int] = {}
        used: set[int] = set()
        for side in SIDES:
            input(f"Uncover all, then Enter. Next: cover {side.upper()} ... ")
            before = sensors.read_raw()
            input(f"Cover {side.upper()} now, then Enter ... ")
            after = sensors.read_raw()
            drops = sorted(((i, before[i] - after[i]) for i in range(4) if i not in used),
                           key=lambda x: x[1], reverse=True)
            idx, drop = drops[0]
            if drop < 200:
                print(f"  Warning: small drop ({drop:.0f}). Assigning A{idx} anyway.")
            mapping[side] = idx
            used.add(idx)
            print(f"  {side} -> A{idx} (drop {drop:.0f})")
        cfg["ldr_channels"] = mapping

        input("\nUnder the lamp, LDRs uncovered. Enter ... ")
        bright = sum(sensors.read_raw())
        input("Cover all four LDRs. Enter ... ")
        dark = sum(sensors.read_raw())
        cfg["dark_threshold"] = (bright + dark) / 2.0
        save_config(cfg)
        print(f"dark_threshold = {cfg['dark_threshold']:.0f} (bright {bright:.0f}, dark {dark:.0f})")
    finally:
        if kb is not None:
            kb.enable_raw()


# ---------------------------------------------------------------------------
# CSV log + upload
# ---------------------------------------------------------------------------

class CsvLog:
    FIELDS = ["iso_time", "t_s", "state", "pan", "tilt", "volts", "amps", "watts",
              "ldr_left", "ldr_right", "ldr_top", "ldr_bottom"]

    def __init__(self, period: float):
        LOG_DIR.mkdir(exist_ok=True)
        self.path = LOG_DIR / f"run-{datetime.now():%Y%m%d-%H%M%S}.csv"
        self.f = self.path.open("w", newline="")
        self.w = csv.writer(self.f)
        self.w.writerow(self.FIELDS)
        self.period = period
        self.last = 0.0
        self.t0 = time.monotonic()
        print(f"Logging to {self.path}")

    def maybe(self, now: float, row: dict) -> None:
        if now - self.last < self.period:
            return
        self.last = now
        r = row
        self.w.writerow([datetime.now().isoformat(timespec="seconds"),
                         f"{now - self.t0:.1f}", r["state"], r["pan"], r["tilt"],
                         r["volts"], r["amps"], r["watts"], *[r["ldr"][s] for s in SIDES]])
        self.f.flush()

    def close(self) -> None:
        self.f.close()


class Outbox:
    """Appends one JSON line per sample for sender.py to push. No network code here."""

    def __init__(self, period: float):
        self.f = OUTBOX_PATH.open("a", buffering=1)
        self.period = period
        self.last = 0.0

    def sample(self, now: float, row: dict) -> None:
        if now - self.last < self.period:
            return
        self.last = now
        self.f.write(json.dumps({
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "volts": row["volts"], "amps": row["amps"], "watts": row["watts"],
            "ldr": row["ldr"], "pan": row["pan"], "tilt": row["tilt"], "state": row["state"],
        }) + "\n")

    def ab_result(self, fixed_mw: float, tracking_mw: float) -> None:
        self.f.write(json.dumps({
            "type": "ab",
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "fixed_mw": round(fixed_mw, 3), "tracking_mw": round(tracking_mw, 3),
            "gain_pct": round(100 * (tracking_mw - fixed_mw) / fixed_mw, 1),
        }) + "\n")

    def close(self) -> None:
        self.f.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Micro-Tracker")
    ap.add_argument("--calibrate", action="store_true", help="cover test then exit")
    ap.add_argument("--no-servo", action="store_true", help="sensors + OLED only")
    ap.add_argument("--no-log", action="store_true", help="skip the CSV log")
    args = ap.parse_args()

    load_env()
    cfg = load_config()
    sensors = Sensors(cfg)

    if args.calibrate:
        cover_test(sensors, cfg)
        return 0

    pan_servo = tilt_servo = None
    if not args.no_servo:
        try:
            pan_servo = HwServo(int(cfg["pwm_chip"]), int(cfg["pan_channel"]))
            tilt_servo = HwServo(int(cfg["pwm_chip"]), int(cfg["tilt_channel"]))
        except Exception as e:
            print(f"Hardware PWM failed: {e}")
            print("Check the pwm-2chan overlay in /boot/firmware/config.txt, reboot, and "
                  "that pwm_chip matches `ls /sys/class/pwm` (Pi 5 is usually pwmchip2).")
            print("Continuing without servos.")
            pan_servo = tilt_servo = None

    pan = Axis("pan", HOME_PAN, cfg["pan_min"], cfg["pan_max"], 0.0, pan_servo)
    tilt = Axis("tilt", HOME_TILT, cfg["tilt_min"], cfg["tilt_max"], 45.0, tilt_servo)

    log = None if args.no_log else CsvLog(1.0 / float(cfg["log_hz"]))
    outbox = Outbox(float(cfg["outbox_s"]))
    kb = Keyboard()
    period = 1.0 / float(cfg["loop_hz"])

    state = "UNARMED"        # UNARMED, FIXED, TRACK
    armed = False
    ema: list[float] | None = None
    axis_turn = 0
    dark_since: float | None = None
    ab: dict | None = None   # A/B gain test state
    last_status = 0.0

    print("\nKeys: a=arm  j/l=pan  i/k=tilt  t=track  x=A/B test  c=calibrate  "
          "b/r=balance  f/g=flip  [ ]=gain  - ==deadband  q=quit")
    print("Keep the MB102 5V jumper OFF until you press 'a', then hold the tray.\n")

    def tracking_on() -> bool:
        if ab is not None:
            return ab["phase"] == "B"
        return state == "TRACK"

    try:
        while True:
            now = time.monotonic()

            # ---- keys ----
            k = kb.getch()
            if k:
                k = k.lower()
                if k == "q":
                    break
                elif k == "c":
                    cover_test(sensors, cfg, kb)
                elif k == "a" and not armed:
                    print("ARMING at electrical center (pan 0, tilt 45). Hold the tray.")
                    pan.pos, tilt.pos = HOME_PAN, HOME_TILT
                    if pan_servo and tilt_servo:
                        pan_servo.start(pan.us())
                        tilt_servo.start(tilt.us())
                    armed = True
                    state = "FIXED"
                elif k == "t" and armed and ab is None:
                    state = "TRACK" if state == "FIXED" else "FIXED"
                    dark_since = None
                elif k == "x" and armed:
                    if ab is None:
                        ab = {"phase": "A", "t0": now, "prev": state, "a": [], "b": []}
                        print("\nA/B test: parking at home, then 10 s fixed vs 10 s tracking")
                    else:
                        state = ab["prev"]
                        ab = None
                        print("\nA/B test aborted")
                elif k == "b" and ema is not None:
                    m = cfg["ldr_channels"]
                    raw_by_side = {s: ema[m[s]] for s in SIDES}
                    if min(raw_by_side.values()) > 50:
                        mean = sum(raw_by_side.values()) / 4.0
                        for s in SIDES:
                            cfg["ldr_scale"][s] = mean / raw_by_side[s]
                        save_config(cfg)
                        print("\nbalanced: " + ", ".join(
                            f"{s}={cfg['ldr_scale'][s]:.3f}" for s in SIDES))
                    else:
                        print("\ntoo dark to balance")
                elif k == "r":
                    for s in SIDES:
                        cfg["ldr_scale"][s] = 1.0
                    save_config(cfg)
                    print("\nbalance reset")
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
                elif state == "FIXED" and ab is None and k in "jlik":
                    if k == "j":
                        pan.jog(-JOG_STEP)
                    elif k == "l":
                        pan.jog(JOG_STEP)
                    elif k == "i":
                        tilt.jog(JOG_STEP)
                    elif k == "k":
                        tilt.jog(-JOG_STEP)

            # ---- sensors ----
            try:
                raw = sensors.read_raw()
            except OSError as e:
                print(f"\nI2C glitch (ADS1115): {e}. Check Duponts / common GND")
                time.sleep(0.1)
                continue
            volts, amps, watts = sensors.read_power()

            ema = raw if ema is None else [EMA_ALPHA * r + (1 - EMA_ALPHA) * e
                                           for r, e in zip(raw, ema)]
            m = cfg["ldr_channels"]
            sc = cfg["ldr_scale"]
            v = {s: ema[m[s]] * sc[s] for s in SIDES}
            L, R, T, B = v["left"], v["right"], v["top"], v["bottom"]
            total = sum(ema)
            ex = (R - L) / (R + L) if R + L > 1 else 0.0
            ez = (T - B) / (T + B) if T + B > 1 else 0.0

            gain = float(cfg["gain"])
            dead = float(cfg["deadband"])
            cap = float(cfg["max_step_deg"])
            mn = float(cfg["min_step_deg"])
            is_dark = total < float(cfg["dark_threshold"])

            # ---- A/B test sequencing ----
            if ab is not None:
                el = now - ab["t0"]
                settle = float(cfg["ab_settle_s"])
                meas = float(cfg["ab_measure_s"])
                if ab["phase"] == "A":
                    pan.toward(HOME_PAN, cap)
                    tilt.toward(HOME_TILT, cap)
                    if el > settle and watts is not None:
                        ab["a"].append(watts)
                    if el >= settle + meas:
                        ab["phase"], ab["t0"] = "B", now
                elif ab["phase"] == "B":
                    if el > settle and watts is not None:
                        ab["b"].append(watts)
                    if el >= settle + meas:
                        a = sum(ab["a"]) / len(ab["a"]) if ab["a"] else None
                        b = sum(ab["b"]) / len(ab["b"]) if ab["b"] else None
                        if a and b is not None and a > 0:
                            print(f"\nA/B RESULT: fixed {a * 1000:.2f} mW, tracking "
                                  f"{b * 1000:.2f} mW, gain {100 * (b - a) / a:+.1f}%")
                            outbox.ab_result(a * 1000, b * 1000)
                        else:
                            print("\nA/B RESULT: no power readings (INA219 missing or 0 W)")
                        state = "TRACK"
                        ab = None

            # ---- control ----
            display = state
            if ab is not None:
                display = f"AB-{ab['phase']}"
            elif state == "TRACK":
                if is_dark:
                    if dark_since is None:
                        dark_since = now
                    if now - dark_since > float(cfg["home_after_s"]):
                        display = "HOME"
                        pan.toward(HOME_PAN, HOME_STEP)
                        tilt.toward(HOME_TILT, HOME_STEP)
                    else:
                        display = "DARK"
                else:
                    dark_since = None
            if tracking_on() and not is_dark:
                ps, ts = int(cfg["pan_sign"]), int(cfg["tilt_sign"])
                do_pan = do_tilt = True
                if cfg["one_axis"]:
                    axis_turn ^= 1
                    do_pan, do_tilt = bool(axis_turn), not axis_turn
                dp = pan.update(ex, ps, gain, dead, cap, mn, now) if do_pan else 0.0
                dt_ = tilt.update(ez, ts, gain, dead, cap, mn, now) if do_tilt else 0.0
                if display == "TRACK" and dp == 0.0 and dt_ == 0.0:
                    display = "LOCKED"

            # ---- outputs ----
            sensors.draw(display, pan.pos if armed else None, tilt.pos, total,
                         volts, amps, watts)
            sample = {
                "state": display, "pan": round(pan.pos, 1), "tilt": round(tilt.pos, 1),
                "volts": None if volts is None else round(volts, 3),
                "amps": None if amps is None else round(amps, 4),
                "watts": None if watts is None else round(watts, 5),
                "ldr": {s: round(v[s]) for s in SIDES},
            }
            if log:
                log.maybe(now, sample)
            outbox.sample(now, sample)

            if now - last_status >= 0.25:
                last_status = now
                pw = f"{watts * 1000:.2f}mW" if watts is not None else f"sum={total:.0f}"
                print(f"\r{display:7} pan {pan.pos:+6.1f} tilt {tilt.pos:5.1f}  "
                      f"ex {ex:+.3f} ez {ez:+.3f}  L{L:5.0f} R{R:5.0f} T{T:5.0f} B{B:5.0f}  "
                      f"{pw}   ", end="", flush=True)

            dt = time.monotonic() - now
            if dt < period:
                time.sleep(period - dt)
    except KeyboardInterrupt:
        pass
    finally:
        kb.close()
        outbox.close()
        if log:
            log.close()
        for s in (pan_servo, tilt_servo):
            if s is not None:
                s.stop()
        try:
            sensors.oled.fill(0)
            sensors.oled.show()
        except Exception:
            pass
        print("\nPWM off. Bye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
