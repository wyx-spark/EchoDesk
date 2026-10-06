@echo off
chcp 65001 >nul
rem Platform: Windows only -- downloads the Windows build tools (rcedit, ISCC) into tools/.
rem 适用平台：仅 Windows
rem
rem  **为什么第一行标注用英文**（v0.2.2 全面双语与三平台友好 实测踩的坑）：
rem  cmd.exe 在 `chcp 65001` 生效**之前**是按传统代码页（简体中文 Windows 上是 GBK）
rem  解析批处理文本的。把 UTF-8 中文放在代码页切换之前，多字节序列会把行尾吃掉，
rem  后面的内容被当成命令执行 —— 本轮就是这么把发布构建弄失败的：
rem      'get' is not recognized as an internal or external command
rem      !! 发布构建失败：元数据写入 失败（退出码 1）
rem  因此本文件（原先没有 chcp 行、却带着中文注释）补上了 `chcp 65001 >nul`：
rem  在它**之后**的中文注释是安全的，之前的不是。门禁 tools/audit_repo.py --check
rem  会检查"非 ASCII 是否出现在 chcp 之前"。
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
