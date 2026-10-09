$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$buildPython = Get-Command py -ErrorAction Stop
$venvPython = Join-Path $PSScriptRoot ".venv-build\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    & $buildPython.Source -3.13 -m venv ".venv-build"
}

& $venvPython -m pip install --disable-pip-version-check -r "requirements-dev.txt"
& $venvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "LUT Processor" `
    --hidden-import vpk `
    "lut_processor_gui.py"

Copy-Item -LiteralPath ".\dist\LUT Processor.exe" -Destination ".\LUT Processor.exe" -Force
Write-Host "Built: $PSScriptRoot\LUT Processor.exe"
