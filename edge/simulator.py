#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║   IoT SIMULATION PLATFORM — EDGE LAYER                  ║
║   Virtual Sensor Simulator with MQTT Publishing         ║
║   Author: IoT Sim Platform                              ║
╚══════════════════════════════════════════════════════════╝

Architecture: Edge Layer → MQTT Broker → Fog/Backend
This module simulates realistic IoT sensor devices that:
  - Generate physically-modelled sensor data (not pure random)
  - Publish via MQTT with configurable QoS
  - Simulate telecom conditions (latency, jitter, packet loss)
  - Support device mobility (GPS movement)
  - Support multiple scenario modes
"""

import json
import math
import time
import random
import threading
import logging
import argparse
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

import urllib.request   # always imported — used for HTTP fallback mode

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    print("[WARN] paho-mqtt not installed. Running in HTTP-fallback mode.")

# ─────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("IoT-Edge")


# ─────────────────────────────────────────────────────────
# CONFIG LOADER
# ─────────────────────────────────────────────────────────
def load_config(path: str = "config/simulation.json") -> dict:
    """Load simulation config; fall back to safe defaults."""
    try:
        with open(path) as f:
            cfg = json.load(f)
        logger.info(f"Config loaded from {path}")
        return cfg
    except FileNotFoundError:
        logger.warning(f"Config not found at {path}, using defaults.")
        return {
            "simulation": {"num_devices": 4, "interval": 2, "scenario": "smart_city",
                           "noise": 0.5, "mobility": True},
            "sensors": {
                "temperature": {"min": 18, "max": 38, "unit": "°C"},
                "humidity":    {"min": 35, "max": 80, "unit": "%"},
                "pressure":    {"min": 980, "max": 1030, "unit": "hPa"},
                "air_quality": {"min": 0,  "max": 500,  "unit": "AQI"},
            },
            "alerts": {"temperature_high": 32, "humidity_high": 75},
            "mqtt": {"broker": "localhost", "port": 1883,
                     "topic_prefix": "iot/sensors", "qos": 1, "keepalive": 60},
            "telecom": {"base_latency_ms": 20, "jitter_ms": 15,
                        "packet_loss_rate": 0.03, "bandwidth_kbps": 250},
            "backend": {"port": 3000, "max_history": 200},
            "gps": {"center_lat": 33.5731, "center_lon": -7.5898, "radius_km": 2.0},
        }


# ─────────────────────────────────────────────────────────
# TELECOM SIMULATION — Wireless Channel Effects
# ─────────────────────────────────────────────────────────
class TelecomChannel:
    """
    Simulates real wireless channel impairments:
    - Latency: base + jitter (Gaussian-distributed)
    - Packet Loss: Bernoulli model (can extend to Gilbert-Elliott)
    - Bandwidth throttling
    These model conditions of LPWAN / NB-IoT / LoRaWAN links.
    """

    def __init__(self, cfg: dict):
        tc = cfg.get("telecom", {})
        self.base_latency  = tc.get("base_latency_ms", 20) / 1000.0
        self.jitter_sigma  = tc.get("jitter_ms", 15) / 1000.0
        self.loss_rate     = tc.get("packet_loss_rate", 0.03)
        self.bandwidth_kbps = tc.get("bandwidth_kbps", 250)
        self._dropped      = 0
        self._sent         = 0

    def transmit(self, payload_bytes: int) -> tuple[bool, float]:
        """
        Simulate packet transmission.
        Returns (delivered: bool, delay_seconds: float)
        """
        self._sent += 1
        # Packet loss model
        if random.random() < self.loss_rate:
            self._dropped += 1
            logger.debug(f"[Telecom] Packet DROPPED (loss rate={self.loss_rate:.0%})")
            return False, 0.0

        # Latency = base + Gaussian jitter
        jitter = random.gauss(0, self.jitter_sigma)
        latency = max(0, self.base_latency + jitter)

        # Bandwidth-induced delay: bits / bandwidth
        tx_delay = (payload_bytes * 8) / (self.bandwidth_kbps * 1000)
        total_delay = latency + tx_delay

        time.sleep(total_delay)
        return True, total_delay * 1000  # return ms

    @property
    def stats(self) -> dict:
        loss_pct = (self._dropped / self._sent * 100) if self._sent else 0
        return {"sent": self._sent, "dropped": self._dropped, "loss_pct": round(loss_pct, 2)}


# ─────────────────────────────────────────────────────────
# PHYSICS-BASED SENSOR MODELS
# ─────────────────────────────────────────────────────────
class SensorModel:
    """
    Base class for all sensor models.
    Uses sinusoidal baseline + noise + drift for realistic data.
    Avoids purely random values — physics-inspired modelling.
    """

    def __init__(self, name: str, cfg: dict, noise_level: float = 0.5):
        self.name       = name
        self.min_val    = cfg.get("min", 0)
        self.max_val    = cfg.get("max", 100)
        self.unit       = cfg.get("unit", "")
        self.noise_level = noise_level
        self._t         = random.uniform(0, 2 * math.pi)   # random phase
        self._period    = random.uniform(30, 120)           # oscillation period (s)
        self._drift     = 0.0
        self._last      = (self.min_val + self.max_val) / 2

    def _midpoint(self): return (self.min_val + self.max_val) / 2
    def _amplitude(self): return (self.max_val - self.min_val) / 2

    def read(self, dt: float = 1.0) -> float:
        """Generate one reading. dt = seconds since last read."""
        self._t += dt

        # Sinusoidal baseline (day/night cycle, periodic process)
        baseline = self._midpoint() + self._amplitude() * 0.6 * math.sin(
            2 * math.pi * self._t / self._period
        )

        # Gaussian noise scaled by noise_level and range
        noise = random.gauss(0, self.noise_level * self._amplitude() * 0.1)

        # Slow random walk (drift)
        self._drift += random.gauss(0, 0.01) * self._amplitude()
        self._drift = max(-self._amplitude() * 0.2, min(self._amplitude() * 0.2, self._drift))

        value = baseline + noise + self._drift
        value = max(self.min_val, min(self.max_val, value))
        self._last = value
        return round(value, 2)


class TemperatureSensor(SensorModel):
    """Temperature with diurnal (24h) variation model."""
    def __init__(self, cfg, noise): super().__init__("temperature", cfg, noise)
    def read(self, dt=1.0):
        self._t += dt
        hour = (self._t / 3600) % 24
        # Peak at 14:00, trough at 05:00
        diurnal = self._amplitude() * 0.7 * math.sin(
            2 * math.pi * (hour - 5) / 24
        )
        noise = random.gauss(0, self.noise_level * 0.5)
        val = self._midpoint() + diurnal + noise
        return round(max(self.min_val, min(self.max_val, val)), 2)


class HumiditySensor(SensorModel):
    """Humidity — inversely correlated with temperature."""
    def __init__(self, cfg, noise): super().__init__("humidity", cfg, noise)


class GPSSensor:
    """
    Simulates device GPS coordinates.
    Supports static mode or random-walk mobility.
    """

    def __init__(self, center_lat: float, center_lon: float,
                 radius_km: float, mobile: bool = True):
        # Random initial position within radius
        angle = random.uniform(0, 2 * math.pi)
        dist  = random.uniform(0, radius_km * 0.8)
        self.lat = center_lat + (dist / 111.0) * math.cos(angle)
        self.lon = center_lon + (dist / (111.0 * math.cos(math.radians(center_lat)))) * math.sin(angle)
        self.mobile   = mobile
        self._heading = random.uniform(0, 360)
        self._speed_kmh = random.uniform(0.5, 5.0)  # pedestrian/vehicle speed

    def read(self, dt: float = 1.0) -> tuple[float, float]:
        if self.mobile:
            # Random heading change (Brownian motion on sphere surface)
            self._heading += random.gauss(0, 10)
            rad = math.radians(self._heading)
            dist_km = self._speed_kmh * dt / 3600
            self.lat += (dist_km / 111.0) * math.cos(rad)
            self.lon += (dist_km / (111.0 * math.cos(math.radians(self.lat)))) * math.sin(rad)
        return round(self.lat, 6), round(self.lon, 6)


# ─────────────────────────────────────────────────────────
# VIRTUAL IoT DEVICE
# ─────────────────────────────────────────────────────────
SCENARIO_PROFILES = {
    "smart_home":     {"sensors": ["temperature", "humidity", "light"],         "label": "🏠 Smart Home"},
    "smart_city":     {"sensors": ["temperature", "humidity", "air_quality", "vibration"], "label": "🌆 Smart City"},
    "industrial_iot": {"sensors": ["temperature", "vibration", "pressure"],    "label": "🏭 Industrial IoT"},
}

SENSOR_CLASS_MAP = {
    "temperature": TemperatureSensor,
    "humidity":    HumiditySensor,
    "pressure":    SensorModel,
    "air_quality": SensorModel,
    "light":       SensorModel,
    "vibration":   SensorModel,
}


@dataclass
class DevicePayload:
    device_id:   str
    scenario:    str
    timestamp:   str
    readings:    dict
    gps:         dict
    telecom:     dict
    alerts:      list = field(default_factory=list)
    metadata:    dict = field(default_factory=dict)


class VirtualDevice:
    """
    Simulates a complete IoT edge device with:
    - Multiple sensor types (scenario-based)
    - GPS mobility
    - MQTT publishing with telecom simulation
    - Alert generation
    """

    def __init__(self, device_id: str, cfg: dict, channel: TelecomChannel,
                 mqtt_client=None, http_port: int = 3000):
        self.device_id  = device_id
        self.cfg        = cfg
        self.channel    = channel
        self.client     = mqtt_client
        self.http_port  = http_port
        self._running   = False
        self._thread: Optional[threading.Thread] = None
        self.logger     = logging.getLogger(f"Device-{device_id}")

        sim = cfg.get("simulation", {})
        self.interval   = sim.get("interval", 2)
        self.scenario   = sim.get("scenario", "smart_city")
        self.mobile     = sim.get("mobility", True)
        noise           = sim.get("noise", 0.5)

        sensor_cfgs     = cfg.get("sensors", {})
        profile         = SCENARIO_PROFILES.get(self.scenario, SCENARIO_PROFILES["smart_city"])
        sensor_names    = profile["sensors"]

        # Instantiate only the sensors needed for this scenario
        self.sensors: dict[str, SensorModel] = {}
        for name in sensor_names:
            scfg = sensor_cfgs.get(name, {"min": 0, "max": 100})
            cls  = SENSOR_CLASS_MAP.get(name, SensorModel)
            if cls == SensorModel:
                self.sensors[name] = SensorModel(name, scfg, noise)
            else:
                self.sensors[name] = cls(scfg, noise)

        gps_cfg = cfg.get("gps", {})
        self.gps = GPSSensor(
            center_lat=gps_cfg.get("center_lat", 33.5731),
            center_lon=gps_cfg.get("center_lon", -7.5898),
            radius_km=gps_cfg.get("radius_km", 2.0),
            mobile=self.mobile
        )

        self.alert_thresholds = cfg.get("alerts", {})
        self.mqtt_cfg = cfg.get("mqtt", {})
        self._seq = 0

    def _check_alerts(self, readings: dict) -> list:
        """Threshold-based alert engine."""
        triggered = []
        for sensor_name, value in readings.items():
            key = f"{sensor_name}_high"
            if key in self.alert_thresholds and value > self.alert_thresholds[key]:
                triggered.append({
                    "type": "threshold_exceeded",
                    "sensor": sensor_name,
                    "value": value,
                    "threshold": self.alert_thresholds[key],
                    "severity": "HIGH" if value > self.alert_thresholds[key] * 1.2 else "MEDIUM"
                })
        return triggered

    def _build_payload(self) -> DevicePayload:
        """Build a full sensor payload."""
        self._seq += 1
        readings = {name: s.read(self.interval) for name, s in self.sensors.items()}
        lat, lon = self.gps.read(self.interval)
        alerts   = self._check_alerts(readings)

        return DevicePayload(
            device_id  = self.device_id,
            scenario   = self.scenario,
            timestamp  = datetime.now(timezone.utc).isoformat(),
            readings   = readings,
            gps        = {"lat": lat, "lon": lon},
            telecom    = self.channel.stats,
            alerts     = alerts,
            metadata   = {
                "seq":          self._seq,
                "interval":     self.interval,
                "scenario":     self.scenario,
                "sensor_types": list(self.sensors.keys()),
            }
        )

    def _publish_mqtt(self, payload: DevicePayload) -> bool:
        """Publish via MQTT with telecom channel simulation."""
        topic   = f"{self.mqtt_cfg.get('topic_prefix', 'iot/sensors')}/{self.device_id}"
        qos     = self.mqtt_cfg.get("qos", 1)
        message = json.dumps(asdict(payload))
        payload_bytes = len(message.encode())

        delivered, delay_ms = self.channel.transmit(payload_bytes)
        if not delivered:
            return False

        if self.client and MQTT_AVAILABLE:
            result = self.client.publish(topic, message, qos=qos)
            ok = result.rc == mqtt.MQTT_ERR_SUCCESS
            self.logger.info(
                f"[MQTT] {topic} | {len(readings_str := str(payload.readings)[:60])}… "
                f"| delay={delay_ms:.1f}ms | QoS={qos} | {'✓' if ok else '✗'}"
            )
            return ok
        else:
            # HTTP fallback to backend REST endpoint
            return self._publish_http(payload, delay_ms)

    def _publish_http(self, payload: DevicePayload, delay_ms: float) -> bool:
        """Fallback: POST data directly to backend REST API."""
        try:
            data = json.dumps(asdict(payload)).encode()
            req  = urllib.request.Request(
                f"http://localhost:{self.http_port}/ingest",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                ok = resp.status == 200
            self.logger.info(
                f"[HTTP] POST /ingest | device={self.device_id} "
                f"| delay={delay_ms:.1f}ms | {'✓' if ok else '✗'}"
            )
            return ok
        except Exception as e:
            self.logger.warning(f"[HTTP] Publish failed: {e}")
            return False

    def _loop(self):
        """Main simulation loop — runs in a dedicated thread."""
        self.logger.info(f"Device started | scenario={self.scenario} | sensors={list(self.sensors)}")
        while self._running:
            payload = self._build_payload()
            self._publish_mqtt(payload)
            time.sleep(self.interval)
        self.logger.info("Device stopped.")

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True, name=self.device_id)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def update_config(self, updates: dict):
        """Hot-reload configuration without restarting (live parameter control)."""
        if "interval" in updates:
            self.interval = updates["interval"]
        if "noise" in updates:
            for s in self.sensors.values():
                s.noise_level = updates["noise"]
        if "sensors" in updates:
            for name, vals in updates["sensors"].items():
                if name in self.sensors:
                    s = self.sensors[name]
                    s.min_val = vals.get("min", s.min_val)
                    s.max_val = vals.get("max", s.max_val)
        self.logger.info(f"Config hot-reloaded: {updates}")


# ─────────────────────────────────────────────────────────
# SIMULATION ORCHESTRATOR
# ─────────────────────────────────────────────────────────
class SimulationOrchestrator:
    """
    Top-level manager for the entire edge simulation.
    Starts/stops all virtual devices, manages MQTT connection.
    """

    def __init__(self, config_path: str = "config/simulation.json"):
        self.cfg     = load_config(config_path)
        self.devices: list[VirtualDevice] = []
        self.channel = TelecomChannel(self.cfg)
        self.client  = None
        self._setup_mqtt()

    def _setup_mqtt(self):
        if not MQTT_AVAILABLE:
            logger.warning("MQTT unavailable — using HTTP fallback mode")
            return

        mqtt_cfg = self.cfg.get("mqtt", {})
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="iot-sim-orchestrator", clean_session=True)

        def on_connect(client, userdata, flags, rc):
            codes = {0: "Connected", 1: "Bad protocol", 2: "Client ID rejected",
                     3: "Server unavailable", 4: "Bad credentials", 5: "Not authorized"}
            if rc == 0:
                logger.info(f"[MQTT] Broker connected: {mqtt_cfg.get('broker')}:{mqtt_cfg.get('port')}")
                # Subscribe to config update channel
                client.subscribe("iot/config/+", qos=1)
            else:
                logger.error(f"[MQTT] Connection failed: {codes.get(rc, 'Unknown')}")

        def on_message(client, userdata, msg):
            """Handle live config updates from backend."""
            try:
                updates = json.loads(msg.payload)
                device_id = msg.topic.split("/")[-1]
                for dev in self.devices:
                    if dev.device_id == device_id or device_id == "all":
                        dev.update_config(updates)
            except Exception as e:
                logger.error(f"Config update error: {e}")

        def on_disconnect(client, userdata, rc):
            if rc != 0:
                logger.warning(f"[MQTT] Unexpected disconnect (rc={rc}), reconnecting...")

        self.client.on_connect    = on_connect
        self.client.on_message    = on_message
        self.client.on_disconnect = on_disconnect

        try:
            self.client.connect(
                mqtt_cfg.get("broker", "localhost"),
                mqtt_cfg.get("port", 1883),
                mqtt_cfg.get("keepalive", 60)
            )
            self.client.loop_start()
        except Exception as e:
            logger.warning(f"[MQTT] Cannot connect to broker: {e}. Switching to HTTP mode.")
            self.client = None

    def spawn_devices(self):
        """Create and start all virtual devices."""
        num = self.cfg["simulation"]["num_devices"]
        scenario = self.cfg["simulation"]["scenario"]
        profile  = SCENARIO_PROFILES.get(scenario, SCENARIO_PROFILES["smart_city"])
        logger.info(f"Spawning {num} devices | {profile['label']}")

        for i in range(num):
            dev_id = f"DEV_{scenario[:3].upper()}_{i+1:03d}"
            dev    = VirtualDevice(
                device_id  = dev_id,
                cfg        = self.cfg,
                channel    = self.channel,
                mqtt_client= self.client,
                http_port  = self.cfg.get("backend", {}).get("port", 3000)
            )
            self.devices.append(dev)
            dev.start()
            time.sleep(0.1)  # stagger startup

    def run(self):
        """Main entry point — blocks until interrupted."""
        self.spawn_devices()
        logger.info(f"{'='*55}")
        logger.info(f"  IoT Simulation Platform — EDGE LAYER ACTIVE")
        logger.info(f"  Devices: {len(self.devices)} | Scenario: {self.cfg['simulation']['scenario']}")
        logger.info(f"  Interval: {self.cfg['simulation']['interval']}s | Mobility: {self.cfg['simulation']['mobility']}")
        logger.info(f"  Telecom: loss={self.cfg['telecom']['packet_loss_rate']:.0%} "
                    f"latency={self.cfg['telecom']['base_latency_ms']}ms")
        logger.info(f"{'='*55}")

        def shutdown(sig, frame):
            logger.info("\nShutting down simulation...")
            for dev in self.devices:
                dev.stop()
            if self.client:
                self.client.loop_stop()
                self.client.disconnect()
            stats = self.channel.stats
            logger.info(f"Telecom stats: {stats}")
            sys.exit(0)

        signal.signal(signal.SIGINT,  shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        while True:
            time.sleep(1)


# ─────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IoT Edge Simulator")
    parser.add_argument("--config", default="config/simulation.json", help="Config file path")
    parser.add_argument("--scenario", choices=["smart_home", "smart_city", "industrial_iot"],
                        help="Override scenario from CLI")
    parser.add_argument("--devices", type=int, help="Override number of devices")
    parser.add_argument("--interval", type=float, help="Override publish interval (seconds)")
    args = parser.parse_args()

    sim = SimulationOrchestrator(args.config)

    # CLI overrides take priority over config file
    if args.scenario: sim.cfg["simulation"]["scenario"] = args.scenario
    if args.devices:  sim.cfg["simulation"]["num_devices"] = args.devices
    if args.interval: sim.cfg["simulation"]["interval"] = args.interval

    sim.run()
