# VIP Vertical Farm — AI Plant Grower

An AI-powered plant growing system that uses Google Gemini to make autonomous growing decisions. The AI observes the plant through cameras, reasons about what it needs, and takes action — with the goal of outperforming human growers.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│           NVIDIA Jetson Orin Nano                    │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │ Sensors   │  │ Cameras  │  │ Actuators        │  │
│  │ (via      │  │ 2x USB   │  │ Grow Light       │  │
│  │  camera)  │  │ Webcam   │  │ (GPIO relay)     │  │
│  └─────┬─────┘  └────┬─────┘  └────────┬─────────┘  │
│        │              │                 │            │
│  ┌─────▼──────────────▼─────────────────▼─────────┐  │
│  │              AI Grower Agent                    │  │
│  │  ┌─────────────────────────────────────────┐   │  │
│  │  │  Context Manager                        │   │  │
│  │  │  • Decision log with full reasoning     │   │  │
│  │  │  • Growth milestones & measurements     │   │  │
│  │  │  • AI self-compression of history       │   │  │
│  │  │  • Sensor trends                        │   │  │
│  │  └─────────────────────────────────────────┘   │  │
│  │  ┌─────────────────────────────────────────┐   │  │
│  │  │  Gemini API                             │   │  │
│  │  │  • Function calling (13 tools)          │   │  │
│  │  │  • Image analysis (plant + dashboard)   │   │  │
│  │  │  • Stateful conversation chains         │   │  │
│  │  │  • Extended thinking / reasoning        │   │  │
│  │  └─────────────────────────────────────────┘   │  │
│  │  ┌─────────────────────────────────────────┐   │  │
│  │  │  Scheduler                              │   │  │
│  │  │  • AI decides when to check in next     │   │  │
│  │  │  • 1 min (urgent) to 8 hr (stable)      │   │  │
│  │  └─────────────────────────────────────────┘   │  │
│  └─────────────────────────────────────────────────┘  │
│        │                                             │
│  ┌─────▼───────────────────────────────────────────┐  │
│  │  REST API (FastAPI, port 8080)                  │  │
│  │  /api/status, /api/decisions, /api/control/...  │  │
│  └─────────────────────────────────────────────────┘  │
└────────────────────────┬────────────────────────────┘
                         │ Firebase sync
                         ▼
              ┌─────────────────────┐
              │  Firebase / Cloud   │
              │  Firestore + Storage│
              └─────────────────────┘
```

```
                    ┌──────────────────────┐
                    │  TP-Link Kasa Cloud  │
                    │  (internet)          │
                    └──────────┬───────────┘
                               │ tplink-cloud-api
                               ▼
                    ┌──────────────────────┐
                    │  Kasa HS103 Plug     │
                    │  Water Pump          │
                    └──────────────────────┘
```

## How the AI Thinks

Each check-in, the AI receives:

1. **System prompt** — Its role, the plant's species, ideal conditions for the current growth stage
2. **Growth summary** — AI-compressed history of the entire grow (self-summarized every 24h)
3. **Recent decisions** — Last 15 decisions with full reasoning
4. **Sensor trends** — 24hr min/max/avg for all sensors
5. **Current readings** — Latest sensor values
6. **Two camera images** — Plant camera + dashboard camera (sensor display)

The AI then:
- **Observes**: Reads sensor values from the dashboard image, assesses plant health from the plant image
- **Assesses**: Calculates VPD, compares conditions to growth stage targets
- **Reasons**: Considers trends over time, not just current snapshots
- **Acts**: Waters, adjusts lights, logs milestones
- **Plans**: Schedules its next check-in (adaptive, 1 min–8 hr)
- **Closes loops**: Uses `observe_in` to schedule follow-up observations after every action

### Context Compression

Every 24 hours the AI compresses its full decision history into a structured summary — capturing key learnings, what worked, growth patterns, and current state. This means long-term memory is never lost even over weeks of growing.

## Hardware Setup

### Components

| Component | Purpose | Interface |
|-----------|---------|-----------|
| NVIDIA Jetson Orin Nano | Main controller | — |
| USB webcam × 2 | Plant imaging + dashboard reading | USB |
| 5V relay module | Grow light control | GPIO BOARD Pin 13 (Active-Low) |
| LED grow light | Supplemental lighting | Via relay |
| TP-Link Kasa HS103 | Water pump control | Wi-Fi (Kasa cloud API) |
| Peristaltic pump | Water delivery | Via Kasa smart plug |

### Wiring (40-pin header)

```
Pin 2  (5V)  → Relay VCC
Pin 6  (GND) → Relay GND
Pin 13        → Relay signal → Grow light   (Active-Low)

