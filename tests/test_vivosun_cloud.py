import hashlib
import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from src import vivosun_cloud as vc
from src.sensors import Sensors


def decrypt(request_time: str, request_code: str, body: bytes) -> dict:
    """What the VIVOSUN server does with an encrypted body."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    _, ks, ke, ivs, ive, salt = request_code.split("-", 5)
    key = hashlib.md5(request_time.encode()).hexdigest()[int(ks):int(ke)].encode()
    iv = salt[int(ivs):int(ive)].encode()
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = dec.update(bytes.fromhex(json.loads(body)["content"])) + dec.finalize()
    unpadder = PKCS7(128).unpadder()
    return json.loads(unpadder.update(padded) + unpadder.finalize())


class FakeServer:
    """Stands in for api-prod.next.vivosun.com."""

    def __init__(self):
        self.requests = []
        self.expire_once = False
        self.rows = [{"inTemp": 2150, "inHumi": 6420, "inVpd": 92, "outTemp": 2310,
                      "outHumi": 4100, "outVpd": -6666},
                     {"inTemp": 2215, "inHumi": 8230, "inVpd": 48, "outTemp": 2290,
                      "outHumi": 4000, "outVpd": 168}]

    def urlopen(self, req, timeout=None):
        path = req.full_url.replace(vc.API_BASE_URL, "")
        h = {k.lower(): v for k, v in req.header_items()}
        self.requests.append((req.get_method(), path, h))
        assert h["app-version"] == vc.APP_VERSION and h["server-platform"] == "android"
        if path == vc.LOGIN_PATH:
            body = json.loads(req.data)
            assert body["password"] == "pw" and body["spAppId"] == "com.vivosun.android"
            return self._ok({"loginToken": "L", "accessToken": "A", "refreshToken": "R", "userId": "u"})
        assert h["login-token"] == "L" and h["access-token"] == "A"
        if self.expire_once:
            self.expire_once = False
            return self._json({"success": False, "message": "token expired, please login"})
        if path == vc.DEVICE_LIST_PATH:
            return self._ok({"deviceGroup": {
                "LIGHT": [{"name": "Grow light", "deviceId": "9", "clientId": "vs-VSLGT1-x",
                           "scene": {"sceneId": "s"}}],
                "CTRL": [{"name": "Tent", "deviceId": "42", "clientId": "vs-VSCTLE42A-abc",
                          "topicPrefix": "t", "onlineStatus": 1, "scene": {"sceneId": "s1"}}]}})
        if path == vc.POINT_LOG_PATH:
            body = decrypt(h["request-time"], h["request-code"], req.data)
            assert body["deviceId"] == "42" and body["sceneId"] == "s1"
            assert body["endTime"] - body["startTime"] == vc.POINT_LOG_WINDOW_S
            return self._ok({"iotDataLogList": self.rows})
        raise AssertionError(path)

    def _ok(self, data):
        return self._json({"success": True, "message": "ok", "data": data})

    @staticmethod
    def _json(obj):
        return _Ctx(json.dumps(obj).encode())


class _Ctx:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return io.BytesIO(self.data)

    def __exit__(self, *a):
        return False


@pytest.fixture
def server(monkeypatch):
    s = FakeServer()
    monkeypatch.setattr(vc.urllib.request, "urlopen", s.urlopen)
    return s


def test_encryption_round_trip():
    t, code, body = vc.encrypt_request_body(b'{"a":1}', 1761900000000)
    assert t == "1761900000000" and code.startswith("AC5-")
    assert decrypt(t, code, body) == {"a": 1}


def test_reads_latest_climate_from_the_controller(server):
    cloud = vc.VivosunCloud("me@x.com", "pw")
    r = cloud.read_climate()
    assert r["temperature_c"] == 22.15 and r["humidity_pct"] == 82.3 and r["vpd_kpa"] == 0.48
    assert r["outside_temperature_c"] == 22.9 and r["outside_vpd_kpa"] == 1.68
    assert r["device"] == "Tent" and r["model"] == "VSCTLE42A"
    # login is plain JSON, every other POST is encrypted
    login = [q for q in server.requests if q[1] == vc.LOGIN_PATH][0]
    assert "request-code" not in login[2]


def test_unavailable_sentinel_and_probe_fallback(server):
    server.rows = [{"inTemp": -6666, "pTemp": 2000, "inHumi": 5000, "outVpd": -6666}]
    r = vc.VivosunCloud("me@x.com", "pw").read_climate()
    assert r["temperature_c"] == 20.0 and r["outside_vpd_kpa"] is None and r["vpd_kpa"] is None


def test_expired_token_logs_in_again(server):
    cloud = vc.VivosunCloud("me@x.com", "pw")
    cloud.read_climate()
    server.expire_once = True
    assert cloud.read_climate()["temperature_c"] == 22.15
    assert [q[1] for q in server.requests].count(vc.LOGIN_PATH) == 2


def test_no_rows_and_missing_credentials(server):
    server.rows = []
    with pytest.raises(vc.VivosunError, match="offline"):
        vc.VivosunCloud("me@x.com", "pw").read_climate()
    with pytest.raises(vc.VivosunAuthError):
        vc.VivosunCloud("", "")


def test_wrong_password_is_an_auth_error(monkeypatch):
    def reject(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b""))
    monkeypatch.setattr(vc.urllib.request, "urlopen", reject)
    with pytest.raises(vc.VivosunAuthError):
        vc.VivosunCloud("me@x.com", "bad").login()


def test_controller_readings_win_over_camera_reading():
    s = Sensors({"sensors": {"vivosun": {"max_age_minutes": 15}}})
    s.update_from_controller({"temperature_c": 22.15, "humidity_pct": 82.3, "vpd_kpa": 0.48,
                              "device": "Tent", "read_at": "now"})
    s.update_from_ai({"temperature_c": 30, "humidity_pct": 10})     # AI misreads the camera
    r = s.read_all()
    assert r["temperature_c"] == 22.15 and "VIVOSUN controller" in r["source"]
    assert "report_sensors" in r["note"]


def test_default_config_never_touches_the_cloud(monkeypatch):
    from src.ai_grower import AIGrower
    g = AIGrower.__new__(AIGrower)
    g.config = {"sensors": {"source": "camera"}}
    g._vivosun = None
    monkeypatch.setattr(vc.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network used"))
    g._read_controller()
    assert g._vivosun is None


def test_cloud_failure_falls_back_to_camera(monkeypatch, caplog):
    from src.ai_grower import AIGrower
    g = AIGrower.__new__(AIGrower)
    g.config = {"sensors": {"source": "vivosun"}}
    g._vivosun = SimpleNamespace(read_climate=lambda: (_ for _ in ()).throw(vc.VivosunError("down")))
    g.sensors = Sensors({})
    g.remote = None
    g._read_controller()
    assert "dashboard camera instead" in caplog.text
    assert g.sensors.read_all()["source"] == "dashboard_camera"


def _loop_grower(reading_fn):
    from src.ai_grower import AIGrower
    g = AIGrower.__new__(AIGrower)
    g.config = {"sensors": {"source": "vivosun", "vivosun": {"poll_minutes": 1, "log_minutes": 15}}}
    g._vivosun = SimpleNamespace(read_climate=reading_fn)
    g._controller_failing = False
    g.sensors = Sensors({})
    g.remote = None
    g.logged = []
    g.context = SimpleNamespace(log_sensors=lambda d: g.logged.append(("local", d)))
    g.firebase = SimpleNamespace(log_sensors=lambda d: g.logged.append(("firebase", d)))
    return g


def test_climate_loop_keeps_website_live_and_logs_every_15_min(monkeypatch):
    from src import ai_grower as ag
    g = _loop_grower(lambda: {"temperature_c": 22.1, "humidity_pct": 80.0, "vpd_kpa": 0.5,
                              "device": "Tent", "read_at": "now"})
    clock = {"t": 1_000_000.0, "rounds": 0}
    monkeypatch.setattr(ag.time, "time", lambda: clock["t"])

    def sleep(s):
        clock["rounds"] += 1
        clock["t"] += s
        if clock["rounds"] >= 3:
            raise StopIteration
    monkeypatch.setattr(ag.time, "sleep", sleep)
    with pytest.raises(StopIteration):
        g._climate_loop()
    # 3 polls one minute apart: website value fresh, but only one saved reading
    assert g.sensors.read_all()["temperature_c"] == 22.1
    assert [kind for kind, _ in g.logged] == ["local", "firebase"]
    saved = g.logged[1][1]
    assert "note" not in saved and saved["source"].startswith("VIVOSUN") and saved["read_at"] == "now"


def test_climate_loop_reports_an_outage_once(monkeypatch, caplog):
    from src import ai_grower as ag
    g = _loop_grower(lambda: (_ for _ in ()).throw(vc.VivosunError("cloud down")))
    rounds = {"n": 0}

    def sleep(s):
        rounds["n"] += 1
        if rounds["n"] >= 4:
            raise StopIteration
    monkeypatch.setattr(ag.time, "sleep", sleep)
    with pytest.raises(StopIteration):
        g._climate_loop()
    assert caplog.text.count("VIVOSUN controller readings unavailable") == 1
    assert g.logged == []


def test_prompt_says_climate_is_measured_only_in_vivosun_mode(tmp_path):
    import shutil
    from pathlib import Path
    from src.context_manager import ContextManager
    import yaml
    repo = Path(__file__).resolve().parent.parent
    (tmp_path / "prompts").mkdir()
    shutil.copy(repo / "prompts" / "system_prompt.md", tmp_path / "prompts" / "system_prompt.md")
    cfg = yaml.safe_load((repo / "config.yaml").read_text(encoding="utf-8"))
    assert "Climate data" not in ContextManager(str(tmp_path), cfg).build_system_prompt()
    cfg["sensors"]["source"] = "vivosun"
    prompt = ContextManager(str(tmp_path), cfg).build_system_prompt()
    assert prompt.rstrip().endswith("only report numbers you can read.")
    assert "MEASURED by the VIVOSUN GrowHub controller" in prompt and "Hard Limits" in prompt


def test_capture_dashboard_tool_not_needed_with_fresh_controller_data():
    from src.ai_grower import AIGrower
    g = AIGrower.__new__(AIGrower)
    g.config = {"sensors": {"source": "vivosun"}}
    g.sensors = Sensors({})
    g.camera = SimpleNamespace(capture_dashboard=lambda label: pytest.fail("camera used"))
    g.sensors.update_from_controller({"temperature_c": 22.0, "device": "Tent", "read_at": "now"})
    r = g._execute_tool("capture_dashboard", {})
    assert r["captured"] is False and "VIVOSUN controller" in r["note"]
