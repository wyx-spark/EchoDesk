; ============================================================================
;  EchoDesk 安装程序打包脚本（Inno Setup 6）
;
;  为什么是 Inno Setup 而不是 7-Zip 自解压包（v0.2.1 实测结论，勿回退）：
;    * 7-Zip 官方 SFX 模块只支持 7 个配置键
;      （Title / BeginPrompt / Progress / RunProgram / Directory /
;        ExecuteFile / ExecuteParameters），**没有 InstallPath**
;      —— 用二进制内嵌字符串在 ASCII 与 UTF-16 两种编码下都核对过。
;    * 它的默认行为是"解压到 %TEMP%\7zNNNNNN，执行 RunProgram 后删掉该目录"。
;      虚拟机实测：用户双击后全盘搜不到 EchoDesk 文件，安装失败。
;    * 仓库里 `publish\7zSD.sfx` 与官方 9.20 的 `7zS.sfx` 是**同一个文件**
;      （版本资源 InternalName=7zS.sfx、OriginalFilename=7zS.sfx.exe、
;        Copyright (c) 1999-2010 Igor Pavlov），改名并不能获得 InstallPath。
;    结论：换模块解决不了问题，改用真正的安装器编译器（Inno Setup）。
;
;  默认安装位置 = 安装程序自己所在的目录（用户双击的那个文件夹）：
;    [Setup] DefaultDirName={code:GetDefaultDir}，由 [Code] 段的
;    GetDefaultDir 返回 ExtractFileDir(ExpandConstant('{srcexe}'))。
;    配合 AppendDefaultDirName=no 与 UsePreviousAppDir=no，
;    安装结果与便携版目录结构**完全一致**，用户不需要理解"解压到哪了"。
;    路径选择页保留，用户仍可改到别处。
;
;  目录结构（与便携版一致，修复七的三层结构）：
;    {app}\                  ← 外层：用户数据、结果目录、文档
;      results\                  自动化结果默认位置
;      ui_state.json / snippets.json
;      docs\ lang\ examples\ LICENSE NOTICE
;      app\                  ← 程序本体
;        EchoDesk.exe
;        backend\EchoDeskBackend.exe ...
;  快捷方式的工作目录一律设为 {app}（外层），这样 results\ 与用户数据生成在外层。
;
;  【不要对最终的 Setup.exe 跑 rcedit】——实测过，会直接毁掉安装包：
;    Inno Setup 把安装数据**追加在 PE 映像之后**（探针实测：PE 最后一个段结束于
;    893,952 字节，文件总长 2,097,164 字节，追加数据 1,203,212 字节）。
;    rcedit 写资源会重建 PE 映像并丢弃追加数据 → 文件被削到 893,952 字节，
;    运行时报错且什么都不安装（退出码 1）。这与本项目此前踩过的
;    「603.8 MB 自解压包被 rcedit 削成 176 KB 空壳」是同一个机制。
;    版本资源改由本文件的 [Setup] VersionInfo* 指令交给编译器写入，
;    make_installer.ps1 会读回校验，并断言"追加数据仍在"。
;
;  用法（一般由 tools\make_installer.ps1 调用）：
;    iscc tools\installer.iss /DMyAppVersion=0.2.1 /DMyIncludeModels=1
;    iscc tools\installer.iss /DMyAppVersion=0.2.1 /DMyIncludeModels=0
; ============================================================================

#ifndef MyAppVersion
  #define MyAppVersion "0.2.1"
#endif

; "1" = 含模型（含 app\backend\models\*.gguf）；"0" = 不含模型。
; 用字符串比较：ISCC 的 /D<name>=<value> 定义出来的是字符串。
#ifndef MyIncludeModels
  #define MyIncludeModels "1"
#endif

; 允许覆盖源目录，便于在不重建 885MB 发布目录的情况下做脚本语法自检。
#ifndef MySourceDir
  #define MySourceDir "..\publish\EchoDesk-v" + MyAppVersion
#endif

#define MyAppName "EchoDesk"
#define MyAppPublisher "Void Entropia Studio"
; 任务书「全局要求三」：公开反馈指向 GitHub Issues 与 Discussions，无实体地址
#define MyAppURL "https://github.com/VoidEntropiaStudio/EchoDesk"
; 修复七：程序本体在 app\ 子目录，外层是用户数据目录
#define MyAppExeSubDir "app"
#define MyAppExeName "EchoDesk.exe"

#if MyIncludeModels == "0"
  #define MyVariantSuffix "-NoModel"
  #define MyVariantLabel "Portable Setup (No Model)"
#else
  #define MyVariantSuffix ""
  #define MyVariantLabel "Portable Setup"
#endif

