
import argparse
import csv
import io
import json
import os
import threading
from datetime import datetime

import joblib
import pandas as pd
import serial
from flask import Flask, jsonify, request, Response, render_template_string
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline

app = Flask(__name__)

# ---------------- CONFIGURATION ----------------
DATA_FILE = "conveyor_data.csv"
MODEL_FILE = "conveyor_rf.joblib"

FEATURES = [
    "load_raw",
    "load_change",
    "temp_c",
    "transit_s",
    "transit_timeout",
    "fast_transit",
    "overload",
    "temp_valid",
]

LABELS = [
    "NORMAL",
    "OVERLOAD",
    "SLOW_SPEED",
    "OVERSPEED",
    "HIGH_TEMPERATURE",
    "TEMPERATURE_SENSOR_FAULT",
    "IR_SENSOR_FAULT",
    "JAM",
    "OTHER_FAULT",
]

LOCK = threading.Lock()
SERIAL_LOCK = threading.Lock()

ser = None
MODEL = None
STOP_LATCHED = True

LAST_TRANSIT_ID = -1
LAST_TIMEOUT_EVENT = False

STATE = {
    "connected": False,
    "motor": "STOP",
    "ir1_count": 0,
    "ir2_count": 0,
    "overall_count": 0,
    "transit_id": 0,
    "transit_s": 0.0,
    "speed_mps": 0.0,
    "load_raw": 0,
    "load_change": 0,
    "overload": False,
    "temp_c": None,
    "temp_valid": False,
    "transit_timeout": False,
    "fast_transit": False,
    "fault": "STARTUP",
    "observation": "Waiting for ESP32",
    "reason": "Waiting for telemetry",
    "recommendation": "Connect the ESP32",
    "last_prediction": "Not trained",
    "sample_count": 0,
}


# ---------------- SERIAL COMMUNICATION ----------------
def send_command(command):
    global ser

    if ser is None or not ser.is_open:
        return False

    try:
        with SERIAL_LOCK:
            ser.write((command + "\n").encode("ascii"))
            ser.flush()
        return True
    except (serial.SerialException, OSError) as exc:
        print("Serial write error:", exc)
        return False


def diagnose(data):
    """Continuous checks for hard-safety conditions."""

    if not data.get("temp_valid", False):
        return (
            "TEMPERATURE_SENSOR_FAULT",
            "Temperature reading is invalid",
            "Check DS18B20 wiring and pull-up resistor",
        )

    temp = data.get("temp_c")
    if temp is None or temp >= 42:
        return (
            "HIGH_TEMPERATURE",
            "Temperature reached the configured safety limit",
            "Keep the conveyor stopped until temperature is safe",
        )

    if not data.get("load_valid", False):
        return (
            "HX711_NOT_READY",
            "Load-cell data is unavailable",
            "Check HX711 power, DOUT and SCK wiring",
        )

    if data.get("overload", False):
        return (
            "OVERLOAD",
            "Raw load change exceeded the configured threshold",
            "Reduce the load and check the platform",
        )

    return (
        "NORMAL",
        "No active hard-safety fault",
        "Continue monitoring",
    )


