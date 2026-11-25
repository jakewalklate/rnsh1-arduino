#!/usr/bin/env bash
# Flash Debug/STM32-RNSH1.elf to the Nucleo over SWD, verify, and reset.
source "$(dirname "$0")/common.sh"
need "$CUBEPROG" "STM32_Programmer_CLI"
[ -f "$ELF" ] || { echo "error: $ELF not found - run tools/build.sh first" >&2; exit 1; }

out="$("$CUBEPROG" -c port=SWD -w "$ELF" -v -rst 2>&1 | sed 's/\x1b\[[0-9;]*m//g')"
if grep -q "Download verified successfully" <<<"$out"; then
  echo "Flashed and verified: $(basename "$ELF")"
else
  echo "$out" | tail -20 >&2
  echo "error: flashing failed (is the board plugged in, and no debug session running?)" >&2
  exit 1
fi
