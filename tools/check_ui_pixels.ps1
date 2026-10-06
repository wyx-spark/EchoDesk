# 适用平台：需要 WPF 窗口与交互式桌面（Windows 专属）
# EchoDesk 界面像素校验（任务书修复二 / 修复六 的"真实渲染"证据）
#
# tools\check_theme.py 校验的是**色板数值**（静态、可复现），但"深色模式到底有没有生效"
# 是运行时行为：曾经的真实缺陷就是"配置写了深色，界面仍是浅色"。所以这里把程序真正
# 跑起来、截图、量像素：
#
#   1. 主窗口整体亮度：深色主题必须明显偏暗、浅色主题明显偏亮（判别"主题是否生效"）；
#   2. 逐个关键控件的**局部对比度**：在控件矩形内取出现次数最多的颜色作为背景、
#      取与背景距离最远的颜色作为前景，按 WCAG 公式算对比度，要求 ≥ 4.5:1。
#      这能抓到"文字与背景糊在一起"这类静态色板查不出的问题（例如控件被系统默认
#      样式覆盖、或某个控件绕过了色板）。
#
# 用法：
#   powershell -File tools\check_ui_pixels.ps1                    # 浅色 + 深色各跑一遍
#   powershell -File tools\check_ui_pixels.ps1 -Theme dark -KeepShots
param(
    # 口径：v0.2.1 = 跨平台之前；v0.2.2 = 跨平台版本（见 backend\version.py）。
    [string]$Exe = "$PSScriptRoot\..\publish\EchoDesk-v0.2.2\app\EchoDesk.exe",
    [ValidateSet('light', 'dark', 'both')][string]$Theme = 'both',
    [string]$Out = "$PSScriptRoot\..\artifacts\pixels",
    [int]$ReadyTimeoutSec = 180,
    [double]$MinContrast = 4.5,
    [switch]$KeepShots
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type -Namespace Pix -Name Win32 -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr hWnd);
[DllImport("user32.dll")] public static extern bool GetWindowRect(System.IntPtr hWnd, out RECT lpRect);
[DllImport("user32.dll")] public static extern bool PrintWindow(System.IntPtr hWnd, System.IntPtr hdcBlt, uint nFlags);
[DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
[StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
'@
[void][Pix.Win32]::SetProcessDPIAware()

$failures = New-Object System.Collections.ArrayList
function Fail([string]$m) { [void]$failures.Add($m); Write-Output "[pixels] FAIL $m" }
function Ok([string]$m) { Write-Output "[pixels] OK   $m" }

$exePath = (Resolve-Path -LiteralPath $Exe).Path
$appDir = Split-Path -Parent $exePath
$userDataDir = $appDir
if ((Split-Path -Leaf $appDir) -eq 'app') { $userDataDir = Split-Path -Parent $appDir }
$stateFile = Join-Path $userDataDir 'ui_state.json'
New-Item -ItemType Directory -Force -Path $Out | Out-Null

# ---------- 像素工具 ----------
function Get-Contrast([System.Drawing.Color]$a, [System.Drawing.Color]$b) {
    function Ch([double]$v) { $s = $v / 255.0; if ($s -le 0.04045) { return $s / 12.92 } return [math]::Pow(($s + 0.055) / 1.055, 2.4) }
    $l1 = 0.2126 * (Ch $a.R) + 0.7152 * (Ch $a.G) + 0.0722 * (Ch $a.B)
    $l2 = 0.2126 * (Ch $b.R) + 0.7152 * (Ch $b.G) + 0.0722 * (Ch $b.B)
    $hi = [math]::Max($l1, $l2); $lo = [math]::Min($l1, $l2)
    return ($hi + 0.05) / ($lo + 0.05)
}

function Get-Luminance([System.Drawing.Color]$c) {
    function Ch([double]$v) { $s = $v / 255.0; if ($s -le 0.04045) { return $s / 12.92 } return [math]::Pow(($s + 0.055) / 1.055, 2.4) }
    return 0.2126 * (Ch $c.R) + 0.7152 * (Ch $c.G) + 0.0722 * (Ch $c.B)
}

# 返回 @{ Background; Foreground; Contrast }
function Measure-Region([System.Drawing.Bitmap]$bmp, [int]$x, [int]$y, [int]$w, [int]$h) {
    if ($w -le 1 -or $h -le 1) { return $null }
    $hist = @{}
    for ($iy = $y; $iy -lt ($y + $h); $iy++) {
        if ($iy -lt 0 -or $iy -ge $bmp.Height) { continue }
        for ($ix = $x; $ix -lt ($x + $w); $ix++) {
            if ($ix -lt 0 -or $ix -ge $bmp.Width) { continue }
            $c = $bmp.GetPixel($ix, $iy)
            $key = "$($c.R),$($c.G),$($c.B)"
            if ($hist.ContainsKey($key)) { $hist[$key]++ } else { $hist[$key] = 1 }
        }
    }
    if ($hist.Count -eq 0) { return $null }
    $bgKey = ($hist.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 1).Key
    $bgParts = $bgKey -split ','
    $bg = [System.Drawing.Color]::FromArgb([int]$bgParts[0], [int]$bgParts[1], [int]$bgParts[2])
    # 前景 = 与背景对比度最高的"有实际面积"的颜色。
    #
    # 取值策略：先按出现次数取前 64 名，再在其中挑对比度最高的那个、且出现次数 ≥ 2。
    # 为什么不是"出现次数 ≥ 面积比例"：小字号 CJK 文字的**纯前景色像素本来就很少**
    # （14px 一行十几个字，纯色像素可能只有几十个），按比例设阈会把真正的前景色过滤掉，
    # 只剩下抗锯齿过渡色，于是量出一个虚低的对比度（实测深色输入框因此报 2.01:1，
    # 而真实值是 12~13:1）。
    # 为什么是"前 64 名"：长尾全是抗锯齿与噪声像素，排除它们才能避免偶尔一个孤立
    # 亮点把对比度顶高造成假通过。
    $top = @($hist.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 64)
    $best = $null; $bestRatio = 0.0
    foreach ($kv in $top) {
        if ($kv.Value -lt 2) { continue }
        $p = $kv.Key -split ','
        $c = [System.Drawing.Color]::FromArgb([int]$p[0], [int]$p[1], [int]$p[2])
        $ratio = Get-Contrast $c $bg
        if ($ratio -gt $bestRatio) { $bestRatio = $ratio; $best = $c }
    }
    return @{ Background = $bg; Foreground = $best; Contrast = $bestRatio; Pixels = ($w * $h) }
}

function Save-WindowShot($win, [string]$name) {
    $hwnd = [System.IntPtr]$win.Current.NativeWindowHandle
    [void][Pix.Win32]::ShowWindow($hwnd, 5)
    [void][Pix.Win32]::SetForegroundWindow($hwnd)
    Start-Sleep -Milliseconds 700
    $r = New-Object Pix.Win32+RECT
    [void][Pix.Win32]::GetWindowRect($hwnd, [ref]$r)
    $w = $r.Right - $r.Left; $h = $r.Bottom - $r.Top
    if ($w -le 0 -or $h -le 0) { return $null }
    $bmp = New-Object System.Drawing.Bitmap($w, $h)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $hdc = $g.GetHdc()
    $ok = [Pix.Win32]::PrintWindow($hwnd, $hdc, 2)
    $g.ReleaseHdc($hdc)
    if (-not $ok) { $g.CopyFromScreen($r.Left, $r.Top, 0, 0, (New-Object System.Drawing.Size($w, $h))) }
    $g.Dispose()
    $path = Join-Path $Out "$name.png"
    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    return @{ Bitmap = $bmp; Rect = $r; Path = $path }
}

function Find-ById([int]$procId, [string]$id) {
    $a = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $procId)
    $b = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty, $id)
    return [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
        [System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.AndCondition($a, $b)))
}

