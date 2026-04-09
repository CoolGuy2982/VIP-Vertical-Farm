"""
Test all hardware functions on the Jetson Nano remotely.
Run this from your laptop while the Jetson is on the same network.

Usage:
    python test_device.py --host 192.168.1.50
    python test_device.py --host 192.168.1.50 --test cameras
    python test_device.py --host 192.168.1.50 --test relays
    python test_device.py --test gemini
    python test_device.py --test all
"""

import argparse
import getpass
import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("Installing paramiko...")
    os.system(f"{sys.executable} -m pip install paramiko")
    import paramiko


PROJECT_DIR = "/home/{user}/VIP-Vertical-Farm"


def parse_args():
    parser = argparse.ArgumentParser(description="Test Jetson Nano hardware remotely")
    parser.add_argument("--host", required=True, help="Jetson hostname or IP address")
    parser.add_argument("--user", default="jetson", help="SSH username (default: jetson)")
    parser.add_argument("--password", default=None, help="SSH password")
    parser.add_argument("--test", default="all",
                        choices=["all", "cameras", "relays", "gemini", "api", "firebase"],
                        help="Which test to run")
    return parser.parse_args()


def connect(host, user, password):
    print(f"\nConnecting to {user}@{host}...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(host, username=user, password=password, timeout=10)
        print(f"Connected!\n")
        return ssh
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)


def run_device_script(ssh, project_dir, script, timeout=30, use_sudo=False):
    """Run a Python script on the Jetson inside the venv."""
    python = f"{project_dir}/venv/bin/python3"
    if use_sudo:
        cmd = f"cd {project_dir} && sudo {python} -c \"{script}\""
    else:
        cmd = f"cd {project_dir} && {python} -c \"{script}\""
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    exit_code = stdout.channel.recv_exit_status()
    return out, err, exit_code


def download_file(ssh, remote_path, local_path):
    sftp = ssh.open_sftp()
    try:
        sftp.get(remote_path, str(local_path))
        return True
    except Exception:
        return False
    finally:
        sftp.close()


def test_cameras(ssh, project_dir):
    print("=" * 50)
    print("TEST: Cameras")
    print("=" * 50)

    script = """
import cv2
import json
results = {}
for i in range(4):
    cap = cv2.VideoCapture(i)
    if cap.isOpened():
        ret, frame = cap.read()
        if ret:
            path = f'{project_dir}/test_cam_{i}.jpg'
            cv2.imwrite(path, frame)
            h, w = frame.shape[:2]
            results[i] = {'status': 'ok', 'resolution': f'{w}x{h}', 'path': path}
        else:
            results[i] = {'status': 'no_frame'}
        cap.release()
    else:
        results[i] = {'status': 'not_found'}
print(json.dumps(results))
""".replace("{project_dir}", project_dir).replace("\n", "; ").strip("; ")

    out, err, code = run_device_script(ssh, project_dir, script)

    if code != 0:
        print(f"  FAILED: {err}")
        return False

    try:
        import json
        results = json.loads(out)
    except Exception:
        print(f"  Output: {out}")
        print(f"  Error: {err}")
        return False

    found = 0
    local_dir = Path(__file__).parent / "test_results"
    local_dir.mkdir(exist_ok=True)

    for idx, info in results.items():
        status = info.get("status")
        if status == "ok":
            print(f"  Camera {idx}: OK ({info['resolution']})")
            local_path = local_dir / f"test_cam_{idx}.jpg"
            if download_file(ssh, info["path"], local_path):
                print(f"    Downloaded to {local_path}")
                print(f"    Open it to see which camera this is (plant vs dashboard)")
            found += 1
        elif status == "not_found":
            print(f"  Camera {idx}: not connected")
        else:
            print(f"  Camera {idx}: {status}")

    if found >= 2:
        print(f"\n  Found {found} cameras. Check the test images to identify plant vs dashboard.")
        print(f"  Then update config.yaml with the right indexes.")
        return True
    elif found == 1:
        print(f"\n  Only 1 camera found. Need 2 (plant + dashboard). Check USB connections.")
        return False
    else:
        print(f"\n  No cameras found! Check USB connections.")
        return False


def _run_relay_test(ssh, project_dir, name, pin):
    """Pulse one relay pin and print full output + any errors."""
    script = (
        f"import Jetson.GPIO as GPIO; import time; "
        f"GPIO.setmode(GPIO.BOARD); GPIO.setwarnings(False); "
        f"GPIO.setup({pin}, GPIO.OUT); GPIO.output({pin}, GPIO.HIGH); "
        f"print('pin {pin} set HIGH (relay OFF)'); time.sleep(0.2); "
        f"print('{name} relay ON'); GPIO.output({pin}, GPIO.LOW); time.sleep(3); "
        f"print('{name} relay OFF'); GPIO.output({pin}, GPIO.HIGH); "
        f"GPIO.cleanup({pin}); print('done')"
    )
    out, err, code = run_device_script(ssh, project_dir, script, timeout=15, use_sudo=True)
    print(f"  exit code : {code}")
    if out:
        for line in out.split("\n"):
            print(f"  out: {line}")
    if err:
        for line in err.split("\n"):
            print(f"  ERR: {line}")
    return code == 0


