import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    logger.warning("OpenCV not found, will use placeholder images")


class Camera:
    def __init__(self, config: dict):
        cam_config = config.get("camera", {})
        self.device_index = cam_config.get("device_index", 0)
        self.resolution = tuple(cam_config.get("resolution", [1280, 720]))
        self.image_dir = Path(cam_config.get("image_dir", "data/images"))
        self.image_dir.mkdir(parents=True, exist_ok=True)

    def capture(self, label: str = "checkin") -> str | None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{label}_{timestamp}.jpg"
        filepath = self.image_dir / filename

        if CV2_AVAILABLE:
            return self._capture_real(filepath)
        return self._capture_placeholder(filepath)

    def _capture_real(self, filepath: Path) -> str | None:
        try:
            cap = cv2.VideoCapture(self.device_index)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])

            # read a few frames first so the camera adjusts exposure
            for _ in range(5):
                cap.read()

            ret, frame = cap.read()
            cap.release()

            if not ret:
                logger.error("camera %d returned no frame", self.device_index)
                return None

            cv2.imwrite(str(filepath), frame)
            logger.info("photo saved: %s", filepath)
            return str(filepath)

        except Exception as e:
            logger.error("camera error: %s", e)
            return None

    def _capture_placeholder(self, filepath: Path) -> str | None:
        try:
            from PIL import Image, ImageDraw

            img = Image.new("RGB", self.resolution, color=(34, 139, 34))
            draw = ImageDraw.Draw(img)
            draw.text((50, 50), f"Plant Cam\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", fill="white")
            img.save(filepath)
            logger.info("[SIM] placeholder photo: %s", filepath)
            return str(filepath)
        except ImportError:
            logger.warning("PIL not installed, skipping placeholder")
            return None

    def get_latest_image(self) -> str | None:
        images = sorted(self.image_dir.glob("*.jpg"))
        return str(images[-1]) if images else None

    def cleanup_old_images(self, keep_count: int = 100):
        images = sorted(self.image_dir.glob("*.jpg"))
        if len(images) > keep_count:
            for img in images[:-keep_count]:
                img.unlink()
                logger.debug("deleted old photo: %s", img)
