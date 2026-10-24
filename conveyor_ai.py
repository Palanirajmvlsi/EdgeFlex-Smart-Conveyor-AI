#!/usr/bin/env python3
"""
Conveyor serial monitoring + rule-based fault prediction.
Install: python -m pip install pyserial
Run:     python conveyor_ai.py --port COM27
Linux:   python conveyor_ai.py --port /dev/ttyUSB0

This version uses configurable rules, not a trained ML model. It reports
separate faults and a rule-based risk score; it does not claim calibrated
AI probability.
"""
import argparse
import csv
import sys
import time
from datetime import datetime

import serial
from serial import SerialException

# ================= USER CONFIGURATION =================
BAUD_RATE = 115200
OVER_TEMPERATURE_C = 70.0  # change to the limit suitable for your installation
HIGH_SPEED_THRESHOLD_S = 4.0
MISSING_IR2_TIMEOUT_S = 10.0
SENSOR_DISTANCE_M = 0.25
# This threshold is raw HX711 counts, not kilograms.
OVERLOAD_THRESHOLD_COUNTS = 5000
# ======================================================

EXPECTED_FIELDS = {
    "type", "uptime_ms", "ir1_count", "ir2_count", "transit_id",
    "transit_s", "transit_timeout", "fast_transit", "load_raw",
    "load_change", "overload", "temp_c", "temp_valid"
}

def as_int(row, key, default=0):
    try:
        return int(float(row.get(key, default)))
    except (TypeError, ValueError):
        return default

def as_float(row, key, default=None):
    try:
        value = float(row.get(key, ""))
        return value if value == value else default
    except (TypeError, ValueError):
        return default

def evaluate(row, state):
    """Return current faults and an illustrative rule-based risk score."""
    faults = []
    recommendations = []
    event_type = row.get("type", "DATA").strip()

    transit_s = as_float(row, "transit_s", -1.0)
    load_change = as_int(row, "load_change")
    overload_flag = as_int(row, "overload") == 1
    temp_valid = as_int(row, "temp_valid") == 1
    temp_c = as_float(row, "temp_c")

    # Timeout is explicit from the ESP32 event; don't keep re-reporting it on every heartbeat.
    if event_type == "TRANSIT_TIMEOUT":
        faults.append("CONVEYOR_MOVEMENT_TIMEOUT")
        recommendations.append(
            "IR1 product was not detected by IR2 within 10 seconds. Check belt, jam, sensor alignment, and wiring."
        )

    # High-speed is checked only when a complete IR1 -> IR2 transit is reported.
    if event_type == "TRANSIT" and transit_s is not None and transit_s >= 0:
        state["last_transit_s"] = transit_s
        state["estimated_speed_m_s"] = SENSOR_DISTANCE_M / transit_s if transit_s > 0 else None
        if transit_s < HIGH_SPEED_THRESHOLD_S:
            faults.append("CONVEYOR_SPEED_TOO_HIGH")
            recommendations.append(
                f"Transit time {transit_s:.3f}s is below {HIGH_SPEED_THRESHOLD_S:.1f}s. Verify motor speed and sensor spacing."
            )

    if overload_flag or load_change >= OVERLOAD_THRESHOLD_COUNTS:
        faults.append("OVERLOAD")
        recommendations.append(
            f"Load change is {load_change} raw counts (threshold {OVERLOAD_THRESHOLD_COUNTS}). Reduce load and inspect the load-cell mounting."
        )

    if temp_valid and temp_c is not None and temp_c >= OVER_TEMPERATURE_C:
        faults.append("OVER_TEMPERATURE")
        recommendations.append(
            f"Temperature is {temp_c:.1f}°C (limit {OVER_TEMPERATURE_C:.1f}°C). Inspect motor, bearings, and ventilation."
        )

    # A simple score for rule severity, NOT an ML probability.
    score = 0
    if "CONVEYOR_MOVEMENT_TIMEOUT" in faults:
        score += 50
    if "CONVEYOR_SPEED_TOO_HIGH" in faults:
        score += 30
    if "OVERLOAD" in faults:
        score += 40
    if "OVER_TEMPERATURE" in faults:
        score += 40
    score = min(score, 100)

    if not faults:
        status = "NORMAL"
        recommendations = ["All configured rules are currently within limits."]
    else:
        status = "FAULT"
    return status, faults, recommendations, score

