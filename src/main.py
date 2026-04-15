import asyncio
import logging
import signal
import sys
import threading
from pathlib import Path

import time
import uvicorn
import yaml

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

    # ── Kasa plug reachability check ─────────────────────────────────────────
    kasa_ip = config.get("kasa", {}).get("plug_ip", "")
    if not kasa_ip or kasa_ip == "ENTER_PLUG_IP_HERE":
        logger.warning("Kasa plug IP not configured — set kasa.plug_ip in config.yaml")
    else:
        try:
            from kasa import SmartPlug
            plug = SmartPlug(kasa_ip)
            asyncio.run(plug.update())
            logger.info("Kasa plug reachable at %s (alias: %s, is_on: %s)",
                        kasa_ip, plug.alias, plug.is_on)
        except Exception as e:
            logger.warning("Kasa plug at %s is NOT reachable: %s", kasa_ip, e)

    # ── Hardware test — reads pin/IP from config, uses actuator methods ───────
    light_pin = config.get("gpio", {}).get("grow_light_pin", "?")

    logger.info("Hardware test: Light relay (BOARD pin %s) ON for 10 s...", light_pin)
    grower.actuators.turn_on_lights(10 / 60)  # 10 seconds expressed as minutes
    time.sleep(10)
    grower.actuators.turn_off_lights()

    logger.info("Hardware test: Pump (Kasa %s) ON for 10 s...", kasa_ip or "UNCONFIGURED")
    grower.actuators.run_pump(10)
    logger.info("Hardware test complete.")

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
