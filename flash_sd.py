#!/usr/bin/env python3
"""
Flash a microSD card with Raspberry Pi OS and pre-configure it for VIP Vertical Farm.

Run this from your laptop after cloning the repo. It will:
  1. Download Raspberry Pi OS Lite (64-bit)
  2. Flash it to your microSD card
  3. Pre-configure Wi-Fi, SSH, and a first-boot setup script
  4. Optionally embed your .env / firebase credentials

Usage:
    python flash_sd.py
    python flash_sd.py --ssid "MyWiFi" --wifi-password "secret"
    python flash_sd.py --skip-flash        # only configure boot partition (already flashed)

NOTE: On Windows, run this as Administrator (right-click terminal > Run as administrator).
"""

import argparse
import ctypes
import getpass
import hashlib
import lzma
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────────

REPO_URL = "https://github.com/CoolGuy2982/VIP-Vertical-Farm.git"
RPI_OS_IMAGE_URL = "https://downloads.raspberrypi.com/raspios_lite_arm64_latest"
CACHE_DIR = Path(__file__).parent / ".cache"
PROJECT_DIR_ON_PI = "/home/{user}/VIP-Vertical-Farm"


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Flash and configure a microSD card for VIP Vertical Farm"
    )
    p.add_argument("--ssid", help="Wi-Fi network name")
    p.add_argument("--wifi-password", help="Wi-Fi password (prompted if SSID given)")
    p.add_argument("--pi-user", default="pi", help="Username on the Pi (default: pi)")
    p.add_argument("--pi-password", help="Password for Pi user (prompted if not given)")
    p.add_argument("--skip-flash", action="store_true",
                   help="Skip flashing — only configure the boot partition")
    p.add_argument("--image", type=Path, help="Path to a local .img or .img.xz file")
    p.add_argument("--embed-env", action="store_true",
                   help="Copy .env and firebase-credentials.json onto the SD card")
    p.add_argument("--country", default="US", help="Wi-Fi country code (default: US)")
    return p.parse_args()


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_admin():
    """Check if running with admin/root privileges."""
    if platform.system() == "Windows":
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
    return os.geteuid() == 0


def run(cmd, **kwargs):
    """Run a shell command and return output."""
    print(f"  $ {cmd}")
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, **kwargs
    )
    if result.stdout.strip():
        for line in result.stdout.strip().split("\n")[:20]:
            print(f"    {line}")
    if result.returncode != 0 and result.stderr.strip():
        for line in result.stderr.strip().split("\n")[:10]:
            print(f"    [err] {line}")
    return result


def hash_password(password):
    """Create a password hash for userconf.txt (SHA-256 via openssl format)."""
    import crypt
    return crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA256))


def hash_password_fallback(password):
    """Fallback password hash using hashlib for Windows (no crypt module)."""
    import secrets
    salt = secrets.token_hex(8)
    # Use a simple approach — the firstrun.sh will set the password properly
    return None


# ── Image download ────────────────────────────────────────────────────────────

