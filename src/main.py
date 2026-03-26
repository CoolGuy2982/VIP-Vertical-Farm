import logging
import signal
import sys
import threading
from pathlib import Path

import uvicorn
import yaml

from .ai_grower import AIGrower
from .api_server import app, set_grower

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_FILE = "data/logs/grower.log"


def setup_logging(base_dir: str):
    log_path = Path(base_dir) / LOG_FILE
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path),
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
