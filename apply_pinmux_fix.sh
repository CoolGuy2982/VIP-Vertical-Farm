#!/usr/bin/env bash
# apply_pinmux_fix.sh
#
# Compiles the "All GPIO pins bidirectional v2" device-tree overlay and
# installs it so the Jetson Orin Nano unlocks Pins 11 and 13 (and all other
# 40-pin header GPIOs) for user-space control via Jetson.GPIO.
#
# Must be run on the Jetson itself with sudo.
# After this script completes, follow the printed instructions to activate
# the overlay via jetson-io.py and then reboot.
#
# Usage:
#   sudo bash apply_pinmux_fix.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DTS_FILE="${SCRIPT_DIR}/all_gpio_pins_v2.dts"
DTBO_FILE="${SCRIPT_DIR}/all_gpio_pins_v2.dtbo"
BOOT_DIR="/boot"

echo "============================================================"
echo "  Jetson Orin Nano — Pinmux Fix (All GPIO Pins v2)"
echo "============================================================"

# ── 1. Check we are running as root ──────────────────────────────
if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: This script must be run with sudo." >&2
    echo "  sudo bash ${BASH_SOURCE[0]}" >&2
    exit 1
fi

# ── 2. Check dtc is available ────────────────────────────────────
if ! command -v dtc &>/dev/null; then
    echo "Installing device-tree compiler (dtc)..."
    apt-get install -y device-tree-compiler
fi

# ── 3. Compile DTS → DTBO ────────────────────────────────────────
echo
echo "Step 1/3: Compiling DTS overlay..."
echo "  dtc -O dtb -o ${DTBO_FILE} ${DTS_FILE}"
dtc -O dtb -o "${DTBO_FILE}" "${DTS_FILE}"
echo "  Compiled: ${DTBO_FILE}"

# ── 4. Copy DTBO to /boot ────────────────────────────────────────
echo
echo "Step 2/3: Copying DTBO to ${BOOT_DIR}..."
cp "${DTBO_FILE}" "${BOOT_DIR}/all_gpio_pins_v2.dtbo"
echo "  Installed: ${BOOT_DIR}/all_gpio_pins_v2.dtbo"

# ── 5. Print activation instructions ─────────────────────────────
echo
echo "Step 3/3: Activate the overlay via jetson-io.py"
echo "------------------------------------------------------------"
echo "  Run the following command, then follow the on-screen menu:"
echo
echo "    sudo /opt/nvidia/jetson-io/jetson-io.py"
echo
echo "  Inside jetson-io.py:"
echo "    1. Select  'Configure Jetson 40pin Header'"
echo "    2. Select  'All GPIO pins bidirectional v2'"
echo "    3. Choose  'Save and reboot to reconfigure pins'"
echo
echo "  The Jetson will reboot. After boot, Pins 11 and 13 (and all"
echo "  other header GPIOs) will be unlocked for Jetson.GPIO use."
echo "------------------------------------------------------------"
echo
echo "Pinmux overlay installed successfully."
