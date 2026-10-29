$ErrorActionPreference = "Stop"

$python = $env:LITEGALLERY_PYTHON
if (-not $python) {
    $python = "S:\Python314\python.exe"
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python was not found: $python"
}

$env:TEMP = "S:\Python314\Temp"
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = "S:\Python314\Cache\pip"
$env:PYINSTALLER_CONFIG_DIR = "S:\Python314\Cache\pyinstaller"
New-Item -ItemType Directory -Force -Path $env:TEMP, $env:PIP_CACHE_DIR, $env:PYINSTALLER_CONFIG_DIR | Out-Null

Push-Location $PSScriptRoot
try {
    & $python -m pip install -e ".[dev,build]"
    if ($LASTEXITCODE -ne 0) {
        throw "Installing build dependencies failed."
    }

    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --windowed `
        --name LiteGallery `
        --add-data "lightroom_plugin;lightroom_plugin" `
        --collect-all rawpy `
        --collect-all psd_tools `
        run_litegallery.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed."
    }
}
finally {
    Pop-Location
}

Write-Output "Build output: dist\LiteGallery\LiteGallery.exe"
