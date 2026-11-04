# 适用平台：调用 Inno Setup 编译器（Windows 专属工具）
<#
.SYNOPSIS
    EchoDesk 安装程序构建（Inno Setup 6）—— 取代原来的 7-Zip 自解压包方案。

.DESCRIPTION
    把已经构建好的发布目录（默认 publish\EchoDesk-v<版本>）打成**一份**真正的
    Windows 安装程序：

      EchoDesk-v0.2.2-Setup.exe

    【只有一份，且不含模型】（v0.2.1 默认模型切换一轮改动）
      此前是两份（含模型版 / -NoModel 版）。现在一律只出一份、文件名不带变体后缀，
      并且**模型永不进包**：离线场景改由单独的"模型补充包"承担，安装程序保持
      "装完即用、模型按需下载"的单一形态（体积也从 608 MB 量级降到 114 MB 量级）。
      因此本脚本的可行性检查里，模型那一项是"**必须没有** .gguf"。

    流程：
      0. 可行性检查：ISCC / 安装脚本 / 发布物内容 / onedir 后端 /
         多文件前端 / 无模型 / 图标六尺寸 / 双语合规文档 / 无调试残留 / 无中文命名 /
         无预置快捷方式 / 中文字体包可用性。**任一不成立即中止**，并打出"怎么修"。
      1. 用 tools\installer.iss 编译（单一变体，无 /DMyIncludeModels）。
      2. 完整性断言 Assert-InstallerIntact：PE 头 + **追加数据仍在** + 版本资源读回
         + 图标可读 + 体积合理。
      3. 可选代码签名（-Sign）。
      4. 计算 SHA256SUMS.txt。
      5. 体积合理性检查。

    ========================================================================
    【为什么放弃 7-Zip 自解压包，改用 Inno Setup】（v0.2.1 实测结论，勿回退）
    ========================================================================
    * 7-Zip 官方 SFX 模块只支持 7 个配置键（Title / BeginPrompt / Progress /
      RunProgram / Directory / ExecuteFile / ExecuteParameters），**没有
      InstallPath** —— 在 ASCII 与 UTF-16 两种编码下都核对过内嵌字符串。
    * 它的默认行为是"解压到 %TEMP%\7zNNNNNN，执行 RunProgram 后删掉该目录"。
      虚拟机实测：用户双击后提示解压完成，却全盘搜不到 EchoDesk 文件。
    * 仓库里 `publish\7zSD.sfx` 与官方 9.20 的 `7zS.sfx` 是**同一个文件**
      （版本资源 InternalName=7zS.sfx、OriginalFilename=7zS.sfx.exe、
        Copyright (c) 1999-2010 Igor Pavlov、FileVersion 9.20），
      改名并不能获得 InstallPath 支持 —— 换文件名解决不了这个问题。
    结论：不再做"拼接 SFX 桩 + config.txt + payload.7z"，也不再依赖任何 .sfx 文件。

    ========================================================================
    【绝对不要对最终的 Setup.exe 跑 rcedit】（实测踩到，会直接毁掉安装包）
    ========================================================================
    Inno Setup 把安装数据**追加在 PE 映像之后**。探针实测（2 MB 的最小安装包）：
      PE 最后一个段结束于 893,952 字节，文件总长 2,097,164 字节，
      即追加数据 1,203,212 字节。
    rcedit 写资源会重建 PE 映像并**丢弃映像之后的全部数据**：
      文件从 2,097,164 字节被削到 893,952 字节，
      运行时报错、什么都不安装（退出码 1）。
    这与本项目此前踩过的「603.8 MB 自解压包被 rcedit 削成 176 KB 空壳」
    是**同一个机制**（那次是对拼接后的 SFX 跑 rcedit）。
    版本资源改由 tools\installer.iss 的 [Setup] VersionInfo* 指令交给编译器写入，
    本脚本读回校验，并用「追加数据必须仍在」这条断言兜住以后有人再调顺序。
    注意：**signtool 是安全的**（Authenticode 签名追加在文件末尾并登记到安全目录，
    不会重建 PE 映像），所以 -Sign 可以正常用。

.PARAMETER Version
    版本号，默认 0.2.1。决定发布目录名与输出文件名。

.PARAMETER ReleaseDir
    要打包的发布目录，默认 publish\EchoDesk-v<Version>。

.PARAMETER OutDir
    安装程序与 SHA256SUMS.txt 的输出目录，默认 publish。

.PARAMETER ISCC
    Inno Setup 命令行编译器路径。默认自动探测（注册表 → 常见安装位置 → PATH）。

.PARAMETER AssumeYes
    跳过"编译时间较长"之类的确认（自动化环境）。

.PARAMETER Sign / CertThumbprint / TimestampUrl
    编译完成后用 signtool 签名安装程序（需证书；找不到 signtool 只告警不失败）。

.EXAMPLE
    & tools\make_installer.ps1
    & tools\make_installer.ps1 -Version 0.2.1 -AssumeYes
    & tools\make_installer.ps1 -Sign -CertThumbprint <指纹>
