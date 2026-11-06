# 适用平台：Windows（WPF 与 Avalonia 都在真窗口上截）
# EchoDesk 视觉对齐取证：把 WPF / Avalonia 两个前端按**同一套窗口、同一套路径**截图，
# 供人工（多模态视觉）逐页比对间距 / 边距 / 字号 / 对齐 / 配色 / 圆角。
#
# 为什么需要它：既有 tools\ui_shot.ps1 与 --probe sweep 都只截**自己那一边**
# （前者 WPF、后者 Avalonia 且强制英文），两边的取证口径不同：
#   * 窗口尺寸不同（WPF 主窗口 860x620、Avalonia 1080x760）；
#   * 截图方式不同（PrintWindow 全窗口 vs RenderTargetBitmap 客户区）；
#   * 语言不同（一个中文一个英文）。
# 口径不同就无法逐像素比对，所以这里统一：
#   同一份隔离数据目录（ECHODESK_DATA）+ 中文 + 同一 DPI + 同一种 PrintWindow 截法。
#
# 用法：
#   powershell -File tools\ui_pair_shot.ps1 -Flavor wpf
#   powershell -File tools\ui_pair_shot.ps1 -Flavor avalonia
#   powershell -File tools\ui_pair_shot.ps1 -Flavor avalonia -Exe <自建产物>
# 产物：artifacts\align\<flavor>\main.png、settings-01-general.png … settings-07-developer.png
param(
    [Parameter(Mandatory = $true)][ValidateSet('wpf', 'avalonia')][string]$Flavor,
    [string]$Exe = '',
    [string]$Out = '',
    [int]$TimeoutSec = 180
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type -Namespace AlignShot -Name Win32 -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr hWnd);
[DllImport("user32.dll")] public static extern bool GetWindowRect(System.IntPtr hWnd, out RECT lpRect);
[DllImport("user32.dll")] public static extern bool GetClientRect(System.IntPtr hWnd, out RECT lpRect);
[DllImport("user32.dll")] public static extern bool PrintWindow(System.IntPtr hWnd, System.IntPtr hdcBlt, uint nFlags);
[DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
[DllImport("user32.dll")] public static extern bool MoveWindow(System.IntPtr hWnd, int X, int Y, int nWidth, int nHeight, bool bRepaint);
[StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
'@

[void][AlignShot.Win32]::SetProcessDPIAware()

$Root = Split-Path -Parent $PSScriptRoot
# [EN] The Windows release directory is derived from backend\version.py; a hard-coded version
#      would make the comparison capture an obsolete build.
# [ZH] Windows 发布目录由 backend\version.py 派生；写死版本号会使对照截图采集到过期的构建。
$pairVersion = ""
$pairVersionFile = Join-Path $Root "backend\version.py"
$pairVersionMatch = Select-String -Path $pairVersionFile -Pattern 'APP_VERSION\s*=\s*["'']([^"'']+)["'']' |
                     Select-Object -First 1
if ($pairVersionMatch) { $pairVersion = $pairVersionMatch.Matches[0].Groups[1].Value.TrimStart('v') }
$wpfExe = if ($pairVersion) {
    Join-Path $Root "publish\EchoDesk-v$pairVersion\app\EchoDesk.exe"
} else {
    Join-Path $Root 'publish\EchoDesk-v0.2.2\app\EchoDesk.exe'
}
if (-not $Exe) {
    if ($Flavor -eq 'wpf') { $Exe = $wpfExe }
    else { $Exe = Join-Path $Root 'frontend-avalonia\EchoDesk.Avalonia\bin\Debug\net10.0\EchoDesk.exe' }
}
if (-not (Test-Path $Exe)) { Write-Output "[align] FAIL 找不到被测程序：$Exe"; exit 1 }
$Exe = (Resolve-Path $Exe).Path
if (-not $Out) { $Out = Join-Path $Root "artifacts\align\$Flavor" }
New-Item -ItemType Directory -Force -Path $Out | Out-Null

# 隔离数据目录：两个前端都必须从**同一份** ui_state 起跑（中文、浅色、非首启），
# 否则截出来的主题/语言不同，比对没有意义。
# 每次都**清空重建**：窗口尺寸/折叠状态会被记忆，残留会让这一轮截到上一轮的尺寸。
$DataDir = Join-Path $Root "artifacts\align\state-$Flavor"
if (Test-Path $DataDir) { Remove-Item -Recurse -Force $DataDir }
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$statePath = Join-Path $DataDir 'ui_state.json'
# 两个前端读 ui_state.json 的键名大小写不同（WPF 用 Language/Theme/DarkMode，
# Avalonia 用 language/theme/font_size…）。同一份夹具里两种都写，保证两边起跑状态一致。
# 注意：PowerShell 的哈希表键**大小写不敏感**，"Language" 与 "language" 会撞成重复键，
# 所以这里直接写 JSON 文本，不用 @{ } 转。
$stateJson = @'
{
  "Language": "zh-Hans", "language": "zh-Hans",
  "Theme": "light", "theme": "light",
  "DarkMode": false, "dark_mode": false,
  "FirstRunDone": true, "first_run_done": true,
  "ShortcutPromptDeclined": true,
  "font_size": 14
}
'@
$stateJson | Set-Content -Path $statePath -Encoding utf8
$env:ECHODESK_DATA = $DataDir

# 两边必须连**同一份后端**，否则截出来的状态区/模型胶囊是两份数据（一个"无模型"、
# 一个"Q6_K 已加载"），根本没法比。Avalonia 开发构建默认会向上找到仓库里的 backend\，
# 那份 config 与发布树不同 —— 这里显式指到发布树的后端，与 WPF 那一路完全一致。
if ($Flavor -eq 'avalonia') {
    $sharedBackend = Join-Path (Split-Path -Parent $wpfExe) 'backend\EchoDeskBackend.exe'
    if (Test-Path -LiteralPath $sharedBackend) {
        $env:ECHODESK_BACKEND = $sharedBackend
        Write-Output "[align] 后端统一为发布树：$sharedBackend"
    }
}

# 会话历史也要清空：两边后端各自把 history 存在 session.json 里，
# 不清的话截图里会带上一轮探针留下的十几轮对话，消息区就看不出布局差异了。
# 只清 history、保留其它字段（mode / model_path 等），跑完原样写回。
$sessionBackups = @{}
foreach ($cand in @((Join-Path (Split-Path -Parent $Exe) 'backend\session.json'),
                    (Join-Path $Root 'backend\session.json'))) {
    if (-not (Test-Path -LiteralPath $cand)) { continue }
    $raw = Get-Content -LiteralPath $cand -Raw -Encoding UTF8
    $sessionBackups[$cand] = $raw
    try {
        $obj = $raw | ConvertFrom-Json
        $obj.history = @()
        ($obj | ConvertTo-Json -Depth 20) | Set-Content -LiteralPath $cand -Encoding utf8
        Write-Output "[align] 已清空会话历史：$cand"
    } catch { Write-Output "[align] 警告：清空会话失败 $cand：$($_.Exception.Message)" }
}

$failures = New-Object System.Collections.ArrayList
function Fail([string]$m) { [void]$failures.Add($m); Write-Output "[align] FAIL $m" }
function Info([string]$m) { Write-Output "[align] $m" }

function Find-ById([int]$procId, [string]$id) {
    $a = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $procId)
    $b = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty, $id)
    return [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
        [System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.AndCondition($a, $b)))
}

