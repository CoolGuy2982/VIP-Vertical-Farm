import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


DECISIONS_FILE = "data/logs/decisions.jsonl"
MILESTONES_FILE = "data/logs/milestones.jsonl"
GROWTH_SUMMARY_FILE = "data/logs/growth_summary.json"
SENSOR_LOG_FILE = "data/logs/sensor_log.jsonl"
PROMPT_TEMPLATE_FILE = "prompts/system_prompt.md"          # default, in git
PROMPT_OVERRIDE_FILE = "prompts/system_prompt.local.md"    # website edits, not in git

FALLBACK_PROMPT = (
    "You are an AI grower for a {variety} on day {day} ({stage_name} stage). "
    "Read the dashboard camera and call report_sensors, assess the plant camera image, "
    "water and light conservatively, and schedule the next check-in. "
    "(The prompt template file is missing; this is a minimal fallback.)"
)

logger = logging.getLogger(__name__)


class ContextManager:
    def __init__(self, base_dir: str, config: dict):
        self.base_dir = Path(base_dir)
        self.config = config
        self.ctx_config = config.get("context", {})

        self.decisions_path = self.base_dir / DECISIONS_FILE
        self.milestones_path = self.base_dir / MILESTONES_FILE
        self.summary_path = self.base_dir / GROWTH_SUMMARY_FILE
        self.sensor_log_path = self.base_dir / SENSOR_LOG_FILE

        for p in [self.decisions_path, self.milestones_path,
                  self.summary_path, self.sensor_log_path]:
            p.parent.mkdir(parents=True, exist_ok=True)

    # decision log

    def log_decision(self, decision: dict):
        # expected keys: timestamp, day, sensors, observation, reasoning, actions, next_checkin_minutes
        decision.setdefault("timestamp", datetime.now().isoformat())
        with open(self.decisions_path, "a") as f:
            f.write(json.dumps(decision) + "\n")

    def get_recent_decisions(self, count: Optional[int] = None) -> list[dict]:
        count = count or self.ctx_config.get("max_recent_decisions", 15)
        if not self.decisions_path.exists():
            return []
        decisions = []
        with open(self.decisions_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    decisions.append(json.loads(line))
        return decisions[-count:]

    def get_all_decisions(self) -> list[dict]:
        if not self.decisions_path.exists():
            return []
        decisions = []
        with open(self.decisions_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    decisions.append(json.loads(line))
        return decisions

    def get_decision_count(self) -> int:
        if not self.decisions_path.exists():
            return 0
        count = 0
        with open(self.decisions_path) as f:
            for line in f:
                if line.strip():
                    count += 1
        return count

    # milestones

    def log_milestone(self, milestone: dict):
        # e.g. "Day 5: first true leaves appeared"
        # optional measurements dict: height_cm, leaf_count, etc.
        milestone.setdefault("timestamp", datetime.now().isoformat())
        with open(self.milestones_path, "a") as f:
            f.write(json.dumps(milestone) + "\n")

    def get_milestones(self) -> list[dict]:
        if not self.milestones_path.exists():
            return []
        milestones = []
        with open(self.milestones_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    milestones.append(json.loads(line))
        return milestones

    # growth summary (AI writes this itself every 24h)

    def get_growth_summary(self) -> dict:
        if not self.summary_path.exists():
            return {
                "last_updated": None,
                "summary": "No growth history yet. This is a new plant.",
                "key_learnings": [],
                "current_stage": "germination",
                "days_in_current_stage": 0,
            }
        with open(self.summary_path) as f:
            return json.load(f)

    def save_growth_summary(self, summary: dict):
        summary["last_updated"] = datetime.now().isoformat()
        with open(self.summary_path, "w") as f:
            json.dump(summary, f, indent=2)

    def needs_compression(self) -> bool:
        summary = self.get_growth_summary()
        if summary["last_updated"] is None:
            return self.get_decision_count() >= 10

        last_updated = datetime.fromisoformat(summary["last_updated"])
        hours = self.ctx_config.get("compression_interval_hours", 24)
        return datetime.now() - last_updated > timedelta(hours=hours)

    # sensor log

    def log_sensors(self, readings: dict):
        entry = {"timestamp": datetime.now().isoformat(), **readings}
        with open(self.sensor_log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def get_sensor_trends(self, hours: Optional[int] = None) -> dict:
        hours = hours or self.ctx_config.get("sensor_trend_hours", 24)
        cutoff = datetime.now() - timedelta(hours=hours)

        if not self.sensor_log_path.exists():
            return {}

        readings = []
        with open(self.sensor_log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                ts = datetime.fromisoformat(entry["timestamp"])
                if ts >= cutoff:
                    readings.append(entry)

        if not readings:
            return {}

        sensor_keys = [
            k for k in readings[0]
            if k != "timestamp" and isinstance(readings[0][k], (int, float))
        ]
        trends = {}
        for key in sensor_keys:
            values = [r[key] for r in readings if key in r and r[key] is not None]
            if values:
                trends[key] = {
                    "min": round(min(values), 1),
                    "max": round(max(values), 1),
                    "avg": round(sum(values) / len(values), 1),
                    "current": round(values[-1], 1),
                    "samples": len(values),
                }
        return trends

    @staticmethod
    def _slim_decision_for_prompt(d: dict) -> dict:
        # Keep only fields useful for downstream reasoning. Drop bulky
        # system_prompt / user_raw_message / full_response / thoughts that
        # legacy records may still contain on disk.
        actions = []
        for a in d.get("actions", []) or []:
            actions.append({
                "tool": a.get("tool"),
                "args": a.get("args"),
            })
        return {
            "timestamp": d.get("timestamp"),
            "day": d.get("day"),
            "trigger_type": d.get("trigger_type"),
            "sensors": d.get("sensors"),
            "observation": d.get("observation"),
            "reasoning": d.get("reasoning"),
            "outcome": d.get("outcome"),
            "actions": actions,
        }

    # context assembly

    def get_days_since_planting(self) -> int:
        planted = self.config.get("plant", {}).get("planted_date")
        if not planted:
            return 0
        planted_date = datetime.strptime(planted, "%Y-%m-%d").date()
        return (datetime.now().date() - planted_date).days

    def get_current_growth_stage(self) -> dict:
        day = self.get_days_since_planting()
        stages = self.config.get("plant", {}).get("growth_stages", [])
        for stage in stages:
            low, high = stage["typical_days"]
            if low <= day <= high:
                return stage
        return stages[-1] if stages else {}

    # system prompt (template in prompts/, editable from the website)

    def prompt_template_paths(self) -> tuple[Path, Path]:
        return (self.base_dir / PROMPT_TEMPLATE_FILE, self.base_dir / PROMPT_OVERRIDE_FILE)

    def get_prompt_template(self) -> tuple[str, str]:
        """(template text, which file it came from: "override", "default" or "builtin")."""
        default, override = self.prompt_template_paths()
        for path, label in ((override, "override"), (default, "default")):
            try:
                return path.read_text(encoding="utf-8"), label
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.error("could not read prompt template %s: %s", path, e)
        return FALLBACK_PROMPT, "builtin"

    def prompt_variables(self) -> dict:
        plant = self.config.get("plant", {})
        stage = self.get_current_growth_stage()
        day = self.get_days_since_planting()
        h_specs = self.config.get("hardware_specs", {})
        if day <= 6:
            stage_guidance = (
                "- **Early germination (Days 1-6)**: No visible sprouts yet - the rockwool"
                " texture can look like sprouts but is not. Focus on keeping rockwool dark/moist"
                " and establishing baseline sensor readings. Do NOT log germination milestones"
                " until Day 7+ with clear visual evidence.\n"
                "- Calibrate your mental model of the hardware: pump rate, light response, sensor read cadence."
            )
        else:
            stage_guidance = (
                "- **Active growth phase**: Sprouts and seedlings may be visible. Compare current"
                " plant images to historical photos to track progress. Look for: new leaf emergence,"
                " stem elongation, colour changes, and any stress signals. Log milestones with"
                " measurements whenever visible growth is confirmed."
            )

        return {
            "variety": plant.get("variety", "plant"),
            "species": plant.get("species", "plant"),
            "planted_date": plant.get("planted_date", "unknown"),
            "day": day,
            "stage_name": stage.get("name", "unknown"),
            "ideal_temp_c": stage.get("ideal_temp_c", "N/A"),
            "ideal_humidity_pct": stage.get("ideal_humidity_pct", "N/A"),
            "ideal_soil_moisture_pct": stage.get("ideal_soil_moisture_pct", "N/A"),
            "light_hours": stage.get("light_hours", "N/A"),
            "tray_tilt_degrees": h_specs.get("tray_tilt_degrees", 0),
            "flow_rate": h_specs.get("flow_rate_ml_s", 1),
            "stage_guidance": stage_guidance,
        }

    @staticmethod
    def render_prompt(template: str, variables: dict) -> str:
        # Drop the leading <!-- ... --> note, then fill {name} placeholders.
        # Anything else in braces is left alone, so edits can't break rendering.
        text = re.sub(r"\A\s*<!--.*?-->\s*", "", template, flags=re.S)
        return re.sub(r"\{([a-z_]+)\}",
                      lambda m: str(variables[m.group(1)]) if m.group(1) in variables else m.group(0),
                      text)

    def hard_limits_section(self) -> str:
        from src.water_budget import WaterBudget
        wb = WaterBudget(self.config).limits()
        flow_rate = self.config.get("hardware_specs", {}).get("flow_rate_ml_s", 1)
        return (
            "## Hard Limits (enforced by the controller, you cannot change them)\n"
            f"- Water budget: at most {wb['max_seconds_per_dose']:.0f} s per dose, at most "
            f"{wb['ai_max_seconds_per_day']:.0f} s of pumping per rolling 24 h (about "
            f"{wb['ai_max_seconds_per_day'] * flow_rate / 1000:.1f} L), and at least "
            f"{wb['ai_min_minutes_between_doses']:.0f} minutes between doses. A dose over the "
            "limit is shortened or refused, and the tool result says when the next dose is "
            "allowed. Plan for about one full watering per day. A refused dose is not an "
            "emergency: wait for the next allowed time and do not retry before then."
        )

    def build_system_prompt(self) -> str:
        template, _ = self.get_prompt_template()
        return (self.render_prompt(template, self.prompt_variables()).rstrip()
                + "\n\n" + self.hard_limits_section())

    def build_context_message(self, current_sensors: dict, sensor_trends: dict,
                               pending_actions: list = None,
                               trigger_context: str = None,
                               water_budget: str = None) -> str:
        summary = self.get_growth_summary()
        recent = self.get_recent_decisions()
        milestones = self.get_milestones()
        day = self.get_days_since_planting()

        parts = []

        if trigger_context:
            parts.append(f"## Trigger Context\n{trigger_context}\n")

        parts.append("## Grow History Summary")
        parts.append(summary.get("summary", "No history yet, this is the first check-in."))
        if summary.get("key_learnings"):
            parts.append("\n**Learned so far:**")
            for learning in summary["key_learnings"]:
                parts.append(f"- {learning}")
        if summary.get("patterns"):
            parts.append("\n**Observed patterns:**")
            for k, v in summary["patterns"].items():
                parts.append(f"- {k}: {v}")

        if milestones:
            parts.append("\n## Growth Timeline")
            for m in milestones[-12:]:
                meas = m.get("measurements", {})
                meas_str = ""
                if meas:
                    meas_str = " [" + ", ".join(f"{k}={v}" for k, v in meas.items()) + "]"
                parts.append(f"- Day {m.get('day', '?')}: {m.get('description', '')}{meas_str}")

        if sensor_trends:
            parts.append("\n## Sensor Trends (last 24h)")
            for key, vals in sensor_trends.items():
                trend_dir = ""
                if vals["current"] > vals["avg"] + 2:
                    trend_dir = " (rising)"
                elif vals["current"] < vals["avg"] - 2:
                    trend_dir = " (falling)"
                parts.append(
                    f"- {key}: avg={vals['avg']}, min={vals['min']}, "
                    f"max={vals['max']}, now={vals['current']}{trend_dir}"
                )

        parts.append(f"\n## Current State - Day {day} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        for key, val in current_sensors.items():
            if key not in ("timestamp", "errors"):
                parts.append(f"- {key}: {val}")
        errors = current_sensors.get("errors", [])
        if errors:
            parts.append(f"- SENSOR ERRORS: {errors}")
        if water_budget:
            parts.append(f"- {water_budget}")

        if pending_actions:
            parts.append("\n## Pending Scheduled Actions")
            for a in pending_actions:
                parts.append(
                    f"- [{a['action_type']}] id={a['id']} "
                    f"in {a.get('seconds_until_fire', '?')}s, {a.get('reason', '')} "
                    f"| context: {a.get('context', '')}"
                )

        if recent:
            parts.append(f"\n## Recent Decisions (last {len(recent)})")
            for d in recent:
                trigger_tag = f" [{d.get('trigger_type', 'checkin')}]" if d.get("trigger_type") else ""
                parts.append(f"\n### {d.get('timestamp', '?')} day {d.get('day', '?')}{trigger_tag}")
                if d.get("observation"):
                    parts.append(f"**Observation**: {d['observation']}")
                if d.get("reasoning"):
                    parts.append(f"**Reasoning**: {d['reasoning']}")
                actions = d.get("actions", [])
                if actions:
                    for a in actions:
                        parts.append(f"  -> {a['tool']}({json.dumps(a.get('args', {}))})")
                if d.get("outcome"):
                    parts.append(f"**Outcome**: {d['outcome']}")

        return "\n".join(parts)

    def build_compression_prompt(self, max_decisions: int = 40) -> str:
        all_decisions = self.get_all_decisions()
        # Only summarize the most recent slice — older history is already
        # absorbed into current_summary. Sending the full log here used to push
        # the compression call past Gemini's 1M token ceiling.
        recent_decisions = all_decisions[-max_decisions:]
        milestones = self.get_milestones()
        current_summary = self.get_growth_summary()

        slim_decisions = [self._slim_decision_for_prompt(d) for d in recent_decisions]
        slim_milestones = milestones[-30:]

        return f"""You are reviewing your own growing history to write a compressed summary.
This summary will be your memory going forward, so make it useful.

## Current Summary
{current_summary.get('summary', 'None yet.')}

## Recent Milestones (last {len(slim_milestones)})
{json.dumps(slim_milestones, indent=2)}

## Recent Decision Log ({len(slim_decisions)} of {len(all_decisions)} total decisions; older history is already in the summary above)
{json.dumps(slim_decisions, indent=2)}

## Your Task
Write an updated summary that captures:
1. Timeline of key events and stage transitions
2. What you've learned about this specific plant's needs
3. What worked and what didn't
4. Where the plant is right now
5. Key actionable insights as bullet points

Respond with a JSON object only, no markdown:
{{
  "summary": "2-4 paragraph narrative of the grow so far",
  "key_learnings": ["learning 1", "learning 2", ...],
  "current_stage": "stage name",
  "days_in_current_stage": N,
  "patterns": {{
    "watering_frequency": "what you observed",
    "light_response": "how the plant responds to light changes",
    "growth_rate": "how fast its growing",
    "problem_areas": "recurring issues if any"
  }}
}}"""
