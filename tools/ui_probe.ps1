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
# 用法：powershell -File tools\ui_probe.ps1 [-KeepOpen] [-ThinkingModel <gguf 路径>]
#
# -ThinkingModel：给"思考内容分离"那一节用的**推理模型**（会思考的模型）。
#   给了就地把它临时装进被测程序的 models\ 目录，并写一份 config.user.json 把
#   model_path 指过去（config.user.json 是后端的覆盖层，优先于 config.json），
#   跑完在收尾处删掉这两样东西。不给则那一节按"当前模型没有思考内容"跳过。
param(
    [string]$Exe = "$PSScriptRoot\..\publish\EchoDesk-v0.2.1\app\EchoDesk.exe",
    [int]$ReadyTimeoutSec = 180,
    [string]$ThinkingModel = "",
    [switch]$KeepOpen
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type -Namespace Win32 -Name Fg -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr hWnd);
[DllImport("user32.dll")] public static extern System.IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern bool MoveWindow(System.IntPtr hWnd, int X, int Y, int nWidth, int nHeight, bool bRepaint);
[DllImport("user32.dll")] public static extern bool GetWindowRect(System.IntPtr hWnd, out RECT lpRect);
[StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
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
    # 重试 3 次：UIA 客户端在界面忙碌时偶发返回空（实测同一轮里前一批全中找到、
    # 紧接的一批全找不中、再往后又全找到）。这不是"控件不存在"，不该被当成失败。
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            $found = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
                [System.Windows.Automation.TreeScope]::Descendants, $and)
            if ($null -ne $found) { return $found }
        } catch {
            Write-Output "[probe] 提示：FindFirst($id) 第 $attempt 次异常：$($_.Exception.Message)"
        }
        if ($attempt -lt 3) { Start-Sleep -Milliseconds 200 }
    }
    return $null
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
# 修复七：程序本体在 app\ 下；崩溃日志仍写在 exe 同目录（ProgramDirectory）
$crashLog = Join-Path $appDir 'frontend_crash.log'
if (Test-Path $crashLog) { Remove-Item $crashLog -Force }   # 每轮从干净状态开始
# 便携版布局下外层目录（用户数据/快捷方式所在），工作目录必须设成它，
# 否则results与 ui_state.json 会落到 app\ 里，与发布布局不一致。
$userDataDir = $appDir
if ((Split-Path -Leaf $appDir) -eq 'app') { $userDataDir = Split-Path -Parent $appDir }

