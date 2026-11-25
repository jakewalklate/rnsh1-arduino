# Shared paths for the tools/ scripts. Override any of these with an
# environment variable of the same name if your install is elsewhere.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_NAME="STM32-RNSH1"
IOC="$PROJECT_DIR/$PROJECT_NAME.ioc"
ELF="$PROJECT_DIR/Debug/$PROJECT_NAME.elf"

CUBEMX="${CUBEMX:-/Applications/STMicroelectronics/STM32CubeMX.app/Contents/MacOs/STM32CubeMX}"
CUBEIDE="${CUBEIDE:-/Applications/STM32CubeIDE.app/Contents/MacOS/STM32CubeIDE}"
CUBEPROG="${CUBEPROG:-$(ls -d /Applications/STM32CubeIDE.app/Contents/Eclipse/plugins/com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.*/tools/bin/STM32_Programmer_CLI 2>/dev/null | tail -1)}"

need() { [ -x "$1" ] || { echo "error: $2 not found at $1" >&2; exit 1; }; }
