# [EN] Native process launcher shared by every UI probe. It deliberately avoids
# [EN] ``Start-Process`` (which goes through ShellExecute): on this machine ShellExecute
# [EN] can raise "Open File - Security Warning / the publisher could not be verified" from the
# [EN] Attachment Manager, which blocks the probe invisibly until its timeout. Files themselves are
# [EN] fine (no MOTW, local path) -- only the launch API triggers it.
# [ZH] 所有界面探针共用的原生进程启动器。它刻意不用 ``Start-Process``（那是 ShellExecute 路径）：
# [ZH] 本机上 ShellExecute 会触发附件管理器的「打开文件 - 安全警告 / 无法验证发布者」，把探针
# [ZH] 无声地卡到超时。文件本身没有问题（无 MOTW、本地路径），触发者只是启动方式。
$script:NativeAppIo = @{}

$script:BlockingDialogPattern = '安全警告|无法验证发布者|Windows 已保护|SmartScreen|用户帐户控制'

function ConvertTo-NativeArgumentString([string[]]$Arguments) {
    return (($Arguments | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }) -join ' ')
}

function Find-BlockingDialog {
    # [EN] Returns the first top-level window that looks like a blocking system dialog, or $null.
    # [ZH] 返回第一个看起来像阻塞式系统对话框的顶层窗口；没有则返回 $null。
    foreach ($candidate in @(Get-Process -ErrorAction SilentlyContinue)) {
        $title = ''
        try { $title = [string]$candidate.MainWindowTitle } catch { continue }
        if (-not $title) { continue }
        if ($title -match $script:BlockingDialogPattern) {
            return [pscustomobject]@{
                Title = $title; Process = $candidate.ProcessName; Id = $candidate.Id
            }
        }
    }
    return $null
}

function Assert-NoBlockingDialog {
    $bad = Find-BlockingDialog
    if ($null -ne $bad) {
        throw ("检测到阻塞式系统对话框：'{0}'（进程 {1}，pid {2}）。启动路径不得使用 " +
               "Start-Process/ShellExecute；已按可视性缺陷判失败。" -f `
               $bad.Title, $bad.Process, $bad.Id)
    }
}

function Start-NativeApp {
    # [EN] Launch ``$Exe`` without ShellExecute. With ``-RedirectOutput`` both streams are drained
    # [EN] asynchronously (``ReadToEndAsync``) so a full pipe can never deadlock the child; call
    # [EN] Get-NativeAppOutput after the process exits to collect them.
    # [ZH] 不经 ShellExecute 启动 ``$Exe``。``-RedirectOutput`` 时两个流都用 ``ReadToEndAsync``
    # [ZH] 异步抽干，管道写满不会造成假死；进程退出后调用 Get-NativeAppOutput 取回输出。
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory = '',
        [switch]$RedirectOutput,
        # [EN] Short startup window during which a system dialog is treated as a hard failure.
        # [ZH] 启动后的短暂窗口期内，出现系统对话框即判硬失败。
        [int]$StartupGuardSec = 8
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    if ($Arguments.Count -gt 0) { $psi.Arguments = ConvertTo-NativeArgumentString $Arguments }
    if ($WorkingDirectory) { $psi.WorkingDirectory = $WorkingDirectory }
    $psi.UseShellExecute = $false          # 关键：绕开 ShellExecute / Attachment Manager
    $psi.CreateNoWindow = $true
    if ($RedirectOutput) {
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
    }
    $proc = [System.Diagnostics.Process]::Start($psi)
    if ($RedirectOutput) {
        $script:NativeAppIo[$proc.Id] = @(
            $proc.StandardOutput.ReadToEndAsync(),
            $proc.StandardError.ReadToEndAsync())
    }
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    while ($watch.Elapsed.TotalSeconds -lt $StartupGuardSec) {
        Assert-NoBlockingDialog
        if ($proc.HasExited) { break }
        Start-Sleep -Milliseconds 500
    }
    return $proc
}

function Get-NativeAppOutput {
    # [EN] ``[stdout, stderr]`` collected by Start-NativeApp -RedirectOutput.
    # [ZH] 由 Start-NativeApp -RedirectOutput 收集的 ``[stdout, stderr]``。
    param([Parameter(Mandatory = $true)]$Process)
    $io = $script:NativeAppIo[$Process.Id]
    if (-not $io) { return @('', '') }
    try {
        return @($io[0].GetAwaiter().GetResult(), $io[1].GetAwaiter().GetResult())
    }
    catch {
        return @('', '')
    }
}