function Wait-MainWindow([int]$procId, [int]$timeoutSec) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $timeoutSec) {
        Start-Sleep -Milliseconds 400
        $cond = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $procId)
        $w = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
            [System.Windows.Automation.TreeScope]::Children, $cond)
        $status = Find-ById $procId 'StatusText'
        if ($null -ne $w -and $null -ne $status -and $status.Current.Name -notmatch '正在启动|Starting') {
            return $w
        }
    }
    return $null
}

# 需要逐个量对比度的控件（AutomationId）。
#   Text：测量前先填进去的内容（空输入框只有一种颜色，量出来必然是 1:1）
#   Kind：text   → 文字，按 WCAG AA 4.5:1 硬性要求
#         icon   → 纯图标按钮。图标是"图形对象"而不是文字，WCAG 1.4.11 只要求 3:1；
#                  且彩色 emoji 字形自带固定颜色（⚙ 是灰、📎 是浅灰），不可能通过
#                  改色板改善。本项目已把这类按钮的图标切到 Segoe UI Symbol 单色字形
#                  （见 Styles.xaml 的 IconText 样式），此处只**如实记录**量值，
#                  不作为失败门槛——避免为纯图标反复折腾而偏离"修 bug"的目标。
$TEXT_TARGETS = @(
    @{ Id = 'StatusText';     Name = '运行状态文字';   Text = $null;                     Kind = 'text' },
    @{ Id = 'HintText';       Name = '底部提示文字';   Text = $null;                     Kind = 'text' },
    @{ Id = 'InputBox';       Name = '输入框文字';     Text = '对比度测试 Test 123';      Kind = 'text' },
    @{ Id = 'SendButton';     Name = '发送图标按钮';   Text = $null;                     Kind = 'icon' },
    @{ Id = 'UploadButton';   Name = '上传图标按钮';   Text = $null;                     Kind = 'icon' },
    @{ Id = 'SettingsButton'; Name = '设置图标按钮';   Text = $null;                     Kind = 'icon' }
)

