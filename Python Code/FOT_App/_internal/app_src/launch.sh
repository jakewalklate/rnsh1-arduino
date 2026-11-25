#!/usr/bin/env bash
# Shared by "Start FOT App (Mac).command" and "Start FOT App (Linux).sh".
#
# Runs the app from this folder's code with uv (https://docs.astral.sh/uv/), which
# downloads a private Python 3.13 and the packages in requirements.txt the first
# time and reuses them afterwards. Nothing is installed system-wide and no Python
# needs to be on the computer.
set -u
cd "$(dirname "$0")" || exit 1

fail() {
  echo
  echo "The app could not start: $1"
  echo "Send a screenshot of this window to whoever looks after the app."
  # Keep the window open so the message can be read.
  read -r -p "Press Return to close." _ || true
  exit 1
}

find_uv() {
  if command -v uv >/dev/null 2>&1; then command -v uv; return 0; fi
  for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -x "$candidate" ]; then echo "$candidate"; return 0; fi
  done
  return 1
}

if ! UV="$(find_uv)"; then
  echo "First start: downloading uv, which sets up Python for the app (needs internet)..."
  # UV_NO_MODIFY_PATH: put uv in ~/.local/bin without editing shell start-up files.
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | UV_NO_MODIFY_PATH=1 sh \
      || fail "uv could not be downloaded. Check the internet connection and try again."
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | UV_NO_MODIFY_PATH=1 sh \
      || fail "uv could not be downloaded. Check the internet connection and try again."
  else
    fail "this computer has neither curl nor wget to download uv."
  fi
  UV="$(find_uv)" || fail "uv was downloaded but cannot be found."
fi

if [ "$(uname)" = "Linux" ] && ! id -nG | grep -qwE "dialout|uucp"; then
  echo "Note: to talk to the Arduino, your user may need to be in the 'dialout' group:"
  echo "    sudo usermod -aG dialout \$USER   (then log out and back in)"
fi

echo "Starting the app (the first start takes a few minutes while it sets up)..."
"$UV" run --quiet --no-project --python 3.13 \
  --with-requirements requirements.txt \
  python desktop.py \
  || fail "see the messages above."
