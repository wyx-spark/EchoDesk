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
;    iscc tools\installer.iss /DMyAppVersion=0.2.1
;
;  【只有一份安装程序，且**不打包任何模型**】（v0.2.1 默认模型切换一轮改动）
;    此前是两份：含模型版与 -NoModel 版，靠 /DMyIncludeModels 区分。
;    现在安装程序只有一份、文件名不带任何变体后缀（EchoDesk-v0.2.1-Setup.exe），
;    模型一律不进包：离线场景改由单独的"模型补充包"承担，安装程序本身保持
;    "装完即用、模型按需下载"的单一形态，体积也从 608 MB 降到 114 MB 量级。
;    [Files] 里的 Excludes 因此是**无条件**的（那段匹配语义的实测结论保留在下方）。
;
;  【卸载只清可再生文件；用户数据与 results\ 一律保留】（v0.2.4 补丁修订）
;    v0.2.4 之前这里会删除 session.json、models\、ui_state.json 与各种 *_registry.json，
;    升级/卸载都可能让用户丢数据。现在 [UninstallDelete] 只列缓存、日志与快捷方式；
;    config、会话、注册表、模型、插件、界面偏好与 results\ 全部保留。
;    [Code] 的 BackupUserData() 还会在卸载前把小型用户数据复制到
;    %LOCALAPPDATA%\EchoDesk\uninstall-backup\<时间戳>\ 作为安全网。
;    注意：**不要**图省事写 `Type: filesandordirs; Name: "{app}"` —— 那会把 results\
;    与全部用户数据一起删掉。
; ============================================================================

#ifndef MyAppVersion
  ; [EN] The default version is overridden by tools\make_installer.ps1 (-MyAppVersion, which
  ; [EN] reads backend\version.py). The literal below is only the fallback for compiling without
  ; [EN] parameters and is kept in sync with version.py: v0.2.1 = pre-cross-platform; v0.2.2 =
  ; [EN] cross-platform release; v0.2.3 = Avalonia as the main line (WPF moves into LTS);
  ; [EN] v0.2.4 = hardware-adaptive tuning with three inference presets.
  ; [ZH] 默认版本由 tools\make_installer.ps1 的 -MyAppVersion 覆盖（它从 backend\version.py 取）。
  ; [ZH] 这里的字面量只是"不传参直接编译"时的兜底，保持与 version.py 一致：
  ; [ZH] v0.2.1 = 跨平台之前；v0.2.2 = 跨平台版本；v0.2.3 = Avalonia 主线（WPF 转 LTS）；
  ; [ZH] v0.2.4 = 硬件自适应与三档推理预设。
  #define MyAppVersion "0.2.4"
#endif

; [EN] Purely numeric core segment (**no prerelease suffix**): Inno's VersionInfoVersion and
; [EN] VersionInfoProductVersion accept four numeric fields only, and `0.2.3-alpha.1.0` makes the
; [EN] compiler fail outright. tools\make_installer.ps1 always passes it through
; [EN] /DMyAppNumericVersion; the value below is only the fallback when no parameter is given and
; [EN] matches app_version_4() in backend\version.py.
; [ZH] 纯数字核心段（**不允许**预发布后缀）：Inno 的 VersionInfoVersion /
; [ZH] VersionInfoProductVersion 只接受四段纯数字，`0.2.3-alpha.1.0` 会让编译器直接报错。
; [ZH] tools\make_installer.ps1 总会用 /DMyAppNumericVersion 传进来；这里只是不传参时的兜底，
; [ZH] 口径与 backend\version.py 的 app_version_4() 一致。
#ifndef MyAppNumericVersion
  #define MyAppNumericVersion "0.2.4"
#endif

; 允许覆盖源目录，便于在不重建发布目录的情况下做脚本语法自检。
#ifndef MySourceDir
  #define MySourceDir "..\publish\EchoDesk-v" + MyAppVersion
#endif

