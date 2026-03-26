import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

GROWTH_LOG_FILE = "data/logs/growth_log.jsonl"


class GrowthTracker:
    def __init__(self, base_dir: str):
        self.log_path = Path(base_dir) / GROWTH_LOG_FILE
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def record_measurement(self, day: int, stage: str,
                           measurements: dict, notes: str = "") -> dict:
        # measurements can include height_cm, leaf_count, stem_diameter_mm,
        # canopy_width_cm, health_score (1-10)
        entry = {
            "timestamp": datetime.now().isoformat(),
            "day": day,
            "stage": stage,
            "measurements": measurements,
            "notes": notes,
        }
        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

        logger.info("growth logged, day %d: %s", day, measurements)
        return entry

    def get_history(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        entries = []
        with open(self.log_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries

    def get_growth_rate(self, metric: str = "height_cm", last_n: int = 5) -> dict | None:
        history = self.get_history()
        relevant = [
            e for e in history
            if metric in e.get("measurements", {})
            and e["measurements"][metric] is not None
        ]

        if len(relevant) < 2:
            return None

        recent = relevant[-last_n:]
        first = recent[0]
        last = recent[-1]

        first_val = first["measurements"][metric]
        last_val = last["measurements"][metric]
        day_diff = last["day"] - first["day"]

        if day_diff <= 0:
            return None

        return {
            "metric": metric,
            "start_value": first_val,
            "end_value": last_val,
            "change": round(last_val - first_val, 2),
            "days": day_diff,
            "rate_per_day": round((last_val - first_val) / day_diff, 2),
            "samples_used": len(recent),
        }

    def get_latest(self) -> dict | None:
        history = self.get_history()
        return history[-1] if history else None

    def get_summary(self) -> dict:
        history = self.get_history()
        if not history:
            return {"total_measurements": 0}

        latest = history[-1]
        first = history[0]

        summary = {
            "total_measurements": len(history),
            "first_measurement_day": first["day"],
            "latest_measurement_day": latest["day"],
            "latest_measurements": latest.get("measurements", {}),
            "days_tracked": latest["day"] - first["day"],
        }

        for metric in ["height_cm", "leaf_count", "stem_diameter_mm"]:
            rate = self.get_growth_rate(metric)
            if rate:
                summary[f"{metric}_rate"] = rate

        return summary
