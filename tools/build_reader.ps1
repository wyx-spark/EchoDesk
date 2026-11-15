# [EN] EchoDesk Reader Windows build entry point.
#
# This script is the Reader-profile counterpart of tools/build_release.ps1. It
# produces a portable folder that contains only the text-processing backend and
# the Avalonia front end, and optionally wraps that folder into
# EchoDesk-Reader-v<version>-Setup.exe through Inno Setup.
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
# 前端组成的便携目录，并可选通过 Inno Setup 封装为 EchoDesk-Reader-v<版本>-Setup.exe。
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
    # [EN] Also compile EchoDesk-Reader-v<version>-Setup.exe through Inno Setup.
    # [ZH] 同时通过 Inno Setup 编译 EchoDesk-Reader-v<版本>-Setup.exe。
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
    # [EN] APP_VERSION carries the display prefix ("v0.2.6-reader"), but the output
    # [EN] folder and the ISCC define use the bare form, otherwise the tree would be
    # [EN] named EchoDesk-Reader-vv0.2.6-reader. The main release chain strips the
    # [EN] prefix for exactly the same reason (see verify_release.ps1).
    # [ZH] APP_VERSION 带展示用前缀（"v0.2.6-reader"），而输出目录与 ISCC 定义要用
    # [ZH] 无前缀形式，否则目录会变成 EchoDesk-Reader-vv0.2.6-reader。主线发布链
    # [ZH] 出于同一原因也做了去前缀（见 verify_release.ps1）。
    $Version = $VersionLine.Matches[0].Groups[1].Value.TrimStart('v')
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
    # [EN] Accept either the PyInstaller dist root (publish\reader\backend, which holds the
    # [EN] EchoDeskBackend folder) or the onedir folder itself (…\EchoDeskBackend, whose
    # [EN] contents are the backend). Copying the dist root verbatim would nest an extra
    # [EN] EchoDeskBackend level inside app\backend and the installed exe would be missing.
    # [ZH] 同时接受两种来源：PyInstaller 的 dist 根（publish\reader\backend，下面是
    # [ZH] EchoDeskBackend 目录）或 onedir 目录本身（…\EchoDeskBackend，其内容就是后端）。
    # [ZH] 若把 dist 根整份拷进去，app\backend 下会多套一层 EchoDeskBackend，安装后找不到 exe。
    $ReuseSrc = $ReuseBackendFrom
    $NestedOnedir = Join-Path $ReuseBackendFrom "EchoDeskBackend"
    if (Test-Path $NestedOnedir) { $ReuseSrc = $NestedOnedir }
    Copy-Item -Recurse -Force (Join-Path $ReuseSrc "*") $BackendTarget
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

# [EN] The shipped layout documented in README needs three more things the frontend
# [EN] publish does not carry: the default configuration (README lists
# [EN] app\backend\config.json as "All configuration"), the bundled example skills and
# [EN] the program icon that installer.iss points its shortcuts at. Omitting them makes
# [EN] ISCC abort at [Files]/[Icons] and leaves a portable tree that contradicts the docs.
# [ZH] README 记录的交付布局还需要三样前端产物里没有的东西：默认配置（README 把
# [ZH] app\backend\config.json 写作"全部配置"）、随包示例插件，以及 installer.iss 给
# [ZH] 快捷方式指定的程序图标。缺任何一样，ISCC 都会在 [Files]/[Icons] 处中断，
# [ZH] 或者产出一个与文档不符的便携目录。
$ConfigSrc = Join-Path $Root "backend\config.json"
if (Test-Path $ConfigSrc) {
    Copy-Item -Force $ConfigSrc (Join-Path $BackendTarget "config.json")
    Write-Ok "app\backend\config.json"
}
$ExampleSkills = Join-Path $Root "examples\skills"
if (Test-Path $ExampleSkills) {
    Copy-Item -Recurse -Force $ExampleSkills (Join-Path $BackendTarget "skills_dev")
    Write-Ok "app\backend\skills_dev\"
}
$IconSrc = Join-Path $Root "assets\EchoDesk.ico"
if (-not (Test-Path $IconSrc)) {
    throw "未找到图标 $IconSrc，请先执行: python tools\make_icon.py / Missing $IconSrc"
}
Copy-Item -Force $IconSrc (Join-Path $AppDir "EchoDesk.ico")
Write-Ok "app\EchoDesk.ico"

