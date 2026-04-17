import asyncio
import logging
import os
import signal
import sys
import threading
from pathlib import Path

import time
import uvicorn
import yaml
from dotenv import load_dotenv

from .ai_grower import AIGrower
from .api_server import app, set_grower

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_FILE = "data/logs/grower.log"


def setup_logging(base_dir: str):
    log_path = Path(base_dir) / LOG_FILE
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setStream(open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False))
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        handlers=[
            stdout_handler,
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


def main():
    base_dir = str(Path(__file__).resolve().parent.parent)
    setup_logging(base_dir)
    logger = logging.getLogger(__name__)

    # Load .env so KASA_USERNAME / KASA_PASSWORD are available to actuators
    load_dotenv(Path(base_dir) / ".env")

    config_path = Path(base_dir) / "config.yaml"
    if not config_path.exists():
        logger.error("config.yaml not found at %s", config_path)
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    logger.info("Config loaded, plant: %s",
                config.get("plant", {}).get("variety", "?"))

    grower = AIGrower(config, base_dir)
    set_grower(grower)

    # ── Kasa cloud connectivity check ────────────────────────────────────────
    kasa_user   = os.environ.get("KASA_USERNAME", "")
    kasa_pass   = os.environ.get("KASA_PASSWORD", "")
    device_alias = config.get("kasa_cloud", {}).get("device_alias", "Water Pump")

    if not kasa_user or not kasa_pass:
        logger.warning(
            "Kasa credentials not set — add KASA_USERNAME and KASA_PASSWORD to .env"
        )
    else:
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
                logger.warning(
                    "Kasa cloud login OK but device '%s' NOT found — "
                    "check kasa_cloud.device_alias in config.yaml",
                    device_alias,
                )
        except Exception as e:
            logger.warning("Kasa cloud check failed: %s", e)

    import Jetson.GPIO as GPIO

    light_pin = config.get("gpio", {}).get("grow_light_pin", "?")

    # ── Camera test — light on first, then test hardware, then turn off ───────
    grower.firebase.start()
    logger.info("Startup: turning light ON (pin %s) for camera test...", light_pin)
    GPIO.output(grower.actuators.light_pin, GPIO.LOW)  # Active-Low: LOW = ON
    grower.actuators._light_on = True
    time.sleep(5)  # let light fully stabilize
    logger.info("Startup: capturing camera test images with light ON...")
    images = grower.camera.capture_both("startup_test")
    for cam_name, img_path in images.items():
        if img_path:
            grower.firebase.upload_image(img_path, trigger_type="startup_test_" + cam_name)
            logger.info("Startup: %s image captured -> %s", cam_name, img_path)
        else:
            logger.warning("Startup: %s camera returned no image", cam_name)

    # ── Hardware relay test ───────────────────────────────────────────────────
    logger.info("Hardware test: Light relay (BOARD pin %s) — already ON, turning OFF now", light_pin)
    grower.actuators.turn_off_lights()

    logger.info("Hardware test: Pump ('%s' via Kasa cloud) ON for 10 s...", device_alias)
    grower.actuators.run_pump(10)
    logger.info("Hardware test complete. Check Firebase Storage to verify camera image quality.")

    def shutdown(signum, frame):
        logger.info("Shutdown received")
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


if __name__ == "__main__":
    main()
