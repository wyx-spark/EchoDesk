<#
.SYNOPSIS
    EchoDesk 自解压绿化安装包构建（发布任务书「任务一 / 任务三」+「可行性检查」）。

.DESCRIPTION
    把已经构建好的发布目录（默认 publish\EchoDesk-v<版本>）打成两份 7-Zip 自解压包：

      EchoDesk-v0.2.1-Setup.exe            含模型（app\backend\models\*.gguf）
      EchoDesk-v0.2.1-NoModel-Setup.exe    不含模型（排除 app\backend\models\）

    流程（与任务书逐条对应）：
      0. 可行性检查：7zS.sfx / rcedit / 发布物内容 / onedir 后端 / 多文件前端 /
         模型 / 图标六尺寸 / 双语合规文档 / 无调试残留 / 无中文命名。
         **任一不成立即中止**，并把「怎么修」一起打出来。
      1. 7z 打包 payload.7z（-t7z -m0=lzma2 -mx=9 -ms=on 固实极限压缩）。
      2. 写 config.txt（UTF-8 无 BOM），指定解压到当前文件夹、解压后运行
         install-complete.bat。
      3. copy /b 拼接 7zS.sfx + config.txt + payload.7z（在 cmd 中执行，路径带引号）。
      4. rcedit 写入版本资源与图标（九个字段）。
      5. 计算 SHA256SUMS.txt。

    为什么这里自己再做一次可行性检查（build_release.ps1 已经断言过结构）：
    本脚本可以**独立运行**（例如只改了 rcedit 字段、只想重打自解压包时），
    此时上游断言并没有跑过；把门槛放在真正用这些前提的地方才拦得住。

    【实测得到的两个约束，写在最前面避免以后重踩】
    * publish\7zS.sfx 是 7-Zip 官方 SFX 模块的**精简版**，支持的配置项只有
      Title / BeginPrompt / Progress / RunProgram / Directory / ExecuteFile /
      ExecuteParameters（用二进制内嵌字符串核对过）。它**没有** OverwriteMode、
      ExtractTitle、ExtractDialogText 这些 7zSD/第三方改版才有的键 —— 写了会被忽略。
      因此"同名文件询问用户"由 BeginPrompt 提前告知 + install-complete.bat 里
      创建快捷方式前的确认来实现，而不是靠 SFX 解压时的覆盖询问。
    * rcedit **只用于自解压包本身**。绝不能对本发布物内的 app\backend\EchoDeskBackend.exe
      使用：PyInstaller 的 PKG 归档追加在 PE 映像之后，rcedit 重建资源会丢掉它，
      后端启动即报「Could not load PyInstaller's embedded PKG archive」。

.PARAMETER Version
    版本号，默认 0.2.1。决定发布目录名与输出文件名。

.PARAMETER ReleaseDir
    要打包的发布目录，默认 publish\EchoDesk-v<Version>。

.PARAMETER OutDir
    自解压包与 SHA256SUMS.txt 的输出目录，默认 publish。

.PARAMETER SkipWithModel / SkipNoModel
    只打其中一份（调试用；正式发布两份都要）。

.PARAMETER KeepPayload
    保留中间的 payload.7z 与 config.txt（默认留在 artifacts\sfx\ 下不删，
    便于核对内容与体积；加此开关不做任何清理，仅用于明确表达意图）。

.PARAMETER ReusePayload
    复用 artifacts\sfx\ 下已有的 payload.7z，跳过最耗时的极限压缩步骤
    （885MB 固实 LZMA2 约二十多分钟）。用于只改 rcedit 字段、config.txt、
    拼接方式等**不影响发布目录内容**的调试。
    安全护栏：若发布目录里存在比 payload 更新的文件，直接报错拒绝复用，
    避免"拿旧 payload 发出新内容"。

.PARAMETER AssumeYes
    跳过"含模型版很大"之类的确认（自动化环境）。

.EXAMPLE
    & tools\make_sfx.ps1
    & tools\make_sfx.ps1 -Version 0.2.1 -SkipNoModel
