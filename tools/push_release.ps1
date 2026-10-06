# 把发布提交与标签推到 GitHub（关联仓库用的一站式脚本）
#
# **统一使用 SSH**（v0.2.1 跨平台-2 任务书全局约定）：本脚本只走 SSH，
# 不再支持、也不再提示 HTTPS / PAT 做法。原因：HTTPS 推送到 GitHub 在这台机器上
# 反复失败（`RPC failed; HTTP 408` / `Recv failure: Connection was reset`），
# 而 SSH 不受同一网络干扰，且不需要每次输凭据。
# 脚本会**断言** remote 是 SSH 形式：发现 HTTPS 直接判为预检失败并给出改法，
# 而不是"照样推、失败了再说"。
#
# 为什么需要它：`git remote add` + `git push` 这两步必须在**有私钥的机器**上做，
# 而私钥只属于维护者。这个脚本把"推之前该核对的都核对掉"做成自动检查，
# 让维护者只需要执行一条命令，而不是先去翻记录确认标签指向哪个提交、安装程序
# 是不是这一版、工作区干不干净。
#
# 它**不**保存、不读取、不打印任何密钥，也不调用凭据管理器：认证完全交给
# ssh-agent 与本机 `~/.ssh` 下的密钥。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools\push_release.ps1 -DryRun     # 只做检查
#   powershell -ExecutionPolicy Bypass -File tools\push_release.ps1             # 检查后推送
#   powershell -ExecutionPolicy Bypass -File tools\push_release.ps1 -RemoteName upstream
#
# 推送失败时的**SSH 排查做法**见脚本末尾提示，或 docs\zh-Hans\DEVELOPER.md。
[CmdletBinding()]
param(
    [string]$RemoteName = "origin",
    [string]$RemoteUrl = "git@github.com:VoidEntropiaStudio/EchoDesk.git",
    [string]$Branch = "master",
    [string]$Tag = "v0.2.1",
    [string]$Installer = "publish\EchoDesk-v0.2.2-Setup.exe",
    [string]$Sums = "publish\SHA256SUMS.txt",
    # 期望哈希。**留空表示以 SHA256SUMS.txt 为准**（构建时生成的那份清单是唯一真相），
    # 只在需要"把这一版钉死在某个哈希上"时才显式传 -ExpectedSha256。
    #
    # 为什么要改：上一版这里写死了一个哈希，而每次重新打包哈希都会变 ——
    # 于是"预检"在下一次发布时必然失败，报的还是"安装程序与登记不一致"这种
    # 看起来像产物坏了的信息（本轮实测踩到）。清单是自动生成的，比手抄可靠。
    [string]$ExpectedSha256 = "",
    [switch]$DryRun,
    [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Write-Step([string]$Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "    [OK] $Text" -ForegroundColor Green }
function Write-Warn2([string]$Text) { Write-Host "    [!]  $Text" -ForegroundColor Yellow }

$problems = @()

# SSH 目标（v0.2.1 跨平台-2 全局约定：本仓库所有 Git 远端操作统一用 SSH）。
$SshTarget = "git@github.com"

# ---------- 1. 工作区干净 ----------
Write-Step "检查工作区"
$dirty = @(git status --porcelain)
if ($dirty.Count -gt 0) {
    if ($AllowDirty) {
        Write-Warn2 "工作区有 $($dirty.Count) 项未提交改动（-AllowDirty，继续）"
    } else {
        $problems += "工作区不干净（$($dirty.Count) 项）：推送会把未提交的改动留在本机，先提交或清理，或用 -AllowDirty"
    }
} else {
    Write-Ok "工作区干净"
}

# ---------- 2. 标签存在、是附注标签、且指向构建提交 ----------
Write-Step "检查标签 $Tag"
$tagType = (git cat-file -t $Tag 2>$null)
if ($tagType -ne "tag") {
    $problems += "标签 $Tag 不存在或不是附注标签（git cat-file -t 返回 '$tagType'）"
} else {
    $tagCommit = (git rev-parse "$Tag^{commit}").Trim()
    $headCommit = (git rev-parse HEAD).Trim()
    Write-Ok "$Tag 是附注标签，指向 $($tagCommit.Substring(0,8))"
    # 不变式：标签之后只允许追加 records\ 与 tools\（两者都不进发布物）
    $diff = @(git diff "$Tag^{commit}" HEAD -- . ":(exclude)records" ":(exclude)tools")
    if ($diff.Count -gt 0) {
        $problems += "标签之后有**随发布物分发**的文件被改动（不变式被破坏）：`n$(($diff | Select-Object -First 20) -join "`n")"
    } else {
        Write-Ok "不变式成立：标签之后只动了 records\ 与 tools\"
    }
    if ($tagCommit -ne $headCommit) {
        Write-Warn2 "标签指向 $($tagCommit.Substring(0,8))，HEAD 已是 $($headCommit.Substring(0,8))（允许，但确认 HEAD 都是 records/tools 改动）"
    }
}

# ---------- 3. 安装程序与校验清单 ----------
Write-Step "检查安装程序"
if (-not (Test-Path $Installer)) {
    Write-Warn2 "找不到 $Installer —— 发布物不入库，本机没有时跳过（推送源码与标签仍然有效）"
} else {
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Installer).Hash.ToLower()
    $sizeMb = [math]::Round((Get-Item $Installer).Length / 1MB, 2)
    Write-Ok "$Installer  $sizeMb MB  SHA256 $($hash.Substring(0,16))…"
    if ($ExpectedSha256 -and $hash -ne $ExpectedSha256.ToLower()) {
        $problems += "安装程序 SHA256 与脚本登记的不一致：实测 $hash，登记 $ExpectedSha256"
    }
    if (Test-Path $Sums) {
        # 清单里应有一行 `<哈希>  <文件名>`，文件名与本脚本要推的产物同名。
        # 按**文件名**匹配而不是"清单里出现过这个哈希"，才能发现"清单是旧的、
        # 产物是新的"这种最危险的情况（上一版只查哈希是否出现，做不到）。
        $name = Split-Path $Installer -Leaf
        $line = @(Get-Content $Sums | Where-Object { $_ -match [regex]::Escape($name) }) | Select-Object -First 1
        if (-not $line) {
            $problems += "SHA256SUMS.txt 里没有 $name 这一行（清单不完整）"
        } elseif ($line -notmatch [regex]::Escape($hash)) {
            $problems += "SHA256SUMS.txt 里 $name 的哈希与产物不一致（清单与产物不匹配）"
        } else {
            Write-Ok "SHA256SUMS.txt 中 $name 的哈希与产物一致"
        }
    } else {
        Write-Warn2 "找不到 $Sums"
    }
}

# ---------- 4. 远端（必须 SSH） ----------
Write-Step "检查远端 $RemoteName（统一 SSH）"
$remotes = @(git remote)
if ($remotes -notcontains $RemoteName) {
    if ($DryRun) {
        Write-Warn2 "远端 $RemoteName 不存在（-DryRun 不添加）；将执行：git remote add $RemoteName $RemoteUrl"
    } else {
        git remote add $RemoteName $RemoteUrl
        Write-Ok "已添加远端：git remote add $RemoteName $RemoteUrl"
    }
} else {
    $url = (git remote get-url $RemoteName).Trim()
    # SSH 在本仓库有两种合法写法：scp 形态 `user@host:path` 与 `ssh://user@host/path`，
    # 两者都含 `@`。HTTPS 形式（http:// 或 https://）一律判预检失败 —— 这是任务书的
    # 全局约定，且实测 HTTPS 在这台机器上反复被中断（HTTP 408 / 连接重置）。
    if ($url -like "http://*" -or $url -like "https://*") {
        $problems += "远端 $RemoteName 是 HTTPS 形式：$url`n    本仓库统一用 SSH，改法（一条命令）：`n        git remote set-url $RemoteName $RemoteUrl"
    } elseif ($url -notlike "*@*") {
        $problems += "远端 $RemoteName 不像 SSH 形式（缺少 user@host）：$url`n    期望：$RemoteUrl"
    } else {
        Write-Ok "远端是 SSH：$RemoteName -> $url"
    }
}

# ---------- 4b. SSH 连通性 ----------
Write-Step "检查 SSH 连通性（$SshTarget）"
# 为什么提前到这里：HTTPS 时代的失败是"推到最后一步才发现链路不通"，一次完整预检全白做；
# 把连通性提前，失败点就落在"网络 / 密钥"这个真正的原因上，而不是含糊的 push 报错。
#
# BatchMode=yes  → 只用密钥，绝不提示口令（否则会静默挂住等人输入）；
# ConnectTimeout → 连不上就快速失败，不无限等待。
#
# 为什么走 `cmd /c` 并把输出重定向进临时文件，而不是 `& ssh ... 2>&1`：
# Windows PowerShell 5.1 会把**原生命令写到 stderr 的每一行**包装成 NativeCommandError
# 记录（ssh 成功时也会往 stderr 写 "does not provide shell access"），于是屏幕上出现
# 红字 "ssh.exe : Hi …" —— 明明是成功，看起来却像失败。`*>` 重定向也不行，错误记录会被
# 格式化后照样写进文件。交给 cmd 做重定向，PowerShell 就看不到那条 stderr 了。
# （这与记录里"Python 的 stderr 让退出码看起来是 1"是同一类问题的同一个解法。）
$sshStdout = [System.IO.Path]::GetTempFileName()
$sshCmdLine = 'ssh -o BatchMode=yes -o ConnectTimeout=10 -T ' + $SshTarget + ' > "' + $sshStdout + '" 2>&1'
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& cmd /c $sshCmdLine | Out-Null
$sshCode = $LASTEXITCODE
$ErrorActionPreference = $prevEap
$sshOutput = ((Get-Content -LiteralPath $sshStdout -Raw -ErrorAction SilentlyContinue) -replace "`r?`n", " ").Trim()
Remove-Item -LiteralPath $sshStdout -Force -ErrorAction SilentlyContinue
# 注意：`ssh -T git@github.com` 成功后**退出码是 1**（GitHub 不提供 shell），
# 因此判据是输出里的 successfully authenticated，而不是退出码。
if ($sshOutput -match "successfully authenticated") {
    Write-Ok "SSH 认证通过：$sshOutput"
} else {
    $problems += @"
SSH 到 $SshTarget 未通过（退出码 $sshCode）：
        $sshOutput
    排查顺序：
        1) 有没有密钥：      Get-ChildItem `$env:USERPROFILE\.ssh
        2) 公钥加到 GitHub： Settings -> SSH and GPG keys（用 id_ed25519.pub 的内容）
        3) 手动测一次：      ssh -T $SshTarget
           期望输出 Hi <用户名>! You've successfully authenticated
        4) 网络挡 22 端口：  在 `$env:USERPROFILE\.ssh\config 里加
             Host github.com
               HostName ssh.github.com
               Port 443
               User git
"@
}

# ---------- 5. 汇总 ----------
if ($problems.Count -gt 0) {
    Write-Host "`n!! 预检未通过，共 $($problems.Count) 项：" -ForegroundColor Red
    foreach ($p in $problems) { Write-Host "   - $p" -ForegroundColor Red }
    Write-Host "`n修好之后再跑一次。预检不通过时**不要**推送。" -ForegroundColor Red
    exit 1
}

if ($DryRun) {
    Write-Host "`n预检全部通过（-DryRun，未推送）。" -ForegroundColor Green
    Write-Host "实际推送请去掉 -DryRun："
    Write-Host "    powershell -ExecutionPolicy Bypass -File tools\push_release.ps1"
    exit 0
}

# ---------- 6. 推送 ----------
Write-Step "推送到 $RemoteName（SSH）"
# 没有密钥时**立刻失败**，不在这里挂住等人输入：
#   * GIT_TERMINAL_PROMPT=0 关掉 git/ssh 自己的终端提问（否则会静默挂住，
#     看不出在等什么口令 —— 实测踩过 5 分钟以上的静默等待）；
#   * GCM_INTERACTIVE=never 关掉 Git Credential Manager 的交互。改用 SSH 后 GCM 不再
#     参与认证，这一条只是防止"某处仍配着 HTTPS helper"时弹出凭据窗口。
$env:GIT_TERMINAL_PROMPT = "0"
$env:GCM_INTERACTIVE = "never"
$failed = $false
git push --set-upstream $RemoteName $Branch
if ($LASTEXITCODE -ne 0) { $failed = $true }
if (-not $failed) {
    git push $RemoteName $Tag
    if ($LASTEXITCODE -ne 0) { $failed = $true }
}

if ($failed) {
    Write-Host "`n!! 推送失败（SSH 链路问题）。按顺序排查：" -ForegroundColor Red
    Write-Host "   1) 先测连通性（本脚本第 4b 步已测过；那步是绿的再往下看）："
    Write-Host "        ssh -T $SshTarget"
    Write-Host "      期望：Hi <用户名>! You've successfully authenticated"
    Write-Host "   2) 密钥存在但没被用上："
    Write-Host "        Get-ChildItem `$env:USERPROFILE\.ssh        # 要有 id_ed25519 与 id_ed25519.pub"
    Write-Host "        ssh-add `$env:USERPROFILE\.ssh\id_ed25519   # 交给 ssh-agent"
    Write-Host "   3) 公钥没加到 GitHub：Settings -> SSH and GPG keys -> New SSH key，"
    Write-Host "      粘贴 id_ed25519.pub 的**全文**（不是私钥）。"
    Write-Host "   4) 网络挡了 22 端口（公司网/防火墙常见）：改走 443，在 `$env:USERPROFILE\.ssh\config 里加"
    Write-Host "        Host github.com"
    Write-Host "          HostName ssh.github.com"
    Write-Host "          Port 443"
    Write-Host "          User git"
    Write-Host "   5) remote 还是 HTTPS（本脚本第 4 步会拦下，但手工推送可能绕过）："
    Write-Host "        git remote set-url $RemoteName $RemoteUrl"
    Write-Host "`n   注意：这条路**不再**使用 HTTPS 或个人访问令牌 —— HTTPS 推送在本机反复被中断" -ForegroundColor Yellow
    Write-Host "   （RPC failed; HTTP 408 / Recv failure: Connection was reset），SSH 是既定做法。" -ForegroundColor Yellow
    exit 1
}

Write-Ok "已推送分支 $Branch 与标签 $Tag"
Write-Host "`n建议接下来（在 GitHub 网页上）：" -ForegroundColor Cyan
Write-Host "   * 确认默认分支是 $Branch（Settings -> Branches）；"
Write-Host "   * 用 .github\workflows\release.yml 建一个草稿 Release，附件用 publish\ 下的"
Write-Host "     EchoDesk-v0.2.2-Setup.exe + SBOM.json + THIRD_PARTY_LICENSES(.zh-Hans).md + SHA256SUMS.txt。"
exit 0
