param(
    [string]$OutputName = "DonationReceiptPrinter"
)

$ErrorActionPreference = "Stop"

Write-Host "Installing dependencies..."
python -m ensurepip --upgrade
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

Write-Host "Building single-file executable..."
python -m PyInstaller `
    --name $OutputName `
    --onefile `
    --windowed `
    app.py

Write-Host ""
Write-Host "Build complete."
Write-Host "Executable: .\\dist\\$OutputName.exe"