#>
[CmdletBinding()]
param(
    [string]$Version = "0.2.1",
    [string]$ReleaseDir = "",
    [string]$OutDir = "",
    [string]$SevenZip = "",
    [switch]$SkipWithModel,
    [switch]$SkipNoModel,
    [switch]$KeepPayload,
    [switch]$AssumeYes,
    [switch]$ReusePayload
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
if (-not $ReleaseDir) { $ReleaseDir = Join-Path $Root "publish\EchoDesk-v$Version" }
$ReleaseDir = (Resolve-Path -LiteralPath $ReleaseDir).Path
if (-not $OutDir) { $OutDir = Join-Path $Root "publish" }
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path

$Work = Join-Path $Root "artifacts\sfx"
$AppDir = Join-Path $ReleaseDir "app"
$BackendDir = Join-Path $AppDir "backend"
$ModelDir = Join-Path $BackendDir "models"

$WithModelExe = Join-Path $OutDir "EchoDesk-v$Version-Setup.exe"
$NoModelExe = Join-Path $OutDir "EchoDesk-v$Version-NoModel-Setup.exe"
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
# 0. 定位外部工具（7z 与 rcedit；找不到就报错并提示放置位置）
# ===========================================================================
Write-Step "定位打包工具"

function Resolve-SevenZip([string]$Explicit) {
    if ($Explicit) {
        if (-not (Test-Path $Explicit)) { throw "指定的 7z 不存在: $Explicit" }
        return (Resolve-Path -LiteralPath $Explicit).Path
    }
    $candidates = @(
        (Join-Path $Root "tools\7z.exe"),
        (Join-Path $Root "tools\7za.exe"),
        (Join-Path ${env:ProgramFiles} "7-Zip\7z.exe")
    )
    if (${env:ProgramFiles(x86)}) { $candidates += (Join-Path ${env:ProgramFiles(x86)} "7-Zip\7z.exe") }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return (Resolve-Path -LiteralPath $c).Path }
    }
    $cmd = Get-Command "7z.exe" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return ""
}

$SevenZipExe = Resolve-SevenZip $SevenZip
$SfxModule = Join-Path $Root "publish\7zS.sfx"
$Rcedit = Join-Path $Root "publish\rcedit-x64.exe"
if (-not (Test-Path $Rcedit)) { $Rcedit = Join-Path $Root "tools\rcedit-x64.exe" }
$Icon = Join-Path $Root "assets\EchoDesk.ico"

if (-not $SevenZipExe) {
    throw @"
未找到 7-Zip 命令行（7z.exe）。
  放置位置（任一）：
    - 安装 7-Zip 后使用默认路径 C:\Program Files\7-Zip\7z.exe
    - 或把 7z.exe 放到 tools\ 下（7z.exe + 7z.dll 需同目录）
    - 或用 -SevenZip <路径> 显式指定
  已中止打包。检查过的候选位置：
$((@((Join-Path $Root 'tools\7z.exe'), (Join-Path $Root 'tools\7za.exe'), (Join-Path ${env:ProgramFiles} '7-Zip\7z.exe')) | ForEach-Object { "    $_" }) -join "`n")
"@
}
Write-Ok "7z      : $SevenZipExe"

# ===========================================================================
# 1. 可行性检查（任务书「可行性检查」十项）
# ===========================================================================
Write-Step "可行性检查"

