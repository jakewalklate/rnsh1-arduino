// ============================================================================
// FOT inline device - Arduino Uno R4 WiFi (RA4M1)
// UI-controlled dual-channel acquisition + phase-locked DAC excitation.
//
// Serial commands:
//   START
//     Begin speaker excitation and CSV streaming.
//
//   STOP
//     Stop speaker excitation and CSV streaming.
//
//   STATUS
//     Print STATUS_RECORDING or STATUS_IDLE.
//
//   CONFIG,EXCITATION_HZ=6
//   CONFIG,EXCITATION_HZ=6,SAMPLE_HZ=420
//   CONFIG,DAC_AMPL=475
//     Configure excitation before START. SAMPLE_HZ is accepted for UI
//     compatibility, but the firmware always derives sample rate as:
//       SAMPLE_HZ = EXCITATION_HZ * N_LUT
//
//   F5 / F6 / F11 / F19
//     Shortcut frequency commands kept for bench testing.
//
// CSV stream while recording:
//   idx,pressure_cmH2O,flow_Lps
// ============================================================================

#include "FspTimer.h"
#include <math.h>
#include <stdlib.h>
#include <string.h>

// ----------------------------- CONFIG --------------------------------------
const int PIN_A = A1;          // +-10 inH2O (pressure)
const int PIN_B = A2;          // +-2 inH2O (flow)
const float ADC_MAX = 16383.0; // 14-bit
const float FS_A_CMH2O = 25.4; // +-10 inH2O
const float FS_B_CMH2O = 5.08; // +-2 inH2O
const float MANUAL_OFFSET_A_CMH2O = 0.00;
const float MANUAL_OFFSET_B_CMH2O = 0.04999;

// New sensor tube calibration.
const float FLOW_GAIN_POS = 2.86f;   // L/(s*cmH2O), push (dp >= 0)
const float FLOW_GAIN_NEG = 2.8428f; // L/(s*cmH2O), pull (dp < 0)

const bool FLOW_CALIBRATION_MODE = false;

float EXCITATION_HZ = 6.0; // boot default; UI CONFIG can change it
const int N_LUT = 70;
float SAMPLE_HZ = EXCITATION_HZ * (float)N_LUT;
const uint16_t DAC_BIAS = 2048;
uint16_t g_dacAmpl = 200;

// ----------------------------- GLOBALS -------------------------------------
volatile uint16_t g_countA = 0;
volatile uint16_t g_countB = 0;
volatile uint16_t g_idx = 0;
volatile bool g_newSample = false;
volatile bool g_recording = false;

uint16_t sineLUT[N_LUT];
volatile uint16_t lutIndex = 0;

FspTimer g_timer;

char cmdBuf[128];
uint8_t cmdLen = 0;

// ----------------------------- LUT -----------------------------------------
uint16_t dacAmplFor(float exHz) {
  int hz = (int)(exHz + 0.5f);

  switch (hz) {
  case 5:
    return 475;
  case 6:
    return 400;
  case 11:
    return 200;
  case 19:
    return 200;
  default:
    return 200;
  }
}

void buildLUT(uint16_t ampl) {
  for (int n = 0; n < N_LUT; n++) {
    float s = sinf(2.0f * PI * (float)n / (float)N_LUT);
    sineLUT[n] = (uint16_t)((float)DAC_BIAS + (float)ampl * s);
  }
}

void applyAmplitude(uint16_t ampl) {
  uint16_t tmpLUT[N_LUT];

  for (int n = 0; n < N_LUT; n++) {
    float s = sinf(2.0f * PI * (float)n / (float)N_LUT);
    tmpLUT[n] = (uint16_t)((float)DAC_BIAS + (float)ampl * s);
  }

  noInterrupts();
  memcpy((void *)sineLUT, tmpLUT, sizeof(tmpLUT));
  g_dacAmpl = ampl;
  interrupts();
}

// ----------------------------- ISR -----------------------------------------
void onTimerTick(timer_callback_args_t *p_args) {
  if (g_recording && !FLOW_CALIBRATION_MODE) {
    R_DAC->DADR[0] = sineLUT[lutIndex];
  } else {
    R_DAC->DADR[0] = DAC_BIAS;
  }

  if (!g_recording) {
    return;
  }

  g_countA = analogRead(PIN_A);
  g_countB = analogRead(PIN_B);
  g_idx = lutIndex;
  g_newSample = true;
  lutIndex = (lutIndex + 1) % N_LUT;
}

