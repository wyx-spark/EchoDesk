# 适用平台：Windows 发布链（PyInstaller + rcedit + Inno Setup），三平台各自有别的打包脚本
<#
.SYNOPSIS
    EchoDesk 发布构建脚本：产出「便携版三层结构」的发布物。

.DESCRIPTION
    流程（与任务书「任务三」对应）：
      1. 清空输出目录（可用 -SkipClean 保留，便于只重跑资源收集）；
      2. 构建后端：**干净 venv 的** PyInstaller onedir →
         app\backend\EchoDeskBackend.exe + app\backend\_internal\
         （见 -CleanVenv；刻意不用 PATH 上的 pyinstaller，原因见该参数说明）；
      3. 构建前端：dotnet publish 多文件（非单文件）→ app\ 下平铺依赖 DLL；
      4. 收集图标、配置、语言包、模型与文档；
      5. 生成 USAGE.txt（面向用户的结构与操作说明，中英双语）；
      6. rcedit 写入前端版本资源与图标（后端由 spec 的 version/icon 写入，原因见 spec 注释）；
      7. 可选代码签名（-Sign）；
      8. **不**在发布物里预置 .lnk：快捷方式由安装程序的 [Icons] 段在目标机器上创建
         （原因见第 9 节：.lnk 是绝对路径，预置会把构建机的个人路径带出去）；
      9. 可选 -MakeInstaller：继续调用 tools\make_installer.ps1，用 **Inno Setup** 编译出
         安装程序（一份，不含模型）并计算 SHA256SUMS.txt。
         旧名 -MakeSfx 仍可作为别名使用：7-Zip 自解压包方案已整体废弃，
         原因是官方 SFX 模块不支持 InstallPath、会把内容解压到 %TEMP% 后清理，
         详见 tools\make_installer.ps1 头部说明。

    发布物结构（修复七：应用本体隔离到 app\，外层只留快捷方式、用户数据与文档；
    发布任务书「全局要求一」要求文件名/目录名全为英文）：
      publish\EchoDesk-v0.2.2\
      ├─ (无 .lnk —— 快捷方式由安装程序在目标机器上创建)
      ├─ results\                    自动化结果（相对工作目录生成，按 日期\任务名\ 分层）
      ├─ ui_state.json               界面状态（首次运行时生成）
      ├─ snippets.json               常用语（首次运行时生成）
      ├─ USAGE.txt                   结构与操作说明（中英双语）
      ├─ README.md / README.zh-Hans.md
      ├─ CHANGELOG.md                版本变更记录（英文正本）
├─ CHANGELOG.zh-Hans.md        同上，中文译本
├─ CONTRIBUTING.md             贡献指南（英文正本）
├─ CONTRIBUTING.zh-Hans.md     同上，中文译本
      ├─ config.example.json         配置示例（用户可比对/复制）
      ├─ install-complete.bat        给 zip 免安装用户手动建快捷方式（安装程序自带快捷方式）
      ├─ LICENSE / LICENSE.zh-Hans.md / NOTICE / NOTICE.zh-Hans.md
      ├─ SECURITY.md / SECURITY.zh-Hans.md
      ├─ docs\                       文档（含 zh-Hans\ 与 en\；过程记录已整体移到仓库 records\，不进包）
      ├─ lang\                       后端语言包（可替换/新增，无需重新打包）
      ├─ examples\skills\            示例插件源码
      └─ app\                        程序本体（外层不出现任何 exe/dll）
         ├─ EchoDesk.exe
         ├─ EchoDesk.ico
         ├─ *.dll / *.json          前端依赖
         ├─ lang\ui\                界面语言包
         └─ backend\                后端（PyInstaller onedir）
            ├─ EchoDeskBackend.exe
            ├─ _internal\
            ├─ config.json
            ├─ models\
            └─ skills_dev\          随包示例插件

    为什么用户数据在外层：便携版最常见的用法是整个文件夹拷来拷去，
    把「用户能改、需要备份」的东西（状态、常用语、结果、语言包、文档）放在外层，
    程序文件收进 app\，用户一眼就能分清哪些能碰、哪些别动。

.PARAMETER Version
    版本号（决定输出目录名 publish\EchoDesk-v<Version>），默认 0.2.1。

.PARAMETER IncludeModels
    把本地已有的 .gguf 模型复制进发布物（离线安装包用；体积很大，默认不复制）。
    模型来源依次为 backend\models、models\、deploy\models、-MigrateFrom 目录，
    以及 Hugging Face 本地缓存快照（下载过一次后即可离线打包）。

.PARAMETER MigrateFrom
    从既有发布目录迁移用户数据（snippets.json、session.json、models\*.gguf）。
    兼容旧版"平铺"布局（自动到 <旧目录> 与 <旧目录>\backend\ 两处找）。

.PARAMETER SkipBackend / SkipFrontend / SkipClean
    跳过对应步骤；-SkipClean 用于在已有产物上只重跑资源收集与元数据。

.PARAMETER CleanVenv
    后端构建所用的「干净 venv」目录，默认 `artifacts\clean-venv`。

    **这个参数是本脚本最容易踩的坑，别改成 PATH 上的 pyinstaller。**
    PyInstaller 是按**运行它的那个环境**分析依赖并收集 `.dist-info` 的，所以
    "发布物里到底是哪个版本的库"取决于用的是哪个解释器。开发机的全局 Python
    混着大量开发期残留与旧版包（v0.2.1 实测 132 个发行包），用它打包会打出
    huggingface_hub 1.27.0 / numpy 2.5.2 / pydantic 2.13.4 的后端，
    而 `docs\SBOM.json` 与 `docs\THIRD_PARTY_LICENSES.md` 是按干净 venv
    （2.0.0 / 2.5.3 / 2.13.5）生成的 —— 对外就是"声明了从未随包分发的版本号"，
    属发布任务书「不该有的一样不能有」+ 合规清单失真。

    因此这里解析到 `<CleanVenv>\Scripts\pyinstaller.exe` 并**写死使用**，
    刻意**不提供回退到 PATH 的分支**：找不到就直接失败，宁可让构建停下来，
    也不要再悄悄打出一个与合规清单口径不一致的包。
    门禁：`tools\check_bundled_packages.py` 会逐项比对发布物 `.dist-info` 的版本
    与清单声明的版本，漂移会在那里当场被拦住。

.PARAMETER ReuseBackendFrom
    复用既有发布目录里的后端（<该目录>\app\backend 或 <该目录>\backend），
    跳过耗时的 PyInstaller 步骤。用于**只改前端**时的快速迭代与布局自检
    （几分钟 → 十几秒）。正式交付请务必不带此参数重跑一次。

.PARAMETER KeepSession
    保留 `app\backend\session.json` 里已有的会话内容。

    默认**重置为空会话**：任何一次端到端验证都会让打包后的后端把测试数据写进
    `session.json`（例如 e2e 里 `/file` 加载的临时文件路径与内容），
    交付物不该带着这些残留。只有确实想把上一版的会话延续给用户时才加这个开关
    （配合 -MigrateFrom 使用）。

.PARAMETER Sign
    构建完成后用 signtool 签名（需 -CertThumbprint 或已配置默认证书）。

.PARAMETER MakeInstaller
    在流程末尾继续产出**安装程序**（发布任务书「任务四」）：
    依次完成重命名收口、双语合规文档补齐、反馈渠道检查、调试内容清理，
    再用 Inno Setup 编译出 EchoDesk-v<版本>-Setup.exe（**一份，不含模型**），
    并计算 SHA256SUMS.txt。
    实际动作由 tools\make_installer.ps1 承担（可独立运行、独立做可行性检查），
    这里只负责按顺序调用，保证"一条命令出完整交付物"。

.PARAMETER MakeSfx
    **已废弃的别名**，等价于 -MakeInstaller。

    v0.2.1 之前交付的"自解压安装包"是 `7zS.sfx + config.txt + payload.7z` 拼出来的。
    实测发现：官方 7-Zip SFX 模块只支持 7 个配置键、**没有 InstallPath**，
    默认行为是解压到 `%TEMP%\7zNNNNNN` 并在执行 RunProgram 后删掉该目录，
    用户双击后全盘搜不到文件；仓库里的 `publish\7zSD.sfx` 也只是官方 9.20 的
    `7zS.sfx` 改名（版本资源 InternalName=7zS.sfx），换名字解决不了问题。
    方案已整体换成 Inno Setup。保留这个参数名只为不破坏既有文档/脚本里的老命令。

.EXAMPLE
    & tools\build_release.ps1 -IncludeModels -MigrateFrom publish\EchoDesk-v0.2.0
    & tools\build_release.ps1 -SkipBackend -SkipFrontend -IncludeModels -MakeSfx
#>
[CmdletBinding()]
param(
    # 默认版本（决定输出目录名 publish\EchoDesk-v<Version>）。
    # 口径：v0.2.1 = 跨平台之前；v0.2.2 = 跨平台版本。见 backend\version.py 的说明。
    # 默认版本从 backend\version.py 读（v0.2.2 裁定①）：原来这里写死 "0.2.2"，
    # 升版本时若忘了改，产出的目录名与**写进 exe 的版本资源**都会是旧值。
    # 派生之后，"改版本"只需改 version.py 一处。
    [string]$Version = "",
    [switch]$IncludeModels,
    [string]$MigrateFrom = "",
    [switch]$SkipBackend,
    [switch]$SkipFrontend,
    [switch]$SkipClean,
    [string]$CleanVenv = "artifacts\clean-venv",
    [switch]$KeepAllCultures,
    [string]$ReuseBackendFrom = "",
    [switch]$KeepSession,
    [switch]$Sign,
    [string]$CertThumbprint = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    [switch]$MakeInstaller,
    [switch]$MakeSfx
)

