import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty

logger = logging.getLogger(__name__)

try:
    import firebase_admin
    from firebase_admin import credentials, firestore, storage
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False
    logger.warning("firebase-admin not found, cloud backup disabled")


class FirebaseSync:
    def __init__(self, config: dict, base_dir: str):
        self.config = config.get("firebase", {})
        self.base_dir = Path(base_dir)
        self.enabled = self.config.get("enabled", False) and FIREBASE_AVAILABLE

        self._upload_queue: Queue = Queue()
        self._running = False

        if not self.enabled:
            logger.info("Firebase sync disabled")
            return

        cred_path = self.config.get("credentials_path", "firebase-credentials.json")
        full_cred_path = self.base_dir / cred_path
        if not full_cred_path.exists():
            # try env var as fallback
            env_cred = os.environ.get("FIREBASE_CREDENTIALS")
            if env_cred:
                full_cred_path = Path(env_cred)

        if not full_cred_path.exists():
            logger.error("Firebase credentials not found at %s", full_cred_path)
            self.enabled = False
            return

        try:
            cred = credentials.Certificate(str(full_cred_path))
            bucket_name = self.config.get("storage_bucket", "")
            firebase_admin.initialize_app(cred, {"storageBucket": bucket_name})
            self.db = firestore.client()
            self.bucket = storage.bucket()
            self.device_id = self.config.get("device_id", "pi-grower-01")
            logger.info("Firebase connected (bucket=%s, device=%s)", bucket_name, self.device_id)
        except Exception as e:
            logger.error("Firebase init failed: %s", e)
            self.enabled = False

    def start(self):
        if not self.enabled:
            return
        self._running = True
        t = threading.Thread(target=self._upload_worker, daemon=True)
        t.start()
        logger.info("Firebase sync worker started")

    def stop(self):
        self._running = False

    # public methods for logging data

    def upload_image(self, local_path: str, trigger_type: str = "checkin"):
        if not self.enabled:
            return
        self._upload_queue.put(("image", {
            "local_path": local_path,
            "trigger_type": trigger_type,
            "timestamp": datetime.now().isoformat(),
        }))

    def log_decision(self, decision: dict):
        if not self.enabled:
            return
        self._upload_queue.put(("decision", decision))

    def log_sensors(self, readings: dict):
        if not self.enabled:
            return
        self._upload_queue.put(("sensors", readings))

    def log_milestone(self, milestone: dict):
        if not self.enabled:
            return
        self._upload_queue.put(("milestone", milestone))

    def log_alert(self, alert: dict):
        if not self.enabled:
            return
        self._upload_queue.put(("alert", alert))

    def save_growth_summary(self, summary: dict):
        if not self.enabled:
            return
        self._upload_queue.put(("growth_summary", summary))

    # background worker

    def _upload_worker(self):
        while self._running:
            try:
                item_type, data = self._upload_queue.get(timeout=2)
            except Empty:
                continue

            try:
                if item_type == "image":
                    self._do_upload_image(data)
                elif item_type == "decision":
                    self._do_log_document("decisions", data)
                elif item_type == "sensors":
                    self._do_log_document("sensor_readings", data)
                elif item_type == "milestone":
                    self._do_log_document("milestones", data)
                elif item_type == "alert":
                    self._do_log_document("alerts", data)
                elif item_type == "growth_summary":
                    self._do_save_summary(data)
            except Exception as e:
                logger.error("Firebase upload failed (%s): %s", item_type, e)
                # put it back for retry after a short delay
                time.sleep(5)
                self._upload_queue.put((item_type, data))

    def _do_upload_image(self, data: dict):
        local_path = data["local_path"]
        if not Path(local_path).exists():
            return

        filename = Path(local_path).name
        remote_path = f"grows/{self.device_id}/images/{filename}"

        blob = self.bucket.blob(remote_path)
        blob.upload_from_filename(local_path, content_type="image/jpeg")

        # also log metadata to firestore
        self._do_log_document("images", {
            "filename": filename,
            "storage_path": remote_path,
            "trigger_type": data.get("trigger_type", "unknown"),
            "timestamp": data.get("timestamp", datetime.now().isoformat()),
        })
        logger.debug("uploaded image: %s", remote_path)

    def _do_log_document(self, collection: str, data: dict):
        doc_data = self._make_serializable(data)
        doc_data["device_id"] = self.device_id
        doc_data.setdefault("timestamp", datetime.now().isoformat())

        col_path = f"grows/{self.device_id}/{collection}"
        self.db.collection(col_path).add(doc_data)

    def _do_save_summary(self, data: dict):
        doc_data = self._make_serializable(data)
        doc_data["device_id"] = self.device_id
        doc_data["updated_at"] = datetime.now().isoformat()

        doc_ref = self.db.document(f"grows/{self.device_id}")
        doc_ref.set(doc_data, merge=True)

    def _make_serializable(self, obj):
        """Convert to JSON-safe types so firestore doesn't choke."""
        if isinstance(obj, dict):
            return {k: self._make_serializable(v) for k, v in obj.items()
                    if v is not None and v != [] and v != {}}
        if isinstance(obj, list):
            return [self._make_serializable(item) for item in obj]
        if isinstance(obj, (int, float, str, bool)):
            return obj
        return str(obj)
