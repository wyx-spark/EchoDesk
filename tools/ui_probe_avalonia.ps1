# 适用平台：Avalonia 跨平台前端（三平台通用；Windows 上本机即可跑）
# EchoDesk Avalonia 界面探针 —— 回答"窗口到底能不能打开"
#
# 为什么需要它：v0.2.2 记录 §7.4 明写"Avalonia 前端这套代码此前从未被真正跑起来过"：
# 三条致命缺陷（x:Name 字段全 null、IBrush 强转、设置窗口栈溢出）全是
# "窗口一构造/一显示就死"，而**当时的门禁里没有任何一条会去构造窗口** ——
# tools/ui_probe.ps1 走的是 WPF 发布物（publish\EchoDesk-v0.2.2\app\EchoDesk.exe）。
#
# 这个脚本调用前端自带的 `--probe` 模式（见 frontend-avalonia/.../Probe.cs）：
# 真的构造主窗口与设置窗口、真的显示一帧，然后把结论写成退出码。
# 崩溃（含 Stack overflow）会直接杀死进程 —— 那种情况下**不会有**
# `PROBE-RESULT:` 这一行，本脚本按"无结论"判失败（这是刻意的：栈溢出无法被 try/catch 兜住）。
#
# 用法：
#   powershell -File tools\ui_probe_avalonia.ps1                      # 用 Debug 构建产物
#   powershell -File tools\ui_probe_avalonia.ps1 -Mode settings
#   powershell -File tools\ui_probe_avalonia.ps1 -Mode agent          # 真后端 + Agent 事件链路
#   powershell -File tools\ui_probe_avalonia.ps1 -Mode market         # 真后端 + 模型市场整条交互
#   powershell -File tools\ui_probe_avalonia.ps1 -Mode pages          # 真后端 + 六个设置页的新增功能
#   powershell -File tools\ui_probe_avalonia.ps1 -Mode markdown       # 不开后端：遍历控件树断言 Markdown 渲染结构
#   powershell -File tools\ui_probe_avalonia.ps1 -Mode chat           # 不开后端：断言平铺式消息布局与流式节流
#   powershell -File tools\ui_probe_avalonia.ps1 -Mode upload         # 程序内自拉后端：真实上传两份临时文件
#   powershell -File tools\ui_probe_avalonia.ps1 -Mode theme          # 真的改系统主题，验证跟随系统即时响应（会还原）
#   powershell -File tools\ui_probe_avalonia.ps1 -Mode shots          # 生成 README 用的中英双语截图（2 倍像素、深色主题）
#   powershell -File tools\ui_probe_avalonia.ps1 -Exe <发布树>\app\EchoDesk.exe
param(
    # 默认指向本仓库的 Debug 构建（开发机跑得最快）。发布树里的自包含产物也可以直接传进来，
    # 那条路径才是"发布出去的东西到底能不能开"的证据。
    [string]$Exe = "$PSScriptRoot\..\frontend-avalonia\EchoDesk.Avalonia\bin\Debug\net10.0\EchoDesk.exe",
    [ValidateSet('main', 'settings', 'both', 'agent', 'market', 'pages', 'markdown', 'chat', 'upload', 'theme', 'shots')]
    [string]$Mode = 'both',
    # [EN] Output directory for `-Mode shots`; empty keeps the probe default (docs\images).
    # [ZH] `-Mode shots` 的图片输出目录；留空则用探针默认值（docs\images）。
    [string]$OutDir = '',
    [int]$TimeoutSec = 120
)

# [EN] Shared launcher: never Start-Process (ShellExecute) for the program under test.
# [ZH] 共用启动器：被测程序一律不经 Start-Process（ShellExecute）启动。
. (Join-Path $PSScriptRoot "ProcessTools.ps1")

$ErrorActionPreference = 'Stop'
$failures = New-Object System.Collections.ArrayList
function Fail([string]$msg) { [void]$failures.Add($msg); Write-Output "[avalonia-probe] FAIL $msg" }
function Ok([string]$msg) { Write-Output "[avalonia-probe] OK   $msg" }

# [EN] `shots` renders six 2x PNGs and its own watchdog is 120 s, which is exactly this script's
# [EN] default: a run that finishes right at the boundary would be killed and reported as a hang.
# [ZH] `shots` 要渲染六张 2 倍像素 PNG，它自己的看门狗是 120 秒，正好等于本脚本的默认超时：
# [ZH] 恰好在边界上跑完的那次会被强制结束，并被误报成挂死。
if ($Mode -eq 'shots' -and -not $PSBoundParameters.ContainsKey('TimeoutSec')) { $TimeoutSec = 180 }

if (-not (Test-Path $Exe)) {
    Fail "被测程序不存在：$Exe（先 dotnet build frontend-avalonia\EchoDesk.Avalonia）"
    Write-Output "[avalonia-probe] 失败 $($failures.Count) 项"
    exit 1
}

Write-Output "[avalonia-probe] 被测程序：$Exe"
Write-Output "[avalonia-probe] 模式：$Mode（超时 $TimeoutSec 秒）"

$exit = $null
$probeArgs = @('--probe', $Mode)
if ($OutDir) { $probeArgs += @('--out', $OutDir) }
try {
    # [EN] Launch without ShellExecute; stdout/stderr are drained asynchronously by the helper.
    # [ZH] 不经 ShellExecute 启动；stdout/stderr 由共用启动器异步抽干。
    $proc = Start-NativeApp -Exe $Exe -Arguments $probeArgs -RedirectOutput
    if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
        # 探针自己也有 60 秒看门狗；走到这里说明连看门狗都没能收场（例如启动阶段就卡住）。
        try { $proc.Kill() } catch { }
        Fail "超过 $TimeoutSec 秒仍未退出（已强制结束）"
    }
    else {
        $exit = $proc.ExitCode
    }

    $io = Get-NativeAppOutput -Process $proc
    $stdout = [string]$io[0]
    $stderr = [string]$io[1]
    Write-Output $stdout.TrimEnd()
    if ($stderr -and $stderr.Trim().Length -gt 0) {
        Write-Output "[avalonia-probe] --- stderr ---"
        Write-Output $stderr.TrimEnd()
    }

    # 判定只看"结论行"与退出码：崩溃/栈溢出时结论行不会出现。
    if ($stdout -match 'PROBE-RESULT:\s*PASS') {
        Ok "窗口构造与首帧显示全部通过（结论行已出现）"
    }
    elseif ($stdout -match 'PROBE-RESULT:\s*FAIL') {
        $line = ($stdout -split "`n" | Where-Object { $_ -match 'PROBE-RESULT:\s*FAIL' } | Select-Object -First 1).Trim()
        Fail "探针自报失败：$line"
    }
    else {
        Fail "进程结束但没有结论行（退出码 $exit）—— 这类进程级崩溃（如 Stack overflow）无法被 try/catch 兜住，通常就是它"
    }

    if ($null -ne $exit -and $exit -ne 0) { Fail "退出码非 0：$exit" }
}
finally {
    Start-Sleep -Milliseconds 200   # 让子进程真正释放管道句柄
}

Write-Output ""
if ($failures.Count -eq 0) {
    Write-Output "[avalonia-probe] 全部通过"
    exit 0
}
Write-Output "[avalonia-probe] 失败 $($failures.Count) 项"
exit 1



