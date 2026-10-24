
#include <HX711.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <Preferences.h>

// ---------------- PIN CONFIGURATION ----------------
#define HX_DOUT   19
#define HX_SCK    18
#define TEMP_PIN   4
#define IR1_PIN   32
#define IR2_PIN   33
#define STEP_PIN  25
#define DIR_PIN   26
#define ENA_PIN   27

// ---------------- THRESHOLDS ----------------
const long RAW_OVERLOAD_LIMIT = 10000;
const float TEMP_LIMIT_C = 42.0;

const unsigned long OVERSPEED_MS = 8000;
const unsigned long SLOW_SPEED_MS = 15000;
const unsigned long TRANSIT_TIMEOUT_MS = 15000;
const unsigned long IR_DEBOUNCE_MS = 100;
const unsigned long STEP_INTERVAL_US = 700;
const float SENSOR_DISTANCE_M = 0.25;

const bool ENA_ACTIVE_HIGH = true;

// ---------------- DEVICES ----------------
HX711 scale;
OneWire oneWire(TEMP_PIN);
DallasTemperature tempSensor(&oneWire);
Preferences preferences;

// ---------------- COUNTERS ----------------
uint32_t ir1Count = 0;
uint32_t ir2Count = 0;
uint32_t overallCount = 0;
uint32_t transitId = 0;

// ---------------- SENSOR DATA ----------------
long loadRaw = 0;
long loadChange = 0;
long zeroOffset = 0;

float temperatureC = NAN;
float transitSeconds = 0;
float speedMps = 0;

bool hxReady = false;
bool hxCalibrated = false;
bool tempValid = false;
bool overload = false;

// ---------------- MOTOR / FAULT STATE ----------------
bool manualStop = true;
bool faultLatched = true;
bool transitActive = false;
bool transitTimeout = false;
bool fastTransit = false;

String fault = "STARTUP";

unsigned long transitStartMs = 0;
unsigned long lastStepUs = 0;
unsigned long lastLoadMs = 0;
unsigned long lastTempMs = 0;
unsigned long lastSendMs = 0;

bool rawIR1 = HIGH;
bool rawIR2 = HIGH;
bool stableIR1 = HIGH;
bool stableIR2 = HIGH;

unsigned long ir1ChangedMs = 0;
unsigned long ir2ChangedMs = 0;

String commandBuffer;

// ---------------- PERSISTENT STORAGE ----------------
void loadSavedCount() {
  preferences.begin("conveyor", false);

  overallCount = preferences.getUInt("overall", 0);
  ir1Count = preferences.getUInt("ir1", 0);
  ir2Count = preferences.getUInt("ir2", 0);
  transitId = preferences.getUInt("transit", 0);
}

void saveCounts() {
  preferences.putUInt("overall", overallCount);
  preferences.putUInt("ir1", ir1Count);
  preferences.putUInt("ir2", ir2Count);
  preferences.putUInt("transit", transitId);
}

void resetCounts() {
  overallCount = 0;
  ir1Count = 0;
  ir2Count = 0;
  transitId = 0;

  saveCounts();

  Serial.println("COUNT_RESET,0");
}

// ---------------- MOTOR CONTROL ----------------
void motorEnable(bool enable) {
  bool level = ENA_ACTIVE_HIGH ? enable : !enable;
  digitalWrite(ENA_PIN, level ? HIGH : LOW);
}

void stopMotor(const String &reason) {
  manualStop = true;
  faultLatched = true;
  fault = reason;

  motorEnable(false);
  digitalWrite(STEP_PIN, LOW);
}

bool hardSafetyOK() {
  return hxReady &&
         hxCalibrated &&
         tempValid &&
         temperatureC < TEMP_LIMIT_C &&
         !overload;
}

void resetOldTransitEvent() {
  transitActive = false;
  transitTimeout = false;
  fastTransit = false;
  transitSeconds = 0;
  speedMps = 0;
  transitStartMs = 0;

  stableIR1 = digitalRead(IR1_PIN);
  stableIR2 = digitalRead(IR2_PIN);
  rawIR1 = stableIR1;
  rawIR2 = stableIR2;

  ir1ChangedMs = millis();
  ir2ChangedMs = millis();
}

