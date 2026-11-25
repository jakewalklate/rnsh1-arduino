#ifndef APP_SENSORS_H
#define APP_SENSORS_H

#include "main.h"
#include <stdbool.h>

/* Pressure sensor A (airway pressure)   -> PA0 / header A0 / ADC1_IN0, rank 1
 * Pressure sensor B (flow differential) -> PA1 / header A1 / ADC1_IN1, rank 2
 * (On the Arduino these were A1 and A2; Nucleo A2 is the DAC output.)
 *
 * Sampling (all set up in the .ioc, no software timing):
 *  - Every TIM6 tick - the same tick that steps the speaker DAC - starts one
 *    ADC scan: IN0, then IN1 about 2.9 us later (79.5-cycle sampling at a
 *    32 MHz ADC clock). That gap is 0.005 deg at 5 Hz, 0.02 deg at 19 Hz.
 *  - DMA stores the pairs in a circular buffer holding exactly one cycle, so
 *    buffer position = position in the speaker cycle.
 *  - Each of the 70 rows per cycle is the average of the N/70 readings
 *    centred on its point in the cycle (26-30 readings), which lowers the
 *    noise about 5x and keeps the Arduino's meaning of idx. */

#define SENSOR_ADC_MAX  4095.0f   /* 12-bit ADC (Arduino R4 used 14-bit: 16383) */

/* The 10%..90% transfer function is a fraction of the SENSOR's supply, the
 * ADC measures a fraction of VDDA (3.3 V). If the sensors run from 3.3 V
 * leave this at 1.0. If they run from 5 V through a divider with ratio
 * k = Rbottom/(Rtop+Rbottom), set this to 3.3 / (5.0 * k). */
#define SENSOR_ADC_TO_SUPPLY_RATIO  1.0f

typedef struct
{
  uint32_t idx;        /* position in the speaker cycle, 0..69 */
  float    a_counts;   /* averaged ADC counts, sensor A (IN0) */
  float    b_counts;   /* averaged ADC counts, sensor B (IN1) */
} sensor_row_t;

/* Calibrate ADC1. Call once after MX_ADC1_Init(), before Wave_Start(). */
HAL_StatusTypeDef Sensors_Init(ADC_HandleTypeDef *hadc);

/* Restart hook for Wave_Start(): (re)arms the ADC + DMA for one cycle of
 * samples_per_cycle readings, in step with the DAC. */
void Sensors_Restart(uint32_t samples_per_cycle);

int Sensors_Available(void);

/* Returns true and fills *row when the next row is complete. Call often;
 * rows not collected within about half a cycle are skipped. */
bool Sensors_PollRow(sensor_row_t *row);

/* Convert (averaged) counts to cmH2O for a sensor with +/- full_scale range. */
float Sensors_ToCmH2O(float counts, float full_scale);

#endif /* APP_SENSORS_H */
