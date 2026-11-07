# EchoDesk reproducible-build verification.
#
# [EN]
# This script proves the acceptance condition for reproducible builds: two complete,
# sequential runs of the same commit — frontend compilation, backend construction and
# archive generation — must produce byte-identical deliverables. It deliberately removes
# every build output and compiler cache between runs, but preserves dependency caches
# (wheels, python-build-standalone archives) because those inputs are pinned by the lock
# files and are not part of the build output.
#
# [ZH]
# 本脚本验证可复现构建的验收条件：同一提交连续执行两次完整流程——前端编译、后端构建与
# 归档生成——必须产出逐字节一致的交付物。脚本在两次运行之间刻意清除全部构建输出与编译器
# 缓存，但保留依赖缓存（轮子、python-build-standalone 归档），因为这些输入已由锁文件
# 固定，且不属于构建输出。
#
# [EN] Usage / [ZH] 用法：
#   powershell tools\verify_reproducible_build.ps1
#   powershell tools\verify_reproducible_build.ps1 -Runs 2
param(
    [string]$Version = "",
    [int]$Runs = 2
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "reproducible.ps1")
Set-Location $Root

# [EN] The verification never bypasses the clean-worktree guard: a reproducible result is
# [EN] meaningful only when it names an exact commit.
# [ZH] 验证流程绝不绕过干净工作区校验：只有指向确切提交的结果才具有意义。
$BuildEpoch = Initialize-SourceDateEpoch -Root $Root
$BuildUtc = Format-UtcTimestamp -Epoch $BuildEpoch
if (-not $Version) {
    $VersionFile = Join-Path $Root "backend\version.py"
    $VersionMatch = Select-String -Path $VersionFile -Pattern 'APP_VERSION\s*=\s*["'']([^"'']+)["'']' |
                    Select-Object -First 1
    if (-not $VersionMatch) { throw "读不出 APP_VERSION：$VersionFile（可用 -Version 显式指定）" }
    $Version = $VersionMatch.Matches[0].Groups[1].Value.TrimStart('v')
}

$Work = Join-Path $Root "artifacts\repro-build"
if (Test-Path $Work) { Remove-Item $Work -Recurse -Force }
New-Item -ItemType Directory -Path $Work -Force | Out-Null

function Invoke-Step([string]$Label, [scriptblock]$Body) {
    Write-Host ""
    Write-Host "==> $Label" -ForegroundColor Cyan
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    & $Body
    if ($LASTEXITCODE -ne 0) { throw "$Label 失败（退出码 $LASTEXITCODE）" }
    $sw.Stop()
    Write-Host ("    [EN] completed in {0:N1}s / [ZH] 完成，用时 {0:N1} 秒" -f $sw.Elapsed.TotalSeconds) -ForegroundColor Green
}

function Clear-BuildState {
    Write-Host ""
    Write-Host "==> [EN] cleaning build outputs and compiler caches / [ZH] 清理构建输出与编译器缓存" -ForegroundColor Cyan
    $targets = @(
        (Join-Path $Root "publish\EchoDesk-v$Version"),
        (Join-Path $Root "publish\EchoDesk-v$Version-Setup.exe"),
        (Join-Path $Root "publish\SHA256SUMS.txt"),
        (Join-Path $Root "publish\backend-SHA256SUMS.txt"),
        (Join-Path $Root "frontend\bin"),
        (Join-Path $Root "frontend\obj"),
        (Join-Path $Root "frontend-avalonia\EchoDesk.Avalonia\bin"),
        (Join-Path $Root "frontend-avalonia\EchoDesk.Avalonia\obj"),
        (Join-Path $Root "dist\SHA256SUMS.txt")
    )
    $targets += @(Get-ChildItem (Join-Path $Root "publish") -Directory -Filter "backend-*" -ErrorAction SilentlyContinue |
                  ForEach-Object { $_.FullName })
    $targets += @(Get-ChildItem (Join-Path $Root "publish") -File -Filter "backend-manifest-*" -ErrorAction SilentlyContinue |
                  ForEach-Object { $_.FullName })
    $targets += @(Get-ChildItem (Join-Path $Root "dist") -Force -ErrorAction SilentlyContinue |
                  Where-Object { $_.Name -like "EchoDesk-v$Version-*" -or $_.Name -like "manifest-*.json" } |
                  ForEach-Object { $_.FullName })
    foreach ($target in $targets) {
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Recurse -Force
        }
    }
    # [EN] Remove Python bytecode caches only inside project sources; dependency caches and
    # [EN] the build virtual environment are deliberately preserved.
    # [ZH] 仅清除项目源码内的 Python 字节码缓存；依赖缓存与构建虚拟环境刻意保留。
    foreach ($sourceDir in @("backend", "tools", "frontend", "frontend-avalonia", "examples")) {
        $dir = Join-Path $Root $sourceDir
        if (-not (Test-Path $dir)) { continue }
        Get-ChildItem $dir -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host "    [EN] build state cleared / [ZH] 构建状态已清除" -ForegroundColor Green
}

