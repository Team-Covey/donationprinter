# Donation Receipt Printer

Local Windows app that listens for incoming Streamlabs donation events and prints each donation to a selected receipt printer.

It prints:
- donor username
- donation message
- donation amount (if provided by Streamlabs)

## What you need

- Windows PC
- Python 3.10+ installed
- Streamlabs developer app (`Client ID` + `Client Secret`)
- Redirect URI set in your Streamlabs app, for example:
  - `http://127.0.0.1:53177/callback`
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
2. Enter your Streamlabs `Client ID` and `Client Secret`.
3. Confirm Redirect URI matches your Streamlabs app settings.
4. Click `Connect Streamlabs` and approve access in browser.
5. Pick your receipt printer.
6. Set `Print Mode`:
   - `auto` for smart default routing
   - `raw` for ESC/POS receipt printers
   - `windows` for normal printers and `Microsoft Print to PDF`
7. Click `Save Config`.
8. Click `Start Listening`.
9. Use `Test Print` to verify printer formatting before going live.

## Notes

- The app uses Streamlabs Socket API so it can run locally without opening inbound ports.
- `Connect Streamlabs` uses OAuth authorization-code flow and requests:
  - `socket.token`
  - `donations.read`
- `Print Mode = windows` supports `Microsoft Print to PDF` for local no-printer testing.
- Printer output uses raw ESC/POS commands for receipt printers (`Print Mode = raw`).
- Tuned defaults for `RW80L MKII`:
  - 48 columns (Font A on 80mm paper)
  - ESC/POS partial cut command
  - extra feed before cut to reduce jams
- Config is stored per-user at:
  - `%APPDATA%\DonationReceiptPrinter\config.json`
