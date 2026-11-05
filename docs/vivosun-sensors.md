# Reading the VIVOSUN controller directly (instead of through the camera)

Status: **prepared, switched off.** The grower still reads the tent's touchscreen
through the dashboard camera (`sensors.source: camera`).

## Why

Today the AI reads temperature/humidity/VPD off the touchscreen photo. That
ties up a camera, fails when the camera is bumped or the screen is dark, and
the AI has made up values when it couldn't read the screen (seen 2026-10-31).

## What the "dashboard" is

The touchscreen in the tent is a **VIVOSUN GrowHub controller (E42/E42A)**:
"Dashboard" page with inside temperature, humidity, VPD, outside temperature
and humidity, plus light %, exhaust (duct) fan and circulation fan. It sends
its readings to VIVOSUN's cloud, which the VIVOSUN phone app shows.

**It does not measure soil/rockwool moisture.** No GrowHub controller has a
soil sensor; the "soil moisture %" in past AI logs was the AI's estimate.

## Options

| | How | Needs someone at the farm? | Pros | Cons |
|---|---|---|---|---|
| **A. VIVOSUN cloud** (prepared) | Pi logs in to the VIVOSUN account and reads the controller's latest values every check-in | **No**, only the account login | Exact numbers, same as the app; frees the dashboard camera | Unofficial API (copied from the app via the community [Home Assistant integration](https://github.com/lientry/homeassistant-vivosun-growhub), MIT); a VIVOSUN app update can break it; account password stored on the Pi |
| B. Bluetooth / local Wi-Fi | Talk to the controller directly | Yes | No cloud | Protocol undocumented; would need reverse-engineering |
| C. Our own sensors | SHT31/SHT45 temperature-humidity sensor on the Pi's I2C pins (~$10-20) | Yes (wiring) | Fully ours, no cloud, can't break with app updates | Hardware work; doesn't replace the controller's display |
| D. Rockwool moisture | Load cell + HX711 under the tray (weight = water), or capacitive probe + ADS1115 ADC | Yes | The one reading nothing measures today | Hardware work; probes are unreliable in rockwool, weight works better |

**Recommendation:** A now (remote, no hardware), C/D next time someone is on
site if the team wants independence from VIVOSUN's cloud and real moisture data.

## What's already built (A)

- `src/vivosun_cloud.py`: read-only client (login, find the GrowHub
  controller, latest climate row). Never changes controller settings.
- `config.yaml` `sensors.source`: `camera` (default) or `vivosun`.
- With `vivosun`: every check-in fetches the controller's readings first; the
  AI is told they're measured and not to read the dashboard; camera-read values
  can't overwrite them. **If the cloud fails, it falls back to the camera** and
  shows an error in the website's AI activity feed.
- Tests: `tests/test_vivosun_cloud.py` (fake VIVOSUN server, encryption round trip).

Not verified yet: the real VIVOSUN servers (no account login has been tried).

## Switching it on (when the team decides)

1. Find the email/password of the VIVOSUN app account the controller is
   registered to (whoever set up the app). Prefer a separate account shared
   with the controller in the app, so a personal password isn't stored on the Pi.
2. On the Pi, add to `~/VIP-Vertical-Farm/.env`:
   ```
   VIVOSUN_EMAIL=...
   VIVOSUN_PASSWORD=...
   ```
3. Test once on the Pi: `venv/bin/python -m src.vivosun_cloud`
   It lists the account's devices and prints one reading. Compare it with the app.
4. Website → Control Center → Settings → Config editor → `config.local.yaml`:
   ```
   sensors:
     source: vivosun
   ```
   Save (with restart). The next check-in's activity shows "Sensors from VIVOSUN controller: ...".
5. The dashboard camera is then free: point it at the plants from another angle.

To switch back: set `source: camera` (or remove the lines) and save.
