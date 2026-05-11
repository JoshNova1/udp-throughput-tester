; ===========================================================================
; Throughput Tester -- Inno Setup installer
;
; Build:    iscc deploy\installer.iss
;           (or run deploy\build-installer.ps1 to do the whole pipeline)
;
; Output:   dist\ThroughputTester-Setup-<version>.exe
;
; Defaults to a per-user install (no admin needed). The user can opt-in to
; the firewall rule and the per-machine install via the wizard.
; ===========================================================================

#define AppName       "Nova Connect Throughput Tester"
#define AppShortName  "ThroughputTester"
#define AppPublisher  "Nova Connect"
; AppVersion can be overridden from the build script:
;     ISCC.exe /DAppVersion=1.2.3 installer.iss
; The CI workflow passes the auto-derived 1.0.<commit-count> here so the
; Setup-X.Y.Z.exe filename, Apps&Features entry, and in-app version
; constant all stay in lockstep.
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#define AppExeName    "UDPThroughputTester.exe"
#define AppId         "{{A2C7F23D-9F1B-4F62-8F1E-3BC0D6F2A001}"
#define SourceDir     "..\dist\UDPThroughputTester"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=https://novaconnect.tech
AppSupportURL=https://novaconnect.tech
AppUpdatesURL=https://novaconnect.tech
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany={#AppPublisher}
VersionInfoDescription=Pre-event UDP/SRT throughput qualification tool
VersionInfoCopyright=Copyright (C) {#AppPublisher}

; Per-user install by default. User can elevate via /ALLUSERS at the command line.
DefaultDirName={autopf}\{#AppShortName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; UI / output
OutputDir=..\dist
OutputBaseFilename=ThroughputTester-Setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
DisableReadyPage=no
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName} {#AppVersion}

; Modern look. Set the line below to point at a .ico file to brand the
; installer; comment it out to use Inno Setup's default. To generate one
; from the Nova Connect mark:
;
;   magick -density 256 -background none -resize 256x256 ^
;          ..\static\images\nova-connect-mark.svg ^
;          nova-connect.ico
;
; (ImageMagick on Windows: choco install imagemagick or download from
; imagemagick.org. Or use any SVG-to-ICO online converter.)
#if FileExists("nova-connect.ico")
  SetupIconFile=nova-connect.ico
#endif
WizardImageFile=
WizardSmallImageFile=
ChangesAssociations=no
AlwaysShowComponentsList=no

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; \
  Description: "Create a desktop shortcut"; \
  GroupDescription: "Additional shortcuts:"; \
  Flags: unchecked

Name: "firewall"; \
  Description: "Add Windows Firewall rules for the test ports (recommended)"; \
  GroupDescription: "Network:"; \
  Check: IsAdminInstallMode

[Files]
; Everything PyInstaller produced -- including bundled ffmpeg / iperf3 in ./bin/
Source: "{#SourceDir}\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

; Inno can't reference files that don't exist at compile time, so we use
; external sources guarded by a check. The build pipeline writes them in.
; Optional README / LICENSE -- included if present.
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
  Comment: "Pre-event UDP/SRT throughput qualification"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
  Tasks: desktopicon
Name: "{autoprograms}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

[Run]
; Firewall rules (admin only) -- pre-authorise the listening ports so the user
; doesn't see the Defender popup on first launch. ports: 8080 (TCP, web UI),
; 5201/9000-9003/9100 (UDP, test traffic).
Filename: "netsh"; \
  Parameters: "advfirewall firewall add rule name=""{#AppName} (TCP 8080)"" dir=in action=allow protocol=TCP localport=8080 program=""{app}\{#AppExeName}"" enable=yes"; \
  Flags: runhidden; \
  Tasks: firewall; \
  StatusMsg: "Adding firewall rule (TCP 8080)..."

Filename: "netsh"; \
  Parameters: "advfirewall firewall add rule name=""{#AppName} (UDP test ports)"" dir=in action=allow protocol=UDP localport=5201,9000-9003,9100 program=""{app}\{#AppExeName}"" enable=yes"; \
  Flags: runhidden; \
  Tasks: firewall; \
  StatusMsg: "Adding firewall rule (UDP test ports)..."

; Launch the app right after install (optional checkbox on final page).
Filename: "{app}\{#AppExeName}"; \
  Description: "Launch {#AppName} now"; \
  Flags: postinstall nowait skipifsilent

[UninstallRun]
; Remove the firewall rules we added.
Filename: "netsh"; \
  Parameters: "advfirewall firewall delete rule name=""{#AppName} (TCP 8080)"""; \
  Flags: runhidden

Filename: "netsh"; \
  Parameters: "advfirewall firewall delete rule name=""{#AppName} (UDP test ports)"""; \
  Flags: runhidden

[Code]
function IsAdminInstallMode(): Boolean;
begin
  Result := IsAdmin();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
  Choice: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // Ask whether to remove per-user data (history, logs, clips).
    DataDir := ExpandConstant('{localappdata}\NovaConnect\ThroughputTester');
    if DirExists(DataDir) then
    begin
      Choice := MsgBox(
        'Remove saved data, history and logs?' #13#13 +
        'Location: ' + DataDir + #13#13 +
        'Choose No to keep them for a future reinstall.',
        mbConfirmation, MB_YESNO);
      if Choice = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
