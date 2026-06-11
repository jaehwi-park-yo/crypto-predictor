@echo off
title BTC Grid Prediction - Local Launcher
cd /d "%~dp0"

set VENV_DIR=.venv
set API_PORT=8000

echo ====================================================
echo  BTC Grid Prediction System  -  Local Launcher
echo ====================================================

where python > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install from https://www.python.org/downloads/
    echo         Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [1/4] Creating virtual environment...
    python -m venv %VENV_DIR%
)

call %VENV_DIR%\Scripts\activate.bat

echo [2/4] Installing dependencies...
rem python -m pip avoids running pip.exe directly (blocked by some
rem Device Guard / AppLocker policies on managed PCs)
python -m pip install -q -r requirements.txt

if not exist "data" mkdir data

echo [3/4] Starting API server (http://localhost:%API_PORT%) ...
start "API Server" cmd /k "call %VENV_DIR%\Scripts\activate.bat && python api_server.py"

timeout /t 3 /nobreak > nul

echo [4/4] Opening dashboard (index.html) ...
start "" "%~dp0index.html"

echo.
echo  API server : http://localhost:%API_PORT%/api/health
echo  Dashboard  : index.html (opened in browser)
echo.
echo  Optional Streamlit UI: streamlit run gui/app.py
echo.
echo  To stop: close the "API Server" window.
pause
