# [EN] Exclusive lock for the release pipeline (PowerShell side). Packaging and the release gate
# [EN] must never run at the same time; both touch publish/ and dist/. Dot-source this file and
# [EN] call Enter-PipelineLock -Role <name> / Exit-PipelineLock.
# [ZH] 发布流水线的互斥锁（PowerShell 侧）。打包与发布门禁不得同时运行；两者都读写 publish/ 与 dist/。
# [ZH] 用 `. <路径>` 引入本文件后调用 Enter-PipelineLock -Role <名称> / Exit-PipelineLock。
$script:PipelineLockPath = Join-Path $PSScriptRoot "..\artifacts\.pipeline.lock"
$script:PipelineLockOwned = $false

function Enter-PipelineLock([string]$Role) {
    # [EN] Children of the lock owner inherit this marker (the gate runs its checks through a
    # [EN] wrapper shell, so the parent-PID test cannot see the gate); they must not re-acquire.
    # [ZH] 锁持有者的子进程会继承此标记（门禁通过包装 shell 跑检查，父 PID 检测看不到门禁本身），
    # [ZH] 不得重复抢锁。
    if ($env:ECHODESK_PIPELINE_LOCK_INHERITED -eq "1") { $script:PipelineLockOwned = $false; return }
    $dir = Split-Path -Parent $script:PipelineLockPath
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    for ($i = 0; $i -lt 2; $i++) {
        try {
            $stream = [System.IO.File]::Open($script:PipelineLockPath,
                [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::None)
        }
        catch [System.IO.IOException] {
            $owner = $null
            try { $owner = Get-Content -LiteralPath $script:PipelineLockPath -Raw | ConvertFrom-Json } catch { }
            if ($owner -and $owner.pid) {
                # [EN] Nested entry: our parent holds the lock (gate/build_release -> child script).
                # [ZH] 嵌套入口：父进程持锁（门禁/build_release -> 子脚本），放行且不接管所有权。
                $parentPid = 0
                try {
                    $parentPid = [int](Get-CimInstance Win32_Process -Filter "ProcessId=$PID").ParentProcessId
                } catch { $parentPid = 0 }
                if ($parentPid -and [int]$owner.pid -eq $parentPid) {
                    $script:PipelineLockOwned = $false
                    return
                }
            }
            $alive = $false
            if ($owner -and $owner.pid) {
                $proc = Get-Process -Id ([int]$owner.pid) -ErrorAction SilentlyContinue
                $alive = $null -ne $proc
            }
            if ($alive) {
                throw ("流水线被占用：pid={0} role={1} started_at={2}；打包与门禁不得并发运行。" -f `
                       $owner.pid, $owner.role, $owner.started_at)
            }
            # [EN] Stale lock from a crashed process: take it over.
            # [ZH] 崩溃进程留下的锁：接管。
            Remove-Item -LiteralPath $script:PipelineLockPath -Force -ErrorAction SilentlyContinue
            continue
        }
        $writer = New-Object System.IO.StreamWriter($stream, (New-Object System.Text.UTF8Encoding($false)))
        $writer.Write((@{ pid = $PID; role = $Role; host = $env:COMPUTERNAME
                          started_at = (Get-Date).ToString("o") } | ConvertTo-Json -Compress))
        $writer.Flush(); $writer.Dispose(); $stream.Dispose()
        $script:PipelineLockOwned = $true
        return
    }
    throw "无法获取流水线锁（连续两次被占用）：$script:PipelineLockPath"
}

function Exit-PipelineLock() {
    if (-not $script:PipelineLockOwned) { return }
    try {
        $owner = Get-Content -LiteralPath $script:PipelineLockPath -Raw | ConvertFrom-Json
        if ([int]$owner.pid -eq $PID) {
            Remove-Item -LiteralPath $script:PipelineLockPath -Force -ErrorAction SilentlyContinue
        }
    }
    catch { }
    $script:PipelineLockOwned = $false
}

# [EN] Best effort: release on normal session end even if a path forgot to call it.
# [ZH] 兜底：即使某条路径忘了显式释放，正常结束时也释放。
Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action { Exit-PipelineLock } | Out-Null
