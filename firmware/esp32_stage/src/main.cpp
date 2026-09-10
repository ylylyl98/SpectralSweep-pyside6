#include <Arduino.h>
#include <Preferences.h>
#include <cmath>
#include <cstdlib>
#include <cstring>

// ESP32-S3 + TB6600, T6x1 lead screw. The configured driver setting is the
// electrical pulse count per revolution and therefore pulses/mm for this lead.
constexpr int STEP_PIN = 4, DIR_PIN = 5;
constexpr int32_t MIN_PPR = 200, MAX_TRAVEL_MM = 100;
constexpr float ACCEL = 2000.0f;
struct DeviceConfig {
  uint32_t magic;
  uint16_t version;
  uint16_t pulsesPerRev;
  uint8_t directionAwayLevel;
  uint8_t reserved[3];
  int32_t limit;
  uint16_t frequencyHz;
};
constexpr uint32_t CONFIG_MAGIC = 0x53544732; // STG2
constexpr uint16_t CONFIG_VERSION = 1;
constexpr size_t CONFIG_BYTES = sizeof(DeviceConfig);

Preferences prefs;
DeviceConfig config{CONFIG_MAGIC, CONFIG_VERSION, 200, 1, {0, 0, 0}, 0, 100};
bool homed = false, moving = false, storageOK = false;
int32_t position = 0, target = 0, traveled = 0;
uint32_t nextPulse = 0, lastContact = 0, quietSince = 0;
bool cooling = false;
char line[80]; size_t used = 0;

int32_t stepsPerMm() { return (int32_t)config.pulsesPerRev; }
int32_t maxSteps() { return MAX_TRAVEL_MM * stepsPerMm(); }

bool saveConfig(const DeviceConfig& value) {
  if (!storageOK) return false;
  return prefs.putBytes("config", &value, CONFIG_BYTES) == CONFIG_BYTES;
}
bool saveConfig() { return saveConfig(config); }

void status(const char* message = "OK") {
  Serial.printf(
    "{\"protocol\":\"stage-v2\",\"capabilities\":[\"scale\",\"direction\",\"maximum\"],\"homed\":%s,\"moving\":%s,\"steps\":%ld,\"limit\":%ld,\"stepsPerMm\":%ld,\"pulsesPerRev\":%ld,\"directionAwayLevel\":%s,\"frequencyHz\":%ld,\"message\":\"%s\"}\n",
    homed ? "true" : "false", moving ? "true" : "false", (long)position,
    (long)config.limit, (long)stepsPerMm(), (long)config.pulsesPerRev,
    config.directionAwayLevel ? "true" : "false", (long)config.frequencyHz, message);
}

void stopMotion(const char* reason) {
  moving = false; homed = false; digitalWrite(STEP_PIN, LOW);
  used = 0; cooling = true; quietSince = millis(); status(reason);
}

void startMove(int32_t dest) {
  if (dest == position) { status("Already at target"); return; }
  target = dest; traveled = 0; moving = true;
  bool away = target > position;
  digitalWrite(DIR_PIN, away == (config.directionAwayLevel != 0) ? HIGH : LOW);
  nextPulse = micros() + 10000; status("Moving");
}

void clearReference() { moving = false; homed = false; position = 0; digitalWrite(STEP_PIN, LOW); status("Reference cleared"); }

