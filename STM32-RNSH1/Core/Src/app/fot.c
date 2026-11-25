#include "app/fot.h"
#include "app/sensors.h"
#include "app/serial.h"
#include "app/wave.h"

#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---- constants copied from arduino_connection.ino ------------------------ */
static const float FS_A_CMH2O     = 25.4f;
static const float FS_B_CMH2O     = 5.08f;
static const float OFFSET_A_CMH2O = 0.00f;
static const float OFFSET_B_CMH2O = 0.04999f;
static const float FLOW_GAIN_POS  = 2.86f;
static const float FLOW_GAIN_NEG  = 2.8428f;

/* Speaker safety cap, as on the Arduino: a recording that is never stopped
 * (app closed or crashed without sending STOP) silences itself after this. */
#define SAFETY_MAX_MS  (20UL * 60UL * 1000UL)

/* ---- state ---------------------------------------------------------------- */
static bool recording;
static bool flow_cal;              /* speaker silent, stream raw dp          */
static bool excite_enabled = true; /* speaker armed                          */
static uint32_t recording_started_ms;

static bool synced;                /* first row of a recording is idx 0 */

static char    cmd_buf[96];
static uint8_t cmd_len;

#define println(s)  Serial_Printf("%s\r\n", (s))

/* ---- speaker -------------------------------------------------------------- */

/* Same rule as the Arduino: the sine plays only while recording, and not
 * during flow calibration or when the app disarmed it. STOP (sent by the web
 * app on stop and on cancel) silences it at the end of the current cycle. */
static bool speaker_should_play(void)
{
  return recording && excite_enabled && !flow_cal;
}

static void restart_rows(void)
{
  synced = false;                  /* next row sent is idx 0 */
}

static void apply_wave(uint32_t hz)
{
  Wave_Configure(hz, speaker_should_play());
  restart_rows();
}

/* ---- sensors -------------------------------------------------------------- */

static float flow_for(float differential_pressure)
{
  float gain = differential_pressure >= 0.0f ? FLOW_GAIN_POS : FLOW_GAIN_NEG;
  return gain * differential_pressure;
}

/* "%.4f" without float printf (newlib-nano does not include it). */
static int format_fixed4(char *out, size_t size, float value)
{
  int32_t scaled = (int32_t)(value * 10000.0f + (value >= 0.0f ? 0.5f : -0.5f));
  uint32_t mag = (uint32_t)(scaled < 0 ? -scaled : scaled);
  return snprintf(out, size, "%s%lu.%04lu", scaled < 0 ? "-" : "",
                  (unsigned long)(mag / 10000U), (unsigned long)(mag % 10000U));
}

static void send_row(const sensor_row_t *row)
{
  float pressure = Sensors_ToCmH2O(row->a_counts, FS_A_CMH2O) - OFFSET_A_CMH2O;
  float differential_pressure = Sensors_ToCmH2O(row->b_counts, FS_B_CMH2O) - OFFSET_B_CMH2O;
  float output = flow_cal ? differential_pressure : flow_for(differential_pressure);

  char p_text[16];
  char o_text[16];
  format_fixed4(p_text, sizeof(p_text), pressure);
  format_fixed4(o_text, sizeof(o_text), output);

  char line[48];
  int n = snprintf(line, sizeof(line), "%u,%s,%s\n", (unsigned)row->idx, p_text, o_text);
  if (n > 0 && n < (int)sizeof(line))
  {
    Serial_Write(line, (uint16_t)n);
  }
}

/* ---- commands --------------------------------------------------------------- */

static void print_header(void)
{
  println(flow_cal ? "idx,pressure_cmH2O,flow_dp_cmH2O"
                   : "idx,pressure_cmH2O,flow_Lps");
}

static void print_rates(const char *label)
{
  uint32_t hz = Wave_GetFrequency();
  Serial_Printf("# %s%lu.000\r\n", label, (unsigned long)hz);
  Serial_Printf("# sample Hz: %lu.000\r\n", (unsigned long)(hz * WAVE_ROWS_PER_CYCLE));
}

static void report_dropped(void)
{
  uint32_t dropped = Serial_TxDroppedBytes();
  if (dropped)
  {
    Serial_ClearTxDropped();
    Serial_Printf("# TX_DROPPED %lu bytes\r\n", (unsigned long)dropped);
  }
}

static void start_recording(void)
{
  Serial_ClearTxDropped();
  recording = true;
  recording_started_ms = HAL_GetTick();
  apply_wave(Wave_GetFrequency()); /* speaker on, from the start of a cycle */

  println("# STARTED");
  if (flow_cal)
  {
    println("# EXCITATION OFF (flow calibration mode)");
  }
  else if (!excite_enabled)
  {
    println("# EXCITATION OFF (disarmed)");
  }
  else
  {
    println("# EXCITATION ON");
  }
  if (!Sensors_Available())
  {
    println("# WARNING: ADC1 not running - no sensor rows will be sent");
  }
  print_header();
  println("# FIRST_SAMPLE_PENDING");
}

static void stop_recording(void)
{
  recording = false;
  apply_wave(Wave_GetFrequency()); /* speaker off at the end of this cycle */
  report_dropped();
  println("# STOPPED");
}

