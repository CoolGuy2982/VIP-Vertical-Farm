import asyncio
import logging
import threading
import time
from datetime import datetime

import Jetson.GPIO as GPIO
from kasa import SmartPlug

logger = logging.getLogger(__name__)

# Active-Low logic for the physical grow-light relay (BOARD Pin 13)
ON_STATE  = GPIO.LOW
OFF_STATE = GPIO.HIGH


class Actuators:
    def __init__(self, config: dict):
        self.config = config
        gpio_cfg = config.get("gpio", {})
        kasa_cfg = config.get("kasa", {})

        # Physical relay pins (grow light + dashboard)
        self.light_pin = gpio_cfg.get("grow_light_pin", 13)
        self.dash_pin  = gpio_cfg.get("dashboard_relay_pin", 22)

        # Kasa Wi-Fi plug for the water pump
        self._kasa_ip = kasa_cfg.get("plug_ip", "")

        self._light_on     = False
        self._dash_on      = False
        self._pump_running = False
        self._light_timer: threading.Timer | None = None
        self._total_pump_seconds = 0.0
        self._action_log: list[dict] = []

        self._init_gpio()

    # ── GPIO (grow light + dashboard) ────────────────────────────────────────

    def _init_gpio(self):
        GPIO.setmode(GPIO.BOARD)
        GPIO.setwarnings(False)

        GPIO.setup(self.light_pin, GPIO.OUT)
        GPIO.setup(self.dash_pin,  GPIO.OUT)

        # Drive both relays OFF immediately — prevents accidental activation on boot
        GPIO.output(self.light_pin, OFF_STATE)
        GPIO.output(self.dash_pin,  OFF_STATE)

        logger.info(
            "GPIO initialized (Active-Low, BOARD) | light=pin%d | Kasa pump=%s",
            self.light_pin,
            self._kasa_ip if self._kasa_ip else "NOT CONFIGURED",
        )

    # ── Kasa helper ──────────────────────────────────────────────────────────

    def _run_async(self, coro):
        """Execute an async coroutine from synchronous code.

        Safe because every caller runs in a context with no active event loop:
        - The grower scheduler fires handlers in a plain threading.Thread.
        - FastAPI routes that are sync `def` are dispatched by anyio into a
          threadpool worker thread — also no event loop present.
        Neither path is on uvicorn's main-thread event loop, so asyncio.run()
        always creates a fresh loop without conflicting with anything.
        """
        return asyncio.run(coro)

    # ── Water pump (Kasa Wi-Fi) ───────────────────────────────────────────────

    def run_pump(self, seconds: float) -> dict:
        pump_cfg = self.config.get("water_pump", {})
        seconds = max(
            pump_cfg.get("min_seconds_per_dose", 1),
            min(pump_cfg.get("max_seconds_per_dose", 60), seconds),
        )

        result = {
            "action":    "run_pump",
            "seconds":   round(seconds, 1),
            "timestamp": datetime.now().isoformat(),
            "method":    "kasa_wifi",
            "plug_ip":   self._kasa_ip,
        }

        if not self._kasa_ip:
            msg = "kasa.plug_ip is not set in config.yaml"
            logger.error("run_pump: %s", msg)
            result["error"] = msg
            return result

        logger.info("pump (Kasa %s) ON for %.1f s", self._kasa_ip, seconds)
        self._pump_running = True
        try:
            plug = SmartPlug(self._kasa_ip)
            self._run_async(plug.turn_on())
            time.sleep(seconds)
            self._run_async(plug.turn_off())
        except Exception as e:
            logger.error("Kasa pump error: %s", e)
            result["error"] = str(e)
        finally:
            self._pump_running = False

        self._total_pump_seconds += seconds
        result["total_pump_seconds"] = round(self._total_pump_seconds, 1)
        self._log_action(result)
        return result

    # ── Grow light (physical relay, Active-Low) ───────────────────────────────

    def turn_on_lights(self, minutes: float) -> dict:
        max_min = self.config.get("light", {}).get("max_on_minutes", 1440)
        minutes = max(1, min(max_min, minutes))

        if self._light_timer and self._light_timer.is_alive():
            self._light_timer.cancel()

        GPIO.output(self.light_pin, ON_STATE)
        self._light_on = True
        self._light_timer = threading.Timer(minutes * 60, self._lights_auto_off)
        self._light_timer.daemon = True
        self._light_timer.start()

        result = {
            "action":    "turn_on_lights",
            "minutes":   round(minutes, 1),
            "off_at":    datetime.fromtimestamp(time.time() + minutes * 60).isoformat(),
            "timestamp": datetime.now().isoformat(),
        }
        self._log_action(result)
        return result

    def turn_off_lights(self) -> dict:
        if self._light_timer and self._light_timer.is_alive():
            self._light_timer.cancel()

        GPIO.output(self.light_pin, OFF_STATE)
        self._light_on = False
        result = {"action": "turn_off_lights", "timestamp": datetime.now().isoformat()}
        self._log_action(result)
        return result

    def _lights_auto_off(self):
        logger.info("lights auto-off timer fired")
        GPIO.output(self.light_pin, OFF_STATE)
        self._light_on = False

    # ── Dashboard relay ───────────────────────────────────────────────────────

    def turn_on_dashboard(self) -> dict:
        GPIO.output(self.dash_pin, ON_STATE)
        self._dash_on = True
        result = {"action": "turn_on_dashboard", "timestamp": datetime.now().isoformat()}
        self._log_action(result)
        return result

    def turn_off_dashboard(self) -> dict:
        GPIO.output(self.dash_pin, OFF_STATE)
        self._dash_on = False
        result = {"action": "turn_off_dashboard", "timestamp": datetime.now().isoformat()}
        self._log_action(result)
        return result

    # ── Status / log ─────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "light_on":           self._light_on,
            "dashboard_on":       self._dash_on,
            "pump_running":       self._pump_running,
            "total_pump_seconds": round(self._total_pump_seconds, 1),
            "kasa_plug_ip":       self._kasa_ip,
        }

    def _log_action(self, action: dict):
        self._action_log.append(action)
        if len(self._action_log) > 200:
            self._action_log = self._action_log[-200:]

    def get_action_log(self) -> list[dict]:
        return list(self._action_log)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def cleanup(self):
        if self._light_timer and self._light_timer.is_alive():
            self._light_timer.cancel()

        GPIO.output(self.light_pin, OFF_STATE)
        GPIO.output(self.dash_pin,  OFF_STATE)
        GPIO.cleanup()

        # Safety: ensure the Kasa plug is off on graceful shutdown
        if self._kasa_ip:
            try:
                plug = SmartPlug(self._kasa_ip)
                self._run_async(plug.turn_off())
                logger.info("Kasa plug %s turned off on shutdown", self._kasa_ip)
            except Exception as e:
                logger.warning("Could not turn off Kasa plug on shutdown: %s", e)