function Get-ArtifactHashes {
    $lines = New-Object System.Collections.Generic.List[string]
    $named = @(
        "publish\EchoDesk-v$Version-Setup.exe",
        "publish\SHA256SUMS.txt",
        "dist\EchoDesk-v$Version-win-x64.zip",
        "dist\EchoDesk-v$Version-linux-x64.tar.gz",
        "dist\EchoDesk-v$Version-osx-arm64.tar.gz",
        "dist\manifest-win-x64.json",
        "dist\manifest-linux-x64.json",
        "dist\manifest-osx-arm64.json",
        "dist\SHA256SUMS.txt"
    )
    foreach ($rel in $named) {
        $full = Join-Path $Root $rel
        if (-not (Test-Path -LiteralPath $full)) { throw "缺少交付物：$rel" }
        $hash = (Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash.ToLower()
        $lines.Add("$hash  $rel")
    }
    # [EN] Hash every file in the Windows release tree and in the three platform trees. This
    # [EN] catches content drift even when a compressor happens to mask it in an archive.
    # [ZH] 对 Windows 发布树与三个平台目录树中的每个文件计算哈希。即使压缩器偶然在归档中
    # [ZH] 掩盖了内容差异，这一步也能发现漂移。
    $trees = @("publish\EchoDesk-v$Version") + @(
        "dist\EchoDesk-v$Version-win-x64",
        "dist\EchoDesk-v$Version-linux-x64",
        "dist\EchoDesk-v$Version-osx-arm64"
    )
    foreach ($treeRel in $trees) {
        $tree = Join-Path $Root $treeRel
        if (-not (Test-Path -LiteralPath $tree)) { throw "缺少发布目录：$treeRel" }
        $base = (Resolve-Path -LiteralPath $tree).Path
        foreach ($file in Get-ChildItem $tree -Recurse -File | Sort-Object FullName) {
            $rel = $file.FullName.Substring($base.Length).TrimStart('\').Replace('\', '/')
            $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLower()
            $lines.Add("$hash  $treeRel/$rel")
        }
    }
    return $lines
}

Write-Host "== EchoDesk 可复现构建验证 / Reproducible-build verification ==" -ForegroundColor Cyan
Write-Host ("版本 / Version: v{0}　提交 / Commit: {1}" -f $Version, (& git -C $Root rev-parse HEAD))
Write-Host ("SOURCE_DATE_EPOCH: {0}（UTC {1}）" -f $BuildEpoch, $BuildUtc)

# [EN] The build environment is part of the reproducibility claim; verify it before running.
# [ZH] 构建环境是可复现性声明的一部分；运行前先校验。
Invoke-Step "[EN] verifying the build virtual environment / [ZH] 校验构建虚拟环境" {
    & python (Join-Path $PSScriptRoot "check_venv_lock.py")
}

$snapshots = @()
for ($run = 1; $run -le $Runs; $run++) {
    Write-Host ""
    Write-Host ("########## RUN {0}/{1} ##########" -f $run, $Runs) -ForegroundColor Magenta
    Clear-BuildState

    Invoke-Step "[EN] Windows release tree + Setup.exe / [ZH] Windows 发布树与安装程序" {
        & (Join-Path $PSScriptRoot "build_release.ps1") -MakeInstaller
    }
    Invoke-Step "[EN] three standalone backend trees / [ZH] 三个 standalone 后端目录树" {
        & python (Join-Path $PSScriptRoot "pack_backend.py") --platform win-x64 --platform linux-x64 --platform osx-arm64
    }
    Invoke-Step "[EN] three-platform archives / [ZH] 三平台归档" {
        foreach ($rid in @("win-x64", "linux-x64", "osx-arm64")) {
            & python (Join-Path $PSScriptRoot "pack_crossplatform.py") --rid $rid
            if ($LASTEXITCODE -ne 0) { throw "pack_crossplatform.py --rid $rid 失败（退出码 $LASTEXITCODE）" }
        }
    }

    $snapshot = Join-Path $Work ("run-{0}.sha256" -f $run)
    $hashes = Get-ArtifactHashes
    [System.IO.File]::WriteAllLines($snapshot, $hashes, (New-Object System.Text.UTF8Encoding($false)))
    $snapshots += $snapshot
    Write-Host ("    [EN] {0} hashed entries recorded in {1}" -f $hashes.Count, $snapshot) -ForegroundColor Green
    Write-Host ("    [ZH] 已记录 {0} 条哈希：{1}" -f $hashes.Count, $snapshot) -ForegroundColor Green
}

Write-Host ""
Write-Host "== [EN] comparing runs / [ZH] 比对两次构建 ==" -ForegroundColor Cyan
$failed = $false
for ($i = 1; $i -lt $snapshots.Count; $i++) {
    $diff = @(Compare-Object (Get-Content -LiteralPath $snapshots[0]) (Get-Content -LiteralPath $snapshots[$i]))
    if ($diff.Count -gt 0) {
        $failed = $true
        Write-Host ("!! [EN] run 1 and run {0} differ in {1} entries / [ZH] 第 1 次与第 {0} 次构建有 {1} 项不同" -f ($i + 1), $diff.Count) -ForegroundColor Red
        $diff | Select-Object -First 40 | Format-Table -AutoSize
    }
    else {
        Write-Host ("[OK] [EN] run 1 and run {0} are byte-identical / [ZH] 第 1 次与第 {0} 次构建逐字节一致" -f ($i + 1)) -ForegroundColor Green
    }
}
if ($failed) {
    throw "[EN] reproducible-build verification failed / [ZH] 可复现构建验证失败"
}
Write-Host ""
Write-Host "[OK] [EN] all runs are byte-identical / [ZH] 全部构建逐字节一致" -ForegroundColor Green
return 0
