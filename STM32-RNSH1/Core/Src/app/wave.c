#include "app/wave.h"

typedef struct
{
  uint8_t  hz;
  uint16_t samples;        /* table length for one cycle */
} wave_setting_t;

/* See the table in wave.h. TIM6 period = 64 MHz / (hz * samples).
 * Every length must be a multiple of WAVE_ROWS_PER_CYCLE (checked at start). */
static const wave_setting_t wave_settings[] =
{
  {  5U, 1820U },
  {  6U, 1400U },
  { 11U, 1540U },
  { 19U, 2100U },
};

/* Two tables: DMA plays one while the next is prepared in the other. */
static uint16_t wave_tables[2][WAVE_MAX_SAMPLES];
static uint8_t  wave_active;          /* index of the table DMA is playing */
static uint32_t wave_len;             /* its length */
static uint32_t wave_hz;
static bool     wave_on;

static DAC_HandleTypeDef *wave_dac;
static TIM_HandleTypeDef *wave_tim;
static wave_restart_hook_t wave_on_restart;

static const wave_setting_t *find_setting(uint32_t hz)
{
  for (uint32_t i = 0; i < sizeof(wave_settings) / sizeof(wave_settings[0]); i++)
  {
    if (wave_settings[i].hz == hz)
    {
      return &wave_settings[i];
    }
  }
  return NULL;
}

/* One cycle of sine (or a flat mid-scale line when silent), without libm:
 *   sin((n+1)w) = 2*cos(w)*sin(n*w) - sin((n-1)*w)
 * cos(w) comes from its Taylor series (w is tiny, so this is exact enough). */
static void wave_fill(uint16_t *table, uint32_t samples, bool sine_on)
{
  if (!sine_on)
  {
    for (uint32_t n = 0; n < samples; n++)
    {
      table[n] = (uint16_t)WAVE_MID_CODE;
    }
    return;
  }

  const double pi = 3.14159265358979323846;
  const double w  = 2.0 * pi / (double)samples;
  const double w2 = w * w;
  const double cos_w = 1.0 - w2 / 2.0 + (w2 * w2) / 24.0 - (w2 * w2 * w2) / 720.0;
  const double k = 2.0 * cos_w;

  double s_prev = -w + (w * w2) / 6.0;   /* sin(-w) */
  double s_curr = 0.0;                   /* sin(0)  */

  for (uint32_t n = 0; n < samples; n++)
  {
    double v = (double)WAVE_MID_CODE + (double)WAVE_AMPLITUDE_CODE * s_curr;
    if (v < 0.0)    v = 0.0;
    if (v > 4095.0) v = 4095.0;
    table[n] = (uint16_t)(v + 0.5);

    double s_next = k * s_curr - s_prev;
    s_prev = s_curr;
    s_curr = s_next;
  }
}

/* TIM6 period (in timer ticks) for hz * samples DAC updates per second. */
static uint32_t wave_period_ticks(uint32_t hz, uint32_t samples)
{
  /* STM32G0: timer clock = PCLK, or 2 x PCLK if the APB prescaler != 1. */
  uint32_t pclk = HAL_RCC_GetPCLK1Freq();
  uint32_t timclk = (HAL_RCC_GetHCLKFreq() == pclk) ? pclk : 2U * pclk;
  uint32_t tick_hz = timclk / (wave_tim->Init.Prescaler + 1U);
  uint32_t rate = hz * samples;
  return (tick_hz + rate / 2U) / rate;
}

/* Stop TIM6 right after the last sample of the cycle has gone out.
 *
 * DMA loads the DAC's holding register on each trigger, and the DAC outputs
 * the value loaded on the trigger before. So when the DMA counter drops to
 * len-1, the pin shows the last sample (just below mid-scale) and the holding
 * register already has sample 0 (mid-scale). Stopping the timer there freezes
 * the output at the end of a cycle. */
