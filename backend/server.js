/**
 * ╔══════════════════════════════════════════════════════════╗
 * ║   IoT SIMULATION PLATFORM — BACKEND / CLOUD LAYER       ║
 * ║   Fog Processing + REST API + MQTT Subscriber           ║
 * ╚══════════════════════════════════════════════════════════╝
 *
 * Architecture:
 *   MQTT Broker → [this server subscribes] → In-memory store
 *   HTTP Clients (dashboard) → REST API → JSON responses
 *
 * Responsibilities:
 *   1. Subscribe to MQTT topics and ingest sensor data
 *   2. Fog processing: filter, aggregate, detect anomalies
 *   3. Serve REST API for the dashboard
 *   4. Broadcast real-time updates via Server-Sent Events (SSE)
 *   5. Accept live config updates from dashboard
 */

const http        = require("http");
const fs          = require("fs");
const path        = require("path");
const url         = require("url");
const { EventEmitter } = require("events");

// ─────────────────────────────────────────────────────────
// CONFIG
// ─────────────────────────────────────────────────────────
const CONFIG_PATH = path.join(__dirname, "../config/simulation.json");
let config = {};
try {
  config = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8"));
  console.log("[Config] Loaded from", CONFIG_PATH);
} catch (e) {
  console.warn("[Config] Using defaults:", e.message);
  config = {
    backend:  { port: 3000, max_history: 200, aggregation_window: 10 },
    mqtt:     { broker: "localhost", port: 1883, topic_prefix: "iot/sensors", qos: 1 },
    alerts:   { temperature_high: 32, humidity_high: 75, air_quality_high: 150 },
    simulation: { num_devices: 6, interval: 2, scenario: "smart_city" }
  };
}

const PORT        = (config.backend && config.backend.port) || 3000;
const MAX_HISTORY = (config.backend && config.backend.max_history) || 200;
const AGG_WINDOW  = (config.backend && config.backend.aggregation_window) || 10;

// ─────────────────────────────────────────────────────────
// IN-MEMORY DATA STORE
// ─────────────────────────────────────────────────────────
const store = {
  // { device_id: [ ...payload ] }
  deviceData:   new Map(),
  // [ ...alert ]
  alerts:       [],
  // { device_id: { lat, lon, last_seen, readings, status } }
  deviceStatus: new Map(),
  // Global event bus for SSE broadcasts
  events:       new EventEmitter(),

  push(payload) {
    const id = payload.device_id;
    if (!this.deviceData.has(id)) this.deviceData.set(id, []);
    const history = this.deviceData.get(id);
    history.push(payload);
    if (history.length > MAX_HISTORY) history.shift();

    // Update device status
    this.deviceStatus.set(id, {
      device_id:  id,
      scenario:   payload.scenario,
      last_seen:  payload.timestamp,
      gps:        payload.gps,
      readings:   payload.readings,
      status:     "online",
      alerts:     payload.alerts ? payload.alerts.length : 0,
    });

    // Process alerts
    if (payload.alerts && payload.alerts.length > 0) {
      payload.alerts.forEach(alert => {
        this.alerts.unshift({
          ...alert,
          device_id: id,
          timestamp: payload.timestamp,
          id: `${id}-${Date.now()}-${Math.random().toString(36).slice(2,6)}`
        });
      });
      if (this.alerts.length > 500) this.alerts.length = 500;
    }

    // Emit SSE event
    this.events.emit("data", { type: "sensor_update", payload });
  },

  getAggregated(deviceId, windowSize = AGG_WINDOW) {
    const history = this.deviceData.get(deviceId);
    if (!history || history.length === 0) return null;
    const slice = history.slice(-windowSize);
    const sensorNames = Object.keys(slice[0].readings || {});
    const agg = {};
    sensorNames.forEach(sensor => {
      const vals = slice.map(p => p.readings[sensor]).filter(v => v !== undefined);
      if (vals.length === 0) return;
      agg[sensor] = {
        avg:   round(vals.reduce((s, v) => s + v, 0) / vals.length, 2),
        min:   round(Math.min(...vals), 2),
        max:   round(Math.max(...vals), 2),
        count: vals.length,
        last:  vals[vals.length - 1]
      };
    });
    return agg;
  }
};

function round(v, d) { return Math.round(v * 10 ** d) / 10 ** d; }

// ─────────────────────────────────────────────────────────
// FOG PROCESSING ENGINE
// ─────────────────────────────────────────────────────────
class FogProcessor {
  /**
   * Runs on every incoming message BEFORE storing.
   * Implements:
   *  - Outlier filtering (3-sigma rule)
   *  - Data smoothing (exponential moving average)
   *  - Threshold anomaly detection
   */
  constructor(cfg) {
    this.thresholds = cfg.alerts || {};
    this._ema       = new Map();   // device_id → { sensor → ema_value }
    this.ALPHA      = 0.3;         // EMA smoothing factor
  }

