import time
from types import SimpleNamespace

import pytest

from src import notifier as nt
from src.notifier import Notifier

CFG = {"alerts": {"email": {"to": "team@example.com", "min_hours_between_repeats": 6,
                            "max_per_day": 3, "website_url": "https://farm.example"}},
       "firebase": {"device_id": "testPi"}}


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setenv("ALERT_SMTP_USER", "farm@example.com")
    monkeypatch.setenv("ALERT_SMTP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setattr(nt, "send_email", lambda *a, **k: out.append(a))
    return out


def wait_for(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_sends_once_then_waits_before_repeating(tmp_path, sent):
    n = Notifier(CFG, tmp_path)
    assert n.notify("problem:camera_plant", "Plant camera failing", "details")
    assert not n.notify("problem:camera_plant", "Plant camera failing", "again")   # within 6 h
    assert wait_for(lambda: len(sent) == 1)
    host, port, user, password, to, subject, body = sent[0]
    assert password == "abcdefghijklmnop"               # Gmail's display spaces removed
    assert to == "team@example.com" and subject == "[VIP Farm] Plant camera failing"
    assert "https://farm.example" in body and "testPi" in body
    # survives a restart
    assert not Notifier(CFG, tmp_path).notify("problem:camera_plant", "x", "y")


def test_daily_cap(tmp_path, sent):
    n = Notifier(CFG, tmp_path)
    results = [n.notify(f"k{i}", "s", "b") for i in range(5)]
    assert results == [True, True, True, False, False]


def test_not_configured_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("ALERT_SMTP_USER", raising=False)
    monkeypatch.delenv("ALERT_SMTP_PASSWORD", raising=False)
    monkeypatch.setattr(nt, "send_email", lambda *a, **k: pytest.fail("sent"))
    n = Notifier(CFG, tmp_path)
    assert not n.configured and not n.notify("k", "s", "b")
    with pytest.raises(RuntimeError, match="not set up"):
        n.send_now("Test", "b")


def test_ai_alert_emails(tmp_path, sent):
    from src.ai_grower import AIGrower
    from src.sensors import Sensors
    g = AIGrower.__new__(AIGrower)
    g._alert_log = []
    g.firebase = SimpleNamespace(log_alert=lambda a: None)
    g.context = SimpleNamespace(get_days_since_planting=lambda: 7)
    g.sensors = Sensors({})
    g.notifier = Notifier(CFG, tmp_path)
    r = g._record_alert("Water pooling on the tray, possible leak", "critical")
    assert r["emailed"] is True
    assert wait_for(lambda: len(sent) == 1)
    assert sent[0][5].startswith("[VIP Farm] CRITICAL: Water pooling")


def test_problem_watcher_waits_then_emails_and_reports_resolved(tmp_path, sent, monkeypatch):
    from src import remote_control as rc
    problems = [{"id": "camera_plant", "severity": "error", "title": "Plant camera is not returning images",
                 "detail": "unplugged"}, {"id": "paused", "severity": "warning", "title": "Standby"}]
    remote = rc.RemoteControl.__new__(rc.RemoteControl)
    remote.grower = SimpleNamespace(notifier=Notifier(CFG, tmp_path))
    remote._problem_first_seen = {}
    monkeypatch.setattr(remote, "_problems", lambda: list(problems))
    clock = {"t": 1000.0}
    monkeypatch.setattr(rc.time, "time", lambda: clock["t"])
    remote._email_problems()                       # just appeared: grace period
    assert not remote.grower.notifier.was_sent("problem:camera_plant")
    clock["t"] += 61
    remote._email_problems()
    assert remote.grower.notifier.was_sent("problem:camera_plant")
    assert not remote.grower.notifier.was_sent("problem:paused")       # standby is deliberate
    problems.pop(0)                                # camera fixed
    clock["t"] += 15
    remote._email_problems()
    assert wait_for(lambda: len(sent) == 2)
    assert "Resolved" in sent[1][5]
    assert not remote.grower.notifier.was_sent("problem:camera_plant")  # next time alerts again


def test_supervisor_crash_email_once_per_half_hour(tmp_path, monkeypatch):
    import supervisor as sv
    (tmp_path / "config.yaml").write_text("alerts:\n  email:\n    to: team@example.com\n")
    (tmp_path / ".env").write_text("ALERT_SMTP_USER=farm@example.com\nALERT_SMTP_PASSWORD=pw\n")
    calls = []
    monkeypatch.setattr(sv.threading, "Thread",
                        lambda target, daemon=True: SimpleNamespace(start=lambda: calls.append(1)))
    m = sv.CrashMailer(tmp_path)
    m.send("Grower crashed", "tail")
    m.send("Grower crashed", "tail again")
    assert calls == [1]