// ----------------------------- TIMER ---------------------------------------
bool startSampleTimer(float hz) {
  uint8_t type = GPT_TIMER;
  int8_t idx = FspTimer::get_available_timer(type);
  if (idx < 0) {
    idx = FspTimer::get_available_timer(type, true);
  }
  if (idx < 0) {
    return false;
  }
  FspTimer::force_use_of_pwm_reserved_timer();
  if (!g_timer.begin(TIMER_MODE_PERIODIC, type, idx, hz, 0.0f, onTimerTick))
    return false;
  if (!g_timer.setup_overflow_irq())
    return false;
  if (!g_timer.open())
    return false;
  if (!g_timer.start())
    return false;
  return true;
}

// ------------------------- FREQUENCY SWITCH --------------------------------
bool isAllowedFrequency(int hz) {
  return hz == 5 || hz == 6 || hz == 11 || hz == 19;
}

void applyFrequency(float exHz, int customAmplitude) {
  EXCITATION_HZ = exHz;
  SAMPLE_HZ = EXCITATION_HZ * (float)N_LUT;

  uint16_t nextAmpl =
      customAmplitude > 0 ? (uint16_t)customAmplitude : dacAmplFor(exHz);

  applyAmplitude(nextAmpl);

  noInterrupts();
  lutIndex = 0;
  g_newSample = false;
  interrupts();

  if (!g_timer.set_frequency(SAMPLE_HZ)) {
    Serial.println("# ERROR: set_frequency failed");
    return;
  }

  Serial.print("# excitation Hz set to: ");
  Serial.println(EXCITATION_HZ, 3);
  Serial.print("# sample Hz: ");
  Serial.println(SAMPLE_HZ, 3);
  Serial.print("# DAC amplitude (counts): ");
  Serial.println(g_dacAmpl);
}

// ----------------------------- SCALING -------------------------------------
float toCmH2O(float count, float fs) {
  float ratio = count / ADC_MAX;
  return ((ratio - 0.10) / 0.80) * (2.0 * fs) - fs;
}

float flowFromDifferentialPressure(float dpCmH2O) {
  float gain = (dpCmH2O >= 0.0f) ? FLOW_GAIN_POS : FLOW_GAIN_NEG;
  return gain * dpCmH2O;
}

// ----------------------------- COMMANDS ------------------------------------
void printHeader() {
  if (FLOW_CALIBRATION_MODE) {
    Serial.println("idx,pressure_cmH2O,flow_dp_cmH2O");
  } else {
    Serial.println("idx,pressure_cmH2O,flow_Lps");
  }
}

void startRecording() {
  noInterrupts();
  lutIndex = 0;
  g_newSample = false;
  g_recording = true;
  interrupts();

  Serial.println("# STARTED");
  printHeader();
}

void stopRecording() {
  noInterrupts();
  g_recording = false;
  g_newSample = false;
  interrupts();

  R_DAC->DADR[0] = DAC_BIAS;
  Serial.println("# STOPPED");
}

void uppercaseInPlace(char *text) {
  for (uint8_t i = 0; text[i] != '\0'; i++) {
    if (text[i] >= 'a' && text[i] <= 'z') {
      text[i] = text[i] - 32;
    }
  }
}

void trimLeadingSpaces(char **text) {
  while (**text == ' ' || **text == '\t') {
    (*text)++;
  }
}

void handleConfig(char *command) {
  int requestedHz = -1;
  int requestedAmpl = -1;

  char *token = strtok(command, ",");

  while (token != NULL) {
    trimLeadingSpaces(&token);

    if (strncmp(token, "EXCITATION_HZ=", 14) == 0 ||
        strncmp(token, "FREQUENCY_HZ=", 13) == 0) {
      char *value = strchr(token, '=');
      requestedHz = atoi(value + 1);
    } else if (strncmp(token, "DAC_AMPL=", 9) == 0) {
      requestedAmpl = atoi(token + 9);
    } else if (strncmp(token, "SAMPLE_HZ=", 10) == 0) {
      // Accepted for UI compatibility. The firmware derives SAMPLE_HZ.
    } else if (strncmp(token, "CPAP_CM_H2O=", 12) == 0) {
      // Accepted for UI compatibility. CPAP is metadata for the Python side.
    }

    token = strtok(NULL, ",");
  }

  if (requestedHz > 0) {
    if (!isAllowedFrequency(requestedHz)) {
      Serial.print("# CONFIG_REJECTED frequency allowed 5/6/11/19, got: ");
      Serial.println(requestedHz);
      return;
    }

    applyFrequency((float)requestedHz, requestedAmpl);
  } else if (requestedAmpl > 0) {
    applyAmplitude((uint16_t)requestedAmpl);
    Serial.print("# DAC amplitude (counts): ");
    Serial.println(g_dacAmpl);
  }

  Serial.println("# CONFIG_ACK");
}

