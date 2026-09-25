<#
.SYNOPSIS
    发布任务书「全局要求三」与「全局要求五」的门禁检查（反馈渠道 + 不该有的内容）。

.DESCRIPTION
    在打包前跑，检查的是**仓库里的源文件**，因为发布物是从这里生成的：
    源文件不合格，打出来的包一定不合格。

    检查项：
      1. 反馈渠道存在：要求的文档里必须出现 GitHub Issues / Discussions，
         安全文档里必须出现 Security Advisory 私有报告入口。
      2. 无个人邮箱：除项目自己的 GitHub noreply 地址与 IANA 保留的 example.com
         示例域名外，不允许出现任何邮箱（QQ/163/Gmail/Outlook/… 一律失败）。
      3. 无实体地址 / 个人身份线索：常见中文地址词与"某某路/号/楼/园区"等形态。
      4. 无个人绝对路径：`C:\Users\<用户名>` 形式的本机路径（除通用占位 root/Public 等）。
      5. 无 AGPL/GPL 运行时残留：发布物不得引用 pymupdf/fitz/mupdf
         （PyInstaller 是构建工具，带官方例外，单独说明）。
      6. 安全文档必须写明"不要在公开 Issue 里披露"。

    为什么写成脚本而不是靠人工核对：这份清单每次发布都要重过一遍，
    人工核对必然会漏；固化成门禁才能保证 v0.2.2 不会退回去。

    退出码 0 = 全部通过；非 0 = 有失败项（并逐条打印文件与行号）。

    【注意】本脚本内部**绝不能用 exit**（PowerShell 的 exit 在 scriptblock 里会结束整个会话，
    曾经让 verify_release.ps1 在第一个检查后就静默退出）。用 throw 或返回布尔值。
