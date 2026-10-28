; Instalador de SupaVic DLNA (Inno Setup 6). Se compila después de PyInstaller:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DAppVersion=1.0.0 build\installer.iss
; Resultado: dist\SupaVicDLNA-Setup-<version>.exe

#define AppName "SupaVic DLNA"
#define AppExe "SupaVicDLNA.exe"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{8E0C7C2B-5A3E-4C1F-9C3A-2F7A1D3B9E41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=SupaVic
DefaultDirName={autopf}\SupaVic DLNA
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=SupaVicDLNA-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no
ShowLanguageDialog=auto

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[CustomMessages]
en.AutoStart=Start SupaVic DLNA when I sign in to Windows (recommended)
es.AutoStart=Iniciar SupaVic DLNA al iniciar sesión en Windows (recomendado)
en.Launch=Open SupaVic DLNA now
es.Launch=Abrir SupaVic DLNA ahora
en.Firewall=Allowing SupaVic DLNA on your local network (Windows Firewall)...
es.Firewall=Permitiendo SupaVic DLNA en tu red local (Firewall de Windows)...

[Tasks]
Name: "autostart"; Description: "{cm:AutoStart}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\SupaVicDLNA\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; Firewall: solo la red local (remoteip=localsubnet), solo este programa
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#AppName}"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#AppName}"" dir=in action=allow enable=yes profile=any remoteip=localsubnet program=""{app}\{#AppExe}"""; Flags: runhidden waituntilterminated; StatusMsg: "{cm:Firewall}"
Filename: "{app}\{#AppExe}"; Parameters: "--autostart on"; Tasks: autostart; Flags: runasoriginaluser runhidden waituntilterminated
Filename: "{app}\{#AppExe}"; Parameters: "--autostart off"; Tasks: not autostart; Flags: runasoriginaluser runhidden waituntilterminated
Filename: "{app}\{#AppExe}"; Description: "{cm:Launch}"; Flags: postinstall nowait skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{app}\{#AppExe}"; Parameters: "--autostart off"; Flags: runhidden waituntilterminated; RunOnceId: "AutostartOff"
Filename: "{sys}\taskkill.exe"; Parameters: "/IM {#AppExe} /F"; Flags: runhidden waituntilterminated; RunOnceId: "Stop"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#AppName}"""; Flags: runhidden waituntilterminated; RunOnceId: "Firewall"

[Code]
// Cierra la versión anterior (servidor + dashboard) antes de copiar archivos.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Codigo: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM {#AppExe} /F', '', SW_HIDE, ewWaitUntilTerminated, Codigo);
  Sleep(800);
  Result := '';
end;
