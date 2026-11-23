"""Builds the double-click app for the computer it runs on.

    python3 build_app.py        (macOS / Linux)
    py build_app.py             (Windows)

Run it once on each kind of computer you want the app on; a Windows app has to be
built on Windows, a Mac app on a Mac. Needs Python 3.11 - 3.14. It makes its own
build environment in FOT_App/.build (the first build downloads PyInstaller and the
app's packages, a few minutes), then puts the finished app in the FOT_App folder:

    Windows:  FOT App.exe   (its Python files go in _internal, next to app_src)
    macOS:    FOT App.app   (self-contained; drag it to the Dock if you like)
    Linux:    FOT App       (a single file)

Every build uses the code and the patient data in FOT_App/_internal/app_src, which
this script never changes.

    python build_app.py --package

also zips a ready-to-use FOT_App folder into .build/ for handing out: the app plus
app_src's code, WITHOUT the patient database or the data folder (patient
recordings and this device's calibration). Unzipping it over an existing FOT_App
folder therefore updates the app and keeps that computer's patients and
calibration. GitHub Actions runs this on Windows, macOS and Linux (see
.github/workflows/build-app.yml). After changing the code, the app does NOT need
rebuilding: it runs app_src as it is. Rebuild only after changing requirements.txt
or desktop.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

APP_SRC = Path(__file__).resolve().parent
ROOT = APP_SRC.parent.parent  # FOT_App
INTERNAL = ROOT / "_internal"
BUILD = ROOT / ".build"
BUILD_VENV = BUILD / "venv"
NAME = "FOT App"
OLD_WINDOWS_LAUNCHER = ROOT / "Start FOT App (Windows).exe"

SUPPORTED = {(3, 11), (3, 12), (3, 13), (3, 14)}

PYINSTALLER_OPTIONS = [
    "--noconfirm",
    "--windowed",  # no console window
    "--name", NAME,
    # Streamlit reads its own web files, version metadata and many lazily imported
    # modules at run time; collect all of it.
    "--collect-all", "streamlit",
    "--collect-all", "plotly",
    "--collect-all", "altair",
    "--collect-all", "webview",
    # Imported by the app's own code (app_src), which PyInstaller does not scan
    # because it stays as plain files next to the app.
    "--hidden-import", "numpy",
    "--hidden-import", "pandas",
    "--hidden-import", "pyarrow",
    "--hidden-import", "serial",
    "--hidden-import", "serial.tools.list_ports",
    "--hidden-import", "sqlite3",
    # Only used by a standalone calibration script, not the app.
    "--exclude-module", "matplotlib",
    "--exclude-module", "tkinter",
]

# pywebview drives a different web viewer on each system, imported only when the
# window opens, so PyInstaller has to be told about it.
PLATFORM_OPTIONS = {
    # Edge WebView2 through .NET (pythonnet).
    "win32": ["--hidden-import", "clr", "--collect-all", "pythonnet",
              "--collect-all", "clr_loader"],
    # Qt WebEngine.
    "linux": ["--hidden-import", "qtpy",
              "--hidden-import", "PyQt6.QtWebEngineWidgets",
              "--hidden-import", "PyQt6.QtWebEngineCore",
              "--hidden-import", "PyQt6.QtWebChannel",
              "--hidden-import", "PyQt6.QtNetwork"],
    # WebKit through PyObjC; collected with webview.
    "darwin": ["--osx-bundle-identifier", "au.edu.fot.oscillometry"],
}

# Left out of a --package zip: patient data, this device's calibration, logs, and
# files only developers need. Names at the top of app_src only: the app has its own
# frontend/views/tests package, which must stay.
PACKAGE_EXCLUDE_TOP = {"system.db", "data", "tests", ".pytest_cache", "build_app.py"}
# Left out at any depth.
PACKAGE_EXCLUDE_ANYWHERE = {"__pycache__", ".DS_Store"}


def _package_ignore(directory, names):
    top = Path(directory).resolve() == APP_SRC
    return [
        name for name in names
        if name in PACKAGE_EXCLUDE_ANYWHERE or (top and name in PACKAGE_EXCLUDE_TOP)
    ]


def say(message):
    print(f"\n== {message}", flush=True)


def build_python():
    return BUILD_VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def ensure_build_environment():
    """Re-run this script inside FOT_App/.build/venv with everything installed."""
    if Path(sys.prefix).resolve() == BUILD_VENV.resolve():
        return False
    if sys.version_info[:2] not in SUPPORTED:
        sys.exit(
            f"Python {sys.version_info[0]}.{sys.version_info[1]} is not supported; "
            "use Python 3.11 - 3.14."
        )
    if not build_python().exists():
        say("Creating the build environment")
        venv.EnvBuilder(with_pip=True, clear=True).create(BUILD_VENV)
    python = str(build_python())
    say("Installing the app's packages and PyInstaller")
    subprocess.check_call([python, "-m", "pip", "install", "--upgrade", "pip"])
    subprocess.check_call([
        python, "-m", "pip", "install",
        "--only-binary=:all:", "--no-binary=proxy-tools",
        "-r", str(APP_SRC / "requirements.txt"),
        "pyinstaller>=6.10",
    ])
    subprocess.check_call([python, str(Path(__file__).resolve())] + sys.argv[1:])
    return True


def run_pyinstaller():
    say("Building with PyInstaller")
    dist, work = BUILD / "dist", BUILD / "work"
    shutil.rmtree(dist, ignore_errors=True)
    platform = "linux" if sys.platform.startswith("linux") else sys.platform
    options = list(PYINSTALLER_OPTIONS) + PLATFORM_OPTIONS.get(platform, [])
    if platform == "linux":
        # One file, so it cannot clash with a Windows build's _internal folder when
        # the same FOT_App folder is shared between computers.
        options.append("--onefile")
    subprocess.check_call([
        sys.executable, "-m", "PyInstaller", *options,
        "--distpath", str(dist), "--workpath", str(work), "--specpath", str(BUILD),
        str(APP_SRC / "desktop.py"),
    ])
    return dist


def install_windows(dist):
    """FOT App.exe plus its Python runtime, which PyInstaller puts in _internal.
    That is the folder app_src lives in, so replace everything there except
    app_src."""
    built = dist / NAME
    say(f"Installing {NAME}.exe")
    for item in INTERNAL.iterdir():
        if item.name != "app_src":
            shutil.rmtree(item) if item.is_dir() else item.unlink()
    for item in (built / "_internal").iterdir():
        target = INTERNAL / item.name
        shutil.copytree(item, target) if item.is_dir() else shutil.copy2(item, target)
    shutil.copy2(built / f"{NAME}.exe", ROOT / f"{NAME}.exe")
    if OLD_WINDOWS_LAUNCHER.exists():
        OLD_WINDOWS_LAUNCHER.unlink()
        print(f"Removed the old {OLD_WINDOWS_LAUNCHER.name}.")
    return ROOT / f"{NAME}.exe"


def install_macos(dist):
    target = ROOT / f"{NAME}.app"
    say(f"Installing {target.name}")
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(dist / f"{NAME}.app", target, symlinks=True)
    return target


def install_linux(dist):
    target = ROOT / NAME
    say(f"Installing {NAME}")
    shutil.copy2(dist / NAME, target)
    target.chmod(0o755)
    return target


def platform_label():
    return {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, "Linux")


def package(app):
    """Zip FOT_App/ (app + code, no patient data) to .build/FOT_App-<system>.zip."""
    say("Packaging")
    stage = BUILD / "package"
    folder = stage / "FOT_App"
    shutil.rmtree(stage, ignore_errors=True)
    (folder / "_internal").mkdir(parents=True)
    shutil.copy2(ROOT / "READ ME FIRST.txt", folder / "READ ME FIRST.txt")
    if app.suffix == ".app":
        shutil.copytree(app, folder / app.name, symlinks=True)
    else:
        shutil.copy2(app, folder / app.name)
    if sys.platform == "win32":
        for item in INTERNAL.iterdir():
            if item.name == "app_src":
                continue
            target = folder / "_internal" / item.name
            shutil.copytree(item, target) if item.is_dir() else shutil.copy2(item, target)
    shutil.copytree(
        APP_SRC, folder / "_internal" / "app_src",
        ignore=_package_ignore,
    )
    archive = BUILD / f"FOT_App-{platform_label()}.zip"
    archive.unlink(missing_ok=True)
    if sys.platform == "darwin":
        # ditto keeps the symbolic links inside the .app, which zipfile would break.
        subprocess.check_call(
            ["ditto", "-c", "-k", "--norsrc", "--keepParent", str(folder), str(archive)]
        )
    else:
        shutil.make_archive(str(archive.with_suffix("")), "zip", stage, "FOT_App")
    print(f"Wrote {archive}")
    return archive


def main():
    if ensure_build_environment():
        return
    dist = run_pyinstaller()
    if sys.platform == "win32":
        app = install_windows(dist)
    elif sys.platform == "darwin":
        app = install_macos(dist)
    else:
        app = install_linux(dist)
    say(f"Done: {app}")
    print("Double-click it to start the app.")
    if "--package" in sys.argv[1:]:
        package(app)


if __name__ == "__main__":
    main()