# ---------- 0. 思考内容分离：临时换成"会思考的模型" ----------
# 默认模型 Qwen3.5-0.8B 的对话模板在 enable_thinking 未传入时走 else 分支
# （塞一个空思考块），因此**不产生思考内容**；要有思考内容必须换一个默认开启思考的
# 模型。这里用 config.user.json（后端配置覆盖层，优先生效）把 model_path 指过去，
# 不动被测程序自带的 config.json，也不删它自带的模型文件。
$probeModelDir = Join-Path $appDir 'backend\models'
$probeUserConfig = Join-Path $appDir 'backend\config.user.json'
$probeModelName = ''
$probeModelCopied = $false
# 提前退出（后端没起来等）时也要撤掉临时覆盖，否则会把被测程序留在"指向临时模型"的状态
function Remove-ProbeModelOverride {
    try {
        if ($script:probeUserConfig -and (Test-Path -LiteralPath $script:probeUserConfig)) {
            Remove-Item -LiteralPath $script:probeUserConfig -Force
        }
        if ($script:probeModelCopied -and $script:probeModelName -ne '') {
            $stray = Join-Path $script:probeModelDir $script:probeModelName
            if (Test-Path -LiteralPath $stray) { Remove-Item -LiteralPath $stray -Force }
            Write-Output "[probe] 已清理临时模型 $script:probeModelName"
        }
    } catch {
        # 清理本身失败不能把探针的失败原因顶掉（$ErrorActionPreference='Stop' 下
        # 这里的异常会让脚本在打印真正原因前中止 —— 本轮实测踩到过）
        Write-Output "[probe] 警告：清理临时覆盖失败：$_"
    }
}
if ($ThinkingModel -ne '' -and (Test-Path -LiteralPath $ThinkingModel)) {
    $probeModelName = Split-Path -Leaf $ThinkingModel
    $dest = Join-Path $probeModelDir $probeModelName
    if (-not (Test-Path -LiteralPath $dest)) {
        Copy-Item -LiteralPath $ThinkingModel -Destination $dest -Force
        $probeModelCopied = $true
    }
    # 必须写成**无 BOM** 的 UTF-8：Windows PowerShell 5.1 的 Set-Content -Encoding UTF8
    # 会写 BOM，带 BOM 的 JSON 会让后端配置解析失败、启动即崩（本轮实测踩到，
    # 顺带把"配置文件带 BOM 也要能启动"这条产品级加固做进了 config.py）。
    # session_file 置空：这一节要断言"最后一个复制按钮复制的是本次回答"，
    # 恢复历史会往面板里插入旧消息，让"最后一个"变得不确定。
    $override = "{ `"model_path`": `"$probeModelName`", `"session_file`": `"`" }"
    [System.IO.File]::WriteAllText($probeUserConfig, $override,
        (New-Object System.Text.UTF8Encoding($false)))
    Write-Output "[probe] 思考区检查用模型 = $probeModelName（临时覆盖，收尾时删除）"
} elseif ($ThinkingModel -ne '') {
    Write-Output "[probe] 警告：找不到 -ThinkingModel 指定的模型 $ThinkingModel，该节将跳过"
}

Write-Output "[probe] exe = $exePath"
Write-Output "[probe] 工作目录 = $userDataDir"
$proc = Start-Process -FilePath $exePath -WorkingDirectory $userDataDir -PassThru
Write-Output "[probe] pid = $($proc.Id)"
$sw = [System.Diagnostics.Stopwatch]::StartNew()

# ---------- 1. 等主窗口 + 后端就绪 ----------
$main = $null
while ($sw.Elapsed.TotalSeconds -lt $ReadyTimeoutSec) {
    Start-Sleep -Milliseconds 400
    $proc.Refresh()
    if ($proc.HasExited) { Write-Output "[probe] FAIL 进程已退出（退出码 $($proc.ExitCode)）"; Remove-ProbeModelOverride; exit 2 }
    $main = Find-AnyById $proc.Id 'StatusText'
    if ($null -ne $main) { break }
}
if ($null -eq $main) { Write-Output "[probe] FAIL 主窗口未出现"; Remove-ProbeModelOverride; exit 2 }
Write-Output "[probe] 主窗口出现 @ $((Ms $sw))ms"

$ready = $false
while ($sw.Elapsed.TotalSeconds -lt $ReadyTimeoutSec) {
    Start-Sleep -Milliseconds 400
    $txt = $main.Current.Name
    if ($txt -eq '已就绪') { $ready = $true; break }
    if ($txt -like '*失败*') { Fail "后端启动失败：$txt"; break }
}
Write-Output "[probe] 后端就绪=$ready @ $((Ms $sw))ms"
if (-not $ready) { Fail '后端未就绪'; if (-not $KeepOpen) { try { $proc.Kill() } catch { } }; Remove-ProbeModelOverride; exit 3 }

# ---------- 2. 点击设置：测量打开耗时与响应性 ----------
$btn = Find-AnyById $proc.Id 'SettingsButton'
if ($null -eq $btn) { Fail '未找到 SettingsButton'; if (-not $KeepOpen) { try { $proc.Kill() } catch { } }; Remove-ProbeModelOverride; exit 4 }

# 预热 UIA：第一次 Find-AnyById 要建立跨进程 provider 树，实测冷启动这一下约 2 秒；
# 不预热的话，这笔一次性开销会被算进"设置窗口打开耗时"，量出来就是 2.3 秒而不是 0.26 秒
# （同一份 EchoDesk.exe，预热与不预热的差异，见 artifacts 里的诊断记录）。
# 这里量的是"窗口多久出现"，就不该把 UIA 的冷启动算进去。
$warm = [System.Diagnostics.Stopwatch]::StartNew()
$null = Find-AnyById $proc.Id 'StatusText'
$null = Find-AnyById $proc.Id 'SettingsButton'
Write-Output "[probe] UIA 预热完成 @ $((Ms $warm))ms"

$sw2 = [System.Diagnostics.Stopwatch]::StartNew()
Invoke-El $btn
$saveButton = $null
$tabsMs = -1
while ($sw2.Elapsed.TotalSeconds -lt 10) {
    Start-Sleep -Milliseconds 100
    if ($tabsMs -lt 0 -and $null -ne (Find-AnyById $proc.Id 'Tabs')) { $tabsMs = (Ms $sw2) }
    $saveButton = Find-AnyById $proc.Id 'SaveButton'
    if ($null -ne $saveButton) { break }
}
Write-Output "[probe] 设置窗口分级耗时：TabControl @ ${tabsMs}ms，SaveButton @ $((Ms $sw2))ms"
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
        foreach ($need in @('常规', '模型', '自动化', '已安装', '技能市场', '开发者')) {
            if ($headers -notcontains $need) { Fail "缺少 Tab：$need" }
        }
        $perTab = @{
            '常规'     = @('ThemeCombo', 'FontSizeSlider', 'DefaultModeCombo', 'ConfirmDestructiveCheck',
                          'LanguageCombo', 'LangPackText', 'EmojiCombo', 'ThemeStatusText', 'EmojiStatusText')
            # v0.2.1 模型市场优化：搜索/排序/分页/本地筛选。注意只能列**可见**控件 ——
            # 折叠的 Expander 内容与 Visibility=Collapsed 的按钮不在 UIA 树里，
            # 因此 HubMoreButton / HubStopButton / HubCancelButton / HubRemoteRetryButton
            # 以及筛选面板里的控件都放到后面的交互段里展开后再找。
            '模型'     = @('HubSourceCombo', 'HubSearchBox', 'HubSortCombo', 'HubSearchButton', 'HubRefreshButton',
                          'HubResultList', 'HubRegistryList', 'HubStatusText', 'HubFilterExpander',
                          'HubPageInfoText', 'HubTotalText', 'HubFirstPageButton', 'HubPrevPageButton',
                          'HubNextPageButton', 'HubLastPageButton', 'HubPageBox', 'HubGoPageButton',
                          'HubPageSizeCombo', 'HubEmptyText')
            # v0.2.1（任务四）：模板库 + 字段级帮助 + 可视化选取 + 测试向导 + 导入导出
            '自动化'   = @('TaskUrlBox', 'TaskSelectorBox', 'SafetyCheck', 'RunOnceButton', 'AutomationLogBox',
                          'TaskNameBox', 'ResultsRootNameBox', 'ResultsLocationBox', 'OpenResultsButton',
                          'TemplateCombo', 'ApplyTemplateButton', 'SaveTemplateButton', 'AutomationGuideButton',
                          'PickTaskListButton', 'PickDetailFileButton',
                          'WizardButton', 'WizardStep1Button', 'WizardStep4Button',
                          'ExportConfigButton', 'ImportConfigButton')
            # v0.2.1（任务二 / 2B）：插件清单 + 重载 + 脚手架 + 面板
            # 注意：SkillEmptyText 只在插件列表为空时可见，折叠元素不在 UIA 树里，不能列为必需控件。
            '已安装'   = @('SkillList', 'SkillsRefreshButton', 'SkillsReloadButton',
                          'SkillsInstallFolderButton', 'SkillsScaffoldButton', 'SkillStatusText')
            '技能市场' = @('MarketSearchBox', 'MarketSortCombo', 'MarketList')
            # v0.2.1（任务 2B.10）：开发者模式 / 目录 / 日志 / 清单 / 钩子 / 面板
            '开发者'   = @('DevModeCheck', 'DevModeStatusText', 'DevPluginListText', 'DevDirsText',
                          'DevApiText', 'DevReloadButton', 'DevLogBox',
                          'LogLinesCombo', 'ScaffoldIdBox', 'ScaffoldCreateButton', 'ManifestList',
                          'ManifestAddRow', 'HooksText', 'PanelList')
        }
        foreach ($tabName in $perTab.Keys) {
            if (-not (Select-Tab $win2 $tabName)) { Fail "无法切到 Tab：$tabName"; continue }
            foreach ($id in $perTab[$tabName]) {
                if ($null -eq (Find-AnyById $proc.Id $id)) { Fail "设置窗口「$tabName」缺少控件 $id" }
            }
        }

        # ---------- 2z. 修复二 / 修复四：主题选择与能力信息（新增验收）----------
        # 修复二的静态色板由 tools\check_theme.py 校验、真实渲染由 tools\check_ui_pixels.ps1
        # 校验，这里只验证"界面上真的能选主题"（三态：跟随系统/浅色/深色）。
        if (Select-Tab $win2 '常规') {
            $themeCombo = Find-AnyById $proc.Id 'ThemeCombo'
            if ($null -eq $themeCombo) {
                Fail '缺少主题下拉框 ThemeCombo'
            } else {
                $itemCond = New-Object System.Windows.Automation.PropertyCondition(
                    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                    [System.Windows.Automation.ControlType]::ListItem)
                $expand = $themeCombo.GetCurrentPattern(
                    [System.Windows.Automation.ExpandCollapsePattern]::Pattern)
                $expand.Expand()
                Start-Sleep -Milliseconds 400
                # WPF ComboBox 的列表项在展开前不在自动化树里，必须先 Expand 再取
                $items = $themeCombo.FindAll([System.Windows.Automation.TreeScope]::Descendants, $itemCond)
                $names = @(); foreach ($it in $items) { $names += $it.Current.Name }
                $expand.Collapse()
                if ($names.Count -ge 3) {
                    Ok "主题三态可选：$($names -join ' / ')"
                } else {
                    Fail "主题下拉框项不足（应为 跟随系统/浅色/深色）：$($names -join ' / ')"
                }
            }
        }
        if (Select-Tab $win2 '模型') {
            # 能力信息由后端 ready/capability 接口提供，界面只负责展示；
            # 后端未就绪时允许为空，因此这里只做"控件存在性"检查（已在上面的 perTab 里做过）。
            $cap = Find-AnyById $proc.Id 'HubStatusText'
            if ($null -ne $cap) { Ok '模型页能力信息控件就位' }
        }

        # ---------- 2a. 字段级帮助（任务书 4.3 验收项）----------
        # 两条路径都要验证：
        #   1) 内联帮助面板始终可见，内容是生活化说明而不是术语；
        #   2) 点字段旁的「?」会弹出该字段的说明与示例（对应"不知道怎么填"的主诉）。
        function Get-PanelText($panel) {
            $text = ''
            $tc = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::Text)
            foreach ($t in $panel.FindAll([System.Windows.Automation.TreeScope]::Descendants, $tc)) {
                $text += $t.Current.Name + ' '
            }
            return $text
        }
        if (-not (Select-Tab $win2 '自动化')) {
            Fail '无法切到「自动化」页验证字段帮助'
        } else {
            # 注意：Border 没有 UIA peer，不会出现在自动化树里，因此要找面板内的
            # TextBlock（FieldHelpBody）而不是外层的 Border。
            $panel = Find-AnyById $proc.Id 'FieldHelpBody'
            if ($null -eq $panel) {
                Fail '缺少内联字段帮助文本 FieldHelpBody'
            } else {
                # FieldHelpBody 本身就是文本元素，文字在它自己的 Name 上（不是后代）
                $bodyText = $panel.Current.Name
                if ($bodyText -like '*输入框*' -or $bodyText -like '*input below*') {
                    Ok '内联字段帮助面板可见并给出使用提示'
                } else {
                    Fail "内联帮助面板内容异常：$($bodyText.Trim())"
                }
            }
            $helpBtn = Find-AnyById $proc.Id 'HelpTaskListUrl'
            if ($null -eq $helpBtn) {
                Fail '缺少任务列表网址旁的「?」按钮'
            } else {
                $inv = $helpBtn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
                $inv.Invoke()
                Start-Sleep -Milliseconds 900
                $dlgCond = New-Object System.Windows.Automation.AndCondition(
                    (New-Object System.Windows.Automation.PropertyCondition(
                        [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $proc.Id)),
                    (New-Object System.Windows.Automation.PropertyCondition(
                        [System.Windows.Automation.AutomationElement]::ClassNameProperty, '#32770')))
                $helpDlg = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
                    [System.Windows.Automation.TreeScope]::Descendants, $dlgCond)
                if ($null -eq $helpDlg) {
                    Fail '点「?」没有弹出字段说明'
                } else {
                    $dlgText = Get-PanelText $helpDlg
                    if ($dlgText -like '*地址栏*' -or $dlgText -like '*address bar*') {
                        Ok '点「?」弹出通俗易懂的字段说明与示例'
                    } else {
                        Fail "「?」说明内容不像通俗说明：$($dlgText.Trim())"
                    }
                    $btnCond2 = New-Object System.Windows.Automation.PropertyCondition(
                        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                        [System.Windows.Automation.ControlType]::Button)
                    $btns = $helpDlg.FindAll([System.Windows.Automation.TreeScope]::Descendants, $btnCond2)
                    if ($btns.Count -gt 0) {
                        $btns[0].GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
                        Start-Sleep -Milliseconds 500
                    }
                }
            }
            $panel2 = Find-AnyById $proc.Id 'FieldHelpBody'
            if ($null -ne $panel2) {
                $after = $panel2.Current.Name
                if ($after -like '*地址栏*') { Ok '内联帮助面板切换为该字段的说明' }
                else { Write-Output "[probe] 提示：内联帮助面板未随点击切换（$($after.Trim())）" }
            }
        }

        # ---------- 2b. 语言切换即时生效（任务书 3.4 验收项）----------
        # 切到英文后，设置窗口的 Tab 标题与主窗口状态区文本都必须立刻变英文，且不需要重启。
        if (-not (Select-Tab $win2 '常规')) { Fail '语言切换前无法切到「常规」' }
        $langCombo = Find-AnyById $proc.Id 'LanguageCombo'
        if ($null -eq $langCombo) {
            Fail '未找到 LanguageCombo，无法验证语言切换'
        } else {
            # WPF 的 ComboBox 在未展开时，其选项不会出现在 UIA 树里，
            # 必须先 Expand 再找 SelectionItem（直接找 Children 会得到空列表）。
            $enItem = $null
            $zhItem = $null
            $expand = $null
            try {
                $expand = $langCombo.GetCurrentPattern(
                    [System.Windows.Automation.ExpandCollapsePattern]::Pattern)
                $expand.Expand()
                Start-Sleep -Milliseconds 600
                $itemCond = New-Object System.Windows.Automation.PropertyCondition(
                    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                    [System.Windows.Automation.ControlType]::ListItem)
                $options = $langCombo.FindAll([System.Windows.Automation.TreeScope]::Descendants, $itemCond)
                foreach ($item in $options) {
                    if ($item.Current.Name -like '*English*') { $enItem = $item }
                    if ($item.Current.Name -like '*简体中文*') { $zhItem = $item }
                }
                if ($null -eq $enItem) {
                    $names = ($options | ForEach-Object { $_.Current.Name }) -join ', '
                    $expand.Collapse()
                    Fail "语言下拉框里没有 English（实际：$names）"
                }
            } catch {
                Fail "无法展开语言下拉框：$($_.Exception.Message)"
            }
            if ($null -ne $enItem) {
                $sel = $enItem.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
                $sel.Select()
                Start-Sleep -Milliseconds 900
                $headers2 = @()
                foreach ($t in $win2.FindAll([System.Windows.Automation.TreeScope]::Descendants, $tabCond)) {
                    $headers2 += $t.Current.Name
                }
                Write-Output "[probe] 切英文后 Tab 页：$($headers2 -join ' / ')"
                if ($headers2 -contains 'Automation' -and $headers2 -contains 'Developer' -and
                    $headers2 -contains 'Installed') {
                    Ok '切换语言后设置窗口文案立即变英文（无需重启）'
                } else {
                    Fail "切换语言后设置窗口没有变英文：$($headers2 -join ', ')"
                }
                # 主窗口状态区也应跟着变
                $hint = Find-AnyById $proc.Id 'HintText'
                if ($null -ne $hint -and $hint.Current.Name -like '*Ctrl+Enter*send*') {
                    Ok '切换语言后主窗口提示文案同步变英文'
                } elseif ($null -ne $hint) {
                    Write-Output "[probe] 主窗口提示：$($hint.Current.Name)"
                }
                # 指令面板的说明也应来自后端语言包（英文）
                $cmdList = Find-AnyById $proc.Id 'CommandList'
                if ($null -ne $cmdList) {
                    $items = $cmdList.FindAll([System.Windows.Automation.TreeScope]::Children,
                                              [System.Windows.Automation.Condition]::TrueCondition)
                    if ($items.Count -gt 0) {
                        Ok "指令面板可用（$($items.Count) 项候选）"
                    }
                }
                # 切回中文，避免影响后续步骤（$zhItem 在展开下拉时已经一起找到）
                if ($null -ne $zhItem) {
                    $zhItem.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
                    Start-Sleep -Milliseconds 900
                    $headers3 = @()
                    foreach ($t in $win2.FindAll([System.Windows.Automation.TreeScope]::Descendants, $tabCond)) {
                        $headers3 += $t.Current.Name
                    }
                    if ($headers3 -contains '自动化') { Ok '能切回中文并立即生效' }
                    else { Fail "切回中文失败：$($headers3 -join ', ')" }
                } else {
                    Write-Output '[probe] 提示：下拉选项里没找到「简体中文」，跳过切回验证'
                }
            }
        }

        # ---------- 2c. 模型市场：加载 / 分页 / 本地筛选（v0.2.1 模型市场优化验收）----------
        # 任务书的验证方式点名要验："搜索 GGUF 能加载全部结果并正确分页""分页控件在滚动
        # 结果列表时保持可见""上一页/下一页/跳到首尾/页码跳转与非法输入""本地筛选实时生效、
        # 状态显示三层数字"。这些只有**在真实界面上点一遍**才算验过，所以在 UIA 上做。
        #
        # 断言尽量不依赖中文文案（只匹配数字与元素可见性）：这个脚本是无 BOM 的 UTF-8，
        # 本机代码页恰好是 65001 才读得对，少用中文字面量就少一份环境依赖。
        if (-not (Select-Tab $win2 '模型')) {
            Fail '无法切到「模型」页验证模型市场'
        } else {
            function Get-TextById([string]$id) {
                $el = Find-AnyById $proc.Id $id
                if ($null -eq $el) { return '' }
                return $el.Current.Name
            }
            # 分页信息形如「第 3 / 250 页」：只取数字，避免依赖中文
            function Get-PagePair {
                $t = Get-TextById 'HubPageInfoText'
                if ($t -match '(\d+)\s*/\s*(\d+)') {
                    return @{ Current = [int]$Matches[1]; Total = [int]$Matches[2]; Raw = $t }
                }
                return @{ Current = -1; Total = -1; Raw = $t }
            }
            function Wait-Loaded([int]$seconds) {
                # 「搜索」按钮在整轮加载期间是禁用的，它重新可用 == 这一轮结束了。
                # 不看这一点的话，会把上一次查询留下的旧页码当成"已经加载好"
                # （实测踩到过：第一轮失败后界面停在「第 1 / 1 页」，重试时立刻返回）。
                $deadline = (Get-Date).AddSeconds($seconds)
                while ((Get-Date) -lt $deadline) {
                    $btn = Find-AnyById $proc.Id 'HubSearchButton'
                    $idle = ($null -eq $btn) -or $btn.Current.IsEnabled
                    $p = Get-PagePair
                    if ($idle -and $p.Total -ge 2) { return $p }
                    Start-Sleep -Milliseconds 500
                }
                return (Get-PagePair)
            }
            # 状态行里的三层数字：第一个数字是"本地筛选后"的条数
            function Get-FilteredCount {
                $t = Get-TextById 'HubStatusText'
                if ($t -match '(\d+)') { return [int]$Matches[1] }
                return -1
            }

            $kw = Find-AnyById $proc.Id 'HubSearchBox'
            if ($null -eq $kw) {
                Fail '未找到 HubSearchBox'
            } else {
                $kw.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue('qwen')
                Invoke-El (Find-AnyById $proc.Id 'HubSearchButton')
                $loaded = Wait-Loaded 240
                if ($loaded.Total -lt 2) {
                    # 前端可能因为"后端正在处理窗口初始化请求"而拿到 busy 响应；
                    # 等一会儿再点一次搜索，并把状态行原文留下来（否则只看到 1/1，无从判断）。
                    Write-Output "[probe] 首次加载只拿到 $($loaded.Raw)，状态行：$(Get-TextById 'HubStatusText')"
                    Start-Sleep -Seconds 3
                    Invoke-El (Find-AnyById $proc.Id 'HubSearchButton')
                    $loaded = Wait-Loaded 240
                }
                Write-Output "[probe] 模型市场加载完成：$($loaded.Raw)"
                if ($loaded.Total -lt 2) {
                    Fail ("加载后没有分页信息或页数不足（{0}）：无法验证分页；状态行={1}" -f `
                          $loaded.Raw, (Get-TextById 'HubStatusText'))
                } else {
                    Ok "搜索后取回结果并正确分页（$($loaded.Raw)）"
                    $totalText = Get-TextById 'HubTotalText'
                    if ($totalText.Length -gt 0) { Ok "显示总条数：$totalText" }
                    else { Fail '没有显示总条数' }
                    if ((Get-TextById 'HubStatusText') -like '*Hugging Face*') {
                        Ok '状态行说明了下载量口径是历史总下载量'
                    } else {
                        Fail "状态行未说明下载量口径：$(Get-TextById 'HubStatusText')"
                    }

                    # --- 边界：第一页时"首页/上一页"必须禁用 ---
                    $first = Find-AnyById $proc.Id 'HubFirstPageButton'
                    $prev = Find-AnyById $proc.Id 'HubPrevPageButton'
                    $next = Find-AnyById $proc.Id 'HubNextPageButton'
                    $last = Find-AnyById $proc.Id 'HubLastPageButton'
                    if (-not $first.Current.IsEnabled -and -not $prev.Current.IsEnabled) {
                        Ok '第 1 页时「首页」「上一页」均禁用'
                    } else {
                        Fail '第 1 页时首页/上一页未禁用'
                    }
                    if ($next.Current.IsEnabled) { Ok '第 1 页时「下一页」可用' } else { Fail '第 1 页时下一页被禁用' }

                    # --- 分页控件不随结果列表滚动（任务三的布局要求）---
                    $listEl = Find-AnyById $proc.Id 'HubResultList'
                    $before = (Find-AnyById $proc.Id 'HubPageInfoText').Current.BoundingRectangle
                    $scrolled = $false
                    try {
                        $sp = $listEl.GetCurrentPattern([System.Windows.Automation.ScrollPattern]::Pattern)
                        if ($sp.Current.VerticallyScrollable) {
                            $sp.Scroll([System.Windows.Automation.ScrollAmount]::LargeIncrement,
                                       [System.Windows.Automation.ScrollAmount]::NoAmount)
                            Start-Sleep -Milliseconds 600
                            $scrolled = $true
                        }
                    } catch { Write-Output "[probe] 提示：结果列表不支持滚动模式（$($_.Exception.Message)）" }
                    $after = (Find-AnyById $proc.Id 'HubPageInfoText').Current.BoundingRectangle
                    $moved = [math]::Abs($after.Top - $before.Top) -gt 1
                    if (-not $moved -and $after.Width -gt 0) {
                        if ($scrolled) { Ok '滚动结果列表时分页控件位置不变（固定可见）' }
                        else { Ok '分页控件位置稳定（列表当前无需滚动）' }
                    } else {
                        Fail "滚动后分页控件发生了位移（$($before.Top) → $($after.Top)）"
                    }

                    # --- 下一页 / 末页 / 首页 ---
                    Invoke-El $next
                    Start-Sleep -Milliseconds 900
                    $p2 = Get-PagePair
                    if ($p2.Current -eq 2) { Ok '「下一页」跳到第 2 页' } else { Fail "下一页后页码异常：$($p2.Raw)" }
                    if ((Find-AnyById $proc.Id 'HubPrevPageButton').Current.IsEnabled) {
                        Ok '非首页时「上一页」可用'
                    } else { Fail '第 2 页时上一页仍禁用' }

                    Invoke-El $last
                    Start-Sleep -Milliseconds 1200
                    $pLast = Get-PagePair
                    if ($pLast.Current -eq $pLast.Total) { Ok "「末页」跳到最后一页（$($pLast.Raw)）" }
                    else { Fail "末页跳转异常：$($pLast.Raw)" }
                    if (-not (Find-AnyById $proc.Id 'HubNextPageButton').Current.IsEnabled) {
                        Ok '最后一页时「下一页」禁用'
                    } else { Fail '最后一页时下一页仍可用' }

                    Invoke-El (Find-AnyById $proc.Id 'HubFirstPageButton')
                    Start-Sleep -Milliseconds 900
                    if ((Get-PagePair).Current -eq 1) { Ok '「首页」回到第 1 页' } else { Fail '首页跳转异常' }

                    # --- 页码跳转：合法、非法、回车 ---
                    $pageBox = Find-AnyById $proc.Id 'HubPageBox'
                    $vpBox = $pageBox.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
                    $vpBox.SetValue('3')
                    Invoke-El (Find-AnyById $proc.Id 'HubGoPageButton')
                    Start-Sleep -Milliseconds 900
                    if ((Get-PagePair).Current -eq 3) { Ok '输入页码 3 后点「跳转」到第 3 页' }
                    else { Fail "页码跳转异常：$((Get-PagePair).Raw)" }

                    $vpBox.SetValue('abc')
                    Invoke-El (Find-AnyById $proc.Id 'HubGoPageButton')
                    Start-Sleep -Milliseconds 800
                    $hint = Get-TextById 'HubPagerHint'
                    if ((Get-PagePair).Current -eq 3 -and $hint.Length -gt 0) {
                        Ok "非法页码给出提示且不跳页（提示：$hint）"
                    } else {
                        Fail "非法页码处理异常（页码 $((Get-PagePair).Raw)，提示 '$hint'）"
                    }

                    $vpBox.SetValue('2')
                    $pageBox.SetFocus()
                    [void][Win32.Fg]::SetForegroundWindow($proc.MainWindowHandle)
                    Start-Sleep -Milliseconds 400
                    $pageBox.SetFocus()
                    Start-Sleep -Milliseconds 300
                    [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
                    Start-Sleep -Milliseconds 900
                    if ((Get-PagePair).Current -eq 2) {
                        Ok '页码框输入后回车即可跳转'
                    } else {
                        Write-Output "[probe] 警告：键盘模拟未送达（前台焦点限制），回车跳转未能在本轮自动验证"
                    }

                    # --- 每页条数切换（真的选一项，不是只看选项名）---
                    $sizeCombo = Find-AnyById $proc.Id 'HubPageSizeCombo'
                    $pagesBefore = (Get-PagePair).Total
                    try {
                        $expandSize = $sizeCombo.GetCurrentPattern(
                            [System.Windows.Automation.ExpandCollapsePattern]::Pattern)
                        $expandSize.Expand()
                        Start-Sleep -Milliseconds 600
                        $itemCond2 = New-Object System.Windows.Automation.PropertyCondition(
                            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                            [System.Windows.Automation.ControlType]::ListItem)
                        $sizeItems = $sizeCombo.FindAll(
                            [System.Windows.Automation.TreeScope]::Descendants, $itemCond2)
                        $names = @(); foreach ($it in $sizeItems) { $names += $it.Current.Name }
                        if ($names -contains '20' -and $names -contains '50' -and $names -contains '100') {
                            Ok "每页条数可选 20 / 50 / 100（实际：$($names -join ' / ')）"
                        } else {
                            Fail "每页条数选项异常：$($names -join ' / ')"
                        }
                        # 选中「50」——同时把结果列表撑到必然可滚，供下面的"分页控件不随滚动"用
                        $picked = $false
                        foreach ($it in $sizeItems) {
                            if ($it.Current.Name -eq '50') {
                                $it.GetCurrentPattern(
                                    [System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
                                $picked = $true
                                break
                            }
                        }
                        Start-Sleep -Milliseconds 1500
                        if (-not $picked) { Fail '每页条数下拉框里没有 50 这一项' }
                        # 必须收起来：开着弹出的下拉框时，同一个 Tab 里后面的控件会被它挡住，
                        # FindFirst(Descendants) 会找不到（实测踩到，纯探针问题）
                        $expandSize.Collapse()
                        Start-Sleep -Milliseconds 400
                    } catch {
                        Fail "每页条数下拉框操作失败：$($_.Exception.Message)"
                    }
                    $pagesAfter = (Get-PagePair).Total
                    # 每页 20 → 50 时，同样条数下总页数必然变少（20/50 = 0.4）
                    if ($pagesAfter -gt 0 -and $pagesBefore -gt 0 -and $pagesAfter -lt $pagesBefore) {
                        Ok "每页条数切换生效（总页数 $pagesBefore → $pagesAfter）"
                    } else {
                        Fail "每页条数没有生效（总页数 $pagesBefore → $pagesAfter）"
                    }
                    if ((Get-PagePair).Current -eq 1) { Ok '切换每页条数后页码回到第 1 页' }
                    else { Fail "切换每页条数后页码没有回到第 1 页（$((Get-PagePair).Raw)）" }

                    # --- 结果列表独立滚动 + 分页控件不随列表滚动（任务三的布局要求）---
                    # 用 ScrollItemPattern.ScrollIntoView 而不是 ScrollPattern.Scroll：
                    # 后者在本页面板里的 ListBox 上实测不可靠（读出来可滚、真滚却抛异常）。
                    $listEl2 = Find-AnyById $proc.Id 'HubResultList'
                    $anchorEl = Find-AnyById $proc.Id 'HubPageInfoText'
                    $anchorBefore = $anchorEl.Current.BoundingRectangle
                    $listRect = $listEl2.Current.BoundingRectangle
                    $independent = $false
                    try {
                        $sp2 = $listEl2.GetCurrentPattern([System.Windows.Automation.ScrollPattern]::Pattern)
                        $independent = [bool]$sp2.Current.VerticallyScrollable
                    } catch { Write-Output "[probe] 提示：读不到结果列表的滚动状态（$($_.Exception.Message)）" }
                    if ($independent) {
                        Ok '每页 50 条时结果列表自己可滚动（列表区域独立滚动）'
                    } else {
                        Fail '每页 50 条时结果列表报告不可滚动（列表区域没有独立滚动）'
                    }

                    $scrolled = $false
                    try {
                        $itemCond3 = New-Object System.Windows.Automation.PropertyCondition(
                            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                            [System.Windows.Automation.ControlType]::ListItem)
                        $visible = $listEl2.FindAll([System.Windows.Automation.TreeScope]::Descendants, $itemCond3)
                        if ($visible.Count -gt 0) {
                            $lastVisible = $visible[$visible.Count - 1]
                            $lastVisible.GetCurrentPattern(
                                [System.Windows.Automation.ScrollItemPattern]::Pattern).ScrollIntoView()
                            Start-Sleep -Milliseconds 800
                            $scrolled = $true
                        }
                    } catch { Write-Output "[probe] 提示：ScrollIntoView 失败（$($_.Exception.Message)）" }

                    $anchorAfter = (Find-AnyById $proc.Id 'HubPageInfoText').Current.BoundingRectangle
                    $anchorMoved = [math]::Abs($anchorAfter.Top - $anchorBefore.Top) -gt 1
                    if (-not $scrolled) {
                        Fail '没能滚动结果列表，无法验证"分页控件不随列表滚动"'
                    } elseif ($anchorMoved) {
                        Fail "滚动后分页控件发生了位移（$($anchorBefore.Top) → $($anchorAfter.Top)）"
                    } else {
                        Ok '滚动结果列表时分页控件位置完全不变（固定可见）'
                    }
                    # 结构上分页控件必须在列表**下方**且落在窗口内
                    if ($anchorBefore.Top -ge ($listRect.Bottom - 2) -and $anchorBefore.Bottom -gt $anchorBefore.Top) {
                        Ok '分页控件位于结果列表下方且可见'
                    } else {
                        Fail "分页控件不在列表下方（列表底 $($listRect.Bottom)，分页顶 $($anchorBefore.Top)）"
                    }

                    # --- 本地实时筛选与结构化筛选（展开筛选面板）---
                    $filteredBefore = Get-FilteredCount
                    $expander = Find-AnyById $proc.Id 'HubFilterExpander'
                    if ($null -eq $expander) {
                        Fail '未找到筛选面板 HubFilterExpander'
                    } else {
                        $expander.GetCurrentPattern(
                            [System.Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
                        Start-Sleep -Milliseconds 700
                        foreach ($id in @('HubFilterBox', 'HubInstalledCombo', 'HubRecommendedCheck',
                                          'HubFilterResetButton')) {
                            if ($null -eq (Find-AnyById $proc.Id $id)) { Fail "筛选面板缺少控件 $id" }
                        }
                        # 分面复选框是代码生成的，没有 AutomationId，只能按类型在面板内找
                        $boxCond = New-Object System.Windows.Automation.PropertyCondition(
                            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                            [System.Windows.Automation.ControlType]::CheckBox)
                        $boxes = $expander.FindAll([System.Windows.Automation.TreeScope]::Descendants, $boxCond)
                        if ($boxes.Count -ge 6) {
                            Ok "结构化筛选器按分面生成了 $($boxes.Count) 个可选项"
                        } else {
                            Fail "结构化筛选器选项过少：$($boxes.Count)"
                        }

                        # 勾一个可用的参数量档位 → 本地筛选后条数必须变少、且页码回到第 1 页
                        $clicked = $false
                        foreach ($b in $boxes) {
                            if ($b.Current.IsEnabled -and $null -ne $b.Current.Name -and
                                $b.Current.Name -match 'B') {
                                $b.GetCurrentPattern(
                                    [System.Windows.Automation.TogglePattern]::Pattern).Toggle()
                                $clicked = $true
                                Write-Output "[probe] 勾选筛选项：$($b.Current.Name)"
                                break
                            }
                        }
                        Start-Sleep -Milliseconds 1400
                        $filteredAfter = Get-FilteredCount
                        if ($clicked -and $filteredAfter -ge 0 -and $filteredAfter -lt $filteredBefore) {
                            Ok "结构化筛选生效且状态显示三层数字（$filteredBefore → $filteredAfter）"
                        } else {
                            Fail "结构化筛选未收窄结果（$filteredBefore → $filteredAfter，clicked=$clicked）"
                        }
                        if ((Get-PagePair).Current -eq 1) { Ok '筛选后页码回到第 1 页' }
                        else { Fail '筛选后页码没有回到第 1 页' }

                        # 本地筛选框：实时过滤 + 筛空后给出"重新搜索远端"的出口
                        $filterBox = Find-AnyById $proc.Id 'HubFilterBox'
                        $filterBox.GetCurrentPattern(
                            [System.Windows.Automation.ValuePattern]::Pattern).SetValue('zzz_no_such_model_zzz')
                        Start-Sleep -Milliseconds 1600
                        if ($null -ne (Find-AnyById $proc.Id 'HubRemoteRetryButton')) {
                            Ok '本地筛选无结果时给出「重新搜索远端」的出口'
                        } else {
                            Fail '本地筛选无结果时没有给出重新搜索远端的入口'
                        }

                        # 重置筛选：条目数恢复，空结果出口消失
                        Invoke-El (Find-AnyById $proc.Id 'HubFilterResetButton')
                        Start-Sleep -Milliseconds 1600
                        if ((Get-FilteredCount) -eq $filteredBefore -and
                            $null -eq (Find-AnyById $proc.Id 'HubRemoteRetryButton')) {
                            Ok '「重置筛选」恢复全部结果'
                        } else {
                            Fail "重置筛选异常（条数 $((Get-FilteredCount))，期望 $filteredBefore）"
                        }
                    }

                    # --- 默认模型钉在列表上方且不计入分页计数 ---
                    if ($null -ne (Find-AnyById $proc.Id 'HubPinnedName')) {
                        $pinnedText = Get-TextById 'HubPinnedMeta'
                        Write-Output "[probe] 默认模型钉住行：$(Get-TextById 'HubPinnedName')｜$pinnedText"
                        Ok '默认模型单独钉在结果上方并标注来源'
                    } else {
                        Write-Output '[probe] 提示：本轮没有配置默认模型，钉住行按设计不显示'
                    }
                }
                $vp2 = $kw.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
                $vp2.SetValue('')
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
                  'StripContextButton', 'StripKvButton', 'StripToggleButton',
                  # 修复四：能力信息与"手动压缩上下文"入口也在轻量状态区
                  'StripCapabilityButton', 'StripCompactButton')) {
    if ($null -eq (Find-AnyById $proc.Id $id)) { Fail "轻量状态区缺少 $id" }
}
$txt = @{}
foreach ($id in @('SearchText', 'AutoText', 'MarketText', 'StripContextText', 'StripKvText',
                  'StripCapabilityText', 'StripCompactText')) {
    $el = Find-AnyById $proc.Id $id
    $txt[$id] = if ($null -ne $el) { $el.Current.Name } else { "" }
}
Write-Output "[probe] 轻量状态区文本：$($txt.Values -join ' | ')"
if ($txt['SearchText'] -notlike '*搜索*') { Fail "状态区搜索项文本异常：$($txt['SearchText'])" }
if ($txt['AutoText'] -notlike '*自动化*') { Fail "状态区自动化项文本异常：$($txt['AutoText'])" }
if ($txt['MarketText'] -notlike '*市场*') { Fail "状态区市场项文本异常：$($txt['MarketText'])" }
if ($txt['StripContextText'] -notlike '*上下文*%*') { Fail "状态区上下文项应显示百分比：$($txt['StripContextText'])" }
if ($txt['StripKvText'] -notlike '💾*') { Fail "状态区 KV Cache 项文本异常：$($txt['StripKvText'])" }
# 修复四：能力项要显示出"参数量/量化/窗口/速度"里的至少一项（后端就绪后必然有）
if ($txt['StripCapabilityText'] -like '*---*' -or [string]::IsNullOrWhiteSpace($txt['StripCapabilityText'])) {
    Fail "状态区能力项未填充（应显示参数量/量化/窗口/速度）：$($txt['StripCapabilityText'])"
}
if ($txt['StripCompactText'] -notlike '*压缩*') { Fail "状态区压缩上下文按钮文本异常：$($txt['StripCompactText'])" }
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

# ---------- 3c. 状态区在窄窗口下不被裁切（实测缺陷：右边看不到）----------
# 现象：状态项加多后（修复四的模型能力/压缩上下文/能力提示），单行横排会在可用宽度
# 不足时把右侧项目裁掉，而且折叠按钮在右列，被裁的项还会被它压住 —— 既看不到也点不到。
# 修复是把状态项容器从 StackPanel 换成 WrapPanel（换行而不是裁切）。
# 这里把主窗口缩到 700px 复现该场景并断言：所有可见状态项都必须落在窗口内、且不与折叠按钮重叠。
function Get-Win32Rect($el) {
    $hwnd = [System.IntPtr](Get-OwnerWindow $el).Current.NativeWindowHandle
    $r = New-Object Win32.Fg+RECT
    [void][Win32.Fg]::GetWindowRect($hwnd, [ref]$r)
    return @{ Handle = $hwnd; Rect = $r }
}
$stripIds = @('StripSearchButton', 'StripAutomationButton', 'StripMarketButton',
              'StripContextButton', 'StripKvButton', 'StripCapabilityButton',
              'StripCompactButton', 'StripToggleButton')
$before = Get-Win32Rect $main
$w0 = $before.Rect.Right - $before.Rect.Left
$h0 = $before.Rect.Bottom - $before.Rect.Top
try {
    [void][Win32.Fg]::MoveWindow($before.Handle, $before.Rect.Left, $before.Rect.Top, 700, ($h0), $true)
    Start-Sleep -Milliseconds 1200

    $win = $before.Rect
    [void][Win32.Fg]::GetWindowRect($before.Handle, [ref]$win)
    $winRect = $win
    $toggleEl = Find-AnyById $proc.Id 'StripToggleButton'
    $toggleRect = if ($null -ne $toggleEl) { $toggleEl.Current.BoundingRectangle } else { $null }

    $clippedNow = @()
    $texts = @()
    foreach ($id in $stripIds) {
        $el = Find-AnyById $proc.Id $id
        if ($null -eq $el) { continue }
        $r = $el.Current.BoundingRectangle
        if ($r.Width -le 0) { continue }
        if ($el.Current.IsOffscreen) { $clippedNow += "$id(off-screen)"; continue }
        if ($r.Right -gt ($winRect.Right - 2)) {
            $clippedNow += ("{0}(超出 {1:N0}px)" -f $id, ($r.Right - $winRect.Right))
            continue
        }
        if ($null -ne $toggleRect -and $id -ne 'StripToggleButton') {
            $overlapX = [math]::Min($r.Right, $toggleRect.Right) - [math]::Max($r.Left, $toggleRect.Left)
            $overlapY = [math]::Min($r.Bottom, $toggleRect.Bottom) - [math]::Max($r.Top, $toggleRect.Top)
            if ($overlapX -gt 2 -and $overlapY -gt 2) { $clippedNow += "$id(与折叠按钮重叠)" }
        }
    }
    if ($clippedNow.Count -gt 0) {
        Fail "窄窗口(700px)下状态项被裁切/遮挡：$($clippedNow -join '、')"
    } else {
        Ok "窄窗口(700px)下所有状态项都在窗口内且不与折叠按钮重叠"
    }
} finally {
    # 恢复原尺寸，避免影响后续检查
    [void][Win32.Fg]::MoveWindow($before.Handle, $before.Rect.Left, $before.Rect.Top, $w0, $h0, $true)
    Start-Sleep -Milliseconds 800
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
            # v0.2.1：对话框文案已多语言化（中文「即将执行 /clear。」/ 英文「/clear is about to run.」），
            # 因此断言不能再依赖某个固定前缀，改为检查**安全属性**本身：
            #   1) 文案里出现的正是被执行的指令名（/clear，而不是 /clearcache）；
            #   2) 同时给出了该指令的后果说明（不是只有一个光秃秃的命令名）。
            if ($dlgText -like '*/clear*') { Ok '执行 /clear 前出现确认提示（且明确是 /clear 而非 /clearcache）' }
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

# ---------- 5b. 思考内容分离（模型思考内容分离 任务四/五）----------
# 断言四件事：
#   1. 回答区正文里没有思考标签（`<think>` / `</think>` 这类）—— 这是本次修复的核心；
#   2. 出现过思考内容时必须有一个「思考过程」折叠区，且**默认收起**；
#   3. 折叠区可以展开、可以再收起（走 UIA 的展开/折叠接口，读屏与键盘用户走的就是这条路）；
#   4. 折叠区正文里能看到模型的思考文字（不是空壳）。
# 当前模型不产生思考内容时（例如默认的 Qwen3.5-0.8B），只断言第 1 条并明确跳过 2~4。
# WPF 的 Expander 同时暴露 ExpandCollapse 与 Toggle 两套模式（不同 .NET 版本实现不一），
# 所以这里两套都试：先 ExpandCollapse，取不到再退到 Toggle —— 探针不该因为接口差异假失败。
function Get-RegionExpanded($el) {
    try {
        $p = $el.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
        return $p.Current.ExpandCollapseState -eq [System.Windows.Automation.ExpandCollapseState]::Expanded
    } catch { }
    try {
        $p = $el.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
        return $p.Current.ToggleState -eq [System.Windows.Automation.ToggleState]::On
    } catch { }
    return $null
}
function Set-RegionExpanded($el, [bool]$expand) {
    try {
        $p = $el.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
        if ($expand) { $p.Expand() } else { $p.Collapse() }
        return $true
    } catch { }
    try {
        $p = $el.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
        $on = $p.Current.ToggleState -eq [System.Windows.Automation.ToggleState]::On
        if ($on -ne $expand) { $p.Toggle() }
        return $true
    } catch { }
    return $false
}

$thinkBox = Find-AnyById $proc.Id 'InputBox'
if ($null -eq $thinkBox) {
    Fail '未找到输入框 InputBox，无法验证思考区'
} else {
    try {
        $vpIn = $thinkBox.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        $vpIn.SetValue('一个笼子里有鸡和兔共 8 只，脚共 22 只，鸡兔各几只？')
        Start-Sleep -Milliseconds 400
        $sendBtn = Find-AnyById $proc.Id 'SendButton'
        if ($null -eq $sendBtn) { Fail '未找到发送按钮 SendButton' } else {
            Invoke-El $sendBtn
            Write-Output "[probe] 已发送测试问题，等待流式回答与思考区…"
        }

        # 结束判定用"发送按钮从 ⏹ 变回 ➤"这次状态翻转，而不是固定等几秒：
        # 生成快慢随模型与机器差异很大，等固定时长会误判。
        $swThink = [System.Diagnostics.Stopwatch]::StartNew()
        $region = $null
        $sawBusy = $false
        $expandedWhileStreaming = $false
        while ($swThink.Elapsed.TotalSeconds -lt 300) {
            Start-Sleep -Milliseconds 600
            $proc.Refresh()
            if ($proc.HasExited) { Fail '思考区检查期间进程退出'; break }
            if ($null -eq $region) { $region = Find-AnyById $proc.Id 'ReasoningRegion' }
            if ($null -ne $region -and -not $expandedWhileStreaming) {
                if ((Get-RegionExpanded $region) -eq $true) { $expandedWhileStreaming = $true }
            }
            $sendNow = Find-AnyById $proc.Id 'SendButton'
            $busy = ($null -ne $sendNow -and $sendNow.Current.Name -eq '⏹')
            if ($busy) { $sawBusy = $true }
            elseif ($sawBusy) { break }   # 忙碌过又还原 = 本轮生成结束
        }
        # 读"回答区正文"：优先用**应用自己的复制按钮**给出的文本（走 WPF TextRange.Text，
        # 就是用户看到的那份），UIA 的 Document/TextPattern 只作参考打印。
        # 为什么不信 UIA：实测 WPF RichTextBox 每次重设 Document 都会在 UIA 树里留下
        # 旧文档的 peer，探针读到的最长也只有 2 个字符 —— 那样"回答区里没有思考标签"
        # 会因为**读不到正文而假通过**（本轮最先就是这么假通过的，已改）。
        $pidCond = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $proc.Id)
        function Copy-NewestMessage {
            $wins = [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
                [System.Windows.Automation.TreeScope]::Children, $pidCond)
            $buttons = @()
            foreach ($win in $wins) {
                $bCond = New-Object System.Windows.Automation.PropertyCondition(
                    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                    [System.Windows.Automation.ControlType]::Button)
                foreach ($b in $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $bCond)) {
                    if ($b.Current.Name -like '*复制*') { $buttons += $b }
                }
            }
            if ($buttons.Count -eq 0) { return "" }
            try { Invoke-El $buttons[$buttons.Count - 1] } catch { return "" }
            Start-Sleep -Milliseconds 900
            return [string](Get-Clipboard -Raw)
        }
        $copied = [string](Copy-NewestMessage)
        $docText = ''
        $scroll = Find-AnyById $proc.Id 'MessageScrollViewer'
        if ($null -ne $scroll) {
            $docCond = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::Document)
            $docLens = @()
            foreach ($d in $scroll.FindAll([System.Windows.Automation.TreeScope]::Descendants, $docCond)) {
                try {
                    $t = $d.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern).DocumentRange.GetText(-1)
                } catch { continue }
                $docLens += $t.Length
                if ($t.Length -gt $docText.Length) { $docText = $t }
            }
            Write-Output "[probe] 消息区 Document 文本长度（仅参考）：$($docLens -join ', ')"
        }
        $answerText = $copied
        Write-Output "[probe] 回答区文本长度=$($answerText.Length)（UIA 读到 $($docText.Length) 字）思考区=$(if ($null -ne $region) {'有'} else {'无'}) 见过忙碌=$sawBusy"
        if ($answerText.Trim().Length -eq 0) {
            Fail '思考区检查：没有拿到回答正文（复制按钮不可用或本轮生成失败）'
        } else {
            $tagHits = @()
            foreach ($tag in @(([char]0x3C) + 'think' + ([char]0x3E),
                               ([char]0x3C) + '/' + 'think' + ([char]0x3E),
                               ([char]0x3C) + 'thinking' + ([char]0x3E))) {
                if ($answerText.Contains($tag)) { $tagHits += $tag }
            }
            if ($tagHits.Count -gt 0) {
                Fail "回答区里出现了思考标签：$($tagHits -join ' ')"
            } else {
                Ok "回答区只有答案，没有思考标签（正文 $($answerText.Length) 字）"
            }

            if ($null -eq $region) {
                Write-Output "[probe] 跳过折叠区检查：当前模型这次没有产生思考内容（默认模型 Qwen3.5-0.8B 即如此）"
            } else {
                if ($expandedWhileStreaming) { Ok '流式思考期间折叠区自动展开' }
                else { Write-Output "[probe] 提示：未捕捉到流式期间的自动展开（生成太快时可能一闪而过）" }
                # 生成结束后应回到收起状态
                $expandedAfter = Get-RegionExpanded $region
                if ($expandedAfter -eq $false) { Ok '生成结束后思考区处于收起状态' }
                else { Fail "生成结束后思考区应自动收起（当前展开=$expandedAfter）" }
                # 手动展开 → 再收起
                Set-RegionExpanded $region $true | Out-Null
                Start-Sleep -Milliseconds 500
                if ((Get-RegionExpanded $region) -eq $true) {
                    Ok '思考区可以手动展开'
                    # 思考区正文的校验方式：**读标题栏的字数**，而不是读正文控件。
                    # 理由（实测）：UIA 对 WPF RichTextBox 的 TextPattern 读取不可靠 ——
                    # 同一个控件在探针里最多只读到 2 个字符，读正文会得出"空思考区"的
                    # 假结论。标题里的"N 字"由同一份思考文本的长度算出（前端
                    # main.reasoning.count），正文为空时这里必然是 0 字，所以它既能证明
                    # "确实有思考内容"，又不受 UIA 文本实现差异影响。
                    $texts = @()
                    $textCond = New-Object System.Windows.Automation.PropertyCondition(
                        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                        [System.Windows.Automation.ControlType]::Text)
                    foreach ($el in $region.FindAll([System.Windows.Automation.TreeScope]::Descendants, $textCond)) {
                        if ($el.Current.Name) { $texts += $el.Current.Name }
                    }
                    Write-Output "[probe] 思考区标题文本：$($texts -join ' | ')"
                    $countMatch = [regex]::Match(($texts -join ' '), '(\d+)\s*字')
                    if ($countMatch.Success -and [int]$countMatch.Groups[1].Value -gt 0) {
                        Ok "思考区显示有 $($countMatch.Groups[1].Value) 字思考内容"
                    } else {
                        Fail "思考区没有给出思考字数（读到：$($texts -join ' | ')）"
                    }
                } else {
                    Fail '思考区无法手动展开'
                }
                Set-RegionExpanded $region $false | Out-Null
                Start-Sleep -Milliseconds 400
                if ((Get-RegionExpanded $region) -eq $false) { Ok '思考区可以再次收起' }
                else { Fail '思考区无法再次收起' }
            }
        }
    } catch {
        Fail "思考区检查异常：$_"
    }
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

# 撤掉思考区检查临时加的模型与配置覆盖层（必须放在**进程关闭之后**：
# 模型文件被后端进程占用时删不掉，本轮实测踩到过）
if (-not $KeepOpen) {
    try { $proc.CloseMainWindow() | Out-Null } catch { }
    Start-Sleep -Seconds 2
    $proc.Refresh(); if (-not $proc.HasExited) { try { $proc.Kill() } catch { } }
}
Start-Sleep -Milliseconds 800
Remove-ProbeModelOverride

if ($failures.Count -gt 0) { exit 1 }
Write-Output "[probe] 全部通过"