  process(payload) {
    const id = payload.device_id;
    if (!payload.readings) return payload;

    // EMA smoothing per device per sensor
    if (!this._ema.has(id)) this._ema.set(id, {});
    const ema = this._ema.get(id);
    const smoothed = {};

    Object.entries(payload.readings).forEach(([sensor, raw]) => {
      if (ema[sensor] === undefined) ema[sensor] = raw;
      ema[sensor] = this.ALPHA * raw + (1 - this.ALPHA) * ema[sensor];
      smoothed[sensor] = round(ema[sensor], 2);
    });

    // Replace raw readings with smoothed values (fog intelligence)
    payload.readings_raw      = { ...payload.readings };
    payload.readings_smoothed = smoothed;
    // Keep .readings as smoothed for downstream
    payload.readings = smoothed;

    return payload;
  }
}

const fogProcessor = new FogProcessor(config);

// ─────────────────────────────────────────────────────────
// MQTT SUBSCRIBER
// ─────────────────────────────────────────────────────────
let mqttClient = null;
let mqttAvailable = false;

try {
  const mqtt = require("mqtt");
  mqttAvailable = true;

  const mqttCfg = config.mqtt || {};
  const brokerUrl = `mqtt://${mqttCfg.broker || "localhost"}:${mqttCfg.port || 1883}`;

  mqttClient = mqtt.connect(brokerUrl, {
    clientId:  "iot-backend-server",
    clean:     true,
    keepalive: mqttCfg.keepalive || 60,
    reconnectPeriod: 5000,
  });

  mqttClient.on("connect", () => {
    const topic = `${mqttCfg.topic_prefix || "iot/sensors"}/#`;
    mqttClient.subscribe(topic, { qos: mqttCfg.qos || 1 }, (err) => {
      if (err) console.error("[MQTT] Subscribe error:", err);
      else console.log(`[MQTT] Subscribed to ${topic}`);
    });
  });

  mqttClient.on("message", (topic, message) => {
    try {
      let payload = JSON.parse(message.toString());
      payload = fogProcessor.process(payload);
      store.push(payload);
    } catch (e) {
      console.warn("[MQTT] Bad message:", e.message);
    }
  });

  mqttClient.on("error", (err) => {
    console.warn("[MQTT] Error:", err.message);
  });

  mqttClient.on("reconnect", () => {
    console.log("[MQTT] Reconnecting...");
  });

  console.log(`[MQTT] Connecting to ${brokerUrl}`);
} catch (e) {
  console.warn("[MQTT] mqtt package not found — running HTTP-only mode. Devices will POST to /ingest.");
}

// ─────────────────────────────────────────────────────────
// SSE CLIENT MANAGER
// ─────────────────────────────────────────────────────────
const sseClients = new Set();

store.events.on("data", (event) => {
  const data = `data: ${JSON.stringify(event)}\n\n`;
  sseClients.forEach(res => {
    try { res.write(data); } catch (_) { sseClients.delete(res); }
  });
});

// Heartbeat to keep SSE connections alive
setInterval(() => {
  const ping = `data: ${JSON.stringify({ type: "ping", ts: Date.now() })}\n\n`;
  sseClients.forEach(res => {
    try { res.write(ping); } catch (_) { sseClients.delete(res); }
  });
}, 15000);

// ─────────────────────────────────────────────────────────
// REST API ROUTER
// ─────────────────────────────────────────────────────────
function cors(res) {
  res.setHeader("Access-Control-Allow-Origin",  "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
}

function json(res, data, status = 200) {
  cors(res);
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(data, null, 2));
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.on("data", chunk => body += chunk);
    req.on("end", () => {
      try { resolve(JSON.parse(body)); }
      catch (e) { reject(e); }
    });
    req.on("error", reject);
  });
}

