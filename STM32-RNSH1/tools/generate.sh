#!/usr/bin/env bash
# Regenerate the CubeMX code (Core/, Drivers/, .cproject) from the .ioc, headless.
# USER CODE sections and Core/*/app/ are kept. Review the result with `git diff`.
# Do not have the .ioc open with unsaved changes in CubeIDE/CubeMX while this runs.
source "$(dirname "$0")/common.sh"
need "$CUBEMX" "STM32CubeMX"

script="$(mktemp -t cubemx-script)"
log="$(mktemp -t cubemx-log)"
trap 'rm -f "$script"' EXIT
printf 'config load %s\nproject generate\nexit\n' "$IOC" > "$script"

echo "Generating code from $(basename "$IOC") ..."
"$CUBEMX" -q "$script" > "$log" 2>&1 || true

# CubeMX prints OK after a successful generate; anything else is a failure.
if grep -qx "OK" "$log"; then
  echo "Done. Changes:"
  git -C "$PROJECT_DIR" status --short
else
  echo "error: CubeMX did not generate the code. Log: $log" >&2
  grep -iE "error|exception" "$log" | grep -v "OptionalMessage" | tail -20 >&2
  exit 1
fi
