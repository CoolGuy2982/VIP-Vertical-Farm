"""
Run on the Jetson to test relay wiring.

    python3 test_gpio.py

Automatically pulses each relay pin HIGH and LOW.
Watch the relay LEDs and listen for clicks.
"""

import time
import sys

try:
    import Jetson.GPIO as GPIO
except ImportError:
    print("ERROR: Jetson.GPIO not found")
    sys.exit(1)

PINS = {"LIGHT": 7, "PUMP": 11}

GPIO.setmode(GPIO.BOARD)
GPIO.setwarnings(False)
for pin in PINS.values():
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, GPIO.LOW)

print("Starting relay test. Watch for relay LED / listen for clicks.\n")

for name, pin in PINS.items():
    print(f"=== {name} (BOARD pin {pin}) ===")

    print(f"  LOW  for 3s  (active-LOW relay should click ON now)")
    GPIO.output(pin, GPIO.LOW)
    time.sleep(3)
    GPIO.output(pin, GPIO.LOW)

    print(f"  HIGH for 3s  (active-HIGH relay should click ON now)")
    GPIO.output(pin, GPIO.HIGH)
    time.sleep(3)
    GPIO.output(pin, GPIO.HIGH)

    print(f"  back to LOW\n")
    GPIO.output(pin, GPIO.LOW)
    time.sleep(1)

GPIO.cleanup()
print("Done. Tell me: did the relay click on LOW or HIGH?")
