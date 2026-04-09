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

for pin in PINS.values():
    GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)

print("All pins HIGH. Starting test in 2 seconds...\n")
time.sleep(2)

for name, pin in PINS.items():
    print(f"=== {name} (BOARD pin {pin}) ===")

    print(f"  >>> Going LOW now  — listen for relay click <<<")
    GPIO.output(pin, GPIO.LOW)
    time.sleep(3)

    print(f"  >>> Going HIGH now — listen for relay click <<<")
    GPIO.output(pin, GPIO.HIGH)
    time.sleep(3)

    print(f"  Done with {name}\n")

GPIO.cleanup()
print("Test complete. Tell me: did it click on LOW or HIGH?")
