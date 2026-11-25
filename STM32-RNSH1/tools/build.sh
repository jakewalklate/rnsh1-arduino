#!/usr/bin/env bash
# Build the Debug configuration with CubeIDE's own headless builder, so the
# result is the same as pressing Build in the IDE. Output: Debug/STM32-RNSH1.elf
source "$(dirname "$0")/common.sh"
need "$CUBEIDE" "STM32CubeIDE"

# Private workspace (outside the project: Eclipse refuses one inside it), so it
# never clashes with the workspace your IDE uses.
workspace="${HOME}/Library/Caches/${PROJECT_NAME}-headless-workspace"
mkdir -p "$workspace"
log="$workspace/build.log"

echo "Building $PROJECT_NAME (Debug) ..."
"$CUBEIDE" --launcher.suppressErrors -nosplash \
  -application org.eclipse.cdt.managedbuilder.core.headlessbuild \
  -data "$workspace" -import "$PROJECT_DIR" -build "$PROJECT_NAME/Debug" > "$log" 2>&1 || true

grep -E "error:|warning:" "$log" | grep -v "^Drivers" || true
if grep -q "Build Finished. 0 errors" "$log" && [ -f "$ELF" ]; then
  grep -A3 "text" "$log" | head -2
  echo "Build OK: $ELF"
else
  echo "error: build failed. Full log: $log" >&2
  tail -30 "$log" >&2
  exit 1
fi
