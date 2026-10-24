# ESP32 Conveyor Monitoring + Python Fault Prediction

## Files
- `conveyor_esp32.ino`: ESP32 firmware. Uses HX711, DS18B20, two active-LOW IR sensors, and TB6600 stepper outputs.
- `conveyor_ai.py`: reads structured CSV serial records, detects timeout/high-speed/overload/over-temperature, and writes `conveyor_log.csv`.

## Arduino wiring (preserves the current GPIO map)
- HX711 DOUT -> GPIO19; SCK -> GPIO18
- DS18B20 DATA -> GPIO4, with a 4.7kΩ pull-up from DATA to 3.3V
- IR1 OUT -> GPIO32; IR2 OUT -> GPIO33
- TB6600 STEP/PUL signal -> GPIO25; DIR -> GPIO26; ENA -> GPIO27
- Ensure every ESP32 input signal is 3.3V-safe. Do not feed a 5V IR output directly into an ESP32 GPIO.
- TB6600 PUL/DIR/ENA wiring depends on the driver's input circuit; follow its manual. Motor power must come from the suitable external motor supply.
- GPIO18/19 are shared with HX711 in this setup, so do not connect the TB6600 STEP/DIR to those pins.

## Arduino setup
1. Install libraries from Arduino Library Manager:
   - HX711 (by Bogdan Necula / compatible HX711 library)
   - OneWire
   - DallasTemperature
2. Select the correct ESP32 board and upload `conveyor_esp32.ino`.
3. The serial output starts with a CSV header. Do not use Serial Monitor while Python is connected.

## Python setup (Windows)
```bash
py -m pip install pyserial
py conveyor_ai.py --port COM4
```
Replace `COM4` with the port shown in Device Manager.

## Python setup (Linux / Raspberry Pi)
```bash
python3 -m pip install pyserial
python3 conveyor_ai.py --port /dev/ttyUSB0
```
If the OS blocks pip installation, use a virtual environment.

## Configurable limits
Edit at the top of `conveyor_ai.py`:
- `OVER_TEMPERATURE_C = 70.0`
- `OVERLOAD_THRESHOLD_COUNTS = 5000`
- `HIGH_SPEED_THRESHOLD_S = 4.0`
- `MISSING_IR2_TIMEOUT_S = 10.0`
- `SENSOR_DISTANCE_M = 0.25`

The ESP32 also enforces the 10-second IR2 timeout and emits a `TRANSIT_TIMEOUT` event.

## Important behavior / safety
- This is rule-based fault detection, not a trained AI/Random Forest model yet. The printed `rule-risk` score is an illustrative severity score, NOT a statistical probability.
- A 25 cm spacing and a 4 s threshold corresponds to 0.0625 m/s; anything faster trips the configured high-speed rule. Confirm this threshold is suitable for the real conveyor.
- Transit time is measured from IR1 to IR2. With products close together, this simple single-pending-event logic may not pair products reliably. For a multi-product line, use a queue of IR1 timestamps.
- HX711 values are raw ADC counts; the `5000` threshold is not a weight in kg. Confirm direction and threshold against measured raw values.
- The stepper pulses continuously and does not automatically stop on fault. Use a suitable hardware emergency stop and add a validated motor-stop policy before relying on this for safety.
- `DRIVER_ENABLE_LEVEL` and `MOTOR_DIRECTION_LEVEL` depend on TB6600 wiring and polarity.
