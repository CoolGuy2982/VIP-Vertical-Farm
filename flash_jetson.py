#!/usr/bin/env python3
"""
Prepare a microSD card for the Jetson Nano after flashing JetPack OS.

This script does NOT flash the OS itself — use one of these to flash first:
  - NVIDIA SDK Manager (GUI): https://developer.nvidia.com/sdk-manager
  - balenaEtcher with a JetPack image
  - dd on Linux/Mac

After the OS is flashed and the Jetson has booted + completed initial setup,
run this script to configure Wi-Fi, clone the project, and set up the service.

Usage:
    python flash_jetson.py --host 192.168.1.50
    python flash_jetson.py --host 192.168.1.50 --ssid "MyWiFi" --wifi-password "secret"
    python flash_jetson.py --host 192.168.1.50 --embed-env
"""

import argparse
import getpass
import os
import shutil
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("Installing paramiko (SSH library)...")
    os.system(f"{sys.executable} -m pip install paramiko")
    import paramiko


REPO_URL = "https://github.com/CoolGuy2982/VIP-Vertical-Farm.git"
PROJECT_DIR = "/home/{user}/VIP-Vertical-Farm"
SERVICE_NAME = "ai-grower"


def parse_args():
    p = argparse.ArgumentParser(
        description="Configure a Jetson Nano for VIP Vertical Farm (post-flash)"
    )
    p.add_argument("--host", required=True, help="Jetson IP address or hostname")
    p.add_argument("--user", default="jetson", help="SSH username (default: jetson)")
    p.add_argument("--password", default=None, help="SSH password (prompted if not given)")
    p.add_argument("--ssid", help="Wi-Fi network name to connect to")
    p.add_argument("--wifi-password", help="Wi-Fi password (prompted if SSID given)")
    p.add_argument("--embed-env", action="store_true",
                   help="Copy .env and firebase-credentials.json to the Jetson")
    return p.parse_args()


