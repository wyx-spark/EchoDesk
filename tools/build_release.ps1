<#
.SYNOPSIS
    EchoDesk 发布构建脚本：产出「单文件夹、多文件、单入口」的发布物。

.DESCRIPTION
    流程（与任务书「任务三」对应）：
      1. 清空输出目录（可用 -SkipClean 保留，便于只重跑资源收集）；
      2. 构建后端：PyInstaller onedir → backend\EchoDeskBackend.exe + backend\_internal\；
      3. 构建前端：dotnet publish 多文件（非单文件）→ 依赖 DLL 全部平铺在发布根目录；
      4. 收集图标、配置、模型与用户数据（含 Hugging Face 本地缓存里的模型）；
      5. 生成使用说明；
      6. rcedit 写入前端版本资源与图标（后端由 spec 的 version/icon 写入，原因见 spec 注释）；
      7. 可选代码签名（-Sign）；
      8. 创建唯一入口快捷方式「EchoDesk 启动.lnk」（WScript.Shell COM）。

    发布物结构（用户只需双击文件夹内的快捷方式）：
      EchoDesk-v0.2.1\
      ├─ EchoDesk 启动.lnk          唯一入口（TargetPath=EchoDesk.exe，工作目录=本文件夹）
      ├─ EchoDesk.exe               前端主程序（多文件发布）
      ├─ EchoDesk.ico               图标（快捷方式与界面使用）
      ├─ ui_state.json              界面状态（首次运行时生成）
      ├─ snippets.json              常用语（首次运行时生成）
      ├─ *.dll / *.json             前端依赖（同目录，不再压进单个 exe，启动更快）
      ├─ backend\                   后端（PyInstaller onedir）
      │  ├─ EchoDeskBackend.exe
      │  ├─ _internal\              后端依赖子文件夹
      │  ├─ config.json             用户可编辑配置
      │  ├─ session.json            会话（运行时生成）
      │  ├─ models\                 本地模型目录（.gguf）
      │  └─ backend_crash.log       崩溃日志（异常时生成）
      └─ 使用说明.txt

.PARAMETER Version
    版本号（决定输出目录名 publish\EchoDesk-v<Version>），默认 0.2.1。

.PARAMETER IncludeModels
    把本地已有的 .gguf 模型复制进发布物（离线安装包用；体积很大，默认不复制）。
    模型来源依次为 backend\models、models\、deploy\models、-MigrateFrom 目录，
    以及 Hugging Face 本地缓存快照（下载过一次后即可离线打包）。

.PARAMETER MigrateFrom
    从既有发布目录迁移用户数据（snippets.json、session.json、models\*.gguf）。

.PARAMETER SkipBackend / SkipFrontend / SkipClean
    跳过对应步骤；-SkipClean 用于在已有产物上只重跑资源收集与元数据。

.PARAMETER Sign
    构建完成后用 signtool 签名（需 -CertThumbprint 或已配置默认证书）。

.EXAMPLE
    & tools\build_release.ps1 -IncludeModels -MigrateFrom publish\EchoDesk-v0.1.0
