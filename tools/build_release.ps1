<#
.SYNOPSIS
    EchoDesk 发布构建脚本：产出「便携版三层结构」的发布物。

.DESCRIPTION
    流程（与任务书「任务三」对应）：
      1. 清空输出目录（可用 -SkipClean 保留，便于只重跑资源收集）；
      2. 构建后端：PyInstaller onedir → app\backend\EchoDeskBackend.exe + app\backend\_internal\；
      3. 构建前端：dotnet publish 多文件（非单文件）→ app\ 下平铺依赖 DLL；
      4. 收集图标、配置、语言包、模型与文档；
      5. 生成使用说明；
      6. rcedit 写入前端版本资源与图标（后端由 spec 的 version/icon 写入，原因见 spec 注释）；
      7. 可选代码签名（-Sign）；
      8. 创建外层快捷方式 EchoDesk-v<版本>.lnk（WScript.Shell COM）。

    发布物结构（修复七：应用本体隔离到 app\，外层只留快捷方式、用户数据与文档）：
      publish\EchoDesk-v0.2.1\
      ├─ EchoDesk-v0.2.1.lnk     唯一入口（TargetPath=app\EchoDesk.exe，工作目录=本层）
      ├─ 批量处理结果\            自动化结果（相对工作目录生成，按 日期\任务名\ 分层）
      ├─ ui_state.json           界面状态（首次运行时生成）
      ├─ snippets.json           常用语（首次运行时生成）
      ├─ 使用说明.txt
      ├─ docs\                   文档（含 zh-Hans\ 与 en\）
      ├─ LICENSE / NOTICE / 合规文档
      ├─ lang\                   后端语言包（可替换/新增，无需重新打包）
      ├─ examples\skills\        示例插件源码
      └─ app\                    程序本体（外层不出现任何 exe/dll）
         ├─ EchoDesk.exe
         ├─ EchoDesk.ico
         ├─ *.dll / *.json      前端依赖
         ├─ lang\ui\            界面语言包
         └─ backend\            后端（PyInstaller onedir）
            ├─ EchoDeskBackend.exe
            ├─ _internal\
            ├─ config.json
            ├─ models\
            └─ skills_dev\       随包示例插件

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

.EXAMPLE
    & tools\build_release.ps1 -IncludeModels -MigrateFrom publish\EchoDesk-v0.2.0
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
    [string]$ReuseBackendFrom = "",
    [switch]$KeepSession,
    [switch]$Sign,
    [string]$CertThumbprint = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Out = Join-Path $Root "publish\EchoDesk-v$Version"
# 修复七：程序本体统一放进 app\，外层只留快捷方式/用户数据/文档
$AppOut = Join-Path $Out "app"
$BackendOut = Join-Path $AppOut "backend"
$ShortcutName = "EchoDesk-v$Version.lnk"

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
    if ($copied -eq 0) { Write-Warn2 "未找到可复制的 .gguf 模型，首次启动时将询问是否下载" }
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

# ---------- 6. 说明文件 ----------
Write-Step "生成发布说明"
$readme = @"
EchoDesk v$Version — 本地 AI 桌面助手
Powered by Void Entropia Studio

【怎么启动】
  双击本文件夹内的「$ShortcutName」即可。
  不用去 app\ 里找可执行文件：app\EchoDesk.exe 是界面主程序，
  app\backend\EchoDeskBackend.exe 由界面自动拉起，请不要单独运行。

【文件夹里都有什么】（修复七：程序收进 app\，外层只放你能改、要备份的东西）
  $ShortcutName   唯一启动入口（工作目录就是本文件夹）
  使用说明.txt      本文件
  批量处理结果\     自动化结果（按 日期\任务名\ 分层的纯文本结果 + 下载的原始文件）
  ui_state.json     界面状态：窗口位置与大小、字号、主题、语言
  snippets.json     常用语模板
  docs\             文档（用户手册、FAQ、自动化指南、隐私政策、许可协议、变更日志、研发记录）
  docs\zh-Hans\     中文文档
  docs\en\          英文文档
  LICENSE / NOTICE  许可证与署名
  lang\             后端语言包（系统消息、错误提示、指令帮助）
  examples\skills\  示例插件源码（Hello World 与 OFD 读取）
  app\              程序本体（请勿随意改动）
    EchoDesk.exe        界面主程序
    EchoDesk.ico        程序图标
    lang\ui\            界面语言包（按钮/标签/对话框文案）
    backend\            后端：模型推理、文件解析、对话管理
      config.json       配置（模型、上下文长度、分段摘要、自动化、技能市场、模型中心等）
      models\           本地模型目录（.gguf 放这里）
      session.json      上次会话（自动保存/恢复）
      skills_dev\       随包示例插件
      models_registry.json  模型库记录（首次使用时生成）

【语言包怎么加新语言】
  界面文案：在 lang\ui\ 下放一份 <语言标签>.json（如 ja.json），
            可参照 app\lang\ui\zh-Hans.json 的结构；重启后在设置里即可选择。
  系统消息：在 lang\ 下放一份 <语言标签>.json，参照已有的 zh-Hans.json。
  两个目录都可以被你自己替换，升级程序不会覆盖外层 lang\。

【首次启动】
  * 若 app\backend\models\ 里没有模型，程序会询问是否下载（约 508MB）；
    也可以自己把 .gguf 文件放进 app\backend\models\ 后重启。
  * 会询问是否在本文件夹创建启动快捷方式（已附带一个，可直接用）。

