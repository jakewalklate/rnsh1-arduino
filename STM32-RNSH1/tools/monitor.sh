#!/usr/bin/env bash
# Open the board's serial port (ST-LINK virtual COM port) like the Arduino
# Serial Monitor. Type commands such as STATUS, START, STOP, F6 and press Enter.
# Quit with Ctrl-] . Close this before using the web app (one program at a time).
source "$(dirname "$0")/common.sh"
BAUD="${BAUD:-460800}"
PORT="${1:-$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)}"
[ -n "$PORT" ] || { echo "error: no /dev/cu.usbmodem* port - is the board plugged in?" >&2; exit 1; }

PY="${PYTHON:-$PROJECT_DIR/../Python Code/FOT_App/.venv/bin/python}"
if [ -x "$PY" ] && "$PY" -c "import serial" 2>/dev/null; then
  exec "$PY" -m serial.tools.miniterm --eol LF "$PORT" "$BAUD"
fi
echo "(pyserial not found, using screen: quit with Ctrl-A then K)"
exec screen "$PORT" "$BAUD"
