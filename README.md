# VIP Vertical Farm — AI Plant Grower

An AI-powered plant growing system that uses Google Gemini to make autonomous growing decisions. The AI observes the plant through sensors and cameras, reasons about what it needs, and takes action — proving that LLM-guided growing can outperform human growers.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│              NVIDIA Jetson Nano (4GB)                │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │ Sensors   │  │ Camera   │  │ Actuators        │  │
│  │ Dashboard │  │ USB      │  │ Water Pump       │  │
│  │ (via cam) │  │ Webcam   │  │ Grow Lights      │  │
│  │           │  │          │  │                   │  │
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
│  │                    │                            │  │
│  │  ┌─────────────────▼───────────────────────┐   │  │
│  │  │  Gemini Interactions API                │   │  │
│  │  │  • Function calling (11 tools)          │   │  │
│  │  │  • Image analysis                       │   │  │
│  │  │  • Stateful conversation chains         │   │  │
│  │  │  • Thinking / reasoning                 │   │  │
│  │  └─────────────────────────────────────────┘   │  │
│  │                    │                            │  │
│  │  ┌─────────────────▼───────────────────────┐   │  │
│  │  │  Scheduler                              │   │  │
│  │  │  • AI decides when to check in next     │   │  │
│  │  │  • 5 min (urgent) to 8 hr (stable)      │   │  │
│  │  └─────────────────────────────────────────┘   │  │
│  └─────────────────────────────────────────────────┘  │
│        │                                             │
│  ┌─────▼───────────────────────────────────────────┐  │
│  │  REST API (FastAPI)                             │  │
│  │  /api/status, /api/decisions, /api/control/...  │  │
│  └─────────────────────────────────────────────────┘  │
└────────────────────────┬────────────────────────────┘
                         │
                         ▼
              ┌─────────────────────┐
              │  Google Cloud VM    │
              │  Mobile App Backend │
              └──────────┬──────────┘
                         │
                         ▼
              ┌─────────────────────┐
              │  Mobile App         │
              │  Monitor & Control  │
              └─────────────────────┘
```

## How the AI Thinks

Each check-in, the AI receives:

1. **System prompt** — Its role, the plant's species, ideal conditions for the current growth stage
2. **Growth summary** — AI-compressed history of the entire grow (self-summarized periodically)
3. **Recent decisions** — Last 15 decisions with full reasoning, so it can learn from patterns
4. **Sensor trends** — 24hr min/max/avg for all sensors
5. **Current readings** — Live sensor data
6. **Plant image** — Latest camera capture

The AI then:
- **Observes**: Analyzes sensor data and the plant image
- **Assesses**: Compares to ideal conditions for the growth stage
- **Reasons**: Considers trends, not just current values
- **Acts**: Uses tools to water, adjust lights
- **Plans**: Decides when to check in next (adaptive scheduling)
- **Records**: Logs milestones and growth measurements

### Context Compression

The AI periodically compresses its own history into a summary — capturing key learnings, what worked, what didn't, and growth patterns. This means it never loses long-term context even over weeks/months of growing.

## Hardware Setup

### Required Components

| Component | Purpose | Connection |
|-----------|---------|------------|
| NVIDIA Jetson Nano (4GB) | Main controller | — |
| USB webcam (plant) | Plant imaging | USB |
| USB webcam (dashboard) | Reads tent sensor display | USB |
| 5V relay module | Water pump control | GPIO 17 (BCM) |
| 12V peristaltic pump | Water delivery | Via relay |
| LED grow light strip | Supplemental lighting | GPIO 27 (BCM) via relay |

### Wiring Diagram

```
Relay 1 → GPIO 17 (physical pin 11) → Water pump
Relay 2 → GPIO 27 (physical pin 13) → Grow lights
Relay VCC → Jetson 5V pin
Relay GND → Jetson GND pin
USB Webcam 1 → USB port (plant camera)
USB Webcam 2 → USB port (dashboard camera)
```

Note: The Jetson Nano 40-pin header is compatible with Raspberry Pi GPIO layout. BCM pin numbers are the same.

## Quick Start

### 1. Flash JetPack OS

Flash NVIDIA JetPack to a microSD card using:
- [NVIDIA SDK Manager](https://developer.nvidia.com/sdk-manager) (recommended)
- [balenaEtcher](https://etcher.balena.io/) with a JetPack image

### 2. Setup the Jetson

After completing JetPack first-boot setup, run from your laptop:

```bash
git clone https://github.com/CoolGuy2982/VIP-Vertical-Farm.git
cd VIP-Vertical-Farm

# One-command setup (clones repo on Jetson, installs deps, creates service)
python flash_jetson.py --host <jetson-ip> --ssid "YourWiFi" --wifi-password "secret" --embed-env
```

Or deploy code updates to an already-configured Jetson:

```bash
python setup_device.py --host <jetson-ip> --user jetson
```

### 3. Configure

Edit `config.yaml`:
- Set your plant species and planting date
- Adjust GPIO pins if your wiring differs

Create `.env` on the Jetson:
```bash
GEMINI_API_KEY=your-gemini-api-key
API_SECRET_KEY=your-api-secret
```

### 4. Run

```bash
python -m src.main
```

The AI grower starts immediately, and the API server runs on port 8080.

### 5. Test hardware

```bash
python test_device.py --host <jetson-ip> --test all
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/status` | Full system status |
| GET | `/api/sensors` | Current sensor readings |
| GET | `/api/sensors/trends?hours=24` | Sensor trends |
| GET | `/api/decisions?count=20` | Recent AI decisions |
| GET | `/api/growth/milestones` | Growth timeline |
| GET | `/api/growth/summary` | AI growth summary |
| GET | `/api/camera/latest` | Latest plant photo |
| POST | `/api/camera/capture` | Take a photo now |
| POST | `/api/control/pump` | Manual watering |
| POST | `/api/control/lights/on` | Turn on grow lights |
| POST | `/api/control/lights/off` | Turn off grow lights |
| POST | `/api/control/checkin` | Force AI check-in |

## AI Tools

The AI has access to these tools:

| Tool | Description |
|------|-------------|
| `capture_plant` | Take a plant photo |
| `capture_dashboard` | Take a dashboard photo |
| `report_sensors` | Log extracted sensor values |
| `run_pump(seconds)` | Run water pump |
| `turn_on_lights(minutes)` | Control grow lights with auto-off |
| `turn_off_lights` | Turn off grow lights |
| `observe_in(minutes)` | Schedule follow-up observation |
| `schedule_checkin(minutes)` | Set next check-in time |
| `log_milestone(description)` | Record growth milestone |
| `get_growth_history` | View past milestones |
| `get_sensor_history(hours)` | View sensor trends |
| `get_decision_log(count)` | Review past decisions |
| `emergency_alert(message)` | Send urgent alert |

## Development

On non-Jetson platforms, actuators run in simulation mode — you can develop and test the AI logic without hardware.

```bash
# Run in dev mode (simulated actuators)
python -m src.main

# The API will be at http://localhost:8080
# View docs at http://localhost:8080/docs
```