def test_relays(ssh, project_dir):
    print("=" * 50)
    print("TEST: Relay modules (pump + lights)")
    print("  Using BOARD pin numbering, active-low logic (LOW = ON)")
    print("  Running with sudo so GPIO access is guaranteed")
    print("=" * 50)

    for name, pin in [("PUMP", 11), ("LIGHT", 7), ("DASHBOARD", 22)]:
        input(f"\n  Press Enter to pulse {name} relay (BOARD pin {pin}) for 3s...")
        print(f"  >>> Watch/listen for the relay click NOW <<<")
        ok = _run_relay_test(ssh, project_dir, name, pin)
        if ok:
            print(f"  {name}: script ran OK")
        else:
            print(f"  {name}: FAILED (see ERR above)")

    print("\n  Troubleshooting:")
    print("    No click at all   → check wiring: BOARD pin 7 (light), 11 (pump)")
    print("                        VCC → Jetson pin 1 (3.3V), GND → pin 6")
    print("    Click but no load → check NO terminal on relay, check load power supply")
    print("    Permission error  → sudo usermod -aG gpio $USER  then reboot")

    return True


def test_relays_inverted(ssh, project_dir):
    """Test if relays are active-low (inverted)."""
    print("\n  Testing if relays are INVERTED (active-low) on pump pin (BOARD 11)...")
    script = (
        "import Jetson.GPIO as GPIO; import time; "
        "GPIO.setmode(GPIO.BOARD); GPIO.setwarnings(False); "
        "GPIO.setup(11, GPIO.OUT); GPIO.output(11, GPIO.HIGH); time.sleep(0.2); "
        "print('trying LOW'); GPIO.output(11, GPIO.LOW); time.sleep(2); "
        "print('back to HIGH'); GPIO.output(11, GPIO.HIGH); "
        "GPIO.cleanup(11); print('done')"
    )
    out, err, code = run_device_script(ssh, project_dir, script, timeout=15, use_sudo=True)
    if out:
        for line in out.split("\n"):
            print(f"    {line}")
    if err:
        for line in err.split("\n"):
            print(f"    ERR: {line}")
    print("\n  Did the relay click when 'trying LOW' printed?")
    print("  If yes: relay is active-low (correct, matches actuators.py).")


def test_gemini(ssh, project_dir):
    print("=" * 50)
    print("TEST: Gemini API connection")
    print("=" * 50)

    script = """
import os; from dotenv import load_dotenv; load_dotenv()
from google import genai
key = os.environ.get('GEMINI_API_KEY', '')
if not key: print('ERROR: No GEMINI_API_KEY in .env'); exit(1)
print(f'API key loaded ({len(key)} chars)')
client = genai.Client(api_key=key)
r = client.interactions.create(model='gemini-3-flash-preview', input='Say hello in exactly 5 words')
print(f'Response: {r.outputs[-1].text}')
print('Gemini connection OK')
"""
    out, err, code = run_device_script(ssh, project_dir, script.replace("\n", "; ").strip("; "), timeout=30)
    for line in out.split("\n"):
        print(f"  {line}")
    if code != 0:
        print(f"  FAILED: {err}")
        return False
    return True


def test_api(ssh, project_dir):
    print("=" * 50)
    print("TEST: API server health check")
    print("=" * 50)

    script = """
import urllib.request, json
try:
    r = urllib.request.urlopen('http://localhost:8080/api/health', timeout=5)
    data = json.loads(r.read())
    print(f'API response: {data}')
    print('API server OK')
except Exception as e:
    print(f'API not reachable: {e}')
    print('Is the service running? Check: sudo systemctl status ai-grower')
"""
    out, err, code = run_device_script(ssh, project_dir, script.replace("\n", "; ").strip("; "))
    for line in out.split("\n"):
        print(f"  {line}")
    return code == 0


def test_firebase(ssh, project_dir):
    print("=" * 50)
    print("TEST: Firebase connection")
    print("=" * 50)

    script = """
import json, os
cred_path = os.path.join('{project_dir}', 'firebase-credentials.json')
if not os.path.exists(cred_path):
    print('ERROR: firebase-credentials.json not found'); exit(1)
print('Credentials file found')
import firebase_admin
from firebase_admin import credentials, firestore
cred = credentials.Certificate(cred_path)
import yaml
with open(os.path.join('{project_dir}', 'config.yaml')) as f:
    config = yaml.safe_load(f)
bucket = config.get('firebase', {}).get('storage_bucket', '')
firebase_admin.initialize_app(cred, {'storageBucket': bucket})
db = firestore.client()
db.collection('test').document('ping').set({'status': 'ok'})
print(f'Firestore write OK (bucket: {bucket})')
db.collection('test').document('ping').delete()
print('Firebase connection OK')
""".replace("{project_dir}", project_dir)

    out, err, code = run_device_script(ssh, project_dir, script.replace("\n", "; ").strip("; "), timeout=30)
    for line in out.split("\n"):
        print(f"  {line}")
    if code != 0:
        print(f"  FAILED: {err}")
        return False
    return True


def main():
    args = parse_args()
    project_dir = PROJECT_DIR.format(user=args.user)

    password = args.password
    if not password:
        password = getpass.getpass(f"SSH password for {args.user}@{args.host}: ")

    ssh = connect(args.host, args.user, password)

    tests = {
        "cameras": test_cameras,
        "relays": test_relays,
        "gemini": test_gemini,
        "api": test_api,
        "firebase": test_firebase,
    }

    results = {}

    if args.test == "all":
        for name, func in tests.items():
            try:
                results[name] = func(ssh, project_dir)
            except Exception as e:
                print(f"  ERROR: {e}")
                results[name] = False
            print()
    else:
        func = tests[args.test]
        try:
            results[args.test] = func(ssh, project_dir)
        except Exception as e:
            print(f"  ERROR: {e}")
            results[args.test] = False

    print("\n" + "=" * 50)
    print("RESULTS")
    print("=" * 50)
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {name}: {status}")

    ssh.close()


if __name__ == "__main__":
    main()
