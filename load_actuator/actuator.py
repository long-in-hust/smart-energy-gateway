"""
Load Actuator Simulator
Subscribes to load commands, updates switch state, publishes status.
Topics:
  Subscribe: energy/{load_id}/load/command
  Publish:   energy/{load_id}/load/status
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("load-actuator")

MQTT_BROKER      = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT        = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USERNAME    = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD    = os.environ.get("MQTT_PASSWORD", "")
LOAD_ID          = os.environ.get("LOAD_ID", "hvac")
DEVICE_ID        = os.environ.get("DEVICE_ID", "load-hvac")

TOPIC_CMD        = f"energy/{LOAD_ID}/load/command"
TOPIC_STATUS     = f"energy/{LOAD_ID}/load/status"

# Internal state
state = {
    "switch": "on",
    "last_command_reason": "startup",
}


def publish_status(client: mqtt.Client, reason: str = None) -> None:
    payload = {
        "device_id":           DEVICE_ID,
        "load_id":             LOAD_ID,
        "switch":              state["switch"],
        "last_command_reason": reason or state["last_command_reason"],
        "timestamp":           datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    client.publish(TOPIC_STATUS, json.dumps(payload))
    logger.info("Status published: load=%s switch=%s reason=%s",
                LOAD_ID, state["switch"], payload["last_command_reason"])


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        logger.info("Connected to MQTT broker %s:%d", MQTT_BROKER, MQTT_PORT)
        client.subscribe(TOPIC_CMD)
        logger.info("Subscribed to command topic: %s", TOPIC_CMD)
        publish_status(client, "startup")
    else:
        logger.error("Connection failed, rc=%d", rc)


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        logger.info("Received command: %s", payload)

        action = payload.get("action", "").lower()
        reason = payload.get("reason", "unknown")

        if action in ("on", "off"):
            old_switch = state["switch"]
            state["switch"] = action
            state["last_command_reason"] = reason
            logger.info("[%s] Switch %s -> %s (reason: %s)",
                        LOAD_ID, old_switch, action, reason)
            publish_status(client, reason)
        else:
            logger.warning("[%s] Unknown action '%s' — ignoring", LOAD_ID, action)

    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON command: %s — %s", msg.payload, exc)
    except Exception as exc:
        logger.error("Error handling command: %s", exc)


def connect_with_retry(client: mqtt.Client, max_retries: int = 20) -> None:
    for attempt in range(1, max_retries + 1):
        try:
            client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            return
        except Exception as exc:
            logger.warning("Attempt %d/%d failed: %s", attempt, max_retries, exc)
            time.sleep(3)
    raise RuntimeError("Could not connect to MQTT broker")


def main():
    logger.info("Starting load-actuator | load=%s | device=%s", LOAD_ID, DEVICE_ID)

    client = mqtt.Client(
        client_id=f"actuator-{LOAD_ID}-{int(time.time())}",
    )
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    client.on_connect = on_connect
    client.on_message = on_message

    connect_with_retry(client)

    try:
        client.loop_forever()
    except KeyboardInterrupt:
        logger.info("Actuator shutting down")
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
