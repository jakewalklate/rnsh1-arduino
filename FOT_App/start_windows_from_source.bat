@echo off
rem Runs the app from source with an installed Python, instead of FOT_App.exe.
rem Only needed after changing the code without rebuilding the .exe.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up the Python environment...
  rem Packages ship ready-made wheels for Python 3.11-3.14; newer versions fail to install.
  py -3.14 -m venv .venv || py -3.13 -m venv .venv || py -3.12 -m venv .venv || py -3.11 -m venv .venv || python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --only-binary=:all: -r requirements.txt
)
".venv\Scripts\python.exe" run_app.py
pause
