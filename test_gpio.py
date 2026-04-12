"""
GPIO relay test for Jetson Orin Nano.

Uses Active-Low logic: pulling a pin LOW turns the relay ON.
The pinmux must be unlocked first via the DTS overlay — see apply_pinmux_fix.sh.

Usage:
    sudo venv/bin/python3 test_gpio.py
"""

import sys
import time

try:
    import Jetson.GPIO as GPIO
except ImportError:
    print("ERROR: Jetson.GPIO not found. Install with: pip install Jetson.GPIO")
    sys.exit(1)

# BOARD pin numbers matching config.yaml
PUMP_PIN  = 11   # Relay 1 — water pump   (UART1_RTS_PR4, line 112)
LIGHT_PIN = 13   # Relay 2 — grow light   (SPI3_SCK_PY0,  line 122)

# Active-Low logic: LOW  = relay coil energised (ON)
#                   HIGH = relay coil released  (OFF)
RELAY_ON  = GPIO.LOW
RELAY_OFF = GPIO.HIGH

PULSE_SECONDS = 3   # how long each relay stays ON during the test


def pulse(pin: int, label: str):
    print(f"\n--- {label} (BOARD pin {pin}) ---")
    print("  NOTE: LOW = relay coil ON (click should be heard)")

    GPIO.output(pin, RELAY_ON)
    state = GPIO.input(pin)
    print(f"  -> Set LOW  (RELAY ON)  — readback: {state}  [expect 0]  ← listen for click")
    time.sleep(PULSE_SECONDS)

    GPIO.output(pin, RELAY_OFF)
    state = GPIO.input(pin)
    print(f"  -> Set HIGH (RELAY OFF) — readback: {state}  [expect 1]  ← listen for click")
    time.sleep(1)


def main():
    print("=" * 55)
    print("  Jetson Orin Nano — GPIO Relay Test (Active-Low)")
    print("=" * 55)
    print(f"  Pump  → BOARD pin {PUMP_PIN}")
    print(f"  Light → BOARD pin {LIGHT_PIN}")
    print()
    print("  IMPORTANT: LOW drives the relay coil ON.")
    print("  You should hear TWO clicks per pin: ON then OFF.")
    print()

    GPIO.setmode(GPIO.BOARD)
    GPIO.setwarnings(False)

    GPIO.setup(PUMP_PIN,  GPIO.OUT, initial=RELAY_OFF)
    GPIO.setup(LIGHT_PIN, GPIO.OUT, initial=RELAY_OFF)
    print("Pins initialised HIGH (relays OFF). Starting in 2 s...")
    time.sleep(2)

    pulse(PUMP_PIN,  "PUMP")
    pulse(LIGHT_PIN, "LIGHT")

    GPIO.cleanup()
    print("\nDone. Both relays returned to OFF state.")
    print("If no clicks were heard, verify the DTS overlay is applied")
    print("(run apply_pinmux_fix.sh and reboot).")


if __name__ == "__main__":
    main()
