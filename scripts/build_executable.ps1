$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

if (Test-Path ".venv") {
    Write-Host "Using existing virtual environment..."
} else {
    Write-Host "Creating virtual environment..."

    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3.13 -m venv .venv
    } else {
        Write-Host "The Windows py launcher was not found. Falling back to python..."
        python -m venv .venv
    }
}

$VenvPython = ".\.venv\Scripts\python.exe"

if (!(Test-Path $VenvPython)) {
    throw "Virtual environment was not created correctly. Expected $VenvPython to exist."
}

Write-Host "Installing dependencies..."
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r requirements.txt
& $VenvPython -m pip install pyinstaller waitress

Write-Host "Building executable..."
& $VenvPython -m PyInstaller `
  --noconfirm `
  --clean `
  --onedir `
  --name MTGSiteLocal `
  --add-data "templates;templates" `
  --add-data "static;static" `
  --add-data ".env.example;." `
  --collect-submodules routes `
  --collect-submodules services `
  --collect-submodules db `
  local_launcher.py

Write-Host ""
Write-Host "Build complete."
Write-Host "Executable should be at: dist\MTGSiteLocal\MTGSiteLocal.exe"