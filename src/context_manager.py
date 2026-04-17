import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


DECISIONS_FILE = "data/logs/decisions.jsonl"
MILESTONES_FILE = "data/logs/milestones.jsonl"
GROWTH_SUMMARY_FILE = "data/logs/growth_summary.json"
SENSOR_LOG_FILE = "data/logs/sensor_log.jsonl"


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

    def build_system_prompt(self) -> str:
        plant = self.config.get("plant", {})
        stage = self.get_current_growth_stage()
        day = self.get_days_since_planting()
        h_specs = self.config.get("hardware_specs", {})
        flow_rate = h_specs.get("flow_rate_ml_s", 1)

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

        return f"""You are an elite AI agricultural scientist and master grower. Your mission: grow a superior {plant.get('variety', 'plant')} that outperforms any human-grown specimen. You have autonomous control over the environment and are expected to use it with expert precision.

## Current Plant Profile
- Cultivar: {plant.get('variety', 'Unknown')}
- Day {day} of grow (planted {plant.get('planted_date', 'unknown')})
- Stage: {stage.get('name', 'unknown')}
- Target temp: {stage.get('ideal_temp_c', 'N/A')}C | Target RH: {stage.get('ideal_humidity_pct', 'N/A')}% | Target soil moisture: {stage.get('ideal_soil_moisture_pct', 'N/A')}% | Target photoperiod: {stage.get('light_hours', 'N/A')}h/day

## Structured Tray Map (18 x 8 Grid)
The tray is organized into 18 rows of 8 cells each. Seed distribution (2 rows per variety):
- Rows 1-2: Iceberg
- Rows 3-4: Bittercrunch
- Rows 5-6: Grand Rapids
- Rows 7-8: Cinnamon Romaine
- Rows 9-10: Bibb
- Rows 11-12: Boston
- Rows 13-14: Waldmanns Green
- Rows 15-16: Oakleaf
- Rows 17-18: Parris Island

## Hardware Constraints & Calculations
- **Tray Tilt ({h_specs.get('tray_tilt_degrees', 0)}°)**: Due to this incline, the top rows (Rows 1-6) dry twice as fast as the bottom collection zone (Rows 13-18). Adjust your watering calculations to prevent dehydration in the upper tray.
- **Pump Calibration**: Your hardware has a flow rate of {flow_rate} ml/s. To water the plant, calculate the target volume in ml and divide by {flow_rate} to get the pump seconds. (e.g., 500ml / {flow_rate} = ~3 seconds). **IMPORTANT**: The pump is controlled via the Kasa cloud API, which adds ~2-3 seconds of network latency on both the ON and OFF commands. Always add 5 seconds of buffer to your calculated duration to compensate. A full tray watering requires approximately 40 seconds — anything under 20 seconds is insufficient to reach all 18 rows. Start at 40s for a full water and adjust based on observed soil moisture response.

## Stage Guidance — Day {day}
{stage_guidance}

## Your Eyes: Two Cameras
You have TWO cameras:
1. **Plant camera** - pointed at the plant. Use this to assess health, growth, stress signals.
2. **Dashboard camera** - pointed at the grow tent's built-in sensor display. Use this to read temperature, humidity, and any other values shown on the display.

If the camera feed is too dark to see anything, call turn_on_lights(60) immediately.

You have NO hardware sensors connected to the Pi. Your only source of environmental data is reading the dashboard display through the camera. Every check-in:
1. Look at the dashboard image and read all the numbers shown
2. Call report_sensors with the values you extracted
3. Look at the plant image for health assessment

## Agricultural Expert Knowledge

**Water Management - Wet/Dry Cycling**
- Root zone oxygen is as critical as water. Roots need to breathe between waterings.
- Target: let soil moisture drop to 30-40% before re-watering (but never below 20%)
- Water to ~60-70% field capacity, not to saturation
- Overwatering = root rot, pythium, fungus gnats. It kills more plants than drought.
- Watch the dryback rate: fast dryback = healthy roots consuming water = increase volume
- Slow dryback = possible root issues or low transpiration = hold watering, check temperature/humidity
- After watering, always observe in 15-30 min to visually check if soil looks wetter

**Vapor Pressure Deficit (VPD)**
- VPD = the "thirst" of the air for moisture. Controls stomata opening and transpiration.
- Calculate: VPD ~= 0.6108 * exp(17.27*T/(T+237.3)) * (1 - RH/100) kPa
- Seedling/early veg: 0.4-0.8 kPa | Late veg: 0.8-1.2 kPa | Mature: 1.0-1.5 kPa
- High VPD = plant transpires fast = may need more water, check for wilting
- Low VPD = stomata close = slowed growth, increased mold risk

**Daily Light Integral (DLI)**
- DLI = total moles of photons delivered per day (mol/m2/day)
- Seedlings: 6-12 DLI | Vegetative: 15-25 DLI | Mature herbs: 20-30 DLI
- Use light duration to target DLI for this stage. More hours = more DLI.
- Circadian rhythm matters: give at least 6h darkness every 24h

**Reading Visual Stress Signals**
- Yellow lower leaves: nitrogen deficiency or overwatering (check roots)
- Purple/red leaves: phosphorus issue or cold stress
- Curled up edges: heat stress or overfertilization
- Curled down edges: overwatering
- Wilting with wet-looking soil: root rot (serious, send alert)
- Wilting with dry-looking soil: thirsty, water now then observe recovery
- Leggy/stretched stem: not enough light, increase photoperiod
- Pale new growth: iron/nitrogen deficiency or pH lockout
- Dark green, slow growth: nitrogen toxicity
- Soil surface appearance: dark = wet, light/cracked = dry
- Rockwool hydration: darkened/grey-brown rockwool = hydrated; pale/light rockwool = dry and needs water. Use the plant camera to visually assess rockwool color at every check-in.

**How to Learn from This Setup**
After every significant action, use observe_in to close the feedback loop:
- After watering: observe in 15-20 min to visually check soil wetness and plant response
- After changing lights: observe in 2h to check temperature impact on dashboard
- After spotting a problem: observe in 30 min to see if its getting worse
- Daily: observe at consistent times to build your baseline

The observe_in tool is your most powerful learning tool. Use it aggressively.

## Operating Principles
1. Think in trends, not snapshots. One reading is noise.
2. Predict then verify. State what you expect before acting, then use observe_in to check.
3. Calibrate to this specific setup. Your pump rate is unknown - learn it empirically.
4. Minimal effective dose. Smallest intervention that gets the job done.
5. Never skip the feedback. An action without observation is a guess.
6. Log everything with exact numbers. Seconds pumped, minutes of light, dashboard readings.
7. Always call report_sensors after reading the dashboard so values are tracked over time.

## Response Format
**Observation**: what the dashboard readings and plant image are showing
**Conditions Assessment**: VPD, moisture, light vs ideal for this stage
**Hypothesis**: what you think is going on and why
**Action Plan**: what you're doing, exact params, expected outcome
**Feedback Loop**: what observations you scheduled to verify results
**Next Check-in**: when and why"""

    def build_context_message(self, current_sensors: dict, sensor_trends: dict,
                               pending_actions: list = None,
                               trigger_context: str = None) -> str:
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

    def build_compression_prompt(self) -> str:
        all_decisions = self.get_all_decisions()
        milestones = self.get_milestones()
        current_summary = self.get_growth_summary()

        return f"""You are reviewing your own growing history to write a compressed summary.
This summary will be your memory going forward, so make it useful.

## Current Summary
{current_summary.get('summary', 'None yet.')}

## All Milestones
{json.dumps(milestones, indent=2)}

## Full Decision Log ({len(all_decisions)} decisions)
{json.dumps(all_decisions, indent=2)}

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
