#ifndef APP_FOT_H
#define APP_FOT_H

#include "main.h"

/* Serial protocol for the FOT web app, ported from
 * Python Code/.../firmware/arduino_connection/arduino_connection.ino.
 *
 * The speaker wave itself lives in app/wave.c. This module reads the sensors,
 * streams them, and passes the app's settings on to the wave:
 *   START, STOP, STATUS, F<hz>      (speaker plays only between START and STOP)
 *   CONFIG,EXCITATION_HZ=5|6|11|19   (speaker frequency)
 *   CONFIG,FLOW_CAL=0|1              (1: speaker silent, stream raw dp)
 *   CONFIG,EXCITE=0|1                (0: speaker silent)
 * While recording it sends "idx,pressure_cmH2O,flow_Lps" rows, 70 per cycle
 * of the speaker wave (idx = position in the cycle, 0..69), so the sample
 * rate is frequency x 70, exactly as with the Arduino. Each row is an average
 * of the ADC readings around that point of the cycle (see sensors.h). */

/* Call once after Serial_Init(), Sensors_Init() and Wave_Start(). */
void Fot_Init(void);

/* Call continuously from the main loop. */
void Fot_Poll(void);

#endif /* APP_FOT_H */
