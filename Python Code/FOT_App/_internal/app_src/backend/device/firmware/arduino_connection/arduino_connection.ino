// FOT inline device - Arduino Uno R4 WiFi
// Timer-driven DAC guarantees the excitation frequency even when
// the USB-CDC serial link stalls.
// Serial: START, STOP, STATUS
// Config: CONFIG,EXCITATION_HZ=6,SAMPLE_HZ=420,DAC_AMPL=200
//          CONFIG,FLOW_CAL=0|1, CONFIG,EXCITE=0|1

#include <FspTimer.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

const int PIN_A = A1;
const int PIN_B = A2;
const float ADC_MAX = 16383.0f;
const float FS_A_CMH2O = 25.4f;
const float FS_B_CMH2O = 5.08f;
const float OFFSET_A_CMH2O = 0.00f;
const float OFFSET_B_CMH2O = 0.04999f;
const float FLOW_GAIN_POS = 2.86f;
const float FLOW_GAIN_NEG = 2.8428f;

const int N_LUT = 70;
const uint16_t DAC_BIAS = 2048;
const uint32_t SAFETY_MAX_MS = 20UL * 60UL * 1000UL;

float excitationHz = 5.0f;
float sampleHz = excitationHz * N_LUT;
uint16_t dacAmpl = 200;

volatile bool recording = false;
volatile bool flowCal = false;
volatile bool exciteEnabled = true;
volatile uint16_t lutIndex = 0;

uint16_t sineLut[N_LUT];
uint32_t recordingStartedMs = 0;

// Batched transmission buffer.
// Blasting 420 individual micro-packets per second over USB-CDC causes
// TinyUSB endpoint stalls and Electron/Windows host UI lag.
// Batching samples into ~120-200 byte chunks reduces USB transaction
// overhead by 6x while delivering full 420 Hz data smoothly to Python.
char txBatchBuf[256];
uint16_t txBatchLen = 0;

char cmdBuf[96];
uint8_t cmdLen = 0;

void flushTxBatch() {
  if (txBatchLen > 0) {
    // Always empty the buffer. If no host is connected the batch is dropped;
    // keeping it would let appendTxBatch() memcpy past the end of txBatchBuf
    // and crash the board (seen on macOS as "Device not configured").
    if (Serial) {
      Serial.write((const uint8_t *)txBatchBuf, txBatchLen);
    }
    txBatchLen = 0;
  }
}

void appendTxBatch(const char *str, int len) {
  if (len <= 0) return;
  if ((uint16_t)(txBatchLen + len) >= sizeof(txBatchBuf)) {
    flushTxBatch();
  }
  memcpy(txBatchBuf + txBatchLen, str, len);
  txBatchLen += len;
  // Flush when we reach ~128 bytes (about 6 samples) to keep latency low (<15ms)
  if (txBatchLen >= 128) {
    flushTxBatch();
  }
}

// ── Hardware-timer-driven DAC ────────────────────────────────
// The timer ISR fires at exactly sampleHz and writes the next
// sine-LUT value to the DAC register.  Because it runs in an
// interrupt, Serial.write() stalls in loop() can never delay or
// stop the excitation waveform.
FspTimer sampleTimer;
bool timerConfigured = false;

volatile bool sampleDue = false; // ISR → loop flag
volatile uint16_t isrPhase = 0;  // LUT index the ISR just output

void dacISR(timer_callback_args_t __attribute__((unused)) * p_args) {
  if (!recording)
    return;

  // Safety failsafe inside the ISR — works even when loop() is
  // stuck inside a blocking Serial.write().
  if ((uint32_t)(millis() - recordingStartedMs) > SAFETY_MAX_MS) {
    recording = false;
    R_DAC->DADR[0] = DAC_BIAS;
    return;
  }

  uint16_t idx = lutIndex;
  lutIndex = (lutIndex + 1) % N_LUT;

  if (exciteEnabled && !flowCal) {
    R_DAC->DADR[0] = sineLut[idx];
  } else {
    R_DAC->DADR[0] = DAC_BIAS;
  }

  isrPhase = idx;
  sampleDue = true;
}

bool configureSampleTimer(float hz) {
  if (timerConfigured) {
    sampleTimer.stop();
    sampleTimer.close();
    timerConfigured = false;
  }

  uint8_t type = GPT_TIMER;
  int8_t ch = FspTimer::get_available_timer(type);
  if (ch < 0) {
    ch = FspTimer::get_available_timer(type, true);
  }
  if (ch < 0) {
    type = AGT_TIMER;
    ch = FspTimer::get_available_timer(type);
  }
  if (ch < 0)
    return false;

  FspTimer::force_use_of_pwm_reserved_timer();
  if (!sampleTimer.begin(TIMER_MODE_PERIODIC, type, ch, hz, 0.0f, dacISR))
    return false;
  if (!sampleTimer.setup_overflow_irq())
    return false;
  if (!sampleTimer.open())
    return false;
  if (!sampleTimer.start())
    return false;
  timerConfigured = true;
  return true;
}

