#!/usr/bin/env python3
"""
Micro-Tracker sample — what you have wired tonight:
  ADS1115 (4 LDRs), SSD1306 OLED, pan GPIO12, tilt GPIO13.

Not wired yet (ignored):
  buttons, power LED, INA219 (optional — used only if found at 0x40).

Keyboard (run from a terminal on the Pi or over SSH):
  a     arm servos (first mid-pulse = pan 0, tilt 45)
  j / l pan left / right 5°
  i / k tilt up / down 5°
  t     toggle TRACK / FIXED
  c     quick cover-test remap of LDR channels
  q     quit (PWM off)

Boot (once, then reboot):
  sudo raspi-config  -> Interface Options -> I2C -> Enable
  Add to /boot/firmware/config.txt:
    dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4
  Then: sudo reboot
  Check: i2cdetect -y 1   (expect 0x48 ADS, 0x3C OLED)

Install:
  cd ~/MicroTracker-Everything/pi   # or wherever you copied this
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt

Run (MB102 3.3V on, 5V jumper OFF until you press 'a'):
  sudo .venv/bin/python tracker.py
"""

from __future__ import annotations

import argparse
import json
import math
import select
import sys
import termios
import time
import tty
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
    import lgpio
except ImportError:
    lgpio = None  # type: ignore


HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"

PAN_MIN, PAN_MAX = -80.0, 80.0
TILT_MIN, TILT_MAX = 15.0, 85.0
JOG_STEP = 5.0
US_PER_DEG = 1000.0 / 180.0  # 1000–2000 µs over 180°
MID_US = 1500.0
SERVO_HZ = 50.0
PERIOD_US = 1_000_000.0 / SERVO_HZ
# Servo signals (BCM). Physical pins: GPIO26=pin37, GPIO13=pin33.
# Do NOT power servo red from Pi 5V — MB102 5V only. Common GND required.
# Swap these two if pan/tilt are reversed.
PAN_GPIO = 26
TILT_GPIO = 13


# ---------------------------------------------------------------------------
# Config / helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with CONFIG_PATH.open() as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    with CONFIG_PATH.open("w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print(f"Saved {CONFIG_PATH}")


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def angle_to_us(pan: float, tilt: float) -> tuple[float, float]:
    pan_us = MID_US + pan * US_PER_DEG
    tilt_us = MID_US + (tilt - 45.0) * US_PER_DEG
    return pan_us, tilt_us


def us_to_duty(us: float) -> float:
    return (us / PERIOD_US) * 100.0


# ---------------------------------------------------------------------------
# Keyboard (non-blocking, no Enter needed)
# ---------------------------------------------------------------------------

class Keyboard:
    def __init__(self):
        self._fd = sys.stdin.fileno()
        self._old = None
        self._raw = False
        if sys.stdin.isatty():
            self._old = termios.tcgetattr(self._fd)
            self.enable_raw()

    def enable_raw(self) -> None:
        if self._old is not None and not self._raw:
            tty.setcbreak(self._fd)
            self._raw = True

    def disable_raw(self) -> None:
        """Restore normal cooked mode so input() works (cover test)."""
        if self._old is not None and self._raw:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
            self._raw = False

    def getch(self) -> str | None:
        if not sys.stdin.isatty() or not self._raw:
            return None
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if not r:
            return None
        return sys.stdin.read(1)

    def close(self):
        self.disable_raw()


# ---------------------------------------------------------------------------
# Hardware
# ---------------------------------------------------------------------------

class _BitBangServo:
    """50 Hz servo pulses in a daemon thread (reliable on Pi 5)."""

    def __init__(self, handle: int, gpio: int):
        import threading

        self.h = handle
        self.gpio = gpio
        self.us = MID_US
        self._run = False
        self._t: threading.Thread | None = None
        lgpio.gpio_claim_output(handle, gpio, 0)

    def start(self) -> None:
        if self._t is not None and self._t.is_alive():
            return
        self._run = True
        import threading

        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self) -> None:
        while self._run:
            try:
                hi = max(self.us, 500.0) / 1_000_000.0
                lo = max(0.020 - hi, 0.0)
                lgpio.gpio_write(self.h, self.gpio, 1)
                time.sleep(hi)
                lgpio.gpio_write(self.h, self.gpio, 0)
                time.sleep(lo)
            except Exception:
                # handle closed / chip gone — exit quietly
                break

    def set_us(self, us: float) -> None:
        self.us = clamp(us, 500.0, 2500.0)

    def stop(self) -> None:
        self._run = False
        if self._t is not None:
            self._t.join(timeout=0.5)
            self._t = None
        try:
            lgpio.gpio_write(self.h, self.gpio, 0)
        except Exception:
            pass