void restartMotor() {
  resetOldTransitEvent();

  if (!hardSafetyOK()) {
    stopMotor("SAFETY_INTERLOCK");
    return;
  }

  manualStop = false;
  faultLatched = false;
  fault = "NORMAL";
  motorEnable(true);

  Serial.println("MOTOR,RUN");
}

// ---------------- SERIAL COMMANDS ----------------
void processCommand(String cmd) {
  cmd.trim();
  cmd.toUpperCase();

  if (cmd == "STOP") {
    stopMotor("MANUAL_STOP");
  }
  else if (cmd == "RESTART") {
    restartMotor();
  }
  else if (cmd == "RESET_COUNT") {
    // Intentionally clears persistent counts.
    resetCounts();
  }
  else if (cmd == "TARE") {
    if (scale.is_ready() && !transitActive) {
      scale.tare(25);
      zeroOffset = scale.get_offset();
      hxCalibrated = true;
      Serial.println("TARE,OK");
    } else {
      Serial.println("TARE,FAILED");
    }
  }
}

void readCommands() {
  while (Serial.available()) {
    char c = (char)Serial.read();

    if (c == '\n' || c == '\r') {
      if (commandBuffer.length() > 0) {
        processCommand(commandBuffer);
        commandBuffer = "";
      }
    } else if (commandBuffer.length() < 40) {
      commandBuffer += c;
    }
  }
}

// ---------------- HX711 RAW READING ----------------
void initializeLoadCell() {
  scale.begin(HX_DOUT, HX_SCK);

  unsigned long started = millis();
  while (!scale.is_ready() && millis() - started < 3000) {
    delay(20);
  }

  hxReady = scale.is_ready();

  if (!hxReady) {
    stopMotor("HX711_NOT_READY");
    return;
  }

  // Empty platform required during startup tare.
  scale.tare(25);
  zeroOffset = scale.get_offset();
  hxCalibrated = true;
}

void readLoadCell() {
  if (!scale.is_ready()) {
    hxReady = false;
    stopMotor("HX711_NOT_READY");
    return;
  }

  hxReady = true;
  loadRaw = scale.read();
  loadChange = labs(loadRaw - zeroOffset);

  overload = loadChange > RAW_OVERLOAD_LIMIT;

  if (overload) {
    stopMotor("OVERLOAD");
  }
}

// ---------------- TEMPERATURE ----------------
void readTemperature() {
  tempSensor.requestTemperatures();

  float value = tempSensor.getTempCByIndex(0);

  tempValid =
      value != DEVICE_DISCONNECTED_C &&
      value >= -55.0 &&
      value <= 125.0;

  if (tempValid) {
    temperatureC = value;

    if (temperatureC >= TEMP_LIMIT_C) {
      stopMotor("HIGH_TEMPERATURE");
    }
  } else {
    temperatureC = NAN;
    stopMotor("TEMPERATURE_SENSOR_FAULT");
  }
}

// ---------------- DEBOUNCED IR INPUTS ----------------
bool updateStableIR(
    int pin,
    bool &rawState,
    bool &stableState,
    unsigned long &changedAt
) {
  bool reading = digitalRead(pin);
  unsigned long now = millis();

  if (reading != rawState) {
    rawState = reading;
    changedAt = now;
  }

  if (rawState != stableState &&
      now - changedAt >= IR_DEBOUNCE_MS) {
    bool previous = stableState;
    stableState = rawState;

    // One detection on stable HIGH -> LOW transition.
    return previous == HIGH && stableState == LOW;
  }

  return false;
}

