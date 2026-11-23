@echo off
rem Starts the Oscillometry Measurement System on Windows. Double-click it.
rem
rem Nothing needs installing first. The first start downloads "uv", a small tool that
rem fetches the right Python and the app's packages by itself (a few minutes, needs
rem internet). Later starts take a few seconds. Leave this black window open while
rem the app is running; it closes by itself when the app window is closed.
setlocal
cd /d "%~dp0_internal\app_src"

set "UV="
for /f "delims=" %%i in ('where uv 2^>nul') do if not defined UV set "UV=%%i"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"

if not defined UV (
  echo First start: downloading uv, which sets up Python for the app ^(needs internet^)...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:UV_NO_MODIFY_PATH='1'; irm https://astral.sh/uv/install.ps1 | iex"
  if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
)
if not defined UV (
  echo.
  echo The app could not start: uv could not be downloaded.
  echo Check the internet connection and try again.
  pause
  exit /b 1
)

echo Starting the app ^(the first start takes a few minutes while it sets up^)...
"%UV%" run --quiet --no-project --python 3.13 --with-requirements requirements.txt python desktop.py
if errorlevel 1 (
  echo.
  echo The app could not start. Send a screenshot of this window to whoever looks after the app.
  pause
)
