@echo off
chcp 65001 >nul
rem Platform: Windows only -- writes version resources into the frontend exe/dll (rcedit).
rem 适用平台：仅 Windows。本行放在 chcp 65001 **之后**是刻意的：在代码页切换之前，
rem cmd.exe 按传统代码页解析 UTF-8 中文会把行尾吃掉（见 tools/fetch_tools.bat 里的说明），
rem 本轮就是这样把发布构建弄失败的。
rem ============================================================================
rem  EchoDesk 可执行文件元数据与图标统一设置（rcedit）
rem
rem  用法:  tools\set_metadata.bat [发布目录] [版本]
rem         版本**必填**（四段式，如 0.2.2.0）；缺省时本脚本会尝试用 python 读
rem         backend\version.py —— 读不到就报错退出，**不再有写死的默认版本**。
rem         发布目录默认按版本派生: publish\EchoDesk-v<三段式版本>
rem
rem  执行时机: 编译构建完成之后、打包发布之前（见 tools\build_release.ps1）
rem
rem  【重要】本脚本只处理前端 EchoDesk.exe，不处理后端 EchoDeskBackend.exe。
rem  原因：PyInstaller 会把 PKG 归档追加在 PE 映像之后，而 rcedit 重写资源时会
rem  重建 PE 映像并丢掉这段追加数据，导致后端启动即报
rem  「Could not load PyInstaller's embedded PKG archive」。
rem  因此后端的版本资源与图标改由 backend\EchoBackend.spec 的 version / icon
rem  参数在追加归档之前写入，字段与下表完全一致。
rem
rem  设置的字段:
rem    CompanyName       Void Entropia Studio
rem    LegalCopyright    Copyright (C) Void Entropia Studio
rem    FileDescription   EchoDesk - Local AI Desktop Assistant
rem    ProductName       EchoDesk
rem    OriginalFilename  对应可执行文件名
rem    InternalName      对应可执行文件名
rem    Comments          Powered by Void Entropia Studio
rem    FileVersion       （取自 backend\version.py，四段式；不再写死）
rem    ProductVersion    同上
rem    图标              assets\EchoDesk.ico（16/32/48/64/128/256 六尺寸）
rem ============================================================================
setlocal

set "ROOT=%~dp0.."
pushd "%ROOT%"
set "ROOT=%CD%"
popd

set "RCEDIT=%ROOT%\tools\rcedit-x64.exe"
set "ICON=%ROOT%\assets\EchoDesk.ico"

rem 版本号从**调用方**传入（tools\build_release.ps1 会从 backend\version.py 读出后传进来）。
rem 【为什么不在这里自己读 version.py】批处理里嵌 python 一行命令 + 反引号 for/f + 单双引号
rem 混排，实测在 cmd 下会被解析成几条乱七八糟的命令（报 "The system cannot find the path
rem specified" 与 "'get' is not recognized"），把整个脚本带坏。版本号本来就只有
rem build_release.ps1 一个调用方，由它派生既可靠又保持"唯一来源"。
rem 缺参数就**报错退出**：写死默认值正是本次要修的根因（原来这里是 set "VERSION=0.2.1.0"）。
set "VERSION=%~2"
if not "%VERSION%"=="" goto :have_version
echo [错误] 未指定版本号（四段式，如 0.2.2.0）。
echo         用法: tools\set_metadata.bat [发布目录] [版本]
echo         调用方 tools\build_release.ps1 会从 backend\version.py 读出后传入。
exit /b 2
:have_version
set "COMPANY=Void Entropia Studio"
set "COPYRIGHT=Copyright (C) Void Entropia Studio"
set "DESC=EchoDesk - Local AI Desktop Assistant"
set "PRODUCT=EchoDesk"
set "COMMENTS=Powered by Void Entropia Studio"

set "TARGET=%~1"
if not "%TARGET%"=="" goto :have_target
rem 默认发布目录按版本派生：0.2.2.0 → publish\EchoDesk-v0.2.2\app
rem （原来写死 v0.2.1，升版本后会指向一个不存在的目录）
set "TARGET=%ROOT%\publish\EchoDesk-v%VERSION:~0,-2%\app"
:have_target

rem 修复七：程序本体在 app\ 下，若调用方传的是发布根目录则自动下探一层
if exist "%TARGET%\EchoDesk.exe" goto :target_ok
if exist "%TARGET%\app\EchoDesk.exe" set "TARGET=%TARGET%\app"
:target_ok

if not exist "%RCEDIT%" goto :no_rcedit
if not exist "%ICON%" goto :no_icon
if not exist "%TARGET%" goto :no_target

echo 正在写入版本资源与图标: %TARGET%
set "COUNT=0"

call :apply "%TARGET%\EchoDesk.exe"

echo 完成，共处理 %COUNT% 个可执行文件（后端由 PyInstaller version/icon 参数写入）。
exit /b 0

:apply
if not exist "%~1" (
    echo   [跳过] 未找到 %~1
    exit /b 0
)
echo   - %~nx1
"%RCEDIT%" "%~1" --set-icon "%ICON%" --set-version-string "CompanyName" "%COMPANY%" --set-version-string "LegalCopyright" "%COPYRIGHT%" --set-version-string "FileDescription" "%DESC%" --set-version-string "ProductName" "%PRODUCT%" --set-version-string "OriginalFilename" "%~nx1" --set-version-string "InternalName" "%~nx1" --set-version-string "Comments" "%COMMENTS%" --set-file-version "%VERSION%" --set-product-version "%VERSION%"
if errorlevel 1 goto :apply_failed
set /a COUNT+=1
exit /b 0

:apply_failed
echo   [失败] %~nx1 元数据写入失败
exit /b 1

:no_rcedit
echo [错误] 未找到 rcedit: %RCEDIT%
echo         请先执行 tools\fetch_tools.bat 下载 rcedit
exit /b 1

:no_icon
echo [错误] 未找到图标: %ICON%
echo         请先执行 python tools\make_icon.py 生成图标
exit /b 1

:no_target
echo [错误] 未找到发布目录: %TARGET%
exit /b 1
