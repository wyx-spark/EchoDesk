# [EN] EchoDesk Reader Windows build entry point.
#
# This script is the Reader-profile counterpart of tools/build_release.ps1. It
# produces a portable folder that contains only the text-processing backend and
# the Avalonia front end, and optionally wraps that folder into
# EchoDesk-Reader-Setup.exe through Inno Setup.
#
# Design constraints:
#   * No browser automation, multimodal or RAG assets are shipped. After the
#     backend is collected, any residue of the removed modules is deleted and a
#     missing-module check aborts the build if one is found.
#   * The Python environment is resolved exactly like the main release chain:
#     PyInstaller must come from the clean virtual environment, never from PATH.
#   * The Inno Setup definition is generated in the temporary directory from
#     tools/installer.iss, so this script does not add a second maintained
#     installer description to the repository.
#
# [ZH] EchoDesk Reader Windows 构建入口。
#
# 本脚本是 tools/build_release.ps1 的 Reader 版本：只产出文本处理后端与 Avalonia
# 前端组成的便携目录，并可选通过 Inno Setup 封装为 EchoDesk-Reader-Setup.exe。
#
# 设计约束：
#   * 不随包分发浏览器自动化、多模态与 RAG 资源。后端收集完成后会删除被删模块的
#     残留，并在发现残留时中止构建（防止"删了源码但打包机环境里还有"）。
#   * Python 环境的解析方式与主线发布链一致：PyInstaller 必须来自干净虚拟环境，
#     绝不回退到 PATH。
#   * Inno Setup 定义在临时目录中由 tools/installer.iss 生成，仓库里不新增第二份
#     需要长期维护的安装脚本。
#
# 用法 / Usage:
#   tools\build_reader.ps1 -MakeInstaller
#   tools\build_reader.ps1 -SkipBackend -ReuseBackendFrom publish\reader\backend
#
# 退出码 / Exit codes: 0 = success, 1 = build failure, 2 = usage/environment problem.
[CmdletBinding()]
param(
    # [EN] Version override. Empty reads APP_VERSION from backend/version.py.
    # [ZH] 版本号覆盖。为空时读取 backend/version.py 的 APP_VERSION。
    [string]$Version = "",
    # [EN] Build output root. / [ZH] 构建输出根目录。
    [string]$OutRoot = "",
    # [EN] Clean virtual environment used for PyInstaller. / [ZH] 用于 PyInstaller 的干净虚拟环境。
    [string]$CleanVenv = "artifacts\clean-venv",
    # [EN] Skip the PyInstaller step and reuse an existing backend folder.
    # [ZH] 跳过 PyInstaller，复用既有后端目录。
    [switch]$SkipBackend,
    # [EN] Existing backend folder when -SkipBackend is set.
    # [ZH] 设置 -SkipBackend 时使用的既有后端目录。
    [string]$ReuseBackendFrom = "",
    # [EN] Also compile EchoDesk-Reader-Setup.exe through Inno Setup.
    # [ZH] 同时通过 Inno Setup 编译 EchoDesk-Reader-Setup.exe。
    [switch]$MakeInstaller,
    # [EN] Explicit path to ISCC.exe. / [ZH] ISCC.exe 的显式路径。
    [string]$ISCC = "",
    # [EN] Keep the intermediate PyInstaller work directory for diagnosis.
    # [ZH] 保留 PyInstaller 中间工作目录以便排查。
    [switch]$KeepWork
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
Set-Location $Root

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "    $Text" -ForegroundColor Green }
function Write-Warn2([string]$Text) { Write-Host "    $Text" -ForegroundColor Yellow }

function Invoke-Native([string]$Exe, [string[]]$Arguments, [string]$What) {
    # [EN] Invoke a native tool and turn a non-zero exit into a terminating error.
    # [ZH] 调用原生工具，并把非零退出码转换为终止性错误。
    Write-Host "    $Exe $($Arguments -join ' ')" -ForegroundColor DarkGray
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$What 失败（exit=$LASTEXITCODE） / $What failed (exit=$LASTEXITCODE)"
    }
}

Write-Step "读取版本号 / Resolve version"
if (-not $Version) {
    $VersionPath = Join-Path $Root "backend\version.py"
    if (-not (Test-Path $VersionPath)) {
        throw "找不到 $VersionPath / Missing $VersionPath"
    }
    $VersionLine = Select-String -Path $VersionPath -Pattern '^APP_VERSION\s*=\s*"([^"]+)"' |
        Select-Object -First 1
    if (-not $VersionLine) {
        throw "无法从 backend/version.py 解析 APP_VERSION / Cannot parse APP_VERSION"
    }
    $Version = $VersionLine.Matches[0].Groups[1].Value
}
Write-Ok "版本 / Version: $Version"

