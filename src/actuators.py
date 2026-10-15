import asyncio
import logging
import os
import platform
import threading
import json
import time
from datetime import datetime
from pathlib import Path

GPIO = None

try:
    import RPi.GPIO as GPIO
except ImportError:
    # Raspberry Pi builds should never use Jetson.GPIO. Use Jetson support only on actual Jetson hosts.
    if os.path.exists("/etc/nv_tegra_release") or platform.machine().startswith("aarch64"):
        try:
            import Jetson.GPIO as GPIO
        except ImportError:
            GPIO = None
    else:
        GPIO = None

from tplinkcloud import TPLinkDeviceManager # type: ignore

from src.water_budget import WaterBudget

logger = logging.getLogger(__name__)

# Active-Low logic for the physical grow-light relay (BOARD Pin 13)
if GPIO is not None:
    ON_STATE  = GPIO.LOW
    OFF_STATE = GPIO.HIGH
else:
    ON_STATE = 0
    OFF_STATE = 1


class Actuators:
    def __init__(self, config: dict, base_dir: str | None = None):
        self.config = config
        gpio_cfg      = config.get("gpio", {})
        kasa_cloud_cfg = config.get("kasa_cloud", {})

        # Physical relay pins (grow light + dashboard)
        self.light_pin = gpio_cfg.get("grow_light_pin", 13)
        self.dash_pin  = gpio_cfg.get("dashboard_relay_pin", 22)

        # Kasa cloud credentials — always from environment, never config
        self._kasa_user  = os.environ.get("KASA_USERNAME", "")
        self._kasa_pass  = os.environ.get("KASA_PASSWORD", "")
        self._device_alias = kasa_cloud_cfg.get("device_alias", "Water Pump")

        self._light_on     = False
        self._dash_on      = False
        self._pump_running = False
        self._light_timer: threading.Timer | None = None
        self._light_off_at: str | None = None
        self._total_pump_seconds = 0.0
        self._action_log: list[dict] = []
        self._pump_lock = threading.Lock()
        self.water_budget = WaterBudget(config, base_dir)
        self._light_state_path = (Path(base_dir) / "data" / "light_state.json") if base_dir else None

        self._init_gpio()

    # ── GPIO (grow light + dashboard) ────────────────────────────────────────

    def _init_gpio(self):
        if GPIO is None:
            logger.warning(
                "GPIO library not available. This build is expected on Raspberry Pi with RPi.GPIO installed. "
                "Install the Pi dependencies before running the hardware controller."
            )
            return

        GPIO.setmode(GPIO.BOARD)
        GPIO.setwarnings(False)

        GPIO.setup(self.light_pin, GPIO.OUT)
        GPIO.setup(self.dash_pin,  GPIO.OUT)

        # Drive both relays OFF immediately — prevents accidental activation on boot
        GPIO.output(self.light_pin, OFF_STATE)
        GPIO.output(self.dash_pin,  OFF_STATE)

        creds_ok = bool(self._kasa_user and self._kasa_pass)
        logger.info(
            "GPIO initialized (Active-Low, BOARD) | light=pin%d "
            "| Kasa cloud pump='%s' credentials=%s",
            self.light_pin,
            self._device_alias,
            "OK" if creds_ok else "MISSING — set KASA_USERNAME/KASA_PASSWORD in .env",
        )

    # ── Kasa cloud helpers ────────────────────────────────────────────────────

    def _run_async(self, coro):
        """Run an async coroutine from synchronous code.

        If called while an event loop is active (for example, during shutdown
        from Uvicorn's signal handler), run it on a worker thread with its own
        event loop because asyncio.run() cannot run in the current loop.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        result = []
        error = []

        def run_in_worker():
            try:
                result.append(asyncio.run(coro))
            except Exception as exc:
                error.append(exc)

        worker = threading.Thread(target=run_in_worker)
        worker.start()
        worker.join()

        if error:
            raise error[0]
        return result[0]

    async def _get_pump_device(self):
        """Authenticate against Kasa cloud and return the pump device.

        Raises RuntimeError if credentials are missing or device not found.
        """
        if not self._kasa_user or not self._kasa_pass:
            raise RuntimeError(
                "KASA_USERNAME or KASA_PASSWORD not set in environment"
            )

        manager = TPLinkDeviceManager(self._kasa_user, self._kasa_pass)
        device  = await manager.find_device(self._device_alias)

        if device is None:
            raise RuntimeError(
                f"Device '{self._device_alias}' not found in Kasa cloud account. "
                "Check kasa_cloud.device_alias in config.yaml matches the Kasa app name exactly."
            )
        return device

    # ── Water pump (Kasa cloud) ───────────────────────────────────────────────

    def run_pump(self, seconds: float, source: str = "ai", reason: str = "") -> dict:
        """Run the pump, subject to the hard water budget (src/water_budget.py).

        source: "ai" (tool call), "scheduled", "manual" (website/API) or
        "startup_test". Blocked doses return {"blocked": True, "error": ...}.
        """
        result = {
            "action":       "run_pump",
            "requested_seconds": round(float(seconds), 1),
            "source":       source,
            "timestamp":    datetime.now().isoformat(),
            "method":       "kasa_cloud",
            "device_alias": self._device_alias,
        }

        if not self._pump_lock.acquire(blocking=False):
            result.update(seconds=0, blocked=True, error="Pump is already running; dose refused")
            logger.warning("pump dose refused (%s): pump already running", source)
            self._log_action(result)
            return result

        try:
            seconds, why = self.water_budget.reserve(seconds, source, reason)
            result["seconds"] = seconds
            result["water_budget"] = self.water_budget.prompt_summary()
            if why:
                result.update(blocked=True, error=why)
                logger.warning("pump dose BLOCKED (%s, %.1fs requested): %s",
                               source, result["requested_seconds"], why)
                self._log_action(result)
                return result

            logger.info("pump ('%s' via Kasa cloud) ON for %.1f s [%s]",
                        self._device_alias, seconds, source)
            self._pump_running = True
            try:
                async def _do_pump():
                    device = await self._get_pump_device()
                    await device.power_on()
                    try:
                        await asyncio.sleep(seconds)   # non-blocking inside the event loop
                    finally:
                        await device.power_off()

                self._run_async(_do_pump())
            except Exception as e:
                logger.error("Kasa cloud pump error: %s", e)
                result["error"] = str(e)
            finally:
                self._pump_running = False
        finally:
            self._pump_lock.release()

        self._total_pump_seconds += seconds
        result["total_pump_seconds"] = round(self._total_pump_seconds, 1)
        self._log_action(result)
        return result

    # ── Grow light (physical relay, Active-Low) ───────────────────────────────

    def _gpio_ready(self) -> bool:
        if GPIO is None:
            logger.warning("GPIO hardware unavailable; skipping relay command")
            return False
        return True

    def turn_on_lights(self, minutes: float) -> dict:
        max_min = self.config.get("light", {}).get("max_on_minutes", 1440)
        minutes = max(1, min(max_min, minutes))

        if not self._gpio_ready():
            result = {
                "action": "turn_on_lights",
                "minutes": round(minutes, 1),
                "timestamp": datetime.now().isoformat(),
                "simulated": True,
            }
            self._log_action(result)
            return result

        if self._light_timer and self._light_timer.is_alive():
            self._light_timer.cancel()

        GPIO.output(self.light_pin, ON_STATE)
        self._light_on = True
        self._light_off_at = datetime.fromtimestamp(time.time() + minutes * 60).isoformat(timespec="seconds")
        self._save_light_state(time.time() + minutes * 60)
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
        if not self._gpio_ready():
            result = {"action": "turn_off_lights", "timestamp": datetime.now().isoformat(), "simulated": True}
            self._log_action(result)
            return result

        if self._light_timer and self._light_timer.is_alive():
            self._light_timer.cancel()

        GPIO.output(self.light_pin, OFF_STATE)
        self._light_on = False
        self._light_off_at = None
        self._save_light_state(None)
        result = {"action": "turn_off_lights", "timestamp": datetime.now().isoformat()}
        self._log_action(result)
        return result

    def _lights_auto_off(self):
        logger.info("lights auto-off timer fired")
        if not self._gpio_ready():
            return
        GPIO.output(self.light_pin, OFF_STATE)
        self._light_on = False
        self._light_off_at = None

    # Lights survive a grower restart: remember when they should go off.
    def _save_light_state(self, off_epoch: float | None):
        if not self._light_state_path:
            return
        try:
            self._light_state_path.parent.mkdir(parents=True, exist_ok=True)
            self._light_state_path.write_text(json.dumps({"off_epoch": off_epoch}))
        except Exception as e:
            logger.debug("could not save light state: %s", e)

    def restore_lights(self) -> bool:
        """Turn the lights back on if they were on before a restart."""
        try:
            off = json.loads(self._light_state_path.read_text()).get("off_epoch")
        except Exception:
            return False
        if not off or off - time.time() < 60:
            return False
        minutes = (off - time.time()) / 60
        logger.info("restoring grow lights after restart (%.0f min left)", minutes)
        self.turn_on_lights(minutes)
        return True

    # ── Dashboard relay ───────────────────────────────────────────────────────

    def turn_on_dashboard(self) -> dict:
        if not self._gpio_ready():
            result = {"action": "turn_on_dashboard", "timestamp": datetime.now().isoformat(), "simulated": True}
            self._log_action(result)
            return result
        GPIO.output(self.dash_pin, ON_STATE)
        self._dash_on = True
        result = {"action": "turn_on_dashboard", "timestamp": datetime.now().isoformat()}
        self._log_action(result)
        return result

    def turn_off_dashboard(self) -> dict:
        if not self._gpio_ready():
            result = {"action": "turn_off_dashboard", "timestamp": datetime.now().isoformat(), "simulated": True}
            self._log_action(result)
            return result
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
            "pump_device":        self._device_alias,
            "pump_method":        "kasa_cloud",
            "water_budget":       self.water_budget.status(),
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

        if GPIO is not None:
            GPIO.output(self.light_pin, OFF_STATE)
            GPIO.output(self.dash_pin,  OFF_STATE)
            GPIO.cleanup()
        self._light_on = False

        # Safety: best-effort pump-off on graceful shutdown
        if self._kasa_user and self._kasa_pass:
            try:
                async def _safe_off():
                    device = await self._get_pump_device()
                    await device.power_off()

                self._run_async(_safe_off())
                logger.info("Kasa cloud pump '%s' turned off on shutdown",
                            self._device_alias)
            except Exception as e:
                logger.warning("Could not turn off Kasa pump on shutdown: %s", e)
