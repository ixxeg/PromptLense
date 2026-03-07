param(
    [switch]$OneFile
)

$ErrorActionPreference = 'Stop'

Set-Location -Path $PSScriptRoot

Write-Host 'Installing build dependency (PyInstaller)...'
py -m pip install pyinstaller

$modeArgs = @('--onedir')
if ($OneFile) {
    $modeArgs = @('--onefile')
}

Write-Host "Building EXE ($($modeArgs[0]))..."
py -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name 'PromptLens' `
  $modeArgs `
  app.py

Write-Host ''
Write-Host 'Build complete.'
Write-Host 'Output folder:'
if ($OneFile) {
    Write-Host "  $PSScriptRoot\\dist"
} else {
    Write-Host "  $PSScriptRoot\\dist\\PromptLens"
}
