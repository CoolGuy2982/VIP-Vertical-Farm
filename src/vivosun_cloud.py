"""Read climate data from the VIVOSUN GrowHub controller (E42A) via VIVOSUN's cloud.

READ-ONLY: this never changes anything on the controller.

The tent's touchscreen ("Dashboard": inside/outside temperature, humidity,
VPD) is a VIVOSUN GrowHub controller. Its readings are uploaded to VIVOSUN's
cloud, which the VIVOSUN phone app reads. There is no official API; this
client copies what the app does, as documented by the community Home Assistant
integration https://github.com/lientry/homeassistant-vivosun-growhub (MIT):

  POST /user/login                 email + password -> loginToken, accessToken
  GET  /iot/device/getTotalList    devices (the controller's sceneId/deviceId)
  POST /iot/data/getPointLog       recent climate rows (encrypted request body)

Values arrive as integers x100 (2215 = 22.15 C); -6666 means "no sensor".
The controller has no soil-moisture sensor.

Off by default. Turn on with config  sensors: {source: vivosun}  and put the
VIVOSUN app account in .env (VIVOSUN_EMAIL, VIVOSUN_PASSWORD). Try it first:
    python -m src.vivosun_cloud            # lists devices, prints one reading
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import string
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api-prod.next.vivosun.com"
LOGIN_PATH = "/user/login"
DEVICE_LIST_PATH = "/iot/device/getTotalList"
POINT_LOG_PATH = "/iot/data/getPointLog"
# The cloud rejects "outdated" apps; these mirror the Android app (Oct 2026).
APP_VERSION = "4.63.1"
API_PROTOCOL_VERSION = "1.0.5"
TIMEOUT_S = 15
POINT_LOG_WINDOW_S = 300
UNAVAILABLE = -6666
SCALE = 100

# raw key -> (our name, conversion)
FIELDS = {
    "inTemp": "temperature_c",
    "inHumi": "humidity_pct",
    "inVpd": "vpd_kpa",
    "outTemp": "outside_temperature_c",
    "outHumi": "outside_humidity_pct",
    "outVpd": "outside_vpd_kpa",
}
# Some models report the tent probe under these names instead (E42A+, A22)
FALLBACKS = {"inTemp": ("pTemp", "bTemp"), "inHumi": ("pHumi", "bHumi"), "inVpd": ("pVpd", "bVpd")}


class VivosunError(Exception):
    pass


class VivosunAuthError(VivosunError):
    pass


def encrypt_request_body(plaintext: bytes, timestamp_ms: int) -> tuple[str, str, bytes]:
    """The app's request encryption (AES-CBC, key/IV slices sent in Request-Code).

    Returns (Request-Time header, Request-Code header, body).
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    md5_hex = hashlib.md5(str(timestamp_ms).encode()).hexdigest()
    key_len = secrets.choice((16, 24, 32))
    key_start = secrets.randbelow(32 - key_len + 1)
    key_end = key_start + key_len
    alphabet = string.ascii_letters + string.digits
    salt_len = 16 + secrets.randbelow(84)
    salt = "".join(secrets.choice(alphabet) for _ in range(salt_len))
    iv_start = secrets.randbelow(salt_len - 16 + 1)
    iv_end = iv_start + 16

    padder = PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(md5_hex[key_start:key_end].encode()),
                       modes.CBC(salt[iv_start:iv_end].encode())).encryptor()
    content_hex = (encryptor.update(padded) + encryptor.finalize()).hex()
    code = f"AC5-{key_start}-{key_end}-{iv_start}-{iv_end}-{salt}"
    return str(timestamp_ms), code, json.dumps({"content": content_hex}, separators=(",", ":")).encode()


def scale(raw) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        raw = int(raw)
    except (TypeError, ValueError):
        return None
    return None if raw == UNAVAILABLE else round(raw / SCALE, 2)