def connect(host, user, password):
    print(f"\nConnecting to {user}@{host}...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(host, username=user, password=password, timeout=10)
        print(f"Connected to {host}")
        return ssh
    except Exception as e:
        print(f"Failed to connect: {e}")
        print("\nTroubleshooting:")
        print("  1. Have you completed the JetPack first-boot setup?")
        print("  2. Is the Jetson connected to the network (Ethernet or Wi-Fi)?")
        print("  3. Try connecting via Ethernet first, then configure Wi-Fi.")
        sys.exit(1)


def run_cmd(ssh, cmd, check=True, timeout=300):
    print(f"  $ {cmd}")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    if out:
        for line in out.split("\n")[:30]:
            print(f"    {line}")
    if err and exit_code != 0:
        for line in err.split("\n")[:10]:
            print(f"    [err] {line}")
    if check and exit_code != 0:
        print(f"  Command failed with exit code {exit_code}")
    return out, err, exit_code


def upload_file(ssh, local_path, remote_path):
    sftp = ssh.open_sftp()
    try:
        # ensure remote directory exists
        remote_dir = "/".join(remote_path.split("/")[:-1])
        try:
            sftp.stat(remote_dir)
        except FileNotFoundError:
            run_cmd(ssh, f"mkdir -p {remote_dir}")
        sftp.put(str(local_path), remote_path)
        print(f"  [uploaded] {local_path.name} → {remote_path}")
    finally:
        sftp.close()


def configure_wifi(ssh, ssid, wifi_password):
    print("\n--- Configuring Wi-Fi ---")
    run_cmd(ssh, "rfkill unblock wifi", check=False)
    # Try nmcli (standard on JetPack Ubuntu)
    out, err, code = run_cmd(
        ssh,
        f'nmcli dev wifi connect "{ssid}" password "{wifi_password}" ifname wlan0',
        check=False
    )
    if code == 0:
        print(f"  Connected to {ssid}")
    else:
        print(f"  Wi-Fi connection failed. You may need to configure manually:")
        print(f"    nmcli dev wifi connect \"{ssid}\" password \"<password>\" ifname wlan0")


def install_system_deps(ssh):
    print("\n--- Installing system packages ---")
    run_cmd(ssh, "sudo apt-get update -qq")
    run_cmd(ssh, "sudo apt-get install -y -qq git python3-pip python3-venv libgpiod2 python3-dev", check=False)


def clone_repo(ssh, project_dir):
    print("\n--- Cloning repository ---")
    # check if already exists
    out, _, code = run_cmd(ssh, f"test -d {project_dir} && echo exists || echo missing", check=False)
    if "exists" in out:
        print("  Repo already exists, pulling latest...")
        run_cmd(ssh, f"cd {project_dir} && git pull")
    else:
        run_cmd(ssh, f"git clone {REPO_URL} {project_dir}")


def setup_python_env(ssh, project_dir):
    print("\n--- Setting up Python environment ---")
    run_cmd(ssh, f"python3 -m venv {project_dir}/venv")
    run_cmd(ssh, f"{project_dir}/venv/bin/pip install --upgrade pip -q")
    run_cmd(ssh, f"{project_dir}/venv/bin/pip install -r {project_dir}/requirements.txt -q", timeout=600)

    print("  Installing Jetson.GPIO...")
    run_cmd(ssh, f"{project_dir}/venv/bin/pip install Jetson.GPIO -q", check=False)


def setup_gpio_permissions(ssh, user):
    print("\n--- Setting up GPIO permissions ---")
    run_cmd(ssh, f"sudo usermod -aG gpio {user}", check=False)
    print(f"  Added {user} to gpio group")


def apply_pinmux_fix(ssh, project_dir):
    print("\n--- Applying Pinmux Fix (All GPIO pins v2) ---")
    run_cmd(ssh, f"chmod +x {project_dir}/apply_pinmux_fix.sh")
    run_cmd(ssh, f"sudo bash {project_dir}/apply_pinmux_fix.sh", check=False)
    print("  Pinmux DTBO compiled and installed to /boot.")
    print("  ACTION REQUIRED: after setup completes, SSH into the Jetson and run:")
    print("    sudo /opt/nvidia/jetson-io/jetson-io.py")
    print("  Select 'All GPIO pins bidirectional v2', save, and reboot.")


def create_data_dirs(ssh, project_dir):
    print("\n--- Creating data directories ---")
    run_cmd(ssh, f"mkdir -p {project_dir}/data/images {project_dir}/data/logs")


def setup_systemd_service(ssh, project_dir, user):
    print("\n--- Setting up auto-start service ---")
    service = f"""[Unit]
Description=VIP Vertical Farm AI Grower
After=network-online.target
Wants=network-online.target

[Service]
User={user}
WorkingDirectory={project_dir}
ExecStart={project_dir}/venv/bin/python -m src.main
Restart=always
RestartSec=10
Environment=PATH={project_dir}/venv/bin:/usr/bin

[Install]
WantedBy=multi-user.target
"""
    run_cmd(ssh, f"echo '{service}' | sudo tee /etc/systemd/system/{SERVICE_NAME}.service > /dev/null")
    run_cmd(ssh, "sudo systemctl daemon-reload")
    run_cmd(ssh, f"sudo systemctl enable {SERVICE_NAME}")
    print(f"  Service '{SERVICE_NAME}' installed and enabled")


def embed_credentials(ssh, project_dir):
    """Upload .env and firebase-credentials.json from local machine."""
    local_base = Path(__file__).parent

    env_file = local_base / ".env"
    if env_file.exists():
        upload_file(ssh, env_file, f"{project_dir}/.env")
        run_cmd(ssh, f"chmod 600 {project_dir}/.env")
    else:
        print("  [skip] .env not found locally")

    firebase_file = local_base / "firebase-credentials.json"
    if firebase_file.exists():
        upload_file(ssh, firebase_file, f"{project_dir}/firebase-credentials.json")
        run_cmd(ssh, f"chmod 600 {project_dir}/firebase-credentials.json")
    else:
        print("  [skip] firebase-credentials.json not found locally")


def print_instructions():
    print("""
============================================================
  JETSON NANO SETUP — Before running this script
============================================================

  1. Flash JetPack OS to your microSD card using one of:
     - NVIDIA SDK Manager (recommended):
       https://developer.nvidia.com/sdk-manager
     - balenaEtcher with a JetPack image:
       https://developer.nvidia.com/embedded/jetpack
     - Or on Linux: sudo dd if=jetpack.img of=/dev/sdX bs=4M

  2. Insert the SD card into the Jetson Nano and power it on

  3. Complete the first-boot setup (language, user, password)
     - Connect a monitor + keyboard for this step
     - OR use headless setup via USB serial connection

  4. Connect the Jetson to your network:
     - Ethernet: plug in a cable
     - Wi-Fi: use --ssid flag with this script

  5. Find the Jetson's IP address:
     - On the Jetson: hostname -I
     - On your router's admin page
     - Or: ping <hostname>.local

  6. Run this script:
     python flash_jetson.py --host <jetson-ip> --embed-env

============================================================
""")


def main():
    args = parse_args()

    print("=" * 60)
    print("  VIP Vertical Farm — Jetson Nano Setup")
    print("=" * 60)

    password = args.password
    if not password:
        password = getpass.getpass(f"SSH password for {args.user}@{args.host}: ")

    ssh = connect(args.host, args.user, password)
    project_dir = PROJECT_DIR.format(user=args.user)

    # Configure Wi-Fi if requested
    if args.ssid:
        wifi_password = args.wifi_password
        if not wifi_password:
            wifi_password = getpass.getpass(f"Wi-Fi password for '{args.ssid}': ")
        configure_wifi(ssh, args.ssid, wifi_password)

    # Install system dependencies
    install_system_deps(ssh)

    # Clone the repo
    clone_repo(ssh, project_dir)

    # Create data directories
    create_data_dirs(ssh, project_dir)

    # Setup Python environment
    setup_python_env(ssh, project_dir)

    # GPIO permissions
    setup_gpio_permissions(ssh, args.user)

    # Apply pinmux fix so hardware GPIOs are unlocked across reboots
    apply_pinmux_fix(ssh, project_dir)

    # Embed credentials if requested
    if args.embed_env:
        print("\n--- Uploading credentials ---")
        embed_credentials(ssh, project_dir)

    # Setup systemd service
    setup_systemd_service(ssh, project_dir, args.user)

    # Get the IP for display
    ip_out, _, _ = run_cmd(ssh, "hostname -I", check=False)
    ip = ip_out.split()[0] if ip_out.strip() else args.host

    ssh.close()

    print(f"\n{'=' * 60}")
    print("  Setup complete!")
    print(f"{'=' * 60}")
    print(f"  Project: {project_dir}")
    print(f"  SSH: ssh {args.user}@{ip}")
    print(f"  Service: sudo systemctl status {SERVICE_NAME}")
    print(f"  Logs: sudo journalctl -u {SERVICE_NAME} -f")
    print(f"  Dashboard: http://{ip}:8080")
    if not args.embed_env:
        print(f"\n  NOTE: You still need to create .env on the Jetson:")
        print(f"    scp .env {args.user}@{ip}:{project_dir}/.env")
    print(f"\n  To start the service:")
    print(f"    ssh {args.user}@{ip}")
    print(f"    sudo systemctl start {SERVICE_NAME}")
    print()


if __name__ == "__main__":
    main()