class Servos:
    """Bit-bang servo PWM on GPIO26/13 (header pins 37/33)."""

    def __init__(self, handle: int, chip: int):
        self.h = handle
        self.chip = chip
        self.pan = _BitBangServo(handle, PAN_GPIO)
        self.tilt = _BitBangServo(handle, TILT_GPIO)
        self.armed = False
        self._closed = False
        print(
            f"Servos on gpiochip{chip}:  "
            f"pan=GPIO{PAN_GPIO} (pin 37)  tilt=GPIO{TILT_GPIO} (pin 33)"
        )
        print("Power: servo red = MB102 5V only. Never Pi 5V. Common GND required.")

    @classmethod
    def open(cls, preferred: int | None = None) -> "Servos":
        if lgpio is None:
            raise RuntimeError("lgpio missing — sudo apt install -y python3-lgpio")
        order: list[int] = []
        if preferred is not None:
            order.append(int(preferred))
        for c in (0, 4, 1, 2):
            if c not in order:
                order.append(c)
        last_err: Exception | None = None
        for c in order:
            try:
                h = lgpio.gpiochip_open(c)
                return cls(h, c)
            except Exception as e:
                last_err = e
                print(f"gpiochip{c} failed: {e}")
        raise RuntimeError(f"Could not open gpiochip for servos: {last_err}")

    def arm(self, pan_deg: float, tilt_deg: float) -> None:
        pan_us, tilt_us = angle_to_us(pan_deg, tilt_deg)
        print(f"ARM: pan={pan_us:.0f}us  tilt={tilt_us:.0f}us")
        self.pan.set_us(pan_us)
        self.tilt.set_us(tilt_us)
        self.pan.start()
        self.tilt.start()
        self.armed = True

    def set(self, pan_deg: float, tilt_deg: float) -> None:
        if not self.armed:
            return
        pan_us, tilt_us = angle_to_us(pan_deg, tilt_deg)
        self.pan.set_us(pan_us)
        self.tilt.set_us(tilt_us)

    def pause(self) -> None:
        """Stop pulses but keep the chip open (for cover test)."""
        self.pan.stop()
        self.tilt.stop()
        self.armed = False

    def stop(self) -> None:
        self.pan.stop()
        self.tilt.stop()
        if not self._closed:
            try:
                lgpio.gpiochip_close(self.h)
            except Exception:
                pass
            self._closed = True
        self.armed = False


class Sensors:
    def __init__(self, cfg: dict):
        self.i2c = busio.I2C(board.SCL, board.SDA)
        time.sleep(0.05)
        found = set(self.i2c.scan())
        print("I2C devices:", [hex(a) for a in sorted(found)])

        if 0x48 not in found:
            raise RuntimeError("ADS1115 not found at 0x48 — check wiring / ADDR to GND")
        self.ads = ADS1115(self.i2c)
        self.ads.gain = 1  # ±4.096 V
        self.chans = [AnalogIn(self.ads, i) for i in range(4)]

        oled_addr = int(cfg.get("oled_address", 60))  # 0x3C = 60
        if oled_addr not in found and 0x3D in found:
            oled_addr = 0x3D
            print("OLED at 0x3D")
        if oled_addr not in found:
            raise RuntimeError("OLED not found at 0x3C/0x3D")
        self.oled = adafruit_ssd1306.SSD1306_I2C(128, 64, self.i2c, addr=oled_addr)
        self.oled.fill(0)
        self.oled.show()
        self.font = ImageFont.load_default()

        self.ina = None
        if INA219 is not None and 0x40 in found:
            self.ina = INA219(self.i2c)
            print("INA219 found — power readout enabled")
        else:
            print("INA219 not present — OLED will show LDR sum instead of watts")

        self.ldr_map = dict(cfg["ldr_channels"])

    def read_raw(self) -> list[float]:
        # Retry on Errno 121 (Remote I/O) — usually a flaky Dupont / servo noise blip
        last: Exception | None = None
        for _ in range(4):
            try:
                return [float(ch.value) for ch in self.chans]
            except OSError as e:
                last = e
                time.sleep(0.02)
        raise OSError(f"ADS1115 read failed after retries: {last}") from last

    def read_ldr(self) -> dict[str, float]:
        raw = self.read_raw()
        return {side: raw[idx] for side, idx in self.ldr_map.items()}

    def read_power(self) -> tuple[float | None, float | None, float | None]:
        if self.ina is None:
            return None, None, None
        try:
            v = float(self.ina.bus_voltage)
            a = float(self.ina.current) / 1000.0  # mA -> A
            w = v * a
            return v, a, w
        except Exception:
            return None, None, None

    def draw(self, state: str, pan: float, tilt: float, ldr: dict[str, float],
             volts: float | None, amps: float | None, watts: float | None) -> None:
        img = Image.new("1", (128, 64))
        d = ImageDraw.Draw(img)
        d.text((0, 0), f"MODE {state}", font=self.font, fill=255)
        if watts is not None and volts is not None and amps is not None:
            d.text((0, 14), f"{watts * 1000:6.1f} mW", font=self.font, fill=255)
            d.text((0, 26), f"{volts:5.2f}V  {amps * 1000:5.1f}mA", font=self.font, fill=255)
        else:
            s = sum(ldr.values())
            d.text((0, 14), f"LDR sum {s:7.0f}", font=self.font, fill=255)
            d.text((0, 26), f"L{ldr['left']:5.0f} R{ldr['right']:5.0f}", font=self.font, fill=255)
        if state == "UNARMED":
            d.text((0, 40), "SERVO OFF", font=self.font, fill=255)
            d.text((0, 52), "press a to arm", font=self.font, fill=255)
        else:
            d.text((0, 40), f"P{pan:+5.1f}  T{tilt:5.1f}", font=self.font, fill=255)
            d.text((0, 52), f"T{ldr['top']:5.0f} B{ldr['bottom']:5.0f}", font=self.font, fill=255)
        try:
            self.oled.image(img)
            self.oled.show()
        except OSError:
            pass  # skip one bad OLED frame rather than crashing


