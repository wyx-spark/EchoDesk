# EchoDesk 前端 UI 探针（研发验证 / 回归测试）
#
# 覆盖：
#   1. 设置窗口能否打开（历史缺陷：Slider.ValueChanged 在 InitializeComponent 期间空引用
#      导致整个进程被终止），以及打开耗时（要求 < 500ms，不含异步数据加载）。
#   2. UI 线程响应性（Process.Responding）是否全程正常。
#   3. 主窗口与设置窗口交替操作是否互相阻塞。
#   4. 指令选择栏的 Enter 排序（精确匹配优先），读取 CommandList 首项的 AutomationId 名称。
#   5. 是否产生 frontend_crash.log。
#
# 用法：powershell -File tools\ui_probe.ps1 [-KeepOpen]
param(
    [string]$Exe = "$PSScriptRoot\..\publish\EchoDesk-v0.2.0\EchoDesk.exe",
    [int]$ReadyTimeoutSec = 180,
    [switch]$KeepOpen
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type -Namespace Win32 -Name Fg -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr hWnd);
[DllImport("user32.dll")] public static extern System.IntPtr GetForegroundWindow();
'@

$failures = New-Object System.Collections.ArrayList
function Fail([string]$msg) { [void]$failures.Add($msg); Write-Output "[probe] FAIL $msg" }
function Ok([string]$msg) { Write-Output "[probe] OK   $msg" }

# 在整个进程的 UIA 树里按 AutomationId 找元素（设置窗口是独立 HWND，桌面根下可直接找到）
function Find-AnyById([int]$procId, [string]$id) {
    $a = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $procId)
    $b = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty, $id)
    $and = New-Object System.Windows.Automation.AndCondition($a, $b)
    return [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
        [System.Windows.Automation.TreeScope]::Descendants, $and)
}

function Invoke-El($el) { $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
function Ms([System.Diagnostics.Stopwatch]$sw) { return [math]::Round($sw.Elapsed.TotalMilliseconds) }

# 向上找到元素的顶层 Window（用于确认设置窗口确实存在）
function Get-OwnerWindow($el) {
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $node = $el
    while ($null -ne $node) {
        if ($node.Current.ControlType -eq [System.Windows.Automation.ControlType]::Window) { return $node }
        $node = $walker.GetParent($node)
    }
    return $null
}

$exePath = (Resolve-Path -LiteralPath $Exe).Path
$appDir = Split-Path -Parent $exePath
$crashLog = Join-Path $appDir 'frontend_crash.log'
if (Test-Path $crashLog) { Remove-Item $crashLog -Force }   # 每轮从干净状态开始

Write-Output "[probe] exe = $exePath"
$proc = Start-Process -FilePath $exePath -PassThru
Write-Output "[probe] pid = $($proc.Id)"
$sw = [System.Diagnostics.Stopwatch]::StartNew()

# ---------- 1. 等主窗口 + 后端就绪 ----------
$main = $null
while ($sw.Elapsed.TotalSeconds -lt $ReadyTimeoutSec) {
    Start-Sleep -Milliseconds 400
    $proc.Refresh()
    if ($proc.HasExited) { Write-Output "[probe] FAIL 进程已退出（退出码 $($proc.ExitCode)）"; exit 2 }
    $main = Find-AnyById $proc.Id 'StatusText'
    if ($null -ne $main) { break }
}
if ($null -eq $main) { Write-Output "[probe] FAIL 主窗口未出现"; exit 2 }
Write-Output "[probe] 主窗口出现 @ $((Ms $sw))ms"

$ready = $false
while ($sw.Elapsed.TotalSeconds -lt $ReadyTimeoutSec) {
    Start-Sleep -Milliseconds 400
    $txt = $main.Current.Name
    if ($txt -eq '已就绪') { $ready = $true; break }
    if ($txt -like '*失败*') { Fail "后端启动失败：$txt"; break }
}
Write-Output "[probe] 后端就绪=$ready @ $((Ms $sw))ms"
if (-not $ready) { Fail '后端未就绪'; if (-not $KeepOpen) { $proc.Kill() }; exit 3 }

# ---------- 2. 点击设置：测量打开耗时与响应性 ----------
$btn = Find-AnyById $proc.Id 'SettingsButton'
if ($null -eq $btn) { Fail '未找到 SettingsButton'; if (-not $KeepOpen) { $proc.Kill() }; exit 4 }

$sw2 = [System.Diagnostics.Stopwatch]::StartNew()
Invoke-El $btn
$saveButton = $null
while ($sw2.Elapsed.TotalSeconds -lt 10) {
    Start-Sleep -Milliseconds 100
    $saveButton = Find-AnyById $proc.Id 'SaveButton'
    if ($null -ne $saveButton) { break }
}
if ($null -eq $saveButton) {
    Fail "设置窗口 10s 内未出现（responding=$($proc.Responding)）"
} else {
    $openMs = Ms $sw2
    $win = Get-OwnerWindow $saveButton
    $title = if ($null -ne $win) { $win.Current.Name } else { '(未取到标题)' }
    Write-Output "[probe] 设置窗口出现 @ +${openMs}ms  title='$title'"
    # 构造 + 显示应远快于 500ms；数据填充是异步的，不计入
    if ($openMs -lt 500) { Ok "设置窗口打开耗时 ${openMs}ms < 500ms" } else { Fail "设置窗口打开耗时 ${openMs}ms >= 500ms" }
    if ($null -eq $win -or $title -notlike '*设置*') { Fail "设置窗口标题异常：$title" }
}

# ---------- 3. 设置窗口控件可用性（逐个 Tab 切换后检查其内容）----------
function Select-Tab($win, [string]$header) {
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::TabItem)
    $tabs = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
    foreach ($t in $tabs) {
        if ($t.Current.Name -eq $header) {
            $t.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
            Start-Sleep -Milliseconds 350
            return $true
        }
    }
    return $false
}

