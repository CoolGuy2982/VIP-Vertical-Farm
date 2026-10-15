import json
import time

import pytest

from src import actuators as actuators_mod
from src.action_scheduler import ActionScheduler
from src.actuators import Actuators
from src.memory_reset import reset_ai_memory
from src.water_budget import WaterBudget

CONFIG = {
    "hardware_specs": {"flow_rate_ml_s": 168},
    "water_pump": {
        "min_seconds_per_dose": 1,
        "max_seconds_per_dose": 45,
        "budget": {
            "ai_max_seconds_per_day": 45,
            "ai_min_minutes_between_doses": 180,
            "manual_max_seconds_per_day": 120,
        },
    },
}


def test_per_dose_cap_and_daily_cap(tmp_path):
    wb = WaterBudget(CONFIG, tmp_path)
    granted, why = wb.reserve(60, "ai")
    assert (granted, why) == (45, None)
    # second dose blocked by the 3 h gap
    granted, why = wb.reserve(10, "ai")
    assert granted == 0 and "180 min apart" in why


def test_daily_cap_counts_scheduled_and_trims(tmp_path, monkeypatch):
    wb = WaterBudget(CONFIG, tmp_path)
    t = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: t[0])
    assert wb.reserve(30, "scheduled")[0] == 30
    t[0] += 4 * 3600
    assert wb.reserve(30, "ai")[0] == 15          # only 15 s left of 45
    t[0] += 4 * 3600
    granted, why = wb.reserve(30, "ai")
    assert granted == 0 and "daily limit" in why
    t[0] += 17 * 3600                              # first dose falls out of the window
    assert wb.reserve(30, "ai")[0] == 30


def test_runaway_loop_is_capped(tmp_path, monkeypatch):
    # the 2026-10-15 loop: 60 s every 15 min
    wb = WaterBudget(CONFIG, tmp_path)
    t = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: t[0])
    total = 0
    for _ in range(96):
        total += wb.reserve(60, "ai")[0]
        t[0] += 15 * 60
    assert total <= 45


def test_manual_bucket_is_separate(tmp_path):
    wb = WaterBudget(CONFIG, tmp_path)
    assert wb.reserve(45, "ai")[0] == 45
    assert wb.reserve(45, "manual")[0] == 45       # no gap, own cap
    assert wb.reserve(45, "manual")[0] == 45
    assert wb.reserve(45, "manual")[0] == 30       # 120 s manual cap
    assert wb.reserve(10, "manual")[0] == 0


def test_budget_survives_restart(tmp_path):
    WaterBudget(CONFIG, tmp_path).reserve(45, "ai")
    granted, why = WaterBudget(CONFIG, tmp_path).reserve(10, "ai")
    assert granted == 0


def test_corrupt_log_fails_closed(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "pump_doses.json").write_text("{not json")
    assert WaterBudget(CONFIG, tmp_path).reserve(10, "ai")[0] == 0


def test_config_cannot_exceed_code_ceilings(tmp_path):
    loose = {"water_pump": {"max_seconds_per_dose": 600, "budget": {
        "ai_max_seconds_per_day": 99999, "ai_min_minutes_between_doses": 0,
        "manual_max_seconds_per_day": 99999}}}
    lim = WaterBudget(loose, tmp_path).limits()
    assert lim["max_seconds_per_dose"] == 60
    assert lim["ai_max_seconds_per_day"] == 120
    assert lim["ai_min_minutes_between_doses"] == 30
    assert lim["manual_max_seconds_per_day"] == 300


def test_run_pump_blocks_without_touching_pump(tmp_path, monkeypatch):
    act = Actuators(CONFIG, tmp_path)
    calls = []
    monkeypatch.setattr(act, "_run_async", lambda coro: (coro.close(), calls.append(1)))
    r1 = act.run_pump(60, source="ai")
    assert r1["seconds"] == 45 and not r1.get("blocked") and calls == [1]
    r2 = act.run_pump(60, source="ai")
    assert r2["blocked"] and r2["seconds"] == 0 and calls == [1]
    assert act.get_status()["water_budget"]["ai_remaining_s"] == 0


def test_memory_reset_archives_and_clears_queue(tmp_path):
    logs = tmp_path / "data" / "logs"
    logs.mkdir(parents=True)
    (logs / "decisions.jsonl").write_text('{"a": 1}\n{"a": 2}\n')
    (logs / "growth_summary.json").write_text('{"summary": "pulse 440"}')
    (tmp_path / "data" / "pump_doses.json").write_text('{"doses": []}')

    sched = ActionScheduler({}, str(tmp_path))
    sched.schedule("run_pump", 600, {"seconds": 60})

    class G:
        config = {"plant": {"species": "Lettuce", "planted_date": "2026-10-08"}}
        scheduler = sched
        gemini = type("Gm", (), {"reset_chain": lambda self: None})()
        _alert_log = ["x"]
        paused = False
        _checkin_lock = False

    r = reset_ai_memory(tmp_path, "new grow", grower=G())
    archive = tmp_path / r["archive"]
    assert (archive / "decisions.jsonl").exists()
    assert not (logs / "decisions.jsonl").exists()
    assert r["files"]["data/logs/decisions.jsonl"]["lines"] == 2
    assert json.loads((archive / "manifest.json").read_text())["reason"] == "new grow"
    # water budget is not part of the AI's memory
    assert (tmp_path / "data" / "pump_doses.json").exists()
    pending = sched.get_pending()
    assert [a["action_type"] for a in pending] == ["checkin"]


def test_memory_reset_refuses_during_checkin(tmp_path):
    class G:
        _checkin_lock = True
    with pytest.raises(RuntimeError):
        reset_ai_memory(tmp_path, grower=G())