void stopSampleTimer() {
  if (!timerConfigured)
    return;
  sampleTimer.stop();
  sampleTimer.close();
  timerConfigured = false;
}
// ── end timer ────────────────────────────────────────────────

void buildLut(uint16_t amplitude) {
  for (int i = 0; i < N_LUT; i++) {
    float sine = sinf(2.0f * PI * i / N_LUT);
    sineLut[i] = DAC_BIAS + (int)(amplitude * sine);
  }
}

void setAmplitude(uint16_t amplitude) {
  uint16_t next[N_LUT];
  for (int i = 0; i < N_LUT; i++) {
    float sine = sinf(2.0f * PI * i / N_LUT);
    next[i] = DAC_BIAS + (int)(amplitude * sine);
  }

  noInterrupts();
  memcpy(sineLut, next, sizeof(sineLut));
  dacAmpl = amplitude;
  interrupts();
}

float toCmH2O(uint16_t count, float fullScale) {
  float ratio = count / ADC_MAX;
  return ((ratio - 0.10f) / 0.80f) * (2.0f * fullScale) - fullScale;
}

float flowFor(float differentialPressure) {
  float gain = differentialPressure >= 0.0f ? FLOW_GAIN_POS : FLOW_GAIN_NEG;
  return gain * differentialPressure;
}

void printHeader() {
  Serial.println(flowCal ? "idx,pressure_cmH2O,flow_dp_cmH2O"
                         : "idx,pressure_cmH2O,flow_Lps");
}

void startRecording() {
  noInterrupts();
  lutIndex = 0;
  sampleDue = false;
  recording = true;
  interrupts();

  txBatchLen = 0;
  recordingStartedMs = millis();

  Serial.println("# STARTED");
  if (flowCal) {
    Serial.println("# EXCITATION OFF (flow calibration mode)");
  } else if (!exciteEnabled) {
    Serial.println("# EXCITATION OFF (disarmed)");
  } else {
    Serial.println("# EXCITATION ON");
  }
  printHeader();
  Serial.println("# FIRST_SAMPLE_PENDING");
}

void stopRecording() {
  noInterrupts();
  recording = false;
  sampleDue = false;
  interrupts();

  R_DAC->DADR[0] = DAC_BIAS;
  flushTxBatch();
  Serial.println("# STOPPED");
}

bool allowedFrequency(int hz) {
  return hz == 5 || hz == 6 || hz == 11 || hz == 19;
}

void setFrequency(int hz) {
  if (!allowedFrequency(hz)) {
    Serial.print("# CONFIG_REJECTED frequency allowed 5/6/11/19, got: ");
    Serial.println(hz);
    return;
  }

  excitationHz = (float)hz;
  sampleHz = excitationHz * N_LUT;
  noInterrupts();
  lutIndex = 0;
  sampleDue = false;
  interrupts();

  if (timerConfigured) {
    sampleTimer.set_frequency(sampleHz);
  }

  Serial.print("# excitation Hz set to: ");
  Serial.println(excitationHz, 3);
  Serial.print("# sample Hz: ");
  Serial.println(sampleHz, 3);
  Serial.print("# DAC amplitude (counts): ");
  Serial.println(dacAmpl);
}

void upper(char *text) {
  for (; *text; text++) {
    if (*text >= 'a' && *text <= 'z') {
      *text -= 'a' - 'A';
    }
  }
}

char *trim(char *text) {
  while (*text == ' ' || *text == '\t') {
    text++;
  }
  return text;
}

void handleConfig(char *command) {
  int hz = -1;
  int amplitude = -1;
  int flowMode = -1;
  int excite = -1;

  char *token = strtok(command, ",");
  while (token) {
    token = trim(token);
    if (strncmp(token, "EXCITATION_HZ=", 14) == 0 ||
        strncmp(token, "FREQUENCY_HZ=", 13) == 0) {
      hz = atoi(strchr(token, '=') + 1);
    } else if (strncmp(token, "DAC_AMPL=", 9) == 0) {
      amplitude = atoi(token + 9);
    } else if (strncmp(token, "FLOW_CAL=", 9) == 0) {
      flowMode = atoi(token + 9) != 0;
    } else if (strncmp(token, "EXCITE=", 7) == 0) {
      excite = atoi(token + 7) != 0;
    }
    token = strtok(NULL, ",");
  }

  if (flowMode >= 0) {
    bool changed = flowCal != (bool)flowMode;
    noInterrupts();
    flowCal = flowMode;
    interrupts();
    Serial.print("# FLOW_CAL ");
    Serial.println(flowCal ? "ON (speaker disabled)" : "OFF");
    if (changed && recording) {
      printHeader();
    }
  }

  if (excite >= 0) {
    noInterrupts();
    exciteEnabled = excite;
    interrupts();
    Serial.print("# EXCITE ");
    Serial.println(exciteEnabled ? "ARMED" : "DISARMED (speaker silent)");
  }

  if (hz > 0) {
    setFrequency(hz);
  } else if (amplitude > 0) {
    setAmplitude((uint16_t)amplitude);
    Serial.print("# DAC amplitude (counts): ");
    Serial.println(dacAmpl);
  }
  Serial.println("# CONFIG_ACK");
}

