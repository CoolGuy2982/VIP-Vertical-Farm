"""Website <-> Pi link through Firebase.

The website runs in Google Cloud and cannot reach the Pi directly, so both
sides meet in Firestore under grows/<device_id>/:

  live/status            heartbeat every ~15 s: health, problems, errors, AI
                         activity, lights/pump, cameras, host stats, version
  live/logs              the last few hundred log lines
  live/frame_plant       latest live-view JPEG (only while someone is watching)
  live/frame_dashboard
  commands/<id>          written by the website: {type, args, status: "pending"}
                         -> the Pi runs it and sets status running/done/error

Nothing here is required for growing: if Firebase is off, this module idles.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

RESTART_EXIT_CODE = 42          # supervisor.py restarts the grower on this code
HEARTBEAT_SECONDS = 15
FIRESTORE_TIMEOUT = 20          # seconds; a stuck Firestore call must not freeze the link
STUCK_SECONDS = 180             # heartbeat silent this long -> restart the grower (watchdog)
WATCHDOG_MIN_GAP_SECONDS = 3600 # at most one watchdog restart per hour
VERSION_CACHE_SECONDS = 300     # git is run at most this often for the status
COMMAND_MAX_AGE_SECONDS = 600   # ignore commands queued >10 min ago (Pi was offline)
LIVE_DEFAULT_SECONDS = 300
LIVE_MAX_SECONDS = 1800
LIVE_WIDTH = 640
VERIFY_SECONDS = 60             # how long new code must stay up to count as healthy
DEFAULT_REPO = "https://github.com/CoolGuy2982/VIP-Vertical-Farm.git"

LOGGER_LABELS = {
    "src.camera": "Camera",
    "src.ai_grower": "AI grower",
    "src.gemini_client": "Gemini AI",
    "src.actuators": "Pump / lights",
    "src.firebase_sync": "Cloud sync",
    "src.remote_control": "Remote control",
    "src.action_scheduler": "Scheduler",
    "src.context_manager": "AI memory",
    "__main__": "Startup",
    "src.main": "Startup",
}


def friendly_error(msg: str) -> str:
    """Short, plain explanation for errors the team sees often."""
    m = str(msg)
    low = m.lower()
    if "503" in m and ("unavailable" in low or "high demand" in low or "overloaded" in low):
        return ("Gemini is overloaded right now (Google 503, not a bug here). "
                "The AI retries automatically; nothing to do.")
    if "429" in m or "resource_exhausted" in low or "too many requests" in low:
        return "Gemini rate limit reached (429). The AI waits and retries automatically."
    return m


# AI-side errors that a later successful check-in makes obsolete
AI_LOG_SOURCES = {"src.ai_grower", "src.gemini_client"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- logs

class LogBuffer(logging.Handler):
    """Keeps recent log lines in memory so the website can show them."""

    def __init__(self, max_lines: int = 400):
        super().__init__(level=logging.INFO)
        self.lines: deque = deque(maxlen=max_lines)
        self.problems: deque = deque(maxlen=60)   # WARNING and above
        self.seq = 0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord):
        try:
            msg = record.getMessage()
            if record.exc_info and record.levelno >= logging.ERROR:
                exc = record.exc_info[1]
                msg = f"{msg} ({type(exc).__name__}: {exc})"
            entry = {
                "t": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds"),
                "epoch": record.created,
                "level": record.levelname,
                "source": record.name,
                "msg": msg[:600],
            }
            with self._lock:
                self.lines.append(entry)
                self.seq += 1
                if record.levelno >= logging.WARNING:
                    self.problems.append(entry)
        except Exception:  # never let logging crash the app
            pass

    def snapshot(self, n: int | None = None) -> list[dict]:
        with self._lock:
            items = list(self.lines)
        return items[-n:] if n else items

    def recent_problems(self, n: int = 25) -> list[dict]:
        with self._lock:
            return list(self.problems)[-n:]


# --------------------------------------------------------------------------- config

def deep_merge(base: dict, override: dict) -> dict:
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(base_dir: str | Path) -> dict:
    """config.yaml plus website-made changes saved in config.local.yaml."""
    base = Path(base_dir)
    with open(base / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    local = base / "config.local.yaml"
    if local.exists():
        with open(local, encoding="utf-8") as f:
            deep_merge(config, yaml.safe_load(f) or {})
    return config


def validate_config(cfg) -> list[str]:
    """Problems that would stop the grower from starting (or make it unsafe)."""
    errors = []
    if not isinstance(cfg, dict):
        return ["config must be a YAML mapping (key: value)"]
    for section in ("plant", "water_pump", "camera", "gemini", "scheduler", "firebase"):
        if not isinstance(cfg.get(section), dict):
            errors.append(f"missing section '{section}:'")
    plant = cfg.get("plant") or {}
    try:
        datetime.strptime(str(plant.get("planted_date")), "%Y-%m-%d")
    except ValueError:
        errors.append("plant.planted_date must be YYYY-MM-DD")
    stages = plant.get("growth_stages")
    if not isinstance(stages, list) or not stages:
        errors.append("plant.growth_stages must be a non-empty list")
    else:
        for i, st in enumerate(stages):
            days = st.get("typical_days") if isinstance(st, dict) else None
            if not (isinstance(days, list) and len(days) == 2):
                errors.append(f"plant.growth_stages[{i}].typical_days must be [start, end]")

    def num(path, lo, hi):
        node = cfg
        for k in path.split("."):
            node = node.get(k) if isinstance(node, dict) else None
        if node is None:
            return
        if not isinstance(node, (int, float)) or isinstance(node, bool) or not lo <= node <= hi:
            errors.append(f"{path} must be a number from {lo} to {hi}")

    num("water_pump.min_seconds_per_dose", 0, 60)
    num("water_pump.max_seconds_per_dose", 1, 600)
    num("water_pump.budget.ai_max_seconds_per_day", 0, 100000)
    num("water_pump.budget.ai_min_minutes_between_doses", 0, 100000)
    num("water_pump.budget.manual_max_seconds_per_day", 0, 100000)
    num("scheduler.min_checkin_minutes", 1, 1440)
    num("scheduler.max_checkin_minutes", 1, 1440)
    num("hardware_specs.flow_rate_ml_s", 1, 10000)
    rot = (cfg.get("camera") or {}).get("plant_rotation")
    if rot is not None and rot not in (0, 90, 180, 270):
        errors.append("camera.plant_rotation must be 0, 90, 180 or 270")
    return errors


class _TimedRef:
    """Firestore document ref whose writes give up after FIRESTORE_TIMEOUT."""

    def __init__(self, ref):
        self._ref = ref

    def set(self, data, merge=False):
        return self._ref.set(data, merge=merge, timeout=FIRESTORE_TIMEOUT)


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


# --------------------------------------------------------------------------- main class

class RemoteControl:
    COMMANDS = {
        "lights_on", "lights_off", "pump", "checkin", "schedule_checkin",
        "pause_ai", "resume_ai", "cancel_action", "capture", "live_start",
        "live_stop", "set_cameras", "swap_cameras", "set_plant",
        "set_schedule", "get_logs", "restart", "reboot", "update_code",
        "ping", "reset_ai_memory", "get_ai_memory", "get_config", "set_config",
        "get_prompt", "set_prompt",
    }

    def __init__(self, grower, config: dict, base_dir: str, log_buffer: LogBuffer | None):
        self.grower = grower
        self.config = config
        self.base = Path(base_dir)
        self.logs = log_buffer or LogBuffer()
        self.fb = grower.firebase
        self.enabled = bool(getattr(self.fb, "enabled", False))
        self.device_id = getattr(self.fb, "device_id", config.get("firebase", {}).get("device_id", "raspPi4"))
        self.started_at = utc_now()
        self.started_epoch = time.time()
        self._running = False
        self._seen: set[str] = set()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="remote-cmd")
        self._status_lock = threading.Lock()
        self._last_status_write = 0.0
        self._last_logs_seq = -1
        self._watch = None
        self._throttled_cache = (0.0, None)
        self.activity: deque = deque(maxlen=40)
        self._heartbeat_tick = time.time()
        self._link_down_since: float | None = None
        self._last_link_warning = 0.0
        self._version_cache: tuple[float, dict | None] = (0.0, None)

        self.live_until = 0.0
        self.live_cams: list[str] = ["plant", "dashboard"]
        self.live_fps = float(config.get("remote", {}).get("live_fps", 1.0))
        self.live_seq = 0
        self._live_event = threading.Event()

        self.update_state_path = self.base / "data" / "update_state.json"
        self.last_good_path = self.base / "data" / "last_good_commit"
        self.repo_url = config.get("remote", {}).get("github_repo", DEFAULT_REPO)

    # ------------------------------------------------------------ firestore refs

    def _doc(self, path: str):
        return _TimedRef(self.fb.db.document(f"grows/{self.device_id}/{path}"))

    def _pending_commands(self):
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter
            return self._commands().where(filter=FieldFilter("status", "==", "pending"))
        except ImportError:
            return self._commands().where("status", "==", "pending")

    def _commands(self):
        return self.fb.db.collection(f"grows/{self.device_id}/commands")

    # ------------------------------------------------------------ lifecycle

    def start(self):
        if not self.enabled:
            logger.info("Remote control disabled (Firebase not connected)")
            return
        if self._running:
            return
        self._running = True
        self._write_status(force=True, extra={"supervisor": {
            "state": "running", "at": utc_now(),
            "supervised": os.environ.get("VFARM_SUPERVISED") == "1"}})
        for target, name in ((self._heartbeat_loop, "remote-heartbeat"),
                             (self._watchdog_loop, "remote-watchdog"),
                             (self._poll_loop, "remote-poll"),
                             (self._live_loop, "remote-live"),
                             (self._verify_startup, "remote-verify")):
            threading.Thread(target=target, name=name, daemon=True).start()
        self._subscribe()
        logger.info("Remote control online (device=%s)", self.device_id)

    def stop(self):
        self._running = False
        self.live_until = 0
        self._live_event.set()
        try:
            if self._watch:
                self._watch.unsubscribe()
        except Exception:
            pass

    # ------------------------------------------------------------ AI activity feed

    def record_activity(self, kind: str, text: str, **data):
        """Called by the AI grower for every step so the website can show it live."""
        event = {"t": utc_now(), "kind": kind, "text": str(text)[:400]}
        for k, v in data.items():
            if v is not None:
                event[k] = v if isinstance(v, (int, float, bool)) else str(v)[:400]
        self.activity.append(event)
        # push soon, but no more than once every 2 s
        if self.enabled and self._running and time.time() - self._last_status_write > 2:
            threading.Thread(target=self._write_status, daemon=True).start()

    def publish_ai_context(self, turn: dict):
        """Exactly what the AI is about to receive (system prompt + context message)."""
        if not (self.enabled and self._running):
            return
        doc = dict(turn)
        doc["user_message"] = str(doc.get("user_message", ""))[:300_000]
        doc["system_prompt"] = str(doc.get("system_prompt", ""))[:200_000]
        doc["published_at"] = utc_now()
        try:
            self._doc("live/ai_context").set(doc)
        except Exception as e:
            logger.debug("could not publish AI context: %s", e)

    # ------------------------------------------------------------ status

    def _heartbeat_loop(self):
        while self._running:
            self._heartbeat_tick = time.time()
            try:
                self._write_status()
                if self.logs.seq != self._last_logs_seq:
                    self._write_logs()
                if self._link_down_since:
                    logger.info("Website link restored after %.0f s",
                                time.time() - self._link_down_since)
                    self._link_down_since = None
            except Exception as e:
                # Visible in the log (rate-limited), so a dead link is never silent
                now = time.time()
                self._link_down_since = self._link_down_since or now
                if now - self._last_link_warning > 300:
                    self._last_link_warning = now
                    logger.warning("Website link: can't write status to Firestore: %s: %s",
                                   type(e).__name__, str(e)[:300])
            for _ in range(HEARTBEAT_SECONDS):
                if not self._running:
                    return
                time.sleep(1)

    def _watchdog_loop(self):
        """Restart the grower if the heartbeat thread gets stuck (e.g. a hung network call).

        Plain errors (internet down) don't count: those return and are retried.
        """
        while self._running:
            time.sleep(15)
            stuck = time.time() - self._heartbeat_tick
            if stuck < STUCK_SECONDS:
                continue
            g = self.grower
            if getattr(g, "_checkin_lock", False) or g.actuators.get_status().get("pump_running"):
                continue    # don't interrupt watering or a check-in; try again shortly
            marker = self.base / "data" / "watchdog_last_restart"
            try:
                last = float(marker.read_text())
            except Exception:
                last = 0.0
            if time.time() - last < WATCHDOG_MIN_GAP_SECONDS:
                continue
            logger.error("Website link stuck for %.0f s (heartbeat not running); restarting the grower",
                         stuck)
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(str(time.time()))
            except Exception:
                pass
            self.request_restart()
            return

    def _write_logs(self):
        self._last_logs_seq = self.logs.seq
        self._doc("live/logs").set({"updated_at": utc_now(), "lines": self.logs.snapshot(300)})

    def _write_status(self, force: bool = False, extra: dict | None = None):
        if not self.enabled:
            return
        with self._status_lock:
            status = self.build_status()
            if extra:
                status.update(extra)
            # Plain JSON only: one odd value (e.g. an SDK type in the AI's sensor
            # arguments) would make Firestore reject every heartbeat
            status = json.loads(json.dumps(status, default=str))
            self._doc("live/status").set(status, merge=True)
            self._last_status_write = time.time()

    def build_status(self) -> dict:
        g = self.grower
        sched = g.scheduler
        cam = g.camera
        problems = self._problems()
        state = "error" if any(p["severity"] == "error" for p in problems) else (
            "warning" if problems else "ok")
        pending = []
        try:
            pending = sched.get_pending()[:20]
        except Exception:
            pass
        act = g.actuators.get_status()
        act["light_off_at"] = getattr(g.actuators, "_light_off_at", None)
        return {
            "heartbeat_at": utc_now(),
            "heartbeat_epoch": time.time(),
            "health": state,
            "problems": problems,
            "recent_errors": self.logs.recent_problems(25),
            "activity": list(self.activity),
            "grower": {
                "started_at": self.started_at,
                "uptime_s": int(time.time() - self.started_epoch),
                "paused": bool(getattr(g, "paused", False)),
                "checkin_running": bool(getattr(g, "_checkin_lock", False)),
                "last_checkin": getattr(g, "last_checkin", None),
                "next_checkin": sched.get_next_checkin(),
                "minutes_until_checkin": sched.get_minutes_until_checkin(),
                "pending_actions": pending,
                "day": g.context.get_days_since_planting(),
                "stage": g.context.get_current_growth_stage().get("name", "unknown"),
                "sensors": {k: v for k, v in g.sensors.read_all().items()
                            if k not in ("note",)},
            },
            "actuators": act,
            "cameras": {
                "plant": dict(cam.health.get("plant", {})),
                "dashboard": dict(cam.health.get("dashboard", {})),
                "plant_device": str(cam.plant_device),
                "dashboard_device": str(cam.dashboard_device),
                "plant_rotation": cam.plant_rotation,
                "available": self._list_cameras(),
            },
            "live": {
                "active": self.live_until > time.time(),
                "until_epoch": self.live_until,
                "cameras": self.live_cams,
                "fps": self.live_fps,
            },
            "settings": {
                "plant": {k: self.config.get("plant", {}).get(k)
                          for k in ("species", "variety", "planted_date")},
                "scheduler": {
                    "min_checkin_minutes": sched.min_checkin_minutes,
                    "max_checkin_minutes": sched.max_checkin_minutes,
                },
            },
            "host": self._host_stats(),
            "version": self._version(),
            "update": self._read_update_state(),
        }

    def _problems(self) -> list[dict]:
        """Turn raw state into plain-language problems for the website banner."""
        out = []
        cam = self.grower.camera
        for name in ("plant", "dashboard"):
            h = cam.health.get(name, {})
            if h.get("consecutive_failures"):
                out.append({
                    "id": f"camera_{name}", "severity": "error",
                    "title": f"{name.capitalize()} camera is not returning images",
                    "detail": h.get("last_error") or "capture failed",
                    "since": h.get("last_error_at"),
                })
        host = self._host_stats()
        if host.get("undervoltage_now"):
            out.append({"id": "power", "severity": "error",
                        "title": "Pi power supply is too weak (under-voltage)",
                        "detail": "USB cameras can drop out. Use the official power supply or a powered USB hub."})
        elif host.get("undervoltage_since_boot"):
            out.append({"id": "power", "severity": "warning",
                        "title": "Under-voltage happened since the Pi booted",
                        "detail": "Cameras may have disconnected. Check the power supply / USB hub."})
        if host.get("disk_free_gb") is not None and host["disk_free_gb"] < 1:
            out.append({"id": "disk", "severity": "error",
                        "title": "SD card almost full",
                        "detail": f"{host['disk_free_gb']} GB free"})
        if (host.get("cpu_temp_c") or 0) >= 80:
            out.append({"id": "temp", "severity": "warning",
                        "title": "Pi is overheating", "detail": f"CPU {host['cpu_temp_c']} °C"})
        if getattr(self.grower, "paused", False):
            out.append({"id": "paused", "severity": "warning",
                        "title": "AI grower is paused",
                        "detail": "No automatic check-ins, watering or lighting until resumed."})
        last = getattr(self.grower, "last_checkin", None) or {}
        if last.get("ok") is False:
            out.append({"id": "checkin", "severity": "error",
                        "title": "Last AI check-in failed",
                        "detail": friendly_error(last.get("error", "")), "since": last.get("at")})
        ok_since = 0.0
        if last.get("ok"):
            try:
                ok_since = datetime.fromisoformat(last["at"]).timestamp()
            except Exception:
                pass
        # Recent ERROR log lines grouped by component (last 30 min)
        cutoff = time.time() - 1800
        seen = {p["id"] for p in out}
        grouped: dict[str, dict] = {}
        for e in self.logs.recent_problems(60):
            if e["level"] not in ("ERROR", "CRITICAL") or e["epoch"] < cutoff:
                continue
            # A check-in that worked since then means the AI problem is over
            ai_error = e["source"] in AI_LOG_SOURCES or (
                e["source"] == "src.remote_control" and "command checkin" in e["msg"])
            if ai_error and e["epoch"] < ok_since:
                continue
            g = grouped.setdefault(e["source"], {"count": 0})
            g["count"] += 1
            g["last"] = e
        for source, g in grouped.items():
            label = LOGGER_LABELS.get(source, source)
            pid = f"log_{source}"
            if source == "src.camera" and any(i.startswith("camera_") for i in seen):
                continue
            out.append({
                "id": pid, "severity": "error",
                "title": f"{label} error" + (f" ({g['count']}× in 30 min)" if g["count"] > 1 else ""),
                "detail": friendly_error(g["last"]["msg"]), "since": g["last"]["t"],
            })
        return out

    # ------------------------------------------------------------ host info

    def _host_stats(self) -> dict:
        stats: dict = {"platform": platform.platform(terse=True), "python": platform.python_version()}
        try:
            stats["cpu_temp_c"] = round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
        except Exception:
            stats["cpu_temp_c"] = None
        try:
            du = shutil.disk_usage(self.base)
            stats["disk_free_gb"] = round(du.free / 1e9, 1)
            stats["disk_used_pct"] = round(100 * du.used / du.total)
        except Exception:
            pass
        try:
            mem = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0])
            stats["mem_used_pct"] = round(100 * (1 - mem["MemAvailable"] / mem["MemTotal"]))
        except Exception:
            pass
        try:
            stats["load_1m"] = round(os.getloadavg()[0], 2)
        except Exception:
            pass
        try:
            stats["system_uptime_s"] = int(float(Path("/proc/uptime").read_text().split()[0]))
        except Exception:
            pass
        stats.update(self._throttled())
        return stats

    def _throttled(self) -> dict:
        ts, cached = self._throttled_cache
        if cached is not None and time.time() - ts < 60:
            return cached
        result: dict = {}
        try:
            out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                                 text=True, timeout=5).stdout.strip()
            value = int(out.split("=")[1], 16)
            result = {"throttled_raw": out,
                      "undervoltage_now": bool(value & 0x1),
                      "undervoltage_since_boot": bool(value & 0x10000)}
        except Exception:
            pass
        self._throttled_cache = (time.time(), result)
        return result

    @staticmethod
    def _list_cameras() -> list[str]:
        import glob
        return sorted(glob.glob("/dev/v4l/by-id/*-video-index0")) or sorted(glob.glob("/dev/video[0-9]"))

    # ------------------------------------------------------------ git helpers

    def _git(self, *args, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.base, capture_output=True,
                              text=True, timeout=timeout)

    def _is_git_repo(self) -> bool:
        try:
            return self._git("rev-parse", "--is-inside-work-tree").stdout.strip() == "true" \
                and (self.base / ".git").exists()
        except Exception:
            return False

    def _version(self, fresh: bool = False) -> dict:
        # git spawns several processes; the status asks every 15 s, so cache it
        ts, cached = self._version_cache
        if cached is not None and not fresh and time.time() - ts < VERSION_CACHE_SECONDS:
            return cached
        value = self._read_version()
        self._version_cache = (time.time(), value)
        return value

    def _read_version(self) -> dict:
        if not self._is_git_repo():
            return {"git": False, "note": "Code was copied to the Pi without git; first "
                    "'Update from GitHub' will set git up."}
        try:
            return {
                "git": True,
                "commit": self._git("rev-parse", "--short", "HEAD").stdout.strip(),
                "branch": self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
                "message": self._git("log", "-1", "--format=%s").stdout.strip()[:200],
                "date": self._git("log", "-1", "--format=%cI").stdout.strip(),
                "local_changes": [l[3:] for l in self._git(
                    "status", "--porcelain", "--untracked-files=no").stdout.splitlines()][:20],
            }
        except Exception as e:
            return {"git": True, "error": str(e)}

    def _read_update_state(self) -> dict | None:
        try:
            return json.loads(self.update_state_path.read_text())
        except Exception:
            return None

    def _write_update_state(self, state: dict):
        self.update_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.update_state_path.write_text(json.dumps(state, indent=2))

    # ------------------------------------------------------------ startup verification

    def _verify_startup(self):
        """After a code update, report whether the new code stays up."""
        state = self._read_update_state()
        pending = state and state.get("state") == "restarting"
        if pending:
            state.update(state="verifying", verifying_since=utc_now())
            self._write_update_state(state)
            what = (f"New code {state.get('to_commit')}" if state.get("kind", "code") == "code"
                    else "New config")
            self._set_command(state.get("command_id"), "running",
                              progress=f"{what} started. "
                                       f"Checking it stays healthy for {VERIFY_SECONDS}s…")
            self._write_status(force=True)
        time.sleep(VERIFY_SECONDS)
        if not self._running:
            return
        commit = self._version(fresh=True).get("commit")
        if commit:
            try:
                self.last_good_path.write_text(commit)
            except Exception:
                pass
        if pending:
            errors = [e for e in self.logs.recent_problems(40)
                      if e["level"] in ("ERROR", "CRITICAL") and e["epoch"] > self.started_epoch]
            state.update(state="success", finished_at=utc_now(),
                         message=f"Running {commit} for {VERIFY_SECONDS}s without crashing",
                         errors_after_start=errors[-10:])
            self._write_update_state(state)
            done = (f"Update OK: now running {commit}." if state.get("kind", "code") == "code"
                    else "Config change OK: grower restarted and stayed up.")
            note = (done
                    + (f" {len(errors)} error(s) logged since start — check below." if errors else ""))
            self._set_command(state.get("command_id"), "done", result={
                "message": note, "from": state.get("from_commit"), "to": commit,
                "commits": state.get("commits", []), "errors_after_start": errors[-10:]})
            self._write_status(force=True)

    # ------------------------------------------------------------ command intake

    def _subscribe(self):
        try:
            query = self._pending_commands()

            def on_snapshot(docs, changes, read_time):
                for d in docs:
                    self._accept(d)

            self._watch = query.on_snapshot(on_snapshot)
        except Exception as e:
            logger.warning("Live command listener unavailable (%s); polling every 20s", e)
            self._watch = None

    def _poll_loop(self):
        """Safety net in case the live listener drops."""
        while self._running:
            interval = 20 if self._watch is None else 60
            for _ in range(interval):
                if not self._running:
                    return
                time.sleep(1)
            try:
                for d in self._pending_commands().limit(20).get(timeout=FIRESTORE_TIMEOUT):
                    self._accept(d)
            except Exception as e:
                logger.warning("command poll failed: %s: %s", type(e).__name__, str(e)[:200])

    def _accept(self, doc):
        if doc.id in self._seen:
            return
        self._seen.add(doc.id)
        data = doc.to_dict() or {}
        self._pool.submit(self._run_command, doc.id, data)

    def _set_command(self, cmd_id: str | None, status: str, result=None, error=None,
                     progress=None):
        if not cmd_id or not self.enabled:
            return
        update = {"status": status, "updated_at": utc_now()}
        if status == "running" and progress is None:
            update["started_at"] = utc_now()
        if status in ("done", "error", "expired"):
            update["finished_at"] = utc_now()
        if result is not None:
            update["result"] = result
        if error is not None:
            update["error"] = str(error)[:2000]
        if progress is not None:
            update["progress"] = str(progress)[:500]
        try:
            _TimedRef(self._commands().document(cmd_id)).set(update, merge=True)
        except Exception as e:
            logger.warning("could not update command %s: %s", cmd_id, e)

    def _run_command(self, cmd_id: str, data: dict):
        ctype = data.get("type")
        args = data.get("args") or {}
        created = data.get("created_epoch") or 0
        if created and time.time() - float(created) > COMMAND_MAX_AGE_SECONDS:
            self._set_command(cmd_id, "expired",
                              error="Pi was offline when this was sent; not run.")
            return
        if ctype not in self.COMMANDS:
            self._set_command(cmd_id, "error", error=f"Unknown command '{ctype}'")
            return
        self._set_command(cmd_id, "running")
        logger.info("website command: %s %s", ctype, json.dumps(args)[:200])
        try:
            result = getattr(self, f"cmd_{ctype}")(args, cmd_id)
            if result is not None:   # None = the command finishes itself later
                self._set_command(cmd_id, "done", result=result)
        except Exception as e:
            logger.error("website command %s failed: %s", ctype, e)
            self._set_command(cmd_id, "error", error=e)
        finally:
            self._write_status(force=True)

    # ------------------------------------------------------------ commands: grow

    def cmd_ping(self, args, cmd_id):
        return {"message": "pong", "uptime_s": int(time.time() - self.started_epoch)}

    def cmd_lights_on(self, args, cmd_id):
        minutes = float(args.get("minutes", 60))
        r = self.grower.actuators.turn_on_lights(minutes)
        if r.get("simulated"):
            raise RuntimeError("GPIO not available on this device — light relay not switched")
        return {"message": f"Lights on for {r.get('minutes')} min", **r}

    def cmd_lights_off(self, args, cmd_id):
        r = self.grower.actuators.turn_off_lights()
        if r.get("simulated"):
            raise RuntimeError("GPIO not available on this device — light relay not switched")
        return {"message": "Lights off", **r}

    def cmd_pump(self, args, cmd_id):
        seconds = float(args.get("seconds", 5))
        g = self.grower
        r = g.actuators.run_pump(seconds, source="manual",
                                 reason=args.get("reason", "Manual watering from the website"))
        try:
            g.context.log_decision({
                "day": g.context.get_days_since_planting(),
                "trigger_type": "manual",
                "sensors": g.sensors.read_all(),
                "observation": f"Manual pump from website: {r.get('seconds')}s",
                "reasoning": args.get("reason", "Manual watering from the website"),
                "actions": [{"tool": "run_pump", "args": {"seconds": r.get("seconds")}}],
            })
        except Exception:
            pass
        if r.get("error"):
            raise RuntimeError(f"Pump failed: {r['error']}")
        return {"message": f"Watered for {r.get('seconds')} s", **r}

    def cmd_reset_ai_memory(self, args, cmd_id):
        """Start a new grow (default) or just clear the AI's memory.

        New grow: archive memory + photos, set planted_date (default today), and
        record grows/<device>.current_grow so the website shows only this grow's
        photos, decisions and sensor trends. Nothing is deleted anywhere.
        """
        from src.memory_reset import reset_ai_memory
        new_grow = args.get("new_grow", True) is not False
        planted = str(args.get("planted_date") or datetime.now().strftime("%Y-%m-%d"))
        datetime.strptime(planted, "%Y-%m-%d")      # validate before changing anything
        r = reset_ai_memory(self.base, args.get("reason", "Reset from the website"),
                            grower=self.grower, archive_images=new_grow)
        if new_grow:
            self.config.setdefault("plant", {})["planted_date"] = planted
            self._save_local({"plant": {"planted_date": planted}})
            grow = {"id": Path(r["archive"]).name, "started_at": datetime.now().isoformat(),
                    "planted_date": planted, "previous_archive": r["archive"],
                    "reason": args.get("reason", "")}
            try:
                fresh_summary = {
                    "current_grow": grow,
                    "summary": "No growth history yet. This is a new plant.",
                    "key_learnings": [], "patterns": {}, "updated_at": datetime.now().isoformat()}
                _TimedRef(self.fb.db.document(f"grows/{self.device_id}")).set(fresh_summary, merge=True)
                _TimedRef(self.fb.db.document(
                    f"grows/{self.device_id}/grow_history/{grow['id']}")).set(grow)
            except Exception as e:
                raise RuntimeError(f"Memory archived and date set, but the website could not be "
                                   f"told about the new grow: {e}. Run the reset again.")
            r["message"] = (f"New grow started (day 0 = {planted}). {r['message']}. "
                            "The website now shows only this grow.")
            r["grow"] = grow
        self.record_activity("system", r["message"])
        return r

    def cmd_checkin(self, args, cmd_id):
        g = self.grower
        if getattr(g, "_checkin_lock", False):
            return {"message": "A check-in is already running"}
        self._set_command(cmd_id, "running", progress="AI check-in running (cameras + Gemini)…")
        g.run_checkin(trigger_type="manual", force=True)
        last = getattr(g, "last_checkin", None) or {}
        if last.get("ok") is False:
            raise RuntimeError(last.get("error") or "check-in failed")
        return {"message": "Check-in finished", **{k: v for k, v in last.items() if k != "ok"}}

    def cmd_schedule_checkin(self, args, cmd_id):
        minutes = int(args.get("minutes", 30))
        a = self.grower.scheduler.schedule_checkin(minutes, "Scheduled from website")
        return {"message": f"Next check-in in {minutes} min", "action_id": a.id}

    def cmd_pause_ai(self, args, cmd_id):
        self.grower.set_paused(True)
        return {"message": "AI paused: no automatic check-ins, watering or lighting"}

    def cmd_resume_ai(self, args, cmd_id):
        self.grower.set_paused(False)
        return {"message": "AI resumed; check-in in about 1 minute"}

    def cmd_cancel_action(self, args, cmd_id):
        ok = self.grower.scheduler.cancel(str(args.get("id", "")))
        if not ok:
            raise RuntimeError("No pending action with that id")
        return {"message": f"Cancelled {args.get('id')}"}

    # ------------------------------------------------------------ commands: cameras

    def cmd_capture(self, args, cmd_id):
        which = args.get("camera", "both")
        names = ["plant", "dashboard"] if which == "both" else [which]
        cam = self.grower.camera
        out, failed = {}, []
        for name in names:
            path = cam.capture_plant("manual") if name == "plant" else cam.capture_dashboard("manual")
            out[name] = path
            if path:
                self.grower.firebase.upload_image(path, f"manual_{name}")
            else:
                failed.append(f"{name}: {cam.health.get(name, {}).get('last_error', 'failed')}")
        if failed and len(failed) == len(names):
            raise RuntimeError("; ".join(failed))
        return {"message": "Photo taken and uploaded" + (f" (failed: {'; '.join(failed)})" if failed else ""),
                "paths": out}

    def cmd_live_start(self, args, cmd_id):
        seconds = max(30, min(LIVE_MAX_SECONDS, float(args.get("seconds", LIVE_DEFAULT_SECONDS))))
        cams = [c for c in (args.get("cameras") or ["plant", "dashboard"]) if c in ("plant", "dashboard")]
        self.live_cams = cams or ["plant", "dashboard"]
        self.live_until = time.time() + seconds
        self._live_event.set()
        return {"message": f"Live view on for {int(seconds)} s", "cameras": self.live_cams}

    def cmd_live_stop(self, args, cmd_id):
        self.live_until = 0
        self._live_event.set()
        return {"message": "Live view stopped"}

    def cmd_set_cameras(self, args, cmd_id):
        cam = self.grower.camera
        changes = {}
        if args.get("plant_cam") not in (None, ""):
            changes["plant_cam"] = str(args["plant_cam"]).strip()
        if args.get("dashboard_cam") not in (None, ""):
            changes["dashboard_cam"] = str(args["dashboard_cam"]).strip()
        if args.get("plant_rotation") not in (None, ""):
            rot = int(args["plant_rotation"])
            if rot not in (0, 90, 180, 270):
                raise ValueError("rotation must be 0, 90, 180 or 270")
            changes["plant_rotation"] = rot
        if not changes:
            raise ValueError("nothing to change")
        cam.set_devices(plant=changes.get("plant_cam"), dashboard=changes.get("dashboard_cam"),
                        plant_rotation=changes.get("plant_rotation"))
        self._save_local({"camera": changes})
        return {"message": "Camera settings saved", **changes}

    def cmd_swap_cameras(self, args, cmd_id):
        cam = self.grower.camera
        plant, dash = cam.dashboard_device, cam.plant_device
        cam.set_devices(plant=plant, dashboard=dash)
        self._save_local({"camera": {"plant_cam": str(plant), "dashboard_cam": str(dash)}})
        return {"message": f"Swapped: plant={plant}, dashboard={dash}"}

    # ------------------------------------------------------------ commands: settings

    def cmd_set_plant(self, args, cmd_id):
        changes = {k: str(args[k]).strip() for k in ("species", "variety", "planted_date")
                   if args.get(k) not in (None, "")}
        if "planted_date" in changes:
            datetime.strptime(changes["planted_date"], "%Y-%m-%d")  # validates
        if not changes:
            raise ValueError("nothing to change")
        self.config.setdefault("plant", {}).update(changes)
        self._save_local({"plant": changes})
        return {"message": "Plant settings saved", **changes}

    def cmd_set_schedule(self, args, cmd_id):
        sched = self.grower.scheduler
        changes = {}
        for k in ("min_checkin_minutes", "max_checkin_minutes"):
            if args.get(k) not in (None, ""):
                v = int(args[k])
                if not 1 <= v <= 1440:
                    raise ValueError(f"{k} must be 1–1440")
                changes[k] = v
                setattr(sched, k, v)
        if not changes:
            raise ValueError("nothing to change")
        self._save_local({"scheduler": changes})
        return {"message": "Schedule limits saved", **changes}

    def _save_local(self, override: dict):
        path = self.base / "config.local.yaml"
        current = {}
        if path.exists():
            current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        deep_merge(current, override)
        path.write_text("# Settings changed from the website. These override config.yaml.\n"
                        + yaml.safe_dump(current, sort_keys=False), encoding="utf-8")
        deep_merge(self.config, override)

    # ------------------------------------------------------------ commands: system

    def cmd_get_logs(self, args, cmd_id):
        self._write_logs()
        return {"message": "Logs refreshed", "lines": len(self.logs.snapshot())}

    def cmd_restart(self, args, cmd_id):
        self._set_command(cmd_id, "done", result={"message": "Restarting grower software…"})
        self.request_restart()
        return None

    def cmd_reboot(self, args, cmd_id):
        self._set_command(cmd_id, "running", progress="Rebooting the Pi…")
        try:
            self.grower.cleanup()
        except Exception:
            pass
        r = subprocess.run(["sudo", "-n", "/sbin/reboot"], capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            raise RuntimeError("Reboot not permitted: " + (r.stderr.strip() or "sudo needs a password")
                               + ". On the Pi run: echo \"$USER ALL=(ALL) NOPASSWD: /sbin/reboot\" "
                               "| sudo tee /etc/sudoers.d/vfarm-reboot")
        self._set_command(cmd_id, "done", result={"message": "Reboot started"})
        return None

    def cmd_update_code(self, args, cmd_id):
        """Pull the latest code from GitHub, sanity-check it, restart, verify."""
        def progress(msg):
            logger.info("update: %s", msg)
            self._set_command(cmd_id, "running", progress=msg)

        branch = str(args.get("branch") or "").strip()
        force = bool(args.get("discard_local_changes"))
        if branch and not all(c.isalnum() or c in "-_./" for c in branch):
            raise ValueError("invalid branch name")

        if not self._is_git_repo():
            progress("Setting up git on the Pi (first update)…")
            backup = self.base / "data" / "pre_git_backup.tar.gz"
            subprocess.run(["tar", "-czf", str(backup), "src", "config.yaml", "requirements.txt"],
                           cwd=self.base, capture_output=True, timeout=120)
            for cmd in (["init"], ["remote", "add", "origin", self.repo_url]):
                self._git(*cmd)
            branch = branch or "main"
            r = self._git("fetch", "origin", branch, timeout=180)
            if r.returncode:
                raise RuntimeError(f"git fetch failed: {r.stderr.strip()}")
            from_commit = None
            r = self._git("checkout", "-f", "-B", branch, f"origin/{branch}")
            if r.returncode:
                raise RuntimeError(f"git checkout failed: {r.stderr.strip()}")
            self._git("branch", f"--set-upstream-to=origin/{branch}", branch)
            commits = ["(first sync from GitHub)"]
        else:
            current = self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            branch = branch or (current if current != "HEAD" else "main")
            dirty = [l[3:] for l in self._git("status", "--porcelain",
                                               "--untracked-files=no").stdout.splitlines()]
            if dirty:
                if not force:
                    raise RuntimeError("The Pi has local edits to: " + ", ".join(dirty[:10])
                                       + ". Tick 'discard local edits' to stash them and update.")
                progress("Stashing local edits on the Pi…")
                self._git("stash", "push", "-m", f"website update {utc_now()}")
            progress(f"Fetching {branch} from GitHub…")
            r = self._git("fetch", "origin", branch, timeout=180)
            if r.returncode:
                raise RuntimeError(f"git fetch failed: {r.stderr.strip()}")
            from_commit = self._git("rev-parse", "--short", "HEAD").stdout.strip()
            if branch != current:
                r = self._git("checkout", branch)
                if r.returncode:
                    r = self._git("checkout", "-b", branch, "--track", f"origin/{branch}")
                if r.returncode:
                    raise RuntimeError(f"could not switch to {branch}: {r.stderr.strip()}")
            r = self._git("merge", "--ff-only", f"origin/{branch}")
            if r.returncode:
                raise RuntimeError("Pi's copy has diverged from GitHub (can't fast-forward): "
                                   + r.stderr.strip()[:400])
            commits = self._git("log", "--oneline", f"{from_commit}..HEAD").stdout.splitlines()[:30]

        to_commit = self._git("rev-parse", "--short", "HEAD").stdout.strip()
        if from_commit and to_commit == from_commit and not args.get("restart_anyway"):
            return {"message": f"Already up to date ({to_commit} on {branch})", "to": to_commit}

        # Dependencies
        changed = self._git("diff", "--name-only", from_commit or "HEAD~0", "HEAD").stdout.splitlines() \
            if from_commit else ["requirements.txt"]
        if "requirements.txt" in changed:
            progress("requirements.txt changed — installing packages…")
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
                               cwd=self.base, capture_output=True, text=True, timeout=900)
            if r.returncode:
                logger.warning("pip install had problems: %s", r.stderr[-800:])

        # Catch syntax errors before restarting (-f: compileall skips files whose
        # mtime matches the cached .pyc to the second, which can hide a broken edit)
        progress("Checking the new code compiles…")
        r = subprocess.run([sys.executable, "-m", "compileall", "-q", "-f", "src"], cwd=self.base,
                           capture_output=True, text=True, timeout=180)
        if r.returncode:
            if from_commit:
                self._git("reset", "--hard", from_commit)
            raise RuntimeError("New code has syntax errors, kept the old version:\n"
                               + (r.stdout + r.stderr)[-1500:])

        self._write_update_state({
            "state": "restarting", "kind": "code", "command_id": cmd_id, "branch": branch,
            "from_commit": from_commit, "to_commit": to_commit, "commits": commits,
            "started_at": utc_now(),
            # supervisor.py uses this if the new code crashes soon after starting
            "rollback": ({"type": "git", "commit": from_commit} if from_commit
                         else {"type": "tar", "backup": "data/pre_git_backup.tar.gz"}),
        })
        progress(f"Updated {from_commit or 'copy'} → {to_commit}. Restarting…")
        self._write_status(force=True)
        self.request_restart()
        return None

    # ------------------------------------------------------------ commands: AI memory

    def cmd_get_ai_memory(self, args, cmd_id):
        """Everything the AI remembers, plus the exact last prompt it was sent."""
        from src.context_manager import ContextManager
        g = self.grower
        ctx = g.context
        decisions = [ContextManager._slim_decision_for_prompt(d)
                     for d in ctx.get_recent_decisions(int(args.get("decisions", 15)))]
        last_turn = None
        try:
            last_turn = json.loads((self.base / "data" / "last_turn.json").read_text(encoding="utf-8"))
            last_turn["user_message"] = last_turn.get("user_message", "")[:300_000]
        except Exception:
            pass
        archive_dir = self.base / "data" / "archive"
        archives = sorted((p.name for p in archive_dir.iterdir() if p.is_dir()), reverse=True) \
            if archive_dir.exists() else []
        return {
            "message": "AI memory loaded",
            "growth_summary": ctx.get_growth_summary(),
            "milestones": ctx.get_milestones()[-30:],
            "recent_decisions": decisions,
            "decision_count": ctx.get_decision_count(),
            "needs_compression": ctx.needs_compression(),
            "pending_actions": g.scheduler.get_pending()[:30],
            "water_budget": g.actuators.water_budget.status(),
            "last_turn": last_turn,
            "archives": archives[:30],
        }

    # ------------------------------------------------------------ commands: config editor

    def _config_backups(self) -> list[str]:
        d = self.base / "data" / "config_backups"
        return sorted((p.name for p in d.glob("*.yaml")), reverse=True)[:30] if d.exists() else []

    @staticmethod
    def _budget_ceilings() -> dict:
        from src import water_budget as wb
        return {"max_seconds_per_dose": wb.HARD_MAX_SECONDS_PER_DOSE,
                "ai_max_seconds_per_day": wb.HARD_MAX_AI_SECONDS_PER_DAY,
                "ai_min_minutes_between_doses": wb.HARD_MIN_AI_GAP_MINUTES,
                "manual_max_seconds_per_day": wb.HARD_MAX_MANUAL_SECONDS_PER_DAY}

    def cmd_get_config(self, args, cmd_id):
        if args.get("backup"):
            name = Path(str(args["backup"])).name
            text = (self.base / "data" / "config_backups" / name).read_text(encoding="utf-8")
            return {"message": f"Backup {name}", "backup": name, "text": text}
        base_p, local_p = self.base / "config.yaml", self.base / "config.local.yaml"
        return {
            "message": "Config loaded",
            "config_yaml": base_p.read_text(encoding="utf-8"),
            "local_yaml": local_p.read_text(encoding="utf-8") if local_p.exists() else "",
            "effective_yaml": yaml.safe_dump(load_config(self.base), sort_keys=False),
            "backups": self._config_backups(),
            "water_budget_ceilings": self._budget_ceilings(),
        }

    def cmd_set_config(self, args, cmd_id):
        """Replace config.yaml or config.local.yaml, then restart and verify.

        If the grower crashes right after the restart, supervisor.py puts the
        old file back.
        """
        which = args.get("file", "local")
        if which not in ("local", "base"):
            raise ValueError("file must be 'local' or 'base'")
        text = str(args.get("text", ""))
        if len(text) > 200_000:
            raise ValueError("config text too large")
        try:
            parsed = yaml.safe_load(text) if text.strip() else {}
        except yaml.YAMLError as e:
            raise ValueError(f"YAML syntax error: {e}")
        if parsed is not None and not isinstance(parsed, dict):
            raise ValueError("config must be a YAML mapping (key: value)")

        base_p, local_p = self.base / "config.yaml", self.base / "config.local.yaml"
        target = base_p if which == "base" else local_p
        base_cfg = parsed if which == "base" else yaml.safe_load(base_p.read_text(encoding="utf-8"))
        local_cfg = parsed if which == "local" else (
            yaml.safe_load(local_p.read_text(encoding="utf-8")) if local_p.exists() else {})
        merged = deep_merge(json.loads(json.dumps(base_cfg or {}, default=str)), local_cfg or {})
        errors = validate_config(merged)
        if errors:
            raise ValueError("Config not saved: " + "; ".join(errors))

        backup_dir = self.base / "data" / "config_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = None
        if target.exists():
            backup = backup_dir / f"{_stamp()}_{target.name}"
            shutil.copy2(target, backup)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
        logger.warning("config edited from website: %s (backup %s)", target.name,
                       backup.name if backup else "none")

        if args.get("restart", True) is False:
            return {"message": f"Saved {target.name}. Takes effect on the next restart.",
                    "backup": backup.name if backup else None}

        self._write_update_state({
            "state": "restarting", "kind": "config", "command_id": cmd_id,
            "file": target.name, "started_at": utc_now(),
            "rollback": {"type": "files", "files": [{
                "path": target.name,
                "backup": backup.relative_to(self.base).as_posix() if backup else None}]},
        })
        self._set_command(cmd_id, "running", progress=f"Saved {target.name}. Restarting grower…")
        self.request_restart()
        return None

    # ------------------------------------------------------------ commands: prompt editor

    def cmd_get_prompt(self, args, cmd_id):
        ctx = self.grower.context
        text, source = ctx.get_prompt_template()
        default_p, _ = ctx.prompt_template_paths()
        return {
            "message": "Prompt loaded",
            "text": text,
            "source": source,
            "default_text": default_p.read_text(encoding="utf-8") if default_p.exists() else "",
            "variables": {k: str(v)[:300] for k, v in ctx.prompt_variables().items()},
            "rendered": ctx.build_system_prompt(),
            "hard_limits": ctx.hard_limits_section(),
        }

    def cmd_set_prompt(self, args, cmd_id):
        """Save a website edit of the system prompt; used from the next check-in on."""
        import re
        ctx = self.grower.context
        _, override = ctx.prompt_template_paths()
        if override.exists():
            backup_dir = self.base / "data" / "prompt_backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(override, backup_dir / f"{_stamp()}_{override.name}")
        if args.get("reset"):
            if override.exists():
                override.unlink()
            logger.warning("system prompt reset to the default from the website")
            self.record_activity("system", "System prompt reset to the default")
            return {"message": "Prompt reset to the default (prompts/system_prompt.md)"}
        text = str(args.get("text", ""))
        if len(text.strip()) < 50:
            raise ValueError("prompt is empty or too short")
        if len(text) > 100_000:
            raise ValueError("prompt too large (max 100k characters)")
        unknown = sorted(set(re.findall(r"\{([a-z_]+)\}", text)) - set(ctx.prompt_variables()))
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text(text, encoding="utf-8")
        logger.warning("system prompt edited from the website (%d chars)", len(text))
        self.record_activity("system", "System prompt edited from the website")
        return {"message": "Prompt saved; used from the next check-in",
                "unknown_placeholders": unknown,
                "rendered_chars": len(ctx.build_system_prompt())}

    # ------------------------------------------------------------ restart

    def request_restart(self):
        """Exit so supervisor.py (or systemd) starts the grower again."""
        def _go():
            time.sleep(1.5)
            logger.info("Restarting grower process…")
            try:
                self.stop()
                self.grower.cleanup()
            except Exception as e:
                logger.warning("cleanup before restart failed: %s", e)
            logging.shutdown()
            if os.environ.get("VFARM_SUPERVISED") == "1" or os.environ.get("INVOCATION_ID"):
                os._exit(RESTART_EXIT_CODE)
            env = dict(os.environ, VFARM_RESTART="1")
            os.chdir(self.base)
            os.execve(sys.executable, [sys.executable, "-m", "src.main"], env)
        threading.Thread(target=_go, daemon=True).start()

    # ------------------------------------------------------------ live view

    def _live_loop(self):
        import cv2
        cam = self.grower.camera
        streaming = False
        while self._running:
            if self.live_until <= time.time():
                if streaming:
                    cam.stop_streams()
                    streaming = False
                    self._write_status(force=True)
                self._live_event.wait(timeout=5)
                self._live_event.clear()
                continue
            if not streaming:
                for name in self.live_cams:
                    cam.start_stream(name)
                streaming = True
                self._write_status(force=True)
            t0 = time.time()
            for name in list(self.live_cams):
                if name == "plant" and getattr(self.grower, "_checkin_lock", False):
                    pass  # check-in shares the open stream; fine to keep going
                frame = cam.read_stream_frame(name)
                if frame is None:
                    cam.start_stream(name)   # try to reopen next round
                    try:
                        self._doc(f"live/frame_{name}").set({
                            "error": cam.health.get(name, {}).get("last_error", "no frame"),
                            "error_at": utc_now()}, merge=True)
                    except Exception:
                        pass
                    continue
                h, w = frame.shape[:2]
                if w > LIVE_WIDTH:
                    frame = cv2.resize(frame, (LIVE_WIDTH, int(h * LIVE_WIDTH / w)))
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
                if not ok:
                    continue
                self.live_seq += 1
                try:
                    self._doc(f"live/frame_{name}").set({
                        "jpeg": buf.tobytes(), "at": utc_now(), "epoch": time.time(),
                        "seq": self.live_seq, "width": frame.shape[1], "height": frame.shape[0],
                        "error": None,
                    })
                except Exception as e:
                    logger.debug("live frame upload failed: %s", e)
            # Firestore allows ~1 write/s per document
            time.sleep(max(0.0, max(1.0, 1.0 / max(self.live_fps, 0.1)) - (time.time() - t0)))