static void set_frequency(int hz)
{
  if (hz <= 0 || !Wave_IsSupportedFrequency((uint32_t)hz))
  {
    Serial_Printf("# CONFIG_REJECTED frequency allowed 5/6/11/19, got: %d\r\n", hz);
    return;
  }
  apply_wave((uint32_t)hz);
  print_rates("excitation Hz set to: ");
}

static void upper(char *text)
{
  for (; *text; text++)
  {
    if (*text >= 'a' && *text <= 'z')
    {
      *text -= 'a' - 'A';
    }
  }
}

static char *trim(char *text)
{
  while (*text == ' ' || *text == '\t')
  {
    text++;
  }
  return text;
}

static void handle_config(char *command)
{
  int hz = -1;
  int flow_mode = -1;
  int excite = -1;

  char *token = strtok(command, ",");
  while (token)
  {
    token = trim(token);
    if (strncmp(token, "EXCITATION_HZ=", 14) == 0 ||
        strncmp(token, "FREQUENCY_HZ=", 13) == 0)
    {
      hz = atoi(strchr(token, '=') + 1);
    }
    else if (strncmp(token, "FLOW_CAL=", 9) == 0)
    {
      flow_mode = atoi(token + 9) != 0;
    }
    else if (strncmp(token, "EXCITE=", 7) == 0)
    {
      excite = atoi(token + 7) != 0;
    }
    /* DAC_AMPL, SAMPLE_HZ, CPAP_CM_H2O: accepted and ignored (the wave's
     * amplitude is fixed in wave.h; the sample rate follows the frequency). */
    token = strtok(NULL, ",");
  }

  bool speaker_changed = false;

  if (flow_mode >= 0)
  {
    bool changed = flow_cal != (bool)flow_mode;
    flow_cal = flow_mode;
    speaker_changed = true;
    Serial_Printf("# FLOW_CAL %s\r\n", flow_cal ? "ON (speaker disabled)" : "OFF");
    if (changed && recording)
    {
      print_header();
    }
  }

  if (excite >= 0)
  {
    excite_enabled = excite;
    speaker_changed = true;
    Serial_Printf("# EXCITE %s\r\n", excite_enabled ? "ARMED" : "DISARMED (speaker silent)");
  }

  if (hz > 0)
  {
    set_frequency(hz);             /* also applies the speaker on/off state */
  }
  else if (speaker_changed)
  {
    apply_wave(Wave_GetFrequency());
  }
  println("# CONFIG_ACK");
}

static void handle_command(char *command)
{
  upper(command);
  char *trimmed = trim(command);
  if (!*trimmed)
  {
    return;
  }

  if (strcmp(trimmed, "START") == 0)
  {
    start_recording();
  }
  else if (strcmp(trimmed, "STOP") == 0)
  {
    stop_recording();
  }
  else if (strcmp(trimmed, "STATUS") == 0)
  {
    println(recording ? "# STATUS_RECORDING" : "# STATUS_IDLE");
  }
  else if (strcmp(trimmed, "ZERO") == 0)
  {
    println("# ZERO_ACK_NOT_IMPLEMENTED");
  }
  else if (strcmp(trimmed, "CALIBRATE") == 0)
  {
    println("# CALIBRATE_ACK_NOT_IMPLEMENTED");
  }
  else if (strncmp(trimmed, "CONFIG", 6) == 0)
  {
    handle_config(trimmed);
  }
  else if (*trimmed == 'F')
  {
    set_frequency(atoi(trimmed + 1));
  }
  else
  {
    Serial_Printf("# UNKNOWN_COMMAND %s\r\n", trimmed);
  }
}

static void read_serial_commands(void)
{
  char c;
  while (Serial_ReadByte(&c))
  {
    if (c == '\n' || c == '\r')
    {
      if (cmd_len)
      {
        cmd_buf[cmd_len] = '\0';
        handle_command(cmd_buf);
        cmd_len = 0;
      }
    }
    else if (cmd_len < sizeof(cmd_buf) - 1)
    {
      cmd_buf[cmd_len++] = c;
    }
    else
    {
      cmd_len = 0;
    }
  }
}

/* ---- public ----------------------------------------------------------------- */

void Fot_Init(void)
{
  restart_rows();

  println("# READY");
  if (!Sensors_Available())
  {
    println("# ERROR: ADC1 did not start - check the ADC1 settings in the .ioc");
  }
  print_rates("excitation Hz: ");
  Serial_Printf("# LUT samples per cycle: %u\r\n", (unsigned)WAVE_ROWS_PER_CYCLE);
  println("# waiting for START command");
}

void Fot_Poll(void)
{
  Serial_Poll();
  read_serial_commands();

  if (recording && (HAL_GetTick() - recording_started_ms) > SAFETY_MAX_MS)
  {
    stop_recording();
    Serial_Printf("# AUTO_STOP excitation ran past the %lu s cap\r\n",
                  (unsigned long)(SAFETY_MAX_MS / 1000UL));
  }

  /* Rows arrive 70 per speaker cycle; they are always collected (so the ADC
   * buffer never falls behind) but only sent while recording. */
  sensor_row_t row;
  while (Sensors_PollRow(&row))
  {
    if (!recording)
    {
      continue;
    }
    if (!synced)
    {
      if (row.idx != 0U)
      {
        continue;
      }
      synced = true;
    }
    send_row(&row);
  }
}