void handleCommand(char *command) {
  upper(command);
  char *trimmed = trim(command);
  if (!*trimmed) {
    return;
  }

  if (strcmp(trimmed, "START") == 0) {
    startRecording();
  } else if (strcmp(trimmed, "STOP") == 0) {
    stopRecording();
  } else if (strcmp(trimmed, "STATUS") == 0) {
    Serial.println(recording ? "# STATUS_RECORDING" : "# STATUS_IDLE");
  } else if (strcmp(trimmed, "ZERO") == 0) {
    Serial.println("# ZERO_ACK_NOT_IMPLEMENTED");
  } else if (strcmp(trimmed, "CALIBRATE") == 0) {
    Serial.println("# CALIBRATE_ACK_NOT_IMPLEMENTED");
  } else if (strncmp(trimmed, "CONFIG", 6) == 0) {
    handleConfig(trimmed);
  } else if (*trimmed == 'F') {
    int hz = atoi(trimmed + 1);
    if (allowedFrequency(hz)) {
      setFrequency(hz);
    } else {
      Serial.print("# ignored frequency (allowed 5/6/11/19): ");
      Serial.println(trimmed);
    }
  } else {
    Serial.print("# UNKNOWN_COMMAND ");
    Serial.println(trimmed);
  }
}

void readSerialCommands() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (cmdLen) {
        cmdBuf[cmdLen] = '\0';
        handleCommand(cmdBuf);
        cmdLen = 0;
      }
    } else if (cmdLen < sizeof(cmdBuf) - 1) {
      cmdBuf[cmdLen++] = c;
    } else {
      cmdLen = 0;
    }
  }
}

void checkSpeakerFailsafe() {
  // The timer ISR also checks the safety limit so the DAC stops
  // even when loop() is blocked on serial I/O.  This check handles
  // the Serial.println notification and clean timer shutdown.
  if (recording && (uint32_t)(millis() - recordingStartedMs) > SAFETY_MAX_MS) {
    stopRecording();
    Serial.print("# AUTO_STOP excitation ran past the ");
    Serial.print(SAFETY_MAX_MS / 1000UL);
    Serial.println(" s cap");
  }
}

void setup() {
  Serial.begin(115200);
  analogReadResolution(14);
  analogWriteResolution(12);

  buildLut(dacAmpl);
  analogWrite(DAC, DAC_BIAS);

  if (!configureSampleTimer(sampleHz)) {
    Serial.println("# ERROR: could not initialize hardware timer");
  }

  Serial.println("# READY");
  Serial.print("# excitation Hz: ");
  Serial.println(excitationHz, 3);
  Serial.print("# sample Hz: ");
  Serial.println(sampleHz, 3);
  Serial.print("# LUT samples per cycle: ");
  Serial.println(N_LUT);
  Serial.print("# DAC amplitude (counts): ");
  Serial.println(dacAmpl);

  Serial.println("# waiting for START command");
}

void loop() {
  readSerialCommands();
  checkSpeakerFailsafe();

  if (!recording || !sampleDue) {
    return;
  }
  sampleDue = false;

  // Grab the LUT phase the ISR just output on the DAC.
  uint16_t phase = isrPhase;

  // ADC reads happen here in the main loop, as close to the ISR
  // trigger as possible.  If this loop iteration was delayed by a
  // slow Serial.write(), the readings reflect the current airway
  // state (still valid for FOT) and the phase tag stays correct.
  uint16_t a = analogRead(PIN_A);
  uint16_t b = analogRead(PIN_B);

  float pressure = toCmH2O(a, FS_A_CMH2O) - OFFSET_A_CMH2O;
  float differentialPressure = toCmH2O(b, FS_B_CMH2O) - OFFSET_B_CMH2O;
  float output = flowCal ? differentialPressure : flowFor(differentialPressure);

  char line[64];
  int n = snprintf(line, sizeof(line), "%u,%.4f,%.4f\n", (unsigned)phase,
                   pressure, output);
  if (n > 0) {
    appendTxBatch(line, n);
  }
}
