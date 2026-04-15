"""
Hardware test for the VIP Vertical Farm — Jetson Orin Nano.

Tests both hardware actuators:
  1. Physical relay → Grow Light (BOARD Pin 13, Active-Low)
  2. Kasa HS103 Wi-Fi Smart Plug → Water Pump

Usage:
    sudo venv/bin/python3 test_hardware.py

Requires:
    - Pinmux overlay applied (run apply_pinmux_fix.sh and reboot)
    - kasa.plug_ip set in config.yaml (or pass --ip <ip>)
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

# ── Load config ──────────────────────────────────────────────────────────────
try:
    import yaml
    config_path = Path(__file__).resolve().parent / "config.yaml"
    with open(config_path) as f:
        _cfg = yaml.safe_load(f)
    LIGHT_PIN = _cfg.get("gpio", {}).get("grow_light_pin", 13)
    CONFIG_KASA_IP = _cfg.get("kasa", {}).get("plug_ip", "")
except Exception as e:
    print(f"WARNING: Could not load config.yaml ({e}), using defaults.")
    LIGHT_PIN = 13
    CONFIG_KASA_IP = ""

# ── Argument parsing ─────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="VIP Farm hardware test")
parser.add_argument("--ip", default=CONFIG_KASA_IP,
                    help="Kasa plug IP (overrides config.yaml)")
parser.add_argument("--pulse", type=float, default=3.0,
                    help="Seconds to hold each relay/plug ON during the test (default: 3)")
parser.add_argument("--skip-light", action="store_true", help="Skip the light relay test")
parser.add_argument("--skip-pump",  action="store_true", help="Skip the Kasa pump test")
args = parser.parse_args()

KASA_IP      = args.ip
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
    from kasa import SmartPlug
    KASA_AVAILABLE = True
except ImportError:
    print("WARNING: python-kasa not found — install with: pip install python-kasa")
    KASA_AVAILABLE = False

# Active-Low relay logic: LOW = relay coil ON, HIGH = relay coil OFF
RELAY_ON  = GPIO.LOW  if GPIO_AVAILABLE else None
RELAY_OFF = GPIO.HIGH if GPIO_AVAILABLE else None

PASS = "[PASS]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"


# ────────────────────────────────────────────────────────────────────────────
# Test 1: Physical grow-light relay (Pin 13, Active-Low)
# ────────────────────────────────────────────────────────────────────────────

def test_light_relay():
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
        print("  Did you apply apply_pinmux_fix.sh and reboot?")
        try:
            GPIO.cleanup()
        except Exception:
            pass
        return False


# ────────────────────────────────────────────────────────────────────────────
# Test 2: Kasa HS103 Wi-Fi Smart Plug (water pump)
# ────────────────────────────────────────────────────────────────────────────

async def _kasa_test(ip: str, pulse_secs: float):
    plug = SmartPlug(ip)

    print(f"  Connecting to Kasa plug at {ip}...")
    await plug.update()
    print(f"  Reachable  alias='{plug.alias}'  model={plug.model}  "
          f"currently_on={plug.is_on}")

    print(f"\n  -> Turning ON  (pump should start)...")
    await plug.turn_on()
    await plug.update()
    print(f"     is_on={plug.is_on}  [expect True]", end="")
    print(f"  {PASS}" if plug.is_on else f"  {FAIL}")
    time.sleep(pulse_secs)

    print(f"\n  -> Turning OFF (pump should stop)...")
    await plug.turn_off()
    await plug.update()
    print(f"     is_on={plug.is_on}  [expect False]", end="")
    print(f"  {PASS}" if not plug.is_on else f"  {FAIL}")


def test_kasa_plug():
    print("\n" + "=" * 55)
    print("  TEST 2: Kasa Wi-Fi Smart Plug  (water pump)")
    print("=" * 55)

    if not KASA_AVAILABLE:
        print(f"  {SKIP} python-kasa not installed.")
        return False

    if not KASA_IP or KASA_IP == "ENTER_PLUG_IP_HERE":
        print(f"  {SKIP} No Kasa IP configured.")
        print("  Set kasa.plug_ip in config.yaml or pass --ip <ip>.")
        return False

    try:
        asyncio.run(_kasa_test(KASA_IP, PULSE_SECS))
        print(f"\n  {PASS} Kasa plug test complete.")
        return True
    except Exception as e:
        print(f"\n  {FAIL} Kasa plug error: {e}")
        print("  Check the IP address and that both devices are on the same network.")
        return False


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  VIP Vertical Farm — Hardware Test")
    print("=" * 55)
    print(f"  Light relay : BOARD pin {LIGHT_PIN}  (Active-Low)")
    print(f"  Pump plug   : {KASA_IP or 'NOT CONFIGURED'}")
    print(f"  Pulse time  : {PULSE_SECS} s per actuator")

    results = {}

    if not args.skip_light:
        results["light_relay"] = test_light_relay()
    else:
        print(f"\n  {SKIP} Light relay test skipped (--skip-light).")

    if not args.skip_pump:
        results["kasa_pump"] = test_kasa_plug()
    else:
        print(f"\n  {SKIP} Kasa pump test skipped (--skip-pump).")

    # ── Summary ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 55)
    print("  Summary")
    print("=" * 55)
    all_passed = True
    for name, passed in results.items():
        if passed is None:
            status = SKIP
        elif passed:
            status = PASS
        else:
            status = FAIL
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
