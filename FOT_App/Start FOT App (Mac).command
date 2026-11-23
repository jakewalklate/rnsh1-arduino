#!/usr/bin/env bash
# Starts the Oscillometry Measurement System on a Mac. Double-click it.
#
# Nothing needs installing first. The first start downloads "uv", a small tool that
# fetches the right Python and the app's packages by itself (a few minutes, needs
# internet). Later starts take a few seconds. Everything it downloads is kept in
# your user folder, not in this folder.
cd "$(dirname "$0")" || exit 1
exec bash "_internal/app_src/launch.sh"