#>
[CmdletBinding()]
param(
    # 口径：v0.2.1 = 跨平台之前；v0.2.2 = 跨平台版本（见 backend\version.py）。
    # 默认版本**从 backend\version.py 派生**（v0.2.2 裁定①）：原来这里写死 "0.2.2"，
    # 升版本时若忘了改，产出的目录名、Setup 文件名与**写进安装包的版本资源**都会是旧值。
    # 派生之后"改版本"只需改 version.py 一处（与 build_release.ps1 同一做法）。
    [string]$Version = "",
    [string]$ReleaseDir = "",
    [string]$OutDir = "",
    [string]$ISCC = "",
    [switch]$AssumeYes,
    [switch]$Sign,
    [string]$CertThumbprint = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"

<#
    顶层 trap：脚本内部抛终止性错误时 PowerShell **不会**设置进程退出码，
    调用方会看到 $LASTEXITCODE=0 而把失败当成功（本项目实测踩过）。
#>
trap {
    Write-Host ""
    Write-Host "!! 安装程序构建失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "   （位置：$($_.InvocationInfo.PositionMessage -replace "`r?`n", ' ')）" -ForegroundColor DarkGray
    exit 1
}

$Root = Split-Path -Parent $PSScriptRoot
if (-not $Version) {
    # 版本从唯一来源派生（v0.2.2 裁定①）—— 与 verify_release.ps1 / build_release.ps1 同一读法
    $VersionFile = Join-Path $Root "backend\version.py"
    $VersionMatch = Select-String -Path $VersionFile -Pattern 'APP_VERSION\s*=\s*["'']([^"'']+)["'']' |
                    Select-Object -First 1
    if (-not $VersionMatch) { throw "读不出 APP_VERSION：$VersionFile（可用 -Version 显式指定）" }
    $Version = $VersionMatch.Matches[0].Groups[1].Value.TrimStart('v')
    Write-Host "版本号（取自 backend\version.py）：$Version" -ForegroundColor DarkGray
}
if (-not $ReleaseDir) { $ReleaseDir = Join-Path $Root "publish\EchoDesk-v$Version" }
if (-not (Test-Path $ReleaseDir)) { throw "发布目录不存在：$ReleaseDir（先跑 tools\build_release.ps1）" }
$ReleaseDir = (Resolve-Path -LiteralPath $ReleaseDir).Path
if (-not $OutDir) { $OutDir = Join-Path $Root "publish" }
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path

$IssPath = Join-Path $PSScriptRoot "installer.iss"

$AppDir = Join-Path $ReleaseDir "app"
$BackendDir = Join-Path $AppDir "backend"
$ModelDir = Join-Path $BackendDir "models"

$SetupExe = Join-Path $OutDir "EchoDesk-v$Version-Setup.exe"
$SumsFile = Join-Path $OutDir "SHA256SUMS.txt"

$script:Problems = @()

function Write-Step([string]$text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Ok([string]$text) { Write-Host "    $text" -ForegroundColor Green }
function Write-Warn2([string]$text) { Write-Host "    $text" -ForegroundColor Yellow }

<#
    记一个问题。全部检查跑完再统一中止 —— 一次把该修的都说清楚，
    比"改一个跑一次"省时间（可行性检查的意义就在这里）。
#>
function Add-Problem([string]$what, [string]$how) {
    $script:Problems += [pscustomobject]@{ 检查项 = $what; 处理办法 = $how }
}

function Invoke-Native([string]$Exe, [string[]]$CmdArgs, [string]$What) {
    Write-Host "    $Exe $($CmdArgs -join ' ')" -ForegroundColor DarkGray
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $code = 0
    try {
        & $Exe @CmdArgs 2>&1 | ForEach-Object { Write-Host "    $_" }
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0) { throw "$What 失败（退出码 $code）" }
}

# ===========================================================================
# 定位 Inno Setup 编译器（ISCC.exe）
# ===========================================================================
Write-Step "定位 Inno Setup 编译器"

<#
    查找顺序（本机实测：Inno Setup 6 装成了**当前用户**安装，
    路径在 %LOCALAPPDATA%\Programs\Inno Setup 6，因此只查 Program Files 会漏）：
      1) -ISCC 显式指定
      2) 注册表卸载项 DisplayName 形如 "Inno Setup*" 的 InstallLocation
         （HKLM 64/32 位 + HKCU）
      3) 常见安装位置（Program Files、Program Files (x86)、
         %LOCALAPPDATA%\Programs 下的 Inno Setup*）
      4) PATH 里的 iscc.exe
#>
function Resolve-ISCC([string]$Explicit) {
    if ($Explicit) {
        if (-not (Test-Path $Explicit)) { throw "指定的 ISCC 不存在: $Explicit" }
        return (Resolve-Path -LiteralPath $Explicit).Path
    }
    $keys = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*"
    )
    foreach ($k in $keys) {
        $hits = @(Get-ItemProperty $k -ErrorAction SilentlyContinue |
            Where-Object { $_.DisplayName -like "Inno Setup*" -and $_.InstallLocation })
        foreach ($h in $hits) {
            $cand = Join-Path $h.InstallLocation.TrimEnd('\') "ISCC.exe"
            if (Test-Path $cand) { return (Resolve-Path -LiteralPath $cand).Path }
        }
    }
    $dirs = @()
    foreach ($base in @(${env:ProgramFiles}, ${env:ProgramFiles(x86)}, (Join-Path $env:LOCALAPPDATA "Programs"))) {
        if (-not $base) { continue }
        foreach ($name in @("Inno Setup 6", "Inno Setup 5")) { $dirs += (Join-Path $base $name) }
    }
    foreach ($d in $dirs) {
        $cand = Join-Path $d "ISCC.exe"
        if (Test-Path $cand) { return (Resolve-Path -LiteralPath $cand).Path }
    }
    # %LOCALAPPDATA%\Programs\Inno Setup*（版本号可能不同）
    $prog = Join-Path $env:LOCALAPPDATA "Programs"
    if (Test-Path $prog) {
        $found = @(Get-ChildItem $prog -Directory -Filter "Inno Setup*" -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName "ISCC.exe" } |
            Where-Object { Test-Path $_ })
        if ($found.Count -gt 0) { return (Resolve-Path -LiteralPath $found[0]).Path }
    }
    $cmd = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return ""
}

$IsccExe = Resolve-ISCC $ISCC
$Rcedit = Join-Path $Root "publish\rcedit-x64.exe"
$Icon = Join-Path $Root "assets\EchoDesk.ico"
$InnoDir = if ($IsccExe) { Split-Path -Parent $IsccExe } else { "" }