$ErrorActionPreference = "Stop"

<#
    顶层 trap：任何终止性错误都转成"非 0 退出码"。
    为什么必须加（v0.2.1 发布时实测踩到）：脚本内部抛错时，若调用方是用
    `& .\tools\build_release.ps1 -IncludeModels -MakeSfx` 直接调用的，
    **脚本并不会设置退出码** —— 外层看到的是 $LASTEXITCODE=0，
    于是"打包失败"被当成"构建成功"（自解压包一个都没生成，却报告成功）。
    现在统一在这里收口：打印原因并以 1 退出。
#>
trap {
    Write-Host ""
    Write-Host "!! 发布构建失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "   （位置：$($_.InvocationInfo.PositionMessage -replace "`r?`n", ' ')）" -ForegroundColor DarkGray
    exit 1
}

$Root = Split-Path -Parent $PSScriptRoot
if (-not $Version) {
    # 版本从唯一来源派生（v0.2.2 裁定①）：参数默认值改成空串之后，这里必须真正解析出来，
    # 否则下面所有路径都会变成 publish\EchoDesk-v\ 。读法与 verify_release / make_installer 一致。
    $VersionFile = Join-Path $Root "backend\version.py"
    $VersionMatch = Select-String -Path $VersionFile -Pattern 'APP_VERSION\s*=\s*["'']([^"'']+)["'']' |
                    Select-Object -First 1
    if (-not $VersionMatch) { throw "读不出 APP_VERSION：$VersionFile（可用 -Version 显式指定）" }
    $Version = $VersionMatch.Matches[0].Groups[1].Value.TrimStart('v')
    Write-Host "版本号（取自 backend\version.py）：$Version" -ForegroundColor DarkGray
}
$Out = Join-Path $Root "publish\EchoDesk-v$Version"
# 修复七：程序本体统一放进 app\，外层只留快捷方式/用户数据/文档
$AppOut = Join-Path $Out "app"
$BackendOut = Join-Path $AppOut "backend"
$ShortcutName = "Start-EchoDesk-v$Version.lnk"