#define MyAppName "EchoDesk"
#define MyAppPublisher "Void Entropia Studio"
; 任务书「全局要求三」：公开反馈指向 GitHub Issues 与 Discussions，无实体地址
#define MyAppURL "https://github.com/wyx-spark/EchoDesk"
; 修复七：程序本体在 app\ 子目录，外层是用户数据目录
#define MyAppExeSubDir "app"
#define MyAppExeName "EchoDesk.exe"

[Setup]
; AppId 必须是合法 GUID：原值 {8E1D6F42-2C1B-4C6E-9F3A-ECHODESK00001} 里含有
; 非十六进制字符（H/O/S/K），只是"看起来像 GUID"，已改成真 GUID。
; 一旦对外发布就**不要**再改，否则升级会被当成另一个产品。
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
OutputBaseFilename=EchoDesk-v{#MyAppVersion}-Setup
Compression=lzma2/max
SolidCompression=yes
; [EN] Round file timestamps to a one-second boundary. This directive is combined with the
; [EN] ``notimestamp`` flag on the [Files] entry below: the flag removes the source
; [EN] timestamps entirely, while the rounding keeps any timestamp Inno still records from
; [EN] drifting with the filesystem clock.
; [ZH] 将文件时间戳向下取整到一秒边界。本指令与下方 [Files] 条目的 ``notimestamp`` 标志
; [ZH] 配合使用：该标志彻底移除源文件时间戳，取整则避免 Inno 仍记录的时间戳随文件系统
; [ZH] 时钟漂移。
TimeStampRounding=1
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
VersionInfoDescription={#MyAppName} v{#MyAppVersion} Portable Setup
VersionInfoTextVersion={#MyAppVersion}
VersionInfoVersion={#MyAppNumericVersion}.0
VersionInfoCopyright=Copyright (C) Void Entropia Studio
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppNumericVersion}.0
VersionInfoOriginalFileName=EchoDesk-v{#MyAppVersion}-Setup.exe

[Languages]
; 英文用编译器自带 Default.isl，永远可用。
Name: "english"; MessagesFile: "compiler:Default.isl"
; 简体中文：官方 Inno Setup 源码树里的 ChineseSimplified.isl 已随仓库内置到
; tools\InnoSetup\ChineseSimplified.isl（来源：
; https://github.com/jrsoftware/issrc/blob/main/Files/Languages/ChineseSimplified.isl）。
; 不再依赖编译器目录里是否恰好存在该文件，因此任何构建机都能出中文安装界面。
; 文件为 UTF-8 with BOM —— 没有 BOM 时 Inno 会按系统 ANSI 代码页读取，中文会乱码。
Name: "chinesesimplified"; MessagesFile: "InnoSetup\ChineseSimplified.isl"

[CustomMessages]
; v0.2.2 硬编码清查第 6 批收尾：卸载提示原先**写死中文**（见 [Code] 的 CurUninstallStepChanged），
; 英文用户会看到一段中文。安装器没有运行期的 JSON 语言包（它跑在程序启动之前），
; 所以这里用 Inno 自己的多语言机制：CustomMessages 按 [Languages] 的当前语言取值，
; 没定义的语言回退到默认语言（english）。占位符用 %1，由 FmtMessage 传入路径。
english.UninstallKeepResults=EchoDesk will keep the batch results folder as it is:%n%n%1%n%nFiles created while the program was running (sessions, models, caches, logs, ...) will be removed.
english.UninstallKeepData=Your user data was backed up before uninstall:%n%n%1%n%nConfig, sessions, skills and model files are kept in place and are never deleted by the uninstaller. Batch results remain at:%n%n%2
; 中文语言文件已随仓库内置，因此中文 CustomMessages 可以无条件声明。
chinesesimplified.UninstallKeepResults=批量处理结果目录会原样保留：%n%n%1%n%n其余由程序运行时产生的文件（会话、模型、缓存、日志等）会被删除。
chinesesimplified.UninstallKeepData=卸载前已备份用户数据：%n%n%1%n%n配置、会话、插件与模型文件原样保留，卸载程序不会删除它们。批量处理结果仍在：%n%n%2

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[InstallDelete]
; [EN] Upgrade hygiene: remove old build/runtime garbage before the new payload is
; [EN] written. These paths are regenerable. Models, config.json, sessions, registries,
; [EN] user skills and results are deliberately absent: they must never be touched.
; [ZH] 升级清理：写入新文件之前移除旧版遗留的构建/运行期垃圾。下列路径都可再生。
; [ZH] 模型、config.json、会话、注册表、用户插件与 results 刻意不在此列：绝不触碰。
Type: filesandordirs; Name: "{app}\app\bin"
Type: filesandordirs; Name: "{app}\app\.cache"
Type: filesandordirs; Name: "{app}\.cache"
Type: filesandordirs; Name: "{app}\cache"
Type: filesandordirs; Name: "{app}\app\backend\.cache"
Type: filesandordirs; Name: "{app}\app\backend\__pycache__"
Type: filesandordirs; Name: "{app}\app\backend\_internal\__pycache__"

[Files]
; 无条件排除模型文件（本版只有一份安装程序，模型不进包；见文件头说明）。
; Excludes 的匹配语义是**相对 Source 目录的路径**（不是含源目录前缀的完整路径），
; 而且模式可以只匹配路径的某一级起（实测：app\backend\models\* 与 *\models\*
; 都能命中，但写成 *\app\backend\models\* 会命中失败 —— 前面多出来的 *\ 要求
; 在 app 之前还有一级目录）。这条实测结论花了 7 组对照才确定，别凭直觉改。
; 同时用 *.gguf 兜底：安装程序就该"一个 .gguf 都没有"，这样校验断言是结构性成立的。
; 模型文件一个都不进包时，空的 models\ 目录也不会被创建（[Dirs] 也没有它）。
; [EN] ``notimestamp`` is required for reproducible output: without it, Inno Setup stores the
; [EN] last-write time of every source file inside the installation payload.
; [ZH] ``notimestamp`` 是可复现产物的必需项：缺少它时，Inno Setup 会把每个源文件的最后
; [ZH] 写入时间写入安装数据。
; [EN] User data must survive an upgrade. The wildcard therefore excludes every file the
; [EN] program writes at runtime; the two explicit entries below copy a default config and the
; [EN] example skills only when they do not exist yet (``onlyifdoesntexist``). An upgrade can
; [EN] therefore never replace config.json, a session, a registry, a model or a user skill.
; [ZH] 用户数据必须在升级后继续存在。因此通配条目排除了程序运行期写入的每一个文件；下方两条
; [ZH] 显式条目只在目标不存在时复制默认配置与示例插件（``onlyifdoesntexist``）。升级因此
; [ZH] 永远不会替换 config.json、会话、注册表、模型或用户插件。
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Excludes: "app\backend\models\*,*\models\*,*.gguf,app\backend\config.json,app\backend\config.user.json,app\backend\session.json,app\backend\browser_state.json,app\backend\models_registry.json,app\backend\skills_registry.json,app\backend\skills_local.json,app\backend\market_cache.json,app\backend\summary_cache.json,app\backend\automation_log.jsonl,app\backend\automation_processed.json,app\backend\skills_dev\*,ui_state.json,snippets.json,tools\*,tests\*,records\*,prompts\*"; Flags: ignoreversion recursesubdirs createallsubdirs notimestamp
Source: "{#MySourceDir}\app\backend\config.json"; DestDir: "{app}\app\backend"; Flags: onlyifdoesntexist notimestamp
Source: "{#MySourceDir}\app\backend\skills_dev\*"; DestDir: "{app}\app\backend\skills_dev"; Flags: onlyifdoesntexist recursesubdirs createallsubdirs notimestamp

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
; ===========================================================================
;  卸载清理：程序**运行时新建**的文件（Inno 只知道自己释放了什么，这些它不知道）
; ===========================================================================
;  规则（v0.2.4 补丁修订：**用户数据绝不删除**）：
;    * 下面只列"可再生"的缓存、日志与快捷方式；
;    * config.json / config.user.json / session.json / 各注册表 / models\ / skills\ /
;      skills_dev\ / ui_state.json / snippets.json 一律**不列** —— 它们是用户数据。
;      卸载时 `[Code]` 会先把小型用户数据备份到
;      `%LOCALAPPDATA%\EchoDesk\uninstall-backup\<时间戳>\`，然后这些文件原样留在安装目录里。
;      **绝不写 `Name: "{app}"` 整目录删除**：那会连 results\ 与用户数据一起删掉。
;    * **{app}\results\ 一个都不列** —— 用户的批量处理结果必须原样保留。
;    * {app}\cache\ 是模型市场搜索结果缓存（{user_data_root}\cache\model_hub），可再生。
;
;  ---- 可再生：缓存、日志、快捷方式 ----
Type: files;          Name: "{app}\*.lnk"
Type: filesandordirs; Name: "{app}\cache"
Type: files;          Name: "{app}\app\frontend_crash.log"
Type: files;          Name: "{app}\app\backend\backend_crash.log"
Type: files;          Name: "{app}\app\backend\skills_dev.log"

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

// 卸载开始时告知"结果目录会被保留"，避免用户以为卸载把结果也删了。
// 静默卸载（/VERYSILENT，验收脚本走的就是这条路）不弹窗，否则会卡住自动化。
// [EN] Copy one small user-data file into the isolated backup directory.
// [ZH] 把一个小型用户数据文件复制到隔离备份目录。
function BackupUserFile(const Src, DstDir: String): Boolean;
begin
  Result := False;
  if not FileExists(Src) then
    Exit;
  ForceDirectories(DstDir);
  Result := FileCopy(Src, AddBackslash(DstDir) + ExtractFileName(Src), False);
  if Result then
    Log('User data backed up: ' + Src);
end;

// [EN] Back up user data before uninstall. Models are large and are deliberately kept
// [EN] in place (the installer never overwrites them); config, sessions, registries and
// [EN] UI preferences are small and are copied to LocalAppData as a safety net.
// [ZH] 卸载前备份用户数据。模型很大，刻意留在原位（安装程序从不覆盖它们）；配置、会话、
// [ZH] 注册表与界面偏好体积小，复制到 LocalAppData 作为安全网。
function BackupUserData(): String;
var
  Dst: String;
begin
  Dst := ExpandConstant('{localappdata}\EchoDesk\uninstall-backup\') +
         GetDateTimeString('yyyymmdd-hhnnss', '-', '-');
  ForceDirectories(Dst);
  BackupUserFile(ExpandConstant('{app}\app\backend\config.json'), Dst);
  BackupUserFile(ExpandConstant('{app}\app\backend\config.user.json'), Dst);
  BackupUserFile(ExpandConstant('{app}\app\backend\session.json'), Dst);
  BackupUserFile(ExpandConstant('{app}\app\backend\browser_state.json'), Dst);
  BackupUserFile(ExpandConstant('{app}\app\backend\models_registry.json'), Dst);
  BackupUserFile(ExpandConstant('{app}\app\backend\skills_registry.json'), Dst);
  BackupUserFile(ExpandConstant('{app}\app\backend\skills_local.json'), Dst);
  BackupUserFile(ExpandConstant('{app}\app\backend\market_cache.json'), Dst);
  BackupUserFile(ExpandConstant('{app}\app\backend\summary_cache.json'), Dst);
  BackupUserFile(ExpandConstant('{app}\app\backend\automation_log.jsonl'), Dst);
  BackupUserFile(ExpandConstant('{app}\app\backend\automation_processed.json'), Dst);
  BackupUserFile(ExpandConstant('{app}\ui_state.json'), Dst);
  BackupUserFile(ExpandConstant('{app}\snippets.json'), Dst);
  Result := Dst;
end;

function InitializeUninstall(): Boolean;
var
  ResultsDir: String;
  BackupDir: String;
begin
  Result := True;
  // [EN] The backup runs even in silent mode; it is data safety, not a prompt.
  // [ZH] 备份在静默模式下也会执行；它是数据安全措施，不是提示。
  BackupDir := BackupUserData();
  if UninstallSilent then
    Exit;
  ResultsDir := ExpandConstant('{app}\results');
  MsgBox(FmtMessage(CustomMessage('UninstallKeepData'), [BackupDir, ResultsDir]),
         mbInformation, MB_OK);
end;