class VivosunCloud:
    def __init__(self, email: str, password: str, device_name: str | None = None,
                 base_url: str = API_BASE_URL):
        if not email or not password:
            raise VivosunAuthError("VIVOSUN_EMAIL / VIVOSUN_PASSWORD are not set in .env")
        self.email, self.password = email, password
        self.device_name = device_name
        self.base_url = base_url
        self._tokens: dict | None = None
        self._device: dict | None = None

    @classmethod
    def from_config(cls, config: dict) -> "VivosunCloud":
        opts = (config.get("sensors", {}) or {}).get("vivosun", {}) or {}
        return cls(os.environ.get("VIVOSUN_EMAIL", ""), os.environ.get("VIVOSUN_PASSWORD", ""),
                   device_name=opts.get("device_name"))

    # -- HTTP ----------------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None, auth: bool = True) -> dict:
        headers = {"Server-Platform": "android", "Api-Version": API_PROTOCOL_VERSION,
                   "App-Version": APP_VERSION}
        if auth:
            if self._tokens is None:
                self.login()
            headers["login-token"] = self._tokens["loginToken"]
            headers["access-token"] = self._tokens["accessToken"]
        data = None
        if body is not None:
            plaintext = json.dumps(body, separators=(",", ":")).encode()
            if method == "POST" and path != LOGIN_PATH:
                headers["Request-Time"], headers["Request-Code"], data = encrypt_request_body(
                    plaintext, int(time.time() * 1000))
            else:
                data = plaintext
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                payload = json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise VivosunAuthError(f"VIVOSUN login rejected (HTTP {e.code})") from e
            raise VivosunError(f"VIVOSUN cloud HTTP {e.code} for {path}") from e
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise VivosunError(f"VIVOSUN cloud unreachable for {path}: {e}") from e
        if not isinstance(payload, dict) or not payload.get("success"):
            message = str(payload.get("message", "no message")) if isinstance(payload, dict) else "bad JSON"
            if any(m in message.lower() for m in ("auth", "credential", "forbidden", "invalid",
                                                  "login", "password", "token", "unauthorized")):
                raise VivosunAuthError(f"VIVOSUN: {message}")
            raise VivosunError(f"VIVOSUN: {message}")
        return payload.get("data") or {}

    def _authed(self, method: str, path: str, body: dict | None = None) -> dict:
        """Authenticated call; one fresh login if the tokens have expired."""
        try:
            return self._request(method, path, body)
        except VivosunAuthError:
            self._tokens = None
            return self._request(method, path, body)

    # -- API -----------------------------------------------------------------

    def login(self):
        data = self._request("POST", LOGIN_PATH, {
            "email": self.email, "password": self.password, "spAppId": "com.vivosun.android",
            "spClientId": str(uuid.uuid4()), "spSessionId": str(uuid.uuid4())}, auth=False)
        if not data.get("loginToken") or not data.get("accessToken"):
            raise VivosunAuthError("VIVOSUN login returned no tokens")
        self._tokens = data
        logger.info("VIVOSUN cloud: logged in")

    def devices(self) -> list[dict]:
        groups = self._authed("GET", DEVICE_LIST_PATH).get("deviceGroup") or {}
        out = []
        for items in groups.values():
            for d in items if isinstance(items, list) else []:
                if not isinstance(d, dict):
                    continue
                client = str(d.get("clientId") or "")
                model = client.split("-")[1].upper() if client.count("-") >= 1 else ""
                out.append({"name": d.get("name"), "deviceId": d.get("deviceId"),
                            "sceneId": (d.get("scene") or {}).get("sceneId"),
                            "model": model, "online": d.get("onlineStatus") == 1,
                            "is_controller": model.startswith("VSCTL")})
        return out

    def controller(self) -> dict:
        if self._device is None:
            devs = [d for d in self.devices() if d["is_controller"] and d["sceneId"]]
            if self.device_name:
                devs = [d for d in devs if str(d["name"]).lower() == self.device_name.lower()]
            if not devs:
                raise VivosunError("No GrowHub controller found on this VIVOSUN account"
                                   + (f" named '{self.device_name}'" if self.device_name else ""))
            self._device = devs[0]
        return self._device

    def read_climate(self) -> dict:
        """Latest reading: temperature_c, humidity_pct, vpd_kpa, outside_* (+ device info)."""
        dev = self.controller()
        now = int(time.time())
        data = self._authed("POST", POINT_LOG_PATH, {
            "sceneId": dev["sceneId"], "deviceId": dev["deviceId"],
            "startTime": now - POINT_LOG_WINDOW_S, "endTime": now,
            "reportType": 0, "orderBy": "asc", "timeLevel": "ONE_MINUTE"})
        rows = [r for r in (data.get("iotDataLogList") or []) if isinstance(r, dict)]
        if not rows:
            raise VivosunError(f"No readings from '{dev['name']}' in the last "
                               f"{POINT_LOG_WINDOW_S // 60} min (controller offline?)")
        row = rows[-1]
        out = {}
        for raw_key, name in FIELDS.items():
            value = scale(row.get(raw_key))
            for alt in FALLBACKS.get(raw_key, ()):
                if value is None:
                    value = scale(row.get(alt))
            out[name] = value
        out.update(device=dev["name"], model=dev["model"],
                   read_at=datetime.now().isoformat(timespec="seconds"))
        return out


def main():
    """Manual test: python -m src.vivosun_cloud  (needs VIVOSUN_EMAIL/PASSWORD in .env)."""
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cloud = VivosunCloud(os.environ.get("VIVOSUN_EMAIL", ""), os.environ.get("VIVOSUN_PASSWORD", ""))
    print("Devices on this VIVOSUN account:")
    for d in cloud.devices():
        print(f"  {d['name']!r:30} model={d['model']:12} online={d['online']} controller={d['is_controller']}")
    print("\nLatest climate reading:")
    print(json.dumps(cloud.read_climate(), indent=2))


if __name__ == "__main__":
    main()
