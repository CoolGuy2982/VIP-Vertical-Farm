"""
Run this directly on the Jetson to diagnose relay wiring.

    sudo python3 test_gpio.py

Works through each relay pin and tries both LOW and HIGH signals
so you can figure out whether your relay module is active-LOW or active-HIGH.
"""

import time
import sys

try:
    import Jetson.GPIO as GPIO
except ImportError:
    print("ERROR: Jetson.GPIO not found.")
    sys.exit(1)

PINS = {
    "LIGHT":      7,
    "PUMP":      11,
    "DASHBOARD": 22,
}

GPIO.setmode(GPIO.BOARD)
GPIO.setwarnings(False)

for pin in PINS.values():
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, GPIO.LOW)

print("=" * 50)
print("GPIO RELAY DIAGNOSTIC")
print("=" * 50)
print("Watch/listen for relay clicks.\n")

for name, pin in PINS.items():
    print(f"--- {name} (BOARD pin {pin}) ---")
    input("  Press Enter to start...")

    print(f"  LOW  signal on pin {pin} (3 seconds)")
    GPIO.output(pin, GPIO.LOW)
    time.sleep(3)
    GPIO.output(pin, GPIO.LOW)  # keep low

    heard = input("  Did relay click when LOW? (y/n): ").strip().lower()
    low_works = heard == "y"

    print(f"  HIGH signal on pin {pin} (3 seconds)")
    GPIO.output(pin, GPIO.HIGH)
    time.sleep(3)
    GPIO.output(pin, GPIO.HIGH)  # keep high

    heard = input("  Did relay click when HIGH? (y/n): ").strip().lower()
    high_works = heard == "y"

    GPIO.output(pin, GPIO.LOW)  # return to low

    if low_works and not high_works:
        print(f"  RESULT: {name} is ACTIVE-LOW (correct, matches actuators.py)")
    elif high_works and not low_works:
        print(f"  RESULT: {name} is ACTIVE-HIGH — actuators.py needs to be flipped!")
    elif not low_works and not high_works:
        print(f"  RESULT: {name} did NOT click on either signal.")
        print(f"          Check: VCC wire should be on Jetson pin 2 (5V), not pin 1 (3.3V)")
        print(f"          Check: signal wire is actually on physical pin {pin}")
    else:
        print(f"  RESULT: clicked on both? Check wiring.")
    print()

GPIO.cleanup()
print("Done.")
