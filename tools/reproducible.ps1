# EchoDesk reproducible-build helpers for PowerShell.
#
# [EN]
# The release chain is driven by PowerShell on Windows, while the individual packagers are
# Python. This file centralises the two rules that must be enforced before any packaging
# step starts: a clean Git worktree and a canonical, UTC-only build timestamp derived from
# the commit time of HEAD (SOURCE_DATE_EPOCH).
#
# [ZH]
# Windows 上的发布链由 PowerShell 驱动，各打包器由 Python 实现。本文件集中实现打包前必须
# 强制执行的两条规则：干净的 Git 工作区，以及由 HEAD 提交时间派生的、仅使用 UTC 的规范
# 构建时间戳（SOURCE_DATE_EPOCH）。
#
# [EN] Dot-source this file; do not execute it directly.
# [ZH] 请以 dot-source 方式加载本文件，不要直接执行。

function Assert-CleanWorktree {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [switch]$AllowDirty
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $status = (& git -C $Root status --porcelain 2>$null) -join "`n"
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($AllowDirty -or $env:ECHODESK_ALLOW_DIRTY_WORKTREE -eq "1") {
        Write-Host "    [EN] WARNING: clean-worktree guard bypassed for a development build." -ForegroundColor Yellow
        Write-Host "    [ZH] 警告：开发构建已显式绕过干净工作区校验。" -ForegroundColor Yellow
        return
    }
    if (-not [string]::IsNullOrWhiteSpace($status)) {
        Write-Host $status -ForegroundColor DarkGray
        throw ("[EN] The Git worktree is not clean; commit or stash every change before packaging.`n" +
               "[ZH] Git 工作区不干净；请在打包前提交或暂存全部改动。")
    }
}

function Initialize-SourceDateEpoch {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [switch]$AllowDirty
    )
    Assert-CleanWorktree -Root $Root -AllowDirty:$AllowDirty
    if ([string]::IsNullOrWhiteSpace($env:SOURCE_DATE_EPOCH)) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $raw = (& git -C $Root log -1 --format=%ct 2>$null | Select-Object -First 1)
        }
        finally {
            $ErrorActionPreference = $previous
        }
        if ([string]::IsNullOrWhiteSpace($raw)) {
            throw ("[EN] Cannot derive SOURCE_DATE_EPOCH: this is not a Git checkout with a commit.`n" +
                   "[ZH] 无法派生 SOURCE_DATE_EPOCH：当前目录不是带提交记录的 Git 检出。")
        }
        $env:SOURCE_DATE_EPOCH = "$raw".Trim()
    }
    $epoch = 0L
    if (-not [long]::TryParse($env:SOURCE_DATE_EPOCH, [ref]$epoch) -or $epoch -lt 0) {
        throw ("[EN] SOURCE_DATE_EPOCH must be a non-negative integer number of seconds: $($env:SOURCE_DATE_EPOCH)`n" +
               "[ZH] SOURCE_DATE_EPOCH 必须为非负整数秒：$($env:SOURCE_DATE_EPOCH)")
    }
    # [EN] Force every child process (dotnet, PyInstaller, pip, ISCC, Python packagers) to
    # [EN] observe the same timestamp. The value is a Unix second count, therefore immune to
    # [EN] the build machine's local time zone and daylight-saving rules.
    # [ZH] 强制所有子进程（dotnet、PyInstaller、pip、ISCC、Python 打包器）看到同一时间戳。
    # [ZH] 该值是 Unix 秒数，因此不受构建机本地时区与夏令时规则影响。
    $env:TZ = "UTC"
    return $epoch
}

function Format-UtcTimestamp {
    param([Parameter(Mandatory = $true)][long]$Epoch)
    return [DateTimeOffset]::FromUnixTimeSeconds($Epoch).UtcDateTime.ToString("yyyy-MM-ddTHH:mm:ssZ")
}

<#
    [EN] Remove a directory or file with the fastest correct primitive for its type.
    [ZH] 按类型选用最快的正确原语删除目录或文件。

    [EN] `Remove-Item -Recurse` walks the whole tree in PowerShell: on the release trees
    (tens of thousands of files, deep dependency directories) it costs noticeably more than
    the native `rd /s /q`. The fallback keeps the function correct where `cmd` is absent.
    [ZH] `Remove-Item -Recurse` 会在 PowerShell 里逐项遍历整棵树；在发布树（数万文件、
    很深的依赖目录）上明显慢于原生 `rd /s /q`。保留兜底分支，使函数在无 `cmd` 的环境下
    依然正确。
#>
function Remove-TreeFast {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) { return }
    if (-not $item.PSIsContainer) {
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        return
    }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & cmd /c rd /s /q "$Path" 2>$null | Out-Null
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue
    }
}
