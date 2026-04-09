"""
Run on the Jetson to test relay wiring.

    python3 test_gpio.py
"""

import time
import sys

try:
    import Jetson.GPIO as GPIO
except ImportError:
    print("ERROR: Jetson.GPIO not found")
    sys.exit(1)

PINS = {"LIGHT": 13, "PUMP": 11}

GPIO.setmode(GPIO.BOARD)
GPIO.setwarnings(True)

print("Setting up pins...")
for name, pin in PINS.items():
    GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)
    state = GPIO.input(pin)
    print(f"  {name} pin {pin} — set HIGH, reads back: {state} (expect 1)")

print("\nAll pins HIGH. Starting test in 2 seconds...\n")
time.sleep(2)

for name, pin in PINS.items():
    print(f"=== {name} (BOARD pin {pin}) ===")

    GPIO.output(pin, GPIO.LOW)
    state = GPIO.input(pin)
    print(f"  Set LOW  — reads back: {state} (expect 0) — listen for click...")
    time.sleep(3)

    GPIO.output(pin, GPIO.HIGH)
    state = GPIO.input(pin)
    print(f"  Set HIGH — reads back: {state} (expect 1) — listen for click...")
    time.sleep(3)

    print()

GPIO.cleanup()
print("Done. Share the readback values above.")
