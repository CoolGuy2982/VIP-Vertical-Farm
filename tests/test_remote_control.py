import json
from datetime import datetime
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


# ── link robustness ─────────────────────────────────────────────────────────

def test_firestore_writes_have_timeouts(env):
    send(env, "ping")
    # the one write without a timeout is the test creating the command doc
    assert env.db.timeouts.count(None) == 1
    assert env.db.timeouts.count(rc.FIRESTORE_TIMEOUT) >= 3


def test_failed_heartbeat_is_logged_as_warning(env, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("DeadlineExceeded")
    monkeypatch.setattr(env.remote, "build_status", boom)
    env.remote._last_link_warning = 0
    env.remote._running = True
    monkeypatch.setattr(rc, "HEARTBEAT_SECONDS", 0)
    import threading
    t = threading.Thread(target=env.remote._heartbeat_loop, daemon=True)
    t.start()
    time.sleep(0.3)
    env.remote._running = False
    t.join(2)
    assert any("can't write status" in r.message for r in caplog.records if r.levelname == "WARNING")


def test_watchdog_restarts_once_when_heartbeat_stuck(env, monkeypatch):
    monkeypatch.setattr(rc.time, "sleep", lambda s: None)
    env.remote._heartbeat_tick = time.time() - rc.STUCK_SECONDS - 5
    env.remote._watchdog_loop()
    assert env.restarts == [1]
    # a second stuck heartbeat within the hour does not restart again
    env.remote._running = True
    calls = {"n": 0}

    def stop_after_one(_):
        calls["n"] += 1
        if calls["n"] > 1:
            env.remote._running = False
    monkeypatch.setattr(rc.time, "sleep", stop_after_one)
    env.remote._watchdog_loop()
    assert env.restarts == [1]


def test_watchdog_waits_for_checkin(env, monkeypatch):
    env.grower._checkin_lock = True
    env.remote._heartbeat_tick = time.time() - rc.STUCK_SECONDS - 5
    n = {"i": 0}

    def tick(_):
        n["i"] += 1
        if n["i"] > 2:
            env.remote._running = False
    monkeypatch.setattr(rc.time, "sleep", tick)
    env.remote._watchdog_loop()
    assert env.restarts == []


def test_status_with_odd_values_still_written(env):
    class SdkValue:            # stands in for e.g. a protobuf map from the Gemini SDK
        def __str__(self):
            return "odd"
    env.grower.sensors = SimpleNamespace(read_all=lambda: {"temperature_c": SdkValue()})
    env.remote._write_status(force=True)
    assert env.db.docs[f"grows/{DEVICE}/live/status"]["grower"]["sensors"]["temperature_c"] == "odd"


def test_gemini_503_is_explained_and_cleared_by_next_good_checkin(env):
    import logging
    log = logging.getLogger("src.remote_control")
    log.addHandler(env.remote.logs)
    try:
        msg = ("website command checkin failed: ServerError: 503 UNAVAILABLE. "
               "{'error': {'code': 503, 'message': 'This model is currently experiencing high demand.'}}")
        log.error(msg)
        env.grower.last_checkin = {"at": "2000-01-01T00:00:00", "ok": False, "error": msg}
        problems = env.remote._problems()
        assert all("Gemini is overloaded" in p["detail"] for p in problems
                   if p["id"] in ("checkin", "log_src.remote_control"))
        assert {"checkin", "log_src.remote_control"} <= {p["id"] for p in problems}
        # a later check-in worked: the old AI errors disappear
        env.grower.last_checkin = {"at": datetime.now().isoformat(timespec="seconds"), "ok": True}
        time.sleep(1.1)
        env.grower.last_checkin["at"] = datetime.now().isoformat(timespec="seconds")
        assert not {"checkin", "log_src.remote_control"} & {p["id"] for p in env.remote._problems()}
    finally:
        log.removeHandler(env.remote.logs)


def test_new_grow_resets_date_photos_and_tells_website(env):
    imgs = env.base / "data" / "images"
    imgs.mkdir(parents=True, exist_ok=True)
    (imgs / "plant_checkin_20261015_100000.jpg").write_bytes(b"x")
    (imgs / "blank").mkdir(exist_ok=True)
    (imgs / "blank" / "blank.jpg").write_bytes(b"b")
    env.grower.camera.image_dir = imgs
    env.grower.context.log_decision({"day": 14, "observation": "old grow"})

    r = send(env, "reset_ai_memory", {"planted_date": "2026-10-22", "reason": "new tray"})
    assert r["status"] == "done", r
    res = r["result"]
    assert res["images"] == 1 and "New grow started" in res["message"]
    # photos archived (blank placeholder kept), memory archived
    assert not list(imgs.glob("*.jpg")) and (imgs / "blank" / "blank.jpg").exists()
    assert (env.base / res["archive"] / "images" / "plant_checkin_20261015_100000.jpg").exists()
    # day 0 from today, saved for restarts
    assert env.grower.context.get_days_since_planting() == (datetime.now().date() - datetime(2026, 10, 22).date()).days
    assert load_config(env.base)["plant"]["planted_date"] == "2026-10-22"
    # website marker
    summary = env.db.docs[f"grows/{DEVICE}"]
    assert summary["current_grow"]["planted_date"] == "2026-10-22"
    assert summary["current_grow"]["started_at"] and summary["key_learnings"] == []
    assert f"grows/{DEVICE}/grow_history/{res['grow']['id']}" in env.db.docs


def test_memory_only_reset_keeps_photos_and_date(env):
    imgs = env.base / "data" / "images"
    imgs.mkdir(parents=True, exist_ok=True)
    (imgs / "plant_a.jpg").write_bytes(b"x")
    env.grower.camera.image_dir = imgs
    before = env.config["plant"]["planted_date"]
    r = send(env, "reset_ai_memory", {"new_grow": False})["result"]
    assert r["images"] == 0 and (imgs / "plant_a.jpg").exists()
    assert env.config["plant"]["planted_date"] == before
    assert f"grows/{DEVICE}" not in env.db.docs


def test_new_grow_rejects_bad_date_before_changing_anything(env):
    env.grower.context.log_decision({"day": 14})
    r = send(env, "reset_ai_memory", {"planted_date": "22/10/2026"})
    assert r["status"] == "error"
    assert env.grower.context.get_decision_count() == 1


def test_standby_turns_light_off_and_keeps_it_off(env, monkeypatch):
    calls = []
    monkeypatch.setattr(env.grower.actuators, "turn_off_lights",
                        lambda: calls.append("off") or {"action": "turn_off_lights"})
    r = send(env, "pause_ai")
    assert r["status"] == "done" and "grow light off" in r["result"]["message"]
    assert env.grower.paused and calls == ["off"]
    problems = {p["id"]: p for p in env.remote._problems()}
    assert problems["paused"]["title"].startswith("Standby")
    send(env, "pause_ai", {"keep_lights": True})
    assert calls == ["off"]


def test_startup_in_standby_skips_hardware_test_and_light_restore():
    src = (REPO / "src" / "main.py").read_text(encoding="utf-8")
    standby = src.index("if grower.paused:")
    assert standby < src.index("grower.actuators.restore_lights()")
    assert standby < src.index("hardware_test(grower, config, logger)")


def test_swap_relabels_photos_and_records_swap_for_website(env):
    imgs = env.base / "data" / "images"
    imgs.mkdir(parents=True, exist_ok=True)
    (imgs / "plant_checkin_20261022_090000.jpg").write_bytes(b"really the dashboard")
    (imgs / "dashboard_checkin_20261022_090000.jpg").write_bytes(b"really the plant")
    (imgs / "blank").mkdir(exist_ok=True)
    env.grower.camera.image_dir = imgs
    r = send(env, "swap_cameras")
    assert r["status"] == "done" and r["result"]["relabelled"] == 2
    assert (imgs / "plant_checkin_20261022_090000.jpg").read_bytes() == b"really the plant"
    assert (imgs / "dashboard_checkin_20261022_090000.jpg").read_bytes() == b"really the dashboard"
    assert not list(imgs.glob(".swap_*"))
    send(env, "swap_cameras")
    swaps = env.db.docs[f"grows/{DEVICE}"]["camera_swaps"]
    assert len(swaps) == 2 and swaps[0] <= swaps[1]
