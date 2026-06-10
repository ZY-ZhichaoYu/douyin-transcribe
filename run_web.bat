@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating .venv ...
    py -3 -m venv .venv
    if errorlevel 1 (
        python -m venv .venv
    )
    if errorlevel 1 (
        echo Failed to create Python virtual environment. Please install Python 3.10 or newer.
        pause
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"

python -c "import importlib.metadata as m; from packaging.version import Version; import gradio, playwright, faster_whisper, yt_dlp, mcp; raise SystemExit(Version(m.version('gradio')) < Version('6.14') or Version(m.version('yt-dlp')) < Version('2026.6.9'))" >nul 2>nul
if errorlevel 1 (
    echo Installing Python dependencies ...
    python -m pip install --upgrade pip
    python -m pip install --upgrade -r requirements.txt
    if errorlevel 1 (
        echo Dependency installation failed.
        pause
        exit /b 1
    )
)

echo Checking Playwright Chromium ...
python -m playwright install chromium
if errorlevel 1 (
    echo Playwright Chromium installation failed.
    pause
    exit /b 1
)

python app.py
pause
