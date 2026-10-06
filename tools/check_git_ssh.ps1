# 统一 SSH 门禁（v0.2.1 跨平台-2 任务书「全局约定：所有 Git 操作统一使用 SSH」）
#
# 为什么需要一条**门禁**，而不只是"这一轮改掉了"：
# remote URL、脚本默认值、文档示例都是"改一次就漂回去"的东西 —— 下次有人
# 复制一段 HTTPS 的 clone 示例进文档，或者在新机器上 `git remote add` 用了 HTTPS，
# 没有任何东西会报警。任务书要求的是"从本轮起统一"，所以要用可执行的检查把它钉住。
#
# 检查项：
#   1. 本仓库的 remote 全部不是 HTTPS（且至少有一个 SSH remote）
#   2. 推送脚本的默认 remote URL 是 SSH 形式
#   3. 仓库内（除历史记录）不存在 HTTPS 形式的 git 操作示例
#      （clone / remote add / remote set-url / push / fetch / pull 后面直接跟 https://）
#   4. 推送脚本不再引导使用个人访问令牌（PAT）
#
# 不检查的：
#   * **网页链接**（issues / discussions / releases / advisories / homepage /
#     SPDX documentNamespace / Inno 的 MyAppURL）保持 HTTPS 是**正确**的 ——
#     SSH 地址在浏览器里打不开。任务书要求改的是 git 操作示例，不是这些链接。
#   * **records\ 下的历史记录**：那里写的是"当时用了什么"（HTTPS 时代的原貌），
#     属于过程事实。改写历史记录会让"记录可信"这件事本身失真。这与
#     tools\check_feedback.ps1 对历史过程文档的处理惯例一致。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools\check_git_ssh.ps1
#   powershell -ExecutionPolicy Bypass -File tools\check_git_ssh.ps1 -TestConnection   # 额外真连一次 GitHub
[CmdletBinding()]
param(
    [string]$RemoteName = "origin",
    # 真连一次 GitHub 验证密钥可用。默认关闭：门禁要能在**离线**环境跑通，
    # 而"有没有网络"不是代码质量的一部分（CI 里可以显式打开）。
    [switch]$TestConnection
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$script:Passes = @()
$script:Failures = @()
function Pass([string]$Text) { $script:Passes += $Text; Write-Host "  [OK]   $Text" -ForegroundColor Green }
function Fail([string]$Text) { $script:Failures += $Text; Write-Host "  [FAIL] $Text" -ForegroundColor Red }

Write-Host "== 统一 SSH 门禁 ==" -ForegroundColor Cyan

# ---------------------------------------------------------------------------
# 1. remote 不得是 HTTPS
# ---------------------------------------------------------------------------
$remoteLines = @(git remote -v 2>$null)
if ($remoteLines.Count -eq 0) {
    Fail "本仓库没有配置任何 remote（期望至少有一个 SSH remote）"
} else {
    $httpsRemotes = @($remoteLines | Where-Object { $_ -match 'https?://' })
    $sshRemotes = @($remoteLines | Where-Object { $_ -match '^\S+\s+(ssh://|\S+@\S+:\S+)' })
    if ($httpsRemotes.Count -eq 0) {
        Pass "remote 无 HTTPS 形式（共 $($remoteLines.Count) 条 remote 记录）"
    } else {
        foreach ($r in $httpsRemotes) {
            Fail "remote 使用了 HTTPS：$r —— 改法：git remote set-url $RemoteName git@github.com:<owner>/<repo>.git"
        }
    }
    if ($sshRemotes.Count -gt 0) {
        Pass "存在 SSH 形式的 remote（$($sshRemotes[0].Trim())）"
    } else {
        Fail "没有任何 SSH 形式的 remote"
    }
}

# ---------------------------------------------------------------------------
# 2. 推送脚本的默认 URL 是 SSH
# ---------------------------------------------------------------------------
$pushScript = Join-Path $Root "tools\push_release.ps1"
if (-not (Test-Path $pushScript)) {
    Fail "找不到 tools\push_release.ps1"
} else {
    $pushText = Get-Content -LiteralPath $pushScript -Raw
    if ($pushText -match '\$RemoteUrl\s*=\s*"(ssh://[^"]+|\S+@\S+:\S+)"') {
        Pass "push_release.ps1 的默认 RemoteUrl 是 SSH 形式"
    } else {
        Fail "push_release.ps1 的默认 RemoteUrl 不是 SSH 形式（应形如 git@github.com:<owner>/<repo>.git）"
    }
    # 4. 不再引导 PAT。
    # 只查**引导性措辞**，不查"个人访问令牌"这个词本身 —— 脚本里必须能写
    # "这条路不再使用个人访问令牌"这种**否定式说明**，否则门禁会逼着人把
    # "为什么不走 PAT"的理由也删掉（实测第一版就是这么误报的）。
    $patPatterns = @('当密码', '粘贴令牌', 'Personal Access Token', 'ghp_', 'repo 权限')
    $patHits = @($patPatterns | Where-Object { $pushText -match [regex]::Escape($_) })
    if ($patHits.Count -eq 0) {
        Pass "push_release.ps1 不含 PAT 引导（诊断信息全部是 SSH 排查）"
    } else {
        Fail "push_release.ps1 仍在引导使用个人访问令牌（PAT）：出现 $($patHits -join '、') —— 任务书要求统一走 SSH"
    }
}

# ---------------------------------------------------------------------------
# 3. 文档/脚本里不得有 HTTPS 形式的 git 操作示例
# ---------------------------------------------------------------------------
# 与 check_feedback.ps1 同口径：只看"该入库"的文件（tracked + 未忽略的 untracked），
# 并把历史过程目录排除在外。
$tracked = @(& git -C $Root -c core.quotePath=false ls-files)
$untracked = @(& git -C $Root -c core.quotePath=false ls-files --others --exclude-standard)
$allFiles = @($tracked) + @($untracked)
$textExt = @('.md', '.txt', '.json', '.yml', '.yaml', '.ps1', '.py', '.cs', '.xaml', '.axaml',
             '.iss', '.bat', '.sh', '.cfg', '.toml', '.spec', '.example')
$skipDir = '^(records|prompts|publish|artifacts|dist)[/\\]'
$files = @($allFiles | Where-Object {
    $ext = [System.IO.Path]::GetExtension($_).ToLower()
    $ext -in $textExt -and $_ -notmatch $skipDir
})

# 只匹配"git 子命令 + 后面跟 HTTPS URL"，因此不会误伤网页链接
# （例如 `[Releases](https://github.com/…)` 前面没有 git 子命令）。
$gitHttpsRe = [regex]'(?i)git\s+(clone|remote\s+add|remote\s+set-url|push|fetch|pull)\s+[^\r\n]*https?://'

$gitHttpsHits = @()
foreach ($rel in $files) {
    $full = Join-Path $Root $rel
    if (-not (Test-Path -LiteralPath $full)) { continue }
    $lineNo = 0
    foreach ($line in (Get-Content -LiteralPath $full -ErrorAction SilentlyContinue)) {
        $lineNo++
        if ($gitHttpsRe.IsMatch($line)) {
            $gitHttpsHits += [pscustomobject]@{ File = $rel; Line = $lineNo; Text = $line.Trim() }
        }
    }
}
if ($gitHttpsHits.Count -eq 0) {
    Pass "未发现 HTTPS 形式的 git 操作示例（扫描 $($files.Count) 个文件；网页链接不在检查范围）"
} else {
    foreach ($h in $gitHttpsHits) {
        Fail "HTTPS 形式的 git 操作示例: $($h.File):$($h.Line) — $($h.Text)"
    }
}

# ---------------------------------------------------------------------------
# 4.（可选）真连一次 GitHub
# ---------------------------------------------------------------------------
if ($TestConnection) {
    $tmp = [System.IO.Path]::GetTempFileName()
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & cmd /c ('ssh -o BatchMode=yes -o ConnectTimeout=10 -T git@github.com > "' + $tmp + '" 2>&1') | Out-Null
    $ErrorActionPreference = $prevEap
    $out = ((Get-Content -LiteralPath $tmp -Raw -ErrorAction SilentlyContinue) -replace "`r?`n", " ").Trim()
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    # `ssh -T git@github.com` 成功后退出码是 1（GitHub 不提供 shell），所以看输出而不是退出码。
    if ($out -match "successfully authenticated") { Pass "SSH 连通性：$out" }
    else { Fail "SSH 连通性未通过：$out" }
} else {
    Write-Host "  [--]   跳过 SSH 连通性实测（加 -TestConnection 打开；门禁默认不依赖网络）" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
Write-Host ""
Write-Host ("-" * 42)
if ($script:Failures.Count -eq 0) {
    Write-Host "通过 $($script:Passes.Count) 项，失败 0 项" -ForegroundColor Green
    Write-Host "统一 SSH 门禁通过。" -ForegroundColor Green
    exit 0
} else {
    Write-Host "通过 $($script:Passes.Count) 项，失败 $($script:Failures.Count) 项" -ForegroundColor Red
    exit 1
}
