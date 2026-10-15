import base64
import glob
import logging
import os
import platform
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    logger.warning("OpenCV not found, will use placeholder images")

_BLANK_JPEG_BYTES = base64.b64decode(
    """/9j/4AAQSkZJRgABAQAAAQABAAD/2wCEAAkGBxAQEBUQEBAVFRUVFRUVFRAQFRUVE" \
    "BQVFRUVFRUYHSggGBolGxUVITEhJSkrLi4uFx8zODMsNygtLisBCgoKBQUFDgUFDisZ" \
    "ExkrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKyr/2wCEAAkGBxAQEBUQEBAVFRUVFRUVFRAQFRUVEBQVFRUVFRUYH" \
    "SggGBolGxUVITEhJSkrLi4uFx8zODMsNygtLisBCgoKBQUFDgUFDisZExkrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr" \
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr"""
)


class Camera:
    """Two USB webcams (plant + dashboard).

    `plant_cam` / `dashboard_cam` in config.yaml can be either:
      * an integer index (0, 2, ...) -- simple, but on Linux / Raspberry Pi the
        /dev/videoN numbers change when cameras are replugged or enumerate in a
        different order at boot, and every UVC webcam also creates a second
        "metadata" node that returns no frames; or
      * a stable device path such as
        /dev/v4l/by-id/usb-Logitech_C270_ABC123-video-index0   (follows the camera)
        /dev/v4l/by-path/platform-...-usb-0:1.2:1.0-video-index0 (follows the USB port)
        These never move, so they are the recommended setting on the Pi.
    Run `python diagnose_cameras.py` on the Pi to list the right values.
    """

    # One capture at a time: the scheduler thread and the API threadpool can both
    # ask for photos, and a webcam that is already open elsewhere returns nothing.
    _capture_lock = threading.Lock()

    def __init__(self, config: dict, base_dir: str | Path | None = None):
        cam_config = config.get("camera", {})
        self.plant_device = self._parse_device(
            cam_config.get("plant_cam", cam_config.get("plant_cam_index", 0)))
        self.dashboard_device = self._parse_device(
            cam_config.get("dashboard_cam", cam_config.get("dashboard_cam_index", 1)))
        # Kept for backwards compatibility with code that reads these attributes.
        self.plant_index = self.plant_device
        self.dashboard_index = self.dashboard_device
        self.resolution = tuple(cam_config.get("resolution", [1280, 720]))
        self.warmup_frames = int(cam_config.get("warmup_frames", 15))
        self.retries = int(cam_config.get("retries", 2))
        self.use_mjpg = bool(cam_config.get("mjpg", True))
        self.image_dir = Path(cam_config.get("image_dir", "data/images"))
        if base_dir is not None and not self.image_dir.is_absolute():
            self.image_dir = Path(base_dir) / self.image_dir
        self.image_dir.mkdir(parents=True, exist_ok=True)
        # Rotation: 0=none, 90=clockwise, 180=flip, 270=counter-clockwise
        self.plant_rotation = cam_config.get("plant_rotation", 0)

    @staticmethod
    def _parse_device(value):
        """Accept 2, "2" or "/dev/v4l/by-id/..." and return an int or a path string."""
        if isinstance(value, int):
            return value
        text = str(value).strip()
        return int(text) if text.isdigit() else text

    def get_blank_image_path(self) -> Path:
        blank_dir = self.image_dir / "blank"
        blank_dir.mkdir(parents=True, exist_ok=True)
        path = blank_dir / "blank.jpg"
        if not path.exists():
            path.write_bytes(_BLANK_JPEG_BYTES)
        return path

    def capture_plant(self, label: str = "checkin") -> str | None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"plant_{label}_{timestamp}.jpg"
        filepath = self.image_dir / filename
        return self._grab_frame(self.plant_device, filepath, "plant")

    def capture_dashboard(self, label: str = "checkin") -> str | None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"dashboard_{label}_{timestamp}.jpg"
        filepath = self.image_dir / filename
        return self._grab_frame(self.dashboard_device, filepath, "dashboard")

    def capture_both(self, label: str = "checkin") -> dict:
        plant_path = self.capture_plant(label)
        dashboard_path = self.capture_dashboard(label)
        return {"plant": plant_path, "dashboard": dashboard_path}

    # ------------------------------------------------------------------ capture

    def _open(self, device):
        """Open a webcam, forcing the V4L2 backend and MJPG on Linux.

        MJPG matters on the Pi: uncompressed YUYV at 1280x720 needs most of a
        USB 2.0 bus, so a second camera (or a hub) often fails to stream at all.
        """
        if platform.system() == "Linux":
            cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
        else:
            cap = cv2.VideoCapture(device)
        if not cap.isOpened():
            cap.release()
            return None
        if self.use_mjpg:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _read_one(self, device, cam_name: str):
        cap = self._open(device)
        if cap is None:
            logger.error("%s cam (%s) could not be opened -- unplugged, wrong device, "
                         "or in use by another process", cam_name, device)
            return None
        try:
            good = 0
            for _ in range(self.warmup_frames):  # let auto-exposure settle
                ok, _frame = cap.read()
                good += bool(ok)
            ok, frame = cap.read()
            if not ok or frame is None:
                logger.error("%s cam (%s) opened but returned no frame (%d/%d warm-up "
                             "frames ok)", cam_name, device, good, self.warmup_frames)
                return None
            return frame
        finally:
            cap.release()

    def _grab_frame(self, device, filepath: Path, cam_name: str) -> str | None:
        if not CV2_AVAILABLE:
            return self._capture_placeholder(filepath, cam_name)

        with self._capture_lock:
            frame = None
            for attempt in range(1, self.retries + 2):
                try:
                    frame = self._read_one(device, cam_name)
                except Exception as e:
                    logger.error("%s cam error: %s", cam_name, e)
                    frame = None
                if frame is not None:
                    break
                if attempt <= self.retries:
                    time.sleep(1.5)

            if frame is None:
                self._log_available_devices(cam_name, device)
                return None

            try:
                if cam_name == "plant" and self.plant_rotation != 0:
                    _rot_map = {
                        90:  cv2.ROTATE_90_CLOCKWISE,
                        180: cv2.ROTATE_180,
                        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
                    }
                    if self.plant_rotation in _rot_map:
                        frame = cv2.rotate(frame, _rot_map[self.plant_rotation])

                if not cv2.imwrite(str(filepath), frame):
                    logger.error("%s photo could not be written to %s", cam_name, filepath)
                    return None
                logger.info("%s photo saved: %s", cam_name, filepath)
                return str(filepath)
            except Exception as e:
                logger.error("%s cam error: %s", cam_name, e)
                return None

    @staticmethod
    def _log_available_devices(cam_name: str, device) -> None:
        """After a failure, say what the Pi can actually see so the config can be fixed."""
        if platform.system() != "Linux":
            return
        by_id = sorted(glob.glob("/dev/v4l/by-id/*-video-index0"))
        nodes = sorted(glob.glob("/dev/video*"))
        logger.error("%s cam (%s) failed. Webcams visible now: %s | /dev/video nodes: %s",
                     cam_name, device, by_id or "none", nodes or "none")
        if isinstance(device, str) and not os.path.exists(device):
            logger.error("configured path %s does not exist -- camera unplugged or "
                         "not enumerated (check power / `dmesg`)", device)

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

    # ------------------------------------------------------------------ files

    def _images_newest_last(self, pattern: str, include_test: bool = True) -> list[Path]:
        # Sort by time, not by name: names start with the label, so a name sort
        # puts every "plant_test_*" after every "plant_checkin_*" regardless of date.
        images = [p for p in self.image_dir.glob(pattern)
                  if include_test or "_test_" not in p.name or "startup_test" in p.name]
        return sorted(images, key=lambda p: p.stat().st_mtime)

    def get_latest_image(self, cam_type: str = "plant") -> str | None:
        # Skip the lights-off availability probes ("plant_test_*") for the website.
        images = self._images_newest_last(f"{cam_type}_*.jpg", include_test=False)
        if images:
            return str(images[-1])

        blank_path = self.get_blank_image_path()
        return str(blank_path)

    def get_recent_plant_images(self, count: int = 4) -> list[tuple[str, str]]:
        """Return (path, human_timestamp) for the last `count` plant photos."""
        images = self._images_newest_last("plant_*.jpg", include_test=False)
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
        images = self._images_newest_last("*.jpg")
        if len(images) > keep_count:
            for img in images[:-keep_count]:
                img.unlink()
                logger.debug("deleted old photo: %s", img)
