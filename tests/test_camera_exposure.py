import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from src import camera as camera_mod  # noqa: E402
from src.camera import Camera  # noqa: E402


class FakeWebcam:
    """Brightness follows the exposure; in auto mode the grow light blows it out."""

    def __init__(self, scene=1.0, honours_exposure=True):
        self.auto = True
        self.exposure = 300.0
        self.scene = scene                  # 1.0 = grow light on, 0.1 = dark tent
        self.honours = honours_exposure
        self.sets = []

    def set(self, prop, value):
        self.sets.append((prop, value))
        if prop == cv2.CAP_PROP_AUTO_EXPOSURE:
            self.auto = value == 3
        elif prop == cv2.CAP_PROP_EXPOSURE and self.honours:
            self.exposure = float(value)
        return True

    def get(self, prop):
        return self.exposure if prop == cv2.CAP_PROP_EXPOSURE else 0

    def read(self):
        exp = 600.0 if (self.auto or not self.honours) else self.exposure
        level = exp * self.scene * 0.8
        # a gradient tray: bright highlights clip first
        frame = np.clip(np.linspace(level * 0.4, level * 1.3, 64 * 64).reshape(64, 64), 0, 255)
        return True, np.dstack([frame] * 3).astype(np.uint8)

    def release(self):
        pass


@pytest.fixture
def cam(tmp_path, monkeypatch):
    c = Camera({"camera": {"warmup_frames": 2}}, base_dir=tmp_path)
    webcam = FakeWebcam()

    def fake_open(device, cam_name=None):
        c._apply_exposure(webcam, device, cam_name) if cam_name else None
        return webcam
    monkeypatch.setattr(c, "_open", fake_open)
    return c, webcam


def test_blown_out_photo_gets_darker(cam):
    c, webcam = cam
    assert Camera._too_bright(webcam.read()[1])          # what the grow light does now
    frame = c._read_one(0, "plant")
    assert not Camera._too_bright(frame)
    assert c._exposure[0] < 300 and webcam.auto is False


def test_learned_exposure_is_reused_then_released_in_the_dark(cam):
    c, webcam = cam
    c._read_one(0, "plant")
    learned = c._exposure[0]
    webcam.exposure, webcam.auto = 999, True             # camera "forgets" between opens
    c._read_one(0, "plant")
    assert webcam.auto is False and c._exposure[0] <= learned
    webcam.scene = 0.05                                  # grow light off
    c._read_one(0, "plant")
    assert 0 not in c._exposure and webcam.auto is True


def test_camera_that_ignores_exposure_falls_back_to_auto(cam, caplog):
    c, webcam = cam
    webcam.honours = False
    c._read_one(0, "plant")
    assert 0 not in c._exposure and webcam.auto is True
    assert any("does not accept a manual exposure" in r.message for r in caplog.records)


def test_fixed_and_per_camera_settings(tmp_path):
    c = Camera({"camera": {"exposure": {"plant": 120, "dashboard": "auto"}}}, base_dir=tmp_path)
    assert c._exposure_for("plant") == 120.0 and c._exposure_for("dashboard") == "auto"
    webcam = FakeWebcam()
    c._apply_exposure(webcam, 0, "plant")
    assert webcam.auto is False and webcam.exposure == 120.0
    assert Camera({"camera": {"exposure": "nonsense"}}, base_dir=tmp_path)._exposure_for("plant") == "auto_correct"


def test_never_trades_a_bright_photo_for_a_black_one(cam):
    c, webcam = cam
    real_read = webcam.read

    def cliff_read():                      # any manual exposure below 250 is pitch black
        if not webcam.auto and webcam.exposure < 250:
            return True, np.zeros((64, 64, 3), np.uint8)
        return real_read()
    webcam.read = cliff_read
    frame = c._read_one(0, "plant")
    assert Camera._light_stats(frame)[1] > 60           # kept the (washed-out) visible photo
    assert 0 not in c._exposure and webcam.auto is True


def test_live_view_goes_back_to_auto_when_the_light_goes_off(cam):
    c, webcam = cam
    c._read_one(0, "plant")                # learns a low exposure under the grow light
    assert 0 in c._exposure
    c._streams[0] = webcam
    webcam.scene = 0.02                    # light off
    c.read_stream_frame("plant")
    assert 0 not in c._exposure and webcam.auto is True