if (-not $OutRoot) { $OutRoot = Join-Path $Root "publish" }
$ReaderRoot = Join-Path $OutRoot "EchoDesk-Reader-v$Version"
$BackendOut = Join-Path $OutRoot "reader\backend"
$FrontendOut = Join-Path $OutRoot "reader\frontend"
$AppDir = Join-Path $ReaderRoot "app"
$BackendTarget = Join-Path $AppDir "backend"

Write-Step "准备输出目录 / Prepare output directories"
if (Test-Path $ReaderRoot) { Remove-Item -Recurse -Force $ReaderRoot }
New-Item -ItemType Directory -Force -Path $AppDir, $BackendTarget | Out-Null

# ---------------------------------------------------------------------------
# 1. Backend (PyInstaller onedir)
# ---------------------------------------------------------------------------
if ($SkipBackend) {
    Write-Step "复用既有后端 / Reuse existing backend"
    if (-not $ReuseBackendFrom -or -not (Test-Path $ReuseBackendFrom)) {
        throw "-SkipBackend 需要有效的 -ReuseBackendFrom / -SkipBackend requires -ReuseBackendFrom"
    }
    Copy-Item -Recurse -Force (Join-Path $ReuseBackendFrom "*") $BackendTarget
} else {
    Write-Step "构建后端 / Build backend (PyInstaller onedir)"
    $VenvRoot = if ([System.IO.Path]::IsPathRooted($CleanVenv)) { $CleanVenv } else { Join-Path $Root $CleanVenv }
    $PyInstallerExe = Join-Path $VenvRoot "Scripts\pyinstaller.exe"
    if (-not (Test-Path $PyInstallerExe)) {
        # [EN] A missing clean environment is an environment problem, not a build
        # [EN] failure: report the exact command instead of silently using PATH.
        # [ZH] 干净环境缺失属于环境问题而非构建失败：直接给出确切命令，绝不静默使用 PATH。
        throw @"
找不到干净虚拟环境中的 PyInstaller：$PyInstallerExe
Clean-venv PyInstaller not found: $PyInstallerExe
请先执行 / Prepare it with:
  python -m venv $CleanVenv
  $CleanVenv\Scripts\python.exe -m pip install --no-deps -r backend\requirements.lock -r backend\requirements-build.lock
"@
    }
    $WorkDir = Join-Path $Root "temp\pyinstaller-reader"
    if (Test-Path $WorkDir) { Remove-Item -Recurse -Force $WorkDir }
    New-Item -ItemType Directory -Force -Path $WorkDir, $BackendOut | Out-Null
    $Spec = Join-Path $Root "backend\EchoBackend.spec"
    Invoke-Native $PyInstallerExe @(
        "--noconfirm", "--clean", "--distpath", $BackendOut,
        "--workpath", $WorkDir, $Spec
    ) "PyInstaller"
    $Collected = Join-Path $BackendOut "EchoDeskBackend"
    if (-not (Test-Path $Collected)) {
        throw "PyInstaller 未产出 EchoDeskBackend / PyInstaller produced no EchoDeskBackend"
    }
    Copy-Item -Recurse -Force (Join-Path $Collected "*") $BackendTarget
    if (-not $KeepWork) { Remove-Item -Recurse -Force $WorkDir -ErrorAction SilentlyContinue }
}

# ---------------------------------------------------------------------------
# 2. Remove residue of the modules deleted in the Reader profile
# ---------------------------------------------------------------------------
Write-Step "清除已删除模块残留 / Remove deleted-module residue"
$RemovedModules = @("vision", "knowledge", "speech", "media", "extractive",
                    "memory", "retrieval", "automation", "playwright", "pyee", "greenlet")
$Leftovers = @()
foreach ($Module in $RemovedModules) {
    Get-ChildItem -Path $BackendTarget -Recurse -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq "$Module.py" -or $_.Name -eq "$Module.pyc" -or
                       $_.Name -eq "$Module.pyd" -or $_.Name -like "$Module.*.pyd" } |
        ForEach-Object {
            $Leftovers += $_.FullName
            Remove-Item -Force $_.FullName
        }
    Get-ChildItem -Path $BackendTarget -Recurse -Force -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in @("$Module", "$Module.dist-info") } |
        ForEach-Object {
            $Leftovers += $_.FullName
            Remove-Item -Recurse -Force $_.FullName
        }
}
if ($Leftovers.Count -gt 0) {
    Write-Warn2 "已删除 $($Leftovers.Count) 项残留 / Removed $($Leftovers.Count) leftover items"
    $Leftovers | ForEach-Object { Write-Warn2 "  $_" }
} else {
    Write-Ok "无残留 / No residue"
}