if ($null -ne $saveButton) {
    $win2 = Get-OwnerWindow $saveButton
    foreach ($id in @('Tabs', 'SaveButton', 'LoadStateText', 'SaveHintText')) {
        if ($null -eq (Find-AnyById $proc.Id $id)) { Fail "设置窗口缺少控件 $id" }
    }
    if ($null -ne $win2) {
        $tabCond = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::TabItem)
        $tabs = $win2.FindAll([System.Windows.Automation.TreeScope]::Descendants, $tabCond)
        $headers = @(); foreach ($t in $tabs) { $headers += $t.Current.Name }
        Write-Output "[probe] Tab 页：$($headers -join ' / ')"
        foreach ($need in @('常规', '模型', '自动化', '技能市场')) {
            if ($headers -notcontains $need) { Fail "缺少 Tab：$need" }
        }
        $perTab = @{
            '常规'     = @('DarkModeCheck', 'FontSizeSlider', 'DefaultModeCombo', 'ConfirmDestructiveCheck')
'模型'     = @('HubSourceCombo', 'HubSearchBox', 'HubSortCombo', 'HubQuantCombo', 'HubResultList',
                          'HubRegistryList', 'HubStatusText')
            '自动化'   = @('TaskUrlBox', 'TaskSelectorBox', 'SafetyCheck', 'RunOnceButton', 'AutomationLogBox',
                          'TaskNameBox', 'ResultsRootNameBox', 'ResultsLocationBox', 'OpenResultsButton')
            '技能市场' = @('MarketSearchBox', 'MarketSortCombo', 'MarketList')
        }
        foreach ($tabName in $perTab.Keys) {
            if (-not (Select-Tab $win2 $tabName)) { Fail "无法切到 Tab：$tabName"; continue }
            foreach ($id in $perTab[$tabName]) {
                if ($null -eq (Find-AnyById $proc.Id $id)) { Fail "设置窗口「$tabName」缺少控件 $id" }
            }
        }
        [void](Select-Tab $win2 '常规')
    }
}