def main():
    parser = argparse.ArgumentParser(description="ESP32 conveyor fault monitor")
    parser.add_argument("--port", required=True, help="Serial port, e.g. COM4 or /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=BAUD_RATE)
    parser.add_argument("--log", default="conveyor_log.csv", help="Output log CSV filename")
    args = parser.parse_args()

    state = {"last_transit_s": None, "estimated_speed_m_s": None}
    log_file = open(args.log, "a", newline="", encoding="utf-8")
    writer = csv.writer(log_file)
    if log_file.tell() == 0:
        writer.writerow([
            "pc_timestamp", "event_type", "uptime_ms", "ir1_count", "ir2_count",
            "transit_s", "estimated_speed_m_s", "load_raw", "load_change",
            "overload", "temp_c", "status", "faults", "risk_score_rule_based"
        ])
        log_file.flush()

    print("\n=== CONVEYOR MONITORING / PREDICTION ===")
    print(f"Port: {args.port} | Baud: {args.baud}")
    print(f"IR distance: {SENSOR_DISTANCE_M:.2f} m | High-speed threshold: < {HIGH_SPEED_THRESHOLD_S:.1f} s")
    print(f"IR2 timeout: {MISSING_IR2_TIMEOUT_S:.1f} s | Overload: {OVERLOAD_THRESHOLD_COUNTS} raw counts")
    print(f"Over-temperature: {OVER_TEMPERATURE_C:.1f} C")
    print("Close Arduino Serial Monitor before running this program.\n")

    try:
        with serial.Serial(args.port, args.baud, timeout=1) as ser:
            # ESP32 USB serial may reset on port open.
            time.sleep(2)
            ser.reset_input_buffer()
            header = None
            while True:
                raw_line = ser.readline()
                if not raw_line:
                    continue  # serial timeout; keep waiting for the next record
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if header is None:
                    candidate = [part.strip() for part in line.split(",")]
                    if EXPECTED_FIELDS.issubset(set(candidate)):
                        header = candidate
                    continue
                values = next(csv.reader([line]))
                if len(values) != len(header):
                    continue
                row = dict(zip(header, values))
                if not EXPECTED_FIELDS.issubset(row.keys()):
                    continue
                event_type = (row.get("type") or "DATA").strip()
                if event_type == "BOOT":
                    print("[ESP32] Started; receiving sensor data.")
                    continue

                status, faults, recommendations, score = evaluate(row, state)
                transit_s = as_float(row, "transit_s", -1.0)
                load_change = as_int(row, "load_change")
                temp_valid = as_int(row, "temp_valid") == 1
                temp_c = as_float(row, "temp_c")
                speed = state["estimated_speed_m_s"]

                # Print heartbeat on DATA and detail events; this gives a live console dashboard.
                timestamp = datetime.now().strftime("%H:%M:%S")
                print(
                    f"[{timestamp}] {event_type:16} | IR1={as_int(row, 'ir1_count'):5} "
                    f"IR2={as_int(row, 'ir2_count'):5} | "
                    f"Transit={transit_s:.3f}s" if transit_s is not None and transit_s >= 0
                    else f"[{timestamp}] {event_type:16} | IR1={as_int(row, 'ir1_count'):5} IR2={as_int(row, 'ir2_count'):5}",
                    end=""
                )
                print(f" | LoadΔ={load_change} counts | Temp={temp_c:.1f}C" if temp_valid and temp_c is not None
                      else f" | LoadΔ={load_change} counts | Temp=INVALID", end="")
                if speed is not None:
                    print(f" | estimated belt speed={speed:.3f} m/s", end="")
                print(f" | {status} | rule-risk={score}/100")

                for fault in faults:
                    print(f"  !!! PREDICTION: {fault}")
                for recommendation in recommendations:
                    print(f"  -> {recommendation}")

                writer.writerow([
                    datetime.now().isoformat(timespec="seconds"), event_type,
                    row.get("uptime_ms", ""), row.get("ir1_count", ""),
                    row.get("ir2_count", ""), row.get("transit_s", ""),
                    f"{speed:.6f}" if speed is not None else "",
                    row.get("load_raw", ""), row.get("load_change", ""),
                    row.get("overload", ""), row.get("temp_c", ""),
                    status, "|".join(faults), score
                ])
                log_file.flush()

    except KeyboardInterrupt:
        print("\nStopped by user.")
    except SerialException as exc:
        print(f"\nSerial error: {exc}", file=sys.stderr)
        print("Check the port name, USB cable, driver, and close Arduino Serial Monitor.", file=sys.stderr)
        sys.exit(2)
    finally:
        log_file.close()

if __name__ == "__main__":
    main()
