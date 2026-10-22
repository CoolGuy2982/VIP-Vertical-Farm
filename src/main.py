import asyncio
import logging
import os
import signal
import sys
import threading
from pathlib import Path

import time
import uvicorn
from dotenv import load_dotenv

from .ai_grower import AIGrower
from .api_server import app, set_grower
from .remote_control import LogBuffer, RemoteControl, load_config

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_FILE = "data/logs/grower.log"


def setup_logging(base_dir: str) -> LogBuffer:
    log_path = Path(base_dir) / LOG_FILE
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setStream(open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False))
    # Recent lines are kept in memory so the website can show logs and errors
    log_buffer = LogBuffer()
    log_buffer.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        handlers=[
            stdout_handler,
            logging.FileHandler(log_path, encoding="utf-8"),
            log_buffer,
        ],
    )
    return log_buffer


def is_restart(config: dict, base_dir: str) -> bool:
    """Restarts (website restart, code/config update, crash recovery) skip the
    hardware test, otherwise every restart would water the plants."""
    if os.environ.get("VFARM_RESTART") == "1":
        return True
    flag = Path(base_dir) / "data" / "skip_hardware_test_once"   # set by setup_device.py
    if flag.exists():
        flag.unlink()
        return True
    return not config.get("startup", {}).get("hardware_test", True)


def check_kasa(config: dict, logger: logging.Logger):
    kasa_user   = os.environ.get("KASA_USERNAME", "")
    kasa_pass   = os.environ.get("KASA_PASSWORD", "")
    device_alias = config.get("kasa_cloud", {}).get("device_alias", "Water Pump")

    if not kasa_user or not kasa_pass:
        logger.warning(
            "Kasa credentials not set — add KASA_USERNAME and KASA_PASSWORD to .env"
        )
        return
    try:
        from tplinkcloud import TPLinkDeviceManager

        async def _check_kasa():
            manager = TPLinkDeviceManager(kasa_user, kasa_pass)
            device  = await manager.find_device(device_alias)
            return device

        device = asyncio.run(_check_kasa())
        if device is not None:
            logger.info(
                "Kasa cloud OK — found pump device '%s' (alias: %s)",
                device_alias, device.get_alias(),
            )
        else:
            logger.error(
                "Kasa cloud login OK but device '%s' NOT found — "
                "check kasa_cloud.device_alias in config.yaml",
                device_alias,
            )
    except Exception as e:
        logger.error("Kasa cloud check failed: %s", e)


def hardware_test(grower: AIGrower, config: dict, logger: logging.Logger):
    light_pin = config.get("gpio", {}).get("grow_light_pin", "?")
    device_alias = config.get("kasa_cloud", {}).get("device_alias", "Water Pump")

    logger.info("Hardware test: Light relay (BOARD pin %s) ON for 10 s...", light_pin)
    grower.actuators.turn_on_lights(10 / 60)
    time.sleep(10)
    grower.actuators.turn_off_lights()

    logger.info("Hardware test: Pump ('%s' via Kasa cloud) ON for 10 s...", device_alias)
    grower.actuators.run_pump(10, source="startup_test", reason="startup hardware test")
    logger.info("Hardware test complete.")

    # ── Camera test — light ON, let auto-exposure settle ─────────────────────
    logger.info("Camera test: light on, capturing startup images...")
    grower.actuators.turn_on_lights(5)
    time.sleep(20)  # let camera auto-exposure fully settle
    images = grower.camera.capture_both("startup_test")
    grower.actuators.turn_off_lights()
    for cam_name, img_path in images.items():
        if img_path:
            grower.firebase.upload_image(img_path, trigger_type="startup_test_" + cam_name)
            logger.info("Camera test: %s -> %s", cam_name, img_path)
        else:
            logger.warning("Camera test: %s camera returned no image", cam_name)
    logger.info("Camera test done. Check Firebase Storage to verify image quality.")


def main():
    base_dir = str(Path(__file__).resolve().parent.parent)
    log_buffer = setup_logging(base_dir)
    logger = logging.getLogger(__name__)

    # Load .env so KASA_USERNAME / KASA_PASSWORD are available to actuators
    load_dotenv(Path(base_dir) / ".env")

    config_path = Path(base_dir) / "config.yaml"
    if not config_path.exists():
        logger.error("config.yaml not found at %s", config_path)
        sys.exit(1)

    # config.yaml + website changes in config.local.yaml
    config = load_config(base_dir)

    logger.info("Config loaded, plant: %s",
                config.get("plant", {}).get("variety", "?"))

    grower = AIGrower(config, base_dir)
    set_grower(grower)

    # Website link first, so startup problems show up on the site
    grower.firebase.start()
    remote = RemoteControl(grower, config, base_dir, log_buffer)
    grower.remote = remote
    remote.start()

    check_kasa(config, logger)

    if grower.paused:
        # Standby (e.g. empty tent): no hardware test, and the light stays off
        logger.info("Standby: skipping the hardware test; lights stay off until resumed")
    elif is_restart(config, base_dir):
        logger.info("Restart: skipping the pump/light/camera hardware test")
        grower.actuators.restore_lights()
    else:
        hardware_test(grower, config, logger)

    def shutdown(signum, frame):
        logger.info("Shutdown received")
        remote.stop()
        grower.cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    threading.Thread(target=grower.start, daemon=True).start()

    api_config = config.get("api", {})
    host = api_config.get("host", "0.0.0.0")
    port = api_config.get("port", 8080)

    logger.info("API server at http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    # uvicorn handles SIGTERM itself and returns here
    logger.info("API server stopped, shutting down")
    remote.stop()
    grower.cleanup()


if __name__ == "__main__":
    main()
