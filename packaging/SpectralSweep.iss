; Build with tools/build_installer.py after the portable EXE smoke succeeds.
#ifndef SourceDir
  #error SourceDir is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
#ifndef ReleaseVersion
  #error ReleaseVersion is required
#endif
#ifndef FileVersion
  #define FileVersion "0.0.0.0"
#endif

[Setup]
AppId={code:InstallerAppId}
UsePreviousLanguage=no
AppName=SpectralSweep
AppVersion={#ReleaseVersion}
AppPublisher=SpectralSweep
AppPublisherURL=https://github.com/ylylyl98/SpectralSweep-pyside6
AppSupportURL=https://github.com/ylylyl98/SpectralSweep-pyside6/issues
AppUpdatesURL=https://github.com/ylylyl98/SpectralSweep-pyside6/releases
DefaultDirName={localappdata}\Programs\SpectralSweep
DefaultGroupName=SpectralSweep
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=SpectralSweep-{#ReleaseVersion}-windows-x64-Setup
SetupIconFile={#SourceDir}\_internal\assets\icons\spectralsweep.ico
UninstallDisplayIcon={app}\SpectralSweep.exe
VersionInfoVersion={#FileVersion}
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
AllowNoIcons=yes
CloseApplications=no
RestartApplications=no
AlwaysRestart=no
RestartIfNeededByRun=no
SetupLogging=yes
Uninstallable=yes
UninstallLogging=yes

[Tasks]
Name: desktopicon; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\SpectralSweep"; Filename: "{app}\SpectralSweep.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\SpectralSweep"; Filename: "{app}\SpectralSweep.exe"; WorkingDir: "{app}"; Tasks: desktopicon

; No [Run], [InstallDelete] or [UninstallDelete]: never start acquisition after
; setup, remove user profiles/data, or queue replacement of locked files.
[Code]
function InstallerAppId(Param: String): String;
var
  VerificationId: String;
  I: Integer;
begin
  Result := 'SpectralSweep.Desktop.Windows.x64';
  VerificationId := ExpandConstant('{param:VerificationId|}');
  if VerificationId = '' then Exit;
  { CI uses the exact release installer, but an independent uninstall registry
    identity. It cannot overwrite the user's real installed-instance record. }
  if Length(VerificationId) <> 36 then
    RaiseException('Invalid installer verification identity');
  for I := 1 to Length(VerificationId) do
    if Pos(VerificationId[I], '0123456789abcdef-') = 0 then
      RaiseException('Invalid installer verification identity');
  Result := 'SpectralSweep.Verification.' + VerificationId;
end;

function RunningApplicationProblem: String;
var
  Locator, Services, Processes: Variant;
begin
  Result := '';
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Services := Locator.ConnectServer('', 'root\CIMV2');
    Processes := Services.ExecQuery(
      'SELECT Name FROM Win32_Process WHERE Name="SpectralSweep.exe" OR Name="SpectralSweep-worker.exe"');
    if Processes.Count > 0 then
      Result := 'SpectralSweep or its background worker is running. Stop acquisition and close SpectralSweep normally, then retry. Setup will not close instrument processes.';
  except
    Result := 'Setup could not check whether SpectralSweep is running. Installation is stopped to protect acquisition. Check the Windows Management Instrumentation service and retry.';
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  NeedsRestart := False;
  Result := RunningApplicationProblem;
end;

function InitializeUninstall: Boolean;
var
  Problem: String;
begin
  Problem := RunningApplicationProblem;
  Result := Problem = '';
  if not Result then
    SuppressibleMsgBox(Problem, mbError, MB_OK, IDOK);
end;