# [EN] Hard gate: a residual import means the packaged backend is not the Reader
# [EN] profile, so the build must stop rather than ship an unverifiable artefact.
# [ZH] 硬门禁：仍有残留导入说明打包出的后端不是 Reader 版本，必须停止而不是发布
# [ZH] 一个无法核实的产物。
$ResidualPattern = 'import\s+(vision|knowledge|speech|media|extractive|memory|retrieval|automation)\b|from\s+(vision|knowledge|speech|media|extractive|memory|retrieval|automation)\s+import'
$ResidualHits = Get-ChildItem -Path $BackendTarget -Filter "*.py" -Recurse -Force -ErrorAction SilentlyContinue |
    Select-String -Pattern $ResidualPattern
if ($ResidualHits) {
    $ResidualHits | ForEach-Object { Write-Warn2 "$($_.Path):$($_.LineNumber): $($_.Line.Trim())" }
    throw "后端仍引用已删除模块 / Backend still imports removed modules"
}

# ---------------------------------------------------------------------------
# 3. Front end (Avalonia publish)
# ---------------------------------------------------------------------------
Write-Step "构建前端 / Build front end (Avalonia publish)"
$Project = Join-Path $Root "frontend-avalonia\EchoDesk.Avalonia\EchoDesk.Avalonia.csproj"
if (-not (Test-Path $Project)) { throw "找不到 Avalonia 工程 / Missing $Project" }
Invoke-Native "dotnet" @(
    "publish", $Project, "-c", "Release", "-r", "win-x64",
    "--self-contained", "false", "-o", $FrontendOut, "-v", "minimal"
) "dotnet publish"

Copy-Item -Recurse -Force (Join-Path $FrontendOut "*") $AppDir

# ---------------------------------------------------------------------------
# 4. Shipped documentation and language packs
# ---------------------------------------------------------------------------
Write-Step "复制随包文档与语言包 / Copy shipped docs and language packs"
$LangSrc = Join-Path $Root "backend\lang"
$LangDst = Join-Path $BackendTarget "lang"
if (Test-Path $LangSrc) {
    New-Item -ItemType Directory -Force -Path $LangDst | Out-Null
    Copy-Item -Force (Join-Path $LangSrc "*.json") $LangDst
}
foreach ($Doc in @("LICENSE", "LICENSE.zh-Hans.md", "NOTICE", "NOTICE.zh-Hans.md", "README.md")) {
    $Src = Join-Path $Root $Doc
    if (Test-Path $Src) { Copy-Item -Force $Src $ReaderRoot }
}

# ---------------------------------------------------------------------------
# 5. Optional installer
# ---------------------------------------------------------------------------
if ($MakeInstaller) {
    Write-Step "编译安装包 / Compile installer"
    $Iscc = $ISCC
    if (-not $Iscc) {
        $Candidates = @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
        )
        $Iscc = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $Iscc -or -not (Test-Path $Iscc)) {
        throw "找不到 ISCC.exe，请用 -ISCC 指定 / ISCC.exe not found; pass -ISCC"
    }
    $Template = Join-Path $Root "tools\installer.iss"
    $Generated = Join-Path $Root "temp\installer.reader.iss"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Generated) | Out-Null
    $IssText = Get-Content -Raw -LiteralPath $Template
    $IssText = $IssText.Replace('#define MyAppName "EchoDesk"',
                                '#define MyAppName "EchoDesk Reader"')
    $IssText = $IssText.Replace('OutputBaseFilename=EchoDesk-v{#MyAppVersion}-Setup',
                                'OutputBaseFilename=EchoDesk-Reader-Setup')
    $IssText = $IssText.Replace('OutputDir=..\publish',
                                "OutputDir=$OutRoot")
    $IssText = $IssText.Replace('Source: "{#MySourceDir}\*";',
                                'Source: "{#MySourceDir}\*";')
    Set-Content -LiteralPath $Generated -Value $IssText -Encoding UTF8
    Invoke-Native $Iscc @(
        "/DMyAppVersion=$Version",
        "/DMySourceDir=$ReaderRoot",
        $Generated
    ) "ISCC"
    $Setup = Join-Path $OutRoot "EchoDesk-Reader-Setup.exe"
    if (-not (Test-Path $Setup)) {
        throw "安装包未生成 / Installer not produced: $Setup"
    }
    Write-Ok "安装包 / Installer: $Setup"
}

Write-Step "完成 / Done"
Write-Ok "便携目录 / Portable folder: $ReaderRoot"
exit 0
