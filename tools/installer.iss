; ============================================================================
;  EchoDesk 安装版打包脚本（Inno Setup 6）
;
;  与便携版的关系：
;    * 便携版 = tools\build_release.ps1 直接产出的 EchoDesk-v0.2.1 文件夹，
;      绿色免安装、可放在 U 盘，删除文件夹即卸载（不写注册表）。
;    * 安装版 = 用本脚本把同一个文件夹打包成单个 setup.exe，供不熟悉文件
;      目录的用户使用：自动创建开始菜单与桌面快捷方式、提供卸载入口。
;  两者内容完全一致，只是分发形态不同；安装版并不会修改程序行为。
;
;  【修复七】安装版目录结构与便携版保持一致：
;    {app}\                  ← 外层：用户数据、结果目录、文档
;      EchoDesk-v0.2.1.lnk   （安装版用开始菜单/桌面快捷方式代替）
;      批量处理结果\
;      ui_state.json / snippets.json
;      docs\ lang\ examples\ LICENSE NOTICE
;      app\                  ← 程序本体
;        EchoDesk.exe
;        backend\EchoDeskBackend.exe ...
;  快捷方式的工作目录一律设为 {app}（外层），这样批量处理结果生成在 {app}\批量处理结果\。
;
;  用法：
;    1) 先执行 tools\build_release.ps1 产出发布文件夹；
;    2) 用 Inno Setup 6 编译本脚本：
;         iscc tools\installer.iss /DMyAppVersion=0.2.1
;
;  说明：
;    * 未做代码签名时，安装包与其中的 exe 都会触发 SmartScreen 提示，
;      正式分发前请先对 exe 签名（build_release.ps1 -Sign）再打包。
;    * 预置模型可把 -IncludeModels 产出的 models 目录一并包含（体积很大）。
; ============================================================================

#ifndef MyAppVersion
  #define MyAppVersion "0.2.1"
#endif

#define MyAppName "EchoDesk"
#define MyAppPublisher "Void Entropia Studio"
#define MyAppURL "https://github.com/"
; 修复七：程序本体在 app\ 子目录，外层是用户数据目录
#define MyAppExeSubDir "app"
#define MyAppExeName "EchoDesk.exe"
#define MySourceDir "..\publish\EchoDesk-v" + MyAppVersion

[Setup]
AppId={{8E1D6F42-2C1B-4C6E-9F3A-ECHODESK00001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\publish
OutputBaseFilename=EchoDesk-v{#MyAppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 后端与前端均为 64 位，安装到 64 位程序目录
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\assets\EchoDesk.ico
UninstallDisplayIcon={app}\{#MyAppExeSubDir}\{#MyAppExeName}
; 不写入任何用户数据到安装目录以外的位置（保持"本地工具"定位）
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "chinese"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式:"; Flags: checkedonce

[Files]
; 便携版文件夹整体安装（含 app\ 程序本体与 backend onedir 目录）
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 模型体积很大（>500MB），默认不打包；若已用 -IncludeModels 生成，可解开下一行
; Source: "{#MySourceDir}\app\backend\models\*.gguf"; DestDir: "{app}\app\backend\models"; Flags: ignoreversion

[Icons]
; 开始菜单入口（工作目录设为外层 {app}，与便携版快捷方式一致）
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeSubDir}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeSubDir}\EchoDesk.ico"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
; 可选桌面快捷方式
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeSubDir}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeSubDir}\EchoDesk.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeSubDir}\{#MyAppExeName}"; WorkingDir: "{app}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载时清理运行时生成的界面状态（用户数据默认保留，避免误删会话）
Type: files; Name: "{app}\ui_state.json"
