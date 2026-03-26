import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

SCHEDULE_FILE = "data/logs/schedule.json"


class Scheduler:
    def __init__(self, config: dict, base_dir: str):
        sched_config = config.get("scheduler", {})
        self.default_minutes = sched_config.get("default_checkin_minutes", 60)
        self.min_minutes = sched_config.get("min_checkin_minutes", 5)
        self.max_minutes = sched_config.get("max_checkin_minutes", 480)

        self.schedule_path = Path(base_dir) / SCHEDULE_FILE
        self.schedule_path.parent.mkdir(parents=True, exist_ok=True)

        self._timer: Optional[threading.Timer] = None
        self._callback: Optional[Callable] = None
        self._next_checkin: Optional[datetime] = None
        self._running = False

    def start(self, callback: Callable):
        self._callback = callback
        self._running = True

        saved = self._load_schedule()
        if saved and saved.get("next_checkin"):
            next_time = datetime.fromisoformat(saved["next_checkin"])
            remaining = (next_time - datetime.now()).total_seconds()
            if remaining > 0:
                logger.info("resuming saved schedule, %.0f min left", remaining / 60)
                self._set_timer(remaining)
                return

        logger.info("no saved schedule, starting now")
        self._set_timer(1)

    def schedule_checkin(self, minutes: int, reason: str = "") -> dict:
        minutes = max(self.min_minutes, min(self.max_minutes, minutes))
        self._next_checkin = datetime.now() + timedelta(minutes=minutes)

        if self._timer and self._timer.is_alive():
            self._timer.cancel()

        self._set_timer(minutes * 60)
        self._save_schedule(reason)

        logger.info("next checkin in %d min (%s), reason: %s",
                    minutes, self._next_checkin.strftime("%H:%M"), reason)
        return {
            "scheduled": True,
            "minutes": minutes,
            "next_checkin": self._next_checkin.isoformat(),
            "reason": reason,
        }

    def _set_timer(self, seconds: float):
        if self._timer and self._timer.is_alive():
            self._timer.cancel()
        self._timer = threading.Timer(seconds, self._on_timer)
        self._timer.daemon = True
        self._timer.start()

    def _on_timer(self):
        if self._running and self._callback:
            logger.info("checkin timer fired")
            try:
                self._callback()
            except Exception as e:
                logger.error("checkin callback error: %s", e)
                self._set_timer(300)  # retry in 5 min

    def get_next_checkin(self) -> Optional[str]:
        return self._next_checkin.isoformat() if self._next_checkin else None

    def get_minutes_until_checkin(self) -> Optional[float]:
        if self._next_checkin:
            remaining = (self._next_checkin - datetime.now()).total_seconds()
            return max(0, remaining / 60)
        return None

    def _save_schedule(self, reason: str = ""):
        data = {
            "next_checkin": self._next_checkin.isoformat() if self._next_checkin else None,
            "reason": reason,
            "saved_at": datetime.now().isoformat(),
        }
        with open(self.schedule_path, "w") as f:
            json.dump(data, f, indent=2)

    def _load_schedule(self) -> Optional[dict]:
        if self.schedule_path.exists():
            with open(self.schedule_path) as f:
                return json.load(f)
        return None

    def stop(self):
        self._running = False
        if self._timer and self._timer.is_alive():
            self._timer.cancel()
        logger.info("scheduler stopped")
