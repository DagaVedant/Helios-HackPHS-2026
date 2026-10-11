#!/usr/bin/env python3
"""Minimal OLED-only test. Wire: VCC->pin1 3V3, GND->pin6, SDA->pin3, SCL->pin5."""

import time

import board
import busio
import adafruit_ssd1306
from PIL import Image, ImageDraw, ImageFont


def main() -> None:
    print("Opening I2C...")
    i2c = busio.I2C(board.SCL, board.SDA)
    time.sleep(0.1)

    while not i2c.try_lock():
        pass
    found = i2c.scan()
    i2c.unlock()
    print("Devices:", [hex(a) for a in found] if found else "(none)")

    if not found:
        print("Nothing on the bus. Check VCC/GND/SDA/SCL and pin 1 near the SD card.")
        return

    addr = 0x3C if 0x3C in found else (0x3D if 0x3D in found else found[0])
    print(f"Using OLED at {hex(addr)}")

    oled = adafruit_ssd1306.SSD1306_I2C(128, 64, i2c, addr=addr)
    oled.fill(0)
    oled.show()

    img = Image.new("1", (128, 64))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    draw.text((0, 0), "OLED OK", font=font, fill=255)
    draw.text((0, 16), f"addr {hex(addr)}", font=font, fill=255)
    draw.text((0, 32), "Micro-Tracker", font=font, fill=255)
    draw.rectangle((0, 48, 127, 63), outline=255, fill=0)
    oled.image(img)
    oled.show()
    print("Drew text on the OLED. If the screen is blank, try the other I2C addr or check VCC=3.3V.")

    for i in range(5):
        draw.rectangle((4 + i * 24, 52, 20 + i * 24, 59), outline=255, fill=255)
        oled.image(img)
        oled.show()
        time.sleep(0.3)

    print("Done.")


if __name__ == "__main__":
    main()