def process_line(line):
    global STOP_LATCHED
    global LAST_TRANSIT_ID
    global LAST_TIMEOUT_EVENT

    fields = line.strip().split(",")

    # Expected ESP32 line:
    # DATA,uptime,ir1,ir2,overall,transit_id,transit_s,
    # timeout,fast,load_raw,load_change,overload,temp_c,temp_valid,fault

    if len(fields) != 15 or fields[0] != "DATA":
        if line.startswith("COUNT_RESET"):
            with LOCK:
                STATE["ir1_count"] = 0
                STATE["ir2_count"] = 0
                STATE["overall_count"] = 0
            print("ESP32 counts reset")
        elif line.startswith("MOTOR,RUN"):
            with LOCK:
                STATE["motor"] = "RUN"
        return

    try:
        temp_valid = bool(int(fields[13]))
        temp_c = (
            float(fields[12])
            if temp_valid and fields[12].lower() != "nan"
            else None
        )

        new = {
            "connected": True,
            "ir1_count": int(fields[2]),
            "ir2_count": int(fields[3]),
            "overall_count": int(fields[4]),
            "transit_id": int(fields[5]),
            "transit_s": float(fields[6]),
            "transit_timeout": bool(int(fields[7])),
            "fast_transit": bool(int(fields[8])),
            "load_raw": int(fields[9]),
            "load_change": int(fields[10]),
            "overload": bool(int(fields[11])),
            "temp_c": temp_c,
            "temp_valid": temp_valid,
            "fault": fields[14],
            "load_valid": True,
        }

        new["speed_mps"] = (
            0.25 / new["transit_s"]
            if new["transit_s"] > 0
            else 0.0
        )

        fault, observation, recommendation = diagnose(new)

        # Hard-safety conditions always take priority.
        if fault != "NORMAL":
            STOP_LATCHED = True
            send_command("STOP")

            new.update({
                "motor": "STOP",
                "fault": fault,
                "observation": observation,
                "reason": observation,
                "recommendation": recommendation,
            })

        else:
            # Speed warnings are evaluated only for a new completed transit.
            current_transit_id = new["transit_id"]

            if current_transit_id != LAST_TRANSIT_ID:
                LAST_TRANSIT_ID = current_transit_id
                elapsed = new["transit_s"]

                if elapsed > 0 and elapsed < 8:
                    STOP_LATCHED = True
                    send_command("STOP")

                    new.update({
                        "motor": "STOP",
                        "fault": "OVERSPEED",
                        "observation": "Overspeed event detected",
                        "reason": (
                            f"Transit took {elapsed:.2f} seconds; "
                            "threshold is 8 seconds"
                        ),
                        "recommendation": (
                            "Inspect motor speed and sensor alignment"
                        ),
                    })

                elif elapsed > 15:
                    STOP_LATCHED = True
                    send_command("STOP")

                    new.update({
                        "motor": "STOP",
                        "fault": "SLOW_SPEED",
                        "observation": "Slow transit event detected",
                        "reason": (
                            f"Transit took {elapsed:.2f} seconds; "
                            "threshold is 15 seconds"
                        ),
                        "recommendation": (
                            "Inspect conveyor movement and possible obstruction"
                        ),
                    })

            # Timeout is handled as an event, not a permanent warning.
            timeout_now = new["transit_timeout"]

            if timeout_now and not LAST_TIMEOUT_EVENT:
                STOP_LATCHED = True
                send_command("STOP")

                new.update({
                    "motor": "STOP",
                    "fault": "TRANSIT_TIMEOUT",
                    "observation": "Transit timeout detected",
                    "reason": "A product did not reach the outlet in time",
                    "recommendation": (
                        "Inspect the belt, product path and IR sensors"
                    ),
                })

            LAST_TIMEOUT_EVENT = timeout_now

            if not STOP_LATCHED:
                new.update({
                    "motor": "RUN",
                    "fault": "NORMAL",
                    "observation": "Conveyor operating normally",
                    "reason": "No new fault detected",
                    "recommendation": "Continue monitoring",
                })

        with LOCK:
            STATE.update(new)

    except (ValueError, IndexError) as exc:
        print("Telemetry parsing error:", exc)


def serial_worker(port, baud):
    global ser

    try:
        ser = serial.Serial(port, baud, timeout=1)
        print(f"Connected to ESP32 on {port} at {baud} baud")

        while True:
            line = ser.readline().decode(
                "utf-8", errors="ignore"
            ).strip()

            if line:
                process_line(line)

    except (serial.SerialException, OSError) as exc:
        print("Serial connection error:", exc)

        with LOCK:
            STATE.update({
                "connected": False,
                "motor": "STOP",
                "observation": "ESP32 disconnected",
                "reason": str(exc),
                "recommendation": (
                    "Check the USB cable, COM port and baud rate"
                ),
            })


# ---------------- DASHBOARD ----------------
HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Conveyor AI Control Center</title>

