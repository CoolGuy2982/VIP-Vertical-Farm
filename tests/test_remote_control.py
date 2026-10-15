import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src import remote_control as rc
from src.action_scheduler import ActionScheduler
from src.actuators import Actuators
from src.context_manager import ContextManager
from src.remote_control import LogBuffer, RemoteControl, load_config
from tests.fake_firestore import FakeFirestore

REPO = Path(__file__).resolve().parent.parent
DEVICE = "testPi"


def make_config():
    return yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))


class FakeGrower:
    def __init__(self, base: Path, config: dict, db: FakeFirestore):
        self.config = config
        self.base_dir = str(base)
        self.paused = False
        self._checkin_lock = False
        self.last_checkin = None
        self.remote = None
        self.checkins = []
        self._alert_log = []
        self.actuators = Actuators(config, str(base))
        self.scheduler = ActionScheduler(config, str(base))
        self.context = ContextManager(str(base), config)
        self.sensors = SimpleNamespace(read_all=lambda: {"temperature_c": 22})
        self.gemini = SimpleNamespace(reset_chain=lambda: None)
        self.firebase = SimpleNamespace(enabled=True, db=db, device_id=DEVICE,
                                        upload_image=lambda *a, **k: None)
        self.camera = SimpleNamespace(
            health={"plant": {}, "dashboard": {}},
            plant_device=2, dashboard_device=0, plant_rotation=270,
            set_devices=lambda **k: None, stop_streams=lambda: None,
            capture_plant=lambda t: None, capture_dashboard=lambda t: None)

    def set_paused(self, paused):
        self.paused = paused

    def run_checkin(self, trigger_type="checkin", trigger_context=None, force=False):
        self.checkins.append(trigger_type)
        self.last_checkin = {"at": "now", "ok": True, "trigger": trigger_type}

    def cleanup(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    shutil.copy(REPO / "config.yaml", tmp_path / "config.yaml")
    (tmp_path / "prompts").mkdir()
    shutil.copy(REPO / "prompts" / "system_prompt.md", tmp_path / "prompts" / "system_prompt.md")
    monkeypatch.setattr(rc, "VERIFY_SECONDS", 0)
    db = FakeFirestore()
    config = load_config(tmp_path)
    grower = FakeGrower(tmp_path, config, db)
    remote = RemoteControl(grower, config, str(tmp_path), LogBuffer())
    grower.remote = remote
    restarts = []
    monkeypatch.setattr(remote, "request_restart", lambda: restarts.append(1))
    remote.start()
    yield SimpleNamespace(base=tmp_path, db=db, grower=grower, remote=remote,
                          restarts=restarts, config=config)
    remote.stop()


def send(env, ctype, args=None, created_epoch=None, wait=True, until=("done", "error", "expired")):
    doc = env.db.collection(f"grows/{DEVICE}/commands").document()
    doc.set({"type": ctype, "args": args or {}, "status": "pending",
             "created_epoch": created_epoch or time.time(), "source": "test"})
    if not wait:
        return doc.id
    return wait_for(env, doc.id, until)


def wait_for(env, cmd_id, until=("done", "error", "expired"), timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = env.db.docs.get(f"grows/{DEVICE}/commands/{cmd_id}", {})
        if d.get("status") in until:
            return d
        time.sleep(0.05)
    raise AssertionError(f"command {cmd_id} stuck: {env.db.docs.get(f'grows/{DEVICE}/commands/{cmd_id}')}")


def status(env):
    env.remote._write_status(force=True)   # the status write after a command is async
    return env.db.docs[f"grows/{DEVICE}/live/status"]


# ── basics ──────────────────────────────────────────────────────────────────

def test_heartbeat_status(env):
    s = status(env)
    assert s["supervisor"]["state"] == "running"
    assert s["health"] in ("ok", "warning", "error")
    assert s["actuators"]["water_budget"]["limits"]["ai_max_seconds_per_day"] == 45
    assert s["grower"]["paused"] is False


def test_ping_via_live_listener(env):
    d = send(env, "ping")
    assert d["status"] == "done" and d["result"]["message"] == "pong"


def test_unknown_and_expired_commands(env):
    assert "Unknown command" in send(env, "rm_rf")["error"]
    assert send(env, "ping", created_epoch=time.time() - 3600)["status"] == "expired"


def test_poll_fallback_without_listener(env):
    env.remote._watch = None
    for w in env.db.watches:
        w.unsubscribe()
    cmd_id = send(env, "ping", wait=False)
    # run one poll pass by hand
    for d in env.remote._commands().where("status", "==", "pending").limit(20).get():
        env.remote._accept(d)
    assert wait_for(env, cmd_id)["status"] == "done"


# ── grow controls ───────────────────────────────────────────────────────────

def test_manual_pump_uses_manual_budget(env, monkeypatch):
    monkeypatch.setattr(env.grower.actuators, "_run_async", lambda coro: coro.close())
    d = send(env, "pump", {"seconds": 45, "reason": "test"})
    assert d["status"] == "done" and d["result"]["seconds"] == 45
    send(env, "pump", {"seconds": 45})
    d = send(env, "pump", {"seconds": 45})
    assert d["result"]["seconds"] == 30                     # 120 s manual cap
    d = send(env, "pump", {"seconds": 10})
    assert d["status"] == "error" and "Water budget" in d["error"]
    # the AI budget is untouched
    assert env.grower.actuators.water_budget.status()["ai_used_s"] == 0


def test_pause_resume_and_checkin(env):
    send(env, "pause_ai")
    assert env.grower.paused and status(env)["grower"]["paused"] is True
    send(env, "resume_ai")
    assert not env.grower.paused
    assert send(env, "checkin")["status"] == "done"
    assert env.grower.checkins == ["manual"]


def test_set_plant_writes_local_config(env):
    d = send(env, "set_plant", {"planted_date": "2026-10-08", "variety": "Butterhead"})
    assert d["status"] == "done"
    assert load_config(env.base)["plant"]["variety"] == "Butterhead"
    assert send(env, "set_plant", {"planted_date": "10/08/2026"})["status"] == "error"


# ── config editor ───────────────────────────────────────────────────────────

def test_get_config(env):
    r = send(env, "get_config")["result"]
    assert "water_pump" in r["config_yaml"] and r["local_yaml"] == ""
    assert r["water_budget_ceilings"]["ai_max_seconds_per_day"] == 120


def test_set_config_rejects_bad_yaml_and_bad_values(env):
    before = (env.base / "config.yaml").read_text(encoding="utf-8")
    d = send(env, "set_config", {"file": "base", "text": "plant: [unclosed"})
    assert d["status"] == "error" and "YAML" in d["error"]
    d = send(env, "set_config", {"file": "local", "text": "plant:\n  planted_date: tomorrow\n"})
    assert d["status"] == "error" and "planted_date" in d["error"]
    d = send(env, "set_config", {"file": "local", "text": "- just\n- a list\n"})
    assert d["status"] == "error"
    assert (env.base / "config.yaml").read_text(encoding="utf-8") == before
    assert not (env.base / "config.local.yaml").exists()
    assert env.restarts == []


def test_set_config_saves_backs_up_and_restarts(env):
    text = "water_pump:\n  budget:\n    ai_max_seconds_per_day: 30\n"
    cmd_id = send(env, "set_config", {"file": "local", "text": text}, wait=False)
    d = wait_for(env, cmd_id, until=("running",))
    for _ in range(100):
        if env.restarts:
            break
        time.sleep(0.05)
    assert env.restarts == [1]
    assert (env.base / "config.local.yaml").read_text(encoding="utf-8") == text
    state = json.loads((env.base / "data" / "update_state.json").read_text())
    assert state["state"] == "restarting" and state["kind"] == "config"
    assert state["rollback"] == {"type": "files", "files": [
        {"path": "config.local.yaml", "backup": None}]}
    # editing config.yaml makes a backup the supervisor can restore
    send(env, "set_config", {"file": "base",
                             "text": (env.base / "config.yaml").read_text(encoding="utf-8")},
         until=("running",))
    for _ in range(100):
        state = json.loads((env.base / "data" / "update_state.json").read_text())
        if state.get("file") == "config.yaml":
            break
        time.sleep(0.05)
    backup = state["rollback"]["files"][0]["backup"]
    assert backup and (env.base / backup).exists()


def test_set_config_without_restart(env):
    d = send(env, "set_config", {"file": "local", "text": "scheduler:\n  min_checkin_minutes: 10\n",
                                 "restart": False})
    assert d["status"] == "done" and env.restarts == []


def test_verify_startup_marks_config_change_done(env):
    cmd_id = env.db.collection(f"grows/{DEVICE}/commands").document().id
    env.remote._write_update_state({"state": "restarting", "kind": "config", "command_id": cmd_id})
    env.remote._verify_startup()
    state = json.loads((env.base / "data" / "update_state.json").read_text())
    assert state["state"] == "success"
    d = env.db.docs[f"grows/{DEVICE}/commands/{cmd_id}"]
    assert d["status"] == "done" and "Config change OK" in d["result"]["message"]


# ── prompt editor + memory viewer ───────────────────────────────────────────

def test_prompt_edit_and_reset(env):
    r = send(env, "get_prompt")["result"]
    assert r["source"] == "default" and "Hard Limits" in r["rendered"]
    text = "You grow {variety} on day {day}. Be careful with water. {nonsense} " + "x" * 50
    r = send(env, "set_prompt", {"text": text})["result"]
    assert r["unknown_placeholders"] == ["nonsense"]
    rendered = env.grower.context.build_system_prompt()
    assert rendered.startswith("You grow Diversified Lettuce Tray on day")
    assert "{nonsense}" in rendered
    assert "Hard Limits" in rendered          # can't be edited away
    assert send(env, "set_prompt", {"text": "hi"})["status"] == "error"
    send(env, "set_prompt", {"reset": True})
    assert env.grower.context.get_prompt_template()[1] == "default"
    assert list((env.base / "data" / "prompt_backups").iterdir())


def test_ai_memory_and_context(env):
    env.grower.context.log_decision({"day": 7, "observation": "pulse 440", "actions": []})
    (env.base / "data" / "last_turn.json").write_text(json.dumps(
        {"at": "now", "system_prompt": "SP", "user_message": "CTX"}))
    r = send(env, "get_ai_memory")["result"]
    assert r["recent_decisions"][0]["observation"] == "pulse 440"
    assert r["last_turn"]["user_message"] == "CTX"
    assert "ai_remaining_s" in r["water_budget"]

    env.remote.publish_ai_context({"at": "now", "system_prompt": "SP", "user_message": "CTX"})
    assert env.db.docs[f"grows/{DEVICE}/live/ai_context"]["user_message"] == "CTX"

    r = send(env, "reset_ai_memory", {"reason": "new grow"})["result"]
    assert (env.base / r["archive"] / "decisions.jsonl").exists()
    r = send(env, "get_ai_memory")["result"]
    assert r["recent_decisions"] == [] and r["archives"]


# ── update from GitHub ──────────────────────────────────────────────────────

def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_update_code_fast_forward_and_syntax_check(env, tmp_path_factory):
    origin = tmp_path_factory.mktemp("origin")
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "t@t")
    git(origin, "config", "user.name", "t")
    (origin / "src").mkdir()
    (origin / "src" / "ok.py").write_text("X = 1\n")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "one")

    pi = env.base
    git(pi, "init", "-q", "-b", "main")
    git(pi, "remote", "add", "origin", str(origin))
    git(pi, "fetch", "-q", "origin", "main")
    git(pi, "checkout", "-qf", "-B", "main", "origin/main")
    first = git(pi, "rev-parse", "--short", "HEAD")

    (origin / "src" / "ok.py").write_text("X = 2\n")
    git(origin, "commit", "-qam", "two")
    cmd_id = send(env, "update_code", {"branch": "main"}, wait=False)
    for _ in range(200):
        if env.restarts:
            break
        time.sleep(0.05)
    assert env.restarts, env.db.docs.get(f"grows/{DEVICE}/commands/{cmd_id}")
    state = json.loads((pi / "data" / "update_state.json").read_text())
    assert state["rollback"] == {"type": "git", "commit": first}
    assert (pi / "src" / "ok.py").read_text() == "X = 2\n"

    # a commit with a syntax error is refused and the Pi stays on the old code
    good = git(pi, "rev-parse", "--short", "HEAD")
    (origin / "src" / "ok.py").write_text("def broken(:\n")
    git(origin, "commit", "-qam", "three")
    d = send(env, "update_code", {"branch": "main"})
    assert d["status"] == "error" and "syntax" in d["error"]
    assert git(pi, "rev-parse", "--short", "HEAD") == good