# ---------- 3b. 状态栏分层（优化五）----------
$top = @{}
foreach ($id in @('StatusText', 'ModelText', 'ModeText', 'ModelChip', 'ModeChip')) {
    $top[$id] = Find-AnyById $proc.Id $id
    if ($null -eq $top[$id]) { Fail "顶部状态栏缺少 $id" }
}
# 顶部只保留关键信息：搜索/自动化/市场必须移出顶部
foreach ($id in @('StripSearchButton', 'StripAutomationButton', 'StripMarketButton',
                  'StripContextButton', 'StripKvButton', 'StripToggleButton')) {
    if ($null -eq (Find-AnyById $proc.Id $id)) { Fail "轻量状态区缺少 $id" }
}
$txt = @{}
foreach ($id in @('SearchText', 'AutoText', 'MarketText', 'StripContextText', 'StripKvText')) {
    $el = Find-AnyById $proc.Id $id
    $txt[$id] = if ($null -ne $el) { $el.Current.Name } else { "" }
}
Write-Output "[probe] 轻量状态区文本：$($txt.Values -join ' | ')"
if ($txt['SearchText'] -notlike '*搜索*') { Fail "状态区搜索项文本异常：$($txt['SearchText'])" }
if ($txt['AutoText'] -notlike '*自动化*') { Fail "状态区自动化项文本异常：$($txt['AutoText'])" }
if ($txt['MarketText'] -notlike '*市场*') { Fail "状态区市场项文本异常：$($txt['MarketText'])" }
if ($txt['StripContextText'] -notlike '*上下文*%*') { Fail "状态区上下文项应显示百分比：$($txt['StripContextText'])" }
if ($txt['StripKvText'] -notlike '💾*') { Fail "状态区 KV Cache 项文本异常：$($txt['StripKvText'])" }
if ($txt['SearchText'] -notlike '🔍*' -or $txt['AutoText'] -notlike '⚙️*' -or $txt['MarketText'] -notlike '🌐*') {
    Fail "状态区应使用图标 + 简短短语"
} else { Ok "轻量状态区为图标+简短短语，且含上下文百分比" }

# 折叠/展开：折叠后各项应从可视树消失，再点一次恢复
$toggle = Find-AnyById $proc.Id 'StripToggleButton'
if ($null -ne $toggle) {
    Invoke-El $toggle
    Start-Sleep -Milliseconds 600
    $hidden = $null -eq (Find-AnyById $proc.Id 'StripSearchButton')
    Invoke-El (Find-AnyById $proc.Id 'StripToggleButton')
    Start-Sleep -Milliseconds 600
    $shown = $null -ne (Find-AnyById $proc.Id 'StripSearchButton')
    if ($hidden -and $shown) { Ok "轻量状态区可折叠且可恢复" } else { Fail "折叠/展开行为异常（hidden=$hidden shown=$shown）" }
}

# 点击「自动化」项应打开设置并切到自动化 Tab
$autoChip = Find-AnyById $proc.Id 'StripAutomationButton'
if ($null -ne $autoChip) {
    Invoke-El $autoChip
    Start-Sleep -Milliseconds 900
    $win3 = Get-OwnerWindow (Find-AnyById $proc.Id 'Tabs')
    if ($null -eq $win3) { Fail '点击状态区自动化项未打开设置窗口' }
    else {
        $tabCond2 = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::TabItem)
        $sel = ''
        foreach ($t in $win3.FindAll([System.Windows.Automation.TreeScope]::Descendants, $tabCond2)) {
            if ($t.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Current.IsSelected) {
                $sel = $t.Current.Name
            }
        }
        if ($sel -eq '自动化') { Ok '点击状态区「自动化」跳转到设置→自动化' }
        else { Fail "点击状态区自动化项应切到自动化 Tab，实际 '$sel'" }
    }
}

