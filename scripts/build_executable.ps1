# scripts/build_executable.ps1

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir

Set-Location $RepoRoot

$VenvDir = Join-Path $RepoRoot ".venv"
$PythonExe = Join-Path $VenvDir "Scripts\python.exe"
$RequirementsFile = Join-Path $RepoRoot "requirements.txt"
$LauncherFile = Join-Path $RepoRoot "local_launcher.py"

Write-Host "Repo root: $RepoRoot"
Write-Host "Virtual environment: $VenvDir"

if (-not (Test-Path $VenvDir)) {
    Write-Host "Creating virtual environment..."
    python -m venv $VenvDir
}

if (-not (Test-Path $PythonExe)) {
    throw "Could not find venv Python at: $PythonExe"
}

Write-Host "Upgrading pip..."
& $PythonExe -m pip install --upgrade pip

if (Test-Path $RequirementsFile) {
    Write-Host "Installing dependencies from requirements.txt..."
    & $PythonExe -m pip install -r $RequirementsFile
}
else {
    Write-Warning "requirements.txt not found at: $RequirementsFile"
    Write-Warning "Skipping requirements install."
}

Write-Host "Installing build dependencies..."
& $PythonExe -m pip install pyinstaller waitress

if (-not (Test-Path $LauncherFile)) {
    throw "Could not find local_launcher.py at: $LauncherFile"
}

Write-Host "Cleaning previous build output..."

$DistDir = Join-Path $RepoRoot "dist"
$BuildDir = Join-Path $RepoRoot "build"

if (Test-Path $DistDir) {
    Remove-Item $DistDir -Recurse -Force
}

if (Test-Path $BuildDir) {
    Remove-Item $BuildDir -Recurse -Force
}

Write-Host "Building executable..."

& $PythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --console `
    --name MTGSiteLocal `
    --onedir `
    --collect-data tzdata `
    --collect-submodules services `
    --collect-submodules py7zr `
    --collect-submodules pyppmd `
    --collect-submodules pybcj `
    --collect-submodules Cryptodome `
    --collect-submodules backports.zstd `
    --collect-binaries backports.zstd `
    --hidden-import backports.zstd `
    --add-data "templates;templates" `
    --add-data "static;static" `
    --add-data "data\app_settings.json;data" `
    --add-data ".env.example;." `
    local_launcher.py

$ExePath = Join-Path $RepoRoot "dist\MTGSiteLocal\MTGSiteLocal.exe"

if (-not (Test-Path $ExePath)) {
    throw "Build failed. Expected executable was not created at: $ExePath"
}

Write-Host ""
Write-Host "Build complete."
Write-Host "Executable created at: $ExePath"