function Write-Step([string]$text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Ok([string]$text) { Write-Host "    $text" -ForegroundColor Green }
function Write-Warn2([string]$text) { Write-Host "    $text" -ForegroundColor Yellow }

<#
    运行外部命令并容忍其 stderr 输出。
    注意：PowerShell 在 ErrorActionPreference=Stop 时会把原生命令的 stderr
    包装成终止性错误（NativeCommandError），导致脚本在 PyInstaller/dotnet
    只打印警告时就中断（曾因此让构建在第 2 步莫名退出）。这里临时放宽偏好
    并把 stderr 转为普通输出，再依据退出码判断成败。
#>
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

Write-Step "EchoDesk v$Version 发布构建"
Write-Ok "仓库根目录: $Root"
Write-Ok "输出目录  : $Out"

# ---------- 0. 前置检查 ----------
$Rcedit = Join-Path $Root "tools\rcedit-x64.exe"
$Icon = Join-Path $Root "assets\EchoDesk.ico"
if (-not (Test-Path $Icon)) {
    throw "未找到图标 $Icon，请先执行: python tools\make_icon.py"
}
if (-not (Test-Path $Rcedit)) {
    Write-Warn2 "未找到 rcedit，将尝试下载（tools\fetch_tools.bat）"
    Invoke-Native "cmd" @("/c", "`"$Root\tools\fetch_tools.bat`"") "下载 rcedit"
    if (-not (Test-Path $Rcedit)) { Write-Warn2 "rcedit 不可用，将跳过前端版本资源写入" }
}
if (Get-Process EchoDesk -ErrorAction SilentlyContinue) {
    throw "EchoDesk.exe 正在运行，请先关闭再构建（否则前端发布可能失败）。"
}

# ---------- 1. 清空输出 ----------
Write-Step "清理输出目录"
# 先校验 -ReuseBackendFrom：它在第 2 步才用到，但清空输出会把它一起删掉
# （曾经把复用源指向本次输出目录，结果"清空 → 找不到后端"）。
$ReuseSrc = ""
if ($ReuseBackendFrom) {
    $srcRoot = (Resolve-Path -LiteralPath $ReuseBackendFrom).Path
    if ($srcRoot.TrimEnd('\') -eq $Out.TrimEnd('\') -or $srcRoot -like "$Out\*") {
        throw "-ReuseBackendFrom 不能指向本次输出目录（$Out）：清空输出会把复用源一起删掉。"
    }
    $ReuseSrc = @(
        (Join-Path $srcRoot "app\backend"),
        (Join-Path $srcRoot "backend")
    ) | Where-Object { Test-Path (Join-Path $_ "EchoDeskBackend.exe") } | Select-Object -First 1
    if (-not $ReuseSrc) { throw "在 $ReuseBackendFrom 下找不到可用的后端（app\backend 或 backend）" }
}
if ($SkipClean) {
    Write-Warn2 "已指定 -SkipClean，保留输出目录现有内容（用于只重跑资源收集/元数据）"
}
else {
    if (Test-Path $Out) { Remove-Item $Out -Recurse -Force }
    Write-Ok "已清空 $Out"
}
New-Item -ItemType Directory -Path $AppOut -Force | Out-Null
New-Item -ItemType Directory -Path $BackendOut -Force | Out-Null

# ---------- 2. 构建后端（PyInstaller onedir） ----------
if ($ReuseSrc) {
    Write-Step "复用既有后端（-ReuseBackendFrom，跳过 PyInstaller）"
    if (Test-Path $BackendOut) { Remove-Item $BackendOut -Recurse -Force }
    New-Item -ItemType Directory -Path $BackendOut -Force | Out-Null
    Copy-Item (Join-Path $ReuseSrc "*") $BackendOut -Recurse -Force
    $size = [math]::Round((Get-ChildItem $BackendOut -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
    Write-Ok "后端已复用: $ReuseSrc → app\backend\（$size MB）"
    Write-Warn2 "注意：这是快速迭代模式，正式交付请不带 -ReuseBackendFrom 重新构建后端"
}
elseif (-not $SkipBackend) {
    Write-Step "构建后端（PyInstaller onedir，耗时较长）"

    # ---- 只用干净 venv 的 PyInstaller，**绝不回退到 PATH**（根因修复，见 -CleanVenv） ----
    # v0.2.1 发布前实测：这里原先写的是 `Invoke-Native "pyinstaller" ...`，
    # 于是解析到 C:\...\Programs\Python\Python313\Scripts\pyinstaller.exe（全局开发环境），
    # 打出的后端里 11 个 .dist-info 有 5 个版本与合规清单不符（click / cryptography /
    # huggingface_hub / numpy / pydantic）。改成显式路径，并让"找不到"成为终止性错误。
    $VenvRoot = if ([System.IO.Path]::IsPathRooted($CleanVenv)) {
        $CleanVenv
    } else {
        Join-Path $Root $CleanVenv
    }
    $PyInstallerExe = Join-Path $VenvRoot "Scripts\pyinstaller.exe"
    if (-not (Test-Path $PyInstallerExe)) {
        throw ("未找到干净 venv 的 PyInstaller：$PyInstallerExe`n" +
               "        后端必须用干净 venv 构建（原因见 -CleanVenv 说明）。" +
               "这里刻意不提供回退到 PATH 的分支：`n" +
               "        回退正是'发布物库版本取决于 PATH 恰好指向谁'这起合规缺陷的根因。`n" +
               "        请先创建干净环境：`n" +
               "          python -m venv $CleanVenv`n" +
               "          & `"$CleanVenv\Scripts\python.exe`" -m pip install -r backend\requirements.txt")
    }
    $pyiVersion = ""
    $prevEap = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $pyiVersion = (& $PyInstallerExe --version 2>&1 | Select-Object -First 1)
    }
    finally {
        $ErrorActionPreference = $prevEap
    }
    Write-Ok "后端构建器：$PyInstallerExe（PyInstaller $pyiVersion）"

    $stage = Join-Path $env:TEMP "echodesk_backend_stage"
    $work = Join-Path $env:TEMP "echodesk_backend_work"
    Remove-Item $stage, $work -Recurse -Force -ErrorAction SilentlyContinue

    Push-Location $Root
    try {
        Invoke-Native $PyInstallerExe @("--noconfirm", "--distpath", $stage, "--workpath", $work, "backend\EchoBackend.spec") "后端构建"
    }
    finally {
        Pop-Location
    }

    $built = Join-Path $stage "EchoDeskBackend"
    if (-not (Test-Path $built)) { throw "未找到后端产物目录: $built" }
    # 先清空旧后端目录再复制：否则上一版残留的依赖文件（例如已被排除的 torch）
    # 会留在 _internal 里，体积与合规风险都不会真正下降。
    if (Test-Path $BackendOut) { Remove-Item $BackendOut -Recurse -Force }
    New-Item -ItemType Directory -Path $BackendOut -Force | Out-Null
    Copy-Item (Join-Path $built "*") $BackendOut -Recurse -Force
    Remove-Item $stage, $work -Recurse -Force -ErrorAction SilentlyContinue
    $size = [math]::Round((Get-ChildItem $BackendOut -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
    Write-Ok "后端已就位: app\backend\EchoDeskBackend.exe（依赖在 app\backend\_internal\，$size MB）"
}

# ---------- 3. 构建前端（多文件发布，输出到 app\） ----------
if (-not $SkipFrontend) {
    Write-Step "构建前端（dotnet publish 多文件模式 → app\）"
    $publishArgs = @(
        "publish", (Join-Path $Root "frontend\EchoDesk.csproj"),
        "-c", "Release", "-r", "win-x64", "--self-contained", "true",
        "-p:PublishSingleFile=false",
        "-p:DebugType=none", "-p:DebugSymbols=false",
        "-p:NuGetAudit=false",
        "-o", $AppOut
    )
    Invoke-Native "dotnet" $publishArgs "前端构建"
    $dllCount = (Get-ChildItem $AppOut -Filter *.dll -File).Count
    Write-Ok "前端已就位: app\EchoDesk.exe（同目录 $dllCount 个依赖 DLL）"
}

# ---------- 3b. 清理 app\：只保留 EchoDesk.exe 这一个可执行文件 ----------
Write-Step "清理 app\（多余可执行文件 / 调试符号 / 未使用的区域资源）"
$stray = Get-ChildItem $AppOut -Filter *.exe -File | Where-Object { $_.Name -ne "EchoDesk.exe" }
foreach ($f in $stray) {
    Write-Ok "移除多余可执行文件: $($f.Name)"
    Remove-Item $f.FullName -Force
}
Get-ChildItem $AppOut -Filter *.pdb -File -ErrorAction SilentlyContinue | Remove-Item -Force
if (-not $KeepAllCultures) {
    # .NET 卫星区域资源目录：与运行无关（界面文案走自带的 lang\ui 语言包），
    # 只留中文，其余删除以减小体积与目录噪音。
    $keep = @("zh-Hans", "zh-Hant")
    foreach ($d in Get-ChildItem $AppOut -Directory | Where-Object { $_.Name -match '^[a-z]{2}(-[A-Za-z]+)?$' }) {
        if ($keep -notcontains $d.Name) { Remove-Item $d.FullName -Recurse -Force }
    }
}
Write-Ok "app\ 内可执行文件仅剩 EchoDesk.exe"

# ---------- 4. 配置与资源 ----------
Write-Step "收集配置、图标与模型"

# 用户可编辑的配置放到后端目录（后端以该目录为工作目录）
Copy-Item (Join-Path $Root "backend\config.json") (Join-Path $BackendOut "config.json") -Force
Write-Ok "backend\config.json"

# 后端语言包（v0.2.1 任务三）：也放一份到程序根目录的 lang\，
# 这样用户/翻译者可以直接替换或新增语言包而无需重新打包（i18n.py 优先读这里）。
$LangSrc = Join-Path $Root "backend\lang"
if (Test-Path $LangSrc) {
    $LangOut = Join-Path $Out "lang"
    New-Item -ItemType Directory -Path $LangOut -Force | Out-Null
    Copy-Item (Join-Path $LangSrc "*.json") $LangOut -Force
    $langCount = (Get-ChildItem $LangOut -Filter *.json -File).Count
    Write-Ok "lang\（后端语言包，$langCount 个文件；界面语言包由前端发布到 lang\ui\）"
}

# 示例插件（v0.2.1 任务二 2.5）：源码在 examples\skills\（入库），
# 复制到 backend\skills_dev\ 让"放进去即被识别"开箱可用。
# backend\skills_dev\ 本身不入库（属运行时目录），因此必须由打包脚本填充。
$ExampleSrc = Join-Path $Root "examples\skills"
if (Test-Path $ExampleSrc) {
    $DevOut = Join-Path $BackendOut "skills_dev"
    New-Item -ItemType Directory -Path $DevOut -Force | Out-Null
    foreach ($skill in Get-ChildItem $ExampleSrc -Directory) {
        Copy-Item $skill.FullName $DevOut -Recurse -Force
    }
    Get-ChildItem $DevOut -Directory -Recurse -Filter "__pycache__" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    $skillCount = (Get-ChildItem $DevOut -Directory).Count
    Write-Ok "backend\skills_dev\（示例插件 $skillCount 个）"
    $ExOut = Join-Path $Out "examples\skills"
    New-Item -ItemType Directory -Path $ExOut -Force | Out-Null
    Copy-Item (Join-Path $ExampleSrc "*") $ExOut -Recurse -Force
}

# 示例 MCP 服务器（v0.2.1 能力扩展-1 任务三）：源码在 examples\mcp\（入库）。
# **必须随包分发**：设置窗口「智能体 → MCP」有一键「＋ 加示例服务器」，
# 它把 examples\mcp\echo_server.py 填进配置让用户立刻验证通路；
# 不打包的话这个按钮在发布版里会指向一个不存在的文件
# （前端子代理在联调时发现：打包脚本此前只复制 examples\skills）。
$McpSrc = Join-Path $Root "examples\mcp"
if (Test-Path $McpSrc) {
    $McpOut = Join-Path $Out "examples\mcp"
    New-Item -ItemType Directory -Path $McpOut -Force | Out-Null
    Copy-Item (Join-Path $McpSrc "*") $McpOut -Recurse -Force
    Get-ChildItem $McpOut -Directory -Recurse -Filter "__pycache__" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Write-Ok "examples\mcp\（示例 MCP 服务器 $((Get-ChildItem $McpOut -File).Count) 个文件）"
}

# 图标：随程序本体放在 app\（快捷方式通过绝对路径引用它）
Copy-Item $Icon (Join-Path $AppOut "EchoDesk.ico") -Force
Write-Ok "app\EchoDesk.ico"

# 模型目录
$ModelDir = Join-Path $BackendOut "models"
New-Item -ItemType Directory -Path $ModelDir -Force | Out-Null

$modelSources = @()
$modelSources += @(
    (Join-Path $Root "backend\models"),
    (Join-Path $Root "models"),
    (Join-Path $Root "deploy\models")
)
if ($MigrateFrom) {
    $modelSources += @(
        (Join-Path $MigrateFrom "models"),
        (Join-Path $MigrateFrom "backend\models")
    )
}
# Hugging Face 本地缓存快照：模型下载过一次就会留在这里，
# 从缓存复制即可不联网直接做出离线安装包。
$hfCache = Join-Path $env:USERPROFILE ".cache\huggingface\hub"
if (Test-Path $hfCache) {
    $snaps = Get-ChildItem $hfCache -Directory -Filter "models--*" -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName "snapshots" } |
        Where-Object { Test-Path $_ } |
        ForEach-Object { Get-ChildItem $_ -Directory -ErrorAction SilentlyContinue } |
        ForEach-Object { $_.FullName }
    $modelSources += @($snaps)
}
# 注意：必须用 @() 包裹后再过滤——PowerShell 会把"单元素结果"退化成字符串，
# 之后 += 就变成字符串拼接，导致模型一个都复制不到（曾踩过这个坑）。
$modelSources = @($modelSources | Where-Object { Test-Path $_ })

if ($IncludeModels) {
    $copied = 0
    foreach ($src in $modelSources) {
        foreach ($gguf in Get-ChildItem $src -Filter *.gguf -File -ErrorAction SilentlyContinue) {
            $dest = Join-Path $ModelDir $gguf.Name
            if (-not (Test-Path $dest)) {
                Copy-Item $gguf.FullName $dest -Force
                Write-Ok "模型: $($gguf.Name)（$([math]::Round($gguf.Length / 1MB)) MB）"
                $copied++
            }
        }
    }
    if ($copied -eq 0) {
        # 区分两种情况 —— 原来的措辞只有"未找到可复制的 .gguf 模型"，
        # 在 -SkipClean 复用已有产物时（模型本来就在 models\ 里）会打出
        # "未找到模型"的警告，让人以为模型丢了（实测被误导过一次）。
        $already = @(Get-ChildItem $ModelDir -Filter *.gguf -File -ErrorAction SilentlyContinue)
        if ($already.Count -gt 0) {
            Write-Ok "模型已在位，无需复制（$($already.Count) 个：$((($already | ForEach-Object { $_.Name }) -join ', '))）"
        }
        else {
            Write-Warn2 "未找到可复制的 .gguf 模型（已查找：backend\models、models\、deploy\models、-MigrateFrom、HF 缓存），首次启动时将询问是否下载"
        }
    }
}
else {
    Write-Warn2 "未复制模型（未指定 -IncludeModels）；首次启动会询问是否下载模型"
}

# ---------- 5. 迁移用户数据 ----------
if ($MigrateFrom) {
    Write-Step "迁移用户数据"
    # 兼容旧版平铺布局：snippets/ui_state 既可能在旧目录根，也可能在旧目录\app\
    $snipCandidates = @(
        (Join-Path $MigrateFrom "snippets.json"),
        (Join-Path $MigrateFrom "app\snippets.json")
    )
    foreach ($snip in $snipCandidates) {
        if (Test-Path $snip) {
            Copy-Item $snip (Join-Path $Out "snippets.json") -Force
            Write-Ok "常用语 snippets.json"
            break
        }
    }
    $sessionCandidates = @(
        (Join-Path $MigrateFrom "backend\session.json"),
        (Join-Path $MigrateFrom "app\backend\session.json"),
        (Join-Path $MigrateFrom "session.json")
    )
    foreach ($sess in $sessionCandidates) {
        if (Test-Path $sess) {
            Copy-Item $sess (Join-Path $BackendOut "session.json") -Force
            Write-Ok "会话 session.json"
            break
        }
    }
}

# ---------- 6. 用户说明（USAGE.txt，中英双语） ----------
# 发布任务书「全局要求四」要求根目录必备 USAGE.txt（面向普通用户的结构与操作说明），
# 「全局要求一」要求文件名英文（原为 使用说明.txt）。
# 正文英文在前、中文在后：面向全球用户先给英文，中文用户往下看同一份内容。
Write-Step "生成 USAGE.txt（结构与操作说明，中英双语）"
$buildTime = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$usage = @"
EchoDesk v$Version - Local AI Desktop Assistant
Powered by Void Entropia Studio
================================================================

[HOW TO START]
  Installed with the Setup program? Launch EchoDesk from the Start Menu
  shortcut (or the desktop shortcut if you ticked that box); its working
  directory is this folder, which is where your data lives.
  Using the portable zip instead? Double-click "$ShortcutName" in this
  folder - that shortcut is the only entry point you need. If it is not
  there yet, run "install-complete.bat" once to create it; it points at
  *your* paths, not someone else's.
  Do not go hunting inside app\: app\EchoDesk.exe is the UI process, and
  app\backend\EchoDeskBackend.exe is started by it - do not run the
  backend on its own.

[WHAT IS IN THIS FOLDER]
  (The program itself is tucked into app\, so everything you can safely
   touch or back up sits out here at the top level.)

  $ShortcutName    portable-zip entry point (working directory = this folder;
                           created by install-complete.bat on *your* machine)
  USAGE.txt                this file
  results\                 automation output (date\task\ folders holding a
                           plain-text result.txt plus the downloaded files)
  ui_state.json            UI state: window position/size, font size, theme, language
  snippets.json            your reusable prompt templates
  config.example.json      a copy of the default backend configuration
  install-complete.bat     creates the shortcut in this folder (portable-zip
                           users run it once; safe to run again)
  README.md / README.zh-Hans.md
  CHANGELOG.md / CHANGELOG.zh-Hans.md
                           what changed in each version (English + Chinese)
  LICENSE / LICENSE.zh-Hans.md / NOTICE / NOTICE.zh-Hans.md
  SECURITY.md / SECURITY.zh-Hans.md
  CONTRIBUTING.md / CONTRIBUTING.zh-Hans.md
                           how to report issues, contribute and translate
  docs\                    documentation (user guide, FAQ, automation guide,
                           privacy policy, EULA, compliance, SBOM)
  docs\en\                 English documentation
  docs\zh-Hans\            Chinese documentation
  docs\DEVELOPER.md        architecture, protocol, extension points (pointer;
                           the full text lives in docs\en\ and docs\zh-Hans\)
  lang\                    backend language packs (system messages, errors, /help)
  examples\skills\         example skill sources (Hello World and OFD reader)
  app\                     the program itself (please leave it alone)
    EchoDesk.exe             the UI process
    EchoDesk.ico             program icon
    lang\ui\                 UI language packs (buttons/labels/dialog text)
    backend\                 backend: inference, file parsing, conversations
      config.json            configuration (model, context length, summarisation,
                             automation, skill marketplace, model hub, ...)
      models\                local models (.gguf files go here)
      session.json           last session (saved/restored automatically)
      skills_dev\            bundled example skills
      models_registry.json   model library record (created on first use)

[ADDING A NEW LANGUAGE]
  UI text:      drop <language-tag>.json into app\lang\ui\ (for example ja.json),
                modelled on app\lang\ui\zh-Hans.json; restart, then pick it in Settings.
  System text:  drop <language-tag>.json into lang\, modelled on the existing zh-Hans.json.
  Both folders are yours to replace; upgrading the program does not overwrite lang\.

[FIRST LAUNCH]
  * If app\backend\models\ is empty the program does NOT exit and does NOT block: it
    starts in a "no model yet" state, shows guidance, and stays usable (file parsing,
    settings and the model market all work; only inference returns the guidance).
  * Three ways to get a model, none of which requires restarting the program:
      1) Settings -> Model -> Model market. The list shows EVERY GGUF repository
         (nothing is hidden by hardware filtering); entries that fit this machine are
         marked "(Recommended)" and the configured default model's repo is pinned first.
      2) Drop a .gguf file into app\backend\models\ and click "Rescan models folder"
         (if exactly one model is found it is loaded automatically).
      3) Click "Load" next to an already-downloaded model in the local model library.
  * A download or a load hot-loads the model: the status bar stops saying "no model"
    and inference works immediately. If loading fails (corrupt file, unsupported
    format, not enough memory) the app stays in the no-model state and says why.
  * Long documents are summarised in segments, and the progress (segment i of N,
    elapsed time, current phase, whether a segment is merely slow) is shown live in
    the chat; the stop button aborts it and the raw text is kept, not half-summarised.
  * It asks whether to create a launcher shortcut in this folder; the installer
    already gave you a Start Menu / desktop shortcut, and portable-zip users can
    just say yes here instead of running install-complete.bat.

[COMMON COMMANDS]
  /help            list every command
  /file <path>     attach a file (long documents are summarised in chunks, not truncated)
  /summarize       summarise the current long document on demand
  /model           list or switch local models
  /status          model capability, mode, context usage and KV-cache estimate
  /task            start from a task template
  /preset          switch generation presets (precise / balanced / creative)
  /compact         compress the conversation context
  /darkmode auto   follow the system theme (or on/off to pin light/dark)
  Ctrl+Enter send - Esc stop - Ctrl+L clear - Ctrl+O attach - Ctrl+T toggle theme
  Ctrl+wheel font size - Ctrl+Shift+C copy the whole conversation
  Each message has Copy / Markdown / Code buttons in its bottom-right corner.

[AFTER MOVING THE FOLDER]
  The shortcut stores an absolute path. After moving the whole folder, run
  app\EchoDesk.exe once: the program offers to rebuild the shortcut (and fixes
  the working directory automatically).

[UNINSTALL]
  EchoDesk is a portable app: just delete the whole folder. Nothing is written
  to the registry.

[OTHER PLATFORMS AND COMMAND EXAMPLES]
  This folder is the Windows build (WPF frontend + PyInstaller backend). Linux
  and macOS use the same backend with an Avalonia frontend, shipped as separate
  bundles on the same release page:
    Windows  (this one)   EchoDesk-v$Version-Setup.exe  /  EchoDesk-v$Version-win-x64.zip
    Linux x86-64          EchoDesk-v$Version-linux-x64.tar.gz
    macOS (Apple silicon) EchoDesk-v$Version-osx-arm64.tar.gz
  There is no installer on Linux/macOS: unpack the archive, then run the
  launcher sitting at the top of the bundle.

    # Windows (PowerShell) - you are already in the right place
    .\app\EchoDesk.exe

    # macOS / Linux - unpack, then start through the bundled launcher
    tar -xzf EchoDesk-v$Version-linux-x64.tar.gz
    cd EchoDesk-v$Version-linux-x64
    ./start.sh

  The data folder differs by platform too; the full table lives in
  config.example.json under "_platform_paths":
    Windows               the folder containing this file
    macOS                 ~/Library/Application Support/EchoDesk
    Linux                 ~/.local/share/EchoDesk  (or `$XDG_DATA_HOME/EchoDesk)

[VERIFY YOUR DOWNLOAD]
  Compare the SHA256 of the package you downloaded against SHA256SUMS.txt,
  which ships alongside the release; it lists the package and the other
  release assets.

[FEEDBACK]
  Problems and feature requests: https://github.com/VoidEntropiaStudio/EchoDesk/issues
  Questions and discussion:      https://github.com/VoidEntropiaStudio/EchoDesk/discussions
  Security vulnerabilities:      report privately - do not open a public issue;
                                see SECURITY.md for the private channel

Build time: $buildTime

================================================================
中文说明（同一份内容的中文版）
================================================================

【怎么启动】
  用安装程序装的：从开始菜单快捷方式启动（勾了桌面图标就用桌面那个）；
  它的工作目录就是本文件夹，你的数据都生成在这里。
  用 zip 免安装版的：双击本文件夹内的「$ShortcutName」即可，这是唯一需要的入口。
  若还没有这个快捷方式，先运行一次 install-complete.bat 创建它；它创建的是
  指向**你自己机器上真实路径**的快捷方式。
  不用去 app\ 里找可执行文件：app\EchoDesk.exe 是界面主程序，
  app\backend\EchoDeskBackend.exe 由界面自动拉起，请不要单独运行。

【文件夹里都有什么】
  （程序本体收进 app\，外层只放你能改、需要备份的东西）

  $ShortcutName   zip 免安装版的启动入口（工作目录就是本文件夹；由 install-complete.bat
                     在你的机器上创建）
  USAGE.txt          本文件
  results\           自动化结果（按 日期\任务名\ 分层的纯文本 result.txt + 下载的原始文件）
  ui_state.json      界面状态：窗口位置与大小、字号、主题、语言
  snippets.json      常用语模板
  config.example.json  后端默认配置的副本（可用于比对或恢复默认）
  install-complete.bat 在本文件夹创建快捷方式（zip 免安装版用户跑一次；可重复运行）
  README.md / README.zh-Hans.md
  CHANGELOG.md / CHANGELOG.zh-Hans.md
                     各版本变更记录（英文正本 + 中文译本）
  LICENSE / LICENSE.zh-Hans.md / NOTICE / NOTICE.zh-Hans.md
  SECURITY.md / SECURITY.zh-Hans.md
  CONTRIBUTING.md / CONTRIBUTING.zh-Hans.md
                     如何提交 Issue、贡献代码与贡献翻译
  docs\              文档（用户手册、FAQ、自动化指南、隐私政策、许可协议、合规文档、SBOM）
  docs\zh-Hans\      中文文档
  docs\en\           英文文档
  docs\DEVELOPER.md  架构、协议与扩展点（指针；正文在 docs\en\ 与 docs\zh-Hans\ 下）
  lang\              后端语言包（系统消息、错误提示、指令帮助）
  examples\skills\   示例插件源码（Hello World 与 OFD 读取）
  app\               程序本体（请勿随意改动）
    EchoDesk.exe       界面主程序
    EchoDesk.ico       程序图标
    lang\ui\           界面语言包（按钮/标签/对话框文案）
    backend\           后端：模型推理、文件解析、对话管理
      config.json      配置（模型、上下文长度、分段摘要、自动化、技能市场、模型中心等）
      models\          本地模型目录（.gguf 放这里）
      session.json     上次会话（自动保存/恢复）
      skills_dev\      随包示例插件
      models_registry.json  模型库记录（首次使用时生成）

【语言包怎么加新语言】
  界面文案：在 app\lang\ui\ 下放一份 <语言标签>.json（如 ja.json），
            可参照 app\lang\ui\zh-Hans.json 的结构；重启后在设置里即可选择。
  系统消息：在 lang\ 下放一份 <语言标签>.json，参照已有的 zh-Hans.json。
  两个目录都可以被你自己替换，升级程序不会覆盖外层 lang\。

【首次启动】
  * 若 app\backend\models\ 里没有模型，程序**不会退出、也不会卡住**：它以"尚无模型"状态
    启动，给出引导并保持可用（文件解析、设置、模型市场都正常，只有推理会返回引导）。
  * 三种拿到模型的方式，都不需要重启程序：
      1) 设置 → 模型 → 模型市场。列表显示**全部** GGUF 仓库（不按硬件过滤隐藏），
         适合本机的条目标有「(推荐)」，当前默认模型对应的仓库固定在列表最前。
      2) 把 .gguf 文件放进 app\backend\models\，点「重新扫描模型文件夹」
         （只扫描到一个模型时会自动加载）。
      3) 在「本地模型库」里点已下载模型旁的「加载」。
  * 下载完成或加载成功后**热加载**：状态栏不再显示"无模型"，推理立即可用。
    加载失败（文件损坏 / 格式不支持 / 内存不足）时保持无模型状态并说明原因。
  * 长文档会分段摘要，进度（第几段/共几段、已耗时、当前阶段、是否只是偏慢）在对话框里
    实时显示；点停止可中止，原文保留、不会注入半份摘要。
  * 会询问是否在本文件夹创建启动快捷方式；安装程序已给你开始菜单/桌面快捷方式，
    zip 免安装版用户在这里选"是"即可，不必再手动跑 install-complete.bat。

