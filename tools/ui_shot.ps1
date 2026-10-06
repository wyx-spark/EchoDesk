# EchoDesk 界面截图取证工具（研发验证用）
#
# 用途：把主窗口 / 设置窗口截成 PNG，供人工（或视觉复核）逐屏检查
#       —— 深色模式是否正确生效、文字与背景对比度是否可辨、
#       emoji 是否渲染成方块。这些是 UIA 探针读不出来的。
#
# 用法：
#   powershell -File tools\ui_shot.ps1                      # 默认发布物，浅色 + 深色 + 设置窗口
#   powershell -File tools\ui_shot.ps1 -Exe <path> -Out <dir>
#   powershell -File tools\ui_shot.ps1 -SkipLaunch          # 只对已在运行的进程截图
param(
    [string]$Exe = "$PSScriptRoot\..\publish\EchoDesk-v0.2.2\app\EchoDesk.exe",
    [string]$Out = "$PSScriptRoot\..\artifacts\ui-shots",
    [int]$ReadyTimeoutSec = 180,
    [switch]$SkipLaunch
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type -Namespace Shot -Name Win32 -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr hWnd);
[DllImport("user32.dll")] public static extern bool GetWindowRect(System.IntPtr hWnd, out RECT lpRect);
[DllImport("user32.dll")] public static extern bool PrintWindow(System.IntPtr hWnd, System.IntPtr hdcBlt, uint nFlags);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(System.IntPtr hWnd);
[DllImport("user32.dll")] public static extern int GetWindowTextLength(System.IntPtr hWnd);
[DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(System.IntPtr hWnd, System.Text.StringBuilder s, int n);
[DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
[StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
'@

[void][Shot.Win32]::SetProcessDPIAware()
New-Item -ItemType Directory -Force -Path $Out | Out-Null

function Get-TopWindows([int]$procId) {
    $out = New-Object System.Collections.ArrayList
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $procId)
    $all = [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
        [System.Windows.Automation.TreeScope]::Children, $cond)
    foreach ($w in $all) { [void]$out.Add($w) }
    return $out
}

function Find-ById([int]$procId, [string]$id) {
    $a = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $procId)
    $b = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty, $id)
    $and = New-Object System.Windows.Automation.AndCondition($a, $b)
    return [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
        [System.Windows.Automation.TreeScope]::Descendants, $and)
}

function Save-WindowShot([System.Windows.Automation.AutomationElement]$win, [string]$name) {
    $hwnd = [System.IntPtr]$win.Current.NativeWindowHandle
    if ($hwnd -eq [System.IntPtr]::Zero) { Write-Output "[shot] 跳过 $name：无 HWND"; return $null }
    [void][Shot.Win32]::ShowWindow($hwnd, 5)
    [void][Shot.Win32]::SetForegroundWindow($hwnd)
    Start-Sleep -Milliseconds 600
    $r = New-Object Shot.Win32+RECT
    [void][Shot.Win32]::GetWindowRect($hwnd, [ref]$r)
    $w = $r.Right - $r.Left; $h = $r.Bottom - $r.Top
    if ($w -le 0 -or $h -le 0) { Write-Output "[shot] 跳过 $name：窗口尺寸异常 $w x $h"; return $null }
    $bmp = New-Object System.Drawing.Bitmap($w, $h)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $hdc = $g.GetHdc()
    # PW_RENDERFULLCONTENT=2：WPF 走 DirectX 合成，必须用它才能截到内容
    $ok = [Shot.Win32]::PrintWindow($hwnd, $hdc, 2)
    $g.ReleaseHdc($hdc)
    if (-not $ok) {
        # 兜底：直接抓屏（窗口必须在前台且未被遮挡）
        $g.CopyFromScreen($r.Left, $r.Top, 0, 0, (New-Object System.Drawing.Size($w, $h)))
    }
    $g.Dispose()
    $path = Join-Path $Out "$name.png"
    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    Write-Output "[shot] $name -> $path ($w x $h)"
    return $path
}

function Invoke-El($el) {
    $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
}

# ---------- 启动 ----------
$proc = $null
if (-not $SkipLaunch) {
    foreach ($p in Get-Process -Name EchoDesk -ErrorAction SilentlyContinue) { $p.Kill(); Start-Sleep -Milliseconds 300 }
    $exePath = (Resolve-Path -LiteralPath $Exe).Path
    $proc = Start-Process -FilePath $exePath -WorkingDirectory (Split-Path -Parent (Split-Path -Parent $exePath)) -PassThru
    Write-Output "[shot] 启动 $exePath (pid=$($proc.Id))"
} else {
    $proc = Get-Process -Name EchoDesk -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $proc) { throw "没有正在运行的 EchoDesk 进程" }
}

# ---------- 等主窗口就绪 ----------
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$mainWin = $null
while ($sw.Elapsed.TotalSeconds -lt $ReadyTimeoutSec) {
    $mainWin = Get-TopWindows($proc.Id) | Where-Object { $_.Current.Name -like 'EchoDesk*' } | Select-Object -First 1
    if ($null -ne $mainWin) { break }
    Start-Sleep -Milliseconds 400
}
if ($null -eq $mainWin) { throw "等待主窗口超时（$ReadyTimeoutSec s）" }

