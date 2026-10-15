#!/usr/bin/env python3
"""Camera troubleshooting for the Raspberry Pi.

Run ON THE PI from the project folder (stop the grower service first so it
isn't holding the cameras):

    sudo systemctl stop <your-grower-service>   # or Ctrl+C the running main.py
    python3 diagnose_cameras.py

It checks power, USB, which process (if any) holds the cameras, and tries to
grab a frame from every webcam, saving test images to data/images/diagnose/.
The end of the output prints the config.yaml lines to use.
"""
import glob
import os
import subprocess
from pathlib import Path


def run(cmd: str) -> str:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=15).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"(could not run: {e})"


def section(title: str):
    print("\n" + "=" * 60 + f"\n{title}\n" + "=" * 60)


section("1. Power (most common Pi webcam problem)")
throttled = run("vcgencmd get_throttled")
print(throttled or "(vcgencmd not available)")
if "=" in throttled:
    try:
        value = int(throttled.split("=")[1], 16)
        if value & 0x1:
            print("  !! UNDER-VOLTAGE RIGHT NOW -- use the official 5V/3A (Pi 4) or 5V/5A (Pi 5) supply,")
            print("     or put the webcams on a POWERED USB hub.")
        elif value & 0x10000:
            print("  !! Under-voltage has happened since boot -- webcams may have dropped off USB.")
            print("     Use the official supply or a powered USB hub.")
        else:
            print("  OK: no under-voltage recorded since boot.")
    except ValueError:
        pass

section("2. Recent kernel messages about USB / cameras / voltage")
print(run("dmesg 2>/dev/null | grep -iE 'uvc|usb .*(disconnect|new)|voltage|video|reset' | tail -25")
      or "(nothing, or run with sudo to read dmesg)")

section("3. USB devices")
print(run("lsusb") or "(lsusb not available)")
print(run("lsusb -t"))

section("4. Video device nodes")
print("by-id:   ", *sorted(glob.glob("/dev/v4l/by-id/*")) or ["none"], sep="\n  ")
print("by-path: ", *sorted(glob.glob("/dev/v4l/by-path/*")) or ["none"], sep="\n  ")
print("nodes:   ", " ".join(sorted(glob.glob("/dev/video*"))) or "none")
groups = run("id -nG")
print(f"\nuser groups: {groups}")
if "video" not in groups.split() and os.geteuid() != 0:
    print("  !! This user is not in the 'video' group: sudo usermod -aG video $USER  (then reboot)")

section("5. Is anything already using a camera?")
busy = run("fuser -v /dev/video* 2>&1")
print(busy or "nothing has the cameras open (good)")
if busy:
    print("  !! A running process holds a camera. Stop it (often a second copy of main.py")
    print("     or a systemd service) before capturing -- only one program can use a webcam.")

section("6. Grab a frame from every webcam")
try:
    import cv2
except ImportError:
    cv2 = None
    print("OpenCV not installed in this Python: pip install opencv-python-headless")

out_dir = Path("data/images/diagnose")
out_dir.mkdir(parents=True, exist_ok=True)
working = []
candidates = sorted(glob.glob("/dev/v4l/by-id/*-video-index0")) or sorted(glob.glob("/dev/video*"))
if cv2:
    for dev in candidates:
        real = os.path.realpath(dev)
        cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
        if not cap.isOpened():
            print(f"  {dev} -> {real}: cannot open")
            continue
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        ok = False
        frame = None
        for _ in range(15):
            ok, frame = cap.read()
        cap.release()
        if ok and frame is not None:
            name = out_dir / f"{Path(real).name}.jpg"
            cv2.imwrite(str(name), frame)
            h, w = frame.shape[:2]
            print(f"  {dev} -> {real}: OK {w}x{h}, saved {name}")
            working.append(dev)
        else:
            print(f"  {dev} -> {real}: opens but NO FRAME (metadata node, bandwidth, or power)")

section("7. Suggested config.yaml")
if len(working) >= 2:
    print("Open the saved images to see which camera is which, then set:\n")
    print("camera:")
    print(f'  plant_cam: "{working[0]}"      # swap these two if reversed')
    print(f'  dashboard_cam: "{working[1]}"')
    if not any("/by-id/" in w for w in working):
        print("\n(no /dev/v4l/by-id entries; use the /dev/v4l/by-path entries listed above instead)")
elif len(working) == 1:
    print("Only ONE webcam works. Check section 1 (power) and 2 (USB disconnects),")
    print("try a powered USB hub, and plug each camera directly into a blue USB 3 port.")
else:
    print("NO webcam works. Check power, cables, and section 5 (camera in use).")
