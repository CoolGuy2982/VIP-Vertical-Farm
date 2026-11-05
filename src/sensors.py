import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)


class Sensors:
    """No hardware sensors. The AI reads values from the dashboard camera image.

    This class just provides a timestamp and a place to store the last
    values the AI extracted from the dashboard photo so other parts of
    the system (context manager, API) still work.
    """

    def __init__(self, config: dict):
        self._last_readings: dict = {}
        # When the VIVOSUN controller supplies readings (sensors.source: vivosun),
        # the AI's camera-read values must not overwrite them.
        self._controller_epoch = 0.0
        self.controller_max_age_s = 60 * float(
            ((config.get("sensors", {}) or {}).get("vivosun", {}) or {}).get("max_age_minutes", 15))

    def read_all(self) -> dict:
        result = {
            "timestamp": datetime.now().isoformat(),
            "source": "dashboard_camera",
            "note": "Values are read visually from the tent dashboard by the AI.",
        }
        if self._last_readings:
            result.update(self._last_readings)
        return result

    def update_from_controller(self, reading: dict):
        """Readings straight from the VIVOSUN controller (see src/vivosun_cloud.py)."""
        keep = ("temperature_c", "humidity_pct", "vpd_kpa", "outside_temperature_c",
                "outside_humidity_pct", "outside_vpd_kpa")
        cleaned = {k: reading[k] for k in keep if reading.get(k) is not None}
        cleaned["source"] = f"VIVOSUN controller ({reading.get('device', '?')})"
        cleaned["note"] = (f"Measured by the VIVOSUN controller at {reading.get('read_at', '?')}. "
                           "No need to read the dashboard or call report_sensors. "
                           "Soil moisture is NOT measured: judge it from the rockwool colour.")
        self._last_readings = cleaned
        self._controller_epoch = time.time()
        logger.info("controller sensors: %s", {k: v for k, v in cleaned.items() if k not in ("note",)})

    def controller_fresh(self) -> bool:
        return time.time() - self._controller_epoch < self.controller_max_age_s

    def update_from_ai(self, readings: dict):
        """Called when the AI extracts sensor values from the dashboard image."""
        if self.controller_fresh():
            logger.info("AI-read dashboard values ignored: controller readings are in use")
            return
        cleaned = {}
        for key in ("temperature_c", "humidity_pct", "light_lux", "soil_moisture_pct"):
            if key in readings and readings[key] is not None:
                try:
                    cleaned[key] = round(float(readings[key]), 1)
                except (ValueError, TypeError):
                    pass
        self._last_readings = cleaned
        if cleaned:
            logger.info("AI reported sensors: %s", cleaned)

    def cleanup(self):
        pass
