import logging
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

try:
    import Jetson.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False
    logger.warning("Jetson.GPIO not found, running in sim mode")


class Actuators:
    def __init__(self, config: dict):
        self.config = config
        gpio = config.get("gpio", {})

        self.pump_pin = gpio.get("water_pump_pin", 17)
        self.light_pin = gpio.get("grow_light_pin", 27)

        self._light_on = False
        self._pump_running = False
        self._light_timer: threading.Timer | None = None
        self._total_pump_seconds = 0.0
        self._action_log: list[dict] = []

        if GPIO_AVAILABLE:
            self._init_gpio()
        else:
            self._simulated = True
            logger.info("[SIM] actuators ready")

    def _init_gpio(self):
        self._simulated = False
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(self.pump_pin, GPIO.OUT)
        GPIO.setup(self.light_pin, GPIO.OUT)
        GPIO.output(self.pump_pin, GPIO.LOW)
        GPIO.output(self.light_pin, GPIO.LOW)

    def run_pump(self, seconds: float) -> dict:
        pump_config = self.config.get("water_pump", {})
        max_s = pump_config.get("max_seconds_per_dose", 60)
        min_s = pump_config.get("min_seconds_per_dose", 1)
        seconds = max(min_s, min(max_s, seconds))

        result = {
            "action": "run_pump",
            "seconds": round(seconds, 1),
            "timestamp": datetime.now().isoformat(),
        }

        if self._simulated:
            logger.info("[SIM] pump on for %.1fs", seconds)
            time.sleep(min(seconds, 0.1))
        else:
            logger.info("pump on for %.1fs", seconds)
            self._pump_running = True
            GPIO.output(self.pump_pin, GPIO.HIGH)
            time.sleep(seconds)
            GPIO.output(self.pump_pin, GPIO.LOW)
            self._pump_running = False

        self._total_pump_seconds += seconds
        result["total_pump_seconds"] = round(self._total_pump_seconds, 1)
        self._log_action(result)
        return result

    def turn_on_lights(self, minutes: float) -> dict:
        max_min = self.config.get("light", {}).get("max_on_minutes", 1440)
        minutes = max(1, min(max_min, minutes))

        if self._light_timer and self._light_timer.is_alive():
            self._light_timer.cancel()

        if self._simulated:
            logger.info("[SIM] lights on for %.1f min", minutes)
        else:
            GPIO.output(self.light_pin, GPIO.HIGH)

        self._light_on = True
        self._light_timer = threading.Timer(minutes * 60, self._lights_auto_off)
        self._light_timer.daemon = True
        self._light_timer.start()

        result = {
            "action": "turn_on_lights",
            "minutes": round(minutes, 1),
            "off_at": datetime.fromtimestamp(time.time() + minutes * 60).isoformat(),
            "timestamp": datetime.now().isoformat(),
        }
        self._log_action(result)
        return result

    def turn_off_lights(self) -> dict:
        if self._light_timer and self._light_timer.is_alive():
            self._light_timer.cancel()

        if self._simulated:
            logger.info("[SIM] lights off")
        else:
            GPIO.output(self.light_pin, GPIO.LOW)

        self._light_on = False
        result = {"action": "turn_off_lights", "timestamp": datetime.now().isoformat()}
        self._log_action(result)
        return result

    def _lights_auto_off(self):
        logger.info("lights auto-off timer fired")
        if not self._simulated:
            GPIO.output(self.light_pin, GPIO.LOW)
        self._light_on = False

    def get_status(self) -> dict:
        return {
            "light_on": self._light_on,
            "pump_running": self._pump_running,
            "total_pump_seconds": round(self._total_pump_seconds, 1),
        }

    def _log_action(self, action: dict):
        self._action_log.append(action)
        if len(self._action_log) > 200:
            self._action_log = self._action_log[-200:]

    def get_action_log(self) -> list[dict]:
        return list(self._action_log)

    def cleanup(self):
        if self._light_timer and self._light_timer.is_alive():
            self._light_timer.cancel()
        if not self._simulated:
            GPIO.output(self.pump_pin, GPIO.LOW)
            GPIO.output(self.light_pin, GPIO.LOW)
            GPIO.cleanup()