[Setup]
; AppId 必须是合法 GUID：原值 {8E1D6F42-2C1B-4C6E-9F3A-ECHODESK00001} 里含有
; 非十六进制字符（H/O/S/K），只是"看起来像 GUID"，已改成真 GUID。
; 一旦对外发布就**不要**再改，否则升级会被当成另一个产品。
; 含模型版与不含模型版**共用同一个 AppId**：它们是同一个产品的两种载荷，
; 后装的会覆盖先装的（同名文件 + ignoreversion），而不是并存两个条目。
AppId={{BBF9A096-EC89-4770-815F-40AF1FDC0AD1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
; 默认装到安装程序自己所在的目录（见文件头说明与 [Code] GetDefaultDir）
DefaultDirName={code:GetDefaultDir}
AppendDefaultDirName=no
UsePreviousAppDir=no
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 保留路径选择页：默认值是安装程序所在目录，但用户想改就改
DisableDirPage=no
OutputDir=..\publish
OutputBaseFilename=EchoDesk-v{#MyAppVersion}{#MyVariantSuffix}-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 后端与前端均为 64 位
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\assets\EchoDesk.ico
UninstallDisplayIcon={app}\{#MyAppExeSubDir}\{#MyAppExeName}
; 不请求管理员权限：默认装到用户自己选/自己所在的普通目录，
; 保持"本地工具"定位（需要装到 Program Files 时用户可在对话框里提权）。
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; ---- 版本资源：由编译器直接写入（**不要**改用 rcedit，见文件头说明）----
; 无 VersionInfoInternalName / VersionInfoComments 指令，这两个字段由 Inno 自己
; 决定，交付物验收脚本据此只校验能设置的字段。
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} v{#MyAppVersion} {#MyVariantLabel}
VersionInfoTextVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersion}.0
VersionInfoCopyright=Copyright (C) Void Entropia Studio
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}.0
VersionInfoOriginalFileName=EchoDesk-v{#MyAppVersion}{#MyVariantSuffix}-Setup.exe

[Languages]
; 英文用编译器自带 Default.isl，永远可用。
Name: "english"; MessagesFile: "compiler:Default.isl"
; 简体中文：Inno Setup 6.7 自带的 Languages\ 目录里**没有** ChineseSimplified.isl
; （官方把它列为需单独下载的第三方翻译）。这里用 ISPP 的 FileExists 做条件包含：
; 机器上放了这个 .isl 就自动启用中文界面，没放就只出英文界面 —— 不会编译失败。
; 注意：此前这里写的是 `Name: "chinese"; MessagesFile: "compiler:Default.isl"`，
; 那只是把英文界面**标成**中文，属于误标，已改掉。
#if FileExists(AddBackslash(CompilerPath) + "Languages\ChineseSimplified.isl")
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
#if MyIncludeModels == "0"
; 不含模型版：排除模型文件。
; Excludes 的匹配语义是**相对 Source 目录的路径**（不是含源目录前缀的完整路径），
; 而且模式可以只匹配路径的某一级起（实测：app\backend\models\* 与 *\models\*
; 都能命中，但写成 *\app\backend\models\* 会命中失败 —— 前面多出来的 *\ 要求
; 在 app 之前还有一级目录）。这条实测结论花了 7 组对照才确定，别凭直觉改。
; 同时用 *.gguf 兜底：不含模型版就该"一个 .gguf 都没有"，这样校验断言是结构性成立的。
; 模型文件一个都不进包时，空的 models\ 目录也不会被创建（[Dirs] 也没有它）。
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Excludes: "app\backend\models\*,*\models\*,*.gguf"; Flags: ignoreversion recursesubdirs createallsubdirs
#else
; 含模型版：发布目录整体安装（含 app\ 程序本体与 backend onedir 目录）
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#endif

[Dirs]
; 便携版里 results\ 是个空目录（自动化结果默认位置），Inno 不会因为
; "复制空目录"而创建它，所以显式声明，保证安装后结构与便携版一致。
Name: "{app}\results"

[Icons]
; 开始菜单入口（工作目录设为外层 {app}，与便携版快捷方式一致）
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeSubDir}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeSubDir}\EchoDesk.ico"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
; 可选桌面快捷方式（默认勾选）
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeSubDir}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeSubDir}\EchoDesk.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeSubDir}\{#MyAppExeName}"; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载时清理运行时生成的界面状态（用户数据默认保留，避免误删会话与结果）
Type: files; Name: "{app}\ui_state.json"
Type: files; Name: "{app}\app\backend\backend_crash.log"
Type: files; Name: "{app}\app\backend\skills_dev.log"

[Code]
// 默认安装目录 = 安装程序（Setup.exe）自己所在的目录。
// 这样用户双击哪个文件夹里的安装程序，EchoDesk 就装到那个文件夹，
// 与便携版"解压到当前文件夹"的预期一致，不会出现"全盘找不到文件"。
//
// 注意：本段是 Pascal 脚本，花括号是**注释定界符**，因此注释里不要出现
// 带花括号的常量名（写成 srcexe 而不是带花括号的形式），否则第一个右花括号
// 会提前结束注释，后面的中文会被当成代码 —— 实测踩到过（报 "Syntax error"）。
// srcexe 是安装程序自身的完整路径，Inno 会在参数求值阶段调用本函数。
function GetDefaultDir(Param: String): String;
begin
  Result := ExtractFileDir(ExpandConstant('{srcexe}'));
end;