Water pump → Kasa HS103 smart plug → wall outlet
                 ↑
           controlled over internet via tplink-cloud-api
```

> **Active-Low logic**: pulling Pin 13 LOW turns the relay ON. The code sets `ON_STATE = GPIO.LOW`.

### Pinmux (Orin Nano only — required before first use)

The Orin Nano locks GPIO pins at the hardware level by default. You must apply the DTS overlay once:

```bash
sudo bash apply_pinmux_fix.sh
sudo /opt/nvidia/jetson-io/jetson-io.py
# Select: Configure Jetson 40pin Header
# Select: All GPIO pins bidirectional v2
# Select: Save and reboot to reconfigure pins
```

## Setup

### 1. Flash JetPack OS

Flash NVIDIA JetPack to a microSD card using:
- [NVIDIA SDK Manager](https://developer.nvidia.com/sdk-manager) (recommended)
- [balenaEtcher](https://etcher.balena.io/) with a JetPack image

### 2. Deploy to Jetson

After JetPack first-boot, run from your laptop:

```bash
git clone https://github.com/CoolGuy2982/VIP-Vertical-Farm.git
cd VIP-Vertical-Farm

# Full first-time setup (installs deps, creates systemd service)
python flash_jetson.py --host <jetson-ip> --embed-env

# Push code updates to an already-configured Jetson
python setup_device.py --host <jetson-ip>
```

### 3. Configure

**`config.yaml`** — set your plant and planting date:
```yaml
plant:
  variety: "Diversified Lettuce Tray"
  planted_date: "2026-04-06"

kasa_cloud:
  device_alias: "Water Pump"   # must match the name in your Kasa app exactly
```

**`.env`** — create this file on the Jetson:
```
GEMINI_API_KEY=your-gemini-api-key
API_SECRET_KEY=your-api-secret
KASA_USERNAME=your-kasa-account-email
KASA_PASSWORD=your-kasa-password
```

### 4. Run

```bash
source venv/bin/activate
python -m src.main
```

The AI grower starts immediately and the REST API is available on port 8080.

### 5. Test hardware

```bash
sudo venv/bin/python3 test_hardware.py
```

This tests both the physical light relay (Pin 13) and the Kasa cloud pump connection.

### 6. Reset experiment data

To wipe all logs and images and start fresh:

```bash
bash reset_experiment.sh
```

Then clear the matching data in Firebase Console (Firestore + Storage).

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/status` | Full system status |
| GET | `/api/sensors` | Current sensor readings |
| GET | `/api/sensors/trends?hours=24` | Sensor trends |
| GET | `/api/decisions?count=20` | Recent AI decisions |
| GET | `/api/decisions/all` | Full decision history |
| GET | `/api/growth/milestones` | Growth timeline |
| GET | `/api/growth/summary` | AI-compressed growth summary |
| GET | `/api/growth/measurements` | Numeric growth data |
| GET | `/api/actions/pending` | Scheduled actions queue |
| DELETE | `/api/actions/{id}` | Cancel a scheduled action |
| GET | `/api/camera/plant/latest` | Latest plant image |
| GET | `/api/camera/dashboard/latest` | Latest dashboard image |
| POST | `/api/camera/plant/capture` | Capture plant image now |
| POST | `/api/camera/dashboard/capture` | Capture dashboard image now |
| POST | `/api/control/pump` | Manual watering `{"seconds": 5}` |
| POST | `/api/control/lights/on` | Turn on lights `{"minutes": 60}` |
| POST | `/api/control/lights/off` | Turn off lights |
| POST | `/api/control/observe` | Schedule observation |
| POST | `/api/control/checkin` | Force immediate AI check-in |
| GET | `/api/alerts` | Alert log |
| GET | `/api/health` | Health check |

## AI Tools

| Tool | Description |
|------|-------------|
| `capture_plant` | Take a plant photo |
| `capture_dashboard` | Take a dashboard/sensor display photo |
| `report_sensors` | Log sensor values extracted from the dashboard image |
| `run_pump(seconds)` | Run water pump via Kasa cloud |
| `turn_on_lights(minutes)` | Turn on grow light with auto-off timer |
| `turn_off_lights` | Turn off grow light immediately |
| `observe_in(delay_minutes, context)` | Schedule a follow-up observation |
| `schedule_checkin(minutes, reason)` | Set next check-in time |
| `get_pending_actions` | View scheduled action queue |
| `cancel_action(action_id)` | Cancel a pending action |
| `log_milestone(description, stage, measurements)` | Record a growth milestone |
| `get_growth_history` | View past milestones and growth summary |
| `get_sensor_history(hours)` | View sensor trends |
| `get_decision_log(count)` | Review recent decisions |
| `emergency_alert(message, severity)` | Log an urgent alert |
