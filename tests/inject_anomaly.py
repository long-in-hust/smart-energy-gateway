#!/usr/bin/env python3
"""
Anomaly injection script — dùng để kiểm thử rule engine & dashboard
Publishes overload scenarios directly to MQTT for testing.

Usage:
  python inject_anomaly.py [--scenario overload|solar|offline]
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

MQTT_BROKER  = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT    = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USER    = os.environ.get("MQTT_USERNAME", "energy_user")
MQTT_PASS    = os.environ.get("MQTT_PASSWORD", "energy_pass123")


def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect():
    try:
        client = mqtt.Client(
            client_id="anomaly-injector",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
    except AttributeError:
        client = mqtt.Client(client_id="anomaly-injector")
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.connect(MQTT_BROKER, MQTT_PORT)
    client.loop_start()
    time.sleep(1)
    return client


def scenario_overload(client: mqtt.Client):
    """Publish extremely high power to trigger overload shedding.
    Publishes repeatedly for 15s so rule engine (runs every 5s) evaluates at least twice.
    """
    loads = [
        ("hvac",     "meter-hvac",     2200.0),
        ("lighting", "meter-lighting",  900.0),
        ("plug",     "meter-plug",     1500.0),
    ]
    print("🔴 Injecting OVERLOAD scenario (all loads at 150% power)…")
    print("  Publishing repeatedly for 15s so rule engine can evaluate…")

    for repeat in range(3):  # publish 3 rounds, 5s apart → rule engine catches at least 2
        for load_id, device_id, power in loads:
            payload = {
                "device_id":      device_id,
                "load_id":        load_id,
                "voltage":        220.0,
                "current_ampere": round(power / 220.0, 2),
                "power_watt":     power,
                "energy_wh":      100.0,
                "priority":       "low",
                "switch":         "on",
                "timestamp":      ts(),
            }
            topic = f"energy/{load_id}/meter/telemetry"
            client.publish(topic, json.dumps(payload))
        print(f"  ✓ Round {repeat+1}/3: hvac=2200W lighting=900W plug=1500W (total=4600W)")
        if repeat < 2:
            time.sleep(5)  # wait for rule engine tick before next round

    print("  Total injected: 4600W → should trigger overload_detected + shedding")


def scenario_solar(client: mqtt.Client):
    """Simulate high solar output to trigger load restoration."""
    print("☀️  Injecting HIGH SOLAR scenario (1400W solar)…")
    payload = {
        "device_id":      "solar-simulator",
        "power_watt":     1400.0,
        "irradiance_wm2": 950.0,
        "is_generating":  True,
        "timestamp":      ts(),
    }
    client.publish("energy/solar/telemetry", json.dumps(payload))
    print("  ✓ energy/solar/telemetry: 1400W")
    print("  Gateway should attempt to restore shed loads")


def scenario_zero_power(client: mqtt.Client):
    """Simulate a meter going to 0W (device may have gone offline)."""
    print("⚫ Injecting ZERO POWER for plug meter…")
    payload = {
        "device_id":  "meter-plug",
        "load_id":    "plug",
        "voltage":    0.0,
        "current_ampere": 0.0,
        "power_watt": 0.0,
        "energy_wh":  50.0,
        "priority":   "low",
        "switch":     "on",
        "timestamp":  ts(),
    }
    client.publish("energy/plug/meter/telemetry", json.dumps(payload))
    print("  ✓ Plug meter now reports 0W")


def main():
    parser = argparse.ArgumentParser(description="Energy anomaly injector")
    parser.add_argument(
        "--scenario",
        choices=["overload", "solar", "zero", "all"],
        default="all",
        help="Which scenario to inject",
    )
    args = parser.parse_args()

    print(f"Connecting to MQTT at {MQTT_BROKER}:{MQTT_PORT}…")
    try:
        client = connect()
    except Exception as exc:
        print(f"Connection failed: {exc}")
        sys.exit(1)

    print(f"Connected.\n")

    if args.scenario in ("overload", "all"):
        scenario_overload(client)
        time.sleep(8)  # wait for rule engine to evaluate after last publish

    if args.scenario in ("solar", "all"):
        scenario_solar(client)
        time.sleep(2)

    if args.scenario in ("zero", "all"):
        scenario_zero_power(client)
        time.sleep(2)

    print("\n✅ Injection complete. Check Grafana dashboard and REST API events.")
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()