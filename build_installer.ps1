param(
    [string]$AppVersion = "1.0.0",
    [switch]$SkipExeBuild
)

$ErrorActionPreference = "Stop"

if (-not $SkipExeBuild) {
    Write-Host "Building app executable..."
    .\build_exe.ps1
}

if (-not (Test-Path ".\dist\DonationReceiptPrinter.exe")) {
    throw "Missing dist\DonationReceiptPrinter.exe. Run .\build_exe.ps1 first."
}

$candidates = @()
$onPath = Get-Command iscc -ErrorAction SilentlyContinue
if ($onPath) {
    $candidates += $onPath.Source
}

if ($env:ProgramFiles) {
    $candidates += (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
}
if (${env:ProgramFiles(x86)}) {
    $candidates += (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
}

$iscc = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) {
    throw @"
Inno Setup (ISCC.exe) was not found.
Install it, then run this script again.

Recommended command:
  winget install --id JRSoftware.InnoSetup -e
"@
}

Write-Host "Using Inno Setup compiler: $iscc"
& $iscc ".\installer.iss" "/DAppVersion=$AppVersion"

Write-Host ""
Write-Host "Installer build complete."
Write-Host "Output: .\installer_dist\DonationReceiptPrinter-Setup.exe"
