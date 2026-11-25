#ifndef APP_WAVE_H
#define APP_WAVE_H

#include "main.h"
#include <stdbool.h>

/* Speaker sine wave on DAC1 OUT1 (PA4 / header A2), played by circular DMA
 * and paced by TIM6 TRGO. It runs on its own; the CPU only touches it to
 * change frequency or to silence it.
 *
 * The same TIM6 tick also starts an ADC scan of both sensors (sensors.c), so
 * every DAC sample has a matching pair of sensor readings.
 *
 * Supported frequencies use these fixed settings (TIM6 tick = 64 MHz, CubeMX
 * Prescaler 0). Every table is a multiple of 70 samples, so each of the 70
 * rows per cycle sent to the app averages a whole number of ADC readings:
 *
 *    Hz   samples/cycle     TIM6 period   DAC/ADC rate   frequency error
 *     5   1820 (70 x 26)       7033         9.10 kHz      4.7 ppm
 *     6   1400 (70 x 20)       7619         8.40 kHz      6.3 ppm
 *    11   1540 (70 x 22)       3778        16.94 kHz     10.6 ppm
 *    19   2100 (70 x 30)       1604        39.90 kHz      6.3 ppm
 *
 * Changes take effect at the end of the current cycle, where the sine is back
 * at mid-scale, so the speaker never sees a jump. */

#define WAVE_DEFAULT_FREQ_HZ  5U
#define WAVE_MAX_SAMPLES      2100U   /* longest table (19 Hz) */
#define WAVE_ROWS_PER_CYCLE   70U     /* rows per cycle sent to the app */

/* 12-bit DAC codes. Centre at mid-scale; amplitude kept away from the rails
 * because the output buffer cannot swing fully to 0 V / VDDA. */
#define WAVE_MID_CODE         2048U
#define WAVE_AMPLITUDE_CODE   1800U   /* ~0.3 V .. ~3.0 V at VDDA = 3.3 V */

/* Called while TIM6 is stopped, right before it starts again, every time the
 * wave (re)starts from sample 0. Used to restart the ADC in step with the DAC. */
typedef void (*wave_restart_hook_t)(uint32_t samples_per_cycle);

/* Start DAC1 CH1 (circular DMA) and TIM6 at 5 Hz, silent (mid-scale).
 * Wave_Configure(hz, true) turns the sine on. */
void Wave_Start(DAC_HandleTypeDef *hdac, TIM_HandleTypeDef *htim,
                wave_restart_hook_t on_restart);

/* true for 5, 6, 11 and 19 Hz. */
bool Wave_IsSupportedFrequency(uint32_t hz);

/* Set frequency and whether the sine plays (false = hold mid-scale, silent).
 * Waits for the current cycle to end (at most one cycle, 200 ms at 5 Hz).
 * Returns HAL_ERROR for an unsupported frequency (nothing changes). */
HAL_StatusTypeDef Wave_Configure(uint32_t hz, bool sine_on);

uint32_t Wave_GetFrequency(void);
bool     Wave_IsOn(void);

#endif /* APP_WAVE_H */