void execute(char* input) {
  if (!strcmp(input, "status") || !strcmp(input, "h")) { lastContact = millis(); status(); return; }
  if (!strcmp(input, "invalidate") || !strcmp(input, "clear_reference")) { if (moving || cooling) { status("Busy - command discarded"); return; } lastContact = millis(); clearReference(); return; }
  if (!strcmp(input, "stop")) { lastContact = millis(); stopMotion("Stopped - position untrusted"); return; }
  if (moving || cooling) { status("Busy - command discarded"); return; }
  lastContact = millis();
  if (!strcmp(input, "zero")) { position = 0; homed = true; status("Manual zero set"); return; }
  if (!strcmp(input, "home")) { if (!homed || config.limit <= 0) { status("Set zero and maximum first"); return; } startMove(0); return; }
  if (!strcmp(input, "calibrate")) {
    DeviceConfig next = config; next.limit = 0;
    if (!saveConfig(next)) { status("Could not clear saved maximum"); return; }
    config = next; homed = false; position = 0;
    status("Calibration: jog to reference then set zero"); return;
  }
  char* space = strchr(input, ' ');
  if (!space) {
    if (!strcmp(input, "setmax")) {
      if (!homed || position <= 0) { status("Maximum save requires a referenced position above zero"); return; }
      DeviceConfig next = config; next.limit = position;
      if (!saveConfig(next)) { status("Could not save maximum"); return; }
      config = next;
      status("Maximum saved"); return;
    }
    status("Unknown command"); return;
  }
  *space = 0; char* end = nullptr;
  float value = strtof(space + 1, &end);
  if (end == space + 1 || *end != 0 || !std::isfinite(value)) { status("Invalid setting"); return; }
  if (!strcmp(input, "frequency")) {
    if (value < 100 || value > 2000 || floorf(value) != value) { status("Frequency must be an integer from 100 to 2000 Hz"); return; }
    DeviceConfig next = config; next.frequencyHz = (uint16_t)value;
    if (!saveConfig(next)) { status("Could not save frequency"); return; }
    config = next;
    status("Frequency updated"); return;
  }
  if (!strcmp(input, "scale") || !strcmp(input, "ppr")) {
    if (value != 200 && value != 800 && value != 1600 && value != 3200) { status("Scale must be 200, 800, 1600, or 3200 pulses/rev"); return; }
    DeviceConfig next = config; next.pulsesPerRev = (uint16_t)value; next.limit = 0;
    if (!saveConfig(next)) { homed = false; position = 0; config.limit = 0; status("Could not save scale - reference cleared"); return; }
    config = next; homed = false; position = 0;
    status("Scale updated"); return;
  }
  if (!strcmp(input, "direction")) {
    if (value != 0 && value != 1) { status("Direction must be GPIO level 0 or 1"); return; }
    DeviceConfig next = config; next.directionAwayLevel = (uint8_t)value; next.limit = 0;
    if (!saveConfig(next)) { homed = false; position = 0; config.limit = 0; status("Could not save direction - reference cleared"); return; }
    config = next; homed = false; position = 0;
    status("Direction updated"); return;
  }
  if (!strcmp(input, "setmax")) {
    if (!homed || value <= 0 || value > MAX_TRAVEL_MM || lroundf(value * stepsPerMm()) <= 0 || value * stepsPerMm() < position) { status("Maximum must be 0 < mm <= 100 and at least current position"); return; }
    DeviceConfig next = config; next.limit = (int32_t)lroundf(value * stepsPerMm());
    if (!saveConfig(next)) { status("Could not save maximum"); return; }
    config = next;
    status("Maximum saved"); return;
  }
  if (fabsf(value) > 100) { status("Invalid distance"); return; }
  bool jog = !strcmp(input, "jog"), relative = !strcmp(input, "move"), absolute = !strcmp(input, "goto");
  if (!jog && !relative && !absolute) { status("Unknown command"); return; }
  int32_t steps = (int32_t)lroundf(value * stepsPerMm());
  if (jog && (fabsf(value) > 10.0f || steps == 0)) { status("Jog must be nonzero and at most 10 mm"); return; }
  if (!homed && !jog) { status("Set zero before normal moves"); return; }
  if (homed && config.limit <= 0) { status("Set maximum distance before moving away"); return; }
  int32_t dest = absolute ? steps : position + steps;
  if (homed && (dest < 0 || dest > config.limit)) { status("Outside software travel limits"); return; }
  if (!homed) { position = 0; dest = steps; } // supervised, finite reference jog
  startMove(dest);
}

void setup() {
  pinMode(STEP_PIN, OUTPUT); pinMode(DIR_PIN, OUTPUT); digitalWrite(STEP_PIN, LOW); digitalWrite(DIR_PIN, LOW);
  Serial.begin(115200); storageOK = prefs.begin("stage", false);
  DeviceConfig saved{};
  if (storageOK && prefs.getBytes("config", &saved, CONFIG_BYTES) == CONFIG_BYTES && saved.magic == CONFIG_MAGIC && saved.version == CONFIG_VERSION &&
      (saved.pulsesPerRev == 200 || saved.pulsesPerRev == 800 || saved.pulsesPerRev == 1600 || saved.pulsesPerRev == 3200) && saved.directionAwayLevel <= 1 && saved.frequencyHz >= 100 && saved.frequencyHz <= 2000 && saved.limit >= 0 && saved.limit <= 100 * saved.pulsesPerRev) config = saved;
  else { config = {CONFIG_MAGIC, CONFIG_VERSION, 200, 1, {0, 0, 0}, 0, 100}; if (storageOK) saveConfig(); }
  homed = false; moving = false; position = 0; used = 0; cooling = false; lastContact = millis(); status("Ready - manual zero required");
}

void loop() {
  for (int n = 0; n < 96 && Serial.available(); ++n) {
    char c = (char)Serial.read();
    if (c == '!') { stopMotion("Stopped - position untrusted"); continue; }
    if (cooling) quietSince = millis();
    if (c == '\r') continue;
    if (c == '\n') { line[used] = 0; if (used) execute(line); used = 0; }
    else if (used < sizeof(line) - 1 && c >= 32 && c <= 126) line[used++] = c;
    else { while (Serial.available()) Serial.read(); stopMotion("Malformed or oversized command"); break; }
  }
  if (cooling && millis() - quietSince >= 250) cooling = false;
  if (moving && millis() - lastContact > 2000) { stopMotion("Connection timeout - set zero again"); return; }
  if (!moving || (int32_t)(micros() - nextPulse) < 0) return;
  digitalWrite(STEP_PIN, HIGH); delayMicroseconds(5); digitalWrite(STEP_PIN, LOW);
  position += target > position ? 1 : -1; ++traveled;
  if (position == target) { moving = false; cooling = true; quietSince = millis(); status("Move complete"); return; }
  int32_t remaining = abs(target - position);
  float ramp = sqrtf(10000.0f + 2 * ACCEL * (float)(traveled < remaining ? traveled : remaining));
  float hz = ramp < config.frequencyHz ? ramp : config.frequencyHz;
  nextPulse = micros() + (uint32_t)(1000000.0f / hz);
}
