import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

app = FastAPI(
    title="VIP Vertical Farm - AI Grower",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_grower = None


def set_grower(grower):
    global _grower
    _grower = grower


def _g():
    if _grower is None:
        raise HTTPException(503, "AI Grower not initialized")
    return _grower


# status

@app.get("/api/status")
def get_status():
    return _g().get_status()


@app.get("/api/sensors")
def get_sensors():
    return _g().sensors.read_all()


@app.get("/api/sensors/trends")
def get_sensor_trends(hours: int = 24):
    return _g().context.get_sensor_trends(hours)


# decisions and growth

@app.get("/api/decisions")
def get_decisions(count: int = 20):
    return {"decisions": _g().context.get_recent_decisions(count)}


@app.get("/api/decisions/all")
def get_all_decisions():
    return {"decisions": _g().context.get_all_decisions()}


@app.get("/api/growth/milestones")
def get_milestones():
    return {"milestones": _g().context.get_milestones()}


@app.get("/api/growth/summary")
def get_growth_summary():
    return _g().context.get_growth_summary()


@app.get("/api/growth/measurements")
def get_measurements():
    return _g().growth.get_summary()


# scheduled actions

@app.get("/api/actions/pending")
def get_pending_actions():
    return {"pending": _g().scheduler.get_pending()}


@app.delete("/api/actions/{action_id}")
def cancel_action(action_id: str):
    cancelled = _g().scheduler.cancel(action_id)
    return {"cancelled": cancelled, "action_id": action_id}


# camera

@app.get("/api/camera/plant/latest")
def get_latest_plant_image():
    grower = _g()
    path = grower.camera.get_latest_image("plant")
    if path and Path(path).exists():
        return FileResponse(path, media_type="image/jpeg")
    raise HTTPException(404, "No plant images yet")


@app.get("/api/camera/dashboard/latest")
def get_latest_dashboard_image():
    grower = _g()
    path = grower.camera.get_latest_image("dashboard")
    if path and Path(path).exists():
        return FileResponse(path, media_type="image/jpeg")
    raise HTTPException(404, "No dashboard images yet")


@app.post("/api/camera/plant/capture")
def capture_plant_image():
    grower = _g()
    path = grower.camera.capture_plant("manual")
    if path:
        return {"image_path": path, "captured": True}
    raise HTTPException(500, "Plant camera capture failed")


@app.post("/api/camera/dashboard/capture")
def capture_dashboard_image():
    grower = _g()
    path = grower.camera.capture_dashboard("manual")
    if path:
        return {"image_path": path, "captured": True}
    raise HTTPException(500, "Dashboard camera capture failed")


# manual controls

class PumpRequest(BaseModel):
    seconds: float
    reason: str = "Manual"


class LightsOnRequest(BaseModel):
    minutes: float
    reason: str = "Manual"


class ObserveRequest(BaseModel):
    delay_minutes: float
    context: str


@app.post("/api/control/pump")
def manual_pump(req: PumpRequest):
    grower = _g()
    result = grower.actuators.run_pump(req.seconds)
    grower.context.log_decision({
        "day": grower.context.get_days_since_planting(),
        "trigger_type": "manual",
        "sensors": grower.sensors.read_all(),
        "observation": f"Manual pump: {req.seconds}s",
        "reasoning": req.reason,
        "actions": [{"tool": "run_pump", "args": {"seconds": req.seconds}}],
    })
    return result


@app.post("/api/control/lights/on")
def manual_lights_on(req: LightsOnRequest):
    return _g().actuators.turn_on_lights(req.minutes)


@app.post("/api/control/lights/off")
def manual_lights_off():
    return _g().actuators.turn_off_lights()


@app.post("/api/control/observe")
def schedule_observation(req: ObserveRequest):
    grower = _g()
    before = grower.sensors.read_all()
    action = grower.scheduler.schedule_observe(
        delay_minutes=req.delay_minutes,
        context=req.context,
        before_sensors=before,
    )
    return {"scheduled": True, "action_id": action.id,
            "fires_at": action.to_dict()["fire_at_readable"]}


@app.post("/api/control/checkin")
def force_checkin():
    _g().manual_checkin()
    return {"triggered": True}


# alerts

@app.get("/api/alerts")
def get_alerts():
    return {"alerts": _g()._alert_log}


# health

@app.get("/api/health")
def health():
    return {"status": "ok"}
