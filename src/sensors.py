import logging
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

    def read_all(self) -> dict:
        result = {
            "timestamp": datetime.now().isoformat(),
            "source": "dashboard_camera",
            "note": "Values are read visually from the tent dashboard by the AI.",
        }
        if self._last_readings:
            result.update(self._last_readings)
        return result

    def update_from_ai(self, readings: dict):
        """Called when the AI extracts sensor values from the dashboard image."""
        cleaned = {}
        for key in ("temperature_c", "humidity_pct", "soil_moisture_pct", "light_lux"):
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
