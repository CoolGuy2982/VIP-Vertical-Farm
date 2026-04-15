"""
Hardware test for the VIP Vertical Farm — Jetson Orin Nano.

Tests both hardware actuators:
  1. Physical relay → Grow Light  (BOARD Pin 13, Active-Low)
  2. Kasa cloud     → Water Pump  (tplink-cloud-api, credentials from .env)

Usage:
    sudo venv/bin/python3 test_hardware.py

Requires:
    - Pinmux overlay applied (run apply_pinmux_fix.sh and reboot) for the light relay
    - KASA_USERNAME and KASA_PASSWORD set in .env (or in the shell environment)
    - Device name in Kasa app matching kasa_cloud.device_alias in config.yaml
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

# ── Load .env so credentials are available without exporting them manually ───
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass  # dotenv is optional here; credentials can be exported manually

# ── Load config ──────────────────────────────────────────────────────────────
try:
    import yaml
    config_path = Path(__file__).resolve().parent / "config.yaml"
    with open(config_path) as f:
        _cfg = yaml.safe_load(f)
    LIGHT_PIN    = _cfg.get("gpio", {}).get("grow_light_pin", 13)
    DEVICE_ALIAS = _cfg.get("kasa_cloud", {}).get("device_alias", "Water Pump")
except Exception as e:
    print(f"WARNING: Could not load config.yaml ({e}), using defaults.")
    LIGHT_PIN    = 13
    DEVICE_ALIAS = "Water Pump"

# ── Argument parsing ─────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="VIP Farm hardware test")
parser.add_argument("--alias", default=DEVICE_ALIAS,
                    help="Kasa device alias to test (overrides config.yaml)")
parser.add_argument("--pulse", type=float, default=3.0,
                    help="Seconds to hold each relay/plug ON during the test (default: 3)")
parser.add_argument("--skip-light", action="store_true", help="Skip the light relay test")
parser.add_argument("--skip-pump",  action="store_true", help="Skip the Kasa pump test")
args = parser.parse_args()

DEVICE_ALIAS = args.alias
PULSE_SECS   = args.pulse

# ── GPIO imports ─────────────────────────────────────────────────────────────
try:
    import Jetson.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    print("WARNING: Jetson.GPIO not found — light relay test will be skipped.")
    GPIO_AVAILABLE = False

# ── Kasa imports ─────────────────────────────────────────────────────────────
try:
    from tplinkcloud import TPLinkDeviceManager
    KASA_AVAILABLE = True
except ImportError:
    print("WARNING: tplink-cloud-api not found — install with: pip install tplink-cloud-api")
    KASA_AVAILABLE = False

RELAY_ON  = GPIO.LOW  if GPIO_AVAILABLE else None
RELAY_OFF = GPIO.HIGH if GPIO_AVAILABLE else None

PASS = "[PASS]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"


# ────────────────────────────────────────────────────────────────────────────
# Test 1: Physical grow-light relay (Pin 13, Active-Low)
# ────────────────────────────────────────────────────────────────────────────

def test_light_relay() -> bool:
    print("\n" + "=" * 55)
    print(f"  TEST 1: Grow Light Relay  (BOARD pin {LIGHT_PIN})")
    print("=" * 55)
    print("  Active-Low logic: LOW = relay coil ON (click heard)")
    print()

    if not GPIO_AVAILABLE:
        print(f"  {SKIP} Jetson.GPIO not available.")
        return False

    try:
        GPIO.setmode(GPIO.BOARD)
        GPIO.setwarnings(False)
        GPIO.setup(LIGHT_PIN, GPIO.OUT, initial=RELAY_OFF)
        print(f"  Pin {LIGHT_PIN} initialised HIGH (relay OFF).")
        time.sleep(0.5)

        print(f"\n  -> Set LOW  (RELAY ON)  — listen for click...")
        GPIO.output(LIGHT_PIN, RELAY_ON)
        readback = GPIO.input(LIGHT_PIN)
        print(f"     Readback: {readback}  [expect 0]", end="")
        print(f"  {PASS}" if readback == 0 else f"  {FAIL} (got {readback})")
        time.sleep(PULSE_SECS)

        print(f"\n  -> Set HIGH (RELAY OFF) — listen for click...")
        GPIO.output(LIGHT_PIN, RELAY_OFF)
        readback = GPIO.input(LIGHT_PIN)
        print(f"     Readback: {readback}  [expect 1]", end="")
        print(f"  {PASS}" if readback == 1 else f"  {FAIL} (got {readback})")
        time.sleep(0.5)

        GPIO.cleanup()
        print(f"\n  {PASS} Light relay test complete.")
        return True

    except Exception as e:
        print(f"\n  {FAIL} Light relay error: {e}")
        print("  Did you run apply_pinmux_fix.sh and reboot?")
        try:
            GPIO.cleanup()
        except Exception:
            pass
        return False


# ────────────────────────────────────────────────────────────────────────────
# Test 2: Kasa cloud smart plug (water pump)
# ────────────────────────────────────────────────────────────────────────────

async def _kasa_test(username: str, password: str, alias: str, pulse_secs: float):
    print(f"  Logging in to Kasa cloud as {username}...")
    manager = TPLinkDeviceManager(username, password)

    print(f"  Searching for device '{alias}'...")
    device = await manager.find_device(alias)

    if device is None:
        raise RuntimeError(
            f"Device '{alias}' not found. "
            "Check the name matches exactly (case-sensitive) in the Kasa app."
        )

    print(f"  Found: '{device.get_alias()}'")

    print(f"\n  -> Turning ON  (pump should start)...")
    await device.power_on()
    print(f"     power_on() sent  {PASS}")
    time.sleep(pulse_secs)

    print(f"\n  -> Turning OFF (pump should stop)...")
    await device.power_off()
    print(f"     power_off() sent  {PASS}")


def test_kasa_pump() -> bool:
    print("\n" + "=" * 55)
    print(f"  TEST 2: Kasa Cloud Smart Plug  ('{DEVICE_ALIAS}')")
    print("=" * 55)

    if not KASA_AVAILABLE:
        print(f"  {SKIP} tplink-cloud-api not installed.")
        return False

    username = os.environ.get("KASA_USERNAME", "")
    password = os.environ.get("KASA_PASSWORD", "")

    if not username or not password:
        print(f"  {FAIL} KASA_USERNAME or KASA_PASSWORD not set.")
        print("  Add them to .env or export them in your shell before running.")
        return False

    try:
        asyncio.run(_kasa_test(username, password, DEVICE_ALIAS, PULSE_SECS))
        print(f"\n  {PASS} Kasa cloud pump test complete.")
        return True
    except Exception as e:
        print(f"\n  {FAIL} Kasa cloud error: {e}")
        return False


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  VIP Vertical Farm — Hardware Test")
    print("=" * 55)
    print(f"  Light relay : BOARD pin {LIGHT_PIN}  (Active-Low)")
    print(f"  Pump device : '{DEVICE_ALIAS}'  (Kasa cloud)")
    print(f"  Pulse time  : {PULSE_SECS} s per actuator")
    print(f"  Kasa user   : {os.environ.get('KASA_USERNAME', '(not set)')}")

    results: dict[str, bool] = {}

    if not args.skip_light:
        results["light_relay"] = test_light_relay()
    else:
        print(f"\n  {SKIP} Light relay test skipped (--skip-light).")

    if not args.skip_pump:
        results["kasa_pump"] = test_kasa_pump()
    else:
        print(f"\n  {SKIP} Kasa pump test skipped (--skip-pump).")

    # ── Summary ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 55)
    print("  Summary")
    print("=" * 55)
    all_passed = True
    for name, passed in results.items():
        status = PASS if passed else FAIL
        if not passed:
            all_passed = False
        print(f"  {status}  {name}")

    if not results:
        print("  No tests ran.")
    elif all_passed:
        print("\n  All tests passed.")
    else:
        print("\n  One or more tests FAILED — check output above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
