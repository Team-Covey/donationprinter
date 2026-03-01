; Inno Setup script for Donation Receipt Printer installer.
; Compile with ISCC.exe installer.iss

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#define AppName "Donation Receipt Printer"
#define AppExeName "DonationReceiptPrinter.exe"
#define AppPublisher "Donation Printer"
#define AppURL "https://streamlabs.com"

[Setup]
AppId={{9B2692AA-EA38-49E2-8DAB-15A2A5E9A45E}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
DefaultDirName={localappdata}\Programs\Donation Receipt Printer
DefaultGroupName=Donation Receipt Printer
DisableProgramGroupPage=yes
OutputDir=installer_dist
OutputBaseFilename=DonationReceiptPrinter-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Donation Receipt Printer"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\Donation Receipt Printer"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch Donation Receipt Printer"; Flags: nowait postinstall skipifsilent