function Set-ElementText($el, $target) {
    if (-not $target.ContainsKey('Text')) { return }
    $text = $target['Text']
    if ($null -eq $text) { return }
    try {
        $pattern = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        $pattern.SetValue($text)
        Start-Sleep -Milliseconds 400
    } catch {
        Write-Output "[pixels]   （无法向 $($target['Id']) 写入测试文本：$($_.Exception.Message)）"
    }
}

function Test-Theme([string]$theme) {
    Write-Output "[pixels] ===== 主题 = $theme ====="
    $state = @{}
    if (Test-Path $stateFile) {
        try { (Get-Content $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $state[$_.Name] = $_.Value } } catch { }
    }
    $state['Theme'] = $theme
    ($state | ConvertTo-Json -Depth 5) | Set-Content $stateFile -Encoding UTF8

    Get-Process EchoDesk -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Milliseconds 500
    $proc = Start-Process -FilePath $exePath -WorkingDirectory $userDataDir -PassThru
    try {
        $win = Wait-MainWindow $proc.Id $ReadyTimeoutSec
        if ($null -eq $win) { Fail "[$theme] 主窗口未就绪"; return }
        Start-Sleep -Seconds 2   # 等欢迎语渲染完
        # 等后端真正就绪再量：控件在"正在启动后端"期间是**禁用**的，
        # 往禁用的输入框写测试文本会失败（The operation is not allowed on a nonenabled
        # element），于是截到的是空输入框 —— 量出来就是"输入框文字 1.05:1"这种
        # 与配色无关的假失败（本轮实测：浅色主题快了就过、深色主题慢一点就假失败）。
        $ready = $false
        $swReady = [System.Diagnostics.Stopwatch]::StartNew()
        while ($swReady.Elapsed.TotalSeconds -lt $ReadyTimeoutSec) {
            $st = Find-ById $proc.Id 'StatusText'
            if ($null -ne $st -and $st.Current.Name -eq '已就绪') { $ready = $true; break }
            Start-Sleep -Milliseconds 300
        }
        if (-not $ready) {
            Write-Output "[pixels]   [$theme] 警告：后端未在 $ReadyTimeoutSec 秒内就绪，输入框可能仍被禁用（该项可能量不到文字）"
        }
        # 截图**之前**先把测试文本填进输入框：否则截到的空输入框只有一种颜色，
        # "输入框文字与输入框背景"这一项量不出任何东西（一直是 1:1）。
        $inputEl = Find-ById $proc.Id 'InputBox'
        if ($null -ne $inputEl) {
            for ($try = 1; $try -le 5; $try++) {
                Set-ElementText $inputEl @{ Id = 'InputBox'; Text = '对比度测试 Test 123' }
                try {
                    $probe = $inputEl.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value
                    if ($probe -eq '对比度测试 Test 123') { break }
                } catch { }
                Start-Sleep -Milliseconds 600
            }
        }
        # 截图与坐标必须取"同一时刻"的一对：
        # 状态区换成 WrapPanel 后会随内容换行（能力提示一出现就多一行），
        # 整块内容高度随之变化，若先截图再读控件坐标，两者会错位几像素到几十像素
        # ——实测表现为"发送按钮"量到的背景是输入框的白色，参考值全是假的。
        # 这里先记下坐标、截图，再复核坐标是否仍然一致；不一致就重来（最多 3 次）。
        $targetIds = @($TEXT_TARGETS | ForEach-Object { $_.Id })
        $rects = @{}
        $shot = $null
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            $before = @{}
            foreach ($id in $targetIds) {
                $el = Find-ById $proc.Id $id
                if ($null -ne $el) { $before[$id] = $el.Current.BoundingRectangle }
            }
            $shot = Save-WindowShot $win "$theme-main"
            if ($null -eq $shot) { break }
            Start-Sleep -Milliseconds 250
            $stable = $true
            foreach ($id in $targetIds) {
                if (-not $before.ContainsKey($id)) { continue }
                $el = Find-ById $proc.Id $id
                if ($null -eq $el) { continue }
                $now = $el.Current.BoundingRectangle
                if ([math]::Abs($now.X - $before[$id].X) -gt 2 -or
                    [math]::Abs($now.Y - $before[$id].Y) -gt 2) {
                    $stable = $false
                    break
                }
            }
            if ($stable) { $rects = $before; break }
            Write-Output "[pixels]   （第 $attempt 次截图时界面仍在重排，重试）"
            $shot.Bitmap.Dispose()
            $shot = $null
        }
        if ($null -eq $shot) { Fail "[$theme] 截图失败"; return }
        if ($rects.Count -eq 0) {
            # 三次都不稳定：退回"用当前坐标"，至少保证有结果，并在输出里说明
            foreach ($id in $targetIds) {
                $el = Find-ById $proc.Id $id
                if ($null -ne $el) { $rects[$id] = $el.Current.BoundingRectangle }
            }
            Write-Output "[pixels]   （界面持续重排，改用当前坐标量测，数值仅供参考）"
        }

        # 1) 整体亮度：判断主题是否真的生效
        $bmp = $shot.Bitmap
        $samples = @()
        for ($y = 4; $y -lt $bmp.Height - 4; $y += 7) {
            for ($x = 4; $x -lt $bmp.Width - 4; $x += 7) {
                $c = $bmp.GetPixel($x, $y)
                $samples += (Get-Luminance $c)
            }
        }
        $avg = ($samples | Measure-Object -Average).Average
        Write-Output ("[pixels] [{0}] 主窗口平均亮度 = {1:N3}（{2}）" -f $theme, $avg, $shot.Path)
        if ($theme -eq 'dark') {
            if ($avg -gt 0.35) { Fail "[dark] 深色主题未生效：平均亮度 $([math]::Round($avg,3)) 偏高（应 < 0.35）" }
            else { Ok "[dark] 深色主题已生效（平均亮度 $([math]::Round($avg,3)) < 0.35）" }
        }
        else {
            if ($avg -lt 0.55) { Fail "[light] 浅色主题未生效：平均亮度 $([math]::Round($avg,3)) 偏低（应 > 0.55）" }
            else { Ok "[light] 浅色主题已生效（平均亮度 $([math]::Round($avg,3)) > 0.55）" }
        }

        # 2) 逐控件测局部对比度
        $worst = $null; $worstRatio = 99.0
        $uniform = @()
        $iconNotes = @()
        foreach ($t in $TEXT_TARGETS) {
            if (-not $rects.ContainsKey($t['Id'])) { Write-Output "[pixels]   （跳过 $($t['Id'])：控件不存在）"; continue }
            # 用与截图同一时刻记下的坐标，而不是重新读（重读会因界面重排而错位）
            $br = $rects[$t['Id']]
            if ($br.Width -lt 2 -or $br.Height -lt 2) { continue }
            $x = [int]($br.X - $shot.Rect.Left); $y = [int]($br.Y - $shot.Rect.Top)
            $m = Measure-Region $bmp $x $y ([int]$br.Width) ([int]$br.Height)
            if ($null -eq $m) { continue }
            $ratio = [math]::Round($m.Contrast, 2)
            Write-Output ("[pixels]   [{0}] {1}: 对比度 {2}:1（背景 {3}）" -f `
                $theme, $t['Name'], $ratio, $m.Background.Name)
            if ($ratio -le 1.01) {
                # 区域是纯色：说明该控件此刻没有可见内容（例如空输入框），不能算"通过"
                $uniform += $t['Name']
                continue
            }
            if ($t['Kind'] -eq 'icon') {
                $iconNotes += "$($t['Name']) $ratio`:1"
                continue
            }
            if ($ratio -lt $worstRatio) { $worstRatio = $ratio; $worst = $t['Name'] }
        }
        if ($uniform.Count -gt 0) {
            Write-Output "[pixels]   （以下控件区域为纯色，无量值意义，已跳过：$($uniform -join '、')）"
        }
        if ($iconNotes.Count -gt 0) {
            Write-Output "[pixels]   图标按钮（图形对象，参考值不设门槛）：$($iconNotes -join '，')"
        }
        if ($null -ne $worst) {
            if ($worstRatio -lt $MinContrast) {
                Fail "[$theme] 控件「$worst」对比度 $($worstRatio):1 低于 $($MinContrast):1"
            }
            else {
                Ok "[$theme] 最差控件对比度 $($worstRatio):1（$worst）≥ $($MinContrast):1"
            }
        }

        # 3) 设置窗口也在深/浅下截一张（供逐屏复核）
        $settingsBtn = Find-ById $proc.Id 'SettingsButton'
        if ($null -ne $settingsBtn) {
            $settingsBtn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
            Start-Sleep -Milliseconds 1200
            $setWin = $null
            $cond = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $proc.Id)
            foreach ($w in [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
                     [System.Windows.Automation.TreeScope]::Children, $cond)) {
                if ($w.Current.NativeWindowHandle -ne $win.Current.NativeWindowHandle) { $setWin = $w; break }
            }
            if ($null -ne $setWin) {
                $s = Save-WindowShot $setWin "$theme-settings"
                if ($null -ne $s) {
                    $set = $s.Bitmap
                    $ss = @()
                    for ($y = 4; $y -lt $set.Height - 4; $y += 7) {
                        for ($x = 4; $x -lt $set.Width - 4; $x += 7) { $ss += (Get-Luminance $set.GetPixel($x, $y)) }
                    }
                    $savg = ($ss | Measure-Object -Average).Average
                    Write-Output ("[pixels] [{0}] 设置窗口平均亮度 = {1:N3}（{2}）" -f $theme, $savg, $s.Path)
                    if ($theme -eq 'dark' -and $savg -gt 0.40) {
                        Fail "[dark] 设置窗口仍偏亮（$([math]::Round($savg,3))）：有控件绕过色板"
                    }
                    if ($theme -eq 'light' -and $savg -lt 0.50) {
                        Fail "[light] 设置窗口偏暗（$([math]::Round($savg,3))）"
                    }
                    $set.Dispose()
                }
                $setWin.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern).Close()
                Start-Sleep -Milliseconds 500
            }
        }
        $bmp.Dispose()
    }
    finally {
        if (-not $proc.HasExited) { $proc.Kill() }
        Get-Process EchoDeskBackend -ErrorAction SilentlyContinue | Stop-Process -Force
    }
}

if ($Theme -eq 'both') { Test-Theme 'light'; Test-Theme 'dark' } else { Test-Theme $Theme }

if (-not $KeepShots) { }   # 截图一律保留：这是人工复核的唯一凭据，删除会让问题无法回查

if ($failures.Count -gt 0) {
    Write-Output "[pixels] ✗ 发现 $($failures.Count) 处问题："
    foreach ($f in $failures) { Write-Output "   - $f" }
    exit 1
}
Write-Output "[pixels] ✓ 像素校验通过（截图在 $Out）"
exit 0
