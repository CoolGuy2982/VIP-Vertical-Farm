"""
Run on the Jetson to test relay wiring.

    python3 test_gpio.py

Uses Linux sysfs GPIO directly (bypasses Jetson.GPIO library entirely).
Linux GPIO numbers from the Jetson Nano pinout:
  Physical pin 11 → Linux GPIO 50
  Physical pin 15 → Linux GPIO 194
"""

import time
import os
import sys

PINS = {
    "PUMP  (physical pin 11)": 50,
    "LIGHT (physical pin 15)": 194,
}

def gpio_write(gpio_num, value):
    with open(f"/sys/class/gpio/gpio{gpio_num}/value", "w") as f:
        f.write(str(value))

def gpio_setup(gpio_num):
    export_path = f"/sys/class/gpio/gpio{gpio_num}"
    if not os.path.exists(export_path):
        with open("/sys/class/gpio/export", "w") as f:
            f.write(str(gpio_num))
        time.sleep(0.1)
    with open(f"{export_path}/direction", "w") as f:
        f.write("out")
    gpio_write(gpio_num, 1)  # start HIGH

def gpio_cleanup(gpio_num):
    gpio_write(gpio_num, 1)  # leave HIGH (relay off)
    with open("/sys/class/gpio/unexport", "w") as f:
        f.write(str(gpio_num))

print("Setting up GPIO via sysfs (Linux GPIO numbers)...")
for name, gpio_num in PINS.items():
    try:
        gpio_setup(gpio_num)
        print(f"  {name} → GPIO {gpio_num} ready")
    except PermissionError:
        print(f"  ERROR: Permission denied. Run with: sudo python3 test_gpio.py")
        sys.exit(1)
    except Exception as e:
        print(f"  ERROR on GPIO {gpio_num}: {e}")
        sys.exit(1)

print("\nAll pins HIGH. Starting test in 2 seconds...\n")
time.sleep(2)

for name, gpio_num in PINS.items():
    print(f"=== {name} (Linux GPIO {gpio_num}) ===")

    print(f"  >>> Going LOW now  — listen for relay click <<<")
    gpio_write(gpio_num, 0)
    time.sleep(3)

    print(f"  >>> Going HIGH now — listen for relay click <<<")
    gpio_write(gpio_num, 1)
    time.sleep(3)

    print(f"  Done with {name}\n")

for name, gpio_num in PINS.items():
    try:
        gpio_cleanup(gpio_num)
    except Exception:
        pass

print("Test complete. Tell me: did it click on LOW or HIGH?")
