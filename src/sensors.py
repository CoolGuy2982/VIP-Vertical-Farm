import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)

try:
    import adafruit_dht
    import board
    import busio
    import digitalio
    import adafruit_mcp3xxx.mcp3008 as MCP
    from adafruit_mcp3xxx.analog_in import AnalogIn
    PI_AVAILABLE = True
except ImportError:
    PI_AVAILABLE = False
    logger.warning("RPi GPIO libraries not found, running in sim mode")


class SensorError(Exception):
    pass


class Sensors:
    def __init__(self, config: dict):
        self.config = config
        gpio = config.get("gpio", {})
        adc_config = config.get("adc", {})

        if PI_AVAILABLE:
            self._init_real_sensors(gpio, adc_config)
        else:
            self._simulated = True
            self._fake_values = {
                "temperature_c": 23.5,
                "humidity_pct": 55.0,
                "soil_moisture_pct": 45.0,
                "light_lux": 800.0,
            }

    def _init_real_sensors(self, gpio: dict, adc_config: dict):
        self._simulated = False

        # DHT22 for temp and humidity
        dht_pin = gpio.get("dht_sensor_pin", 4)
        pin_map = {4: board.D4, 17: board.D17, 27: board.D27, 22: board.D22}
        self._dht = adafruit_dht.DHT22(pin_map.get(dht_pin, board.D4))

        # MCP3008 ADC over SPI for the analog sensors
        spi = busio.SPI(clock=board.SCK, MISO=board.MISO, MOSI=board.MOSI)
        cs = digitalio.DigitalInOut(board.CE0)
        mcp = MCP.MCP3008(spi, cs)

        soil_ch = gpio.get("soil_moisture_adc_channel", 0)
        self._soil_channel = AnalogIn(mcp, getattr(MCP, f"P{soil_ch}"))

        light_ch = gpio.get("light_sensor_adc_channel", 1)
        self._light_channel = AnalogIn(mcp, getattr(MCP, f"P{light_ch}"))

        self._vref = adc_config.get("vref", 3.3)

    def read_all(self) -> dict:
        if self._simulated:
            return self._read_simulated()
        return self._read_real()

    def _read_real(self) -> dict:
        errors = []
        result = {"timestamp": datetime.now().isoformat(), "errors": []}

        # DHT22 can be flaky, retry a few times
        for attempt in range(3):
            try:
                result["temperature_c"] = round(self._dht.temperature, 1)
                result["humidity_pct"] = round(self._dht.humidity, 1)
                break
            except RuntimeError as e:
                if attempt == 2:
                    errors.append(f"DHT22 failed: {e}")
                    result["temperature_c"] = None
                    result["humidity_pct"] = None
                time.sleep(0.5)

        # Soil moisture: dry soil reads ~2.5V (0%), wet reads ~1.0V (100%)
        try:
            voltage = self._soil_channel.voltage
            moisture_pct = max(0, min(100, (2.5 - voltage) / 1.5 * 100))
            result["soil_moisture_pct"] = round(moisture_pct, 1)
        except Exception as e:
            errors.append(f"soil moisture failed: {e}")
            result["soil_moisture_pct"] = None

        # Light sensor, rough lux conversion (calibrate for your specific sensor)
        try:
            voltage = self._light_channel.voltage
            lux = (voltage / self._vref) * 10000
            result["light_lux"] = round(lux, 0)
        except Exception as e:
            errors.append(f"light sensor failed: {e}")
            result["light_lux"] = None

        result["errors"] = errors
        if errors:
            logger.warning("sensor errors: %s", errors)

        return result

    def _read_simulated(self) -> dict:
        import random
        base = self._fake_values
        return {
            "temperature_c": round(base["temperature_c"] + random.uniform(-1, 1), 1),
            "humidity_pct": round(base["humidity_pct"] + random.uniform(-3, 3), 1),
            "soil_moisture_pct": round(base["soil_moisture_pct"] + random.uniform(-2, 2), 1),
            "light_lux": round(base["light_lux"] + random.uniform(-50, 50), 0),
            "timestamp": datetime.now().isoformat(),
            "errors": [],
        }

    def cleanup(self):
        if not self._simulated and hasattr(self, "_dht"):
            self._dht.exit()
