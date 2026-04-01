import json
import logging
import time
from datetime import datetime
from pathlib import Path

from .action_scheduler import ActionScheduler, ScheduledAction
from .actuators import Actuators
from .camera import Camera
from .context_manager import ContextManager
from .firebase_sync import FirebaseSync
from .gemini_client import GeminiClient
from .growth_tracker import GrowthTracker
from .sensors import Sensors

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 15


class AIGrower:
    def __init__(self, config: dict, base_dir: str):
        self.config = config
        self.base_dir = base_dir

        self.sensors = Sensors(config)
        self.actuators = Actuators(config)
        self.camera = Camera(config)
        self.context = ContextManager(base_dir, config)
        self.growth = GrowthTracker(base_dir)
        self.gemini = GeminiClient(config)
        self.scheduler = ActionScheduler(config, base_dir)
        self.firebase = FirebaseSync(config, base_dir)

        self._checkin_lock = False
        self._alert_log: list[dict] = []

        self.scheduler.register_handler("checkin", self._handle_checkin_action)
        self.scheduler.register_handler("observe", self._handle_observe_action)
        self.scheduler.register_handler("run_pump", self._handle_pump_action)
        self.scheduler.register_handler("turn_on_lights", self._handle_lights_on_action)
        self.scheduler.register_handler("turn_off_lights", self._handle_lights_off_action)

        logger.info("AI Grower initialized")

    def start(self):
        logger.info("=" * 60)
        logger.info("VIP Vertical Farm - AI Grower starting")
        logger.info("Plant: %s | Day %d",
                    self.config.get("plant", {}).get("variety", "?"),
                    self.context.get_days_since_planting())
        logger.info("=" * 60)
        self.scheduler.start()
        self.firebase.start()
        self.scheduler.schedule_checkin(1, "Initial startup check-in")

    def _handle_checkin_action(self, action: ScheduledAction):
        self.run_checkin(trigger_type="checkin")

    def _handle_observe_action(self, action: ScheduledAction):
        self.run_checkin(
            trigger_type="observe",
            trigger_context=(
                f"SCHEDULED OBSERVATION\n"
                f"You scheduled this observation at: {action.params.get('scheduled_at', 'unknown')}\n"
                f"Your stated context: {action.context}\n"
                f"Before-state sensors: {json.dumps(action.params.get('before_sensors', {}))}\n\n"
                f"Compare current readings to before-state. Did your hypothesis prove correct? "
                f"What did you learn about this specific setup?"
            ),
        )

    def _handle_pump_action(self, action: ScheduledAction):
        seconds = action.params.get("seconds", 5)
        logger.info("scheduled pump running: %.1fs", seconds)
        self.actuators.run_pump(seconds)

    def _handle_lights_on_action(self, action: ScheduledAction):
        minutes = action.params.get("minutes", 60)
        logger.info("scheduled lights on for %.1f min", minutes)
        self.actuators.turn_on_lights(minutes)

    def _handle_lights_off_action(self, action: ScheduledAction):
        logger.info("scheduled lights off firing")
        self.actuators.turn_off_lights()

    def run_checkin(self, trigger_type: str = "checkin",
                    trigger_context: str = None):
        if self._checkin_lock:
            logger.warning("Check-in already in progress, skipping [%s]", trigger_type)
            return

        self._checkin_lock = True
        checkin_start = time.time()

        try:
            day = self.context.get_days_since_planting()
            logger.info("-" * 50)
            logger.info("[%s] Day %d - %s", trigger_type.upper(), day,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

            images = self.camera.capture_both(trigger_type)
            plant_image = images.get("plant")
            dashboard_image = images.get("dashboard")

            if plant_image:
                self.firebase.upload_image(plant_image, f"{trigger_type}_plant")
            if dashboard_image:
                self.firebase.upload_image(dashboard_image, f"{trigger_type}_dashboard")

            sensor_data = self.sensors.read_all()
            self.context.log_sensors(sensor_data)
            self.firebase.log_sensors(sensor_data)

            sensor_trends = self.context.get_sensor_trends()
            pending = self.scheduler.get_pending()

            if self.context.needs_compression():
                self._compress_context()

            system_prompt = self.context.build_system_prompt()
            context_msg = self.context.build_context_message(
                sensor_data, sensor_trends,
                pending_actions=pending,
                trigger_context=trigger_context,
            )

            image_note = ""
            if plant_image:
                image_note += "Image 1 is the PLANT camera. "
            if dashboard_image:
                image_note += "Image 2 is the DASHBOARD camera showing sensor readings. Read the values and call report_sensors. "

            if trigger_type == "observe":
                user_message = (
                    f"{context_msg}\n\n---\n\n"
                    f"{image_note}\n"
                    f"This is the OBSERVATION RESULT you scheduled. "
                    f"Analyze what changed, whether your hypothesis was correct, "
                    f"and what you learned. Update your mental model of this setup. "
                    f"Take any follow-up actions needed and schedule your next check-in."
                )
            else:
                user_message = (
                    f"{context_msg}\n\n---\n\n"
                    f"{image_note}\n"
                    f"Time for your check-in. Read the dashboard image for sensor values "
                    f"and call report_sensors. Examine the plant image for health. "
                    f"Apply your agricultural expertise: assess VPD, check moisture trends, "
                    f"look for stress signals. Take precise, measured actions. "
                    f"Use observe_in to close feedback loops on any action you take. "
                    f"Schedule your next check-in."
                )

            check_images = [p for p in [plant_image, dashboard_image] if p]
            response = self._run_tool_loop(system_prompt, user_message, check_images)

            elapsed = time.time() - checkin_start
            self._log_decision(response, sensor_data, elapsed, trigger_type)

            logger.info("[%s] done in %.1fs", trigger_type.upper(), elapsed)

        except Exception as e:
            logger.error("[%s] FAILED: %s", trigger_type.upper(), e, exc_info=True)
            self.scheduler.schedule_checkin(10, f"Retry after error: {e}")

        finally:
            self._checkin_lock = False

    def _run_tool_loop(self, system_prompt: str, user_message: str,
                       image_paths: list[str] | None = None) -> dict:
        all_actions = []
        all_thoughts = []
        final_text = None

        interaction = self.gemini.create_interaction(
            system_instruction=system_prompt,
            user_message=user_message,
            image_paths=image_paths or [],
            use_tools=True,
        )

        for round_num in range(MAX_TOOL_ROUNDS):
            parsed = self.gemini.extract_response(interaction)
            all_thoughts.extend(parsed["thoughts"])

            if parsed["text"] and not parsed["function_calls"]:
                final_text = parsed["text"]
                break

            if parsed["function_calls"]:
                tool_results = []
                for call in parsed["function_calls"]:
                    result = self._execute_tool(call["name"], call["arguments"])
                    all_actions.append({
                        "tool": call["name"],
                        "args": call["arguments"],
                        "result": result,
                    })
                    tool_results.append({
                        "call_id": call["id"],
                        "name": call["name"],
                        "result": result,
                    })

                interaction = self.gemini.submit_tool_results(
                    parsed["interaction_id"], tool_results, system_prompt
                )
            elif parsed["text"]:
                final_text = parsed["text"]
                break
            else:
                logger.warning("Empty Gemini response (round %d)", round_num)
                break

        return {"text": final_text, "actions": all_actions, "thoughts": all_thoughts}

    def _execute_tool(self, name: str, arguments: dict) -> dict:
        logger.info("  -> %s(%s)", name, json.dumps(arguments)[:150])

        try:
            if name == "capture_plant":
                path = self.camera.capture_plant("tool_capture")
                if path:
                    self.firebase.upload_image(path, "tool_plant")
                return {"image_path": path, "captured": path is not None,
                        "camera": "plant"}

            elif name == "capture_dashboard":
                path = self.camera.capture_dashboard("tool_capture")
                if path:
                    self.firebase.upload_image(path, "tool_dashboard")
                return {"image_path": path, "captured": path is not None,
                        "camera": "dashboard",
                        "note": "Read the values shown and call report_sensors"}

            elif name == "report_sensors":
                self.sensors.update_from_ai(arguments)
                data = self.sensors.read_all()
                self.context.log_sensors(data)
                self.firebase.log_sensors(data)
                return {"logged": True, "values": data}

            elif name == "run_pump":
                return self.actuators.run_pump(arguments.get("seconds", 5))

            elif name == "turn_on_lights":
                return self.actuators.turn_on_lights(arguments.get("minutes", 60))

            elif name == "turn_off_lights":
                return self.actuators.turn_off_lights()

            elif name == "observe_in":
                action = self.scheduler.schedule_observe(
                    delay_minutes=arguments.get("delay_minutes", 15),
                    context=arguments.get("context", ""),
                    before_sensors=self.sensors.read_all(),
                )
                return {
                    "scheduled": True,
                    "action_id": action.id,
                    "fires_at": datetime.fromtimestamp(action.fire_at).isoformat(),
                    "delay_minutes": arguments.get("delay_minutes"),
                    "context": arguments.get("context"),
                    "before_sensors_captured": True,
                }

            elif name == "schedule_checkin":
                action = self.scheduler.schedule_checkin(
                    arguments.get("minutes", 60),
                    arguments.get("reason", ""),
                )
                return {
                    "scheduled": True,
                    "action_id": action.id,
                    "minutes": arguments.get("minutes"),
                    "fires_at": datetime.fromtimestamp(action.fire_at).isoformat(),
                }

            elif name == "get_pending_actions":
                return {"pending": self.scheduler.get_pending()}

            elif name == "cancel_action":
                cancelled = self.scheduler.cancel(arguments.get("action_id", ""))
                return {"cancelled": cancelled}

            elif name == "log_milestone":
                day = self.context.get_days_since_planting()
                milestone = {
                    "day": day,
                    "description": arguments.get("description", ""),
                    "stage": arguments.get("stage", ""),
                    "measurements": arguments.get("measurements", {}),
                }
                self.context.log_milestone(milestone)
                self.firebase.log_milestone(milestone)
                measurements = arguments.get("measurements", {})
                if measurements:
                    self.growth.record_measurement(
                        day=day,
                        stage=arguments.get("stage", ""),
                        measurements=measurements,
                        notes=arguments.get("description", ""),
                    )
                return {"logged": True, "day": day}

            elif name == "get_growth_history":
                return {
                    "milestones": self.context.get_milestones()[-20:],
                    "growth_summary": self.growth.get_summary(),
                }

            elif name == "get_sensor_history":
                hours = arguments.get("hours", 24)
                return self.context.get_sensor_trends(hours)

            elif name == "get_decision_log":
                count = arguments.get("count", 10)
                return {"decisions": self.context.get_recent_decisions(count)}

            elif name == "emergency_alert":
                return self._record_alert(
                    arguments.get("message", "Unknown alert"),
                    arguments.get("severity", "warning"),
                )

            else:
                return {"error": f"Unknown tool: {name}"}

        except Exception as e:
            logger.error("Tool error [%s]: %s", name, e, exc_info=True)
            return {"error": str(e)}

    def _log_decision(self, response: dict, sensors: dict,
                      elapsed: float, trigger_type: str):
        text = response["text"] or ""
        decision = {
            "timestamp": datetime.now().isoformat(),
            "day": self.context.get_days_since_planting(),
            "trigger_type": trigger_type,
            "sensors": {k: v for k, v in sensors.items()
                        if k not in ("timestamp", "errors")},
            "observation": self._extract_section(text, "Observation"),
            "reasoning": self._extract_section(text, "Hypothesis"),
            "outcome": self._extract_section(text, "Feedback Loop"),
            "actions": [{"tool": a["tool"], "args": a["args"]}
                        for a in response["actions"]],
            "full_response": text,
            "thoughts": response["thoughts"],
            "elapsed_seconds": round(elapsed, 1),
        }
        self.context.log_decision(decision)
        self.firebase.log_decision(decision)

    def _extract_section(self, text: str, header: str) -> str:
        if not text:
            return ""
        for marker in (f"**{header}**:", f"**{header}**"):
            if marker in text:
                start = text.index(marker) + len(marker)
                headers = ["**Observation**", "**VPD/Conditions Assessment**",
                           "**Hypothesis**", "**Action Plan**",
                           "**Feedback Loop**", "**Next Check-in**"]
                end = len(text)
                for h in headers:
                    if h != f"**{header}**" and h in text[start:]:
                        candidate = start + text[start:].index(h)
                        if candidate < end:
                            end = candidate
                return text[start:end].strip()
        return ""

    def _compress_context(self):
        logger.info("Compressing context history...")
        prompt = self.context.build_compression_prompt()
        interaction = self.gemini.create_interaction(
            system_instruction=(
                "You are a data analyst summarizing plant growth history. "
                "Respond ONLY with valid JSON, no markdown."
            ),
            user_message=prompt,
            use_tools=False,
            continue_chain=False,
        )
        parsed = self.gemini.extract_response(interaction)
        if parsed["text"]:
            try:
                text = parsed["text"]
                if "```" in text:
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                summary = json.loads(text.strip())
                self.context.save_growth_summary(summary)
                self.firebase.save_growth_summary(summary)
                self.gemini.reset_chain()
                logger.info("Context compressed")
            except (json.JSONDecodeError, IndexError) as e:
                logger.error("Compression parse error: %s", e)

    def _record_alert(self, message: str, severity: str) -> dict:
        alert = {
            "timestamp": datetime.now().isoformat(),
            "message": message,
            "severity": severity,
            "day": self.context.get_days_since_planting(),
        }
        self._alert_log.append(alert)
        self.firebase.log_alert(alert)
        logger.warning("ALERT [%s]: %s", severity, message)
        return {"logged": True, **alert}

    def get_status(self) -> dict:
        return {
            "day": self.context.get_days_since_planting(),
            "plant": self.config.get("plant", {}).get("variety", "Unknown"),
            "stage": self.context.get_current_growth_stage().get("name", "unknown"),
            "next_checkin": self.scheduler.get_next_checkin(),
            "minutes_until_checkin": self.scheduler.get_minutes_until_checkin(),
            "pending_actions": self.scheduler.get_pending(),
            "actuators": self.actuators.get_status(),
            "latest_sensors": self.sensors.read_all(),
            "growth_summary": self.context.get_growth_summary(),
            "recent_decisions": self.context.get_recent_decisions(5),
            "growth_data": self.growth.get_summary(),
            "latest_plant_image": self.camera.get_latest_image("plant"),
            "latest_dashboard_image": self.camera.get_latest_image("dashboard"),
            "alerts": self._alert_log[-10:],
        }

    def manual_checkin(self):
        import threading
        t = threading.Thread(target=self.run_checkin, daemon=True)
        t.start()

    def cleanup(self):
        logger.info("AI Grower shutting down...")
        self.firebase.stop()
        self.scheduler.stop()
        self.sensors.cleanup()
        self.actuators.cleanup()
        self.camera.cleanup_old_images()
