import json
import shutil
import subprocess
import sys
import textwrap

import pytest

import supervisor as sv


class FakeReporter:
    def __init__(self, on_status=None):
        self.statuses, self.commands = [], []
        self.on_status = on_status

    def status(self, s):
        self.statuses.append(s)
        if self.on_status:
            self.on_status(s)

    def command(self, cmd_id, update):
        self.commands.append((cmd_id, update))


def write_state(base, state):
    (base / "data").mkdir(exist_ok=True)
    (base / "data" / "update_state.json").write_text(json.dumps(state))


def test_rollback_files_restores_backup_or_removes_new_file(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "config.yaml").write_text("broken: [")
    (tmp_path / "data" / "old_config.yaml").write_text("good: 1\n")
    (tmp_path / "config.local.yaml").write_text("new: 1\n")
    msg = sv.rollback({"rollback": {"type": "files", "files": [
        {"path": "config.yaml", "backup": "data/old_config.yaml"},
        {"path": "config.local.yaml", "backup": None}]}}, tmp_path)
    assert (tmp_path / "config.yaml").read_text() == "good: 1\n"
    assert not (tmp_path / "config.local.yaml").exists()
    assert "restored" in msg and "removed" in msg


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_rollback_git(tmp_path):
    def git(*a):
        return subprocess.run(["git", *a], cwd=tmp_path, capture_output=True, text=True,
                              check=True).stdout.strip()
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "f.py").write_text("v1")
    git("add", ".")
    git("commit", "-qm", "1")
    old = git("rev-parse", "--short", "HEAD")
    (tmp_path / "f.py").write_text("v2")
    git("commit", "-qam", "2")
    sv.rollback({"rollback": {"type": "git", "commit": old}}, tmp_path)
    assert (tmp_path / "f.py").read_text() == "v1"


def test_crash_after_update_rolls_back_and_reports(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "config.local.yaml").write_text("bad: 1\n")
    write_state(tmp_path, {"state": "verifying", "kind": "config", "command_id": "c1",
                           "rollback": {"type": "files",
                                        "files": [{"path": "config.local.yaml", "backup": None}]}})
    rep = FakeReporter()
    sup = sv.Supervisor(tmp_path, cmd=["x"], reporter=rep)
    sup.tail.extend(["Traceback ...", "KeyError: 'plant'"])
    delay = sup.on_crash(1, ran_s=20)
    assert delay == 0
    assert not (tmp_path / "config.local.yaml").exists()
    state = json.loads((tmp_path / "data" / "update_state.json").read_text())
    assert state["state"] == "rolled_back"
    cmd_id, update = rep.commands[0]
    assert cmd_id == "c1" and update["status"] == "error" and "undone" in update["error"]
    assert rep.statuses[-1]["state"] == "crashed"
    assert rep.statuses[-1]["last_crash"]["log_tail"][-1] == "KeyError: 'plant'"


def test_late_crash_is_not_rolled_back_and_backs_off(tmp_path):
    write_state(tmp_path, {"state": "success", "kind": "code",
                           "rollback": {"type": "git", "commit": "abc"}})
    rep = FakeReporter()
    sup = sv.Supervisor(tmp_path, cmd=["x"], reporter=rep)
    delays = [sup.on_crash(1, ran_s=30) for _ in range(4)]
    assert delays == [5, 10, 20, 40]
    assert rep.commands == []
    assert sup.on_crash(1, ran_s=sv.STABLE_RUN_S + 1) == 5     # long run resets backoff


def test_loop_restarts_on_42_and_marks_restarts(tmp_path):
    child = textwrap.dedent("""
        import os, sys, pathlib
        p = pathlib.Path("runs.txt")
        n = len(p.read_text().splitlines()) if p.exists() else 0
        with open(p, "a") as f:
            f.write(os.environ.get("VFARM_RESTART", "-") + "\\n")
        print("child run", n, flush=True)
        sys.exit([42, 3, 42][min(n, 2)])
    """)
    (tmp_path / "config.local.yaml").write_text("bad: 1\n")
    write_state(tmp_path, {"state": "restarting", "kind": "config", "command_id": "c9",
                           "rollback": {"type": "files",
                                        "files": [{"path": "config.local.yaml", "backup": None}]}})
    sup = None

    def on_status(s):
        if s["state"] == "starting" and s["restarts"] >= 2:
            sup.stopping = True

    rep = FakeReporter(on_status)
    sup = sv.Supervisor(tmp_path, cmd=[sys.executable, "-c", child], reporter=rep)
    sup.loop()
    runs = (tmp_path / "runs.txt").read_text().split()
    assert runs == ["-", "1", "1"]           # only the first start is a cold start
    assert not (tmp_path / "config.local.yaml").exists()      # crash -> rolled back
    assert rep.commands and rep.commands[0][0] == "c9"
    assert rep.statuses[-1]["state"] == "stopped"
