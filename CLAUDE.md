# FOT device – STM32 NUCLEO-G071RB (STM32G071RBT6) + FOT web app

Two parts live in this folder:
- `STM32-RNSH1/` – the firmware (git repository). Speaker sine on the DAC, the two
  pressure sensors on the ADC, serial protocol to the web app.
- `Python Code/FOT_App/` – the FOT desktop/web app that talks to the board over serial.

## Firmware rules
- `STM32-RNSH1/STM32-RNSH1.ioc` is the single source of truth for pins, clocks and
  peripherals. Claude may edit it (it is a plain `Key=Value` text file), then MUST
  regenerate with `tools/generate.sh` – never hand-edit the generated init code instead.
- In generated files (Core/Src/main.c, stm32g0xx_it.c, stm32g0xx_hal_msp.c, …) ONLY edit
  between `/* USER CODE BEGIN ... */` and `/* USER CODE END ... */` markers.
  Everything else is overwritten by every generate.
- Never edit `Drivers/` (HAL/CMSIS); generate copies what it needs.
- Application code goes in `Core/Src/app/` and `Core/Inc/app/`:
  - `wave.c`    – speaker sine (DAC1 + TIM6 + DMA), frequencies 5/6/11/19 Hz
  - `sensors.c` – ADC1 IN0/IN1 sampled on every TIM6 tick by DMA, averaged per row
  - `serial.c`  – non-blocking UART (DMA TX ring, interrupt RX)
  - `fot.c`     – web-app protocol (START/STOP/STATUS/CONFIG,…), CSV rows
- Use the HAL API (not LL/register access) unless asked otherwise.
- The serial protocol must stay compatible with
  `Python Code/FOT_App/_internal/app_src/backend/device/serial_device.py`.

## Workflow (run from STM32-RNSH1/)
1. Change `.ioc` and/or app code.
2. `tools/generate.sh` – only if the .ioc changed (headless STM32CubeMX).
3. `tools/build.sh`    – CubeIDE headless build → `Debug/STM32-RNSH1.elf`.
4. `tools/flash.sh`    – flash over SWD, verify, reset.
5. Test on the board (serial at 460800 baud; `tools/monitor.sh` for a terminal).
6. Show the change with `git diff`; commit only when asked.

Do not run `tools/generate.sh` while the .ioc is open with unsaved changes in
CubeIDE/CubeMX – the two copies would overwrite each other.

## Python app
- Venv: `Python Code/FOT_App/.venv/bin/python`
- Tests (from `Python Code/FOT_App/_internal/app_src`):
  `../../.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`
- Device settings (baud 460800, port detection, frequencies) are in `backend/config.py`.
