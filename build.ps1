$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create build environment' }
}
$python = '.\.venv\Scripts\python.exe'
& $python -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw 'Failed to install build dependencies' }
& $python -m PyInstaller --noconfirm --clean --onefile --windowed --name N1RadioConfigurator --collect-all bleak --add-data 'apk_tone_lists.json:.' radio_configurator.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
Write-Host 'Built: dist\N1RadioConfigurator.exe'
