"""
Solar Power Simulator
Simulates PV panel output based on a virtual sun curve.
Publishes to: energy/solar/telemetry
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
logger = logging.getLogger("solar-simulator")

MQTT_BROKER       = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT         = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USERNAME     = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD     = os.environ.get("MQTT_PASSWORD", "")
DEVICE_ID         = os.environ.get("SOLAR_DEVICE_ID", "solar-simulator")
PEAK_POWER        = float(os.environ.get("SOLAR_PEAK_POWER", 1500.0))
PUBLISH_INTERVAL  = int(os.environ.get("SOLAR_PUBLISH_INTERVAL", 5))

TOPIC_TELEMETRY   = "energy/solar/telemetry"

step = 0


def simulate_solar(step: int) -> dict:
    """Simulate solar output with day/night cycle and cloud variations."""
    # 24-hour cycle: sun rises at virtual hour 6, sets at 18
    hour_angle = (step * PUBLISH_INTERVAL / 3600) % 24
    if 6 <= hour_angle <= 18:
        solar_fraction = math.sin(math.pi * (hour_angle - 6) / 12)
    else:
        solar_fraction = 0.0

    # Occasional cloud cover
    cloud_factor = 1.0
    if random.random() < 0.15:
        cloud_factor = random.uniform(0.1, 0.6)

    power = round(PEAK_POWER * solar_fraction * cloud_factor + random.uniform(-10, 10), 2)
    power = max(0.0, power)
    irradiance = round(power / PEAK_POWER * 1000, 1)  # W/m2 proxy

    return {
        "device_id":       DEVICE_ID,
        "power_watt":      power,
        "irradiance_wm2":  irradiance,
        "is_generating":   power > 10.0,
        "timestamp":       datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        logger.info("Connected to MQTT broker %s:%d", MQTT_BROKER, MQTT_PORT)
    else:
        logger.error("Failed to connect, rc=%d", rc)


def connect_with_retry(client: mqtt.Client, max_retries: int = 20) -> None:
    for attempt in range(1, max_retries + 1):
        try:
            client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            return
        except Exception as exc:
            logger.warning("Connection attempt %d/%d failed: %s", attempt, max_retries, exc)
            time.sleep(3)
    raise RuntimeError("Could not connect to MQTT broker")


def main():
    global step
    logger.info("Starting solar-simulator | peak=%.1fW | interval=%ds",
                PEAK_POWER, PUBLISH_INTERVAL)

    client = mqtt.Client(
        client_id=f"solar-sim-{int(time.time())}",
    )
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    client.on_connect = on_connect
    connect_with_retry(client)
    client.loop_start()

    try:
        while True:
            payload = simulate_solar(step)
            client.publish(TOPIC_TELEMETRY, json.dumps(payload))
            logger.info("Solar: %.1fW (irradiance=%.1f W/m²)",
                        payload["power_watt"], payload["irradiance_wm2"])
            step += 1
            time.sleep(PUBLISH_INTERVAL)
    except KeyboardInterrupt:
        logger.info("Solar simulator shutting down")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