# ---------------------------------------------------------------------------
# 5. Optional installer
# ---------------------------------------------------------------------------
if ($MakeInstaller) {
    Write-Step "编译安装包 / Compile installer"
    $Iscc = $ISCC
    if (-not $Iscc) {
        $Candidates = @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
            # [EN] Inno Setup 6 installs per-user by default ("Install for me only"),
            # [EN] which lands here and is invisible to the two Program Files lookups.
            # [ZH] Inno Setup 6 默认按「仅为我安装」落到用户目录，上面两个 Program Files
            # [ZH] 路径都看不到它。
            "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
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
    # [EN] The installer carries the numeric version so several Reader builds can sit in
    # [EN] the same publish\ folder without one silently overwriting another; the
    # [EN] prerelease suffix stays in the display version only.
    # [ZH] 安装包名带数字版本号，使多个 Reader 构建可以并存在 publish\ 里而不互相覆盖；
    # [ZH] 预发布后缀只保留在展示用版本号里。
    $SetupVersion = ($Version -split '[-+]')[0]
    $IssText = $IssText.Replace('OutputBaseFilename=EchoDesk-v{#MyAppVersion}-Setup',
                                "OutputBaseFilename=EchoDesk-Reader-v$SetupVersion-Setup")
    # [EN] The version resource's OriginalFilename is a literal in the template; leaving it
    # [EN] alone makes the Reader installer claim it is EchoDesk-v<full version>-Setup.exe.
    # [ZH] 版本资源里的 OriginalFilename 在模板中是字面量；不改它，Reader 安装包会声称自己叫
    # [ZH] EchoDesk-v<完整版本>-Setup.exe。
    $IssText = $IssText.Replace('VersionInfoOriginalFileName=EchoDesk-v{#MyAppVersion}-Setup.exe',
                                "VersionInfoOriginalFileName=EchoDesk-Reader-v$SetupVersion-Setup.exe")
    $IssText = $IssText.Replace('OutputDir=..\publish',
                                "OutputDir=$OutRoot")
    $IssText = $IssText.Replace('Source: "{#MySourceDir}\*";',
                                'Source: "{#MySourceDir}\*";')
    Set-Content -LiteralPath $Generated -Value $IssText -Encoding UTF8
    # [EN] installer.iss references its language file as InnoSetup\ChineseSimplified.isl,
    # [EN] relative to the .iss itself. The main chain compiles the template in place
    # [EN] (tools\), but this script generates a copy under temp\, so the companion
    # [EN] directory must be copied next to it or ISCC aborts at [Languages].
    # [ZH] installer.iss 用相对自身位置的 InnoSetup\ChineseSimplified.isl 引用语言文件。
    # [ZH] 主线链在 tools\ 原地编译模板；本脚本把模板生成到 temp\，因此必须把配套目录
    # [ZH] 一并拷过去，否则 ISCC 会在 [Languages] 处中断。
    $IslSrc = Join-Path $Root "tools\InnoSetup"
    $IslDst = Join-Path $Root "temp\InnoSetup"
    if (-not (Test-Path $IslSrc)) {
        throw "找不到 tools\InnoSetup / Missing tools\InnoSetup"
    }
    New-Item -ItemType Directory -Force -Path $IslDst | Out-Null
    Copy-Item -Force (Join-Path $IslSrc "*.isl") $IslDst
    Invoke-Native $Iscc @(
        "/DMyAppVersion=$Version",
        # [EN] Four-part numeric version for the PE version resource: without this define
        # [EN] installer.iss falls back to its stale literal (0.2.4.0) and the shipped
        # [EN] installer reports an old version. Same value as backend/version.py's
        # [EN] app_version_4().
        # [ZH] PE 版本资源用的四段纯数字：不传这个定义，installer.iss 会回落到过时的字面量
        # [ZH]（0.2.4.0），安装包就会报着旧版本号。取值与 backend/version.py 的
        # [ZH] app_version_4() 一致。
        "/DMyAppNumericVersion=$SetupVersion",
        "/DMySourceDir=$ReaderRoot",
        $Generated
    ) "ISCC"
    $Setup = Join-Path $OutRoot "EchoDesk-Reader-v$SetupVersion-Setup.exe"
    if (-not (Test-Path $Setup)) {
        throw "安装包未生成 / Installer not produced: $Setup"
    }
    Write-Ok "安装包 / Installer: $Setup"
}

Write-Step "完成 / Done"
Write-Ok "便携目录 / Portable folder: $ReaderRoot"
exit 0