def download_image(image_arg):
    """Download Raspberry Pi OS Lite image, return path to .img file."""
    if image_arg and image_arg.exists():
        img_path = image_arg
        if img_path.suffix == ".xz":
            return decompress_xz(img_path)
        return img_path

    CACHE_DIR.mkdir(exist_ok=True)
    xz_path = CACHE_DIR / "raspios_lite_arm64_latest.img.xz"
    img_path = CACHE_DIR / "raspios_lite_arm64_latest.img"

    if img_path.exists():
        print(f"\nUsing cached image: {img_path}")
        return img_path

    print(f"\nDownloading Raspberry Pi OS Lite (64-bit)...")
    print(f"  URL: {RPI_OS_IMAGE_URL}")
    print(f"  This may take a few minutes...\n")

    def progress_hook(block_num, block_size, total_size):
        downloaded = block_num * block_size
        if total_size > 0:
            pct = min(100, downloaded * 100 // total_size)
            mb = downloaded / (1024 * 1024)
            total_mb = total_size / (1024 * 1024)
            print(f"\r  [{pct:3d}%] {mb:.0f} / {total_mb:.0f} MB", end="", flush=True)

    try:
        urllib.request.urlretrieve(RPI_OS_IMAGE_URL, str(xz_path), progress_hook)
        print()
    except Exception as e:
        print(f"\nDownload failed: {e}")
        print("You can manually download from https://www.raspberrypi.com/software/operating-systems/")
        print(f"Then run: python flash_sd.py --image <path-to-image>")
        sys.exit(1)

    return decompress_xz(xz_path)


def decompress_xz(xz_path):
    """Decompress an .img.xz file to .img."""
    img_path = xz_path.with_suffix("")  # remove .xz
    if img_path.exists():
        return img_path

    print(f"\nDecompressing {xz_path.name}...")
    with lzma.open(str(xz_path), "rb") as xz_file:
        with open(str(img_path), "wb") as img_file:
            while True:
                chunk = xz_file.read(4 * 1024 * 1024)
                if not chunk:
                    break
                img_file.write(chunk)
                mb = img_file.tell() / (1024 * 1024)
                print(f"\r  {mb:.0f} MB written", end="", flush=True)
    print(f"\n  Decompressed to {img_path}")

    # remove .xz to save space
    xz_path.unlink()
    return img_path


# ── SD card detection ─────────────────────────────────────────────────────────

def detect_sd_cards_windows():
    """Detect removable USB drives on Windows."""
    result = run(
        'powershell -Command "Get-Disk | Where-Object { $_.BusType -eq \'USB\' } '
        '| Select-Object Number,FriendlyName,@{N=\'SizeGB\';E={[math]::Round($_.Size/1GB,1)}} '
        '| Format-Table -AutoSize | Out-String"'
    )
    disks = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        match = re.match(r"(\d+)\s+(.+?)\s+([\d.]+)", line)
        if match:
            num, name, size_gb = match.groups()
            disks.append({
                "number": int(num),
                "name": name.strip(),
                "size_gb": float(size_gb),
                "device": f"\\\\.\\PhysicalDrive{num}",
            })
    return disks


def detect_sd_cards_linux():
    """Detect removable drives on Linux."""
    result = run("lsblk -d -o NAME,SIZE,RM,TRAN,MODEL -n")
    disks = []
    for line in result.stdout.strip().split("\n"):
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "1":  # RM=1 means removable
            name = parts[0]
            disks.append({
                "number": name,
                "name": " ".join(parts[4:]) if len(parts) > 4 else name,
                "size_gb": parts[1],
                "device": f"/dev/{name}",
            })
    return disks


def detect_sd_cards_mac():
    """Detect external drives on macOS."""
    result = run("diskutil list external")
    disks = []
    for match in re.finditer(r"(/dev/disk\d+)\s+\(external.*?(\d+[\.\d]*\s+\w+)\)", result.stdout):
        dev, size = match.groups()
        disks.append({
            "number": dev.split("disk")[-1],
            "name": dev,
            "size_gb": size,
            "device": dev,
        })
    return disks


def detect_sd_cards():
    """Detect removable SD cards (cross-platform)."""
    system = platform.system()
    if system == "Windows":
        return detect_sd_cards_windows()
    elif system == "Linux":
        return detect_sd_cards_linux()
    elif system == "Darwin":
        return detect_sd_cards_mac()
    else:
        print(f"Unsupported OS: {system}")
        sys.exit(1)


def select_sd_card():
    """Let the user pick which SD card to flash."""
    print("\n--- Detecting SD cards ---")
    disks = detect_sd_cards()

    if not disks:
        print("\nNo removable drives found!")
        print("  - Is the microSD card inserted (via adapter)?")
        print("  - On Windows, make sure you're running as Administrator.")
        sys.exit(1)

    print("\nFound removable drives:")
    for i, d in enumerate(disks):
        print(f"  [{i + 1}] {d['name']}  ({d['size_gb']} GB)  [{d['device']}]")

    if len(disks) == 1:
        choice = 0
        print(f"\nOnly one drive found: {disks[0]['name']}")
    else:
        try:
            choice = int(input("\nWhich drive? Enter number: ")) - 1
        except (ValueError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)

    if choice < 0 or choice >= len(disks):
        print("Invalid selection.")
        sys.exit(1)

    disk = disks[choice]

    # Safety confirmation
    print(f"\n{'=' * 60}")
    print(f"  WARNING: ALL DATA on this drive will be ERASED!")
    print(f"  Drive: {disk['name']}")
    print(f"  Size:  {disk['size_gb']} GB")
    print(f"  Path:  {disk['device']}")
    print(f"{'=' * 60}")
    confirm = input("\nType 'YES' to confirm: ").strip()
    if confirm != "YES":
        print("Aborted.")
        sys.exit(1)

    return disk


# ── Flashing ──────────────────────────────────────────────────────────────────

def flash_windows(image_path, disk):
    """Flash image to SD card on Windows using diskpart + raw write."""
    disk_num = disk["number"]

    # Clean the disk with diskpart
    print(f"\nCleaning disk {disk_num}...")
    diskpart_script = f"select disk {disk_num}\nclean\n"
    script_path = CACHE_DIR / "diskpart_clean.txt"
    script_path.write_text(diskpart_script)
    result = run(f"diskpart /s \"{script_path}\"")
    if result.returncode != 0:
        print("diskpart failed. Are you running as Administrator?")
        sys.exit(1)

    # Write image directly to physical drive
    print(f"\nFlashing {image_path.name} to PhysicalDrive{disk_num}...")
    device_path = f"\\\\.\\PhysicalDrive{disk_num}"
    image_size = image_path.stat().st_size
    chunk_size = 4 * 1024 * 1024  # 4MB chunks

    try:
        with open(str(image_path), "rb") as img:
            with open(device_path, "r+b") as disk_dev:
                written = 0
                while True:
                    chunk = img.read(chunk_size)
                    if not chunk:
                        break
                    disk_dev.write(chunk)
                    written += len(chunk)
                    pct = written * 100 // image_size
                    mb = written / (1024 * 1024)
                    print(f"\r  [{pct:3d}%] {mb:.0f} MB", end="", flush=True)
                disk_dev.flush()
        print("\n  Flash complete!")
    except PermissionError:
        print("\nPermission denied! Make sure you're running as Administrator.")
        sys.exit(1)

    # Rescan so Windows picks up the new partitions
    print("\nRescanning disks...")
    rescan_script = "rescan\n"
    script_path = CACHE_DIR / "diskpart_rescan.txt"
    script_path.write_text(rescan_script)
    run(f"diskpart /s \"{script_path}\"")
    time.sleep(3)


def flash_linux(image_path, disk):
    """Flash image to SD card on Linux using dd."""
    device = disk["device"]
    # Unmount all partitions
    run(f"sudo umount {device}* 2>/dev/null", check=False)
    print(f"\nFlashing {image_path.name} to {device}...")
    result = run(
        f"sudo dd if=\"{image_path}\" of=\"{device}\" bs=4M status=progress conv=fsync"
    )
    if result.returncode != 0:
        print("Flash failed!")
        sys.exit(1)
    run("sudo sync")
    print("  Flash complete!")


def flash_mac(image_path, disk):
    """Flash image to SD card on macOS using dd."""
    device = disk["device"]
    raw_device = device.replace("disk", "rdisk")
    run(f"diskutil unmountDisk {device}")
    print(f"\nFlashing {image_path.name} to {raw_device}...")
    result = run(
        f"sudo dd if=\"{image_path}\" of=\"{raw_device}\" bs=4m"
    )
    if result.returncode != 0:
        print("Flash failed!")
        sys.exit(1)
    run("sudo sync")
    print("  Flash complete!")


def flash_image(image_path, disk):
    """Flash the image to the SD card (cross-platform)."""
    system = platform.system()
    if system == "Windows":
        flash_windows(image_path, disk)
    elif system == "Linux":
        flash_linux(image_path, disk)
    elif system == "Darwin":
        flash_mac(image_path, disk)


# ── Boot partition configuration ──────────────────────────────────────────────

def find_boot_partition():
    """Find the boot partition mount point after flashing."""
    system = platform.system()

    if system == "Windows":
        # Check all drive letters for the boot partition
        print("\nLooking for boot partition...")
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:\\")
            # RPi OS boot partition has these files
            if (drive / "cmdline.txt").exists():
                print(f"  Found boot partition at {drive}")
                return drive
            # Also check for an empty drive that just got mounted
            if (drive / "bootcode.bin").exists():
                print(f"  Found boot partition at {drive}")
                return drive

        print("\nBoot partition not found automatically.")
        print("It may take a moment for Windows to mount it.")
        print("Check File Explorer for a new 'boot' or 'bootfs' drive.\n")
        drive_letter = input("Enter the boot partition drive letter (e.g. E): ").strip().upper()
        return Path(f"{drive_letter}:\\")

    elif system == "Linux":
        # Try common mount points
        for mount in ["/media", "/mnt"]:
            for d in Path(mount).glob("**/cmdline.txt"):
                return d.parent
        boot_path = input("Enter boot partition mount path: ").strip()
        return Path(boot_path)

    elif system == "Darwin":
        boot = Path("/Volumes/bootfs")
        if boot.exists():
            return boot
        boot = Path("/Volumes/boot")
        if boot.exists():
            return boot
        boot_path = input("Enter boot partition mount path: ").strip()
        return Path(boot_path)


def configure_boot(boot_path, args):
    """Write configuration files to the boot partition."""
    print(f"\n--- Configuring boot partition at {boot_path} ---")

    pi_user = args.pi_user
    pi_password = args.pi_password or getpass.getpass(f"Choose a password for Pi user '{pi_user}': ")
    project_dir = PROJECT_DIR_ON_PI.format(user=pi_user)

    # 1. Enable SSH — just create an empty file named 'ssh'
    (boot_path / "ssh").touch()
    print("  [ok] SSH enabled")

    # 2. userconf.txt — sets the default username:password
    #    On Bookworm this is the primary way to set credentials
    #    Format: username:encrypted-password
    try:
        import crypt as crypt_mod
        pw_hash = crypt_mod.crypt(pi_password, crypt_mod.mksalt(crypt_mod.METHOD_SHA256))
        (boot_path / "userconf.txt").write_text(f"{pi_user}:{pw_hash}\n")
        print("  [ok] User credentials set via userconf.txt")
        pw_hash_available = True
    except (ImportError, Exception):
        # Windows doesn't have crypt module — firstrun.sh will handle it
        pw_hash_available = False
        print("  [info] Password will be set on first boot")

    # 3. Build the firstrun.sh script
    wifi_setup = ""
    if args.ssid:
        wifi_password = args.wifi_password or getpass.getpass(f"Wi-Fi password for '{args.ssid}': ")
        wifi_setup = f"""
# ── Configure Wi-Fi ───────────────────────────────────────
sleep 5
nmcli dev wifi connect "{args.ssid}" password "{wifi_password}" ifname wlan0 || true
rfkill unblock wifi || true
for i in 1 2 3 4 5; do
    if nmcli dev wifi connect "{args.ssid}" password "{wifi_password}" ifname wlan0 2>/dev/null; then
        break
    fi
    sleep 3
done
"""
    password_setup = ""
    if not pw_hash_available:
        password_setup = f"""
# ── Set user password (Windows couldn't hash it) ─────────
echo "{pi_user}:{pi_password}" | chpasswd
"""

    firstrun_script = f"""#!/bin/bash
set -e

echo "=========================================="
echo " VIP Vertical Farm — First Boot Setup"
echo "=========================================="
{password_setup}
{wifi_setup}
# ── Wait for network ─────────────────────────────────────
echo "Waiting for network..."
for i in $(seq 1 30); do
    if ping -c 1 github.com &>/dev/null; then
        echo "Network is up!"
        break
    fi
    sleep 2
done

# ── Install system packages ──────────────────────────────
echo "Installing system packages..."
apt-get update -qq
apt-get install -y -qq git python3-pip python3-venv libgpiod2

# ── Clone the repository ─────────────────────────────────
PROJECT_DIR="{project_dir}"
if [ ! -d "$PROJECT_DIR" ]; then
    echo "Cloning repository..."
    git clone {REPO_URL} "$PROJECT_DIR"
else
    echo "Repo already exists, pulling latest..."
    cd "$PROJECT_DIR" && git pull
fi

# ── Move credentials from boot partition ──────────────────
BOOT="/boot/firmware"
[ -d "$BOOT" ] || BOOT="/boot"

if [ -f "$BOOT/vipfarm_env" ]; then
    cp "$BOOT/vipfarm_env" "$PROJECT_DIR/.env"
    chown {pi_user}:{pi_user} "$PROJECT_DIR/.env"
    chmod 600 "$PROJECT_DIR/.env"
    echo "Moved .env into project"
fi

if [ -f "$BOOT/vipfarm_firebase.json" ]; then
    cp "$BOOT/vipfarm_firebase.json" "$PROJECT_DIR/firebase-credentials.json"
    chown {pi_user}:{pi_user} "$PROJECT_DIR/firebase-credentials.json"
    chmod 600 "$PROJECT_DIR/firebase-credentials.json"
    echo "Moved firebase credentials into project"
fi

# ── Python environment ────────────────────────────────────
echo "Setting up Python environment..."
python3 -m venv "$PROJECT_DIR/venv"
"$PROJECT_DIR/venv/bin/pip" install --upgrade pip -q
"$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt" -q
"$PROJECT_DIR/venv/bin/pip" install RPi.GPIO -q 2>/dev/null || true

# ── Create data directories ──────────────────────────────
mkdir -p "$PROJECT_DIR/data/images" "$PROJECT_DIR/data/logs"
chown -R {pi_user}:{pi_user} "$PROJECT_DIR"

# ── Create systemd service ───────────────────────────────
cat > /etc/systemd/system/ai-grower.service << 'SVCEOF'
[Unit]
Description=VIP Vertical Farm AI Grower
After=network-online.target
Wants=network-online.target

[Service]
User={pi_user}
WorkingDirectory={project_dir}
ExecStart={project_dir}/venv/bin/python -m src.main
Restart=always
RestartSec=10
Environment=PATH={project_dir}/venv/bin:/usr/bin

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable ai-grower

# ── Print connection info ─────────────────────────────────
IP=$(hostname -I | awk '{{print $1}}')
echo ""
echo "=========================================="
echo " Setup complete!"
echo " IP address: $IP"
echo " SSH: ssh {pi_user}@$IP"
echo " Dashboard: http://$IP:8080"
echo "=========================================="

# ── Remove this script from running again ─────────────────
sed -i 's| systemd.run=/boot/firmware/firstrun.sh||' /boot/firmware/cmdline.txt 2>/dev/null || true
sed -i 's| systemd.run=/boot/firstrun.sh||' /boot/cmdline.txt 2>/dev/null || true

exit 0
"""

    # Write firstrun.sh
    # Determine boot path on Pi (Bookworm uses /boot/firmware, older uses /boot)
    firstrun_path = boot_path / "firstrun.sh"
    firstrun_path.write_text(firstrun_script.replace("\r\n", "\n"), encoding="utf-8")
    print("  [ok] First-boot setup script written")

    # 4. Modify cmdline.txt to run firstrun.sh on boot
    cmdline_path = boot_path / "cmdline.txt"
    if cmdline_path.exists():
        cmdline = cmdline_path.read_text().strip()
        # Check if it already references firstrun
        if "firstrun.sh" not in cmdline:
            # Determine the correct path prefix based on what exists
            if "root=" in cmdline:
                # Add systemd.run to execute our script
                cmdline += " systemd.run=/boot/firmware/firstrun.sh systemd.run_success_action=reboot"
                cmdline_path.write_text(cmdline + "\n")
                print("  [ok] cmdline.txt updated to run firstrun.sh")
            else:
                print("  [warn] cmdline.txt format unexpected — you may need to run firstrun.sh manually")
    else:
        print("  [warn] cmdline.txt not found — firstrun.sh may not auto-run")

    # 5. Optionally embed .env and firebase credentials
    local_base = Path(__file__).parent
    if args.embed_env:
        env_file = local_base / ".env"
        firebase_file = local_base / "firebase-credentials.json"

        if env_file.exists():
            shutil.copy2(str(env_file), str(boot_path / "vipfarm_env"))
            print("  [ok] .env embedded (will be moved to project on first boot)")
        else:
            print("  [skip] .env not found locally")

        if firebase_file.exists():
            shutil.copy2(str(firebase_file), str(boot_path / "vipfarm_firebase.json"))
            print("  [ok] firebase-credentials.json embedded")
        else:
            print("  [skip] firebase-credentials.json not found locally")

    return pi_password


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  VIP Vertical Farm — SD Card Flasher")
    print("=" * 60)

    args = parse_args()

    # Check for admin/root
    if not args.skip_flash and not is_admin():
        print("\nERROR: This script must be run as Administrator (Windows) or root (Linux/Mac).")
        if platform.system() == "Windows":
            print("Right-click your terminal and select 'Run as administrator'.")
        else:
            print("Re-run with: sudo python flash_sd.py")
        sys.exit(1)

    if not args.skip_flash:
        # Download / locate image
        image_path = download_image(args.image)

        # Select SD card
        disk = select_sd_card()

        # Flash it
        flash_image(image_path, disk)

    # Find and configure boot partition
    boot_path = find_boot_partition()

    if not boot_path or not boot_path.exists():
        print(f"\nCould not find boot partition at {boot_path}")
        print("Try ejecting and reinserting the SD card, then run:")
        print("  python flash_sd.py --skip-flash")
        sys.exit(1)

    pi_password = configure_boot(boot_path, args)

    # Done!
    print(f"\n{'=' * 60}")
    print("  SD card is ready!")
    print(f"{'=' * 60}")
    print(f"\nNext steps:")
    print(f"  1. Eject the SD card and insert it into the Raspberry Pi")
    print(f"  2. Power on the Pi — first boot takes 3-5 minutes")
    if args.ssid:
        print(f"  3. Find the Pi on your network and SSH in:")
        print(f"       ssh {args.pi_user}@<pi-ip-address>")
    else:
        print(f"  3. Connect the Pi via Ethernet or add Wi-Fi manually")
        print(f"     (No Wi-Fi was configured — use --ssid next time)")
    print(f"  4. Password: {pi_password}")
    if not args.embed_env:
        print(f"\n  NOTE: You still need to create .env on the Pi:")
        print(f"    scp .env {args.pi_user}@<pi-ip>:~/VIP-Vertical-Farm/.env")
    print()


if __name__ == "__main__":
    main()
