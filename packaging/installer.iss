; Inno Setup 6: LineOps-Setup-<versão>.exe. Rode pelo build.bat (ele passa a versão).
#ifndef AppVersion
  #define AppVersion "0.11.0"
#endif

[Setup]
AppId={{D2B9DEB8-93CC-4AB9-AF73-9AF90A3A6EC3}
AppName=LineOps
AppVersion={#AppVersion}
AppVerName=LineOps {#AppVersion}
AppPublisher=Projeto pessoal (sem afiliação com a GOL Linhas Aéreas)
DefaultDirName={autopf}\LineOps
DefaultGroupName=LineOps
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=LineOps-Setup-{#AppVersion}
SetupIconFile=lineops.ico
UninstallDisplayIcon={app}\LineOps.exe
UninstallDisplayName=LineOps
InfoBeforeFile=aviso.txt
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na área de trabalho"; GroupDescription: "Atalhos:"
Name: "autostart"; Description: "Iniciar o LineOps com o Windows (fica na bandeja; o laço da IVAO e a equipe seguem trabalhando)"; GroupDescription: "Opções:"; Flags: unchecked
Name: "firewall"; Description: "Liberar acesso pela rede de casa (PC do simulador e celular), só em redes privadas"; GroupDescription: "Opções:"; Flags: unchecked

[Files]
Source: "..\dist\LineOps\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\LineOps ACARS\*"; DestDir: "{app}\acars"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "aviso.txt"; DestDir: "{app}"; DestName: "LEIA-ME.txt"

[InstallDelete]
; o projeto se chamava GolOps: tira o executável e os atalhos antigos (os dados ficam em %LOCALAPPDATA% e não são tocados)
Type: files; Name: "{app}\GolOps.exe"
Type: files; Name: "{app}\GolOps ACARS.exe"
Type: files; Name: "{group}\GolOps.lnk"
Type: files; Name: "{group}\GolOps ACARS.lnk"
Type: files; Name: "{autodesktop}\GolOps.lnk"
Type: files; Name: "{autodesktop}\GolOps ACARS.lnk"
Type: filesandordirs; Name: "{app}\acars\GolOps ACARS.exe"

[Icons]
Name: "{group}\LineOps"; Filename: "{app}\LineOps.exe"
Name: "{group}\LineOps ACARS"; Filename: "{app}\acars\LineOps ACARS.exe"; WorkingDir: "{app}\acars"
Name: "{group}\Pasta de dados do LineOps"; Filename: "{localappdata}\LineOps\dados"
Name: "{group}\Desinstalar o LineOps"; Filename: "{uninstallexe}"
Name: "{autodesktop}\LineOps"; Filename: "{app}\LineOps.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "LineOps"; ValueData: """{app}\LineOps.exe"" --hidden"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""LineOps"" dir=in action=allow program=""{app}\LineOps.exe"" profile=private enable=yes"; Flags: runhidden; Tasks: firewall
Filename: "{app}\LineOps.exe"; Description: "Abrir o LineOps agora"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""LineOps"""; Flags: runhidden; RunOnceId: "LineOpsFirewall"

[Code]
function WebView2Installed(): Boolean;
var v: String;
begin
  Result := RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v) or
            RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v);
  if Result then Result := (v <> '') and (v <> '0.0.0.0');
end;

const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';

var
  HadOldAutostart: Boolean;

{ O GolOps (nome antigo) fica na bandeja ocupando a porta 8787: com ele aberto, a LineOps não sobe e o navegador
  mostra o app antigo. Fecha antes de instalar. Os dados não são tocados (ficam em %LOCALAPPDATA%). }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var rc: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM "GolOps.exe"', '', SW_HIDE, ewWaitUntilTerminated, rc);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM "GolOps ACARS.exe"', '', SW_HIDE, ewWaitUntilTerminated, rc);
  HadOldAutostart := RegValueExists(HKCU, RunKey, 'GolOps');
  if HadOldAutostart then
    RegDeleteValue(HKCU, RunKey, 'GolOps');
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  { quem tinha o GolOps iniciando com o Windows continua com a LineOps }
  if (CurStep = ssPostInstall) and HadOldAutostart then
    RegWriteStringValue(HKCU, RunKey, 'LineOps', '"' + ExpandConstant('{app}\LineOps.exe') + '" --hidden');
  if (CurStep = ssPostInstall) and (not WebView2Installed()) then
    MsgBox('O WebView2 da Microsoft não foi encontrado. O LineOps vai abrir no seu navegador padrão.' + #13#10 +
           'Para a janela própria, instale o "WebView2 Runtime" (gratuito) no site da Microsoft.', mbInformation, MB_OK);
end;
