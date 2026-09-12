; ============================================================================
;  BIT 选课助手 —— Windows 安装包构建脚本（Inno Setup 6）
;
;  产物：BIT-Course-Helper-<版本>-setup.exe
;
;  设计取舍：
;  * 默认 **按用户安装**（PrivilegesRequired=lowest），装到 %LOCALAPPDATA%，
;    全程不需要管理员权限 —— 学生电脑通常没有管理员权限，这点很重要。
;  * 同时提供"为所有用户安装"的选项（PrivilegesRequiredOverridesAllowed）。
;  * 卸载时清理干净，并询问是否保留配置文件。
;
;  构建：
;    ISCC.exe packaging\installer.iss /DAppVersion=0.1.0 /DSourceDir=...\dist\...
; ============================================================================

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\BIT-Course-Helper"
#endif

#define AppName "BIT 选课助手"
#define AppNameEn "BIT Course Helper"
#define AppPublisher "Sirius-Peng"
#define AppURL "https://github.com/Sirius-Peng/bitxk"
#define AppExeName "bitxk-gui.exe"

[Setup]
AppId={{7E3C1A94-5B62-4F0D-9C31-2A8E6D4B7F10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppNameEn}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile={#SourceDir}\LICENSE
OutputBaseFilename=BIT-Course-Helper-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 学生机器通常没有管理员权限，所以默认按用户安装
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={app}\{#AppExeName}
SetupIconFile=
; 抢课工具会被某些杀软盯上，这里显式声明没有捆绑
AppMutex=bitxk_mutex

[Languages]
; 中文语言包来自 Inno Setup 官方仓库的 Languages 目录。
; 若本机没有该文件，构建时改用英文提示（见 build-installer.ps1）。
#ifndef NoChinese
Name: "chinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#endif
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: checkedonce
Name: "addtopath"; Description: "把命令行工具加入 PATH（方便在终端里直接敲 bitxk）"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
; SourceDir 指向发行暂存目录，其结构为：
;   bitxk-gui.exe / bitxk.exe / _internal\ / README.md / LICENSE
;   / config.example.toml / QUICKSTART.txt / BIT-Course-Helper-cli\
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "BIT-Course-Helper-cli"
; 精简命令行版单独放进 cli 子目录，方便加到 PATH
Source: "{#SourceDir}\BIT-Course-Helper-cli\*"; DestDir: "{app}\cli"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[Registry]
; 可选：把命令行工具的目录写进用户 PATH
Root: HKCU; Subkey: "Environment"; ValueType: string; ValueName: "Path"; \
    ValueData: "{app}\cli"; Flags: uninsdeletevalue; Tasks: addtopath

[UninstallDelete]
; 只清理我们自己生成的运行产物，不碰用户的配置
Type: filesandordirs; Name: "{app}\_internal"

[Code]
// 卸载时问一下要不要保留配置与登录态
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ConfigPath: String;
  KeepData: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    ConfigPath := ExpandConstant('{app}\config.toml');
    if FileExists(ConfigPath) then
    begin
      KeepData := MsgBox('是否保留配置文件 config.toml？' + Chr(13) + Chr(10) +
        Chr(13) + Chr(10) + '保留的话，重新安装后不用再填一遍课程和账号。' +
        Chr(13) + Chr(10) + '登录态缓存在用户目录的 .bitxk 下，不受卸载影响。',
        mbConfirmation, MB_YESNO);
      if KeepData = IDNO then
        DeleteFile(ConfigPath);
    end;
  end;
end;
