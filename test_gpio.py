"""
Direct GPIO relay test — run this ON the Jetson Nano.

    python3 test_gpio.py

Tests each relay in sequence (BOARD pin numbering, active-low logic).
"""

import time
import sys

try:
    import Jetson.GPIO as GPIO
except ImportError:
    print("ERROR: Jetson.GPIO not found. Try: sudo apt install python3-jetson-gpio")
    sys.exit(1)

PINS = {
    "LIGHT":     7,
    "PUMP":     11,
    "DASHBOARD": 22,
}

GPIO.setmode(GPIO.BOARD)
GPIO.setwarnings(False)

for name, pin in PINS.items():
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, GPIO.HIGH)   # start OFF (active-low)

print("GPIO ready. Pins initialised HIGH (all relays OFF).\n")

for name, pin in PINS.items():
    input(f"Press Enter to test {name} relay (BOARD pin {pin})...")
    print(f"  Setting pin {pin} LOW  → {name} relay should click ON")
    GPIO.output(pin, GPIO.LOW)
    time.sleep(3)
    print(f"  Setting pin {pin} HIGH → {name} relay should click OFF")
    GPIO.output(pin, GPIO.HIGH)
    result = input(f"  Did you hear a click? (y/n): ").strip().lower()
    if result != "y":
        print(f"  *** {name} relay did NOT click — check wiring on pin {pin} ***")
    print()

GPIO.cleanup()
print("Done. GPIO cleaned up.")
