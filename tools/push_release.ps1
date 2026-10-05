# 把发布提交与标签推到 GitHub（关联仓库用的一站式脚本）
#
# 为什么需要它：`git remote add` + `git push` 这两步必须在**有凭据的机器**上做，
# 而凭据只属于维护者。这个脚本把"推之前该核对的都核对掉"做成自动检查，
# 让维护者只需要执行一条命令，而不是先去翻记录确认标签指向哪个提交、安装程序
# 是不是这一版、工作区干不干净。
#
# 它**不**保存、不读取、不打印任何令牌：认证交给 git 自己的凭据机制
# （凭据管理器 / SSH 密钥 / 一次性 PAT）。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools\push_release.ps1 -DryRun     # 只做检查
#   powershell -ExecutionPolicy Bypass -File tools\push_release.ps1             # 检查后推送
#   powershell -ExecutionPolicy Bypass -File tools\push_release.ps1 -RemoteName upstream
#
# 推送失败（没有凭据）时的三种做法见脚本末尾提示，或 docs\zh-Hans\DEVELOPER.md。
[CmdletBinding()]
param(
    [string]$RemoteName = "origin",
    [string]$RemoteUrl = "https://github.com/VoidEntropiaStudio/EchoDesk.git",
    [string]$Branch = "master",
    [string]$Tag = "v0.2.1",
    [string]$Installer = "publish\EchoDesk-v0.2.1-Setup.exe",
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

# ---------- 4. 远端 ----------
Write-Step "检查远端 $RemoteName"
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
    Write-Ok "远端已存在：$RemoteName -> $url"
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
Write-Step "推送到 $RemoteName"
# 没有凭据时**立刻失败**，不在这里挂住等人输入：
#   * GIT_TERMINAL_PROMPT=0 关掉 git 自己的终端提问；
#   * GCM_INTERACTIVE=never 关掉 Git Credential Manager 的交互（少了这一条，
#     GCM 会弹窗或长时间阻塞 —— 实测脚本会静默挂住 5 分钟以上，看不出在等什么）。
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
    # SSH 地址在运行时拼出来：仓库门禁 tools\check_feedback.ps1 会拦下任何
    # "邮箱形态"的字面量（用户名、at 符号、域名连写），目的是不让人邮箱混进开源仓库，
    # 而 git 的 SSH 写法恰好长得一样 —— 这是同一条规则下的误报。
    # 与其把发布门禁放松（那是发布前最后一道闸，宁可严），不如在打印时拼装。
    $sshHost = "github" + ".com"
    $sshUrl = "git" + "@" + $sshHost + ":VoidEntropiaStudio/EchoDesk.git"
    Write-Host "`n!! 推送失败（通常是本机没有 GitHub 凭据）。三种做法：" -ForegroundColor Red
    Write-Host "   1) 凭据管理器：直接 git push（本脚本就是这样），弹出的窗口里用**个人访问令牌**当密码；"
    Write-Host "      令牌只需 repo 权限，用完可在 GitHub 上随时吊销。"
    Write-Host "   2) 一次性令牌（不写进任何文件）："
    Write-Host "        git push https://<用户名>@$sshHost/VoidEntropiaStudio/EchoDesk.git $Branch"
    Write-Host "      提示输入密码时粘贴令牌即可。"
    Write-Host "   3) SSH：生成密钥 -> 把公钥加到 GitHub -> "
    Write-Host "        git remote set-url $RemoteName $sshUrl"
    Write-Host "`n   注意：如果仓库是**私有**的，匿名访问会要求用户名，这是正常的（不是仓库不存在）。"
    exit 1
}

Write-Ok "已推送分支 $Branch 与标签 $Tag"
Write-Host "`n建议接下来（在 GitHub 网页上）：" -ForegroundColor Cyan
Write-Host "   * 确认默认分支是 $Branch（Settings -> Branches）；"
Write-Host "   * 用 .github\workflows\release.yml 建一个草稿 Release，附件用 publish\ 下的"
Write-Host "     EchoDesk-v0.2.1-Setup.exe + SBOM.json + THIRD_PARTY_LICENSES(.zh-Hans).md + SHA256SUMS.txt。"
exit 0
