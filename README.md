# VIP Vertical Farm — AI Plant Grower

An AI-powered plant growing system that uses Google Gemini to make autonomous growing decisions. The AI observes the plant through sensors and cameras, reasons about what it needs, and takes action — proving that LLM-guided growing can outperform human growers.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  Raspberry Pi 4 (4GB)                │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │ Sensors   │  │ Camera   │  │ Actuators        │  │
│  │ DHT22     │  │ USB      │  │ Water Pump       │  │
│  │ Soil Moist│  │ Webcam   │  │ Grow Lights(PWM) │  │
│  │ Light     │  │          │  │ Fan (PWM)        │  │
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
- **Acts**: Uses tools to water, adjust lights, control fan
- **Plans**: Decides when to check in next (adaptive scheduling)
- **Records**: Logs milestones and growth measurements

### Context Compression

The AI periodically compresses its own history into a summary — capturing key learnings, what worked, what didn't, and growth patterns. This means it never loses long-term context even over weeks/months of growing.

## Hardware Setup

### Required Components

| Component | Purpose | Connection |
|-----------|---------|------------|
| Raspberry Pi 4 (4GB) | Main controller | — |
| DHT22 sensor | Temperature & humidity | GPIO 4 |
| Capacitive soil moisture sensor | Soil moisture | MCP3008 CH0 |
| Photoresistor + 10kΩ resistor | Light level | MCP3008 CH1 |
| MCP3008 ADC | Analog-to-digital conversion | SPI |
| USB webcam | Plant imaging | USB |
| 5V relay module | Water pump control | GPIO 17 |
| 12V peristaltic pump | Water delivery | Via relay |
| LED grow light strip (PWM) | Supplemental lighting | GPIO 18 (PWM) |
| 12V DC fan | Air circulation | GPIO 27 |

### Wiring Diagram

```
DHT22 → GPIO 4 (with 10kΩ pull-up to 3.3V)
MCP3008 → SPI0 (CLK=SCLK, MISO, MOSI, CS=CE0)
  CH0 ← Soil moisture sensor
  CH1 ← Photoresistor voltage divider
Relay → GPIO 17 → Water pump
MOSFET → GPIO 18 (PWM) → Grow lights
MOSFET → GPIO 27 → Fan
```

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/VIP-Vertical-Farm.git
cd VIP-Vertical-Farm
pip install -r requirements.txt

# On Raspberry Pi, also install GPIO libraries:
pip install adafruit-circuitpython-dht adafruit-circuitpython-mcp3xxx RPi.GPIO
```

### 2. Configure

Edit `config.yaml`:
- Set your plant species and planting date
- Adjust GPIO pins if your wiring differs
- Calibrate the water pump (ml_per_second)

### 3. Set environment variables

```bash
export GEMINI_API_KEY="your-gemini-api-key"
export API_SECRET_KEY="your-api-secret"
```

### 4. Run

```bash
python -m src.main
```

The AI grower starts immediately, and the API server runs on port 8080.

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
| POST | `/api/control/water` | Manual watering |
| POST | `/api/control/lights` | Set grow lights |
| POST | `/api/control/fan` | Set fan speed |
| POST | `/api/control/checkin` | Force AI check-in |

## AI Tools

The AI has access to 11 tools:

| Tool | Description |
|------|-------------|
| `read_sensors` | Read temperature, humidity, soil moisture, light |
| `water_plant(ml)` | Dispense precise amount of water |
| `set_grow_lights(brightness, hours)` | Control grow lights with auto-off |
| `set_fan(speed)` | Control ventilation fan |
| `capture_image` | Take a plant photo |
| `schedule_checkin(minutes)` | Set next check-in time |
| `log_milestone(description)` | Record growth milestone |
| `get_growth_history` | View past milestones |
| `get_sensor_history(hours)` | View sensor trends |
| `get_decision_log(count)` | Review past decisions |
| `emergency_alert(message)` | Send urgent alert |

## Development

On non-Pi platforms, sensors and actuators run in simulation mode — you can develop and test the AI logic without hardware.

```bash
# Run in dev mode (simulated sensors)
python -m src.main

# The API will be at http://localhost:8080
# View docs at http://localhost:8080/docs
```
