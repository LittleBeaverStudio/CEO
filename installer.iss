; 首席执行官 —— Inno Setup 安装脚本（v1.0.8）
#define AppName "首席执行官"
#define AppVersion "1.0.8"
#define AppExeName "首席执行官.exe"
#define AppPublisher "小河狸工作室"

[Setup]
AppId={{8C1F2A34-77C8-4E5B-9A0D-2F5B6C7D8E91}}
AppName={#AppName}
; v1.0.7：显示名不带版本号（"首席执行官"），版本与发布者放在下一行，
; 与"小河狸制片厂 / 2.3.0 | 小河狸工作室 | 日期"的形态一致
AppVerName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=dist_installer
OutputBaseFilename=首席执行官安装包_v{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#AppExeName}
; v1.0.5：安装包/安装向导也用软件自己的 logo（与 exe、托盘、页面同一份 app.ico）
SetupIconFile=app.ico

[Files]
Source: "dist\{#AppName}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加："

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 数据目录 %LOCALAPPDATA%\首席执行官（旧版为 小河狸首席执行官，程序首启自动迁移）由用户自行决定是否删除，安装器不触碰
Type: files; Name: "{app}\*.log"

[Code]
// v1.0.7：不再调用 ie4uinit.exe。
// 32 位安装包访问系统目录会被重定向到 SysWOW64，那里没有这个文件，
// 安装时就弹 "Unable to execute file ... code 2"。
// 改用 shell32 的 SHChangeNotify 通知资源管理器刷新图标缓存，纯 API 调用不会失败。
const
  SHCNE_ASSOCCHANGED = $08000000;
  SHCNF_IDLIST = $0000;

procedure ShellNotify(wEventId: Integer; uFlags: Cardinal; dwItem1, dwItem2: Cardinal);
  external 'SHChangeNotify@shell32.dll stdcall';

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    ShellNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, 0, 0);
end;
