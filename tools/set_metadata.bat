@echo off
chcp 65001 >nul
rem ============================================================================
rem  EchoDesk 可执行文件元数据与图标统一设置（rcedit）
rem
rem  用法:  tools\set_metadata.bat [发布目录]
rem         默认发布目录: publish\EchoDesk-v0.2.0
rem
rem  执行时机: 编译构建完成之后、打包发布之前（见 tools\build_release.ps1）
rem
rem  设置的字段:
rem    CompanyName       Void Entropia Studio
rem    LegalCopyright    Copyright (C) Void Entropia Studio
rem    FileDescription   EchoDesk - Local AI Desktop Assistant
rem    ProductName       EchoDesk
rem    OriginalFilename  对应可执行文件名
rem    InternalName      对应可执行文件名
rem    Comments          Powered by Void Entropia Studio
rem    FileVersion       0.2.0.0
rem    ProductVersion    0.2.0.0
rem    图标              assets\EchoDesk.ico（16/32/48/64/128/256 六尺寸）
rem ============================================================================
setlocal

set "ROOT=%~dp0.."
pushd "%ROOT%"
set "ROOT=%CD%"
popd

set "RCEDIT=%ROOT%\tools\rcedit-x64.exe"
set "ICON=%ROOT%\assets\EchoDesk.ico"

set "VERSION=0.2.0.0"
set "COMPANY=Void Entropia Studio"
set "COPYRIGHT=Copyright (C) Void Entropia Studio"
set "DESC=EchoDesk - Local AI Desktop Assistant"
set "PRODUCT=EchoDesk"
set "COMMENTS=Powered by Void Entropia Studio"

set "TARGET=%~1"
if "%TARGET%"=="" set "TARGET=%ROOT%\publish\EchoDesk-v0.2.0"

if not exist "%RCEDIT%" goto :no_rcedit
if not exist "%ICON%" goto :no_icon
if not exist "%TARGET%" goto :no_target

echo 正在写入版本资源与图标: %TARGET%
set "COUNT=0"

call :apply "%TARGET%\EchoDesk.exe"
call :apply "%TARGET%\backend\EchoDeskBackend.exe"

echo 完成，共处理 %COUNT% 个可执行文件。
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