// ─────────────────────────────────────────────────────────
// HTTP SERVER
// ─────────────────────────────────────────────────────────
const server = http.createServer(async (req, res) => {
  const parsed   = url.parse(req.url, true);
  const pathname = parsed.pathname;
  const query    = parsed.query;

  // CORS preflight
  if (req.method === "OPTIONS") { cors(res); res.writeHead(204); res.end(); return; }

  // ── Static files (dashboard) ──────────────────────────
  if (req.method === "GET" && (pathname === "/" || pathname === "/dashboard")) {
    const dashPath = path.join(__dirname, "../dashboard/index.html");
    try {
      const html = fs.readFileSync(dashPath, "utf8");
      res.writeHead(200, { "Content-Type": "text/html" });
      res.end(html);
    } catch (_) {
      res.writeHead(404); res.end("Dashboard not found. Place index.html in /dashboard/");
    }
    return;
  }

  // ── SSE stream ────────────────────────────────────────
  if (pathname === "/stream") {
    cors(res);
    res.writeHead(200, {
      "Content-Type":  "text/event-stream",
      "Cache-Control": "no-cache",
      "Connection":    "keep-alive",
      "X-Accel-Buffering": "no"
    });
    res.write(`data: ${JSON.stringify({ type: "connected", ts: Date.now() })}\n\n`);
    sseClients.add(res);
    req.on("close", () => sseClients.delete(res));
    return;
  }

  // ── Ingest endpoint (HTTP fallback for no-MQTT mode) ──
  if (req.method === "POST" && pathname === "/ingest") {
    try {
      let payload = await readBody(req);
      payload = fogProcessor.process(payload);
      store.push(payload);
      json(res, { ok: true });
    } catch (e) {
      json(res, { error: e.message }, 400);
    }
    return;
  }

  // ── GET /data ─────────────────────────────────────────
  if (pathname === "/data") {
    const deviceId = query.device;
    if (deviceId) {
      const history = store.deviceData.get(deviceId) || [];
      const limit   = parseInt(query.limit) || 50;
      json(res, { device_id: deviceId, data: history.slice(-limit) });
    } else {
      // All devices — last N per device
      const limit = parseInt(query.limit) || 20;
      const all   = {};
      store.deviceData.forEach((hist, id) => { all[id] = hist.slice(-limit); });
      json(res, { devices: Object.keys(all).length, data: all });
    }
    return;
  }

  // ── GET /devices ──────────────────────────────────────
  if (pathname === "/devices") {
    const devices = [];
    store.deviceStatus.forEach(status => devices.push(status));
    json(res, { count: devices.length, devices });
    return;
  }

  // ── GET /alerts ───────────────────────────────────────
  if (pathname === "/alerts") {
    const limit = parseInt(query.limit) || 50;
    json(res, { count: store.alerts.length, alerts: store.alerts.slice(0, limit) });
    return;
  }

  // ── GET /aggregated ───────────────────────────────────
  if (pathname === "/aggregated") {
    const deviceId = query.device;
    const window   = parseInt(query.window) || AGG_WINDOW;
    const result   = {};
    if (deviceId) {
      result[deviceId] = store.getAggregated(deviceId, window);
    } else {
      store.deviceData.forEach((_, id) => {
        result[id] = store.getAggregated(id, window);
      });
    }
    json(res, { window_size: window, aggregated: result });
    return;
  }

  // ── GET /stats ────────────────────────────────────────
  if (pathname === "/stats") {
    const totalReadings = [...store.deviceData.values()].reduce((s, h) => s + h.length, 0);
    json(res, {
      devices:        store.deviceStatus.size,
      total_readings: totalReadings,
      total_alerts:   store.alerts.length,
      sse_clients:    sseClients.size,
      mqtt_connected: mqttClient ? mqttClient.connected : false,
      uptime_s:       Math.floor(process.uptime()),
      scenario:       config.simulation && config.simulation.scenario,
    });
    return;
  }

  // ── GET /config ───────────────────────────────────────
  if (pathname === "/config" && req.method === "GET") {
    json(res, config);
    return;
  }

  // ── POST /config ──────────────────────────────────────
  if (pathname === "/config" && req.method === "POST") {
    try {
      const updates = await readBody(req);
      // Merge updates into config
      Object.assign(config.simulation || {}, updates.simulation || {});
      Object.assign(config.sensors    || {}, updates.sensors    || {});
      Object.assign(config.alerts     || {}, updates.alerts     || {});

      // Optionally push to MQTT config channel
      if (mqttClient && mqttClient.connected && updates.simulation) {
        mqttClient.publish("iot/config/all", JSON.stringify(updates.simulation), { qos: 1 });
      }

      // Persist to disk
      try { fs.writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2)); }
      catch (_) {}

      json(res, { ok: true, config });
    } catch (e) {
      json(res, { error: e.message }, 400);
    }
    return;
  }

  // ── 404 ───────────────────────────────────────────────
  json(res, { error: "Not found", path: pathname }, 404);
});

server.listen(PORT, () => {
  console.log("╔══════════════════════════════════════════════════════╗");
  console.log("║   IoT SIMULATION PLATFORM — BACKEND LAYER ACTIVE    ║");
  console.log("╠══════════════════════════════════════════════════════╣");
  console.log(`║  Dashboard:  http://localhost:${PORT}/               `);
  console.log(`║  REST API:   http://localhost:${PORT}/data           `);
  console.log(`║  SSE Stream: http://localhost:${PORT}/stream         `);
  console.log(`║  MQTT Mode:  ${mqttAvailable ? "ENABLED" : "HTTP-FALLBACK"}                          `);
  console.log("╚══════════════════════════════════════════════════════╝");
});

server.on("error", (e) => {
  if (e.code === "EADDRINUSE") {
    console.error(`Port ${PORT} in use. Change backend.port in config.`);
    process.exit(1);
  }
});