function Get-OwnerWindow($el) {
    if ($null -eq $el) { return $null }
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $node = $el
    while ($null -ne $node) {
        if ($node.Current.ControlType -eq [System.Windows.Automation.ControlType]::Window) { return $node }
        $node = $walker.GetParent($node)
    }
    return $null
}

# PrintWindow(PW_RENDERFULLCONTENT=2) 是全窗口（含非客户区）截图，WPF 走 DirectX 合成时靠它。
# 兜底顺序：PrintWindow → 抓屏。抓屏要求窗口在前台且没被挡，所以前面先 SetForegroundWindow。
function Save-WindowShot($win, [string]$name) {
    $hwnd = [System.IntPtr]$win.Current.NativeWindowHandle
    if ($hwnd -eq [System.IntPtr]::Zero) { Fail "$name 无 HWND"; return $null }
    [void][AlignShot.Win32]::ShowWindow($hwnd, 5)
    [void][AlignShot.Win32]::SetForegroundWindow($hwnd)
    Start-Sleep -Milliseconds 700
    $r = New-Object AlignShot.Win32+RECT
    [void][AlignShot.Win32]::GetWindowRect($hwnd, [ref]$r)
    $w = $r.Right - $r.Left; $h = $r.Bottom - $r.Top
    if ($w -le 0 -or $h -le 0) { Fail "$name 尺寸异常 $w x $h"; return $null }
    $bmp = New-Object System.Drawing.Bitmap($w, $h)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $hdc = $g.GetHdc()
    $ok = [AlignShot.Win32]::PrintWindow($hwnd, $hdc, 2)
    $g.ReleaseHdc($hdc)
    if (-not $ok) {
        Info "$name PrintWindow 失败，改用抓屏"
        $g.CopyFromScreen($r.Left, $r.Top, 0, 0, (New-Object System.Drawing.Size($w, $h)))
    }
    $g.Dispose()
    $path = Join-Path $Out "$name.png"
    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    # 一像素级"是不是全黑/全白"自检：Avalonia 走 GPU 合成时 PrintWindow 可能只截到黑框，
    # 那种图看不出任何差异，留着会让后面每一轮比对都白做。
    $probe = @()
    for ($y = [int]($h * 0.15); $y -lt [int]($h * 0.85); $y += [math]::Max(1, [int]($h / 20))) {
        for ($x = [int]($w * 0.15); $x -lt [int]($w * 0.85); $x += [math]::Max(1, [int]($w / 20))) {
            $probe += $bmp.GetPixel($x, $y)
        }
    }
    $distinct = ($probe | ForEach-Object { "$($_.R),$($_.G),$($_.B)" } | Sort-Object -Unique).Count
    $bmp.Dispose()
    Write-Output "[align] $name -> $path ($w x $h, 采样色数 $distinct)"
    if ($distinct -le 2) { Fail "$name 截图几乎纯色（采样色数 $distinct），截图链路可能没截到内容" }
    return $path
}