void readIR() {
  bool inletEvent = updateStableIR(
      IR1_PIN, rawIR1, stableIR1, ir1ChangedMs
  );

  bool outletEvent = updateStableIR(
      IR2_PIN, rawIR2, stableIR2, ir2ChangedMs
  );

  if (inletEvent) {
    ir1Count++;
    preferences.putUInt("ir1", ir1Count);

    transitActive = true;
    transitTimeout = false;
    fastTransit = false;
    transitStartMs = millis();
  }

  if (outletEvent) {
    ir2Count++;

    // Only count completed products that have a matching inlet event.
    if (transitActive) {
      unsigned long elapsed = millis() - transitStartMs;

      transitSeconds = elapsed / 1000.0f;
      transitId++;
      overallCount++;

      speedMps = transitSeconds > 0
          ? SENSOR_DISTANCE_M / transitSeconds
          : 0;

      transitActive = false;
      transitTimeout = false;
      fastTransit = false;

      // Persist after each completed product.
      saveCounts();

      if (elapsed < OVERSPEED_MS) {
        fastTransit = true;
        stopMotor("OVERSPEED");
      } else if (elapsed > SLOW_SPEED_MS) {
        stopMotor("SLOW_SPEED");
      }
    } else {
      // Outlet trigger without a matching inlet event.
      stopMotor("IR_SEQUENCE_FAULT");
    }
  }

  if (transitActive &&
      millis() - transitStartMs >= TRANSIT_TIMEOUT_MS) {
    transitActive = false;
    transitTimeout = true;
    stopMotor("TRANSIT_TIMEOUT");
  }
}

// ---------------- TELEMETRY ----------------
void sendData() {
  // DATA,uptime,ir1,ir2,overall,transit_id,transit_s,
  // timeout,fast,load_raw,load_change,overload,temp_c,temp_valid,fault

  Serial.print("DATA,");
  Serial.print(millis()); Serial.print(',');
  Serial.print(ir1Count); Serial.print(',');
  Serial.print(ir2Count); Serial.print(',');
  Serial.print(overallCount); Serial.print(',');
  Serial.print(transitId); Serial.print(',');
  Serial.print(transitSeconds, 3); Serial.print(',');
  Serial.print(transitTimeout ? 1 : 0); Serial.print(',');
  Serial.print(fastTransit ? 1 : 0); Serial.print(',');
  Serial.print(loadRaw); Serial.print(',');
  Serial.print(loadChange); Serial.print(',');
  Serial.print(overload ? 1 : 0); Serial.print(',');

  if (tempValid) Serial.print(temperatureC, 2);
  else Serial.print("nan");

  Serial.print(',');
  Serial.print(tempValid ? 1 : 0); Serial.print(',');
  Serial.println(fault);
}

// ---------------- SETUP ----------------
void setup() {
  Serial.begin(115200);

  pinMode(IR1_PIN, INPUT_PULLUP);
  pinMode(IR2_PIN, INPUT_PULLUP);

  pinMode(STEP_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);
  pinMode(ENA_PIN, OUTPUT);

  digitalWrite(STEP_PIN, LOW);
  digitalWrite(DIR_PIN, HIGH);
  motorEnable(false);

  loadSavedCount();

  tempSensor.begin();
  tempSensor.setWaitForConversion(true);

  initializeLoadCell();
  readTemperature();

  stableIR1 = rawIR1 = digitalRead(IR1_PIN);
  stableIR2 = rawIR2 = digitalRead(IR2_PIN);

  if (hardSafetyOK()) {
    fault = "READY";
  } else {
    stopMotor("STARTUP_SAFETY_CHECK");
  }

  sendData();
}

// ---------------- LOOP ----------------
void loop() {
  readCommands();

  unsigned long now = millis();

  if (now - lastLoadMs >= 100) {
    lastLoadMs = now;
    readLoadCell();
  }

  if (now - lastTempMs >= 1000) {
    lastTempMs = now;
    readTemperature();
  }

  readIR();

  if (!hardSafetyOK()) {
    motorEnable(false);
    manualStop = true;
    faultLatched = true;
  }

  if (!manualStop && !faultLatched && hardSafetyOK()) {
    if (micros() - lastStepUs >= STEP_INTERVAL_US) {
      lastStepUs = micros();
      digitalWrite(STEP_PIN, !digitalRead(STEP_PIN));
    }

    motorEnable(true);
  } else {
    digitalWrite(STEP_PIN, LOW);
    motorEnable(false);
  }

  if (now - lastSendMs >= 500) {
    lastSendMs = now;
    sendData();
  }
}
