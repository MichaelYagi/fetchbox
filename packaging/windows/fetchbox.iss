; Inno Setup script for the Windows installer (Inno Setup 6).
;   iscc /DAppVersion=1.2.3 /FFetchbox-Windows-10-11-v1.2.3-Setup packaging\windows\fetchbox.iss
; Needs dist\Fetchbox.exe, dist\README.txt, dist\LICENSE.txt, dist\THIRD-PARTY-NOTICES.txt
; and build\icons\fetchbox.ico (packaging\build-windows.ps1 plus the release workflow).
;
; Installs for the current user only: no administrator prompt, into
; %LOCALAPPDATA%\Programs\Fetchbox. Your data (%LOCALAPPDATA%\Fetchbox) is separate and
; kept unless you say otherwise when uninstalling; downloaded media is never touched.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define Root SourcePath + "..\.."

[Setup]
; Never change the AppId: Windows uses it to recognise upgrades of the same app.
AppId={{0E7AF37C-2293-4532-9903-F126BBDBAF33}
AppName=Fetchbox
AppVersion={#AppVersion}
AppVerName=Fetchbox {#AppVersion}
AppPublisher=Fetchbox
AppPublisherURL=https://github.com/MichaelYagi/fetchbox
AppSupportURL=https://github.com/MichaelYagi/fetchbox/issues
VersionInfoVersion={#AppVersion}
PrivilegesRequired=lowest
DefaultDirName={autopf}\Fetchbox
DisableProgramGroupPage=yes
DisableDirPage=auto
LicenseFile={#Root}\dist\LICENSE.txt
SetupIconFile={#Root}\build\icons\fetchbox.ico
UninstallDisplayIcon={app}\Fetchbox.exe
UninstallDisplayName=Fetchbox
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
OutputDir={#Root}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; A running Fetchbox holds Fetchbox.exe open: close it (Restart Manager) to upgrade.
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: startup; Description: "Start Fetchbox when I sign in to Windows"
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#Root}\dist\Fetchbox.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\dist\README.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\dist\LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\dist\THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Fetchbox"; Filename: "{app}\Fetchbox.exe"
Name: "{autodesktop}\Fetchbox"; Filename: "{app}\Fetchbox.exe"; Tasks: desktopicon

[Registry]
; The same entry the tray's "Start at login" toggle writes and reads.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
  ValueName: "Fetchbox"; ValueData: """{app}\Fetchbox.exe"""; Tasks: startup; \
  Flags: uninsdeletevalue

[Run]
Filename: "{app}\Fetchbox.exe"; Description: "Start Fetchbox now"; \
  Flags: nowait postinstall skipifsilent
; An update from the tray runs silently with /RELAUNCH=1: start Fetchbox again after.
Filename: "{app}\Fetchbox.exe"; Flags: nowait; Check: Relaunch

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';

function Relaunch: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  { An update from the tray: whatever is left of the old Fetchbox must not hold
    Fetchbox.exe open (the tray has asked the server to stop and is quitting). }
  if Relaunch then
  begin
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM Fetchbox.exe', '', SW_HIDE,
      ewWaitUntilTerminated, Code);
    Sleep(1000);
  end;
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
  Data: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    { Stop Fetchbox (the tray and the server it started) so its files can go. }
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM Fetchbox.exe', '', SW_HIDE,
      ewWaitUntilTerminated, Code);
    { The tray's toggle may have added the login entry without the installer. }
    RegDeleteValue(HKCU, RunKey, 'Fetchbox');
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    Data := ExpandConstant('{localappdata}\Fetchbox');
    if DirExists(Data) and not UninstallSilent() then
      if MsgBox('Also delete your Fetchbox settings and library database?' + #13#10#13#10 +
                Data + #13#10#13#10 +
                'Your movies, shows and downloads are not affected either way. ' +
                'Keep this if you might install Fetchbox again.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Data, True, True, True);
  end;
end;