# ---------------------------------------------------------------------------
# Cover-test remap (good while LDRs are still in a temp cluster)
# ---------------------------------------------------------------------------

def cover_test(sensors: Sensors, cfg: dict, kb: Keyboard | None = None,
               servos: Servos | None = None) -> None:
    if servos is not None:
        servos.pause()
        print("(servos paused for calibration)")
    if kb is not None:
        kb.disable_raw()

    print("\nCover test — cover ONE LDR at a time when prompted.")
    print("The channel that drops the most is assigned to that side.\n")
    try:
        mapping: dict[str, int] = {}
        used: set[int] = set()
        for side in ("left", "right", "top", "bottom"):
            input(f"Uncover all, then press Enter. Next: cover {side.upper()} ... ")
            before = sensors.read_raw()
            input(f"Cover {side.upper()} now, then press Enter ... ")
            after = sensors.read_raw()
            drops = [(i, before[i] - after[i]) for i in range(4) if i not in used]
            drops.sort(key=lambda x: x[1], reverse=True)
            best_i, best_drop = drops[0]
            if best_drop < 200:
                print(f"  Warning: small drop ({best_drop:.0f}). Still assigning A{best_i}.")
            mapping[side] = best_i
            used.add(best_i)
            print(f"  {side} -> A{best_i}  (drop {best_drop:.0f})")
        cfg["ldr_channels"] = mapping
        sensors.ldr_map = mapping
        save_config(cfg)

        input("\nUnder the lamp, LDRs uncovered — press Enter ... ")
        bright = sum(sensors.read_raw())
        input("Cover all four LDRs — press Enter ... ")
        dark = sum(sensors.read_raw())
        cfg["dark_threshold"] = (bright + dark) / 2.0
        save_config(cfg)
        print(f"dark_threshold = {cfg['dark_threshold']:.0f}  (bright {bright:.0f}, dark {dark:.0f})")
    finally:
        if kb is not None:
            kb.enable_raw()
            print("Keys active again. Press a to re-arm servos if needed.")


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------