void handleFrequencyShortcut(const char *command) {
  int hz = atoi(command + 1);

  if (isAllowedFrequency(hz)) {
    applyFrequency((float)hz, -1);
  } else {
    Serial.print("# ignored frequency (allowed 5/6/11/19): ");
    Serial.println(command);
  }
}

void handleCommand(char *command) {
  uppercaseInPlace(command);
  char *trimmed = command;
  trimLeadingSpaces(&trimmed);

  if (strlen(trimmed) == 0) {
    return;
  }

  if (strcmp(trimmed, "START") == 0) {
    startRecording();
  } else if (strcmp(trimmed, "STOP") == 0) {
    stopRecording();
  } else if (strcmp(trimmed, "STATUS") == 0) {
    Serial.println(g_recording ? "# STATUS_RECORDING" : "# STATUS_IDLE");
  } else if (strcmp(trimmed, "ZERO") == 0) {
    Serial.println("# ZERO_ACK_NOT_IMPLEMENTED");
  } else if (strcmp(trimmed, "CALIBRATE") == 0) {
    Serial.println("# CALIBRATE_ACK_NOT_IMPLEMENTED");
  } else if (strncmp(trimmed, "CONFIG", 6) == 0) {
    handleConfig(trimmed);
  } else if (trimmed[0] == 'F') {
    handleFrequencyShortcut(trimmed);
  } else {
    Serial.print("# UNKNOWN_COMMAND ");
    Serial.println(trimmed);
  }
}

void readSerialCommands() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();

    if (c == '\n' || c == '\r') {
      if (cmdLen > 0) {
        cmdBuf[cmdLen] = '\0';
        handleCommand(cmdBuf);
        cmdLen = 0;
      }
    } else if (cmdLen < (uint8_t)(sizeof(cmdBuf) - 1)) {
      cmdBuf[cmdLen++] = c;
    }
  }
}

// ----------------------------- SETUP ---------------------------------------
void setup() {
  Serial.begin(115200);
  while (!Serial) {
    ;
  }

  analogReadResolution(14);
  analogWriteResolution(12);

  g_dacAmpl = dacAmplFor(EXCITATION_HZ);
  buildLUT(g_dacAmpl);
  analogWrite(DAC, DAC_BIAS);

  Serial.println("# READY");
  Serial.println("# A1 and A2 use fixed manual offsets...");
  Serial.print("# excitation Hz: ");
  Serial.println(EXCITATION_HZ, 3);
  Serial.print("# sample Hz: ");
  Serial.println(SAMPLE_HZ, 3);
  Serial.print("# LUT samples per cycle: ");
  Serial.println(N_LUT);
  Serial.print("# DAC amplitude (counts): ");
  Serial.println(g_dacAmpl);
  Serial.print("# manual offset A (cmH2O): ");
  Serial.println(MANUAL_OFFSET_A_CMH2O, 4);
  Serial.print("# manual offset B (cmH2O): ");
  Serial.println(MANUAL_OFFSET_B_CMH2O, 4);
  Serial.print("# flow calibration mode: ");
  Serial.println(FLOW_CALIBRATION_MODE ? "ON (speaker disabled)" : "OFF");
  Serial.print("# flow gain push: ");
  Serial.print(FLOW_GAIN_POS, 6);
  Serial.print("  pull: ");
  Serial.println(FLOW_GAIN_NEG, 6);
  Serial.println("# waiting for START command");

  if (!startSampleTimer(SAMPLE_HZ)) {
    Serial.println("ERROR: could not start timer");
    while (1) {
      ;
    }
  }
}

// ----------------------------- LOOP ----------------------------------------
void loop() {
  readSerialCommands();

  if (!g_recording || !g_newSample) {
    return;
  }

  noInterrupts();
  uint16_t cA = g_countA;
  uint16_t cB = g_countB;
  uint16_t idx = g_idx;
  g_newSample = false;
  interrupts();

  float pA = toCmH2O(cA, FS_A_CMH2O) - MANUAL_OFFSET_A_CMH2O;
  float dpB = toCmH2O(cB, FS_B_CMH2O) - MANUAL_OFFSET_B_CMH2O;
  float outputB =
      FLOW_CALIBRATION_MODE ? dpB : flowFromDifferentialPressure(dpB);

  Serial.print(idx);
  Serial.print(',');
  Serial.print(pA, 4);
  Serial.print(',');
  Serial.println(outputB, 4);
}
