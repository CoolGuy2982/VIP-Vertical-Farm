"""
Push code to the Jetson Nano and set everything up.
Run this from your laptop while the Jetson is on the same network.

Usage:
    python setup_device.py --host 192.168.1.50
    python setup_device.py --host 192.168.1.50 --user jetson --password jetson
"""

import argparse
import getpass
import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("Installing paramiko (SSH library)...")
    os.system(f"{sys.executable} -m pip install paramiko")
    import paramiko


PROJECT_DIR = "/home/{user}/VIP-Vertical-Farm"
VENV_DIR = f"{{project_dir}}/venv"
SERVICE_NAME = "ai-grower"

# files and folders to push (relative to this script)
PUSH_ITEMS = [
    "src/",
    "config.yaml",
    "requirements.txt",
    ".env",
    "firebase-credentials.json",
    ".env.example",
    "apply_pinmux_fix.sh",
    "all_gpio_pins_v2.dts",
]

# skip these when uploading
SKIP_PATTERNS = ["__pycache__", ".pyc", "data/images/", "data/logs/", "venv/"]


def parse_args():
    parser = argparse.ArgumentParser(description="Set up the Jetson Nano for VIP Vertical Farm")
    parser.add_argument("--host", required=True, help="Jetson hostname or IP address")
    parser.add_argument("--user", default="jetson", help="SSH username (default: jetson)")
    parser.add_argument("--password", default=None, help="SSH password (will prompt if not given)")
    parser.add_argument("--skip-deps", action="store_true", help="Skip installing dependencies")
    parser.add_argument("--code-only", action="store_true", help="Only push code, skip all setup")
    return parser.parse_args()


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
        print("  1. Is the Jetson on and connected to the same network?")
        print("  2. Try using the Jetson's IP address instead of hostname")
        print("  3. Is SSH enabled on the Jetson?")
        sys.exit(1)


def run_cmd(ssh, cmd, check=True):
    print(f"  $ {cmd}")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=300)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    if out:
        for line in out.split("\n"):
            print(f"    {line}")
    if err and exit_code != 0:
        for line in err.split("\n"):
            print(f"    [err] {line}")
    if check and exit_code != 0:
        print(f"  Command failed with exit code {exit_code}")
    return out, err, exit_code


def should_skip(path_str):
    for pattern in SKIP_PATTERNS:
        if pattern in path_str:
            return True
    return False


def push_files(ssh, local_base, remote_base):
    sftp = ssh.open_sftp()
    local_base = Path(local_base)
    uploaded = 0
    skipped = 0

    print(f"\nPushing files to {remote_base}...")

    # make sure the remote base exists
    try:
        sftp.stat(remote_base)
    except FileNotFoundError:
        run_cmd(ssh, f"mkdir -p {remote_base}")

    for item in PUSH_ITEMS:
        local_path = local_base / item

        if not local_path.exists():
            print(f"  [skip] {item} (not found locally)")
            skipped += 1
            continue

        if local_path.is_file():
            remote_path = f"{remote_base}/{item}"
            remote_dir = "/".join(remote_path.split("/")[:-1])
            try:
                sftp.stat(remote_dir)
            except FileNotFoundError:
                run_cmd(ssh, f"mkdir -p {remote_dir}")
            print(f"  [file] {item}")
            sftp.put(str(local_path), remote_path)
            uploaded += 1
        elif local_path.is_dir():
            for file_path in local_path.rglob("*"):
                if file_path.is_file():
                    rel = file_path.relative_to(local_base)
                    rel_str = str(rel).replace("\\", "/")
                    if should_skip(rel_str):
                        continue
                    remote_path = f"{remote_base}/{rel_str}"
                    remote_dir = "/".join(remote_path.split("/")[:-1])
                    try:
                        sftp.stat(remote_dir)
                    except FileNotFoundError:
                        run_cmd(ssh, f"mkdir -p {remote_dir}")
                    print(f"  [file] {rel_str}")
                    sftp.put(str(file_path), remote_path)
                    uploaded += 1

    sftp.close()
    print(f"\nUploaded {uploaded} files, skipped {skipped}")
    return uploaded


def install_system_deps(ssh):
    print("\n--- Installing system packages ---")
    run_cmd(ssh, "sudo apt-get update -qq")
    run_cmd(ssh, "sudo apt-get install -y -qq python3-pip python3-venv libgpiod2", check=False)


def setup_python_env(ssh, project_dir):
    print("\n--- Setting up Python environment ---")
    run_cmd(ssh, f"python3 -m venv {project_dir}/venv")
    run_cmd(ssh, f"{project_dir}/venv/bin/pip install --upgrade pip -q")
    run_cmd(ssh, f"{project_dir}/venv/bin/pip install -r {project_dir}/requirements.txt -q")

    # install Jetson.GPIO for hardware relay control
    print("  Installing Jetson.GPIO...")
    run_cmd(ssh, f"{project_dir}/venv/bin/pip install Jetson.GPIO -q", check=False)


def setup_gpio_permissions(ssh, user):
    print("\n--- Setting up GPIO permissions ---")
    run_cmd(ssh, f"sudo usermod -aG gpio {user}", check=False)
    print(f"  Added {user} to gpio group (takes effect on next login)")


def apply_pinmux_fix(ssh, project_dir):
    print("\n--- Applying Pinmux Fix (All GPIO pins v2) ---")
    run_cmd(ssh, f"chmod +x {project_dir}/apply_pinmux_fix.sh")
    run_cmd(ssh, f"sudo bash {project_dir}/apply_pinmux_fix.sh", check=False)
    print("  Pinmux DTBO compiled and installed to /boot.")
    print("  ACTION REQUIRED: SSH into the Jetson and run:")
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
    run_cmd(ssh, f"sudo systemctl daemon-reload")
    run_cmd(ssh, f"sudo systemctl enable {SERVICE_NAME}")
    print(f"  Service '{SERVICE_NAME}' installed and enabled")


def restart_service(ssh):
    print("\n--- Restarting service ---")
    run_cmd(ssh, f"sudo systemctl restart {SERVICE_NAME}", check=False)
    run_cmd(ssh, f"sudo systemctl status {SERVICE_NAME} --no-pager -l", check=False)


def main():
    args = parse_args()
    local_base = Path(__file__).resolve().parent

    password = args.password
    if not password:
        password = getpass.getpass(f"SSH password for {args.user}@{args.host}: ")

    ssh = connect(args.host, args.user, password)
    project_dir = PROJECT_DIR.format(user=args.user)

    # always push code
    push_files(ssh, local_base, project_dir)

    if not args.code_only:
        create_data_dirs(ssh, project_dir)

        if not args.skip_deps:
            install_system_deps(ssh)
            setup_python_env(ssh, project_dir)
            setup_gpio_permissions(ssh, args.user)

        apply_pinmux_fix(ssh, project_dir)
        setup_systemd_service(ssh, project_dir, args.user)
        restart_service(ssh)

        print("\n" + "=" * 50)
        print("Setup complete!")
        print(f"  Project: {project_dir}")
        print(f"  Service: sudo systemctl status {SERVICE_NAME}")
        print(f"  Logs: sudo journalctl -u {SERVICE_NAME} -f")
        print(f"  Dashboard: http://{args.host}:8080")
        print("=" * 50)
    else:
        restart_service(ssh)
        print("\nCode pushed and service restarted.")

    ssh.close()


if __name__ == "__main__":
    main()
