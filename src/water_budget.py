"""Hard daily water budget for the pump.

Enforced in Actuators.run_pump, so neither the AI nor the scheduler can
water past it. Doses are written to disk *before* the pump turns on, so
restarts and crashes can't reset the budget.

Two buckets, each over a rolling 24 h window:
  - "ai":     AI tool calls + scheduled pump actions (also a minimum gap)
  - "manual": website / local API / startup hardware test
"""

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DOSE_LOG_FILE = "data/pump_doses.json"
WINDOW_S = 24 * 3600

# Absolute ceilings in code: a typo in config.yaml / config.local.yaml can
# tighten the budget but never loosen it past these.
HARD_MAX_SECONDS_PER_DOSE = 60
HARD_MAX_AI_SECONDS_PER_DAY = 120
HARD_MIN_AI_GAP_MINUTES = 30
HARD_MAX_MANUAL_SECONDS_PER_DAY = 300

AI_SOURCES = {"ai", "scheduled"}

DEFAULTS = {
    "ai_max_seconds_per_day": 45,
    "ai_min_minutes_between_doses": 180,
    "manual_max_seconds_per_day": 120,
}


def bucket_for(source: str) -> str:
    return "ai" if source in AI_SOURCES else "manual"


class WaterBudget:
    def __init__(self, config: dict, base_dir: str | None = None):
        self.config = config
        self.path = Path(base_dir) / DOSE_LOG_FILE if base_dir else None
        self._lock = threading.Lock()
        self._doses: list[dict] = self._load()

    # ── limits ───────────────────────────────────────────────────────────────

    def limits(self) -> dict:
        pump_cfg = self.config.get("water_pump", {}) or {}
        cfg = {**DEFAULTS, **(pump_cfg.get("budget", {}) or {})}
        return {
            "max_seconds_per_dose": min(float(pump_cfg.get("max_seconds_per_dose", 45)),
                                        HARD_MAX_SECONDS_PER_DOSE),
            "min_seconds_per_dose": float(pump_cfg.get("min_seconds_per_dose", 1)),
            "ai_max_seconds_per_day": min(float(cfg["ai_max_seconds_per_day"]),
                                          HARD_MAX_AI_SECONDS_PER_DAY),
            "ai_min_minutes_between_doses": max(float(cfg["ai_min_minutes_between_doses"]),
                                                HARD_MIN_AI_GAP_MINUTES),
            "manual_max_seconds_per_day": min(float(cfg["manual_max_seconds_per_day"]),
                                              HARD_MAX_MANUAL_SECONDS_PER_DAY),
        }

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self) -> list[dict]:
        if not self.path or not self.path.exists():
            return []
        try:
            return list(json.loads(self.path.read_text()).get("doses", []))
        except Exception as e:
            # Unreadable log: fail closed for the AI by assuming the budget is
            # used up for the next 24 h, rather than silently resetting it.
            logger.error("pump dose log unreadable (%s); AI budget locked for 24h", e)
            return [{"epoch": time.time(), "seconds": HARD_MAX_AI_SECONDS_PER_DAY,
                     "source": "ai", "note": "dose log unreadable"}]

    def _save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"doses": self._doses}, indent=1))
        tmp.replace(self.path)

    def _prune(self, now: float):
        # keep 48 h so the website can show yesterday too
        self._doses = [d for d in self._doses if now - d["epoch"] < 2 * WINDOW_S]

    # ── API ──────────────────────────────────────────────────────────────────

    def _used(self, bucket: str, now: float) -> float:
        return sum(d["seconds"] for d in self._doses
                   if bucket_for(d["source"]) == bucket and now - d["epoch"] < WINDOW_S)

    def _last_ai_epoch(self) -> float | None:
        ai = [d["epoch"] for d in self._doses if bucket_for(d["source"]) == "ai"]
        return max(ai) if ai else None

    def reserve(self, seconds: float, source: str, reason: str = "") -> tuple[float, str | None]:
        """Check the budget and, if allowed, record the dose immediately.

        Returns (granted_seconds, None) or (0, why_blocked). granted may be
        less than requested when only part of the budget is left.
        """
        lim = self.limits()
        bucket = bucket_for(source)
        now = time.time()
        with self._lock:
            self._prune(now)
            requested = float(seconds)
            seconds = min(requested, lim["max_seconds_per_dose"])
            cap = lim["ai_max_seconds_per_day"] if bucket == "ai" else lim["manual_max_seconds_per_day"]
            remaining = cap - self._used(bucket, now)

            if bucket == "ai":
                last = self._last_ai_epoch()
                gap_s = lim["ai_min_minutes_between_doses"] * 60
                if last is not None and now - last < gap_s:
                    nxt = datetime.fromtimestamp(last + gap_s).isoformat(timespec="minutes")
                    return 0, (f"Water budget: doses must be at least "
                               f"{lim['ai_min_minutes_between_doses']:.0f} min apart. "
                               f"Next dose allowed at {nxt}. Do not retry before then.")

            if remaining < lim["min_seconds_per_dose"]:
                return 0, (f"Water budget: the daily limit of {cap:.0f} s "
                           f"({bucket} doses, rolling 24 h) is used up. "
                           f"Next dose allowed at {self._next_free(bucket, cap, now)}.")

            granted = round(max(lim["min_seconds_per_dose"], min(seconds, remaining)), 1)
            self._doses.append({
                "epoch": now,
                "at": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
                "seconds": granted,
                "requested": round(requested, 1),
                "source": source,
                "reason": (reason or "")[:200],
            })
            self._save()
        if granted < requested:
            logger.warning("water budget: %s dose cut from %.1fs to %.1fs", source, requested, granted)
        return granted, None

    def _next_free(self, bucket: str, cap: float, now: float) -> str:
        # earliest time enough old doses fall out of the window to free 1 s
        doses = sorted((d for d in self._doses
                        if bucket_for(d["source"]) == bucket and now - d["epoch"] < WINDOW_S),
                       key=lambda d: d["epoch"])
        used = sum(d["seconds"] for d in doses)
        for d in doses:
            used -= d["seconds"]
            if cap - used >= 1:
                return datetime.fromtimestamp(d["epoch"] + WINDOW_S).isoformat(timespec="minutes")
        return "unknown"

    def status(self) -> dict:
        lim = self.limits()
        now = time.time()
        with self._lock:
            self._prune(now)
            ai_used = self._used("ai", now)
            man_used = self._used("manual", now)
            last = self._last_ai_epoch()
            recent = [d for d in self._doses if now - d["epoch"] < WINDOW_S]
        gap_s = lim["ai_min_minutes_between_doses"] * 60
        next_ai = None
        if last is not None and now - last < gap_s:
            next_ai = datetime.fromtimestamp(last + gap_s).isoformat(timespec="minutes")
        flow = float(self.config.get("hardware_specs", {}).get("flow_rate_ml_s", 0) or 0)
        return {
            "limits": lim,
            "ai_used_s": round(ai_used, 1),
            "ai_remaining_s": round(max(0.0, lim["ai_max_seconds_per_day"] - ai_used), 1),
            "manual_used_s": round(man_used, 1),
            "manual_remaining_s": round(max(0.0, lim["manual_max_seconds_per_day"] - man_used), 1),
            "next_ai_dose_allowed_at": next_ai,
            "last_24h_ml": round((ai_used + man_used) * flow) if flow else None,
            "doses_24h": recent[-20:],
        }

    def prompt_summary(self) -> str:
        s = self.status()
        lim = s["limits"]
        line = (f"Water budget (enforced by the controller): {s['ai_used_s']:.0f} of "
                f"{lim['ai_max_seconds_per_day']:.0f} s used in the last 24 h, "
                f"{s['ai_remaining_s']:.0f} s left; max {lim['max_seconds_per_dose']:.0f} s per dose; "
                f"at least {lim['ai_min_minutes_between_doses']:.0f} min between doses.")
        if s["next_ai_dose_allowed_at"]:
            line += f" Next dose allowed at {s['next_ai_dose_allowed_at']}."
        return line
