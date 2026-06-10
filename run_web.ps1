Set-Location $PSScriptRoot

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "Creating .venv ..."
    py -3 -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        python -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create Python virtual environment. Please install Python 3.10 or newer."
    }
}

. ".\.venv\Scripts\Activate.ps1"

python -c "import importlib.metadata as m; from packaging.version import Version; import gradio, playwright, faster_whisper, yt_dlp, mcp; raise SystemExit(Version(m.version('gradio')) < Version('6.14') or Version(m.version('yt-dlp')) < Version('2026.6.9'))" *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing Python dependencies ..."
    python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }

    python -m pip install --upgrade -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
}

Write-Host "Checking Playwright Chromium ..."
python -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    throw "Playwright Chromium installation failed."
}

python app.py