【常用操作】
  /help            查看全部指令
  /file <路径>     上传文件（长文档自动分段摘要，不截断丢信息）
  /summarize       对当前长文档手动触发全文摘要
  /model           查看/切换本地模型
  /status          查看模型能力、模式、上下文占用与 KV Cache 估算
  /task            从任务模板开始
  /preset          切换生成预设（精确 / 均衡 / 创意）
  /compact         压缩对话上下文
  /darkmode auto   主题跟随系统（也可 on/off 固定深浅色）
  Ctrl+Enter 发送 · Esc 停止 · Ctrl+L 清空 · Ctrl+O 上传 · Ctrl+T 切换主题
  Ctrl+滚轮 调字号 · Ctrl+Shift+C 复制全部对话
  每条消息右下角可「复制 / Markdown / 代码」

【移动文件夹之后】
  快捷方式里记的是绝对路径；把整个文件夹搬到别处后，重新运行一次
  app\EchoDesk.exe，程序会提示并重建快捷方式（也会自动修正工作目录）。

【卸载】
  本程序为绿色便携版：直接删除整个文件夹即可，不写注册表。

【其他平台与命令示例】
  本文件夹是 **Windows** 版（WPF 前端 + PyInstaller 后端）。Linux 与 macOS 用同一份后端
  配 Avalonia 前端，在同一发布页上以单独的压缩包提供：
    Windows（本目录）      EchoDesk-v$Version-Setup.exe  /  EchoDesk-v$Version-win-x64.zip
    Linux x86-64           EchoDesk-v$Version-linux-x64.tar.gz
    macOS（Apple 芯片）    EchoDesk-v$Version-osx-arm64.tar.gz
  Linux/macOS 上没有安装程序：解压后直接运行压缩包顶层的启动脚本即可。

    # Windows（PowerShell）—— 你已经在正确的位置
    .\app\EchoDesk.exe

    # macOS / Linux —— 解压后通过随包的启动脚本运行
    tar -xzf EchoDesk-v$Version-linux-x64.tar.gz
    cd EchoDesk-v$Version-linux-x64
    ./start.sh

  用户数据目录在三平台上也不一样，完整对照见 config.example.json 里的 "_platform_paths"：
    Windows               本文件所在的文件夹
    macOS                 ~/Library/Application Support/EchoDesk
    Linux                 ~/.local/share/EchoDesk（设了 `$XDG_DATA_HOME 时为 `$XDG_DATA_HOME/EchoDesk）