# 等后端就绪：状态栏离开「正在启动」即可截图
$status = Find-ById $proc.Id 'StatusText'
$sw.Restart()
while ($sw.Elapsed.TotalSeconds -lt $ReadyTimeoutSec) {
    if ($null -ne $status -and $status.Current.Name -notmatch '正在启动|Starting|Connecting|连接') { break }
    Start-Sleep -Milliseconds 500
    $status = Find-ById $proc.Id 'StatusText'
}
Start-Sleep -Seconds 2   # 等欢迎语渲染完
Write-Output "[shot] 后端状态：$($status.Current.Name)"

$shots = New-Object System.Collections.ArrayList
[void]$shots.Add((Save-WindowShot $mainWin '01-main-light'))

# ---------- 设置窗口（浅色） ----------
$settingsBtn = Find-ById $proc.Id 'SettingsButton'
if ($null -ne $settingsBtn) {
    Invoke-El $settingsBtn
    Start-Sleep -Milliseconds 900
    $setWin = Get-TopWindows($proc.Id) | Where-Object { $_.Current.Name -notlike 'EchoDesk*v*' } |
              Where-Object { $_.Current.NativeWindowHandle -ne $mainWin.Current.NativeWindowHandle } |
              Select-Object -First 1
    if ($null -ne $setWin) {
        [void]$shots.Add((Save-WindowShot $setWin '02-settings-light'))
        # 逐 Tab 截图（TabItem 的 SelectionItemPattern）
        $tabs = $setWin.FindAll([System.Windows.Automation.TreeScope]::Descendants,
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::TabItem)))
        $i = 0
        foreach ($t in $tabs) {
            $i++
            try {
                $t.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
                Start-Sleep -Milliseconds 500
                [void]$shots.Add((Save-WindowShot $setWin ("02-settings-light-tab$i")))
            } catch { Write-Output "[shot] tab$i 选择失败：$($_.Exception.Message)" }
        }
        $setWin.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern).Close()
        Start-Sleep -Milliseconds 500
    } else { Write-Output "[shot] 未找到设置窗口" }
} else { Write-Output "[shot] 未找到 SettingsButton（跳过设置窗口截图）" }

# ---------- 切深色：直接改 ui_state.json 后重启，走真实启动路径 ----------
$appDir = Split-Path -Parent $exePath
$stateFile = Join-Path (Split-Path -Parent $appDir) 'ui_state.json'
if (-not (Test-Path $stateFile)) { $stateFile = Join-Path $appDir 'ui_state.json' }
if (Test-Path $stateFile) {
    $state = Get-Content $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $state.DarkMode = $true
    $state | ConvertTo-Json -Depth 5 | Set-Content $stateFile -Encoding UTF8
    Write-Output "[shot] 已把 $stateFile 的 DarkMode 设为 true，重启截图"
    $proc.Kill(); Start-Sleep -Seconds 1
    $proc = Start-Process -FilePath $exePath -WorkingDirectory (Split-Path -Parent $appDir) -PassThru
    $sw.Restart()
    $mainWin = $null
    while ($sw.Elapsed.TotalSeconds -lt $ReadyTimeoutSec) {
        $mainWin = Get-TopWindows($proc.Id) | Where-Object { $_.Current.Name -like 'EchoDesk*' } | Select-Object -First 1
        if ($null -ne $mainWin) { break }
        Start-Sleep -Milliseconds 400
    }
    $sw.Restart()
    while ($sw.Elapsed.TotalSeconds -lt $ReadyTimeoutSec) {
        $s = Find-ById $proc.Id 'StatusText'
        if ($null -ne $s -and $s.Current.Name -notmatch '正在启动|Starting') { break }
        Start-Sleep -Milliseconds 500
    }
    Start-Sleep -Seconds 2
    [void]$shots.Add((Save-WindowShot $mainWin '10-main-dark'))
    $settingsBtn = Find-ById $proc.Id 'SettingsButton'
    if ($null -ne $settingsBtn) {
        Invoke-El $settingsBtn
        Start-Sleep -Milliseconds 900
        $setWin = Get-TopWindows($proc.Id) | Where-Object { $_.Current.Name -notlike 'EchoDesk*v*' } |
                  Where-Object { $_.Current.NativeWindowHandle -ne $mainWin.Current.NativeWindowHandle } |
                  Select-Object -First 1
        if ($null -ne $setWin) {
            [void]$shots.Add((Save-WindowShot $setWin '11-settings-dark'))
            $tabs = $setWin.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                (New-Object System.Windows.Automation.PropertyCondition(
                    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                    [System.Windows.Automation.ControlType]::TabItem)))
            $i = 0
            foreach ($t in $tabs) {
                $i++
                try {
                    $t.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
                    Start-Sleep -Milliseconds 500
                    [void]$shots.Add((Save-WindowShot $setWin ("11-settings-dark-tab$i")))
                } catch { }
            }
            $setWin.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern).Close()
        }
    }
} else { Write-Output "[shot] 未找到 ui_state.json，跳过深色截图" }

if (-not $SkipLaunch) { $proc.Kill() }
Write-Output "[shot] 完成，共 $($shots.Count) 张：$Out"
