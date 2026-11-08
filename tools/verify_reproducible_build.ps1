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
# v0.2.5 pipeline optimisations (none of them relaxes the byte comparison):
#   * native `rd /s /q` for build-state cleanup (Remove-TreeFast);
#   * one threaded Python hashing pass instead of ~13,000 Get-FileHash calls per run;
#   * no intermediate `publish/backend-<rid>` archives — the same trees are embedded in
#     the dist archives that are compared.
# The verifier deliberately keeps both PyInstaller analyses cold by default: measured on
# this machine, reusing the analysis cache only saves ~5 s (52.6 s -> 47.4 s), which is not
# worth weakening the independence of the two runs. Pass -ReuseAnalysisCache to opt in.
#
# [ZH]
# 本脚本验证可复现构建的验收条件：同一提交连续执行两次完整流程——前端编译、后端构建与
# 归档生成——必须产出逐字节一致的交付物。脚本在两次运行之间刻意清除全部构建输出与编译器
# 缓存，但保留依赖缓存（轮子、python-build-standalone 归档），因为这些输入已由锁文件
# 固定，且不属于构建输出。
#
# v0.2.5 流水线优化（没有一条放松逐字节比较）：
#   * 清理改用原生 `rd /s /q`（Remove-TreeFast）；
#   * 每次运行只做一次线程化 Python 哈希，替代约 13,000 次 Get-FileHash 调用；
#   * 不再生成中间 `publish/backend-<rid>` 归档——同样的目录树已嵌入参与比较的 dist 归档。
# 验证器默认仍然让两次 PyInstaller 分析都冷跑：本机实测复用分析缓存只省约 5 秒
# （52.6 s → 47.4 s），不值得为此削弱两次运行的独立性。需要时传 -ReuseAnalysisCache。
#
# [EN] Usage / [ZH] 用法：
#   powershell tools\verify_reproducible_build.ps1
#   powershell tools\verify_reproducible_build.ps1 -Runs 2
#   powershell tools\verify_reproducible_build.ps1 -ReuseAnalysisCache   # 更快的开发期检查
param(
    [string]$Version = "",
    [int]$Runs = 2,
    # [EN] Opt-in: let RUN 2 reuse the PyInstaller analysis cache written by RUN 1. Default
    # [EN] is two cold analyses, because the measured saving is only ~10% of PyInstaller's
    # [EN] own runtime and independence is worth more than those seconds.
    # [ZH] 可选：让 RUN 2 复用 RUN 1 写下的 PyInstaller 分析缓存。默认是两次冷分析，
    # [ZH] 因为实测节省仅为 PyInstaller 自身耗时的约 10%，独立性比这几秒更重要。
    [switch]$ReuseAnalysisCache
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
Remove-TreeFast -Path $Work
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
            # [EN] Native recursive delete: measurably faster than Remove-Item -Recurse on
            # [EN] the deep dependency trees (see Remove-TreeFast).
            # [ZH] 原生递归删除：在很深的依赖目录上明显快于 Remove-Item -Recurse
            # [ZH]（见 Remove-TreeFast）。
            Remove-TreeFast -Path $target
        }
    }
    # [EN] Remove Python bytecode caches only inside project sources; dependency caches and
    # [EN] the build virtual environment are deliberately preserved.
    # [ZH] 仅清除项目源码内的 Python 字节码缓存；依赖缓存与构建虚拟环境刻意保留。
    foreach ($sourceDir in @("backend", "tools", "frontend", "frontend-avalonia", "examples")) {
        $dir = Join-Path $Root $sourceDir
        if (-not (Test-Path $dir)) { continue }
        Get-ChildItem $dir -Recurse -Directory -Force -Filter "__pycache__" -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notmatch '\\\.venv\\' } |
            ForEach-Object { Remove-TreeFast -Path $_.FullName }
    }
    Write-Host "    [EN] build state cleared / [ZH] 构建状态已清除" -ForegroundColor Green
}

function Get-ArtifactHashes {
    param([string]$OutFile)
    # [EN] One stdlib hashing pass with a thread pool replaces ~13,000 Get-FileHash calls.
    # [EN] The manifest format is unchanged, so run-to-run comparison semantics are identical.
    # [ZH] 用标准库线程池的一次哈希替代约 13,000 次 Get-FileHash 调用。清单格式不变，
    # [ZH] 因此两次运行之间的比较语义完全一致。
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
    $trees = @(
        "publish\EchoDesk-v$Version",
        "dist\EchoDesk-v$Version-win-x64",
        "dist\EchoDesk-v$Version-linux-x64",
        "dist\EchoDesk-v$Version-osx-arm64"
    )
    $hashArgs = @("--out", $OutFile)
    foreach ($item in $named) { $hashArgs += @("--file", $item) }
    foreach ($item in $trees) { $hashArgs += @("--tree", $item) }
    & python (Join-Path $PSScriptRoot "hash_manifest.py") @hashArgs
    if ($LASTEXITCODE -ne 0) { throw "生成哈希清单失败（退出码 $LASTEXITCODE）" }
}