【常用操作】
  /help            查看全部指令
  /file <路径>     上传文件（长文档自动分段摘要，不截断丢信息）
  /summarize       对当前长文档手动触发全文摘要
  /model           查看/切换本地模型
  /status          查看模型能力、模式、上下文占用与 KV Cache 估算
  /darkmode auto   主题跟随系统（也可 on/off 固定深浅色）
  Ctrl+Enter 发送 · Esc 停止 · Ctrl+L 清空 · Ctrl+O 上传 · Ctrl+T 切换主题
  Ctrl+滚轮 调字号 · Ctrl+Shift+C 复制全部对话
  每条消息右下角可「复制 / Markdown / 代码」

【移动文件夹之后】
  快捷方式里记的是绝对路径；把整个文件夹搬到别处后，重新运行一次
  app\EchoDesk.exe，程序会提示并重建快捷方式（也会自动修正工作目录）。

【卸载】
  本程序为绿色便携版：直接删除整个文件夹即可，不写注册表。

构建时间：$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
"@
Set-Content -Path (Join-Path $Out "使用说明.txt") -Value $readme -Encoding UTF8
Write-Ok "使用说明.txt"

# 合规文档随发布包一同提供（任务书修复五要求）：
# 根目录放 LICENSE / NOTICE（最容易找到的位置），其余合规文档随 docs\ 一起发。
foreach ($legal in @("LICENSE", "NOTICE", "SECURITY.md")) {
    $src = Join-Path $Root $legal
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $Out $legal) -Force
        Write-Ok $legal
    }
}

# 文档随发布包一同提供（任务书要求：研发记录需纳入发布物）。
# v0.2.1 起 docs\ 下有按语言分的子目录（docs\zh-Hans、docs\en），因此必须 -Recurse。
#
# 这里**复制 docs\ 下的全部文件**，不只是 *.md：修复五新增的 sbom 是 JSON
# （`docs\SBOM.json`），早期版本用 `-Filter *.md` 把它漏在仓库里没进包，
# 而 release.yml 与 `tools/verify_release.ps1` 都断言它必须存在 —— 属实测发现的打包缺陷。
# 另外 docs\ 里将来可能出现图片等非 md 资源，整份复制可以避免重犯。
$DocsOut = Join-Path $Out "docs"
New-Item -ItemType Directory -Path $DocsOut -Force | Out-Null
$docCount = 0
$docOther = 0
$DocsRoot = Join-Path $Root "docs"
foreach ($doc in Get-ChildItem $DocsRoot -File -Recurse -ErrorAction SilentlyContinue) {
    $rel = $doc.FullName.Substring($DocsRoot.Length).TrimStart('\')
    $dest = Join-Path $DocsOut (Split-Path $rel -Parent)
    if (-not (Test-Path $dest)) { New-Item -ItemType Directory -Path $dest -Force | Out-Null }
    Copy-Item $doc.FullName $dest -Force
    if ($doc.Extension -eq ".md") { $docCount++ } else { $docOther++; Write-Ok "docs\$rel" }
}
Write-Ok "docs\（$docCount 个 Markdown + $docOther 个其他文件，含按语言的子目录）"

# 批量处理结果目录：先建好空目录，让"结果会生成在哪"一目了然
# （后端 automation.results_root 以工作目录/程序根目录为基准解析，指向的正是这里）
$ResultsOut = Join-Path $Out "批量处理结果"
New-Item -ItemType Directory -Path $ResultsOut -Force | Out-Null
Write-Ok "批量处理结果\（自动化结果的默认位置）"

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
            summary          = ""
            model_path       = if ($old.model_path) { $old.model_path } else { "" }
            file_name        = ""
            file_content     = ""
            file_summary     = ""
            file_summary_meta = @{}
            history          = @()
        }
        ($fresh | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath $SessionFile -Encoding UTF8
        Write-Ok "session.json 已重置为空会话（保留 mode/model_path/custom_prompt；-KeepSession 可跳过）"
    }
    catch {
        Remove-Item -LiteralPath $SessionFile -Force -ErrorAction SilentlyContinue
        Write-Ok "session.json 无法解析，已删除（后端首次保存时会重建）"
    }
}
# ---------- 7. 写入版本资源与图标 ----------
if (Test-Path $Rcedit) {
    Write-Step "写入前端版本资源与图标（rcedit）"
    Invoke-Native "cmd" @("/c", "`"$Root\tools\set_metadata.bat`" `"$AppOut`"") "元数据写入"
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

# ---------- 9. 创建外层入口快捷方式 ----------
if (-not $NoShortcut) {
    Write-Step "创建启动快捷方式（外层 → app\EchoDesk.exe）"
    $target = Join-Path $AppOut "EchoDesk.exe"
    $link = Join-Path $Out $ShortcutName
    if (-not (Test-Path $target)) { Write-Warn2 "未找到主程序，跳过快捷方式" }
    else {
        $shell = New-Object -ComObject WScript.Shell
        $sc = $shell.CreateShortcut($link)
        $sc.TargetPath = $target
        # 工作目录设为外层：进程的当前目录即外层，批量处理结果等相对路径产物不会埋进 app\
        $sc.WorkingDirectory = $Out
        $sc.Description = "EchoDesk - Local AI Desktop Assistant"
        $sc.WindowStyle = 1
        $iconInApp = Join-Path $AppOut "EchoDesk.ico"
        if (Test-Path $iconInApp) { $sc.IconLocation = "$iconInApp,0" }
        elseif (Test-Path $Icon) { $sc.IconLocation = "$Icon,0" }
        $sc.Save()
        Write-Ok "$ShortcutName → app\EchoDesk.exe（工作目录=外层 $Out）"
    }
}

# ---------- 9b. 结构校验 ----------
# 修复七的验收条件之一："外层除快捷方式、结果目录、用户数据外，不出现程序文件"。
# 这里直接断言，避免以后有人不小心把 dotnet publish 的输出目录改回外层。
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
Write-Ok "入口: $ShortcutName"
