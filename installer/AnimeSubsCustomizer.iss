; Anime Subs Customizer — per-user installer (Inno Setup 7). See docs/adr/ADR-002-installer.md.
;
;   ISCC.exe /DAppVersion=1.2.0 /DSourceDir=<dist\AnimeSubsCustomizer> [/DFFmpegDir=<folder with ffmpeg.exe>] installer\AnimeSubsCustomizer.iss
;
; Silent switches tested in CI: /VERYSILENT /SUPPRESSMSGBOXES /NORESTART [/LANG=arabic]
; Silent uninstall keeps settings unless /REMOVESETTINGS=1 is passed.

#define AppName "Anime Subs Customizer"
#define AppExe "AnimeSubsCustomizer.exe"
#define AppMutexName "ASS-Subs-Changer-Singleton"
#define AppUserModelId "RokoTech.AnimeSubsCustomizer"
#define RepoUrl "https://github.com/roko-tech/Anime-Subs-Customizer"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\dist\AnimeSubsCustomizer"
#endif
#ifndef OutputDir
  #define OutputDir "..\build\out"
#endif

[Setup]
; The AppId must never change: it is how an upgrade finds the existing install.
AppId={{8B6E2F44-6C1A-4E0B-9D3F-2A7C5E9B1D20}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=roko-tech
AppPublisherURL={#RepoUrl}
AppSupportURL={#RepoUrl}/issues
AppUpdatesURL={#RepoUrl}/releases
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
; Per-user: installs to %LOCALAPPDATA%\Programs, no administrator prompt.
PrivilegesRequired=lowest
; Setup refuses to continue while the app runs; [Code] asks it to quit first.
AppMutex={#AppMutexName}
; Anything else still holding a file in {app} (e.g. an ffmpeg child) is closed.
CloseApplications=force
RestartApplications=no
ShowLanguageDialog=yes
LanguageDetectionMethod=uilanguage
OutputDir={#OutputDir}
OutputBaseFilename=AnimeSubsCustomizer-Setup
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "arabic"; MessagesFile: "compiler:Languages\Arabic.isl"

[CustomMessages]
english.AppComponent=Anime Subs Customizer
arabic.AppComponent=Anime Subs Customizer
english.FFmpegComponent=FFmpeg (extracts subtitles from videos and shows real frames in the preview)
arabic.FFmpegComponent=FFmpeg (لاستخراج الترجمات من ملفات الفيديو وعرض لقطات حقيقية في المعاينة)
english.FullInstall=Recommended
arabic.FullInstall=مستحسن
english.CustomInstall=Custom
arabic.CustomInstall=مخصّص
english.AutostartTask=Start automatically when I sign in (in the notification area)
arabic.AutostartTask=التشغيل تلقائيًا عند تسجيل الدخول (في منطقة الإعلام)
english.DesktopIcon=Create a desktop shortcut
arabic.DesktopIcon=إنشاء اختصار على سطح المكتب
english.LaunchApp=Start Anime Subs Customizer
arabic.LaunchApp=تشغيل Anime Subs Customizer
english.RemoveSettingsQ=Also delete your settings and the app's list of styled files?%n%nYour subtitles and their "Original Subs" backups are never deleted.
arabic.RemoveSettingsQ=هل تريد أيضًا حذف إعداداتك وقائمة الملفات المنسّقة الخاصة بالتطبيق؟%n%nلا تُحذف ملفات الترجمة ولا نسخها الاحتياطية في «Original Subs» أبدًا.

[Types]
Name: "full"; Description: "{cm:FullInstall}"
Name: "custom"; Description: "{cm:CustomInstall}"; Flags: iscustom

[Components]
Name: "app"; Description: "{cm:AppComponent}"; Types: full custom; Flags: fixed
#ifdef FFmpegDir
Name: "ffmpeg"; Description: "{cm:FFmpegComponent}"; Types: full
#endif

[Tasks]
Name: "autostart"; Description: "{cm:AutostartTask}"
Name: "desktopicon"; Description: "{cm:DesktopIcon}"; Flags: unchecked

[InstallDelete]
; Files dropped from a newer bundle must not linger. User data lives in
; %LOCALAPPDATA%\Anime Subs Customizer, never under {app}.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\bin"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: app
#ifdef FFmpegDir
Source: "{#FFmpegDir}\*"; DestDir: "{app}\bin"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: ffmpeg
#endif

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelId}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon; AppUserModelID: "{#AppUserModelId}"
; The same shortcut the app's own "Autostart at login" toggle manages.
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--minimized"; Tasks: autostart

[Registry]
; Seeds the app's first-run language with the language chosen here.
Root: HKCU; Subkey: "Software\{#AppName}"; ValueType: string; ValueName: "InstallerLanguage"; ValueData: "{code:InstallerLangCode}"; Flags: uninsdeletekey

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent
; A silent upgrade of a running copy starts it again, back in the tray.
Filename: "{app}\{#AppExe}"; Parameters: "--minimized"; Flags: nowait; Check: RelaunchAfterSilentUpgrade

[UninstallDelete]
; Created later by the app's own Autostart toggle (not tracked by [Icons]).
Type: files; Name: "{userstartup}\{#AppName}.lnk"

[Code]
var
  WasRunning: Boolean;

function InstallerLangCode(Param: String): String;
begin
  if ActiveLanguage = 'arabic' then
    Result := 'ar'
  else
    Result := 'en';
end;

{ Ask a running copy to quit (it closes its own ffmpeg children and exits),
  then wait until its single-instance mutex is gone, i.e. the process ended.
  Runs before Setup's AppMutex check, so an upgrade works while the tray app
  runs, in silent mode too. }
procedure QuitRunningApp(const Exe: String);
var
  Code, I: Integer;
begin
  if not CheckForMutexes('{#AppMutexName}') then
    exit;
  WasRunning := True;
  if FileExists(Exe) then
    Exec(Exe, '--quit', '', SW_HIDE, ewWaitUntilTerminated, Code);
  I := 0;
  while CheckForMutexes('{#AppMutexName}') and (I < 150) do
  begin
    Sleep(100);
    I := I + 1;
  end;
  Sleep(300);
end;

function PreviousInstallDir(): String;
var
  Key: String;
begin
  Result := '';
  Key := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8B6E2F44-6C1A-4E0B-9D3F-2A7C5E9B1D20}_is1';
  RegQueryStringValue(HKCU, Key, 'InstallLocation', Result);
end;

function InitializeSetup(): Boolean;
var
  Dir: String;
begin
  WasRunning := False;
  Dir := PreviousInstallDir();
  if Dir <> '' then
    QuitRunningApp(AddBackslash(Dir) + '{#AppExe}');
  Result := True;
end;

function InitializeUninstall(): Boolean;
begin
  QuitRunningApp(ExpandConstant('{app}\{#AppExe}'));
  Result := True;
end;

function RelaunchAfterSilentUpgrade(): Boolean;
begin
  Result := WasRunning and WizardSilent;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  RemoveSettings: Boolean;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  if ExpandConstant('{param:REMOVESETTINGS|0}') = '1' then
    RemoveSettings := True
  else if UninstallSilent then
    RemoveSettings := False
  else
    RemoveSettings := MsgBox(CustomMessage('RemoveSettingsQ'), mbConfirmation,
                             MB_YESNO or MB_DEFBUTTON2) = IDYES;
  { Only the app's own data folder. Subtitles and Original Subs\ backups live in
    the user's library and are never touched. }
  if RemoveSettings then
    DelTree(ExpandConstant('{localappdata}\{#AppName}'), True, True, True);
end;
