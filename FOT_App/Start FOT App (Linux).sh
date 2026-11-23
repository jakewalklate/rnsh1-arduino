#!/usr/bin/env bash
# Starts the Oscillometry Measurement System on Linux.
# Double-click it (choose "Run" if asked), or in a terminal:  bash "Start FOT App (Linux).sh"
#
# Nothing needs installing first. The first start downloads "uv", a small tool that
# fetches the right Python and the app's packages by itself (a few minutes, needs
# internet). Later starts take a few seconds.
cd "$(dirname "$0")" || exit 1
exec bash "_internal/app_src/launch.sh"
