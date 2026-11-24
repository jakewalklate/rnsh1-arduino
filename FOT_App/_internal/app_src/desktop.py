"""Desktop launcher: runs the app in its own window instead of a browser tab.

    python desktop.py

starts the Streamlit server in a background process on a free local port, waits for
it to answer, and opens it in a window with no address bar and no back or forward
buttons:

  * Windows: Microsoft Edge in app mode (Edge is on every Windows 10/11 PC). Not
    pywebview, whose WebView2-through-.NET window showed only a white page there.
  * macOS: pywebview (WebKit).
  * Linux: pywebview (Qt WebEngine), if Qt can open a window on that PC.

The keyboard and mouse shortcuts for back and forward are switched off by the app
page itself (app.py runs BLOCK_HISTORY_NAVIGATION_JS), so they are off in any
window. Closing the window stops the server.

The same file is the entry point of the packaged Windows build (see
build_windows.ps1). A frozen build has no separate Python to run Streamlit with, so
it starts a second copy of itself with --serve, which runs the server instead of the
window.

If pywebview is missing or cannot open a window (for example a Linux PC without a
desktop), it falls back to opening the app in the normal web browser, so the app is
always usable.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

TITLE = "Oscillometry Measurement System"
WINDOW_SIZE = (1400, 900)
MIN_WINDOW_SIZE = (900, 640)
STARTUP_TIMEOUT_SECONDS = 90

FROZEN = getattr(sys, "frozen", False)


def _find_app_dir():
    """The app_src folder: the code the server runs and the patient database.

    From source it is the folder holding this file. A packaged build finds it in the
    "_internal" folder next to itself, so every build (Windows FOT App.exe, macOS
    FOT App.app, Linux FOT App) shares one app_src and one set of patient data:
      FOT_App/FOT App.exe    -> FOT_App/_internal/app_src
      FOT_App/FOT App.app    -> FOT_App/_internal/app_src
      FOT_App/FOT App        -> FOT_App/_internal/app_src
    """
    if not FROZEN:
        return Path(__file__).resolve().parent
    exe = Path(sys.executable).resolve()
    candidates = [exe.parent]
    for parent in exe.parents:
        if parent.suffix == ".app":  # .../FOT App.app/Contents/MacOS/FOT App
            candidates.insert(0, parent.parent)
            break
    for folder in candidates:
        app_dir = folder / "_internal" / "app_src"
        if (app_dir / "app.py").exists():
            return app_dir
    return Path(getattr(sys, "_MEIPASS", exe.parent)) / "app_src"


APP_DIR = _find_app_dir()
LOG_PATH = APP_DIR / "data" / "desktop.log"

# Back and forward exist only as keyboard and mouse shortcuts in the window. The app
# keeps its page in the URL, so going back would silently jump to an earlier page
# (or an earlier patient) without the app's own clean-up running. Swallow them.
BLOCK_HISTORY_NAVIGATION_JS = r"""
(function () {
  if (window.__fotNoBack) { return; }
  window.__fotNoBack = true;
  function isTyping(el) {
    if (!el) { return false; }
    var tag = (el.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || el.isContentEditable;
  }
  window.addEventListener("keydown", function (e) {
    var key = e.key;
    var altArrow = e.altKey && (key === "ArrowLeft" || key === "ArrowRight");
    var cmdBracket = e.metaKey && (key === "[" || key === "]");
    var cmdArrow = e.metaKey && (key === "ArrowLeft" || key === "ArrowRight") && !isTyping(e.target);
    var browserKey = key === "BrowserBack" || key === "BrowserForward";
    var backspace = key === "Backspace" && !isTyping(e.target);
    if (altArrow || cmdBracket || cmdArrow || browserKey || backspace) {
      e.preventDefault();
      e.stopPropagation();
    }
  }, true);
  // Mouse side buttons (3 = back, 4 = forward).
  ["mousedown", "mouseup", "auxclick"].forEach(function (type) {
    window.addEventListener(type, function (e) {
      if (e.button === 3 || e.button === 4) { e.preventDefault(); e.stopPropagation(); }
    }, true);
  });
})();
"""


def log(message):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def free_port(preferred=8501):
    """The usual Streamlit port if it is free, otherwise any free one. A browser tab
    left open from an earlier run then cannot talk to this server by mistake."""
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return sock.getsockname()[1]
    raise RuntimeError("No free local port for the app server.")


def streamlit_args(port):
    return [
        "streamlit", "run", str(APP_DIR / "app.py"),
        "--server.headless", "true",
        "--server.address", "127.0.0.1",
        "--server.port", str(port),
        "--browser.gatherUsageStats", "false",
        # A packaged build is never a Streamlit development checkout.
        "--global.developmentMode", "false",
        # Files do not change under a running copy; watching them only lets a
        # half-finished edit or a copy of the folder leave the app in a mixed state.
        "--server.fileWatcherType", "none",
        # No Deploy button or developer menu (also set in .streamlit/config.toml).
        "--client.toolbarMode", "minimal",
    ]


def serve(port):
    """Run the Streamlit server in this process (the --serve child)."""
    # The parent holds our stdin open. If it dies without stopping us (crash, killed
    # from Task Manager), stdin reaches end of file and the server exits with it, so
    # no orphan keeps the Arduino's serial port open.
    def exit_with_parent():
        try:
            while sys.stdin.read(1024):
                pass
        except Exception:
            pass
        os._exit(0)

    if sys.stdin is not None and not sys.stdin.isatty():
        threading.Thread(target=exit_with_parent, daemon=True).start()

    os.chdir(APP_DIR)
    sys.path.insert(0, str(APP_DIR))
    from streamlit.web import cli

    sys.argv = streamlit_args(port)
    sys.exit(cli.main())


def start_server(port):
    if FROZEN:
        command = [sys.executable, "--serve", str(port)]
    else:
        command = [sys.executable, str(Path(__file__).resolve()), "--serve", str(port)]
    log_file = LOG_PATH.open("a", encoding="utf-8") if LOG_PATH.parent.exists() else None
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(
        command,
        cwd=APP_DIR,
        stdin=subprocess.PIPE,
        stdout=log_file or subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )


def wait_until_ready(url, server):
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(
                f"The app server stopped while starting (exit code {server.returncode}). "
                f"See {LOG_PATH}."
            )
        try:
            with urllib.request.urlopen(f"{url}/_stcore/health", timeout=2) as reply:
                if reply.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"The app server did not start within {STARTUP_TIMEOUT_SECONDS} s.")


def stop_server(server):
    if server.poll() is not None:
        return
    try:
        server.stdin.close()
    except Exception:
        pass
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()


def probe_linux_window():
    """The --probe-window child: can Qt open a window on this Linux PC? Run apart from
    the launcher because Qt aborts the whole process, rather than raising an error,
    when a system library it needs (e.g. libxcb-cursor0) is missing."""
    from qtpy import QtWebEngineWidgets  # noqa: F401  (must precede QApplication)
    from qtpy.QtWidgets import QApplication

    QApplication([])
    sys.exit(0)


def linux_window_works():
    if FROZEN:
        command = [sys.executable, "--probe-window"]
    else:
        command = [sys.executable, str(Path(__file__).resolve()), "--probe-window"]
    try:
        result = subprocess.run(command, capture_output=True, timeout=30)
    except Exception as error:
        log(f"Could not test the app window: {error}")
        return False
    if result.returncode != 0:
        detail = (result.stderr or b"").decode(errors="replace").strip().splitlines()[-1:]
        log(f"The app window cannot open on this PC ({' '.join(detail)}).")
        return False
    return True


def find_edge():
    """msedge.exe: on PATH, or in Program Files where Windows installs it."""
    import shutil

    found = shutil.which("msedge")
    if found:
        return found
    for base in (
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramFiles"),
        os.environ.get("LOCALAPPDATA"),
    ):
        if base:
            candidate = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if candidate.exists():
                return str(candidate)
    return None


EDGE_PROFILE = APP_DIR / "data" / "edge-profile"


def edge_profile_in_use(profile=EDGE_PROFILE):
    """True while an Edge process has the app's profile open. Edge holds
    <profile>/lockfile open while running; Windows refuses to delete an open file."""
    lockfile = Path(profile) / "lockfile"
    if not lockfile.exists():
        return False
    try:
        lockfile.unlink()
    except PermissionError:
        return True
    except OSError:
        return False
    return False


def edge_app_command(edge, url):
    # Its own profile folder makes this a separate Edge process that lives exactly
    # as long as the app window, so waiting for it tells us when the window closed,
    # and the user's normal Edge windows, tabs and sign-in are untouched.
    profile = EDGE_PROFILE
    return [
        edge,
        f"--app={url}",
        f"--user-data-dir={profile}",
        f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=msEdgeStartupBoost,Translate",
        "--disable-background-mode",
    ]


def open_edge_window(url):
    """Windows: the app in an Edge app-mode window. Blocks until it is closed.
    Returns False if Edge is missing or could not start."""
    edge = find_edge()
    if not edge:
        log("Microsoft Edge was not found.")
        return False
    command = edge_app_command(edge, url)
    log(f"Opening the app window with {edge}")
    started = time.monotonic()
    try:
        result = subprocess.run(command)
    except OSError as error:
        log(f"Could not start Microsoft Edge: {error}")
        return False
    if time.monotonic() - started < 3 and result.returncode != 0:
        log(f"Microsoft Edge closed straight away (exit code {result.returncode}).")
        return False
    # Edge sometimes hands the window to another Edge process and returns at once.
    # Keep the server running until nothing has the app's profile open any more.
    while edge_profile_in_use():
        time.sleep(1)
    return True


def open_window(url):
    """Show the app in a window. Returns False if no window could be opened."""
    if sys.platform == "win32":
        return open_edge_window(url)
    if sys.platform.startswith("linux") and not linux_window_works():
        log("Opening the app in the web browser instead.")
        return False
    try:
        import webview
    except ImportError:
        log("pywebview is not installed; opening the app in the web browser instead.")
        return False

    window = webview.create_window(
        TITLE,
        url,
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        min_size=MIN_WINDOW_SIZE,
        text_select=True,
    )

    def block_history(*_):
        try:
            window.evaluate_js(BLOCK_HISTORY_NAVIGATION_JS)
        except Exception as error:
            log(f"Could not switch off back/forward shortcuts: {error}")

    window.events.loaded += block_history
    try:
        webview.start(private_mode=False)
    except Exception as error:
        log(f"Could not open the app window ({error}); using the web browser instead.")
        return False
    return True


def run_in_browser(url, server):
    webbrowser.open(url)
    print(f"The app is open in your web browser at {url}")
    print("Leave this window open while you use it; close it to stop the app.")
    try:
        server.wait()
    except KeyboardInterrupt:
        pass


SHORTCUT_MARKER = "desktop_shortcut_created"


def ensure_desktop_shortcut():
    """First run of a packaged build on Windows or Linux: put an app icon on the
    desktop (and the Linux app menu). Done once; a shortcut the user deletes is not
    put back. macOS has no desktop shortcuts: the app is dragged to the Dock."""
    marker = APP_DIR / "data" / SHORTCUT_MARKER
    if not FROZEN or marker.exists() or sys.platform == "darwin":
        return
    exe = Path(sys.executable).resolve()
    try:
        if sys.platform == "win32":
            desktop_dir = Path.home() / "Desktop"
            link = desktop_dir / f"{TITLE}.lnk"
            script = (
                "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($args[0]); "
                "$s.TargetPath = $args[1]; $s.WorkingDirectory = $args[2]; "
                "$s.IconLocation = $args[1]; $s.Description = $args[3]; $s.Save()"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", script,
                 str(link), str(exe), str(exe.parent), TITLE],
                check=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=30,
            )
        else:
            entry = (
                "[Desktop Entry]\nType=Application\n"
                f"Name={TITLE}\nExec=\"{exe}\"\nPath={exe.parent}\n"
                "Terminal=false\nCategories=Science;Medical;\n"
            )
            targets = [Path.home() / ".local" / "share" / "applications"]
            if (Path.home() / "Desktop").is_dir():
                targets.append(Path.home() / "Desktop")
            for folder in targets:
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / "fot-app.desktop"
                path.write_text(entry, encoding="utf-8")
                path.chmod(0o755)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        log("Desktop shortcut created.")
    except Exception as error:
        log(f"Could not create a desktop shortcut: {error}")


def main(argv):
    if len(argv) >= 2 and argv[0] == "--serve":
        serve(int(argv[1]))
        return
    if argv[:1] == ["--probe-window"]:
        probe_linux_window()
        return

    ensure_desktop_shortcut()

    port = free_port()
    url = f"http://127.0.0.1:{port}"
    log(f"Starting the app server on {url}")
    server = start_server(port)
    try:
        wait_until_ready(url, server)
        log("App server ready; opening the window.")
        if not open_window(url):
            run_in_browser(url, server)
    finally:
        stop_server(server)
        log("App closed.")


if __name__ == "__main__":
    main(sys.argv[1:])
