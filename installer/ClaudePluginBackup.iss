; Installer di Plugin Backup per Claude Code — Inno Setup 6 (https://jrsoftware.org/isinfo.php)
; Non compilare a mano: lo fa build.py, che passa la versione con /DAppVersion=...

#ifndef AppVersion
  #define AppVersion "1.0"
#endif
#define AppName "Plugin Backup per Claude Code"
#define AppExe "ClaudePluginBackup.exe"
#define AppPublisher "Cesa028"
#ifndef SrcDir
  #define SrcDir "..\build\package"
#endif

[Setup]
AppId={{7B1D6E52-3F0A-4C8E-9A57-2D4E81C9B6F3}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppCopyright=© 2026 {#AppPublisher} — Licenza MIT
VersionInfoVersion={#AppVersion}
VersionInfoDescription=Plugin Backup Setup
DefaultDirName={autopf}\Plugin Backup
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; installazione senza permessi di amministratore (solo per l'utente),
; con possibilità di scegliere "per tutti gli utenti"
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile=license-page.txt
SetupIconFile=..\build\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
WizardSizePercent=110
Compression=lzma2/ultra64
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
OutputDir=..\dist
OutputBaseFilename=ClaudePluginBackup-Setup-{#AppVersion}

[Languages]
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[UninstallDelete]
; alla disinstallazione rimuove le impostazioni del programma (i backup in Documenti restano!)
Type: filesandordirs; Name: "{userappdata}\ClaudePluginBackup"
Type: files; Name: "{userdesktop}\Plugin Backup.lnk"

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
