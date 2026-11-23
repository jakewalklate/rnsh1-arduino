"""Cross-platform launcher for the Oscillometry Measurement System.

Runs the Streamlit app in _internal/app_src with whatever Python runs this file and
opens it in the default web browser. Works on Windows, macOS and Linux:

    python run_app.py            (Windows)
    python3 run_app.py           (macOS / Linux)

The double-click starters ("Start FOT App (macOS).command", "start_linux.sh") set
up a private Python environment the first time and then call this file.
"""

import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_DIR = HERE / "_internal" / "app_src"
PORT = 8501


def _wait_then_open_browser(url, timeout_seconds=60):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.5)
    print(f"The app did not start within {timeout_seconds} s. Open {url} manually.")


def main():
    if not (APP_DIR / "app.py").exists():
        sys.exit(f"Could not find the app source at {APP_DIR}")

    try:
        import serial  # noqa: F401
        import streamlit  # noqa: F401
    except ImportError as error:
        sys.exit(
            f"Missing Python package ({error.name}). Install the requirements first:\n"
            f'    {Path(sys.executable).name} -m pip install -r "{HERE / "requirements.txt"}"'
        )

    url = f"http://localhost:{PORT}"
    threading.Thread(target=_wait_then_open_browser, args=(url,), daemon=True).start()
    print(f"Starting the app at {url} -- leave this window open while you use it.")

    command = [
        sys.executable, "-m", "streamlit", "run", "app.py",
        "--server.headless", "true",
        "--server.port", str(PORT),
        "--browser.gatherUsageStats", "false",
    ]
    try:
        sys.exit(subprocess.call(command, cwd=APP_DIR))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