<style>
:root {
 --bg:#09111f; --panel:#142137; --border:#2a3c57;
 --text:#edf4ff; --muted:#9fb1cb;
 --green:#25d5a0; --red:#ff6478;
 --blue:#78b7ff; --gold:#ffcb6b;
}
* { box-sizing:border-box; }
body {
 margin:0; background:var(--bg); color:var(--text);
 font-family:Segoe UI,Arial,sans-serif;
}
header {
 padding:22px 4%; background:#101b2d;
 border-bottom:1px solid var(--border);
 display:flex; justify-content:space-between;
 align-items:center; gap:16px; flex-wrap:wrap;
}
h1 { font-size:25px; margin:0 0 7px; }
h2 { font-size:18px; margin:0 0 17px; }
.subtitle,.muted { color:var(--muted); font-size:13px; }
main { max-width:1500px; margin:auto; padding:24px; }
.grid {
 display:grid; grid-template-columns:repeat(4,minmax(0,1fr));
 gap:15px;
}
.two {
 display:grid; grid-template-columns:repeat(2,minmax(0,1fr));
 gap:15px;
}
.card {
 background:var(--panel); border:1px solid var(--border);
 border-radius:14px; padding:21px; min-width:0;
}
.metric {
 font-size:30px; font-weight:750; margin:13px 0;
 overflow-wrap:anywhere;
}
.tag {
 display:inline-block; padding:7px 11px; border-radius:20px;
 font-size:12px; font-weight:750; margin-left:4px;
}
.good { background:#124435; color:#6cf0c2; }
.bad { background:#4b2330; color:#ff9baa; }
.warn { background:#4a391c; color:#ffdfa0; }
.actions { display:flex; flex-wrap:wrap; gap:10px; }
button,select {
 border:0; border-radius:8px; padding:12px 15px;
 font-size:13px; font-weight:700; cursor:pointer;
}
button:hover { filter:brightness(1.1); }
button:disabled { opacity:.55; cursor:not-allowed; }
.stop { background:var(--red); color:#28080d; }
.restart { background:var(--green); color:#06271e; }
.blue { background:var(--blue); color:#0b203b; }
.gold { background:var(--gold); color:#352300; }
select { background:#0b1729; color:white; border:1px solid var(--border); }
.value { font-size:14px; line-height:1.7; overflow-wrap:anywhere; }
a { color:var(--blue); font-size:13px; margin-right:14px; }
hr { border:0; border-top:1px solid var(--border); margin:18px 0; }
footer { text-align:center; padding:22px; color:var(--muted); font-size:12px; }
@media(max-width:950px) {
 .grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
 .two { grid-template-columns:1fr; }
}
@media(max-width:520px) {
 main { padding:12px; }
 .grid { grid-template-columns:1fr; }
 header { padding:18px; }
}
</style>
</head>

<body>
<header>
 <div>
  <h1>Conveyor AI Control Center</h1>
  <div class="subtitle">Live monitoring · Fault diagnostics · Random Forest</div>
 </div>
 <div>
  <span id="connection" class="tag warn">CONNECTING</span>
  <span id="motor" class="tag bad">MOTOR STOP</span>
 </div>
</header>

<main>
 <section class="grid">
  <div class="card">
   <div class="muted">INLET · IR1 COUNT</div>
   <div id="ir1" class="metric">0</div>
   <div class="muted">Detected at inlet</div>
  </div>
  <div class="card">
   <div class="muted">OUTLET · IR2 COUNT</div>
   <div id="ir2" class="metric">0</div>
   <div class="muted">Outlet detections</div>
  </div>
  <div class="card">
   <div class="muted">OVERALL COMPLETED PRODUCTS</div>
   <div id="overall" class="metric">0</div>
   <div class="muted">Saved in ESP32 flash</div>
  </div>
  <div class="card">
   <div class="muted">HX711 RAW VALUE</div>
   <div id="raw" class="metric">—</div>
   <div class="muted">ADC raw reading</div>
  </div>
 </section>

 <section class="card" style="margin-top:16px">
  <h2>Motor Control</h2>
  <div class="actions">
   <button class="stop" onclick="post('/api/stop')">STOP CONVEYOR</button>
   <button class="restart" onclick="post('/api/restart')">RESTART CONVEYOR</button>
   <button class="stop" onclick="resetCount()">RESET COUNTS</button>
  </div>
  <p id="motorMessage" class="value">Waiting for ESP32…</p>
  <p class="muted">
   Resetting counters does not delete your saved CSV or trained model.
  </p>
 </section>

 <section class="two" style="margin-top:16px">
  <div class="card">
   <h2>Observation</h2>
   <p id="observation" class="value">Waiting for data</p>
  </div>
  <div class="card">
   <h2>Fault Analysis</h2>
   <div class="muted">REASON</div>
   <p id="reason" class="value">—</p>
   <div class="muted">RECOMMENDATION</div>
   <p id="recommendation" class="value">—</p>
  </div>
 </section>

 <section class="grid" style="margin-top:16px">
  <div class="card">
   <div class="muted">RAW CHANGE FROM TARE</div>
   <div id="change" class="metric">—</div>
   <div class="muted">Raw counts only</div>
  </div>
  <div class="card">
   <div class="muted">TEMPERATURE</div>
   <div id="temp" class="metric">—</div>
   <div class="muted">Configured limit: 70 °C</div>
  </div>
  <div class="card">
   <div class="muted">TRANSIT TIME</div>
   <div id="transit" class="metric">—</div>
   <div class="muted">IR1 to IR2</div>
  </div>
  <div class="card">
   <div class="muted">SPEED ESTIMATE</div>
   <div id="speed" class="metric">—</div>
   <div class="muted">Using 25 cm spacing</div>
  </div>
 </section>

 <section class="two" style="margin-top:16px">
  <div class="card">
   <h2>Collect Labelled Data</h2>
   <p class="muted">Select the real operating condition and save the current readings.</p>
   <div class="actions">
    <select id="label"></select>
    <button class="blue" onclick="collectSample()">SAVE SAMPLE</button>
   </div>
   <p id="collectMsg" class="value"></p>
   <p class="muted">Saved samples: <b id="samples">0</b></p>
   <hr>
   <a href="/api/download/csv">Download CSV</a>
   <a href="/api/download/excel">Download Excel</a>
  </div>

  <div class="card">
   <h2>Random Forest AI</h2>
   <p class="muted">Predictions are advisory and must not replace physical safety controls.</p>
   <div class="actions">
    <button class="gold" onclick="trainModel()">TRAIN MODEL</button>
    <button class="blue" onclick="predict()">PREDICT</button>
    <button class="stop" onclick="deleteModel()">DELETE MODEL</button>
   </div>
   <p id="mlMsg" class="value">Prediction: Not trained</p>
   <hr>
   <a href="/api/download/model">Download trained model</a>
  </div>
 </section>
</main>

<footer>Conveyor AI Control Center · Local dashboard</footer>

<script>
const labels = __LABELS__;
const selector = document.getElementById('label');

labels.forEach(label => {
 const option = document.createElement('option');
 option.value = label;
 option.textContent = label;
 selector.appendChild(option);
});

async function post(url, body={}) {
 try {
  const response = await fetch(url, {
   method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify(body)
  });
  const result = await response.json();

  document.getElementById('motorMessage').textContent =
   result.message || '';

  if (!response.ok) alert(result.message || 'Request failed');

  await refresh();
  return result;
 } catch(error) {
  document.getElementById('motorMessage').textContent =
   'Dashboard request failed';
  return {};
 }
}

async function resetCount() {
 if (!confirm('Reset IR1, IR2 and overall counts to zero?')) return;
 await post('/api/reset-count');
}

async function collectSample() {
 const result = await post('/api/collect', {
  label:selector.value
 });
 document.getElementById('collectMsg').textContent =
  result.message || '';
}

async function trainModel() {
 document.getElementById('mlMsg').textContent = 'Training…';
 const result = await post('/api/train');
 document.getElementById('mlMsg').textContent =
  result.message || '';
}

async function predict() {
 const result = await post('/api/predict');
 document.getElementById('mlMsg').textContent =
  result.prediction
   ? 'Prediction: ' + result.prediction
   : (result.message || 'Prediction failed');
}

async function deleteModel() {
 if (!confirm('Delete the trained model? CSV data will remain.')) return;
 const result = await post('/api/model/delete');
 document.getElementById('mlMsg').textContent =
  result.message || '';
}

async function refresh() {
 try {
  const response = await fetch('/api/status');
  const s = await response.json();

  const connection = document.getElementById('connection');
  connection.textContent = s.connected
   ? 'ESP32 CONNECTED' : 'ESP32 DISCONNECTED';
  connection.className = 'tag ' + (s.connected ? 'good' : 'bad');

  const motor = document.getElementById('motor');
  motor.textContent = 'MOTOR ' + s.motor;
  motor.className = 'tag ' + (s.motor === 'RUN' ? 'good' : 'bad');

  document.getElementById('ir1').textContent = s.ir1_count ?? 0;
  document.getElementById('ir2').textContent = s.ir2_count ?? 0;
  document.getElementById('overall').textContent = s.overall_count ?? 0;
  document.getElementById('raw').textContent = s.load_raw ?? '—';
  document.getElementById('change').textContent = s.load_change ?? '—';

  document.getElementById('temp').textContent =
   s.temp_valid && s.temp_c !== null
    ? Number(s.temp_c).toFixed(1) + ' °C' : 'INVALID';

  document.getElementById('transit').textContent =
   Number(s.transit_s || 0).toFixed(2) + ' s';

  document.getElementById('speed').textContent =
   Number(s.speed_mps || 0).toFixed(3) + ' m/s';

  document.getElementById('observation').textContent =
   s.observation || '—';

  document.getElementById('reason').textContent =
   s.reason || '—';

  document.getElementById('recommendation').textContent =
   s.recommendation || '—';

  document.getElementById('motorMessage').textContent =
   s.fault === 'NORMAL'
    ? 'Conveyor running or ready; monitoring new events.'
    : 'Current status: ' + (s.fault || '—');

  document.getElementById('samples').textContent =
   s.sample_count || 0;

  if (!document.getElementById('mlMsg').textContent.startsWith('Training')) {
   document.getElementById('mlMsg').textContent =
    'Prediction: ' + (s.last_prediction || 'Not trained');
  }
 } catch(error) {
  document.getElementById('connection').textContent = 'DASHBOARD ERROR';
  document.getElementById('connection').className = 'tag bad';
 }
}

refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
""".replace("__LABELS__", json.dumps(LABELS))


# ---------------- API ROUTES ----------------
@app.get("/")
def home():
    return render_template_string(HTML)


@app.get("/api/status")
def status():
    with LOCK:
        result = dict(STATE)

    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, newline="", encoding="utf-8") as file:
                result["sample_count"] = max(
                    sum(1 for _ in csv.reader(file)) - 1, 0
                )
        except OSError:
            pass

    result["model_exists"] = os.path.exists(MODEL_FILE)
    return jsonify(result)


@app.post("/api/stop")
def stop():
    global STOP_LATCHED
    STOP_LATCHED = True

    if not send_command("STOP"):
        return jsonify(
            ok=False,
            message="Cannot send STOP: ESP32 is disconnected",
        ), 503

    with LOCK:
        STATE.update({
            "motor": "STOP",
            "fault": "MANUAL_STOP",
            "observation": "Manual stop requested",
            "reason": "Operator pressed STOP",
            "recommendation": "Inspect the conveyor before restarting",
        })

    return jsonify(ok=True, message="STOP command sent")


@app.post("/api/restart")
def restart():
    global STOP_LATCHED
    global LAST_TRANSIT_ID
    global LAST_TIMEOUT_EVENT

    with LOCK:
        safety_fault, observation, recommendation = diagnose(STATE)

        if safety_fault != "NORMAL":
            STATE.update({
                "motor": "STOP",
                "fault": safety_fault,
                "observation": observation,
                "reason": observation,
                "recommendation": recommendation,
            })
            return jsonify(
                ok=False,
                message="Safety interlock: " + safety_fault,
            ), 409

        current_id = STATE["transit_id"]

    if not send_command("RESTART"):
        return jsonify(
            ok=False,
            message="Cannot restart: ESP32 is disconnected",
        ), 503

    # Clear prior diagnostic events; evaluate only future transits.
    LAST_TRANSIT_ID = current_id
    LAST_TIMEOUT_EVENT = False
    STOP_LATCHED = False

    with LOCK:
        STATE.update({
            "motor": "RUN",
            "fault": "NORMAL",
            "transit_timeout": False,
            "fast_transit": False,
            "observation": "Conveyor restart requested",
            "reason": "Previous IR/speed diagnostic acknowledged",
            "recommendation": "Monitoring for new events",
        })

    return jsonify(ok=True, message="Restart command sent")


@app.post("/api/reset-count")
def reset_count():
    if not send_command("RESET_COUNT"):
        return jsonify(
            ok=False,
            message="Cannot reset: ESP32 is disconnected",
        ), 503

    with LOCK:
        STATE.update({
            "ir1_count": 0,
            "ir2_count": 0,
            "overall_count": 0,
            "observation": "Count reset requested",
            "reason": "Waiting for ESP32 confirmation",
        })

    return jsonify(
        ok=True,
        message="Count reset command sent to ESP32",
    )


@app.post("/api/collect")
def collect():
    body = request.get_json(silent=True) or {}
    label = body.get("label", "NORMAL")

    if label not in LABELS:
        return jsonify(ok=False, message="Invalid label"), 400

    with LOCK:
        snapshot = dict(STATE)

    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "label": label,
    }
    row.update({feature: snapshot.get(feature) for feature in FEATURES})

    exists = os.path.exists(DATA_FILE)

    try:
        with open(DATA_FILE, "a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=row.keys())
            if not exists:
                writer.writeheader()
            writer.writerow(row)
    except OSError as exc:
        return jsonify(ok=False, message=str(exc)), 500

    with LOCK:
        STATE["sample_count"] += 1

    return jsonify(ok=True, message=f"Sample saved: {label}")


@app.post("/api/train")
def train():
    global MODEL

    if not os.path.exists(DATA_FILE):
        return jsonify(
            ok=False,
            message="Collect labelled samples first",
        ), 400

    try:
        df = pd.read_csv(DATA_FILE)

        if len(df) < 20:
            return jsonify(
                ok=False,
                message="Collect at least 20 samples",
            ), 400

        if df["label"].nunique() < 2:
            return jsonify(
                ok=False,
                message="Collect at least two different labels",
            ), 400

        X = df[FEATURES].apply(pd.to_numeric, errors="coerce")
        y = df["label"].astype(str)

        model = Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("forest", RandomForestClassifier(
                n_estimators=150,
                random_state=42,
                class_weight="balanced",
            )),
        ])

        model.fit(X, y)
        joblib.dump(model, MODEL_FILE)
        MODEL = model

        return jsonify(
            ok=True,
            message=f"Random Forest trained with {len(df)} samples",
        )

    except Exception as exc:
        return jsonify(ok=False, message=str(exc)), 500


@app.post("/api/predict")
def predict():
    global MODEL

    try:
        if MODEL is None and os.path.exists(MODEL_FILE):
            MODEL = joblib.load(MODEL_FILE)

        if MODEL is None:
            return jsonify(
                ok=False,
                message="Train a model first",
            ), 400

        with LOCK:
            snapshot = dict(STATE)

        X = pd.DataFrame([{
            feature: pd.to_numeric(
                pd.Series([snapshot.get(feature)]),
                errors="coerce",
            ).iloc[0]
            for feature in FEATURES
        }])

        prediction = str(MODEL.predict(X)[0])

        with LOCK:
            STATE["last_prediction"] = prediction

        return jsonify(ok=True, prediction=prediction)

    except Exception as exc:
        return jsonify(ok=False, message=str(exc)), 500


@app.post("/api/model/delete")
def delete_model():
    global MODEL
    MODEL = None

    if os.path.exists(MODEL_FILE):
        try:
            os.remove(MODEL_FILE)
        except OSError as exc:
            return jsonify(ok=False, message=str(exc)), 500

    with LOCK:
        STATE["last_prediction"] = "Not trained"

    return jsonify(
        ok=True,
        message="Model deleted. CSV training data preserved.",
    )


@app.get("/api/download/csv")
def download_csv():
    if not os.path.exists(DATA_FILE):
        return "No dataset available", 404

    with open(DATA_FILE, "r", encoding="utf-8") as file:
        content = file.read()

    return Response(
        content,
        mimetype="text/csv",
        headers={
            "Content-Disposition":
                "attachment; filename=conveyor_data.csv"
        },
    )


@app.get("/api/download/excel")
def download_excel():
    if not os.path.exists(DATA_FILE):
        return "No dataset available", 404

    output = io.BytesIO()

    try:
        pd.read_csv(DATA_FILE).to_excel(
            output, index=False, engine="openpyxl"
        )
    except Exception as exc:
        return str(exc), 500

    output.seek(0)

    return Response(
        output.getvalue(),
        mimetype=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition":
                "attachment; filename=conveyor_data.xlsx"
        },
    )


@app.get("/api/download/model")
def download_model():
    if not os.path.exists(MODEL_FILE):
        return "No trained model available", 404

    with open(MODEL_FILE, "rb") as file:
        content = file.read()

    return Response(
        content,
        mimetype="application/octet-stream",
        headers={
            "Content-Disposition":
                "attachment; filename=conveyor_rf.joblib"
        },
    )


# ---------------- START APPLICATION ----------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Conveyor AI dashboard"
    )
    parser.add_argument(
        "--port", required=True,
        help="ESP32 serial port, for example COM4"
    )
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--web-port", type=int, default=5000)
    args = parser.parse_args()

    if os.path.exists(MODEL_FILE):
        try:
            MODEL = joblib.load(MODEL_FILE)
        except Exception as exc:
            print("Saved model could not be loaded:", exc)

    threading.Thread(
        target=serial_worker,
        args=(args.port, args.baud),
        daemon=True,
    ).start()

    app.run(
        host="127.0.0.1",
        port=args.web_port,
        debug=False,
    )
