@echo off
setlocal
cd /d "%~dp0"

if exist "LUT Processor.exe" (
    start "" "LUT Processor.exe"
    exit /b 0
)

where py >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.11 or newer, then run this file again.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv || exit /b 1
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" lut_processor_gui.py
