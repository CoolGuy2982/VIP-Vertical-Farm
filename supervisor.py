#!/usr/bin/env python3
"""Keeps the AI grower running and reports crashes to the website.

systemd runs this (see setup_device.py); it runs `python -m src.main`:

  exit 42           restart requested (website restart / code or config update)
  exit 0            normal stop; restarted unless we are stopping
  anything else     crash: report it to Firestore (live/status.supervisor),
                    roll back a code/config update that crashed within
                    ROLLBACK_WINDOW_S, then restart with backoff

Rollback info comes from data/update_state.json, written by
src/remote_control.py before it restarts:
  {"state": "restarting"|"verifying", "kind": "code"|"config", "command_id": ...,
   "rollback": {"type": "git", "commit": "abc123"}
             | {"type": "tar", "backup": "data/pre_git_backup.tar.gz"}
             | {"type": "files", "files": [{"path": "config.local.yaml", "backup": "data/...|null"}]}}

Deliberately self-contained (no `src` imports), so it still works when an
update has broken the grower code.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
RESTART_EXIT_CODE = 42
ROLLBACK_WINDOW_S = 300       # a crash this soon after an update rolls it back
STABLE_RUN_S = 600            # running this long resets the crash backoff
MAX_BACKOFF_S = 300
STOP_TIMEOUT_S = 45
UPDATE_STATE = BASE / "data" / "update_state.json"
CHILD_CMD = [sys.executable, "-m", "src.main"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str):
    print(f"{datetime.now().isoformat(timespec='seconds')} [supervisor] {msg}", flush=True)


# --------------------------------------------------------------------------- firestore

class Reporter:
    """Best-effort Firestore writes; never raises."""

    def __init__(self, base: Path):
        self.db = None
        self.device_id = "raspPi4"
        try:
            import yaml
            cfg = yaml.safe_load((base / "config.yaml").read_text(encoding="utf-8")) or {}
            local = base / "config.local.yaml"
            if local.exists():
                fb_local = (yaml.safe_load(local.read_text(encoding="utf-8")) or {}).get("firebase")
                if isinstance(fb_local, dict):
                    cfg.setdefault("firebase", {}).update(fb_local)
            fb = cfg.get("firebase", {}) or {}
            self.device_id = fb.get("device_id", self.device_id)
            if not fb.get("enabled", False):
                return
            import firebase_admin
            from firebase_admin import credentials, firestore
            cred = base / fb.get("credentials_path", "firebase-credentials.json")
            if not cred.exists() and os.environ.get("FIREBASE_CREDENTIALS"):
                cred = Path(os.environ["FIREBASE_CREDENTIALS"])
            app = firebase_admin.initialize_app(credentials.Certificate(str(cred)),
                                                name="supervisor")
            self.db = firestore.client(app)
        except Exception as e:
            log(f"Firestore reporting unavailable: {e}")

    def status(self, supervisor: dict):
        if not self.db:
            return
        try:
            self.db.document(f"grows/{self.device_id}/live/status").set(
                {"supervisor": supervisor}, merge=True)
        except Exception as e:
            log(f"could not write status: {e}")

    def command(self, cmd_id: str | None, update: dict):
        if not (self.db and cmd_id):
            return
        try:
            self.db.document(f"grows/{self.device_id}/commands/{cmd_id}").set(
                {**update, "updated_at": utc_now()}, merge=True)
        except Exception as e:
            log(f"could not update command {cmd_id}: {e}")


# --------------------------------------------------------------------------- rollback

def read_update_state(base: Path = BASE) -> dict | None:
    try:
        return json.loads((base / "data" / "update_state.json").read_text())
    except Exception:
        return None


def write_update_state(state: dict, base: Path = BASE):
    path = base / "data" / "update_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def rollback(state: dict, base: Path = BASE) -> str:
    """Undo the change described by state["rollback"]. Returns a description."""
    rb = state.get("rollback") or {}
    kind = rb.get("type")
    if kind is None and state.get("from_commit"):        # older update_state files
        kind, rb = "git", {"type": "git", "commit": state["from_commit"]}

    if kind == "git":
        r = subprocess.run(["git", "reset", "--hard", rb["commit"]], cwd=base,
                           capture_output=True, text=True, timeout=120)
        if r.returncode:
            raise RuntimeError(f"git reset failed: {r.stderr.strip()}")
        return f"rolled back to commit {rb['commit']}"

    if kind == "tar":
        backup = base / rb.get("backup", "data/pre_git_backup.tar.gz")
        if not backup.exists():
            raise RuntimeError(f"backup {backup} not found")
        r = subprocess.run(["tar", "-xzf", str(backup)], cwd=base,
                           capture_output=True, text=True, timeout=120)
        if r.returncode:
            raise RuntimeError(f"restoring backup failed: {r.stderr.strip()}")
        git_dir = base / ".git"
        if git_dir.exists():   # back to the pre-git state; keep the repo for debugging
            git_dir.rename(base / "data" / f"failed_git_{int(time.time())}")
        return "restored the pre-update copy of the code"

    if kind == "files":
        restored = []
        for f in rb.get("files", []):
            target = base / f["path"]
            if f.get("backup"):
                shutil.copy2(base / f["backup"], target)
                restored.append(f"{f['path']} restored")
            elif target.exists():
                target.unlink()        # the file didn't exist before the change
                restored.append(f"{f['path']} removed")
        return "; ".join(restored) or "nothing to restore"

    raise RuntimeError(f"no rollback information ({kind!r})")


# --------------------------------------------------------------------------- supervisor

class Supervisor:
    def __init__(self, base: Path = BASE, cmd: list[str] | None = None,
                 reporter: Reporter | None = None):
        self.base = base
        self.cmd = cmd or CHILD_CMD
        self.reporter = reporter if reporter is not None else Reporter(base)
        self.child: subprocess.Popen | None = None
        self.stopping = False
        self.tail: deque = deque(maxlen=80)
        self.restarts = 0
        self.crashes_in_a_row = 0

    def handle_signal(self, signum, frame):
        log(f"signal {signum}: stopping the grower")
        self.stopping = True
        if self.child and self.child.poll() is None:
            self.child.send_signal(signal.SIGTERM)

    def _pump_output(self, proc: subprocess.Popen):
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            self.tail.append(line.rstrip()[:400])

    def run_child(self, first: bool) -> tuple[int, float]:
        env = dict(os.environ, VFARM_SUPERVISED="1", PYTHONUNBUFFERED="1")
        if not first:
            env["VFARM_RESTART"] = "1"
        self.tail.clear()
        started = time.time()
        self.child = subprocess.Popen(self.cmd, cwd=self.base, env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, bufsize=1,
                                      encoding="utf-8", errors="replace")
        reader = threading.Thread(target=self._pump_output, args=(self.child,), daemon=True)
        reader.start()
        while True:
            try:
                code = self.child.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if self.stopping:
                    try:
                        code = self.child.wait(timeout=STOP_TIMEOUT_S)
                    except subprocess.TimeoutExpired:
                        log("grower did not stop in time; killing it")
                        self.child.kill()
                        code = self.child.wait()
                    break
        reader.join(timeout=5)
        return code, time.time() - started

    def on_crash(self, code: int, ran_s: float) -> float:
        """Report a crash, roll back a fresh update if needed. Returns delay before restart."""
        tail = list(self.tail)[-60:]
        log(f"grower crashed (exit code {code}) after {ran_s:.0f}s")
        self.crashes_in_a_row = 1 if ran_s > STABLE_RUN_S else self.crashes_in_a_row + 1

        state = read_update_state(self.base)
        rolled = None
        if state and state.get("state") in ("restarting", "verifying") and ran_s < ROLLBACK_WINDOW_S:
            what = "code update" if state.get("kind", "code") == "code" else "config change"
            try:
                rolled = rollback(state, self.base)
                log(f"{what} crashed the grower: {rolled}")
                state.update(state="rolled_back", finished_at=utc_now(), exit_code=code,
                             message=rolled, log_tail=tail[-40:])
                self.reporter.command(state.get("command_id"), {
                    "status": "error", "finished_at": utc_now(),
                    "error": f"The {what} crashed the grower (exit code {code}), "
                             f"so it was undone: {rolled}. Last log lines are in the result.",
                    "result": {"log_tail": tail[-40:], "rolled_back": True}})
            except Exception as e:
                log(f"ROLLBACK FAILED: {e}")
                state.update(state="rollback_failed", finished_at=utc_now(), exit_code=code,
                             message=str(e), log_tail=tail[-40:])
                self.reporter.command(state.get("command_id"), {
                    "status": "error", "finished_at": utc_now(),
                    "error": f"The {what} crashed the grower and the rollback failed: {e}",
                    "result": {"log_tail": tail[-40:], "rolled_back": False}})
            write_update_state(state, self.base)
            self.crashes_in_a_row = 0          # retry the old version straight away

        delay = 0 if rolled else min(MAX_BACKOFF_S, 5 * 2 ** max(0, self.crashes_in_a_row - 1))
        crash = {"at": utc_now(), "exit_code": code, "ran_s": int(ran_s), "log_tail": tail,
                 "rolled_back": rolled, "crashes_in_a_row": self.crashes_in_a_row}
        # last_crash stays in live/status after the grower is back up
        self.reporter.status({"state": "crashed", **crash, "restarts": self.restarts,
                              "restart_in_s": delay, "supervised": True, "last_crash": crash})
        return delay

    def loop(self):
        first = True
        while not self.stopping:
            self.reporter.status({"state": "starting", "at": utc_now(),
                                  "restarts": self.restarts, "supervised": True})
            code, ran_s = self.run_child(first)
            first = False
            if self.stopping:
                break
            self.restarts += 1
            if code == RESTART_EXIT_CODE:
                log("restart requested")
                self.crashes_in_a_row = 0
                continue
            if code == 0:
                log("grower exited normally; restarting in 5 s")
                delay = 5
            else:
                delay = self.on_crash(code, ran_s)
            for _ in range(int(delay)):
                if self.stopping:
                    break
                time.sleep(1)
        self.reporter.status({"state": "stopped", "at": utc_now(), "supervised": True})
        log("stopped")


def main():
    sup = Supervisor()
    signal.signal(signal.SIGTERM, sup.handle_signal)
    signal.signal(signal.SIGINT, sup.handle_signal)
    sup.loop()


if __name__ == "__main__":
    main()