【校验下载是否完整】
  把下载到的安装包 SHA256 与随发布提供的 SHA256SUMS.txt 对照即可，
  该文件同时列出了安装包与其它发布资产。

【反馈】
  问题与功能请求：https://github.com/VoidEntropiaStudio/EchoDesk/issues
  提问与讨论：    https://github.com/VoidEntropiaStudio/EchoDesk/discussions
  安全漏洞：      请私下报告，不要开公开 Issue —— 见 SECURITY.md

构建时间：$buildTime
"@
# .txt 写成 UTF-8 **带 BOM**：Windows 10 的记事本遇到无 BOM 的 UTF-8 会按 ANSI 解码，
# 中文会变乱码。被程序/工具解析的文件才必须无 BOM：install-complete.bat（第 6b 步有断言）
# 与 tools\installer.iss 都是无 BOM，改动时别把 BOM 带进去。
$utf8Bom = New-Object System.Text.UTF8Encoding($true)
[System.IO.File]::WriteAllText((Join-Path $Out "USAGE.txt"), $usage, $utf8Bom)
Write-Ok "USAGE.txt（结构与操作说明，英文 + 中文）"

# 合规文档随发布包一同提供（任务书「全局要求二 / 全局要求四」）：
# 外层放英文默认版 + 对应的 .zh-Hans.md 中文译本，成对出现，用户与法务可直接取用。
# README 与 CHANGELOG 也在必备清单里；docs\ 下的同名文档由第 6c 步整份复制。
foreach ($legal in @(
        "LICENSE", "LICENSE.zh-Hans.md",
        "NOTICE", "NOTICE.zh-Hans.md",
        "SECURITY.md", "SECURITY.zh-Hans.md",
        "README.md", "README.zh-Hans.md",
        "CHANGELOG.md", "CHANGELOG.zh-Hans.md",
        "CONTRIBUTING.md", "CONTRIBUTING.zh-Hans.md"
    )) {
    $src = Join-Path $Root $legal
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $Out $legal) -Force
    }
    else {
        Write-Warn2 "缺少 $legal（任务书「全局要求四」要求根目录必备），发布物会缺这份文件"
    }
}
Write-Ok "外层合规与说明文档（LICENSE/NOTICE/SECURITY/README/CHANGELOG/CONTRIBUTING，各含中英两份）"

# 配置示例：后端默认配置的副本 + 双语说明 + 三平台路径对照。
#
# 为什么不再"原样复制"（v0.2.2 全面双语与三平台友好 任务五 5.1）：
# 用户打开这个文件时看到的是 200 多行没有任何解释的 JSON —— 字段什么意思、改了会不会生效、
# "用户数据目录"在他自己的平台上到底是哪个路径，全都要去翻文档。
# 现在改由 tools\gen_config_example.py **生成**：它在副本上注入 `_comment`（双语说明）
# 与 `_platform_paths`（三平台目录对照）。
#
# **注入放在副本上，运行期配置一个字不动**：backend\config.json 会被 pydantic 直接解析，
# 往里塞自定义键是在赌"模型允许额外字段"，赌输的后果是后端启动即失败。
# 生成器是 Python，三平台构建走同一条路径、得到同一份结果（Windows 发布链与
# tools\pack_crossplatform.py 都调用它，口径不会漂移）。
$cfgSrc = Join-Path $Root "backend\config.json"
if (Test-Path $cfgSrc) {
    & python (Join-Path $Root "tools\gen_config_example.py") --out (Join-Path $Out "config.example.json")
    if ($LASTEXITCODE -ne 0) { throw "生成 config.example.json 失败（退出码 $LASTEXITCODE）" }
    Write-Ok "config.example.json（默认配置副本 + 双语说明 + 三平台路径对照）"
}

# ---------- 6b. 安装脚本（任务二） ----------
# 本脚本**不再由安装程序自动执行**：安装程序用 Inno Setup 的 [Icons] 段直接创建
# 开始菜单/桌面快捷方式（工作目录 = 安装目录）。这里保留它给 **zip 免安装版**用户：
# 在解压目录里创建 Start-EchoDesk-v<版本>.lnk，指向 app\EchoDesk.exe，
# 工作目录 = 解压目录，图标用 app\EchoDesk.ico（缺失时退回 exe 自带图标）。
Write-Step "生成 install-complete.bat（给 zip 免安装版手动建快捷方式）"
<#
    这里用**单引号** here-string（@' ... '@），不做任何变量展开。
    为什么（v0.2.1 实测踩到）：批处理里有一行 PowerShell 单行命令，
    里面有 `$s` / `$l` 这些变量；如果写成双引号 here-string，
    PowerShell 会把它们当成本脚本的变量展开成空串 ——
    生成的批处理里变成 ` = New-Object -ComObject WScript.Shell;  = .CreateShortcut(...)`，
    语法直接坏掉，解压后创建快捷方式必然失败，而构建过程一路"成功"。
    版本号因此改成显式占位符 __VERSION__，写出前再替换。