static void wave_stop_at_cycle_end(void)
{
  DMA_HandleTypeDef *dma = wave_dac->DMA_Handle1;
  uint32_t start = HAL_GetTick();

  while ((HAL_GetTick() - start) < 1000U)
  {
    /* Counter == len: the last sample is loaded and is output next. */
    if (__HAL_DMA_GET_COUNTER(dma) != wave_len)
    {
      continue;
    }
    /* From here it is less than one DAC period (<= 200 us): no interrupts,
     * so the stop cannot be late. */
    uint32_t primask = __get_PRIMASK();
    __disable_irq();
    for (uint32_t spin = 0; spin < 20000U; spin++)
    {
      if (__HAL_DMA_GET_COUNTER(dma) == wave_len - 1U)
      {
        HAL_TIM_Base_Stop(wave_tim);
        __set_PRIMASK(primask);
        return;
      }
    }
    __set_PRIMASK(primask);
  }
  HAL_TIM_Base_Stop(wave_tim);       /* fallback: never wait forever */
}

void Wave_Start(DAC_HandleTypeDef *hdac, TIM_HandleTypeDef *htim,
                wave_restart_hook_t on_restart)
{
  wave_dac = hdac;
  wave_tim = htim;
  wave_on_restart = on_restart;

  for (uint32_t i = 0; i < sizeof(wave_settings) / sizeof(wave_settings[0]); i++)
  {
    if (wave_settings[i].samples % WAVE_ROWS_PER_CYCLE != 0U ||
        wave_settings[i].samples > WAVE_MAX_SAMPLES)
    {
      Error_Handler();   /* table lengths must be whole multiples of 70 */
    }
  }

  const wave_setting_t *s = find_setting(WAVE_DEFAULT_FREQ_HZ);
  wave_active = 0;
  wave_len = s->samples;
  wave_hz = s->hz;
  wave_on = false;                 /* silent until Wave_Configure(.., true) */
  wave_fill(wave_tables[wave_active], wave_len, wave_on);
  __HAL_TIM_SET_AUTORELOAD(wave_tim, wave_period_ticks(wave_hz, wave_len) - 1U);

  /* Arm the DAC + DMA first so the first TIM6 trigger already has data. */
  if (HAL_DAC_Start_DMA(hdac, DAC_CHANNEL_1, (uint32_t *)wave_tables[wave_active],
                        wave_len, DAC_ALIGN_12B_R) != HAL_OK)
  {
    Error_Handler();
  }
  if (wave_on_restart != NULL)
  {
    wave_on_restart(wave_len);
  }

  if (HAL_TIM_Base_Start(htim) != HAL_OK)
  {
    Error_Handler();
  }
}

bool Wave_IsSupportedFrequency(uint32_t hz)
{
  return find_setting(hz) != NULL;
}

HAL_StatusTypeDef Wave_Configure(uint32_t hz, bool sine_on)
{
  const wave_setting_t *s = find_setting(hz);
  if (s == NULL)
  {
    return HAL_ERROR;
  }
  if (hz == wave_hz && sine_on == wave_on)
  {
    return HAL_OK;                        /* already playing this */
  }

  uint32_t ticks = wave_period_ticks(hz, s->samples);
  if (ticks < 2U || ticks > 65536U)
  {
    return HAL_ERROR;
  }

  /* Prepare the new cycle while the old one keeps playing. */
  uint8_t next = (uint8_t)(wave_active ^ 1U);
  wave_fill(wave_tables[next], s->samples, sine_on);

  wave_stop_at_cycle_end();

  /* Rewind onto the new table. The DAC channel stays enabled the whole time,
   * so the pin holds its (mid-scale) value and never floats. */
  HAL_DMA_Abort(wave_dac->DMA_Handle1);
  HAL_DAC_SetValue(wave_dac, DAC_CHANNEL_1, DAC_ALIGN_12B_R, WAVE_MID_CODE);
  __HAL_TIM_SET_AUTORELOAD(wave_tim, ticks - 1U);
  __HAL_TIM_SET_COUNTER(wave_tim, 0);
  if (HAL_DMA_Start(wave_dac->DMA_Handle1, (uint32_t)wave_tables[next],
                    (uint32_t)&wave_dac->Instance->DHR12R1, s->samples) != HAL_OK)
  {
    Error_Handler();
  }

  wave_active = next;
  wave_len = s->samples;
  wave_hz = hz;
  wave_on = sine_on;
  if (wave_on_restart != NULL)
  {
    wave_on_restart(wave_len);
  }

  if (HAL_TIM_Base_Start(wave_tim) != HAL_OK)
  {
    Error_Handler();
  }
  return HAL_OK;
}

uint32_t Wave_GetFrequency(void)
{
  return wave_hz;
}

bool Wave_IsOn(void)
{
  return wave_on;
}