def track_step(ldr: dict[str, float], pan: float, tilt: float, cfg: dict,
               flip: int) -> tuple[float, float]:
    """One closed-loop step. flip=0 both axes, 1 pan only, 2 tilt only."""
    L, R, T, B = ldr["left"], ldr["right"], ldr["top"], ldr["bottom"]
    dead = float(cfg["deadband"])
    gain = float(cfg["gain"])
    cap = float(cfg["max_step_deg"])
    ps = float(cfg["pan_sign"])
    ts = float(cfg["tilt_sign"])

    def axis_step(hi: float, lo: float, sign: float) -> float:
        den = hi + lo
        if den < 1:
            return 0.0
        err = (hi - lo) / den
        if abs(err) < dead:
            return 0.0
        return clamp(sign * gain * err, -cap, cap)

    # brighter side is "hi": move toward right when R > L, toward top when T > B
    dpan = axis_step(R, L, ps) if flip != 2 else 0.0
    dtilt = axis_step(T, B, ts) if flip != 1 else 0.0
    return (
        clamp(pan + dpan, PAN_MIN, PAN_MAX),
        clamp(tilt + dtilt, TILT_MIN, TILT_MAX),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Micro-Tracker sample controller")
    ap.add_argument("--calibrate", action="store_true", help="Run cover test then exit")
    ap.add_argument("--no-servo", action="store_true", help="Sensors + OLED only")
    args = ap.parse_args()

    cfg = load_config()
    sensors = Sensors(cfg)

    if args.calibrate:
        cover_test(sensors, cfg)
        return 0

    servos = None
    if not args.no_servo:
        pref = cfg.get("gpiochip", cfg.get("pwm_chip", None))
        pref_i = int(pref) if pref is not None and str(pref).lower() != "auto" else None
        try:
            servos = Servos.open(pref_i)
            cfg["gpiochip"] = servos.chip
            save_config(cfg)
        except Exception as e:
            print(f"Servo PWM failed: {e}")
            print("If pins are stuck in hardware PWM mode, comment out dtoverlay=pwm-2chan")
            print("in /boot/firmware/config.txt, reboot, then retry.")
            print("Continuing with --no-servo behavior.")
            servos = None

    state = "UNARMED"
    pan, tilt = 0.0, 45.0
    axis_turn = 0
    kb = Keyboard()
    period = 1.0 / float(cfg.get("loop_hz", 20))

    print("\nKeys: a=arm  j/l=pan  i/k=tilt  t=track  c=calibrate  q=quit")
    print("Keep MB102 5V jumper OFF until you press 'a', then hold the tray.\n")

    try:
        while True:
            t0 = time.monotonic()
            ch = kb.getch()
            if ch:
                ch = ch.lower()
                if ch == "q":
                    break
                if ch == "c":
                    cover_test(sensors, cfg, kb=kb, servos=servos)
                    state = "UNARMED"
                    continue
                elif ch == "a" and state == "UNARMED":
                    print("ARMING — mid-pulse (pan 0, tilt 45). Hold the tray.")
                    pan, tilt = 0.0, 45.0
                    if servos is not None:
                        servos.arm(pan, tilt)
                    state = "FIXED"
                elif ch == "t" and state in ("FIXED", "TRACK", "DARK"):
                    state = "FIXED" if state in ("TRACK", "DARK") else "TRACK"
                    print(f"-> {state}")
                elif state == "FIXED":
                    if ch == "j":
                        pan = clamp(pan - JOG_STEP, PAN_MIN, PAN_MAX)
                    elif ch == "l":
                        pan = clamp(pan + JOG_STEP, PAN_MIN, PAN_MAX)
                    elif ch == "i":
                        tilt = clamp(tilt + JOG_STEP, TILT_MIN, TILT_MAX)
                    elif ch == "k":
                        tilt = clamp(tilt - JOG_STEP, TILT_MIN, TILT_MAX)
                    if servos is not None and ch in "jlik":
                        servos.set(pan, tilt)

            try:
                ldr = sensors.read_ldr()
            except OSError as e:
                print(f"\nI2C glitch (ADS1115): {e} — check Duponts / common GND")
                time.sleep(0.1)
                continue
            volts, amps, watts = sensors.read_power()
            display_state = state

            if state == "TRACK":
                if sum(ldr.values()) < float(cfg["dark_threshold"]):
                    display_state = "DARK"
                else:
                    flip = 0
                    if cfg.get("one_axis"):
                        axis_turn ^= 1
                        flip = 1 if axis_turn else 2
                    pan, tilt = track_step(ldr, pan, tilt, cfg, flip)
                    if servos is not None:
                        servos.set(pan, tilt)

            sensors.draw(display_state, pan, tilt, ldr, volts, amps, watts)

            # terminal heartbeat (SSH-friendly)
            if math.floor(t0 * 2) != math.floor((t0 - period) * 2):
                pwr = f"{watts*1000:.1f}mW" if watts is not None else f"sum={sum(ldr.values()):.0f}"
                print(
                    f"\r{display_state:7}  P{pan:+6.1f} T{tilt:5.1f}  "
                    f"L{ldr['left']:5.0f} R{ldr['right']:5.0f} "
                    f"T{ldr['top']:5.0f} B{ldr['bottom']:5.0f}  {pwr}   ",
                    end="",
                    flush=True,
                )

            dt = time.monotonic() - t0
            if dt < period:
                time.sleep(period - dt)
    except KeyboardInterrupt:
        print("\nInterrupted")
    finally:
        kb.close()
        if servos is not None:
            servos.stop()
        sensors.oled.fill(0)
        sensors.oled.show()
        print("\nPWM off. Bye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