#>
[CmdletBinding()]
param(
    [string]$Version = "0.2.1",
    [switch]$IncludeModels,
    [string]$MigrateFrom = "",
    [switch]$SkipBackend,
    [switch]$SkipFrontend,
    [switch]$SkipClean,
    [switch]$NoShortcut,
    [switch]$KeepAllCultures,
    [switch]$Sign,
    [string]$CertThumbprint = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Out = Join-Path $Root "publish\EchoDesk-v$Version"
$BackendOut = Join-Path $Out "backend"

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
if ($SkipClean) {
    Write-Warn2 "已指定 -SkipClean，保留输出目录现有内容（用于只重跑资源收集/元数据）"
}
else {
    if (Test-Path $Out) { Remove-Item $Out -Recurse -Force }
    Write-Ok "已清空 $Out"
}
New-Item -ItemType Directory -Path $BackendOut -Force | Out-Null

# ---------- 2. 构建后端（PyInstaller onedir） ----------
if (-not $SkipBackend) {
    Write-Step "构建后端（PyInstaller onedir，耗时较长）"
    $stage = Join-Path $env:TEMP "echodesk_backend_stage"
    $work = Join-Path $env:TEMP "echodesk_backend_work"
    Remove-Item $stage, $work -Recurse -Force -ErrorAction SilentlyContinue

    Push-Location $Root
    try {
        Invoke-Native "pyinstaller" @("--noconfirm", "--distpath", $stage, "--workpath", $work, "backend\EchoBackend.spec") "后端构建"
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
    Write-Ok "后端已就位: backend\EchoDeskBackend.exe（依赖在 backend\_internal\，$size MB）"
}

# ---------- 3. 构建前端（多文件发布） ----------
if (-not $SkipFrontend) {
    Write-Step "构建前端（dotnet publish 多文件模式）"
    $publishArgs = @(
        "publish", (Join-Path $Root "frontend\EchoDesk.csproj"),
        "-c", "Release", "-r", "win-x64", "--self-contained", "true",
        "-p:PublishSingleFile=false",
        "-p:DebugType=none", "-p:DebugSymbols=false",
        "-p:NuGetAudit=false",
        "-o", $Out
    )
    Invoke-Native "dotnet" $publishArgs "前端构建"
    $dllCount = (Get-ChildItem $Out -Filter *.dll -File).Count
    Write-Ok "前端已就位: EchoDesk.exe（同目录 $dllCount 个依赖 DLL）"
}

# ---------- 3b. 清理发布根目录：只保留 EchoDesk.exe 这一个可执行文件 ----------
Write-Step "清理发布根目录"
$stray = Get-ChildItem $Out -Filter *.exe -File | Where-Object { $_.Name -ne "EchoDesk.exe" }
foreach ($f in $stray) {
    Write-Ok "移除多余可执行文件: $($f.Name)"
    Remove-Item $f.FullName -Force
}
Get-ChildItem $Out -Filter *.pdb -File -ErrorAction SilentlyContinue | Remove-Item -Force
if (-not $KeepAllCultures) {
    # 多语言资源只保留中文，减少目录噪音与体积（其余文化目录与运行无关）
    $keep = @("zh-Hans", "zh-Hant")
    foreach ($d in Get-ChildItem $Out -Directory | Where-Object { $_.Name -match '^[a-z]{2}(-[A-Za-z]+)?$' }) {
        if ($keep -notcontains $d.Name) { Remove-Item $d.FullName -Recurse -Force }
    }
}
Write-Ok "根目录可执行文件仅剩 EchoDesk.exe"

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

# 图标：快捷方式与界面使用
Copy-Item $Icon (Join-Path $Out "EchoDesk.ico") -Force
Write-Ok "EchoDesk.ico"

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
    if ($copied -eq 0) { Write-Warn2 "未找到可复制的 .gguf 模型，首次启动时将询问是否下载" }
}
else {
    Write-Warn2 "未复制模型（未指定 -IncludeModels）；首次启动会询问是否下载模型"
}

# ---------- 5. 迁移用户数据 ----------
if ($MigrateFrom) {
    Write-Step "迁移用户数据"
    $snip = Join-Path $MigrateFrom "snippets.json"
    if (Test-Path $snip) {
        Copy-Item $snip (Join-Path $Out "snippets.json") -Force
        Write-Ok "常用语 snippets.json"
    }
    foreach ($sess in @((Join-Path $MigrateFrom "session.json"), (Join-Path $MigrateFrom "backend\session.json"))) {
        if (Test-Path $sess) {
            Copy-Item $sess (Join-Path $BackendOut "session.json") -Force
            Write-Ok "会话 session.json"
            break
        }
    }
}

# ---------- 6. 说明文件 ----------
Write-Step "生成发布说明"
$readme = @"
EchoDesk v$Version — 本地 AI 桌面助手
Powered by Void Entropia Studio

【怎么启动】
  双击本文件夹内的「EchoDesk 启动.lnk」即可。
  无需分辨哪个可执行文件是入口：EchoDesk.exe 是界面主程序，
  backend\EchoDeskBackend.exe 由界面自动拉起，请不要单独运行。

【文件夹里都有什么】
  EchoDesk 启动.lnk   唯一启动入口（工作目录即本文件夹）
  EchoDesk.exe        界面主程序（多文件发布，依赖 DLL 同目录，启动更快）
  EchoDesk.ico        程序图标
  backend\            后端：模型推理、文件解析、对话管理
    config.json       配置（模型、上下文长度、分段摘要、自动化、技能市场、模型中心等）
    models\           本地模型目录（.gguf 放这里）
    models_registry.json  模型库记录（由「设置 → 模型」下载的模型自动登记；首次使用时生成）
    session.json      上次会话（自动保存/恢复）
    批量处理结果\      自动化结果（保存位置留空时的默认位置，按 日期\任务名\ 分层的纯文本结果）
  docs\               文档（用户手册、FAQ、隐私政策、许可协议、变更日志、本版研发记录与本地化文档）
  docs\zh-Hans\       中文文档（含 AUTOMATION_GUIDE.md 自动化填写指南）
  docs\en\            英文文档
  lang\               后端语言包（系统消息、错误提示、指令帮助）
  lang\ui\            界面语言包（按钮/标签/对话框文案；两个目录都可自行添加新语言后重启生效）
  backend\skills_dev\ 示例与开发中的插件（放进去即被识别，无需登记注册表）
  examples\skills\    示例插件源码（Hello World 与 OFD 读取）
  ui_state.json       界面状态：窗口位置与大小、字号、主题
  snippets.json       常用语模板
  使用说明.txt        本文件

【首次启动】
  * 若 models\ 里没有模型，程序会询问是否下载（约 508MB）；
    也可以自己把 .gguf 文件放进 backend\models\ 后重启。
  * 会询问是否在本文件夹创建启动快捷方式。

【常用操作】
  /help            查看全部指令
  /file <路径>     上传文件（长文档自动分段摘要，不截断丢信息）
  /summarize       对当前长文档手动触发全文摘要
  /model           查看/切换本地模型
  /status          查看模型、模式、上下文占用与 KV Cache 估算
  Ctrl+Enter 发送 · Esc 停止 · Ctrl+L 清空 · Ctrl+O 上传 · Ctrl+Shift+C 复制全部对话
  每条消息右下角可「复制 / Markdown / 代码」

【卸载】
  本程序为绿色便携版：直接删除整个文件夹即可，不写注册表。

构建时间：$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
"@
Set-Content -Path (Join-Path $Out "使用说明.txt") -Value $readme -Encoding UTF8
Write-Ok "使用说明.txt"

# 文档随发布包一同提供（任务书要求：研发记录需纳入发布物）。
# v0.2.1 起 docs\ 下有按语言分的子目录（docs\zh-Hans、docs\en），
# 因此这里必须 -Recurse，否则 AUTOMATION_GUIDE.md 等本地化文档不会进包。
$DocsOut = Join-Path $Out "docs"
New-Item -ItemType Directory -Path $DocsOut -Force | Out-Null
$docCount = 0
$DocsRoot = Join-Path $Root "docs"
foreach ($doc in Get-ChildItem $DocsRoot -Filter *.md -File -Recurse -ErrorAction SilentlyContinue) {
    $rel = $doc.FullName.Substring($DocsRoot.Length).TrimStart('\')
    $dest = Join-Path $DocsOut (Split-Path $rel -Parent)
    if (-not (Test-Path $dest)) { New-Item -ItemType Directory -Path $dest -Force | Out-Null }
    Copy-Item $doc.FullName $dest -Force
    $docCount++
}
Write-Ok "docs\（$docCount 个文档，含按语言的子目录）"

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
    (Join-Path $Out "frontend_crash.log"),
    (Join-Path $Out "批量处理结果")
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
# ---------- 7. 写入版本资源与图标 ----------
if (Test-Path $Rcedit) {
    Write-Step "写入前端版本资源与图标（rcedit）"
    Invoke-Native "cmd" @("/c", "`"$Root\tools\set_metadata.bat`" `"$Out`"") "元数据写入"
    Write-Ok "EchoDesk.exe 元数据与图标已写入（后端由 spec 的 version/icon 写入）"
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
        foreach ($exe in @((Join-Path $Out "EchoDesk.exe"), (Join-Path $BackendOut "EchoDeskBackend.exe"))) {
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

# ---------- 9. 创建唯一入口快捷方式 ----------
if (-not $NoShortcut) {
    Write-Step "创建启动快捷方式"
    $target = Join-Path $Out "EchoDesk.exe"
    $link = Join-Path $Out "EchoDesk 启动.lnk"
    if (-not (Test-Path $target)) { Write-Warn2 "未找到主程序，跳过快捷方式" }
    else {
        $shell = New-Object -ComObject WScript.Shell
        $sc = $shell.CreateShortcut($link)
        $sc.TargetPath = $target
        $sc.WorkingDirectory = $Out
        $sc.Description = "EchoDesk - Local AI Desktop Assistant"
        $sc.WindowStyle = 1
        if (Test-Path $Icon) { $sc.IconLocation = "$Icon,0" }
        $sc.Save()
        Write-Ok "EchoDesk 启动.lnk → EchoDesk.exe（工作目录=发布根目录）"
    }
}

# ---------- 10. 汇总 ----------
Write-Step "发布完成"
$total = (Get-ChildItem $Out -Recurse -File | Measure-Object Length -Sum).Sum
$topFiles = (Get-ChildItem $Out -File).Count
$backFiles = (Get-ChildItem $BackendOut -Recurse -File).Count
$modelInfo = @(Get-ChildItem $ModelDir -Filter *.gguf -File -ErrorAction SilentlyContinue)
$modelText = if ($modelInfo.Count -gt 0) {
    "含模型: " + (($modelInfo | ForEach-Object { $_.Name }) -join ", ")
}
else { "不含模型（首次启动会询问下载）" }
Write-Ok "发布目录: $Out"
Write-Ok "根目录文件 $topFiles 个 · backend 内文件 $backFiles 个 · 合计 $([math]::Round($total / 1MB, 1)) MB"
Write-Ok $modelText
Write-Ok "入口: EchoDesk 启动.lnk"