#>
$installBat = @'
@echo off
rem ===========================================================================
rem  EchoDesk v__VERSION__ - portable shortcut helper
rem  给 zip 免安装版用户用：在当前文件夹创建启动快捷方式。
rem  （用安装程序装的不用跑这个 —— 安装程序会直接创建开始菜单/桌面快捷方式。）
rem  可安全重复运行：已存在同名快捷方式时会先询问是否覆盖。
rem
rem  为什么用 chcp 65001 + UTF-8(无 BOM)：本文件含中文提示，cmd 默认代码页
rem  会把它们显示成乱码；同时文件**不能带 BOM**，否则 cmd 会把它当成命令报错。
rem ===========================================================================
setlocal EnableExtensions
chcp 65001 >nul 2>nul

set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

set "APP=%HERE%\app\EchoDesk.exe"
set "ICON=%HERE%\app\EchoDesk.ico"
set "LNK=%HERE%\Start-EchoDesk-v__VERSION__.lnk"

echo.
echo  EchoDesk v__VERSION__ - Local AI Desktop Assistant
echo  ====================================================
echo.

if not exist "%APP%" goto :no_app
if exist "%LNK%" goto :ask_overwrite

:create
set "ICONARG=%ICON%"
if not exist "%ICON%" set "ICONARG="
rem 任务二：图标不存在时退回程序自带图标（IconLocation 留空 = 用 exe 内嵌图标）

powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = New-Object -ComObject WScript.Shell; $l = $s.CreateShortcut('%LNK%'); $l.TargetPath = '%APP%'; $l.WorkingDirectory = '%HERE%'; $l.Description = 'EchoDesk - Local AI Desktop Assistant'; $l.WindowStyle = 1; if ('%ICONARG%' -ne '') { $l.IconLocation = '%ICONARG%,0' }; $l.Save()"
if errorlevel 1 goto :failed

echo  [OK] Shortcut created / 已创建快捷方式：
echo       %LNK%
echo.
echo  Double-click it to start EchoDesk. / 双击它即可启动 EchoDesk。
echo  Working directory / 工作目录：%HERE%
echo  (results\ and ui_state.json are created here, not inside app\)
echo  (results\ 与 ui_state.json 会生成在这里，而不是 app\ 里面)
echo.
echo  Documentation / 文档：docs\  ^|  USAGE.txt
echo.
goto :done

:ask_overwrite
echo  A shortcut with this name already exists / 同名快捷方式已存在：
echo    %LNK%
echo.
set "ANSWER="
set /p "ANSWER=Overwrite it? 覆盖它？[Y/N] "
if /i "%ANSWER%"=="Y" goto :create
if /i "%ANSWER%"=="YES" goto :create
echo  [SKIP] Kept the existing shortcut / 已保留原有快捷方式。
echo.
goto :done

:no_app
echo  [ERROR] Not found / 未找到主程序：
echo    %APP%
echo.
echo  The package may not have been extracted completely. Please extract the
echo  whole archive into one folder and run this script again.
echo  程序文件可能没有完整解压。请把整个压缩包解压到同一个文件夹后重新运行本脚本。
echo.
goto :done

:failed
echo  [ERROR] Failed to create the shortcut / 创建快捷方式失败。
echo  You can create it by hand: right-drag app\EchoDesk.exe into this folder
echo  and choose "Create shortcuts here".
echo  也可以手动创建：右键把 app\EchoDesk.exe 拖到本文件夹，选"在此创建快捷方式"。
echo.

:done
echo  ------------------------------------------------------------
pause
endlocal
exit /b 0
'@
# 占位符替换成真实版本号；替换后断言不再有残留占位符
if ($installBat -notmatch '__VERSION__') { throw "install-complete.bat 模板缺少 __VERSION__ 占位符" }
$installBat = $installBat.Replace('__VERSION__', $Version)
if ($installBat -match '__VERSION__') { throw "install-complete.bat 仍有未替换的占位符" }
# 生成结果必须含 PowerShell 变量（防"$ 被吃掉"这类回归）
foreach ($needle in @('$s = New-Object', '$l = $s.CreateShortcut', '$l.TargetPath', '$l.Save()')) {
    if ($installBat -notlike "*$needle*") {
        throw "install-complete.bat 里的 PowerShell 片段缺少 '$needle'（here-string 是否又写成双引号了？）"
    }
}
# 批处理必须是 UTF-8 **无 BOM**（带 BOM 时 cmd 会把 BOM 当命令，首行报错）。
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$batPath = Join-Path $Out "install-complete.bat"
[System.IO.File]::WriteAllText($batPath, $installBat, $utf8NoBom)
$batBytes = [System.IO.File]::ReadAllBytes($batPath)
if ($batBytes.Length -ge 3 -and $batBytes[0] -eq 0xEF -and $batBytes[1] -eq 0xBB -and $batBytes[2] -eq 0xBF) {
    throw "install-complete.bat 带 BOM，cmd 会在首行报错。"
}
Write-Ok "install-complete.bat（UTF-8 无 BOM，$($batBytes.Length) 字节，PowerShell 片段写入校验通过）"

