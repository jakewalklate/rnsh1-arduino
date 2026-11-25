#include "app/sensors.h"
#include "app/wave.h"

/* One speaker cycle of (IN0, IN1) pairs, written by circular DMA. */
static uint16_t adc_buf[2U * WAVE_MAX_SAMPLES];

static ADC_HandleTypeDef *sensor_adc;
static uint32_t samples_per_cycle;     /* N: pairs in one cycle           */
static uint32_t per_row;               /* k = N / 70: pairs averaged/row  */
static volatile uint32_t cycles_done;  /* full buffers since the restart  */
static uint32_t next_row;              /* rows counted from the restart   */
static bool running;

float Sensors_ToCmH2O(float counts, float full_scale)
{
  float ratio = (counts / SENSOR_ADC_MAX) * SENSOR_ADC_TO_SUPPLY_RATIO;
  return ((ratio - 0.10f) / 0.80f) * (2.0f * full_scale) - full_scale;
}

HAL_StatusTypeDef Sensors_Init(ADC_HandleTypeDef *hadc)
{
  sensor_adc = NULL;
  /* The .ioc must give a 2-rank scan (IN0, IN1) started by TIM6. */
  if (hadc->Init.NbrOfConversion != 2U ||
      hadc->Init.ExternalTrigConv != ADC_EXTERNALTRIG_T6_TRGO ||
      hadc->DMA_Handle == NULL)
  {
    return HAL_ERROR;
  }
  /* Calibrate once while the ADC is still disabled (right after MX init). */
  if (HAL_ADCEx_Calibration_Start(hadc) != HAL_OK)
  {
    return HAL_ERROR;
  }
  sensor_adc = hadc;
  return HAL_OK;
}

void Sensors_Restart(uint32_t samples)
{
  if (sensor_adc == NULL)
  {
    return;
  }
  /* TIM6 is stopped while this runs, so no conversion can start until the
   * DAC and ADC both begin again from sample 0 on the same tick. */
  HAL_ADC_Stop_DMA(sensor_adc);
  running = false;
  samples_per_cycle = samples;
  per_row = samples / WAVE_ROWS_PER_CYCLE;
  cycles_done = 0;
  next_row = 1;   /* row 0's window would start before the first reading */
  if (HAL_ADC_Start_DMA(sensor_adc, (uint32_t *)adc_buf, 2U * samples) == HAL_OK)
  {
    running = true;
  }
}

int Sensors_Available(void)
{
  return running;
}

/* Pairs completed since the restart. Never over-counts: if the DMA has
 * wrapped but the interrupt has not run yet, this reads low for a moment. */
static uint32_t pairs_done(void)
{
  uint32_t cycles;
  uint32_t remaining;
  do
  {
    cycles = cycles_done;
    remaining = __HAL_DMA_GET_COUNTER(sensor_adc->DMA_Handle);
  } while (cycles != cycles_done);

  uint32_t halfwords = 2U * samples_per_cycle;
  uint32_t written = (halfwords - remaining) % halfwords;
  return cycles * samples_per_cycle + written / 2U;
}

bool Sensors_PollRow(sensor_row_t *row)
{
  if (!running)
  {
    return false;
  }

  /* Pair g was converted on TIM6 tick g, when the DAC started outputting
   * table sample g-1 (the DAC is one tick behind its DMA). Row R stands for
   * cycle position R*k, so it averages the k pairs centred there:
   * g = R*k + 1 - k/2 ... R*k + k/2. */
  const uint32_t k = per_row;
  const uint32_t half = k / 2U;
  uint32_t done = pairs_done();

  /* Fallen behind by more than half a cycle? Skip ahead (data overwritten). */
  if (done > next_row * k + samples_per_cycle / 2U)
  {
    next_row = (done - half) / k;
  }
  if (done < next_row * k + half + 1U)
  {
    return false;                      /* window not complete yet */
  }

  uint32_t first = next_row * k + 1U - half;
  uint32_t sum_a = 0;
  uint32_t sum_b = 0;
  for (uint32_t i = 0; i < k; i++)
  {
    uint32_t pos = ((first + i) % samples_per_cycle) * 2U;
    sum_a += adc_buf[pos];
    sum_b += adc_buf[pos + 1U];
  }

  row->idx = next_row % WAVE_ROWS_PER_CYCLE;
  row->a_counts = (float)sum_a / (float)k;
  row->b_counts = (float)sum_b / (float)k;
  next_row++;
  return true;
}

/* DMA wrapped: one more full cycle of readings is in the buffer. */
void HAL_ADC_ConvCpltCallback(ADC_HandleTypeDef *hadc)
{
  if (hadc == sensor_adc)
  {
    cycles_done++;
  }
}
