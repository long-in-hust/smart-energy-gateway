"""
Energy Gateway
- Subscribes to all meter telemetry, solar, and actuator status
- Validates and normalises messages
- Runs rule engine every SUMMARY_INTERVAL seconds
- Writes to InfluxDB
- Publishes commands and summary to MQTT
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from rule_engine import RuleEngine
from state_store import StateStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("energy-gateway")

# ── Config ────────────────────────────────────────────────────────────────────
MQTT_BROKER          = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT            = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USERNAME        = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD        = os.environ.get("MQTT_PASSWORD", "")
INFLUXDB_URL         = os.environ.get("INFLUXDB_URL", "http://localhost:8086")
INFLUXDB_TOKEN       = os.environ.get("INFLUXDB_TOKEN", "")
INFLUXDB_ORG         = os.environ.get("INFLUXDB_ORG", "smart-energy")
INFLUXDB_BUCKET      = os.environ.get("INFLUXDB_BUCKET", "energy_data")
OVERLOAD_THRESHOLD   = float(os.environ.get("OVERLOAD_THRESHOLD_WATT", 3500.0))
SUMMARY_INTERVAL     = int(os.environ.get("SUMMARY_INTERVAL", 5))
METER_OFFLINE_TIMEOUT = int(os.environ.get("METER_OFFLINE_TIMEOUT", 30))

KNOWN_LOADS = ["hvac", "lighting", "plug"]

# ── Topic patterns ────────────────────────────────────────────────────────────
TOPIC_METER_TELE  = "energy/+/meter/telemetry"
TOPIC_SOLAR_TELE  = "energy/solar/telemetry"
TOPIC_LOAD_STATUS = "energy/+/load/status"
TOPIC_SUMMARY     = "energy/gateway/summary"
TOPIC_EVENT       = "energy/gateway/event"


# ── InfluxDB helpers ──────────────────────────────────────────────────────────
def get_influx_write_api():
    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    return client, client.write_api(write_options=SYNCHRONOUS)


def write_meter_telemetry(write_api, data: dict) -> None:
    try:
        p = (
            Point("meter_telemetry")
            .tag("load_id",  data["load_id"])
            .tag("device_id", data["device_id"])
            .tag("priority", data.get("priority", "unknown"))
            .field("power_watt",     float(data.get("power_watt", 0)))
            .field("current_ampere", float(data.get("current_ampere", 0)))
            .field("voltage",        float(data.get("voltage", 220)))
            .field("energy_wh",      float(data.get("energy_wh", 0)))
            .field("switch",         1 if data.get("switch") == "on" else 0)
        )
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=p)
    except Exception as exc:
        logger.warning("InfluxDB write meter_telemetry failed: %s", exc)


def write_solar_telemetry(write_api, data: dict) -> None:
    try:
        p = (
            Point("solar_telemetry")
            .tag("device_id", data.get("device_id", "solar"))
            .field("power_watt",     float(data.get("power_watt", 0)))
            .field("irradiance_wm2", float(data.get("irradiance_wm2", 0)))
        )
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=p)
    except Exception as exc:
        logger.warning("InfluxDB write solar failed: %s", exc)


def write_summary(write_api, summary: dict) -> None:
    try:
        p = (
            Point("energy_summary")
            .field("total_power_watt", float(summary.get("total_power_watt", 0)))
            .field("solar_power_watt", float(summary.get("solar_power_watt", 0)))
            .field("grid_power_watt",  float(summary.get("grid_power_watt", 0)))
            .field("overload",         1 if summary.get("overload") else 0)
        )
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=p)
    except Exception as exc:
        logger.warning("InfluxDB write summary failed: %s", exc)


def write_event(write_api, event: dict) -> None:
    try:
        p = (
            Point("gateway_events")
            .tag("event_type",  event.get("event_type", "unknown"))
            .tag("severity",    event.get("severity", "info"))
            .tag("load_id",     event.get("load_id", "system"))
            .field("value",         float(event.get("value", 0)))
            .field("threshold",     float(event.get("threshold", 0)))
            .field("message",       event.get("message", ""))
            .field("action_taken",  event.get("action_taken", ""))
        )
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=p)
    except Exception as exc:
        logger.warning("InfluxDB write event failed: %s", exc)


def write_load_status(write_api, data: dict) -> None:
    try:
        p = (
            Point("load_status")
            .tag("load_id",  data.get("load_id", "unknown"))
            .tag("device_id", data.get("device_id", "unknown"))
            .field("switch", 1 if data.get("switch") == "on" else 0)
        )
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=p)
    except Exception as exc:
        logger.warning("InfluxDB write load_status failed: %s", exc)


# ── Message validation ────────────────────────────────────────────────────────
def validate_meter_telemetry(data: dict) -> bool:
    required = {"device_id", "load_id", "power_watt", "timestamp"}
    if not required.issubset(data.keys()):
        logger.warning("Missing fields in meter telemetry: %s", required - data.keys())
        return False
    if not isinstance(data.get("power_watt"), (int, float)):
        logger.warning("power_watt is not numeric")
        return False
    return True


def normalise_meter(raw: dict) -> dict:
    """Ensure all numeric fields are floats and set defaults."""
    return {
        "device_id":     str(raw.get("device_id", "")),
        "load_id":       str(raw.get("load_id", "")),
        "voltage":       round(float(raw.get("voltage", 220.00)), 2),
        "current_ampere": round(float(raw.get("current_ampere", 0.00)), 2),
        "power_watt":    round(float(raw.get("power_watt", 0.00)), 2),
        "energy_wh":     round(float(raw.get("energy_wh", 0.00)), 2),
        "priority":      str(raw.get("priority", "low")),
        "switch":        str(raw.get("switch", "on")),
        "timestamp":     str(raw.get("timestamp", "")),
    }


# ── Gateway ───────────────────────────────────────────────────────────────────
class EnergyGateway:
    def __init__(self):
        self.store  = StateStore()
        self.engine = RuleEngine(overload_threshold=OVERLOAD_THRESHOLD)
        self._influx_client, self._write_api = None, None
        self._mqtt: mqtt.Client = None
        self._init_influx()

    def _init_influx(self):
        retries = 15
        for i in range(retries):
            try:
                self._influx_client, self._write_api = get_influx_write_api()
                self._influx_client.health()
                logger.info("InfluxDB connected at %s", INFLUXDB_URL)
                return
            except Exception as exc:
                logger.warning("InfluxDB attempt %d/%d: %s", i + 1, retries, exc)
                time.sleep(4)
        logger.error("InfluxDB unavailable — proceeding without persistence")

    # ── MQTT callbacks ────────────────────────────────────────────────────────
    def on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            logger.info("MQTT connected: %s:%d", MQTT_BROKER, MQTT_PORT)
            client.subscribe(TOPIC_METER_TELE)
            client.subscribe(TOPIC_SOLAR_TELE)
            client.subscribe(TOPIC_LOAD_STATUS)
            logger.info("Subscribed to telemetry and status topics")
        else:
            logger.error("MQTT connect failed rc=%d", rc)

    def on_disconnect(self, client, userdata, disconnect_flags, rc, properties=None):
        logger.warning("MQTT disconnected rc=%d — reconnecting…", rc)

    def on_message(self, client, userdata, msg):
        topic = msg.topic
        try:
            data = json.loads(msg.payload.decode())
        except json.JSONDecodeError as exc:
            logger.warning("JSON parse error on topic %s: %s", topic, exc)
            return

        if topic == TOPIC_SOLAR_TELE:
            self._handle_solar(data)
        elif topic.endswith("/meter/telemetry"):
            self._handle_meter(data)
        elif topic.endswith("/load/status"):
            self._handle_load_status(data)

    # ── Message handlers ──────────────────────────────────────────────────────
    def _handle_meter(self, raw: dict) -> None:
        if not validate_meter_telemetry(raw):
            return
        norm = normalise_meter(raw)
        load_id = norm["load_id"]
        self.store.update_meter(load_id, norm)
        if self._write_api:
            write_meter_telemetry(self._write_api, norm)
        logger.debug("Meter update: %s %.1fW", load_id, norm["power_watt"])

    def _handle_solar(self, data: dict) -> None:
        self.store.update_solar(data)
        if self._write_api:
            write_solar_telemetry(self._write_api, data)

    def _handle_load_status(self, data: dict) -> None:
        load_id = data.get("load_id", "")
        switch  = data.get("switch", "on")
        reason  = data.get("last_command_reason", "")
        self.store.update_load_switch(load_id, switch, reason)
        if self._write_api:
            write_load_status(self._write_api, data)
            # Nếu load bị tắt → ghi power=0 vào meter_telemetry
            if switch == "off":
                from influxdb_client import Point
                p = (
                    Point("meter_telemetry")
                    .tag("load_id", load_id)
                    .tag("device_id", f"meter-{load_id}")
                    .tag("priority", self.store.get_meter_states().get(load_id, {}).get("priority", "low"))
                    .field("power_watt", 0.0)
                    .field("switch", 0)
                )
                try:
                    self._write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=p)
                except Exception as exc:
                    logger.warning("Failed to write zero power: %s", exc)
        logger.info("Load status: %s switch=%s reason=%s", load_id, switch, reason)

    # ── Rule evaluation loop ──────────────────────────────────────────────────
    def _run_rule_loop(self) -> None:
        logger.info("Rule evaluation loop started (interval=%ds)", SUMMARY_INTERVAL)
        while True:
            time.sleep(SUMMARY_INTERVAL)
            try:
                self._evaluate()
                self._check_offline()
            except Exception as exc:
                logger.error("Rule loop error: %s", exc)

    def _evaluate(self) -> None:
        meter_states = self.store.get_meter_states()
        solar_power  = self.store.get_solar_power()

        if not meter_states:
            return

        result, total_power, grid_power = self.engine.evaluate(meter_states, solar_power)

        # Send commands
        for action in result.actions:
            self._send_command(action.load_id, action.action, action.reason)

        # Process events
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for event in result.events:
            ev_dict = {
                "event_type":   event.event_type,
                "severity":     event.severity,
                "value":        event.value,
                "threshold":    event.threshold,
                "message":      event.message,
                "action_taken": event.action_taken or "",
                "load_id":      event.load_id or "system",
                "timestamp":    now,
            }
            self.store.add_event(ev_dict)
            self._mqtt.publish(TOPIC_EVENT, json.dumps(ev_dict))
            logger.info("Event: %s severity=%s value=%.1f",
                        event.event_type, event.severity, event.value)
            if self._write_api:
                write_event(self._write_api, ev_dict)

        # Publish summary
        summary = {
            "total_power_watt": round(total_power, 2),
            "solar_power_watt": round(solar_power, 2),
            "grid_power_watt":  round(grid_power, 2),
            "overload":         total_power > OVERLOAD_THRESHOLD,
            "timestamp":        now,
        }
        self.store.update_summary(summary)
        self._mqtt.publish(TOPIC_SUMMARY, json.dumps(summary))
        logger.info("Summary: total=%.1fW solar=%.1fW grid=%.1fW overload=%s",
                    total_power, solar_power, grid_power, summary["overload"])
        if self._write_api:
            write_summary(self._write_api, summary)

    def _check_offline(self) -> None:
        now = time.time()
        for load_id in KNOWN_LOADS:
            last = self.store.get_last_seen(load_id)
            if last is None:
                continue
            age = now - last
            if age > METER_OFFLINE_TIMEOUT:
                ev_dict = {
                    "event_type":   "meter_offline",
                    "severity":     "critical",
                    "value":        round(age, 1),
                    "threshold":    float(METER_OFFLINE_TIMEOUT),
                    "message":      f"Meter {load_id} offline for {age:.0f}s",
                    "action_taken": "",
                    "load_id":      load_id,
                    "timestamp":    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                # Rate-limit: chỉ sinh event mỗi 60s
                existing = self.store.get_events(load_id=load_id, limit=5)
                recent_offline = [
                    e for e in existing
                    if e["event_type"] == "meter_offline"
                    and (now - 60) < datetime.fromisoformat(
                        e["timestamp"].replace("Z", "+00:00")
                    ).timestamp()
                ]
                    
                if not recent_offline:
                    self.store.add_event(ev_dict)
                    self._mqtt.publish(TOPIC_EVENT, json.dumps(ev_dict))
                    logger.warning("OFFLINE: meter %s (%.0fs ago)", load_id, age)
                    if self._write_api:
                        write_event(self._write_api, ev_dict)

    def _send_command(self, load_id: str, action: str, reason: str) -> None:
        cmd = {
            "load_id":   load_id,
            "target":    "load_switch",
            "action":    action,
            "reason":    reason,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        topic = f"energy/{load_id}/load/command"
        self._mqtt.publish(topic, json.dumps(cmd))
        logger.info("Command sent: %s -> %s (reason: %s)", load_id, action, reason)

    # ── Main entry ────────────────────────────────────────────────────────────
    def run(self) -> None:
        logger.info("Starting Energy Gateway")
        self._mqtt = mqtt.Client(
            client_id=f"energy-gateway-{int(time.time())}",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        if MQTT_USERNAME:
            self._mqtt.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

        self._mqtt.on_connect    = self.on_connect
        self._mqtt.on_disconnect = self.on_disconnect
        self._mqtt.on_message    = self.on_message

        self._mqtt.reconnect_delay_set(min_delay=2, max_delay=30)

        for attempt in range(1, 21):
            try:
                self._mqtt.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
                break
            except Exception as exc:
                logger.warning("MQTT attempt %d/20: %s", attempt, exc)
                time.sleep(3)

        rule_thread = threading.Thread(target=self._run_rule_loop, daemon=True)
        rule_thread.start()

        try:
            self._mqtt.loop_forever()
        except KeyboardInterrupt:
            logger.info("Gateway shutting down")
        finally:
            self._mqtt.disconnect()
            if self._influx_client:
                self._influx_client.close()


if __name__ == "__main__":
    EnergyGateway().run()