# ---------- 6c. 文档 ----------
# 文档随发布包一同提供。v0.2.1 起 docs\ 下有按语言分的子目录
# （docs\zh-Hans、docs\en），因此必须 -Recurse。
#
# 这里**复制 docs\ 下的全部文件**，不只是 *.md：修复五新增的 SBOM 是 JSON
# （docs\SBOM.json），早期版本用 `-Filter *.md` 把它漏在仓库里没进包，
# 而 release.yml 与 tools\verify_release.ps1 都断言它必须存在 —— 属实测发现的打包缺陷。
#
# 但**过程文档必须排除**：发布任务书「全局要求五」明确把
# 「研发记录、修复记录、发布记录等过程文档」列为不该有的内容。
# v0.2.1 之前这些记录是"顺便进包"的（原注释还写着"研发记录需纳入发布物"），
# 与任务书冲突，已改为排除 + 断言（见第 9b 步的结构校验）。
$DocsOut = Join-Path $Out "docs"
New-Item -ItemType Directory -Path $DocsOut -Force | Out-Null
$DevRecordRe = '^(v?0\.\d+\.\d+)-.*记录\.md$'
$docCount = 0
$docOther = 0
$docSkipped = @()
$DocsRoot = Join-Path $Root "docs"
foreach ($doc in Get-ChildItem $DocsRoot -File -Recurse -ErrorAction SilentlyContinue) {
    if ($doc.Name -match $DevRecordRe) {
        $docSkipped += $doc.FullName.Substring($DocsRoot.Length).TrimStart('\')
        continue
    }
    $rel = $doc.FullName.Substring($DocsRoot.Length).TrimStart('\')
    $dest = Join-Path $DocsOut (Split-Path $rel -Parent)
    if (-not (Test-Path $dest)) { New-Item -ItemType Directory -Path $dest -Force | Out-Null }
    Copy-Item $doc.FullName $dest -Force
    if ($doc.Extension -eq ".md") { $docCount++ } else { $docOther++; Write-Ok "docs\$rel" }
}
if ($docSkipped.Count -gt 0) {
    Write-Ok "已排除过程文档 $($docSkipped.Count) 份（任务书「全局要求五」）: $($docSkipped -join ', ')"
}
Write-Ok "docs\（$docCount 个 Markdown + $docOther 个其他文件，含按语言的子目录）"

# results\：先建好空目录，让"结果会生成在哪"一目了然
# （后端 automation.results_root 以工作目录/程序根目录为基准解析，指向的正是这里）
$ResultsOut = Join-Path $Out "results"
New-Item -ItemType Directory -Path $ResultsOut -Force | Out-Null
Write-Ok "results\（自动化结果的默认位置）"

# ---------- 6b. 清理运行期残留 ----------
# 交付物应当只含"程序 + 资源"，不含任何"跑过一次"的痕迹：
#   * 空的后端崩溃日志（faulthandler 在启动时就创建文件，正常退出时是 0 字节）
#   * 插件开发日志 / 本地插件清单 / 各类缓存（首次运行时按需生成）
#   * 迁移过来但为空、或本来就该由用户首次运行时生成的注册表文件
# 会话与常用语**故意保留**：-MigrateFrom 的意义就是延续上次的使用体验。
Write-Step "清理运行期残留"
$residual = @(
    (Join-Path $BackendOut "backend_crash.log"),
    (Join-Path $BackendOut "skills_dev.log"),
    (Join-Path $BackendOut "skills_local.json"),
    (Join-Path $BackendOut "market_cache.json"),
    (Join-Path $BackendOut "models_registry.json"),
    (Join-Path $Out "automation_templates.json"),
    (Join-Path $AppOut "automation_templates.json"),
    (Join-Path $Out "frontend_crash.log"),
    (Join-Path $AppOut "frontend_crash.log"),
    (Join-Path $Out "ui_state.json"),
    (Join-Path $AppOut "ui_state.json")
)
$cleaned = 0
foreach ($item in $residual) {
    if (Test-Path $item) {
        # 空文件与目录一律删除；有内容的崩溃日志保留（那是真实的故障线索）
        $isFile = -not (Get-Item $item).PSIsContainer
        if ($isFile -and (Get-Item $item).Length -gt 0 -and $item -like "*crash.log") {
            Write-Warn2 "保留了非空的崩溃日志: $item（请先确认其中的异常是否影响交付）"
            continue
        }
        Remove-Item $item -Recurse -Force -ErrorAction SilentlyContinue
        $cleaned++
    }
}
Write-Ok "已清理运行期残留 $cleaned 项"

# 会话文件：默认重置为空会话（见 -KeepSession 的说明）。
# 为什么必须做：验证阶段会把打包后的后端真正跑起来，它会把测试数据写进 session.json
# （实测残留过 e2e 临时 OFD 文件的绝对路径与正文），这些绝不能随发布物交付。
$SessionFile = Join-Path $BackendOut "session.json"
if ((Test-Path $SessionFile) -and -not $KeepSession) {
    try {
        $old = Get-Content -LiteralPath $SessionFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $fresh = [ordered]@{
            mode             = if ($old.mode) { $old.mode } else { "general" }
            custom_prompt    = if ($null -ne $old.custom_prompt) { $old.custom_prompt } else { "" }
            # 任务层（/task）不随空会话模板交付：它是"上一轮对话的选择"，
            # 新用户首次启动应当是纯模式提示词。
            task_prompt      = ""
            summary          = ""
            model_path       = if ($old.model_path) { $old.model_path } else { "" }
            file_name        = ""
            file_content     = ""
            file_summary     = ""
            file_summary_meta = @{}
            history          = @()
        }
        ($fresh | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath $SessionFile -Encoding UTF8
        Write-Ok "session.json 已重置为空会话（保留 mode/model_path/custom_prompt，清空任务层；-KeepSession 可跳过）"
    }
    catch {
        Remove-Item -LiteralPath $SessionFile -Force -ErrorAction SilentlyContinue
        Write-Ok "session.json 无法解析，已删除（后端首次保存时会重建）"
    }
}
# ---------- 7. 写入版本资源与图标 ----------
if (Test-Path $Rcedit) {
    Write-Step "写入前端版本资源与图标（rcedit）"
    # 版本号**必须传进去**（v0.2.2 裁定① 之后 set_metadata.bat 不再有写死的默认值）：
    # `$Version` 是上面从 backend\version.py 派生的三段式，这里补成版本资源要的四段式。
    # 【实测踩到】裁定① 里我在 set_metadata.bat 加了"版本必填"，却**漏改了这个调用点**，
    # 于是重打包在"元数据写入"这一步直接失败（退出码 2）——单看两个文件各自都对，
    # 错在**接线**上。验证一个"参数化"的改动，必须跑到调用方为止。
    Invoke-Native "cmd" @("/c", "`"$Root\tools\set_metadata.bat`" `"$AppOut`" `"$Version.0`"") "元数据写入"
    Write-Ok "app\EchoDesk.exe 元数据与图标已写入（后端由 spec 的 version/icon 写入）"
}

# ---------- 8. 可选代码签名 ----------
if ($Sign) {
    Write-Step "代码签名"
    $signtoolCmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    $signtool = if ($signtoolCmd) { $signtoolCmd.Source } else { $null }
    if (-not $signtool) {
        Write-Warn2 "未找到 signtool.exe（需安装 Windows SDK），跳过签名"
    }
    else {
        $signArgs = @("sign", "/fd", "SHA256", "/tr", $TimestampUrl, "/td", "SHA256")
        if ($CertThumbprint) { $signArgs += @("/sha1", $CertThumbprint) } else { $signArgs += @("/a") }
        foreach ($exe in @((Join-Path $AppOut "EchoDesk.exe"), (Join-Path $BackendOut "EchoDeskBackend.exe"))) {
            if (Test-Path $exe) {
                try {
                    Invoke-Native $signtool ($signArgs + @($exe)) "签名 $(Split-Path -Leaf $exe)"
                    Write-Ok "已签名 $(Split-Path -Leaf $exe)"
                }
                catch {
                    Write-Warn2 "签名失败: $(Split-Path -Leaf $exe)"
                }
            }
        }
    }
}

# ---------- 9. 启动入口：由 install-complete.bat 在解压/部署后创建 ----------
<#
    这里**故意不在发布目录里预先生成 .lnk**（v0.2.1 发布复核时的修正）。
    原因（两条，都是硬性的）：
      1) Windows 快捷方式是**绝对路径**。在构建机上生成的 .lnk 里记的是
         `C:\Users\<维护者>\Desktop\...\publish\EchoDesk-v0.2.2\app\EchoDesk.exe`，
         随发布物分发出去既**泄露构建机的个人绝对路径**（发布任务书「全局要求五」
         把"个人绝对路径"列为绝对禁止项），在用户机器上又必然是一个死链接。
         实测：这一项连 `check_feedback.ps1` 都没拦到 —— 它扫的是文本文件，
         而 .lnk 是二进制、还在 .gitignore 覆盖的 publish\ 里。
      2) 发布任务书「任务二」本来就把"创建 Start-EchoDesk-v<版本>.lnk"
         定为 `install-complete.bat` 的职责；安装程序则用 Inno Setup 的
         [Icons] 段在目标机器上创建开始菜单/桌面快捷方式，
         用户拿到的都是**指向自己机器上真实路径**的快捷方式。

    安装程序用户：快捷方式由安装程序创建，不需要这一步。
    zip 免安装解压版用户：需要手动运行一次 install-complete.bat（或直接启动
    app\EchoDesk.exe，程序首次运行会询问是否创建快捷方式）—— USAGE.txt 与
    发布说明里都写明了这一步。
#>
Write-Step "启动入口（不在发布物里预置 .lnk，改由 install-complete.bat 创建）"
$preexistingLinks = @(Get-ChildItem $Out -Recurse -File -Filter *.lnk -ErrorAction SilentlyContinue)
if ($preexistingLinks.Count -gt 0) {
    throw ("发布物里出现了预置快捷方式（会把构建机的绝对路径带出去，任务书禁止个人绝对路径）: " +
        (($preexistingLinks | ForEach-Object { $_.FullName }) -join ', '))
}
Write-Ok "发布目录内无 .lnk（快捷方式由 install-complete.bat 在目标机器上创建）"

# ---------- 9b. 结构校验 ----------
# 修复七的验收条件之一："外层除快捷方式、结果目录、用户数据外，不出现程序文件"。
# 这里直接断言，避免以后有人不小心把 dotnet publish 的输出目录改回外层。
#
# v0.2.1 发布任务书又加了三条硬约束，一并在这里拦住（构建期就失败，
# 比打包完再被 make_installer 的可行性检查挡下来更早、更好定位）：
#   * 全局要求一：发布物内文件名/目录名全英文；
#   * 全局要求五：不得有过程文档（研发/修复/发布记录）与运行期残留；
#   * 全局要求四：根目录必备文件齐全。
Write-Step "校验发布目录结构"
$strayRootExe = @(Get-ChildItem $Out -File -Include *.exe, *.dll, *.pdb)
$strayDeep = @(Get-ChildItem $Out -Recurse -File -Include *.exe, *.dll |
    Where-Object { $_.FullName -notlike "$AppOut*" })
if ($strayRootExe.Count -gt 0 -or $strayDeep.Count -gt 0) {
    $names = ($strayRootExe + $strayDeep | ForEach-Object { $_.FullName }) -join "; "
    throw "外层出现了程序文件（修复七要求程序本体全部在 app\ 内）: $names"
}
if (-not $SkipFrontend -and -not (Test-Path (Join-Path $AppOut "EchoDesk.exe"))) {
    throw "未找到 app\EchoDesk.exe"
}
Write-Ok "外层无 exe/dll · 程序本体位于 app\"

# 全局要求一：全英文命名（中/日/韩字符一律不允许出现在文件或目录名里）
$cjkRe = [regex]'[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]'
$cnNames = @(Get-ChildItem $Out -Recurse -Force -ErrorAction SilentlyContinue |
    Where-Object { $cjkRe.IsMatch($_.Name) } |
    ForEach-Object { $_.FullName.Substring($Out.Length).TrimStart('\') })
if ($cnNames.Count -gt 0) {
    throw "发布物内出现非 ASCII 文件名/目录名（任务书「全局要求一」要求全英文）: $($cnNames -join ', ')"
}
Write-Ok "全英文命名（文件与目录名均无中文/日文/韩文）"

# 全局要求五：过程文档与运行期残留不得进包
$devRecords = @(Get-ChildItem $Out -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^(v?0\.\d+\.\d+)-.*记录\.md$' })
if ($devRecords.Count -gt 0) {
    throw "发布物内出现过程文档（任务书「全局要求五」）: $(($devRecords | ForEach-Object { $_.Name }) -join ', ')"
}
$runtimeLeftovers = @(Get-ChildItem $Out -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -in @('session.json', 'ui_state.json', 'snippets.json', 'browser_state.json',
            'backend_crash.log', 'frontend_crash.log', 'automation_log.jsonl', 'skills_local.json',
            'summary_cache.json', 'market_cache.json', 'skills_dev.log', 'automation_templates.json') } |
    Where-Object {
        # session.json 是例外：交付物里带一份"空会话"是设计（后端首次启动会用它），
        # 但内容必须真的空 —— 不能带任何会话历史或文件正文（e2e 跑过之后最容易留下）。
        if ($_.Name -ne 'session.json') { return $true }
        $txt = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
        if (-not $txt) { return $false }
        $historyEmpty = [regex]::IsMatch($txt, '"history"\s*:\s*\[\s*\]')
        $fileEmpty = [regex]::IsMatch($txt, '"file_content"\s*:\s*"\s*"')
        return (-not ($historyEmpty -and $fileEmpty))
    })
if ($runtimeLeftovers.Count -gt 0) {
    throw "发布物内出现运行期残留（任务书「全局要求五」）: $(($runtimeLeftovers | ForEach-Object { $_.Name }) -join ', ')"
}
Write-Ok "无过程文档 · 无运行期残留（会话为空会话）"

# 全局要求四：根目录必备文件齐全
$mustHave = @(
    "README.md", "README.zh-Hans.md",
    "LICENSE", "LICENSE.zh-Hans.md",
    "NOTICE", "NOTICE.zh-Hans.md",
    "SECURITY.md", "SECURITY.zh-Hans.md",
    "CHANGELOG.md", "CHANGELOG.zh-Hans.md", "USAGE.txt",
    "CONTRIBUTING.md", "CONTRIBUTING.zh-Hans.md",
    "config.example.json", "install-complete.bat",
    "docs\PRIVACY.md", "docs\PRIVACY.zh-Hans.md",
    "docs\EULA.md", "docs\EULA.zh-Hans.md",
    "docs\ENCRYPTED_DOCUMENTS.md", "docs\ENCRYPTED_DOCUMENTS.zh-Hans.md",
    "docs\SECURITY.md",
    "docs\THIRD_PARTY_LICENSES.md", "docs\THIRD_PARTY_LICENSES.zh-Hans.md",
    "docs\SBOM.json", "docs\DEVELOPER.md",
    "docs\en\DEVELOPER.md", "docs\zh-Hans\DEVELOPER.md",
    "docs\en\COMPLIANCE.md", "docs\zh-Hans\COMPLIANCE.md",
    "docs\en\README.md", "docs\en\USER_GUIDE.md", "docs\en\FAQ.md", "docs\en\AUTOMATION_GUIDE.md",
    "docs\zh-Hans\README.md", "docs\zh-Hans\USER_GUIDE.md", "docs\zh-Hans\FAQ.md",
    "docs\zh-Hans\AUTOMATION_GUIDE.md",
    "lang", "examples\skills", "results"
)
$missing = @($mustHave | Where-Object { -not (Test-Path (Join-Path $Out $_)) })
if ($missing.Count -gt 0) {
    throw "发布物缺少任务书「全局要求四」要求的必备文件: $($missing -join ', ')"
}
Write-Ok "必备文件齐全（$($mustHave.Count) 项，含双语合规文档与 docs\ 下全部文档）"

# ---------- 10. 汇总 ----------
Write-Step "发布完成"
$total = (Get-ChildItem $Out -Recurse -File | Measure-Object Length -Sum).Sum
$topFiles = (Get-ChildItem $Out -File).Count
$appFiles = (Get-ChildItem $AppOut -File).Count
$backFiles = (Get-ChildItem $BackendOut -Recurse -File).Count
$modelInfo = @(Get-ChildItem $ModelDir -Filter *.gguf -File -ErrorAction SilentlyContinue)
$modelText = if ($modelInfo.Count -gt 0) {
    "含模型: " + (($modelInfo | ForEach-Object { $_.Name }) -join ", ")
}
else { "不含模型（首次启动会询问下载）" }
Write-Ok "发布目录: $Out"
Write-Ok "外层文件 $topFiles 个 · app\ 内 $appFiles 个 · backend 内 $backFiles 个 · 合计 $([math]::Round($total / 1MB, 1)) MB"
Write-Ok $modelText
Write-Ok "入口: install-complete.bat 在目标机器上创建 $ShortcutName"

# ---------- 11. 安装程序（发布任务书「任务四」） ----------
# 任务书要求构建脚本用一个开关把"打包"这一段收口，八步依次为：
#   1. 按「全局要求一」重命名中文文件/目录为英文；      -> 本脚本第 6/6c 步已按英文名生成，
#                                                        第 9b 步断言无中文名（有问题就失败）
#   2. 按「全局要求二」补齐双语合规文档；                -> 第 6 步成对复制 + 第 9b 步断言
#   3. 按「全局要求三」替换反馈渠道为 GitHub Issues/Security Advisory；
#                                                        -> tools\check_feedback.ps1 门禁
#   4. 按「全局要求五」清理调试内容与运行期数据；        -> 第 6b 步清理 + 第 9b 步断言
#   5. 编译安装程序（一份，不含模型）；6. 写版本资源；7. 计算 SHA256SUMS.txt
#                                                        -> 全部由 tools\make_installer.ps1 承担
#   注意第 7 步已经不是"外挂 rcedit"：版本资源由 Inno Setup 的 [Setup] VersionInfo*
#   指令在编译时写入。**绝不能对最终 Setup.exe 跑 rcedit** —— 会重建 PE 映像并丢掉
#   Inno 追加在映像之后的安装数据（实测：2 MB 的包被削到 893,952 字节且运行即失败）。
# 把 5–8 放在独立脚本里，是为了让"只改安装脚本、只想重打安装程序"时能单独跑
# （make_installer.ps1 自己做一遍可行性检查，不依赖本脚本已经跑过第 9b 步）。
if ($MakeSfx -and -not $MakeInstaller) {
    Write-Warn2 "-MakeSfx 已更名为 -MakeInstaller（旧名保留为别名）"
    Write-Warn2 "  原因：7-Zip 自解压包方案已废弃，改用 Inno Setup 编译安装程序。"
    $MakeInstaller = $true
}
if ($MakeInstaller) {
    Write-Step "编译安装程序（-MakeInstaller，Inno Setup）"

    # 第 3 步：反馈渠道门禁。放在打包前，因为它检查的是**源码与文档**，
    # 一旦不合格，打出来的包也是错的。
    #
    # 这里直接以脚本方式调用（`& script`）而不是再起一个 powershell.exe：
    #   * 少一层进程，出错时异常直接在本脚本里抛出，位置与调用栈都是真的；
    #   * 不必把路径塞进命令行（本仓库路径含中文与空格，多一层转义就多一处坑）；
    #   * 两个脚本都用 throw 报错，$ErrorActionPreference=Stop 下会自然终止构建。
    $feedbackCheck = Join-Path $PSScriptRoot "check_feedback.ps1"
    if (Test-Path $feedbackCheck) {
        & $feedbackCheck
        Write-Ok "反馈渠道检查通过（GitHub Issues / Discussions / Security Advisory，无个人邮箱与实体地址）"
    }
    else {
        Write-Warn2 "未找到 tools\check_feedback.ps1，跳过反馈渠道检查"
    }

    # 第 5–8 步
    $makeInstallerScript = Join-Path $PSScriptRoot "make_installer.ps1"
    if (-not (Test-Path $makeInstallerScript)) { throw "未找到 $makeInstallerScript" }
    # 签名参数透传：安装程序在第 7 步（本脚本的 -Sign）之后才生成，
    # 所以安装程序必须在这里自己签，否则交付物是未签名的。
    #
    # 必须用**哈希表** splat，不能用数组 splat —— 数组 splat 是按位置传参，
    # `@("-Version", $Version)` 会把 "-Version" 当成第一个位置参数的值
    # （实测踩到：make_installer.ps1 收到 ReleaseDir="0.2.1" 并报"发布目录不存在"）。
    $installerArgs = @{ Version = $Version }
    if ($Sign) {
        $installerArgs["Sign"] = $true
        if ($CertThumbprint) { $installerArgs["CertThumbprint"] = $CertThumbprint }
        $installerArgs["TimestampUrl"] = $TimestampUrl
    }
    $installerStarted = Get-Date
    & $makeInstallerScript @installerArgs

    # 后置断言：这一步必须真的产出交付物。
    # 之前只打印"未生成: xxx"就继续，导致"打包没跑成"仍以成功结束（实测踩到）。
    #
    # 两个断言缺一不可：只看"文件存在"是不够的 —— 旧的产物（例如被废弃的
    # 7-Zip 自解压包）留在 publish\ 里，就能让存在性断言误判成成功（实测踩到）。
    # 所以还要求文件是**本次构建重新生成**的（时间戳晚于打包开始时刻）。
    Write-Step "-MakeInstaller 完成"
    # 交付物只有一份 Setup（不含模型），见 tools\make_installer.ps1 头部说明
    $deliverables = @(
        (Join-Path $Root "publish\EchoDesk-v$Version-Setup.exe"),
        (Join-Path $Root "publish\SHA256SUMS.txt")
    )
    $missingDeliverables = @($deliverables | Where-Object { -not (Test-Path $_) })
    foreach ($f in $deliverables) {
        if (Test-Path $f) {
            Write-Ok "$(Split-Path -Leaf $f)  $([math]::Round((Get-Item $f).Length / 1MB, 2)) MB"
        }
    }
    if ($missingDeliverables.Count -gt 0) {
        throw "任务书要求的交付物未生成: $(($missingDeliverables | ForEach-Object { Split-Path -Leaf $_ }) -join ', ')"
    }
    $stale = @($deliverables | Where-Object { (Get-Item $_).LastWriteTime -lt $installerStarted })
    if ($stale.Count -gt 0) {
        throw ("交付物不是本次构建生成的（时间戳早于本次打包开始）：" +
            "$(($stale | ForEach-Object { Split-Path -Leaf $_ }) -join ', ')。" +
            "很可能是打包步骤失败却留下了旧文件 —— 请查看上方 make_installer.ps1 的报错。")
    }
}
else {
    Write-Host ""
    Write-Warn2 "未生成安装程序。要产出交付物（EchoDesk-v$Version-Setup.exe + SHA256SUMS.txt），加 -MakeInstaller："
    Write-Warn2 "  & tools\build_release.ps1 -MakeInstaller   # 安装程序不含模型，故不需要 -IncludeModels"
}
