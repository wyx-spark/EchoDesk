@echo off
rem ============================================================================
rem  下载 EchoDesk 构建所需的第三方命令行工具（rcedit）
rem
rem  rcedit 用于为可执行文件写入版本资源（CompanyName / FileVersion / 图标等）。
rem  二进制体积约 1.3MB，不入库，由本脚本按需下载。
rem
rem  用法: tools\fetch_tools.bat
rem ============================================================================
setlocal

set "ROOT=%~dp0.."
pushd "%ROOT%"
set "ROOT=%CD%"
popd

set "RCEDIT=%ROOT%\tools\rcedit-x64.exe"
set "URL=https://github.com/electron/rcedit/releases/download/v2.0.0/rcedit-x64.exe"

if exist "%RCEDIT%" (
    echo rcedit 已存在，跳过下载: %RCEDIT%
    exit /b 0
)

echo 正在下载 rcedit ...
curl.exe -sS -L --max-time 180 -o "%RCEDIT%" "%URL%"
if errorlevel 1 (
    echo [错误] 下载失败，请检查网络后重试，或手动下载并放到 %RCEDIT%
    exit /b 1
)
if not exist "%RCEDIT%" (
    echo [错误] 下载后未找到文件: %RCEDIT%
    exit /b 1
)

"%RCEDIT%" >nul 2>&1
if errorlevel 2 (
    echo [错误] rcedit 无法运行
    exit /b 1
)
echo 完成: %RCEDIT%
exit /b 0