# --- 1.1 7zS.sfx 存在且可用 ---
if (-not (Test-Path $SfxModule)) {
    Add-Problem "7zS.sfx 缺失：$SfxModule" @"
请把 7-Zip 的 SFX 模块放到 publish\ 下并命名为 7zS.sfx。
  获取方式：7-Zip 安装目录本身带 7z.sfx，但**自解压安装包**用的是带
  RunProgram 支持的那个模块（通常来自 7-Zip Extra 包的 7zS.sfx，
  或 7zSD 等改版）。本脚本只认 publish\7zS.sfx。
"@
}
else {
    $sfxLen = (Get-Item $SfxModule).Length
    $sfxBytes = [System.IO.File]::ReadAllBytes($SfxModule)
    $sfxText = [System.Text.Encoding]::ASCII.GetString($sfxBytes)
    if ($sfxText -notmatch '!@Install@!UTF-8!') {
        Add-Problem "7zS.sfx 不是可用的 SFX 模块（未找到配置标记 !@Install@!UTF-8!）：$SfxModule" `
            "请确认放的是 7-Zip SFX 模块本体，而不是改名的普通程序。"
    }
    else {
        # 把该模块真正支持的配置项列出来（实测核对，避免写 7zSD 才有的键）
        $supported = @()
        $unicode = [System.Text.Encoding]::Unicode.GetString($sfxBytes)
        foreach ($key in @("Title", "BeginPrompt", "Progress", "RunProgram", "Directory",
                "ExecuteFile", "ExecuteParameters", "OverwriteMode", "ExtractTitle",
                "ExtractDialogText", "ExtractCancelText", "InstallPath")) {
            if ($unicode -match [regex]::Escape($key)) { $supported += $key }
        }
        Write-Ok "7zS.sfx : $SfxModule（$([math]::Round($sfxLen / 1KB)) KB）"
        Write-Ok "  该模块支持的配置项: $($supported -join ', ')"
        if ($supported -notcontains "OverwriteMode") {
            Write-Warn2 "  该模块无 OverwriteMode：解压时不会询问覆盖；"
            Write-Warn2 "  「同名询问」由 BeginPrompt 提前告知 + install-complete.bat 内确认实现。"
        }
    }
}

# --- 1.2 rcedit 存在且可用 ---
if (-not (Test-Path $Rcedit)) {
    Add-Problem "rcedit 缺失：publish\rcedit-x64.exe（或 tools\rcedit-x64.exe）" @"
请把 rcedit-x64.exe 放到 publish\ 下（任务书说明该文件已在 publish\ 中提供），
或执行 tools\fetch_tools.bat 下载到 tools\。
"@
}
else {
    # rcedit 没有 --version 参数，传进去它会回一句 "Unable to load file: --version"。
    # 这里要判的是"能不能执行"，所以只要有输出就算可用（能打印就说明 PE 能加载），
    # 并把这个已知的无害提示说明清楚，避免下次有人误以为是失败。
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $verOut = (& $Rcedit --version 2>&1 | Out-String).Trim()
    $rc = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if (-not $verOut) {
        Add-Problem "rcedit 无法执行：$Rcedit（退出码 $rc）" "请重新下载 rcedit-x64.exe 并确认未被杀软拦截。"
    }
    else {
        Write-Ok "rcedit  : $Rcedit（可执行；rcedit 无 --version，上方 'Unable to load file' 属预期）"
    }
}

# --- 1.3 发布物内容已就绪 ---
$requiredAtRoot = @(
    "app", "docs", "lang", "examples", "results", "config.example.json", "USAGE.txt",
    "README.md", "README.zh-Hans.md", "CHANGELOG.md", "LICENSE", "LICENSE.zh-Hans.md",
    "NOTICE", "NOTICE.zh-Hans.md", "SECURITY.md", "SECURITY.zh-Hans.md",
    "install-complete.bat"
)
# 发布物里**不得**出现预置快捷方式：.lnk 是绝对路径，在构建机上生成会把维护者的
# 个人路径（C:\Users\<用户名>\...）打进包里（任务书「全局要求五」绝对禁止项），
# 而且它在用户机器上必然是死链接。快捷方式由 install-complete.bat 创建。
$strayLinks = @(Get-ChildItem $ReleaseDir -Recurse -File -Filter *.lnk -ErrorAction SilentlyContinue)
if ($strayLinks.Count -gt 0) {
    Add-Problem "发布物内含预置快捷方式 $($strayLinks.Count) 个（会把构建机绝对路径带出去）" `
        "快捷方式应由 install-complete.bat 在目标机器上创建；请确认构建脚本没有回退到" +
        "「在发布目录里生成 .lnk」的旧做法（tools\build_release.ps1 第 9 节）。"
}
$missingRoot = @($requiredAtRoot | Where-Object { -not (Test-Path (Join-Path $ReleaseDir $_)) })
if ($missingRoot.Count -gt 0) {
    Add-Problem "发布物缺少必需内容: $($missingRoot -join ', ')" @"
先跑完整构建：& tools\build_release.ps1 -IncludeModels
（这些文件/目录由构建脚本生成：USAGE.txt、results\、config.example.json、
 Start-EchoDesk-*.lnk、install-complete.bat、双语合规文档副本等）
"@
}
else {
    Write-Ok "发布物外层内容齐全（$($requiredAtRoot.Count) 项）"
}

# --- 1.4 后端为 onedir 构建产物 ---
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

# --- 1.5 前端为多文件发布产物 ---
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

# --- 1.6 模型文件（含模型版才检查） ---
$models = @(Get-ChildItem $ModelDir -Filter *.gguf -File -ErrorAction SilentlyContinue)
if (-not $SkipWithModel) {
    if ($models.Count -eq 0) {
        Add-Problem "含模型版要求的模型文件缺失：app\backend\models\*.gguf" @"
先跑：& tools\build_release.ps1 -IncludeModels
（模型来源依次为 backend\models、models\、deploy\models、-MigrateFrom 目录
 以及 Hugging Face 本地缓存快照）
或只打不含模型版：& tools\make_sfx.ps1 -SkipWithModel
"@
    }
    else {
        $msize = [math]::Round(($models | Measure-Object Length -Sum).Sum / 1MB)
        Write-Ok "模型    : $($models.Count) 个（$msize MB）"
    }
}

# --- 1.7 图标文件存在且含六尺寸 ---
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

# --- 1.8 双语合规文档已生成（任务书「全局要求二」清单） ---
$bilingualPairs = @(
    @("LICENSE", "LICENSE.zh-Hans.md"),
    @("NOTICE", "NOTICE.zh-Hans.md"),
    @("SECURITY.md", "SECURITY.zh-Hans.md"),
    @("CODE_OF_CONDUCT.md", "CODE_OF_CONDUCT.zh-Hans.md"),
    @("docs/PRIVACY.md", "docs/PRIVACY.zh-Hans.md"),
    @("docs/EULA.md", "docs/EULA.zh-Hans.md"),
    @("docs/THIRD_PARTY_LICENSES.md", "docs/THIRD_PARTY_LICENSES.zh-Hans.md")
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
    Write-Ok "双语合规文档 7 对 + docs\ 必备 12 份齐全"
}

# --- 1.9 无调试内容残留（任务书「全局要求五」） ---
$forbiddenFiles = @(
    "session.json", "ui_state.json", "snippets.json", "browser_state.json",
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
# 研发/修复记录属过程文档，不得随发布物分发（任务书「全局要求五」明列）
$devRecords = @(Get-ChildItem $ReleaseDir -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^(v?0\.\d+\.\d+)-(研发记录|修复记录|发布记录)\.md$' })
if ($devRecords.Count -gt 0) {
    $residue += ($devRecords | ForEach-Object { $_.FullName.Substring($ReleaseDir.Length).TrimStart('\') })
}
if ($residue.Count -gt 0) {
    Add-Problem "发布物内有调试内容 / 运行期数据残留 $($residue.Count) 项（前 12 项：$((($residue | Select-Object -First 12) -join ', '))）" `
        "重跑构建脚本会清理运行期残留；过程文档（研发/修复记录）由构建脚本排除，请确认构建脚本是最新版。"
}
else {
    Write-Ok "无调试内容残留（运行期数据 / 构建中间产物 / 过程文档）"
}

# --- 1.10 无中文命名（任务书「全局要求一」） ---
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
    throw "可行性检查未通过（$($script:Problems.Count) 项），未生成任何自解压包。"
}
Write-Ok "可行性检查全部通过（10 项）"

# ===========================================================================
# 2. 打包 payload.7z（极限压缩、LZMA2、固实）
# ===========================================================================
if (-not (Test-Path $Work)) { New-Item -ItemType Directory -Path $Work -Force | Out-Null }
$PayloadWith = Join-Path $Work "payload-with-model.7z"
$PayloadNo = Join-Path $Work "payload-nomodel.7z"

function New-Payload([string]$Dest, [switch]$ExcludeModels) {
    <#
        -ReusePayload：复用已有的 payload.7z，跳过最耗时的压缩步骤。
        为什么需要它：`-mx=9` 固实压缩 885MB 要二十多分钟，而调试 rcedit/config/拼接
        这些**不改发布目录内容**的环节时，payload 完全没必要重打（本轮实测被这条
        拖慢了两轮）。

        但同时必须防止"拿旧 payload 发出新内容"：复用前检查发布目录里有没有比
        payload 更新的文件，有就拒绝复用并说明原因 —— 便宜和正确之间不取巧。
    #>
    if ($ReusePayload -and (Test-Path $Dest)) {
        $newest = Get-ChildItem $ReleaseDir -Recurse -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        $payTime = (Get-Item $Dest).LastWriteTime
        if ($newest -and $newest.LastWriteTime -gt $payTime) {
            throw ("拒绝复用 payload：发布目录里有比它更新的文件" +
                "（$($newest.FullName.Substring($ReleaseDir.Length).TrimStart('\')) " +
                "$($newest.LastWriteTime.ToString('HH:mm:ss')) > payload $($payTime.ToString('HH:mm:ss'))）。" +
                "去掉 -ReusePayload 重新打包，或先更新 payload。")
        }
        Write-Warn2 "复用已有 payload（-ReusePayload）：$(Split-Path -Leaf $Dest) $([math]::Round((Get-Item $Dest).Length / 1MB, 1)) MB"
        return
    }
    if (Test-Path $Dest) { Remove-Item $Dest -Force }
    # 在发布目录内以 * 展开：归档条目就是 app\... / docs\... 这类相对路径，
    # 解压到"当前文件夹"后正好还原发布物的原始层次。
    # -ms=on 固实、-m0=lzma2、-mx=9 极限。
    # 变量名不用 $args：它是 PowerShell 在函数里的自动变量，赋值语义容易踩坑。
    $sevenArgs = @("a", "-t7z", "-m0=lzma2", "-mx=9", "-ms=on", "-bso0", "-bsp0")
    if ($ExcludeModels) {
        # 目录本身与其中内容都要排除，否则不含模型版里会留下一个空的 models\
        $sevenArgs += @('-xr!app\backend\models', '-xr!app\backend\models\*')
    }
    $sevenArgs += @($Dest, "*")
    Push-Location $ReleaseDir
    try {
        Invoke-Native $SevenZipExe $sevenArgs "打包 payload"
    }
    finally {
        Pop-Location
    }
    $size = [math]::Round((Get-Item $Dest).Length / 1MB, 1)
    $files = (@(& $SevenZipExe l -ba $Dest) | Measure-Object).Count
    Write-Ok "$(Split-Path -Leaf $Dest)：$size MB / $files 个条目"
}

if (-not $SkipWithModel) {
    Write-Step "打包含模型 payload（极限压缩，耗时较长）"
    New-Payload $PayloadWith
}
if (-not $SkipNoModel) {
    Write-Step "打包不含模型 payload"
    New-Payload $PayloadNo -ExcludeModels
}

# ===========================================================================
# 3. config.txt（UTF-8 无 BOM）+ 拼接自解压包
# ===========================================================================
Write-Step "生成 config.txt 并拼接自解压包"

<#
    写 UTF-8 无 BOM 文本。
    为什么不用 Set-Content：Windows PowerShell 5.1 的 -Encoding UTF8 会写 BOM，
    SFX 读到 BOM 会把第一个配置键名弄坏（表现为提示语丢失）。
    这里用 .NET 显式指定 no-BOM 编码，字节可控。
#>
function Set-TextNoBom([string]$Path, [string]$Text) {
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $enc)
}

function New-ConfigTxt([string]$Dest, [string]$Title) {
    # 该模块支持的键见本脚本开头的实测说明；刻意不写 OverwriteMode 等不存在的键。
    # BeginPrompt 里明确提示"同名文件会被覆盖"，这就是本模块能做到的"提前询问"。
    $cfg = @"
;!@Install@!UTF-8!
Title="$Title"
BeginPrompt="Extract $Title into the current folder?`n`nIf files with the same name already exist there, they will be replaced."
Progress="yes"
RunProgram="install-complete.bat"
;!@InstallEnd@!
"@
    Set-TextNoBom $Dest $cfg
    $bytes = [System.IO.File]::ReadAllBytes($Dest)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    if ($hasBom) { throw "config.txt 写成了带 BOM 的 UTF-8（$Dest），SFX 会解析失败。" }
    Write-Ok "config.txt（UTF-8 无 BOM，$($bytes.Length) 字节）"
}

<#
    用 cmd 的 copy /b 拼接：<已盖版本资源的 sfx 桩> + config.txt + payload.7z。
    必须走 cmd —— PowerShell 的 Copy-Item 不接受二进制追加。
    路径统一加引号（本仓库路径含中文与空格）。
#>
function Get-ShortPath([string]$Path) {
    try {
        $fso = New-Object -ComObject Scripting.FileSystemObject
        return $fso.GetFile($Path).ShortPath
    }
    catch { return $Path }
}

function Join-Sfx([string]$Exe, [string]$Stub, [string]$Payload, [string]$Title) {
    $cfg = Join-Path $Work "config.txt"
    New-ConfigTxt $cfg $Title
    if (Test-Path $Exe) { Remove-Item $Exe -Force }

    $cmdLine = "copy /b `"$(Get-ShortPath $Stub)`" + `"$(Get-ShortPath $cfg)`" + `"$(Get-ShortPath $Payload)`" `"$Exe`""
    Write-Host "    cmd /c $cmdLine" -ForegroundColor DarkGray
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $out = & cmd /c $cmdLine 2>&1 | Out-String
    $rc = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($rc -ne 0 -or -not (Test-Path $Exe)) {
        throw "拼接自解压包失败（退出码 $rc）: $out"
    }
    Assert-SfxIntact $Exe $Stub $cfg $Payload
    $actual = (Get-Item $Exe).Length
    Write-Ok ("$(Split-Path -Leaf $Exe)：$([math]::Round($actual / 1MB, 1)) MB " +
        "（= 桩 $((Get-Item $Stub).Length) + cfg $((Get-Item $cfg).Length) + payload $((Get-Item $Payload).Length) 字节）")
}

<#
    自解压包完整性断言：文件大小必须正好等于"桩 + config.txt + payload"，
    并且在 **payload 应该开始的那个偏移**上能找到 7z 归档魔数。

    为什么必须断言（v0.2.1 实测踩到，且非常隐蔽）：
    `rcedit` 重写资源时会**重建 PE 映像并丢掉映像之后追加的数据** ——
    同样的机制此前已在 PyInstaller 的 PKG 归档上踩过一次（见 set_metadata.bat 注释）。
    当时把 rcedit 放在拼接**之后**，结果两份"603.8 MB 的安装包"被削成 176 KB 的空壳，
    而 rcedit 退出码是 0、脚本一路报告成功 —— 只有体积合理性检查报了"0.2 MB 超出区间"。
    正确顺序是**先给 7zS.sfx 桩写资源，再拼接**；本函数用来兜住"以后有人又调了顺序"。

    注意魔数的位置：7z 是"签名头在开头、真正的头在末尾"的格式，
    **文件末尾并没有魔数**（第一版在这里按"尾部 64KB 内搜魔数"实现，
    结果对正确的包误报失败）。所以只在固定偏移上核对。
#>
function Assert-SfxIntact([string]$Exe, [string]$Stub, [string]$Cfg, [string]$Payload) {
    $expected = (Get-Item $Stub).Length + (Get-Item $Cfg).Length + (Get-Item $Payload).Length
    $actual = (Get-Item $Exe).Length
    if ($actual -ne $expected) {
        throw ("自解压包完整性被破坏：期望 $expected 字节，实际 $actual 字节（$Exe）。" +
            "若差值正好等于 payload 大小，通常是 rcedit 在拼接之后被调用 —— " +
            "rcedit 重建 PE 映像会丢弃其后追加的 payload，必须先写桩再拼接。")
    }
    $fs = [System.IO.File]::OpenRead($Exe)
    try {
        # payload 应当恰好从"桩 + config.txt"之后开始，那里必须是 7z 魔数
        $offset = (Get-Item $Stub).Length + (Get-Item $Cfg).Length
        $fs.Seek($offset, [System.IO.SeekOrigin]::Begin) | Out-Null
        $head = New-Object byte[] 6
        $read = $fs.Read($head, 0, 6)
        $magic = @(0x37, 0x7A, 0xBC, 0xAF, 0x27, 0x1C)
        $ok = ($read -eq 6)
        for ($i = 0; $i -lt 6 -and $ok; $i++) { if ($head[$i] -ne $magic[$i]) { $ok = $false } }
        if (-not $ok) {
            throw ("payload 偏移 $offset 处不是 7z 归档魔数（读到 " +
                (($head | ForEach-Object { $_.ToString('X2') }) -join ' ') +
                "），文件可能被截断或被覆盖（$Exe）。")
        }
        # 开头必须是 PE 映像（MZ），否则说明拼反了
        $fs.Seek(0, [System.IO.SeekOrigin]::Begin) | Out-Null
        $mz = New-Object byte[] 2
        $fs.Read($mz, 0, 2) | Out-Null
        if ($mz[0] -ne 0x4D -or $mz[1] -ne 0x5A) { throw "文件头不是 MZ，自解压包拼反了（$Exe）。" }
    }
    finally { $fs.Dispose() }
}

# ===========================================================================
# 4. rcedit 写版本资源（九个字段 + 图标）—— 必须写在**桩**上，早于拼接
# ===========================================================================
<#
    顺序说明（这是本脚本最容易搞错的地方，务请看注释再改）：
      1) 复制一份 7zS.sfx 作为"桩"；
      2) 用 rcedit 给**桩**写入九个字段与图标；
      3) 再 copy /b 把 桩 + config.txt + payload.7z 拼成最终 Setup.exe。
    反过来（先拼接再 rcedit）会让 rcedit 重建 PE 映像，把追加的 payload 整段丢掉，
    产出 176 KB 的空壳却"成功返回" —— v0.2.1 实测踩到。
    两个包的 FileDescription / OriginalFilename 不同，所以各盖一份桩。
#>
function New-StampedStub([string]$Dest, [string]$FileDescription, [string]$OriginalFilename) {
    if (-not (Test-Path $Rcedit)) { throw "未找到 rcedit，无法写入版本资源：$Rcedit" }
    if (Test-Path $Dest) { Remove-Item $Dest -Force }
    Copy-Item $SfxModule $Dest -Force
    $rceditArgs = @(
        $Dest,
        "--set-icon", $Icon,
        "--set-version-string", "CompanyName", "Void Entropia Studio",
        "--set-version-string", "LegalCopyright", "Copyright (C) Void Entropia Studio",
        "--set-version-string", "FileDescription", $FileDescription,
        "--set-version-string", "ProductName", "EchoDesk",
        "--set-version-string", "OriginalFilename", $OriginalFilename,
        "--set-version-string", "InternalName", "EchoDeskSetup",
        "--set-version-string", "Comments", "Powered by Void Entropia Studio",
        "--set-file-version", "$Version.0",
        "--set-product-version", "$Version.0"
    )
    Invoke-Native $Rcedit $rceditArgs "写入 $OriginalFilename 版本资源（sfx 桩）"
    # 盖完桩立刻验证：读回 VersionInfo，别等到最后才发现字段没写上
    $vi = (Get-Item $Dest).VersionInfo
    foreach ($pair in @(
            @("FileVersion", "$Version.0"), @("ProductVersion", "$Version.0"),
            @("CompanyName", "Void Entropia Studio"),
            @("ProductName", "EchoDesk"), @("InternalName", "EchoDeskSetup")
        )) {
        if ("$($vi.$($pair[0]))".Trim() -ne $pair[1]) {
            throw "sfx 桩 $($pair[0]) 写入失败：得到 '$($vi.$($pair[0]))'，期望 '$($pair[1])'"
        }
    }
    if ("$($vi.OriginalFilename)" -ne $OriginalFilename) {
        throw "sfx 桩 OriginalFilename 写入失败：$($vi.OriginalFilename)"
    }
    if ("$($vi.FileDescription)" -ne $FileDescription) {
        throw "sfx 桩 FileDescription 写入失败：$($vi.FileDescription)"
    }
    Write-Ok "  $OriginalFilename 桩已写入（FileDescription: $FileDescription，读回校验通过）"
}

if ($script:Problems.Count -eq 0) {
    Write-Step "写入自解压包版本资源与图标（rcedit 写入 sfx 桩，早于拼接）"
    $stubWith = Join-Path $Work "sfx-stub-with-model.exe"
    $stubNo = Join-Path $Work "sfx-stub-nomodel.exe"
    $WithModelExe = Join-Path $OutDir "EchoDesk-v$Version-Setup.exe"
    $NoModelExe = Join-Path $OutDir "EchoDesk-v$Version-NoModel-Setup.exe"
    New-StampedStub $stubWith "EchoDesk v$Version Portable Setup" (Split-Path -Leaf $WithModelExe)
    New-StampedStub $stubNo "EchoDesk v$Version Portable Setup (No Model)" (Split-Path -Leaf $NoModelExe)

    Write-Step "拼接自解压安装包（桩 + config.txt + payload.7z）"
    if (-not $SkipWithModel) { Join-Sfx $WithModelExe $stubWith $PayloadWith "EchoDesk v$Version" }
    if (-not $SkipNoModel) { Join-Sfx $NoModelExe $stubNo $PayloadNo "EchoDesk v$Version (no model)" }

    # 拼接后再次读回版本资源：确认拼接没有破坏（也不该破坏）PE 里的资源
    Write-Step "拼接后复核版本资源（确认 payload 与资源同时存在）"
    foreach ($exe in @($WithModelExe, $NoModelExe)) {
        if (-not (Test-Path $exe)) { continue }
        $vi = (Get-Item $exe).VersionInfo
        if ("$($vi.ProductName)" -ne "EchoDesk" -or "$($vi.FileVersion)" -ne "$Version.0") {
            throw "$(Split-Path -Leaf $exe) 的版本资源在拼接后丢失（ProductName='$($vi.ProductName)' FileVersion='$($vi.FileVersion)'）"
        }
        $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
        Write-Ok "$(Split-Path -Leaf $exe)：$mb MB · ProductName=$($vi.ProductName) · $($vi.FileVersion) · $($vi.OriginalFilename)"
    }
}

# ===========================================================================
# 5. SHA256SUMS.txt
# ===========================================================================
Write-Step "计算 SHA256 校验清单"
$assets = @()
foreach ($p in @(
        $WithModelExe, $NoModelExe,
        (Join-Path $Root "docs\SBOM.json"),
        (Join-Path $Root "docs\THIRD_PARTY_LICENSES.md"),
        (Join-Path $Root "docs\THIRD_PARTY_LICENSES.zh-Hans.md"),
        (Join-Path $Root "LICENSE"), (Join-Path $Root "LICENSE.zh-Hans.md"),
        (Join-Path $Root "NOTICE"), (Join-Path $Root "NOTICE.zh-Hans.md")
    )) {
    if (Test-Path $p) { $assets += (Resolve-Path -LiteralPath $p).Path }
}
$lines = @()
foreach ($a in $assets) {
    $h = (Get-FileHash -LiteralPath $a -Algorithm SHA256).Hash.ToLower()
    # 清单里用文件名：附件是平铺上传的，用户核对时不必关心本机路径
    $lines += "$h  $(Split-Path -Leaf $a)"
}
Set-TextNoBom $SumsFile ($lines -join "`r`n")
Write-Ok "SHA256SUMS.txt（$($lines.Count) 个资产）"
foreach ($l in $lines) { Write-Host "    $l" -ForegroundColor DarkGray }

# ===========================================================================
# 6. 合理性检查（任务书「合理性检查」里与打包有关的部分）
# ===========================================================================
<#
    任务书给的两个区间（含模型约 850-900 MB、不含模型约 350-400 MB）是**解压后**的
    发布目录体积口径，不是自解压包（压缩后）的体积。
    第一版这里拿"自解压包文件大小"去比这两个区间，于是每次都会误报
    "603.6 MB 超出 850-900"；更糟的是，它曾把一个**被 rcedit 削成 176 KB 的空壳**
    报成"0.2 MB 超出区间"，措辞让人以为是压缩率问题而不是文件损坏。
    现在按"解压后体积"比对，并把压缩包大小单独列出来。
#>
Write-Step "打包合理性检查"
$releaseWith = (Get-ChildItem $ReleaseDir -Recurse -File | Measure-Object Length -Sum).Sum
$modelsBytes = (Get-ChildItem $ModelDir -Filter *.gguf -File -ErrorAction SilentlyContinue |
    Measure-Object Length -Sum).Sum
$releaseNo = $releaseWith - $modelsBytes
$warn = 0
foreach ($t in @(
        @{ Bytes = $releaseWith; Min = 850; Max = 900; Label = "含模型版（解压后）" },
        @{ Bytes = $releaseNo; Min = 350; Max = 400; Label = "不含模型版（解压后）" }
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
foreach ($p in @($WithModelExe, $NoModelExe)) {
    if (Test-Path $p) {
        Write-Ok "  $(Split-Path -Leaf $p)：压缩后 $([math]::Round((Get-Item $p).Length / 1MB, 1)) MB"
    }
}
if ($warn -eq 0) { Write-Ok "两份自解压包（解压后）体积均在合理区间内" }

Write-Host ""
Write-Host "打包完成。" -ForegroundColor Cyan
Write-Ok "产物目录: $OutDir"
foreach ($p in @($WithModelExe, $NoModelExe, $SumsFile)) {
    if (Test-Path $p) { Write-Ok "  $(Split-Path -Leaf $p)  $([math]::Round((Get-Item $p).Length / 1MB, 2)) MB" }
}
if (-not $KeepPayload) {
    Write-Ok "中间文件保留在 artifacts\sfx\（payload*.7z + config.txt，便于核对体积与内容）"
}
