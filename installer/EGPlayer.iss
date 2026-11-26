; EG Player installer (Inno Setup). Built by release.ps1:
;   ISCC.exe /DAppVersion=1.2.0 installer\EGPlayer.iss
; Installs per-user by default (no admin prompt) into %LOCALAPPDATA%\Programs\EG Player, so the
; in-app updater can replace it silently. "Install for all users" is offered as an option.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "EG Player"
#define AppExe "EGPlayer.exe"
#define SrcDir "..\dist\EGPlayer"

[Setup]
AppId={{8E3B6D2A-5C1F-4B7E-9A2D-3F6C1E8B4A90}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=EG Player
AppComments=Fast video player with frame-accurate scrubbing, any-percentage speed and a mini editor
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
DisableReadyPage=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
UsePreviousPrivileges=yes
OutputDir=..\releases
OutputBaseFilename=EGPlayer-Setup-{#AppVersion}
SetupIconFile=..\assets\egplayer.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
ChangesAssociations=yes
CloseApplications=force
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "openwith"; Description: "Add EG Player to the ""Open with"" menu for video and audio files"; GroupDescription: "Windows integration:"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; drop the previous version's Python runtime so stale modules never mix with the new ones
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "Play and edit videos"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
#include "assoc.iss"

[Run]
; also runs after silent updates, so the app comes back by itself
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\EGPlayer\cache"
