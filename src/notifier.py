"""Email alerts from the Pi when something goes wrong.

Sent for: the AI calling emergency_alert, problems the website banner shows
(camera failing, check-ins failing, under-voltage, disk, overheating, repeated
errors) plus a "resolved" note when they clear, and crashes (supervisor.py).

Uses Gmail SMTP with an app password (Google account -> Security -> App
passwords). Put in ~/VIP-Vertical-Farm/.env:
    ALERT_SMTP_USER=vip.vf.purdue@gmail.com
    ALERT_SMTP_PASSWORD=<16-character app password>
Recipient and limits: config.yaml  alerts.email. Without the two .env lines,
nothing is sent (logged once).

Spam guard: the same alert is repeated at most every min_hours_between_repeats
(default 6 h), and at most max_per_day emails (default 12) go out per day. The
state is kept in data/alert_state.json so restarts don't reset it.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import smtplib
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = "data/alert_state.json"


def send_email(host: str, port: int, user: str, password: str, to: str,
               subject: str, body: str, timeout: float = 20) -> None:
    msg = EmailMessage()
    msg["From"] = f"VIP Vertical Farm <{user}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL(host, port, timeout=timeout) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)


class Notifier:
    def __init__(self, config: dict, base_dir: str | Path | None = None):
        cfg = ((config.get("alerts", {}) or {}).get("email", {}) or {})
        self.to = str(cfg.get("to") or os.environ.get("ALERT_EMAIL_TO", "")).strip()
        self.host = cfg.get("smtp_host", "smtp.gmail.com")
        self.port = int(cfg.get("smtp_port", 465))
        self.repeat_s = 3600 * float(cfg.get("min_hours_between_repeats", 6))
        self.max_per_day = int(cfg.get("max_per_day", 12))
        self.website = str(cfg.get("website_url") or "")
        self.enabled = cfg.get("enabled", True) is not False
        self.device = (config.get("firebase", {}) or {}).get("device_id", "raspPi4")
        self.state_path = Path(base_dir) / STATE_FILE if base_dir else None
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._warned = False
        self.last_error: str | None = None

    # -- config ---------------------------------------------------------------

    @property
    def user(self) -> str:
        return os.environ.get("ALERT_SMTP_USER", "").strip()

    @property
    def password(self) -> str:
        # Gmail shows app passwords in groups of four; spaces are not part of it
        return os.environ.get("ALERT_SMTP_PASSWORD", "").replace(" ", "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.to and self.user and self.password)

    def describe(self) -> dict:
        return {"configured": self.configured, "to": self.to if self.configured else None,
                "last_error": self.last_error}

    # -- state ----------------------------------------------------------------

    def _load(self) -> dict:
        if not self.state_path:
            return getattr(self, "_mem_state", {"sent": {}, "day": "", "count": 0})
        try:
            return json.loads(self.state_path.read_text())
        except Exception:
            return {"sent": {}, "day": "", "count": 0}

    def _save(self, state: dict):
        if not self.state_path:
            self._mem_state = state
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(state))
        except Exception as e:
            logger.debug("could not save alert state: %s", e)

    def was_sent(self, key: str) -> bool:
        with self._lock:
            return key in self._load().get("sent", {})

    def forget(self, key: str):
        with self._lock:
            state = self._load()
            state.get("sent", {}).pop(key, None)
            self._save(state)

    # -- sending --------------------------------------------------------------

    def notify(self, key: str, subject: str, body: str, repeat_hours: float | None = None) -> bool:
        """Queue an alert email unless the same key was sent recently. True if queued."""
        if not self.configured:
            if not self._warned:
                self._warned = True
                logger.info("Email alerts not set up (ALERT_SMTP_USER / ALERT_SMTP_PASSWORD "
                            "in .env); not emailing: %s", subject)
            return False
        repeat_s = self.repeat_s if repeat_hours is None else 3600 * repeat_hours
        now = time.time()
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            state = self._load()
            if state.get("day") != today:
                state["day"], state["count"] = today, 0
            last = state.setdefault("sent", {}).get(key)
            if last and now - last < repeat_s:
                return False
            if state["count"] >= self.max_per_day:
                logger.warning("Email alert limit (%d/day) reached; not emailing: %s",
                               self.max_per_day, subject)
                return False
            state["sent"][key] = now
            state["count"] += 1
            self._save(state)
        self._queue.put((subject, body))
        self._ensure_worker()
        return True

    def send_now(self, subject: str, body: str) -> None:
        """Send immediately (the website's test button). Raises on failure."""
        if not self.configured:
            raise RuntimeError("Email alerts are not set up: add ALERT_SMTP_USER and "
                               "ALERT_SMTP_PASSWORD to the Pi's .env and restart the grower")
        send_email(self.host, self.port, self.user, self.password, self.to,
                   self._subject(subject), self._body(body))
        self.last_error = None

    def _subject(self, subject: str) -> str:
        return f"[VIP Farm] {subject}"[:200]

    def _body(self, body: str) -> str:
        footer = f"\n\n--\nSent by the grower on {self.device} at {datetime.now():%Y-%m-%d %H:%M}."
        if self.website:
            footer += f"\nDashboard: {self.website}"
        return body.strip() + footer

    def _ensure_worker(self):
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run, name="email-alerts", daemon=True)
            self._worker.start()

    def _run(self):
        while True:
            try:
                subject, body = self._queue.get(timeout=60)
            except queue.Empty:
                return
            for attempt in range(3):
                try:
                    send_email(self.host, self.port, self.user, self.password, self.to,
                               self._subject(subject), self._body(body))
                    logger.info("alert email sent: %s", subject)
                    self.last_error = None
                    break
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"[:300]
                    time.sleep(10 * (attempt + 1))
            else:
                # warning, not error: an error here would itself become a "problem" alert
                logger.warning("could not send alert email (%s): %s", self.last_error, subject)