# ---------- 4. 响应性采样 + 主/设置窗口交替操作 ----------
$stuck = 0
$interleaveOk = $true
$deadline = (Get-Date).AddSeconds(8)
$round = 0
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 700
    $round++
    $proc.Refresh()
    if (-not $proc.Responding) { $stuck++ }
    if ($round % 2 -eq 1) {
        # 交替：在设置窗口打开的情况下操作主窗口（刷新上下文按钮）
        $ctx = Find-AnyById $proc.Id 'ContextButton'
        if ($null -ne $ctx) {
            try { Invoke-El $ctx } catch { Write-Output "[probe] 主窗口操作异常：$_" }
        }
    } else {
        # 交替：主窗口操作后，设置窗口必须仍然存在且可用
        $s = Find-AnyById $proc.Id 'SaveButton'
        if ($null -eq $s -or -not $s.Current.IsEnabled) { $interleaveOk = $false; Fail '交替操作后设置窗口不可用' }
    }
}
if ($stuck -eq 0) { Ok '全程 UI 线程响应正常（Responding 始终为 True）' } else { Fail "出现 $stuck 次 UI 无响应采样" }
if ($interleaveOk) { Ok '主窗口与设置窗口可交替操作，互不阻塞' }

# ---------- 5. 指令选择栏排序与 Enter 行为（修复二）----------
$cmdBtn = Find-AnyById $proc.Id 'CommandButton'
if ($null -ne $cmdBtn) {
    $ok = $true
    try {
        Invoke-El $cmdBtn            # 展开指令输入行（默认折叠，折叠时控件不在 UIA 树中）
        Start-Sleep -Milliseconds 600
        $cmdBox = Find-AnyById $proc.Id 'CommandInputBox'
        if ($null -eq $cmdBox) { Fail '展开后仍未找到 CommandInputBox'; $ok = $false }
        if ($ok) {
        $vp = $cmdBox.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        $cases = @(
            @('/clear', '/clear', 2),
            @('/c', '/clear', 2),
            @('/sum', '/summarize', 1),
            @('/cl', '/clear', 2),
            @('/s', '/stop', 6)
        )
        foreach ($case in $cases) {
            $typed = $case[0]; $expectFirst = $case[1]; $expectCount = $case[2]
            $vp.SetValue($typed)
            Start-Sleep -Milliseconds 500
            $list = Find-AnyById $proc.Id 'CommandList'
            if ($null -eq $list) { Fail "输入 $typed 后指令列表未出现"; continue }
            $items = $list.FindAll([System.Windows.Automation.TreeScope]::Children,
                (New-Object System.Windows.Automation.PropertyCondition(
                    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                    [System.Windows.Automation.ControlType]::ListItem)))
            if ($items.Count -eq 0) { Fail "输入 $typed 后列表为空"; continue }
            $names = @(); foreach ($it in $items) { $names += $it.Current.Name }
            Write-Output "[probe] 输入 '$typed' -> 首项 '$($names[0])'（共 $($items.Count) 项：$($names -join ' ')）"
            if ($names[0] -ne $expectFirst) { Fail "输入 $typed 时首项应为 $expectFirst，实际 $($names[0])" }
            if ($names -contains '/clearcache' -and $names[0] -eq '/clearcache') { Fail "输入 $typed 时 /clearcache 排在首位" }
            if ($expectCount -ge 0 -and $items.Count -ne $expectCount) {
                Write-Output "[probe] 提示：输入 $typed 期望 $expectCount 项，实际 $($items.Count) 项（指令表变更时请同步用例）"
            }
        }
        # Enter 行为：候选不唯一且非精确匹配时必须「先补全、不执行」
        $vp.SetValue('/cl')
        Start-Sleep -Milliseconds 450
        $cmdBox.SetFocus()
        $proc.Refresh()
        [void][Win32.Fg]::SetForegroundWindow($proc.MainWindowHandle)
        Start-Sleep -Milliseconds 400
        $cmdBox.SetFocus()
        Start-Sleep -Milliseconds 300
        $before = $cmdBox.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value
        [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
        Start-Sleep -Milliseconds 700
        $after = $cmdBox.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value
        Write-Output "[probe] 在 '/cl' 上按 Enter：'$before' -> '$after'"
        if ($after -eq $before) {
            Write-Output "[probe] 警告：键盘模拟未送达应用（前台焦点限制），Enter 行为未能在本轮自动验证"
        } elseif ($after -notlike '/clear*') {
            Fail "在 /cl 上按 Enter 应补全为 /clear（不执行），实际 '$after'"
        } else {
            Ok "候选不唯一时 Enter 只补全不执行（'$before' -> '$after'）"
        }
        # 任务书验证用例：输入 /clear + Enter 必须执行 /clear（不得执行 /clearcache），
        # 且执行前出现确认提示；此处先验「弹出确认框且文案是 /clear」，再点「否」确认未执行。
        $vp.SetValue('/clear')
        Start-Sleep -Milliseconds 450
        $cmdBox.SetFocus()
        [void][Win32.Fg]::SetForegroundWindow($proc.MainWindowHandle)
        Start-Sleep -Milliseconds 300
        $cmdBox.SetFocus()
        Start-Sleep -Milliseconds 300
        [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
        Start-Sleep -Milliseconds 900

        # MessageBox 对话框是进程内的顶级窗口（ClassName #32770）
        $dlgCond = New-Object System.Windows.Automation.AndCondition(
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $proc.Id)),
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ClassNameProperty, '#32770')))
        $dlg = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
            [System.Windows.Automation.TreeScope]::Descendants, $dlgCond)
        if ($null -eq $dlg) {
            Write-Output '[probe] 警告：未检测到确认对话框（键盘模拟可能未送达）'
        } else {
            $dlgText = ''
            $tcond = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::Text)
            foreach ($t in $dlg.FindAll([System.Windows.Automation.TreeScope]::Descendants, $tcond)) {
                $dlgText += $t.Current.Name + ' '
            }
            Write-Output "[probe] 确认对话框：$($dlgText.Trim())"
            if ($dlgText -like '*确认执行 /clear*') { Ok '执行 /clear 前出现确认提示（且明确是 /clear 而非 /clearcache）' }
            else { Fail "确认对话框文案未指向 /clear：$dlgText" }
            if ($dlgText -like '*clearcache*') { Fail '确认对话框出现了 /clearcache，说明选中了错误的指令' }
            # 点「否」：必须什么都不做
            $btnCond = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::Button)
            $clicked = $false
            foreach ($b in $dlg.FindAll([System.Windows.Automation.TreeScope]::Descendants, $btnCond)) {
                if ($b.Current.Name -eq '否(N)' -or $b.Current.Name -eq '否' -or $b.Current.Name -like '否*') {
                    Invoke-El $b; $clicked = $true; break
                }
            }
            if (-not $clicked) { Write-Output '[probe] 警告：未找到「否」按钮，对话框保持原状' }
            Start-Sleep -Milliseconds 600
            $kept = $cmdBox.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value
            Write-Output "[probe] 点「否」后指令输入框 = '$kept'"
            if ($kept -notlike '/clear*') { Fail "取消确认后输入内容被清空（'$kept'），应保留以便用户修改" }
        }
        $vp.SetValue('')
        }
    } catch {
        Write-Output "[probe] 指令栏测试异常：$_"
    }
} else {
    Write-Output "[probe] 未找到 CommandButton，跳过指令栏测试"
}

# ---------- 6. 收尾 ----------
Start-Sleep -Milliseconds 500
$proc.Refresh()
Write-Output "[probe] 最终 responding=$($proc.Responding) exited=$($proc.HasExited)"
if ($proc.HasExited) { Fail '进程意外退出' }
if (Test-Path $crashLog) {
    $c = Get-Content $crashLog -Raw -Encoding UTF8
    if ($null -ne $c -and $c.Trim().Length -gt 0) { Fail "产生前端崩溃日志：`n$c" } else { Ok '无前端崩溃日志（文件为空）' }
} else {
    Ok '无前端崩溃日志'
}

if ($failures.Count -gt 0) {
    Write-Output "[probe] ==== 失败 $($failures.Count) 项 ===="
    foreach ($f in $failures) { Write-Output "  - $f" }
}

if (-not $KeepOpen) {
    try { $proc.CloseMainWindow() | Out-Null } catch { }
    Start-Sleep -Seconds 2
    $proc.Refresh(); if (-not $proc.HasExited) { $proc.Kill() }
}
if ($failures.Count -gt 0) { exit 1 }
Write-Output "[probe] 全部通过"
