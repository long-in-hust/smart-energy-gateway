"""
Energy Gateway REST API
Provides read access to gateway state and allows manual load control.

Endpoints:
  GET  /health
  GET  /loads
  GET  /loads/{load_id}/state
  GET  /energy/summary
  GET  /events
  POST /loads/{load_id}/command
  GET  /loads/{load_id}/events
  PUT  /config/threshold            ← bonus: change overload threshold at runtime
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("energy-api")

# ── Config ────────────────────────────────────────────────────────────────────
MQTT_BROKER    = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT      = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USERNAME  = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD  = os.environ.get("MQTT_PASSWORD", "")
STATE_FILE     = os.environ.get("GATEWAY_STATE_FILE", "/tmp/gateway_state.json")
API_HOST       = os.environ.get("API_HOST", "0.0.0.0")
API_PORT       = int(os.environ.get("API_PORT", 8000))

KNOWN_LOADS    = ["hvac", "lighting", "plug"]
START_TIME     = time.time()

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="Smart Energy Management Gateway API",
    description="Monitor and control virtual smart loads",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── MQTT client (publish-only) ────────────────────────────────────────────────
_mqtt_client: Optional[mqtt.Client] = None


def get_mqtt() -> mqtt.Client:
    global _mqtt_client
    if _mqtt_client is None or not _mqtt_client.is_connected():
        client = mqtt.Client(
            client_id=f"energy-api-{int(time.time())}",
        )
        if MQTT_USERNAME:
            client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        for attempt in range(1, 11):
            try:
                client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
                client.loop_start()
                _mqtt_client = client
                logger.info("MQTT connected")
                return client
            except Exception as exc:
                logger.warning("MQTT connect attempt %d/10: %s", attempt, exc)
                time.sleep(2)
        raise HTTPException(status_code=503, detail="MQTT broker unreachable")
    return _mqtt_client


# ── State reader ──────────────────────────────────────────────────────────────
def read_state() -> dict:
    """Read persisted state written by gateway.py"""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        logger.warning("State file corrupt: %s", exc)
        return {}


# ── Pydantic schemas ──────────────────────────────────────────────────────────
class LoadCommand(BaseModel):
    action: str = Field(..., pattern="^(on|off)$", description="on or off")
    reason: str = Field(default="manual_control")


class ThresholdUpdate(BaseModel):
    threshold_watt: float = Field(..., gt=0, description="New overload threshold in Watts")


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health", tags=["System"])
def health():
    state = read_state()
    loads_seen = list(state.get("meter_states", {}).keys())
    return {
        "status":    "ok",
        "uptime_s":  round(time.time() - START_TIME, 1),
        "loads_seen": loads_seen,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@app.get("/loads", tags=["Loads"])
def list_loads():
    state = read_state()
    meter_states = state.get("meter_states", {})
    result = []
    for load_id in KNOWN_LOADS:
        m = meter_states.get(load_id, {})
        result.append({
            "load_id":    load_id,
            "switch":     m.get("switch", "unknown"),
            "power_watt": m.get("power_watt", 0.0),
            "priority":   m.get("priority", "unknown"),
        })
    return {"loads": result}


@app.get("/loads/{load_id}/state", tags=["Loads"])
def get_load_state(load_id: str):
    if load_id not in KNOWN_LOADS:
        raise HTTPException(status_code=404, detail=f"Load '{load_id}' not found")
    state = read_state()
    meter = state.get("meter_states", {}).get(load_id)
    if not meter:
        raise HTTPException(status_code=404, detail=f"No data for load '{load_id}' yet")
    return {"load_id": load_id, "state": meter}


@app.get("/loads/{load_id}/events", tags=["Loads"])
def get_load_events(load_id: str, limit: int = 20):
    if load_id not in KNOWN_LOADS:
        raise HTTPException(status_code=404, detail=f"Load '{load_id}' not found")
    state = read_state()
    all_events = state.get("events", [])
    load_events = [e for e in all_events if e.get("load_id") == load_id]
    return {"load_id": load_id, "events": load_events[-limit:]}


@app.get("/energy/summary", tags=["Energy"])
def get_summary():
    state = read_state()
    summary = state.get("summary", {})
    if not summary:
        return {"message": "No summary data yet — gateway may be starting up"}
    return summary


@app.get("/events", tags=["Events"])
def get_all_events(limit: int = 50):
    state = read_state()
    events = state.get("events", [])
    return {"count": len(events), "events": events[-limit:]}


@app.post("/loads/{load_id}/command", tags=["Control"])
def send_load_command(load_id: str, cmd: LoadCommand):
    if load_id not in KNOWN_LOADS:
        raise HTTPException(status_code=404, detail=f"Load '{load_id}' not found")

    mqtt_client = get_mqtt()
    topic = f"energy/{load_id}/load/command"
    payload = {
        "load_id":   load_id,
        "target":    "load_switch",
        "action":    cmd.action,
        "reason":    cmd.reason,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    result = mqtt_client.publish(topic, json.dumps(payload))
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        raise HTTPException(status_code=500, detail="Failed to publish MQTT command")

    logger.info("Manual command: load=%s action=%s reason=%s", load_id, cmd.action, cmd.reason)
    return {
        "status":    "command_sent",
        "load_id":   load_id,
        "action":    cmd.action,
        "reason":    cmd.reason,
        "topic":     topic,
        "timestamp": payload["timestamp"],
    }


@app.put("/config/threshold", tags=["Config"])
def update_threshold(body: ThresholdUpdate):
    """
    Bonus endpoint: update the overload threshold.
    Publishes a config message that the gateway subscribes to.
    """
    mqtt_client = get_mqtt()
    topic = "energy/gateway/config"
    payload = {
        "overload_threshold_watt": body.threshold_watt,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    mqtt_client.publish(topic, json.dumps(payload))
    logger.info("Threshold update published: %.1fW", body.threshold_watt)
    return {"status": "ok", "new_threshold_watt": body.threshold_watt}


# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    logger.info("Energy API starting on %s:%d", API_HOST, API_PORT)
    # Pre-connect MQTT
    try:
        get_mqtt()
    except Exception as exc:
        logger.warning("MQTT pre-connect failed (will retry on first request): %s", exc)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host=API_HOST, port=API_PORT, reload=False)