#>
[CmdletBinding()]
param(
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$script:Failures = @()
$script:Passes = @()

function Pass([string]$text) { $script:Passes += $text; if (-not $Quiet) { Write-Host "  [OK]   $text" -ForegroundColor Green } }
function Fail([string]$text) { $script:Failures += $text; Write-Host "  [FAIL] $text" -ForegroundColor Red }

<#
    取仓库内被跟踪的文本文件（排除 prompts\ 任务书、publish\ 产物、
    二进制与历史过程文档）。历史记录里的旧邮箱是"当时的事实"，
    本门禁检查的是**当前对外文档**。
#>
function Get-TextFiles {
    # core.quotePath=false 必须加：默认情况下 git 会把非 ASCII 路径转义成
    # "docs/v0.2.1-\344\277\256..." 这种形式，[IO.Path]::GetExtension 会直接抛
    # Illegal characters in path（实测踩到，脚本在扫描阶段就崩）。
    $files = & git -C $Root -c core.quotePath=false ls-files
    foreach ($f in $files) {
        $rel = $f -replace '/', '\'
        if ($rel -like "prompts\*") { continue }
        if ($rel -like "publish\*") { continue }
        if ($rel -like "artifacts\*") { continue }
        $ext = [System.IO.Path]::GetExtension($rel).ToLower()
        if ($ext -notin @('.md', '.txt', '.json', '.yml', '.yaml', '.ps1', '.py', '.cs', '.xaml',
                '.bat', '.iss', '.cfg', '.ini', '.spec')) { continue }
        $full = Join-Path $Root $rel
        if (Test-Path $full) { $full }
    }
}

function Search-Pattern([string[]]$Files, [string]$Pattern, [string]$What) {
    $hits = @()
    foreach ($f in $Files) {
        $m = Select-String -LiteralPath $f -Pattern $Pattern -ErrorAction SilentlyContinue
        foreach ($x in $m) {
            $hits += [pscustomobject]@{
                File = $x.Path.Substring($Root.Length).TrimStart('\')
                Line = $x.LineNumber
                Text = $x.Line.Trim()
            }
        }
    }
    return $hits
}

Write-Host "==> 反馈渠道与「不该有的」门禁检查" -ForegroundColor Cyan

$files = @(Get-TextFiles)
Write-Host "    扫描被跟踪的文本文件 $($files.Count) 个" -ForegroundColor DarkGray

# ---------------------------------------------------------------------------
# 1. 反馈渠道存在（任务书「全局要求三」）
# ---------------------------------------------------------------------------
$ISSUES = "https://github.com/VoidEntropiaStudio/EchoDesk/issues"
$DISCUSS = "https://github.com/VoidEntropiaStudio/EchoDesk/discussions"
$ADVISORY = "https://github.com/VoidEntropiaStudio/EchoDesk/security/advisories/new"

$needBoth = @(
    "README.md", "README.zh-Hans.md", "CONTRIBUTING.md",
    "docs\en\FAQ.md", "docs\zh-Hans\FAQ.md"
)
foreach ($rel in $needBoth) {
    $full = Join-Path $Root $rel
    if (-not (Test-Path $full)) { Fail "缺少文件: $rel"; continue }
    $text = Get-Content -LiteralPath $full -Raw -Encoding UTF8
    $hasIssues = $text -match [regex]::Escape($ISSUES)
    $hasDiscuss = $text -match [regex]::Escape($DISCUSS)
    if ($hasIssues -and $hasDiscuss) { Pass "$rel 同时给出 Issues 与 Discussions 链接" }
    else {
        Fail "$rel 反馈渠道不全（Issues=$hasIssues Discussions=$hasDiscuss）——任务书要求公开反馈指向 GitHub Issues 与 Discussions"
    }
}

# 安全文档：私有报告入口 + nodemailer 之外的兜底邮箱
foreach ($rel in @("SECURITY.md", "SECURITY.zh-Hans.md")) {
    $full = Join-Path $Root $rel
    if (-not (Test-Path $full)) { Fail "缺少安全文档: $rel"; continue }
    $text = Get-Content -LiteralPath $full -Raw -Encoding UTF8
    if ($text -match [regex]::Escape($ADVISORY)) { Pass "$rel 含 Security Advisory 私有报告入口" }
    else { Fail "$rel 未给出 Security Advisory 私有报告入口（任务书「全局要求三」首选渠道）" }

    # "请勿公开" 必须写明。中文写法很自由（"请不要为安全问题开公开 Issue"
    # 里并没有"不要开公开 Issue"这个连续子串），所以用"公开"与"Issue"邻近判定，
    # 而不是去匹配某个固定短句 —— 固定短句会让门禁对合法改写误报。
    $zhOk = ($text -match '公开[^\n。]{0,12}Issue') -or ($text -match 'Issue[^\n。]{0,12}公开')
    $enOk = ($text -match '(?i)do not (open|file)[^.\n]{0,40}public') -or
    ($text -match '(?i)not[^.\n]{0,20}public (issue|advisory|discussion)')
    if ($zhOk -or $enOk) {
        Pass "$rel 明确要求不要公开披露安全问题"
    }
    else { Fail "$rel 未明确要求"不要在公开 Issue 中披露安全问题"" }

    if ($text -match [regex]::Escape("VoidEntropiaStudio@users.noreply.github.com")) {
        Pass "$rel 兜底渠道为 GitHub noreply 邮箱"
    }
    else { Fail "$rel 缺少 GitHub noreply 兜底邮箱" }
}

# EULA / PRIVACY 的条款与数据问题也要指向 Issues
foreach ($rel in @("docs\EULA.md", "docs\EULA.zh-Hans.md", "docs\PRIVACY.md", "docs\PRIVACY.zh-Hans.md")) {
    $full = Join-Path $Root $rel
    if (-not (Test-Path $full)) { Fail "缺少文件: $rel"; continue }
    $text = Get-Content -LiteralPath $full -Raw -Encoding UTF8
    if ($text -match [regex]::Escape($ISSUES)) { Pass "$rel 指向 GitHub Issues" }
    else { Fail "$rel 未指向 GitHub Issues（任务书「全局要求三」）" }
}

# ---------------------------------------------------------------------------
# 2. 无个人邮箱（只允许 noreply 与 example.com 示例域名）
# ---------------------------------------------------------------------------
$BANNED_MAIL_DOMAINS = @(
    'qq\.com', '163\.com', '126\.com', 'yeah\.net', 'sina\.com', 'sohu\.com',
    'foxmail\.com', 'gmail\.com', 'googlemail\.com', 'outlook\.com', 'hotmail\.com',
    'live\.com', 'yahoo\.com', 'icloud\.com', 'me\.com', 'protonmail\.com',
    'aliyun\.com', '139\.com', '189\.cn'
)
$bannedHits = @()
foreach ($d in $BANNED_MAIL_DOMAINS) {
    $bannedHits += Search-Pattern $files "[A-Za-z0-9._%+\-]+@$d" ""
}
if ($bannedHits.Count -eq 0) { Pass "无个人邮箱域名（QQ/163/Gmail/Outlook 等 $($BANNED_MAIL_DOMAINS.Count) 类）" }
else {
    foreach ($h in $bannedHits) { Fail "疑似个人邮箱: $($h.File):$($h.Line) — $($h.Text)" }
}

# 所有邮箱：只允许 noreply 与 IANA 保留示例域名。
# 必须排除 URL 的 userinfo 形式（`http://user:password@proxy.corp.example:8080`）：
# 那是"示例代理地址"的写法，不是邮箱，但它能被邮箱正则匹配到（实测误报过一次）。
$MAIL_RE = [regex]'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}'
$allMail = Search-Pattern $files $MAIL_RE.ToString() ""
$badMail = @()
foreach ($h in $allMail) {
    if ($h.File -match '^docs\\v0\.2\.\d-(研发记录|修复记录)') { continue }
    foreach ($m in $MAIL_RE.Matches($h.Text)) {
        $addr = $m.Value
        # 前面就是 `//` 或 `:` → 说明这是 URL 里的 user:pass@host
        $before = $h.Text.Substring(0, $m.Index)
        if ($before -match '//$' -or $before -match '//[^/@]*:$') { continue }
        if ($addr -like "*@users.noreply.github.com") { continue }
        if ($addr -match '@(example\.(com|org|net))$') { continue }
        $badMail += [pscustomobject]@{ File = $h.File; Line = $h.Line; Text = "$addr  <-- $($h.Text)" }
    }
}
if ($badMail.Count -eq 0) {
    Pass "全部邮箱地址仅为 GitHub noreply 或 IANA 保留示例域名（共扫描到 $($allMail.Count) 行含 @ 形态）"
}
else {
    foreach ($h in $badMail) { Fail "不允许的邮箱地址: $($h.File):$($h.Line) — $($h.Text)" }
}

# ---------------------------------------------------------------------------
# 3. 无实体地址 / 个人身份线索
# ---------------------------------------------------------------------------
# 只在"真的会对外发布联系方式"的文档里查，且要求关键词与门牌数字紧邻。
# 为什么不扫全仓库：中文散文里"三层目录""第三方组件"这类词极易被
# "N层/N号"的宽泛模式误伤（实测在 CHANGELOG 里误报过一次），
# 而门禁一旦频繁误报就会被绕过，等于没有门禁。
$addrScope = @(
    "LICENSE", "LICENSE.zh-Hans.md", "NOTICE", "NOTICE.zh-Hans.md",
    "SECURITY.md", "SECURITY.zh-Hans.md", "README.md", "README.zh-Hans.md",
    "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "CODE_OF_CONDUCT.zh-Hans.md",
    "USAGE.txt",
    "docs\PRIVACY.md", "docs\PRIVACY.zh-Hans.md",
    "docs\EULA.md", "docs\EULA.zh-Hans.md",
    "docs\en\COMPLIANCE.md", "docs\zh-Hans\COMPLIANCE.md"
)
$addrFiles = @($addrScope | ForEach-Object { Join-Path $Root $_ } | Where-Object { Test-Path $_ })
$addrPatterns = @(
    '\d+\s*(号楼|号院|栋|单元|室)\b',
    '(街道|大道|产业园|科技园|写字楼|大厦)\s*\d+',
    '(?i)\b(P\.?O\.? Box|Suite|Apt\.?)\s*\d+'
)
$addrHits = @()
foreach ($p in $addrPatterns) { $addrHits += Search-Pattern $addrFiles $p "" }
if ($addrHits.Count -eq 0) {
    Pass "合规与用户文档中未发现实体地址（门牌/楼层/园区/信箱）"
}
else { foreach ($h in $addrHits) { Fail "疑似实体地址: $($h.File):$($h.Line) — $($h.Text)" } }

# ---------------------------------------------------------------------------
# 4. 无个人绝对路径（C:\Users\<真实用户名>）
# ---------------------------------------------------------------------------
$pathHits = @(Search-Pattern $files '(?i)C:\\Users\\(?!Public\b|Default\b|All Users\b|<)[A-Za-z0-9._\-]+' "")
$pathHits = @($pathHits | Where-Object { $_.File -notmatch '^docs\\v0\.2\.\d-(研发记录|修复记录)' })
if ($pathHits.Count -eq 0) { Pass "未发现个人绝对路径（C:\Users\<用户名>）" }
else { foreach ($h in $pathHits) { Fail "个人绝对路径残留: $($h.File):$($h.Line) — $($h.Text)" } }

# ---------------------------------------------------------------------------
# 5. 无 AGPL/GPL 运行时残留（任务书「全局要求五」）
# ---------------------------------------------------------------------------
# 只认"真的还在用"，不认"解释为什么不用了"。
# v0.2.1 的代码与注释里到处都在提 pymupdf（说明它是怎么被换掉的），
# 把注释也算成残留会得到一个永远失败的门禁（实测第一次跑就误报 13 项）。
$agplReal = @()

# 5.1 requirements.txt 里**未注释**的 pymupdf/fitz 行
$reqFile = Join-Path $Root "backend\requirements.txt"
if (Test-Path $reqFile) {
    $lineNo = 0
    foreach ($line in Get-Content -LiteralPath $reqFile -Encoding UTF8) {
        $lineNo++
        # 取注释之前的部分。
        # 注意不要写 `$line.Split('#', 1)`：.NET 的 Split(char[], int) 在 count=1 时
        # 返回的是**整行**（最多切成 1 段），注释根本没被去掉 —— 实测因此把
        # 5 行解释性注释误判成"未注释的 AGPL 依赖"。
        $code = if ($line.Contains('#')) { $line.Substring(0, $line.IndexOf('#')) } else { $line }
        if ($code -match '(?i)\b(pymupdf|fitz|mupdf)\b') {
            $agplReal += "backend\requirements.txt:$lineNo 未注释的 AGPL 依赖: $($line.Trim())"
        }
    }
}

# 5.2 后端代码里真的 import 了
$pyFiles = @($files | Where-Object { $_ -like "*\backend\*.py" -and $_ -notmatch '\\_(internal|pycache)\\' })
$importHits = @(Search-Pattern $pyFiles '(?m)^\s*(import|from)\s+(pymupdf|fitz)\b' "")
foreach ($h in $importHits) { $agplReal += "$($h.File):$($h.Line) 直接 import: $($h.Text)" }

# 5.3 spec 必须把 pymupdf/fitz 放进 excludes（这是"防误打包"的正向证据）
$specFile = Join-Path $Root "backend\EchoBackend.spec"
if (Test-Path $specFile) {
    $spec = Get-Content -LiteralPath $specFile -Raw -Encoding UTF8
    if ($spec -match '(?i)"pymupdf"' -and $spec -match 'excludes') {
        Pass "backend\EchoBackend.spec 已把 pymupdf/fitz 列入 excludes（防止间接 import 被误打包）"
    }
    else {
        Fail "backend\EchoBackend.spec 未把 pymupdf/fitz 列入 excludes —— 间接 import 可能被误打包"
    }
}

# 5.4 已安装环境里不得有 pymupdf
$cleanPy = Join-Path $Root "artifacts\clean-venv\Scripts\python.exe"
if (Test-Path $cleanPy) {
    $probe = & $cleanPy -c "import importlib.util as u; print('yes' if u.find_spec('pymupdf') or u.find_spec('fitz') else 'no')" 2>&1
    if ("$probe".Trim() -eq "no") { Pass "干净 venv 中未安装 pymupdf/fitz" }
    else { Fail "干净 venv 中仍可导入 pymupdf/fitz: $probe" }
}

if ($agplReal.Count -eq 0) { Pass "无 pymupdf/fitz/mupdf 运行时残留（依赖声明与代码 import 均为零）" }
else { foreach ($h in $agplReal) { Fail "AGPL 残留: $h" } }

# ---------------------------------------------------------------------------
# 6. 本地插件/密钥类文件不得入库
# ---------------------------------------------------------------------------
$secretPatterns = @(
    '(?i)(api[_-]?key|secret|password|passwd|token)\s*[:=]\s*["''][^"'']{16,}["'']',
    '-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----',
    '(?i)authorization:\s*bearer\s+[A-Za-z0-9._\-]{20,}'
)
$secretHits = @()
foreach ($p in $secretPatterns) { $secretHits += Search-Pattern $files $p "" }
$secretHits = @($secretHits | Where-Object {
        $_.File -notmatch '^docs\\v0\.2\.\d-(研发记录|修复记录)' -and
        $_.File -notmatch '^backend\\lang\\' -and
        $_.File -notmatch 'placeholder|例如|e\.g\.|<'
    })
if ($secretHits.Count -eq 0) { Pass "未发现密钥/令牌/口令明文（API Key、私钥、Bearer Token）" }
else { foreach ($h in $secretHits) { Fail "疑似凭证明文: $($h.File):$($h.Line) — $($h.Text)" } }

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "------------------------------------------" -ForegroundColor Cyan
Write-Host "通过 $($script:Passes.Count) 项，失败 $($script:Failures.Count) 项"
if ($script:Failures.Count -gt 0) {
    Write-Host ""
    Write-Host "失败明细（修订后重跑本脚本）：" -ForegroundColor Red
    foreach ($f in $script:Failures) { Write-Host "  - $f" -ForegroundColor Red }
    throw "反馈渠道 / 不该有的内容 门禁未通过（$($script:Failures.Count) 项）"
}
Write-Host "反馈渠道与内容洁净度门禁通过。" -ForegroundColor Green