if (-not $IsccExe) {
    throw @"
未找到 Inno Setup 命令行编译器（ISCC.exe）。
  安装方式（任一）：
    - 官网下载安装：https://jrsoftware.org/isdl.php （Inno Setup 6）
    - 或用包管理器：winget install --id JRSoftware.InnoSetup
  本脚本会自动查找：注册表卸载项、Program Files / Program Files (x86) /
  %LOCALAPPDATA%\Programs 下的 "Inno Setup*"、以及 PATH。
  也可以用 -ISCC <路径> 显式指定。已中止打包。
"@
}
Write-Ok "ISCC    : $IsccExe"
$isccVi = (Get-Item $IsccExe).VersionInfo
Write-Ok "           $($isccVi.ProductName) / $($isccVi.FileDescription)"

# ===========================================================================
# 1. 可行性检查
# ===========================================================================
Write-Step "可行性检查"

# --- 1.1 安装脚本存在且编译器组件齐全 ---
if (-not (Test-Path $IssPath)) {
    Add-Problem "找不到安装脚本：$IssPath" "请确认 tools\installer.iss 存在（它是安装程序的唯一描述文件）。"
}
else {
    Write-Ok "安装脚本: tools\installer.iss（$((Get-Content $IssPath).Count) 行）"
}
foreach ($part in @("Setup.e32", "SetupLdr.e32", "Default.isl")) {
    $p = Join-Path $InnoDir $part
    if (-not (Test-Path $p)) {
        Add-Problem "Inno Setup 安装不完整：缺少 $part（$InnoDir）" `
            "请重新安装 Inno Setup 6（安装包至少要含编译器与默认语言文件）。"
    }
}

# --- 1.2 发布物内容已就绪 ---
$requiredAtRoot = @(
    "app", "docs", "lang", "examples", "results", "config.example.json", "USAGE.txt",
    "README.md", "README.zh-Hans.md",
    "CHANGELOG.md", "CHANGELOG.zh-Hans.md",
    "CONTRIBUTING.md", "CONTRIBUTING.zh-Hans.md",
    "LICENSE", "LICENSE.zh-Hans.md",
    "NOTICE", "NOTICE.zh-Hans.md", "SECURITY.md", "SECURITY.zh-Hans.md",
    "install-complete.bat"
)
# 发布物里**不得**出现预置快捷方式：.lnk 是绝对路径，在构建机上生成会把维护者的
# 个人路径（C:\Users\<用户名>\...）打进包里（任务书「全局要求五」绝对禁止项），
# 而且它在用户机器上必然是死链接。快捷方式由安装程序的 [Icons] 段创建。
$strayLinks = @(Get-ChildItem $ReleaseDir -Recurse -File -Filter *.lnk -ErrorAction SilentlyContinue)
if ($strayLinks.Count -gt 0) {
    Add-Problem "发布物内含预置快捷方式 $($strayLinks.Count) 个（会把构建机绝对路径带出去）" `
        "快捷方式应由安装程序的 [Icons] 段在目标机器上创建；请确认构建脚本没有回退到" +
        "「在发布目录里生成 .lnk」的旧做法（tools\build_release.ps1 第 9 节）。"
}
$missingRoot = @($requiredAtRoot | Where-Object { -not (Test-Path (Join-Path $ReleaseDir $_)) })
if ($missingRoot.Count -gt 0) {
    Add-Problem "发布物缺少必需内容: $($missingRoot -join ', ')" @"
先跑完整构建：& tools\build_release.ps1 -IncludeModels
（这些文件/目录由构建脚本生成：USAGE.txt、results\、config.example.json、
 install-complete.bat、双语合规文档副本等）
"@
}
else {
    Write-Ok "发布物外层内容齐全（$($requiredAtRoot.Count) 项）"
}

# --- 1.3 后端为 onedir 构建产物 ---
$beExe = Join-Path $BackendDir "EchoDeskBackend.exe"
$beInternal = Join-Path $BackendDir "_internal"
if (-not (Test-Path $beExe)) {
    Add-Problem "未找到后端可执行文件：app\backend\EchoDeskBackend.exe" "请先完成后端构建（PyInstaller onedir）。"
}
elseif (-not (Test-Path $beInternal)) {
    Add-Problem "后端不是 onedir 布局：缺少 app\backend\_internal\" "请用 backend\EchoBackend.spec 以 onedir 模式重新构建后端。"
}
else {
    $n = (Get-ChildItem $beInternal -Recurse -File -ErrorAction SilentlyContinue | Measure-Object).Count
    Write-Ok "后端    : onedir 就位（_internal\ 内 $n 个文件）"
}

# --- 1.4 前端为多文件发布产物 ---
$feExe = Join-Path $AppDir "EchoDesk.exe"
if (-not (Test-Path $feExe)) {
    Add-Problem "未找到前端主程序：app\EchoDesk.exe" "请先完成前端发布（dotnet publish 多文件模式）。"
}
else {
    $dlls = @(Get-ChildItem $AppDir -Filter *.dll -File -ErrorAction SilentlyContinue)
    if ($dlls.Count -eq 0) {
        Add-Problem "app\ 下没有依赖 DLL，前端不是多文件发布产物" "请以 -p:PublishSingleFile=false 重新发布前端。"
    }
    else {
        Write-Ok "前端    : 多文件就位（同目录 $($dlls.Count) 个 DLL）"
    }
}

# --- 1.5 模型文件：安装程序里**必须没有** ---
# 本版只有一份不含模型的安装程序（见文件头说明）。发布目录里若还有 .gguf，
# 说明它是用 -IncludeModels 构建的：那种目录适合做便携版/模型补充包，
# 但不该拿来编安装程序 —— 这里直接挡住，而不是"顺手把它打进包"。
$models = @(Get-ChildItem $ModelDir -Filter *.gguf -File -ErrorAction SilentlyContinue)
if ($models.Count -eq 0) {
    Write-Ok "模型    : 无（安装程序不含模型，符合预期）"
}
else {
    $msize = [math]::Round(($models | Measure-Object Length -Sum).Sum / 1MB)
    Add-Problem "发布目录里带有模型文件（app\backend\models 下 $($models.Count) 个，$msize MB）" @"
安装程序一律不含模型，请用不带 -IncludeModels 的构建产物：
  & tools\build_release.ps1        # 然后再跑 make_installer.ps1
（含模型的目录请另做便携版或"模型补充包"，不要拿来编安装程序）
"@
}

# --- 1.6 图标文件存在且含六尺寸 ---
if (-not (Test-Path $Icon)) {
    Add-Problem "未找到图标：assets\EchoDesk.ico" "请先生成图标：python tools\make_icon.py"
}
else {
    $ico = [System.IO.File]::ReadAllBytes($Icon)
    $isIco = ($ico.Length -ge 6 -and $ico[0] -eq 0 -and $ico[1] -eq 0 -and $ico[2] -eq 1 -and $ico[3] -eq 0)
    if (-not $isIco) {
        Add-Problem "assets\EchoDesk.ico 不是合法 ICO 文件" "请重新生成：python tools\make_icon.py"
    }
    else {
        $count = [BitConverter]::ToUInt16($ico, 4)
        $sizes = @()
        for ($i = 0; $i -lt $count; $i++) {
            $off = 6 + $i * 16
            $w = $ico[$off]; $h = $ico[$off + 1]
            if ($w -eq 0) { $w = 256 }
            if ($h -eq 0) { $h = 256 }
            $sizes += "$w`x$h"
        }
        Write-Ok "图标    : assets\EchoDesk.ico（$count 个尺寸: $($sizes -join ', ')）"
        if ($count -lt 6) {
            Add-Problem "图标只含 $count 个尺寸（任务书要求六尺寸）" "请重新生成：python tools\make_icon.py"
        }
    }
}

# --- 1.7 双语合规文档已生成（任务书「全局要求二」清单） ---
$bilingualPairs = @(
    @("LICENSE", "LICENSE.zh-Hans.md"),
    @("NOTICE", "NOTICE.zh-Hans.md"),
    @("SECURITY.md", "SECURITY.zh-Hans.md"),
    @("CODE_OF_CONDUCT.md", "CODE_OF_CONDUCT.zh-Hans.md"),
    @("docs/PRIVACY.md", "docs/PRIVACY.zh-Hans.md"),
    @("docs/EULA.md", "docs/EULA.zh-Hans.md"),
    @("docs/THIRD_PARTY_LICENSES.md", "docs/THIRD_PARTY_LICENSES.zh-Hans.md"),
    @("docs/ENCRYPTED_DOCUMENTS.md", "docs/ENCRYPTED_DOCUMENTS.zh-Hans.md")
)
$missingDocs = @()
foreach ($pair in $bilingualPairs) {
    foreach ($rel in $pair) {
        # 双语合规文档既要在仓库里有（根目录），也可能随发布物复制一份（外层 / docs\）
        $inRepo = Join-Path $Root ($rel -replace "/", "\")
        $inRel = Join-Path $ReleaseDir ($rel -replace "/", "\")
        if (-not (Test-Path $inRepo) -and -not (Test-Path $inRel)) {
            $missingDocs += $rel
        }
    }
}
# docs/ 与语言子目录单列，便于一次看清缺哪份
foreach ($rel in @("docs/SBOM.json", "docs/en/COMPLIANCE.md", "docs/zh-Hans/COMPLIANCE.md",
        "docs/en/README.md", "docs/en/USER_GUIDE.md", "docs/en/FAQ.md", "docs/en/AUTOMATION_GUIDE.md",
        "docs/zh-Hans/README.md", "docs/zh-Hans/USER_GUIDE.md", "docs/zh-Hans/FAQ.md",
        "docs/zh-Hans/AUTOMATION_GUIDE.md", "docs/DEVELOPER.md")) {
    $inRepo = Join-Path $Root ($rel -replace "/", "\")
    $inRel = Join-Path $ReleaseDir ($rel -replace "/", "\")
    if (-not (Test-Path $inRepo) -and -not (Test-Path $inRel)) { $missingDocs += $rel }
}
if ($missingDocs.Count -gt 0) {
    Add-Problem "缺少双语 / 必备合规文档 $($missingDocs.Count) 份：$($missingDocs -join ', ')" `
        "补齐后重跑构建脚本（docs\ 下的文档会整份复制进发布物）。"
}
else {
    Write-Ok "双语合规文档 $($bilingualPairs.Count) 对 + docs\ 必备 12 份齐全"
}

# --- 1.8 无调试内容残留（任务书「全局要求五」） ---
# 注意 session.json **不在**这个清单里：交付物带一份"空会话"是设计（后端首次启动会用它，
# 见 build_release.ps1 第 9b 步），它的合法性单独判（见下方"空会话"检查）。
# 这条必须与 build_release.ps1 的规则一致 —— 否则构建脚本刚放好的空会话会被打包脚本
# 判成"运行期残留"而中止打包（实测踩到：可行性检查报 app\backend\session.json）。
$forbiddenFiles = @(
    "ui_state.json", "snippets.json", "browser_state.json",
    "backend_crash.log", "frontend_crash.log", "automation_log.jsonl",
    "automation_processed.json", "summary_cache.json", "market_cache.json",
    "skills_local.json", "skills_registry.json", "models_registry.json",
    "skills_dev.log", "automation_templates.json", "config.user.json"
)
$forbiddenExt = @("*.pdb", "*.bak", "*.old", "*.suo", "*.user", "*.tmp")
$forbiddenDirs = @(".git", ".gitattributes", ".idea", ".vs", "__pycache__", "node_modules", ".venv", "verify-logs")
$residue = @()
# 预置快捷方式（见上方 $strayLinks）：也算"不该有的内容"
if ($strayLinks.Count -gt 0) {
    $residue += ($strayLinks | ForEach-Object { $_.FullName.Substring($ReleaseDir.Length).TrimStart('\') })
}
foreach ($name in $forbiddenFiles) {
    $hit = @(Get-ChildItem $ReleaseDir -Recurse -File -Filter $name -ErrorAction SilentlyContinue)
    if ($hit.Count -gt 0) { $residue += ($hit | ForEach-Object { $_.FullName.Substring($ReleaseDir.Length).TrimStart('\') }) }
}
foreach ($pat in $forbiddenExt) {
    # 用 -Filter 而不是 -Include：-Include 在多级递归下要先展开路径通配，
    # 且只有配合 -Recurse 或路径以 \* 结尾时才生效，容易静默漏检。
    $hit = @(Get-ChildItem $ReleaseDir -Recurse -File -Filter $pat -ErrorAction SilentlyContinue)
    if ($hit.Count -gt 0) { $residue += ($hit | ForEach-Object { $_.FullName.Substring($ReleaseDir.Length).TrimStart('\') }) }
}
foreach ($dir in $forbiddenDirs) {
    $hit = @(Get-ChildItem $ReleaseDir -Recurse -Force -Directory -Filter $dir -ErrorAction SilentlyContinue)
    if ($hit.Count -gt 0) { $residue += ($hit | ForEach-Object { $_.FullName.Substring($ReleaseDir.Length).TrimStart('\') + "\" }) }
}
# 研发/修复/发布记录属过程文档，不得随发布物分发（任务书「全局要求五」明列）
$devRecords = @(Get-ChildItem $ReleaseDir -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^(v?0\.\d+\.\d+)-.*记录\.md$' })
if ($devRecords.Count -gt 0) {
    $residue += ($devRecords | ForEach-Object { $_.FullName.Substring($ReleaseDir.Length).TrimStart('\') })
}
if ($residue.Count -gt 0) {
    Add-Problem "发布物内有调试内容 / 运行期数据残留 $($residue.Count) 项（前 12 项：$((($residue | Select-Object -First 12) -join ', '))）" `
        "重跑构建脚本会清理运行期残留；过程文档（研发/修复/发布记录）由构建脚本排除，请确认构建脚本是最新版。"
}
else {
    Write-Ok "无调试内容残留（运行期数据 / 构建中间产物 / 过程文档）"
}

# --- 1.8b 空会话检查（session.json 是唯一允许留下的运行期文件） ---
<#
    交付物里带一份 session.json 是**设计**：后端首次启动会用它。但它必须是**空会话** ——
    任何一次端到端验证都会让后端把测试数据写进去（例如 e2e 里 /file 加载的临时文件路径与
    正文），那属于"不该随发布物分发"的内容。
    判定规则与 build_release.ps1 第 9b 步逐字一致（两处必须同步改）：
      * "history" 必须是空数组；
      * "file_content" 必须是空字符串。
#>
$sessionFiles = @(Get-ChildItem $ReleaseDir -Recurse -File -Filter "session.json" -ErrorAction SilentlyContinue)
$dirtySessions = @()
foreach ($sf in $sessionFiles) {
    $txt = Get-Content -LiteralPath $sf.FullName -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
    if (-not $txt) { continue }
    $historyEmpty = [regex]::IsMatch($txt, '"history"\s*:\s*\[\s*\]')
    $fileEmpty = [regex]::IsMatch($txt, '"file_content"\s*:\s*"\s*"')
    if (-not ($historyEmpty -and $fileEmpty)) {
        $dirtySessions += $sf.FullName.Substring($ReleaseDir.Length).TrimStart('\')
    }
}
if ($dirtySessions.Count -gt 0) {
    Add-Problem "发布物里的 session.json 不是空会话（含会话历史或文件正文）：$($dirtySessions -join ', ')" `
        "重跑 tools\build_release.ps1 会把它重置为空会话（该行为默认开启，-KeepSession 才会跳过）。"
}
elseif ($sessionFiles.Count -gt 0) {
    Write-Ok "会话为空会话（session.json 里无历史、无文件正文）"
}

# --- 1.9 无中文命名（任务书「全局要求一」） ---
$cjk = [regex]'[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]'
$cnNames = @(Get-ChildItem $ReleaseDir -Recurse -Force -ErrorAction SilentlyContinue |
    Where-Object { $cjk.IsMatch($_.Name) } |
    ForEach-Object { $_.FullName.Substring($ReleaseDir.Length).TrimStart('\') })
if ($cnNames.Count -gt 0) {
    Add-Problem "发布物内有中文文件名 / 目录名 $($cnNames.Count) 项（前 12 项：$((($cnNames | Select-Object -First 12) -join ', '))）" `
        "按任务书「全局要求一」全部改为英文；程序生成的名字（results\、result.txt、auto-task、failure-screenshot-*.png）需同步改源代码。"
}
else {
    Write-Ok "发布物内文件名/目录名全为 ASCII（无中文）"
}

# --- 1.10 安装界面语言（信息项，不阻断） ---
$zhIsl = Join-Path $InnoDir "Languages\ChineseSimplified.isl"
if (Test-Path $zhIsl) {
    Write-Ok "安装界面: 英文 + 简体中文（找到 $zhIsl）"
}
else {
    Write-Warn2 "安装界面只有英文：Inno Setup 自带的 Languages\ 目录里没有 ChineseSimplified.isl"
    Write-Warn2 "  （官方把它列为需单独下载的第三方翻译）。把该 .isl 放进上面的目录即可自动启用中文界面，"
    Write-Warn2 "  安装脚本用 ISPP 的 FileExists 做条件包含，不需要改任何代码。"
}

# --- 统一判定 ---
if ($script:Problems.Count -gt 0) {
    Write-Host ""
    Write-Host "可行性检查未通过，共 $($script:Problems.Count) 项 —— 已中止打包。" -ForegroundColor Red
    Write-Host ""
    for ($i = 0; $i -lt $script:Problems.Count; $i++) {
        $p = $script:Problems[$i]
        Write-Host ("[{0}] {1}" -f ($i + 1), $p.检查项) -ForegroundColor Red
        foreach ($line in ($p.处理办法 -split "`n")) { Write-Host "     $line" -ForegroundColor Yellow }
        Write-Host ""
    }
    throw "可行性检查未通过（$($script:Problems.Count) 项），未生成任何安装程序。"
}
Write-Ok "可行性检查全部通过（10 项）"

# ===========================================================================
# 2. PE 解析：判断安装包是否仍带着"追加在 PE 映像之后"的安装数据
# ===========================================================================
<#
    返回 LastSectionEnd 与 Appended（= 文件长度 - 最后一个段的结束偏移）。
    Inno Setup 把安装数据追加在 PE 映像之后，所以正常产物必然 Appended > 0；
    若某次改动让 Appended 变成 0，就说明有人重建了 PE 映像（rcedit 之类），
    安装包已经是个空壳。
#>
function Get-PeImageInfo([string]$Path) {
    $len = (Get-Item -LiteralPath $Path).Length
    $fs = [System.IO.File]::OpenRead($Path)
    try {
        $br = New-Object System.IO.BinaryReader($fs)
        $fs.Seek(0x3C, [System.IO.SeekOrigin]::Begin) | Out-Null
        $e = $br.ReadInt32()
        $fs.Seek($e, [System.IO.SeekOrigin]::Begin) | Out-Null
        $sig = $br.ReadUInt32()
        $fs.Seek($e + 6, [System.IO.SeekOrigin]::Begin) | Out-Null
        $numSec = $br.ReadUInt16()
        $fs.Seek($e + 20, [System.IO.SeekOrigin]::Begin) | Out-Null
        $optSize = $br.ReadUInt16()
        $fs.Seek($e + 24 + $optSize, [System.IO.SeekOrigin]::Begin) | Out-Null
        $maxEnd = [int64]0
        for ($i = 0; $i -lt $numSec; $i++) {
            $null = $br.ReadBytes(8)          # Name
            $null = $br.ReadUInt32()          # VirtualSize
            $null = $br.ReadUInt32()          # VirtualAddress
            $rawSize = $br.ReadUInt32()       # SizeOfRawData
            $rawPtr = $br.ReadUInt32()        # PointerToRawData
            $null = $br.ReadBytes(16)         # 其余 16 字节
            $end = [int64]$rawPtr + [int64]$rawSize
            if ($end -gt $maxEnd) { $maxEnd = $end }
        }
        return [pscustomobject]@{
            FileLength     = $len
            Sections       = $numSec
            LastSectionEnd = $maxEnd
            Appended       = $len - $maxEnd
        }
    }
    finally { $fs.Dispose() }
}

<#
    版本号文本归一化：只用于**比较**，不改变实际写入的值。
    必须做这一步的原因（实测）：Inno Setup 在编译时会把版权串里的 `(C)` 写成 `©` ——
    写 `Copyright (C) Void Entropia Studio`，从 PE 里读回来是
    `Copyright © Void Entropia Studio`。若按原样比对，断言会因这个已知的等价改写而误报失败。
#>
function ConvertTo-Comparable([string]$Text) {
    return (("$Text").Trim() -replace '\(C\)', '©')
}

<#
    安装程序完整性断言（取代原来的 Assert-SfxIntact）。
    四重校验：
      1) 文件头是 MZ（是个 PE 可执行文件）；
      2) **追加数据仍在**（Appended > 0 且不小于 1 MB）——
         这是防"rcedit 削壳"的关键一条，也是 Inno 安装数据的存在性证明；
      3) 版本资源七个字段读回正确（含变体差异，如 FileDescription 的 "(No Model)"）；
      4) 图标能从 PE 里取出来。
#>
function Assert-InstallerIntact([string]$Exe, [string]$ExpectedDescription, [string]$ExpectedOriginalName) {
    if (-not (Test-Path -LiteralPath $Exe)) { throw "安装程序未生成：$Exe" }

    $fs = [System.IO.File]::OpenRead($Exe)
    try {
        $mz = New-Object byte[] 2
        $fs.Read($mz, 0, 2) | Out-Null
        if ($mz[0] -ne 0x4D -or $mz[1] -ne 0x5A) { throw "文件头不是 MZ，不是可执行文件：$Exe" }
    }
    finally { $fs.Dispose() }

    $pe = Get-PeImageInfo $Exe
    if ($pe.Appended -le 0) {
        throw ("安装包不含追加数据（PE 最后一段结束于 $($pe.LastSectionEnd)，文件长 $($pe.FileLength)）。" +
            "Inno Setup 的安装数据是追加在 PE 映像之后的，Appended=0 说明 PE 被重建过 —— " +
            "最常见的原因是**对最终 Setup.exe 跑了 rcedit**（rcedit 会丢弃追加数据，" +
            "本项目已实测：2 MB 的安装包被削到 893,952 字节且运行即失败）。" +
            "版本资源请用 tools\installer.iss 的 [Setup] VersionInfo* 指令写入，不要用 rcedit。")
    }
    if ($pe.Appended -lt 1MB) {
        throw "安装包的追加数据只有 $([math]::Round($pe.Appended / 1KB, 1)) KB，明显不完整：$Exe"
    }
    Write-Ok "  追加数据 $([math]::Round($pe.Appended / 1MB, 1)) MB（PE 段 $($pe.Sections) 个，末段结束于 $($pe.LastSectionEnd)）"

    $vi = (Get-Item -LiteralPath $Exe).VersionInfo
    <#
        两处 Inno 的**等价改写**（实测，不是缺陷，断言必须顺着它写）：
          * 版权串里的 `(C)` 会被写成 `©`：写 `Copyright (C) Void Entropia Studio`，
            读回来是 `Copyright © Void Entropia Studio`（ConvertTo-Comparable 负责归一化）。
          * `FileVersion` 的**字符串**会把末尾的 `.0` 省掉：写 `VersionInfoVersion=0.2.1.0`，
            `VersionInfo.FileVersion` 读回 `0.2.1`，而 `FileVersionRaw` 仍是 `0.2.1.0`
            （`ProductVersion` 字符串则保留 `0.2.1.0`）。
        所以版本号一律比对 **Raw**（数值口径，稳定），文本字段比对前做归一化。
    #>
    $expect = [ordered]@{
        CompanyName       = "Void Entropia Studio"
        LegalCopyright    = "Copyright (C) Void Entropia Studio"
        ProductName       = "EchoDesk"
        FileVersionRaw    = "$Version.0"
        ProductVersionRaw = "$Version.0"
        FileDescription   = $ExpectedDescription
        OriginalFilename  = $ExpectedOriginalName
    }
    foreach ($k in $expect.Keys) {
        if ((ConvertTo-Comparable $vi.$k) -ne (ConvertTo-Comparable $expect[$k])) {
            throw "版本资源 $k 不正确：得到 '$($vi.$k)'，期望 '$($expect[$k])'（$Exe）"
        }
    }
    Write-Ok "  版本资源 7 个字段读回正确（CompanyName / LegalCopyright / ProductName / " +
        "FileVersionRaw / ProductVersionRaw / FileDescription / OriginalFilename）"

    try {
        Add-Type -AssemblyName System.Drawing -ErrorAction SilentlyContinue
        $ico = [System.Drawing.Icon]::ExtractAssociatedIcon($Exe)
        if ($null -eq $ico) { throw "取不到关联图标" }
        $ico.Dispose()
        Write-Ok "  图标可从 PE 取出"
    }
    catch {
        throw "图标校验失败：$($_.Exception.Message)"
    }
}

# ===========================================================================
# 3. 编译安装程序（单一份，不含模型）
# ===========================================================================
<#
    ISCC 参数：
      /Qp                          安静编译但显示进度（大包压缩要看进度）
      /O<目录>                     覆盖 [Setup] OutputDir
      /DMyAppVersion=<版本>        版本号
      /DMySourceDir=<绝对路径>      发布目录（绝对路径，避开中文目录相对化问题）
    变体由 installer.iss 固定为"不含模型"，不再有 /DMyIncludeModels。
#>
function New-Installer([string]$Exe, [string]$Description, [string]$OriginalName) {
    # 先删掉同名旧产物：万一编译失败，旧文件不能冒充"本次构建的交付物"。
    # 实测踩到：被废弃的 7-Zip 自解压包留在 publish\ 里，让上层"交付物存在"的
    # 断言误判成成功（603.81 MB / 108.7 MB 那两份其实是旧包）。
    if (Test-Path -LiteralPath $Exe) {
        Write-Ok "  删除同名旧产物：$(Split-Path -Leaf $Exe)"
        Remove-Item -LiteralPath $Exe -Force
    }
    $isccArgs = @(
        "/Qp",
        "/O$OutDir",
        "/DMyAppVersion=$Version",
        "/DMySourceDir=$ReleaseDir",
        $IssPath
    )
    Invoke-Native $IsccExe $isccArgs "编译 $(Split-Path -Leaf $Exe)"
    Assert-InstallerIntact $Exe $Description $OriginalName
    $mb = [math]::Round((Get-Item -LiteralPath $Exe).Length / 1MB, 2)
    Write-Ok "$(Split-Path -Leaf $Exe)：$mb MB"
}

if (-not $AssumeYes) {
    Write-Step "编译安装程序（不含模型，lzma2/max 固实压缩）"
}
else {
    Write-Step "编译安装程序（不含模型）"
}
New-Installer $SetupExe "EchoDesk v$Version Portable Setup" (Split-Path -Leaf $SetupExe)

# ===========================================================================
# 4. 可选代码签名
# ===========================================================================
<#
    签名是安全的：Authenticode 签名**追加在文件末尾**并登记到 PE 的安全目录，
    不会重建 PE 映像，因此不会丢掉 Inno 的追加安装数据（与 rcedit 相反）。
#>
if ($Sign) {
    Write-Step "签名安装程序（-Sign）"
    $signtoolCmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    $signtool = if ($signtoolCmd) { $signtoolCmd.Source } else { $null }
    if (-not $signtool) {
        Write-Warn2 "未找到 signtool.exe（需安装 Windows SDK），跳过签名"
    }
    else {
        foreach ($exe in @($SetupExe)) {
            if (-not (Test-Path -LiteralPath $exe)) { continue }
            $signArgs = @("sign", "/fd", "sha256", "/tr", $TimestampUrl, "/td", "sha256")
            if ($CertThumbprint) { $signArgs += @("/sha1", $CertThumbprint) } else { $signArgs += @("/a") }
            Invoke-Native $signtool ($signArgs + @($exe)) "签名 $(Split-Path -Leaf $exe)"
            Write-Ok "$(Split-Path -Leaf $exe) 已签名"
        }
        # 签名后复核追加数据仍在（防止签名工具被换成会重建 PE 的版本）
        foreach ($exe in @($SetupExe)) {
            if (-not (Test-Path -LiteralPath $exe)) { continue }
            $pe = Get-PeImageInfo $exe
            if ($pe.Appended -le 0) { throw "签名后 $(Split-Path -Leaf $exe) 的追加数据消失，安装包已损坏。" }
        }
        Write-Ok "签名后追加数据复核通过"
    }
}

# ===========================================================================
# 5. SHA256SUMS.txt
# ===========================================================================
Write-Step "计算 SHA256 校验清单"
$assets = @()
foreach ($p in @(
        $SetupExe,
        (Join-Path $Root "docs\SBOM.json"),
        (Join-Path $Root "docs\THIRD_PARTY_LICENSES.md"),
        (Join-Path $Root "docs\THIRD_PARTY_LICENSES.zh-Hans.md"),
        (Join-Path $Root "LICENSE"), (Join-Path $Root "LICENSE.zh-Hans.md"),
        (Join-Path $Root "NOTICE"), (Join-Path $Root "NOTICE.zh-Hans.md")
    )) {
    # **必须哈希"随包发出的那一份"**：文档在发布构建时会做规范化（去过程性内容、统一换行），
    # 发布目录里的副本与仓库源文件并不总是逐字节相同。原先这里直接哈希仓库根的文件，
    # 于是 SHA256SUMS.txt 与用户手里拿到的文件对不上（verify_release 实测报
    # "哈希不一致: NOTICE.zh-Hans.md"）。优先取发布目录里的同名文件，取不到才回退仓库源文件。
    $shipped = Join-Path $OutDir (Split-Path -Leaf $p)
    if (Test-Path -LiteralPath $shipped) {
        $assets += (Resolve-Path -LiteralPath $shipped).Path
    } elseif (Test-Path -LiteralPath $p) {
        $assets += (Resolve-Path -LiteralPath $p).Path
    }
}
$lines = @()
foreach ($a in $assets) {
    $h = (Get-FileHash -LiteralPath $a -Algorithm SHA256).Hash.ToLower()
    # 清单里用文件名：附件是平铺上传的，用户核对时不必关心本机路径
    $lines += "$h  $(Split-Path -Leaf $a)"
}
$enc = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($SumsFile, ($lines -join "`r`n"), $enc)
Write-Ok "SHA256SUMS.txt（$($lines.Count) 个资产）"
foreach ($l in $lines) { Write-Host "    $l" -ForegroundColor DarkGray }

# ===========================================================================
# 6. 合理性检查
# ===========================================================================
<#
    任务书给的区间（不含模型约 350-400 MB）是**解压/安装后**的发布目录体积口径，
    不是安装程序（压缩后）的体积。
    第一版曾拿"安装包文件大小"去比这个区间，于是每次都误报；
    更糟的是，它曾把一个**被 rcedit 削成 176 KB 的空壳**报成"0.2 MB 超出区间"，
    措辞让人以为是压缩率问题而不是文件损坏。现在按"安装后体积"比对，
    并把压缩包体积单列；安装包本身的损坏由 Assert-InstallerIntact 负责拦。
    （含模型版那条 850-900 MB 的区间随"不再出含模型安装程序"一并去掉；
      发布目录里本来也不该有模型 —— 1.5 已经挡过。）
#>
Write-Step "打包合理性检查"
$releaseBytes = (Get-ChildItem $ReleaseDir -Recurse -File | Measure-Object Length -Sum).Sum
$warn = 0
foreach ($t in @(
        @{ Bytes = $releaseBytes; Min = 350; Max = 400; Label = "安装后体积" }
    )) {
    $mb = [math]::Round($t.Bytes / 1MB, 1)
    if ($mb -lt $t.Min -or $mb -gt $t.Max) {
        Write-Warn2 "$($t.Label) $mb MB 超出任务书给的合理区间 $($t.Min)-$($t.Max) MB —— 请人工确认体积变化原因"
        $warn++
    }
    else {
        Write-Ok "$($t.Label) $mb MB（任务书区间 $($t.Min)-$($t.Max) MB）"
    }
}
foreach ($pair in @(
        @{ Exe = $SetupExe; Src = $releaseBytes; Label = "安装程序" }
    )) {
    if (-not (Test-Path -LiteralPath $pair.Exe)) { continue }
    $exeMb = [math]::Round((Get-Item -LiteralPath $pair.Exe).Length / 1MB, 2)
    $ratio = if ($pair.Src -gt 0) { (Get-Item -LiteralPath $pair.Exe).Length / $pair.Src } else { 0 }
    Write-Ok "  $($pair.Label)：$exeMb MB（压缩率 $([math]::Round($ratio * 100, 1))% of 安装后体积）"
    # 截断守卫：安装包不可能比安装后体积的一成还小；过小说明内容没进去
    if ($ratio -lt 0.10) {
        throw "$($pair.Label) 只有安装后体积的 $([math]::Round($ratio * 100, 1))%，内容疑似未打进包（$($pair.Exe)）。"
    }
}
if ($warn -eq 0) { Write-Ok "安装程序（安装后）体积在合理区间内" }

Write-Host ""
Write-Host "打包完成。" -ForegroundColor Cyan
Write-Ok "产物目录: $OutDir"
foreach ($p in @($SetupExe, $SumsFile)) {
    if (Test-Path -LiteralPath $p) { Write-Ok "  $(Split-Path -Leaf $p)  $([math]::Round((Get-Item -LiteralPath $p).Length / 1MB, 2)) MB" }
}
Write-Ok "安装程序不含模型（模型由程序按需下载，或用单独的模型补充包）"
Write-Ok "默认安装位置 = 用户双击的那个安装程序所在目录；安装后可改路径，卸载走 unins000.exe"
Write-Ok "卸载会清掉运行期文件（会话/模型/缓存/日志等），但 results\ 原样保留"