Write-Host "== EchoDesk 可复现构建验证 / Reproducible-build verification ==" -ForegroundColor Cyan
Write-Host ("版本 / Version: v{0}　提交 / Commit: {1}" -f $Version, (& git -C $Root rev-parse HEAD))
Write-Host ("SOURCE_DATE_EPOCH: {0}（UTC {1}）" -f $BuildEpoch, $BuildUtc)

# [EN] The build environment is part of the reproducibility claim; verify it before running.
# [ZH] 构建环境是可复现性声明的一部分；运行前先校验。
Invoke-Step "[EN] verifying the build virtual environment / [ZH] 校验构建虚拟环境" {
    & python (Join-Path $PSScriptRoot "check_venv_lock.py")
}

# [EN] The PyInstaller analysis cache is cleared before RUN 1 in every mode. By default
# [EN] RUN 2 also runs cold (build_release.ps1 -ColdBuild), preserving two independent
# [EN] analyses; -ReuseAnalysisCache lets RUN 2 reuse RUN 1's TOCs for faster iteration.
# [EN] Even then the stage output (EXE + _internal) is regenerated — only the analysis is reused.
# [ZH] 无论哪种模式，RUN 1 前都会清空 PyInstaller 分析缓存。默认 RUN 2 也是冷分析
# [ZH]（build_release.ps1 -ColdBuild），保持两次分析相互独立；-ReuseAnalysisCache 让 RUN 2
# [ZH] 复用 RUN 1 的 TOC，用于更快的开发期检查。即便如此，stage 产物（EXE + _internal）
# [ZH] 仍会重新生成——复用的只有分析结果。
$analysisCache = Join-Path $Root "artifacts\pyinstaller-cache"
Remove-TreeFast -Path $analysisCache
if ($ReuseAnalysisCache) {
    Write-Host "PyInstaller 分析缓存：RUN 1 冷分析、RUN 2 复用（-ReuseAnalysisCache）" -ForegroundColor Yellow
}
else {
    Write-Host "PyInstaller 分析缓存：两次运行均为冷分析（默认；-ReuseAnalysisCache 可提速开发期检查）" -ForegroundColor DarkGray
}

$snapshots = @()
$verificationStarted = [System.Diagnostics.Stopwatch]::StartNew()
for ($run = 1; $run -le $Runs; $run++) {
    Write-Host ""
    Write-Host ("########## RUN {0}/{1} ##########" -f $run, $Runs) -ForegroundColor Magenta
    Clear-BuildState

    Invoke-Step "[EN] Windows release tree + Setup.exe / [ZH] Windows 发布树与安装程序" {
        $releaseArgs = @{ MakeInstaller = $true }
        # [EN] RUN 1 is always cold because the cache was just removed. RUN 2 is warm only
        # [EN] when the caller explicitly opted in.
        # [ZH] RUN 1 始终冷分析（缓存刚被清空）；只有显式选择时才让 RUN 2 复用。
        if (-not $ReuseAnalysisCache -or $run -eq 1) { $releaseArgs["ColdBuild"] = $true }
        & (Join-Path $PSScriptRoot "build_release.ps1") @releaseArgs
    }
    Invoke-Step "[EN] three standalone backend trees / [ZH] 三个 standalone 后端目录树" {
        # [EN] Intermediate backend archives are not release assets: the same trees are
        # [EN] embedded in the three dist archives compared below, so creating them here
        # [EN] would compress ~400 MB twice per run without adding coverage.
        # [ZH] 中间后端归档不是发布资产：同样的目录树已嵌入下面比较的三个 dist 归档，
        # [ZH] 在这里再生成它们等于每次运行多压缩约 400 MB，却不增加任何覆盖。
        & python (Join-Path $PSScriptRoot "pack_backend.py") --platform win-x64 --platform linux-x64 --platform osx-arm64 --archive none
    }
    Invoke-Step "[EN] three-platform archives / [ZH] 三平台归档" {
        foreach ($rid in @("win-x64", "linux-x64", "osx-arm64")) {
            & python (Join-Path $PSScriptRoot "pack_crossplatform.py") --rid $rid
            if ($LASTEXITCODE -ne 0) { throw "pack_crossplatform.py --rid $rid 失败（退出码 $LASTEXITCODE）" }
        }
    }

    $snapshot = Join-Path $Work ("run-{0}.sha256" -f $run)
    Get-ArtifactHashes -OutFile $snapshot
    $snapshots += $snapshot
    $entries = (Get-Content -LiteralPath $snapshot).Count
    Write-Host ("    [EN] {0} hashed entries recorded in {1}" -f $entries, $snapshot) -ForegroundColor Green
    Write-Host ("    [ZH] 已记录 {0} 条哈希：{1}" -f $entries, $snapshot) -ForegroundColor Green
}
$verificationStarted.Stop()
Write-Host ("`n== [EN] two full runs took {0:N1} s / [ZH] 两次完整构建共耗时 {0:N1} 秒 ==" -f $verificationStarted.Elapsed.TotalSeconds) -ForegroundColor Cyan

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
