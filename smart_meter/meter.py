"""
Smart Meter Simulator
Simulates energy consumption for HVAC, Lighting, or Plug loads.
Publishes telemetry to MQTT: energy/{load_id}/meter/telemetry
"""

import json
import logging
import math
import os
import random
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("smart-meter")

# ── Config from environment ──────────────────────────────────────────────────
MQTT_BROKER       = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT         = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USERNAME     = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD     = os.environ.get("MQTT_PASSWORD", "")
LOAD_ID           = os.environ.get("LOAD_ID", "hvac")
DEVICE_ID         = os.environ.get("DEVICE_ID", "meter-hvac")
BASE_POWER        = float(os.environ.get("BASE_POWER", 1200.0))
PRIORITY          = os.environ.get("PRIORITY", "low")          # low | medium | high
PUBLISH_INTERVAL  = int(os.environ.get("PUBLISH_INTERVAL", 5))
VOLTAGE           = float(os.environ.get("VOLTAGE", 220.0))

TOPIC_TELEMETRY   = f"energy/{LOAD_ID}/meter/telemetry"
TOPIC_LOAD_STATUS = f"energy/{LOAD_ID}/load/status"
TOPIC_LOAD_CMD    = f"energy/{LOAD_ID}/load/command"

# ── State ────────────────────────────────────────────────────────────────────
state = {
    "switch": "on",
    "energy_wh": 0.0,
    "step": 0,
}


def simulate_power(step: int) -> float:
    """Simulate realistic power consumption with daily cycle + noise."""
    # Simulate a 24-hour cycle: peak during day hours
    hour_angle = (step * PUBLISH_INTERVAL / 3600) % 24  # virtual hour within day
    daily_factor = 0.5 + 0.5 * math.sin(math.pi * (hour_angle - 6) / 12)
    daily_factor = max(0.3, daily_factor)

    # Load-specific patterns
    if LOAD_ID == "hvac":
        # HVAC peaks midday, occasional spikes
        power = BASE_POWER * daily_factor
        if random.random() < 0.05:   # 5% chance of spike (overload scenario)
            power *= random.uniform(1.8, 2.5)
    elif LOAD_ID == "lighting":
        # Lighting is higher when dark (inverse of solar)
        light_factor = 1.0 - daily_factor * 0.6
        power = BASE_POWER * (0.4 + light_factor * 0.6)
        if random.random() < 0.03:
            power *= random.uniform(1.2, 1.5)
    else:  # plug
        # Plug loads: random bursts
        power = BASE_POWER * random.uniform(0.2, 1.2)
        if random.random() < 0.08:   # frequent spikes for plug
            power *= random.uniform(1.5, 2.2)

    noise = random.uniform(-0.03, 0.03) * power
    return max(0.0, round(power + noise, 2))


def build_telemetry(power_watt: float) -> dict:
    current_ampere = round(power_watt / VOLTAGE, 3) if VOLTAGE > 0 else 0.0
    energy_kwh_interval = power_watt * (PUBLISH_INTERVAL / 3600)
    state["energy_wh"] += energy_kwh_interval

    return {
        "device_id": DEVICE_ID,
        "load_id":   LOAD_ID,
        "voltage":   VOLTAGE,
        "current_ampere": current_ampere,
        "power_watt": power_watt if state["switch"] == "on" else 0.0,
        "energy_wh":  round(state["energy_wh"], 3),
        "priority":   PRIORITY,
        "switch":     state["switch"],
        "timestamp":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ── MQTT callbacks ────────────────────────────────────────────────────────────
def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        logger.info("Connected to MQTT broker %s:%d", MQTT_BROKER, MQTT_PORT)
        client.subscribe(TOPIC_LOAD_CMD)
        logger.info("Subscribed to %s", TOPIC_LOAD_CMD)
    else:
        logger.error("Failed to connect, rc=%d", rc)


def on_message(client, userdata, msg):
    """Handle load switch commands from gateway."""
    try:
        payload = json.loads(msg.payload.decode())
        logger.info("Received command: %s", payload)
        action = payload.get("action", "").lower()
        if action in ("on", "off"):
            state["switch"] = action
            logger.info("[%s] Switch -> %s (reason: %s)",
                        LOAD_ID, action, payload.get("reason", "unknown"))
            # Publish updated status immediately
            status = {
                "device_id": DEVICE_ID,
                "load_id":   LOAD_ID,
                "switch":    state["switch"],
                "last_command_reason": payload.get("reason", "unknown"),
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            userdata["client"].publish(TOPIC_LOAD_STATUS, json.dumps(status))
        else:
            logger.warning("Unknown action: %s", action)
    except (json.JSONDecodeError, KeyError) as exc:
        logger.error("Invalid command payload: %s — %s", msg.payload, exc)


def connect_with_retry(client: mqtt.Client, max_retries: int = 20) -> None:
    for attempt in range(1, max_retries + 1):
        try:
            client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            return
        except Exception as exc:
            logger.warning("Connection attempt %d/%d failed: %s",
                           attempt, max_retries, exc)
            time.sleep(3)
    raise RuntimeError("Could not connect to MQTT broker after retries")


def main():
    logger.info("Starting smart-meter | load=%s | device=%s | base_power=%.1fW | priority=%s",
                LOAD_ID, DEVICE_ID, BASE_POWER, PRIORITY)

    client = mqtt.Client(
        client_id=f"meter-{LOAD_ID}-{int(time.time())}",
        userdata={"client": None},
    )
    client.user_data_set({"client": client})

    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    client.on_connect = on_connect
    client.on_message = on_message

    connect_with_retry(client)
    client.loop_start()

    try:
        while True:
            power = simulate_power(state["step"])
            payload = build_telemetry(power)
            client.publish(TOPIC_TELEMETRY, json.dumps(payload))
            logger.info("Published telemetry: load=%s power=%.1fW switch=%s",
                        LOAD_ID, payload["power_watt"], state["switch"])
            state["step"] += 1
            time.sleep(PUBLISH_INTERVAL)
    except KeyboardInterrupt:
        logger.info("Meter shutting down")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
