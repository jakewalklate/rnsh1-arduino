#!/usr/bin/env bash
# Start the Oscillometry Measurement System on macOS or Linux.
# The first run creates a private Python environment (.venv) next to this file and
# installs the requirements; later runs start straight away.
set -euo pipefail
cd "$(dirname "$0")"

# Python versions the app's packages ship ready-made wheels for (3.14 matches the
# Windows build). A newer Python, e.g. 3.15, makes pip try to compile pyarrow from
# source, which fails. Set PYTHON=/path/to/python to override.
SUPPORTED="3.14 3.13 3.12 3.11"

is_supported() {
  local version
  version="$("$1" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null)" || return 1
  [[ " $SUPPORTED " == *" $version "* ]]
}

find_python() {
  if [ -n "${PYTHON:-}" ]; then
    echo "$PYTHON"; return 0
  fi
  local v candidate
  for v in $SUPPORTED; do
    candidate="$(command -v "python$v" 2>/dev/null || true)"
    if [ -n "$candidate" ] && is_supported "$candidate"; then
      echo "$candidate"; return 0
    fi
  done
  if command -v python3 >/dev/null 2>&1 && is_supported python3; then
    command -v python3; return 0
  fi
  # uv (https://docs.astral.sh/uv/) can locate -- or download -- a supported Python.
  if command -v uv >/dev/null 2>&1; then
    for v in $SUPPORTED; do
      candidate="$(uv python find "$v" 2>/dev/null || true)"
      if [ -n "$candidate" ]; then echo "$candidate"; return 0; fi
    done
    if uv python install 3.14 >/dev/null 2>&1; then
      uv python find 3.14; return 0
    fi
  fi
  return 1
}

STAMP=.venv/.installed
# A previous setup that failed half-way, or one built on an unsupported Python, is
# rebuilt rather than reused.
if [ -d .venv ] && { [ ! -f "$STAMP" ] || ! is_supported .venv/bin/python; }; then
  echo "Removing an incomplete Python environment from an earlier run..."
  rm -rf .venv
fi

if [ ! -f "$STAMP" ]; then
  if ! PY="$(find_python)"; then
    echo "This app needs Python 3.11 - 3.14 (newer versions are not supported by its"
    echo "packages yet). Install Python 3.14 from https://www.python.org/downloads/"
    echo "and run this again."
    exit 1
  fi
  echo "First run: setting up the Python environment with $("$PY" --version) (this takes a minute)..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  # --only-binary: fail fast with a clear error instead of trying to compile packages.
  .venv/bin/python -m pip install --only-binary=:all: -r requirements.txt
  touch "$STAMP"
fi

if [ "$(uname)" = "Linux" ] && ! id -nG | grep -qwE "dialout|uucp"; then
  echo "Note: to talk to the Arduino, your user may need to be in the 'dialout' group:"
  echo "    sudo usermod -aG dialout \$USER   (then log out and back in)"
fi

exec .venv/bin/python run_app.py
