# Donation Receipt Printer

Local Windows app that listens for incoming Streamlabs donation events and prints each donation to a selected receipt printer.

It prints:
- donor username
- donation message
- donation amount (if provided by Streamlabs)

## What you need

- Windows PC
- Python 3.10+ installed
- Streamlabs API access token with permission to request a socket token
- Installed printer driver for your receipt printer

## Run from source

```powershell
python -m pip install -r requirements.txt
python app.py
```

## Build `.exe`

```powershell
.\build_exe.ps1
```

After build, your executable is in:

`.\dist\DonationReceiptPrinter.exe`

## Build installer (`Setup.exe`)

This creates a standard Windows installer you can send to another person.

1. Install Inno Setup once:

```powershell
winget install --id JRSoftware.InnoSetup -e
```

2. Build installer:

```powershell
.\build_installer.ps1
```

Installer output:

`.\installer_dist\DonationReceiptPrinter-Setup.exe`

## How to use

1. Launch the app.
2. Paste your Streamlabs access token.
3. Pick your receipt printer.
4. Click `Save Config`.
5. Click `Start Listening`.
6. Use `Test Print` to verify printer formatting before going live.

## Notes

- The app uses Streamlabs Socket API so it can run locally without opening inbound ports.
- Printer output uses raw ESC/POS commands and should work for most thermal receipt printers.
- Tuned defaults for `RW80L MKII`:
  - 48 columns (Font A on 80mm paper)
  - ESC/POS partial cut command
  - extra feed before cut to reduce jams
- Config is stored per-user at:
  - `%APPDATA%\DonationReceiptPrinter\config.json`
