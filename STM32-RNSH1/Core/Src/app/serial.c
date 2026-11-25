#include "app/serial.h"
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#define TX_SIZE SERIAL_TX_BUFFER_SIZE
#define RX_SIZE SERIAL_RX_BUFFER_SIZE

static UART_HandleTypeDef *serial_uart;

/* TX ring: main loop writes at tx_head, DMA sends from tx_tail. */
static uint8_t           tx_buf[TX_SIZE];
static volatile uint16_t tx_head;
static volatile uint16_t tx_tail;
static volatile uint16_t tx_inflight;      /* bytes handed to the current DMA */
static volatile uint8_t  tx_busy;
static volatile uint32_t tx_start_tick;
static volatile uint32_t tx_dropped;

/* RX ring: UART interrupt writes at rx_head, main loop reads at rx_tail. */
static uint8_t           rx_buf[RX_SIZE];
static volatile uint16_t rx_head;
static volatile uint16_t rx_tail;
static uint8_t           rx_byte;

static uint32_t lock(void)
{
  uint32_t primask = __get_PRIMASK();
  __disable_irq();
  return primask;
}

static void unlock(uint32_t primask)
{
  __set_PRIMASK(primask);
}

/* Start DMA on the next contiguous block, if idle. Call with IRQs locked. */
static void tx_kick_locked(void)
{
  if (tx_busy || tx_head == tx_tail)
  {
    return;
  }
  uint16_t len = (tx_head > tx_tail) ? (uint16_t)(tx_head - tx_tail)
                                     : (uint16_t)(TX_SIZE - tx_tail);
  tx_busy = 1;
  tx_inflight = len;
  tx_start_tick = HAL_GetTick();
  if (HAL_UART_Transmit_DMA(serial_uart, &tx_buf[tx_tail], len) != HAL_OK)
  {
    /* Data stays queued; Serial_Poll() recovers the UART and retries. */
    tx_busy = 0;
    tx_inflight = 0;
  }
}

/* Drop/finish the in-flight block and move on. Call with IRQs locked. */
static void tx_advance_locked(int dropped)
{
  if (dropped)
  {
    tx_dropped += tx_inflight;
  }
  tx_tail = (uint16_t)((tx_tail + tx_inflight) % TX_SIZE);
  tx_inflight = 0;
  tx_busy = 0;
  tx_kick_locked();
}

static void rx_arm(void)
{
  (void)HAL_UART_Receive_IT(serial_uart, &rx_byte, 1);
}

void Serial_Init(UART_HandleTypeDef *huart)
{
  serial_uart = huart;
  tx_head = tx_tail = tx_inflight = 0;
  tx_busy = 0;
  tx_dropped = 0;
  rx_head = rx_tail = 0;
  rx_arm();
}

void Serial_Poll(void)
{
  if (serial_uart == NULL)
  {
    return;
  }

  /* Watchdog: a transfer that should long be finished means a lost
   * completion; abort it so output can never freeze. */
  if (tx_busy)
  {
    uint32_t baud = serial_uart->Init.BaudRate ? serial_uart->Init.BaudRate : 115200U;
    uint32_t expected_ms = ((uint32_t)tx_inflight * 10U * 1000U) / baud;
    if ((HAL_GetTick() - tx_start_tick) > (2U * expected_ms + 20U))
    {
      HAL_UART_AbortTransmit(serial_uart);
      uint32_t p = lock();
      if (tx_busy)
      {
        tx_advance_locked(1);
      }
      unlock(p);
    }
  }
  else if (tx_head != tx_tail)
  {
    /* Data waiting but nothing sending: the last start failed. */
    if (serial_uart->gState != HAL_UART_STATE_READY)
    {
      HAL_UART_AbortTransmit(serial_uart);
    }
    uint32_t p = lock();
    tx_kick_locked();
    unlock(p);
  }

  /* Receiver stopped after a blocking error (e.g. overrun): restart it. */
  if (serial_uart->RxState == HAL_UART_STATE_READY)
  {
    rx_arm();
  }
}

int Serial_Write(const char *data, uint16_t len)
{
  if (serial_uart == NULL || len == 0)
  {
    return 0;
  }

  uint32_t p = lock();
  uint16_t used = (uint16_t)((tx_head + TX_SIZE - tx_tail) % TX_SIZE);
  uint16_t room = (uint16_t)(TX_SIZE - 1U - used);
  if (len > room)
  {
    tx_dropped += len;
    unlock(p);
    return 0;
  }

  uint16_t first = (uint16_t)(TX_SIZE - tx_head);
  if (first > len)
  {
    first = len;
  }
  memcpy(&tx_buf[tx_head], data, first);
  memcpy(&tx_buf[0], data + first, len - first);
  tx_head = (uint16_t)((tx_head + len) % TX_SIZE);

  tx_kick_locked();
  unlock(p);
  return 1;
}

int Serial_Printf(const char *fmt, ...)
{
  char tmp[128];
  va_list args;
  va_start(args, fmt);
  int len = vsnprintf(tmp, sizeof(tmp), fmt, args);
  va_end(args);

  if (len <= 0)
  {
    return 0;
  }
  if (len >= (int)sizeof(tmp))
  {
    len = sizeof(tmp) - 1;
  }
  return Serial_Write(tmp, (uint16_t)len);
}

int Serial_ReadByte(char *c)
{
  if (rx_tail == rx_head)
  {
    return 0;
  }
  *c = (char)rx_buf[rx_tail];
  rx_tail = (uint16_t)((rx_tail + 1U) % RX_SIZE);
  return 1;
}

uint32_t Serial_TxDroppedBytes(void)
{
  return tx_dropped;
}

void Serial_ClearTxDropped(void)
{
  tx_dropped = 0;
}

/* ---- HAL callbacks (override the weak versions in the HAL driver) ---- */

void HAL_UART_TxCpltCallback(UART_HandleTypeDef *huart)
{
  if (huart == serial_uart)
  {
    uint32_t p = lock();
    if (tx_busy)
    {
      tx_advance_locked(0);
    }
    unlock(p);
  }
}

void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
{
  if (huart == serial_uart)
  {
    uint16_t next = (uint16_t)((rx_head + 1U) % RX_SIZE);
    if (next != rx_tail)            /* full: drop the byte */
    {
      rx_buf[rx_head] = rx_byte;
      rx_head = next;
    }
    rx_arm();
  }
}

void HAL_UART_ErrorCallback(UART_HandleTypeDef *huart)
{
  if (huart != serial_uart)
  {
    return;
  }

  /* A TX DMA error ends the transfer without TxCplt: drop that block and
   * carry on (this was the original "UART freezes" bug). RX-only errors
   * leave gState busy, so an ongoing transmission is left alone. */
  uint32_t p = lock();
  if (tx_busy && huart->gState == HAL_UART_STATE_READY)
  {
    tx_advance_locked(1);
  }
  unlock(p);

  /* Overrun etc. stop the receiver: re-arm it. */
  if (huart->RxState == HAL_UART_STATE_READY)
  {
    rx_arm();
  }
}
