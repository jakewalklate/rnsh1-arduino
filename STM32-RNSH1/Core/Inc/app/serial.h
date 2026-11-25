#ifndef APP_SERIAL_H
#define APP_SERIAL_H

#include "main.h"

/* Non-blocking UART link to the host (USART2 = ST-LINK virtual COM port).
 * TX: bytes go into a ring buffer that DMA drains in the background, so the
 *     main loop never waits on the UART. If the buffer is full the whole
 *     message is dropped and counted (see Serial_TxDroppedBytes()).
 * RX: bytes arrive by interrupt into a small ring buffer.               */

#define SERIAL_TX_BUFFER_SIZE  2048U
#define SERIAL_RX_BUFFER_SIZE  128U

void     Serial_Init(UART_HandleTypeDef *huart);

/* Call every main-loop pass: restarts a stuck transfer or a stopped receiver. */
void     Serial_Poll(void);

/* Queue len bytes. Returns 1 if queued, 0 if there was no room (dropped). */
int      Serial_Write(const char *data, uint16_t len);

/* printf-style helper (integers/strings only: newlib-nano has no %f). */
int      Serial_Printf(const char *fmt, ...);

/* Returns 1 and stores a byte in *c if one was received, else 0. */
int      Serial_ReadByte(char *c);

uint32_t Serial_TxDroppedBytes(void);
void     Serial_ClearTxDropped(void);

#endif /* APP_SERIAL_H */