$proc = $null
try {
    Info "被测程序：$Exe"
    $proc = Start-Process -FilePath $Exe -WorkingDirectory (Split-Path -Parent $Exe) -PassThru
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $mainEl = $null
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 400
        $mainEl = Find-ById $proc.Id 'StatusText'
        if ($null -ne $mainEl) { break }
        if ($proc.HasExited) { Fail "进程已退出（退出码 $($proc.ExitCode)）"; break }
    }
    if ($null -eq $mainEl) { Fail "主窗口未出现"; exit 1 }

    # 等后端状态离开"开始/连接"：状态区文字会从"正在启动"变成"就绪"，
    # 两者高度可能不同（能力提示多一行），不等它稳定截出来会和下一页错位。
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
        $t = (Find-ById $proc.Id 'StatusText').Current.Name
        if ($t -match '就绪|Ready|失败|Error|未就绪') { break }
        Start-Sleep -Milliseconds 500
    }
    Start-Sleep -Seconds 2
    $mainWin = Get-OwnerWindow (Find-ById $proc.Id 'StatusText')
    if ($null -eq $mainWin) { Fail '取不到主窗口'; exit 1 }
    Info "主窗口：$($mainWin.Current.Name)"
    (Save-WindowShot $mainWin 'main')

    $setBtn = Find-ById $proc.Id 'SettingsButton'
    if ($null -eq $setBtn) { Fail '未找到 SettingsButton'; }
    else {
        try { $setBtn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
        catch { Fail "设置按钮点击失败：$($_.Exception.Message)" }
        $setWin = $null
        $sd = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $sd) {
            Start-Sleep -Milliseconds 400
            $tabs = Find-ById $proc.Id 'Tabs'
            if ($null -ne $tabs) { $setWin = Get-OwnerWindow $tabs; break }
        }
        if ($null -eq $setWin) { Fail '设置窗口未出现（30s 内找不到 Tabs）' }
        else {
            Info "设置窗口：$($setWin.Current.Name)"
            Start-Sleep -Milliseconds 1200
            $names = @('general', 'model', 'agent', 'automation', 'market', 'installed', 'developer')
            $tabCond = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::TabItem)
            $tabEls = $setWin.FindAll([System.Windows.Automation.TreeScope]::Descendants, $tabCond)
            Info "设置窗口 Tab 数：$($tabEls.Count)"
            $count = [math]::Min($names.Count, $tabEls.Count)
            for ($i = 0; $i -lt $count; $i++) {
                $tab = $tabEls.Item($i)
                try {
                    $tab.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
                } catch { Fail "第 $($i + 1) 个页签选不中：$($_.Exception.Message)"; continue }
                Start-Sleep -Milliseconds 900
                (Save-WindowShot $setWin ("settings-{0:00}-{1}" -f ($i + 1), $names[$i]))
            }
            try { $setWin.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern).Close() } catch { }
        }
    }
}
finally {
    if ($proc -and -not $proc.HasExited) { try { $proc.Kill() } catch { } }
    Get-Process EchoDeskBackend -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    foreach ($k in $sessionBackups.Keys) {
        try { [System.IO.File]::WriteAllText($k, $sessionBackups[$k], (New-Object System.Text.UTF8Encoding($false))) }
        catch { Write-Output "[align] 警告：恢复会话失败 $k" }
    }
    Remove-Item Env:\ECHODESK_DATA -ErrorAction SilentlyContinue
    Remove-Item Env:\ECHODESK_BACKEND -ErrorAction SilentlyContinue
}

Write-Output "[align] 截图目录：$Out"
if ($failures.Count -gt 0) {
    Write-Output "[align] ✗ 失败 $($failures.Count) 项"
    foreach ($f in $failures) { Write-Output "   - $f" }
    exit 1
}
Write-Output '[align] ✓ 截图完成'
exit 0
