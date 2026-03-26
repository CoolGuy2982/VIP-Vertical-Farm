import heapq
import json
import logging
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

QUEUE_FILE = "data/logs/action_queue.json"


class ScheduledAction:
    def __init__(self, action_id: str, action_type: str, fire_at: float,
                 params: dict, reason: str, context: str = ""):
        self.id = action_id
        self.action_type = action_type
        self.fire_at = fire_at
        self.params = params
        self.reason = reason
        self.context = context
        self.created_at = datetime.now().isoformat()

    def __lt__(self, other):
        return self.fire_at < other.fire_at

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "action_type": self.action_type,
            "fire_at": self.fire_at,
            "fire_at_readable": datetime.fromtimestamp(self.fire_at).isoformat(),
            "params": self.params,
            "reason": self.reason,
            "context": self.context,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ScheduledAction":
        a = cls(
            action_id=d["id"],
            action_type=d["action_type"],
            fire_at=d["fire_at"],
            params=d["params"],
            reason=d["reason"],
            context=d.get("context", ""),
        )
        a.created_at = d.get("created_at", a.created_at)
        return a


class ActionScheduler:
    # Min-heap of ScheduledAction objects sorted by fire_at timestamp.
    # Supported action types:
    #   checkin       - triggers a full AI check-in
    #   observe       - takes photo + sensors and calls AI with before/after diff
    #   run_pump      - runs the pump relay for N seconds
    #   turn_on_lights - turns lights on for N minutes
    #   turn_off_lights - turns lights off
    # Queue is saved to disk so it survives reboots.

    def __init__(self, config: dict, base_dir: str):
        sched_config = config.get("scheduler", {})
        self.min_checkin_minutes = sched_config.get("min_checkin_minutes", 5)
        self.max_checkin_minutes = sched_config.get("max_checkin_minutes", 480)

        self.queue_path = Path(base_dir) / QUEUE_FILE
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)

        self._heap: list[ScheduledAction] = []
        self._lock = threading.Lock()
        self._handlers: dict[str, Callable] = {}
        self._running = False
        self._worker: Optional[threading.Thread] = None
        self._cancelled: set[str] = set()

        self._load_saved_queue()

    def register_handler(self, action_type: str, handler: Callable):
        self._handlers[action_type] = handler

    def start(self):
        self._running = True
        self._worker = threading.Thread(target=self._run_loop, daemon=True)
        self._worker.start()
        logger.info("scheduler started, %d actions pending", len(self._heap))

    def stop(self):
        self._running = False
        logger.info("scheduler stopped")

    def schedule(self, action_type: str, delay_seconds: float,
                 params: dict = None, reason: str = "",
                 context: str = "") -> ScheduledAction:
        action = ScheduledAction(
            action_id=str(uuid.uuid4())[:8],
            action_type=action_type,
            fire_at=time.time() + delay_seconds,
            params=params or {},
            reason=reason,
            context=context,
        )
        with self._lock:
            heapq.heappush(self._heap, action)
        self._save_queue()

        logger.info("queued [%s] id=%s in %.0fs, %s",
                    action_type, action.id, delay_seconds, reason)
        return action

    def schedule_checkin(self, minutes: int, reason: str = "") -> ScheduledAction:
        minutes = max(self.min_checkin_minutes, min(self.max_checkin_minutes, minutes))
        return self.schedule("checkin", minutes * 60, reason=reason)

    def schedule_observe(self, delay_minutes: float, context: str,
                         before_sensors: dict = None) -> ScheduledAction:
        return self.schedule(
            "observe",
            delay_minutes * 60,
            params={
                "before_sensors": before_sensors or {},
                "scheduled_at": datetime.now().isoformat(),
            },
            reason=f"observation in {delay_minutes:.1f} min",
            context=context,
        )

    def cancel(self, action_id: str) -> bool:
        self._cancelled.add(action_id)
        self._save_queue()
        logger.info("cancelled action %s", action_id)
        return True

    def get_pending(self) -> list[dict]:
        now = time.time()
        with self._lock:
            return [
                {**a.to_dict(), "seconds_until_fire": max(0, round(a.fire_at - now))}
                for a in sorted(self._heap, key=lambda x: x.fire_at)
                if a.id not in self._cancelled
            ]

    def get_next_checkin(self) -> Optional[str]:
        upcoming = [a for a in self._heap
                    if a.action_type == "checkin" and a.id not in self._cancelled]
        if upcoming:
            next_one = min(upcoming, key=lambda a: a.fire_at)
            return datetime.fromtimestamp(next_one.fire_at).isoformat()
        return None

    def get_minutes_until_checkin(self) -> Optional[float]:
        upcoming = [a for a in self._heap
                    if a.action_type == "checkin" and a.id not in self._cancelled]
        if upcoming:
            next_one = min(upcoming, key=lambda a: a.fire_at)
            return max(0, (next_one.fire_at - time.time()) / 60)
        return None

    def _run_loop(self):
        while self._running:
            now = time.time()
            fired = []

            with self._lock:
                while self._heap and self._heap[0].fire_at <= now:
                    action = heapq.heappop(self._heap)
                    if action.id not in self._cancelled:
                        fired.append(action)

            for action in fired:
                self._fire(action)

            if fired:
                self._save_queue()

            time.sleep(1)

    def _fire(self, action: ScheduledAction):
        handler = self._handlers.get(action.action_type)
        if not handler:
            logger.warning("no handler registered for type: %s", action.action_type)
            return

        logger.info("firing [%s] id=%s, %s", action.action_type, action.id, action.reason)
        try:
            handler(action)
        except Exception as e:
            logger.error("handler failed [%s]: %s", action.action_type, e, exc_info=True)

    def _save_queue(self):
        with self._lock:
            data = {
                "saved_at": datetime.now().isoformat(),
                "actions": [
                    a.to_dict() for a in self._heap
                    if a.id not in self._cancelled and a.fire_at > time.time()
                ],
            }
        with open(self.queue_path, "w") as f:
            json.dump(data, f, indent=2)

    def _load_saved_queue(self):
        if not self.queue_path.exists():
            return
        try:
            with open(self.queue_path) as f:
                data = json.load(f)
            now = time.time()
            loaded = 0
            for d in data.get("actions", []):
                if d["fire_at"] > now:
                    action = ScheduledAction.from_dict(d)
                    heapq.heappush(self._heap, action)
                    loaded += 1
            if loaded:
                logger.info("loaded %d saved actions from disk", loaded)
        except Exception as e:
            logger.warning("could not load saved queue: %s", e)
