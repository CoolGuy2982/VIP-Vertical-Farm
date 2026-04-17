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
        self.plant_index = cam_config.get("plant_cam_index", 0)
        self.dashboard_index = cam_config.get("dashboard_cam_index", 1)
        self.resolution = tuple(cam_config.get("resolution", [1280, 720]))
        self.image_dir = Path(cam_config.get("image_dir", "data/images"))
        self.image_dir.mkdir(parents=True, exist_ok=True)

    def capture_plant(self, label: str = "checkin") -> str | None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"plant_{label}_{timestamp}.jpg"
        filepath = self.image_dir / filename
        return self._grab_frame(self.plant_index, filepath, "plant")

    def capture_dashboard(self, label: str = "checkin") -> str | None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"dashboard_{label}_{timestamp}.jpg"
        filepath = self.image_dir / filename
        return self._grab_frame(self.dashboard_index, filepath, "dashboard")

    def capture_both(self, label: str = "checkin") -> dict:
        plant_path = self.capture_plant(label)
        dashboard_path = self.capture_dashboard(label)
        return {"plant": plant_path, "dashboard": dashboard_path}

    def _grab_frame(self, device_index: int, filepath: Path, cam_name: str) -> str | None:
        if not CV2_AVAILABLE:
            return self._capture_placeholder(filepath, cam_name)

        try:
            cap = cv2.VideoCapture(device_index)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])

            for _ in range(5):
                cap.read()

            ret, frame = cap.read()
            cap.release()

            if not ret:
                logger.error("%s cam (index %d) returned no frame", cam_name, device_index)
                return None


            cv2.imwrite(str(filepath), frame)
            logger.info("%s photo saved: %s", cam_name, filepath)
            return str(filepath)

        except Exception as e:
            logger.error("%s cam error: %s", cam_name, e)
            return None

    def _capture_placeholder(self, filepath: Path, cam_name: str) -> str | None:
        try:
            from PIL import Image, ImageDraw

            color = (34, 139, 34) if cam_name == "plant" else (50, 50, 80)
            img = Image.new("RGB", self.resolution, color=color)
            draw = ImageDraw.Draw(img)
            draw.text((50, 50), f"{cam_name.upper()} CAM\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", fill="white")
            img.save(filepath)
            logger.info("[SIM] placeholder %s photo: %s", cam_name, filepath)
            return str(filepath)
        except ImportError:
            logger.warning("PIL not installed, skipping placeholder")
            return None

    def get_latest_image(self, cam_type: str = "plant") -> str | None:
        images = sorted(self.image_dir.glob(f"{cam_type}_*.jpg"))
        return str(images[-1]) if images else None

    def get_recent_plant_images(self, count: int = 4) -> list[tuple[str, str]]:
        """Return (path, human_timestamp) for the last `count` plant photos."""
        images = sorted(self.image_dir.glob("plant_*.jpg"))
        result = []
        for img in images[-count:]:
            parts = img.stem.split("_")
            try:
                ts = datetime.strptime(f"{parts[-2]}_{parts[-1]}", "%Y%m%d_%H%M%S")
                ts_str = ts.strftime("%Y-%m-%d %H:%M")
            except (ValueError, IndexError):
                ts_str = "unknown time"
            result.append((str(img), ts_str))
        return result

    def cleanup_old_images(self, keep_count: int = 200):
        images = sorted(self.image_dir.glob("*.jpg"))
        if len(images) > keep_count:
            for img in images[:-keep_count]:
                img.unlink()
                logger.debug("deleted old photo: %s", img)
