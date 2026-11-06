# 英文环境 UI 扫描（WPF）—— 主窗口 + 设置窗口全部页签（v0.2.3 起为 8 个），抓全部可见控件文本查中文。
#
# 为什么需要它（维护者要求）：静态扫描（check_lang / audit_lang_keys）只看得到源码里的字面量，
# 抓不到"动态拼接"与"渲染期才存在"的中文。这里用 UI Automation 在**真实进程**里读控件，
# 再用 [\u4e00-\u9fa5] 判定；每页同时截一张图到 artifacts/ui-sweep/ 供人工复核。
#
# 与 tools/ui_probe.ps1 的分工：那个验"功能能点通"，本脚本只做一件事 —— **英文界面里有没有中文**。
# 用法：
#   powershell -NoProfile -File tools\ui_sweep_wpf.ps1
#   powershell -NoProfile -File tools\ui_sweep_wpf.ps1 -Exe <另一份 EchoDesk.exe>
param(
    [string]$Exe = "",
    [string]$OutDir = "",
    [int]$TimeoutSec = 180,
    # 扫描语言：en（默认，查英文界面里的中文残留）/ zh-Hans（查中文界面里的英文残留）
    [ValidateSet('en', 'zh-Hans')][string]$Language = 'en',
    # 中文模式下只列"疑似英文"清单供人工确认（技术名词会混在里面），不据此判死
    [switch]$CheckLatin
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type -AssemblyName System.Drawing

$Root = Split-Path -Parent $PSScriptRoot
if (-not $Exe) {
    # [EN] The release directory is derived from backend\version.py so that the sweep always
    #      enumerates the current build instead of an obsolete release tree.
    # [ZH] 发布目录由 backend\version.py 派生，使全页扫描始终枚举当前构建，而非过期的发布树。
    $VersionFile = Join-Path $Root "backend\version.py"
    $versionMatch = Select-String -Path $VersionFile -Pattern 'APP_VERSION\s*=\s*["'']([^"'']+)["'']' |
                    Select-Object -First 1
    if (-not $versionMatch) { throw "读不出 APP_VERSION：$VersionFile（可用 -Exe 显式指定）" }
    $releaseVersion = $versionMatch.Matches[0].Groups[1].Value.TrimStart('v')
    $Exe = Join-Path $Root "publish\EchoDesk-v$releaseVersion\app\EchoDesk.exe"
}
if (-not (Test-Path $Exe)) { Write-Output "[sweep] FAIL 找不到被测程序：$Exe"; exit 1 }
$Exe = (Resolve-Path $Exe).Path
$AppDir = Split-Path -Parent $Exe                 # ……\app
$PortableRoot = Split-Path -Parent $AppDir        # 便携布局下用户数据在外层
if (-not $OutDir) { $OutDir = Join-Path $Root "artifacts\ui-sweep" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$Cjk = [regex]'[\u4e00-\u9fa5]'
$Autonyms = @('简体中文', '繁體中文')
# 刻意双语显示的标题（"语言 / Language" 这类，两种用户都能一眼找到）——不是漏译。
$BilingualLabels = @('语言 / Language')
# 插件元数据属于**专有名词 / 品牌名**，不参与 UI 本地化翻译（维护者裁定）——
# 界面里显示插件自己的名字（哪怕它是中文）是正确的产品设计，不是漏译。
# 只列插件显示名本身，不放宽别的文案。
$PluginNames = @('Hello World 示例插件', 'OFD 文档读取', 'Hello World example plugin', 'OFD document reader')
$PluginNameWithId = [regex]'^\s*[^()（）]{1,60}\s*[（(][A-Za-z0-9_.\-]{2,60}[)）]\s*$'
$LogLine = [regex]'^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}'
$PathFrag = [regex]'[A-Za-z]:\\[^\r\n，。；]*|/[A-Za-z0-9_.\-]+(?:/[^\r\n，。；]*)*'

$failures = New-Object System.Collections.ArrayList
$latinCandidates = New-Object System.Collections.ArrayList
$texts = 0
$shots = 0
# 实际扫到的设置页签数（v0.2.3：页签会继续增加，PASS 文案不能再写死"7 页签"）
$tabCount = 0

function Fail([string]$msg) { [void]$failures.Add($msg); Write-Output "[sweep] FAIL $msg" }
function Info([string]$msg) { Write-Output "[sweep] $msg" }

# 中文界面里本来就该是英文的技术名词 / 框架文案（品牌、协议、快捷键、参数名、滚动条无障碍名）
$LatinAllowed = @(
    'EchoDesk', 'WPF', 'Avalonia', 'Agent', 'MCP', 'API', 'Token', 'GGUF', 'HF', 'GitHub',
    'URL', 'KV', 'Top-P', 'top_p', 'Ctrl', 'Enter', 'Esc', 'Tab', 'Markdown', 'JSON',
    'HTML', 'HTTP', 'HTTPS', 'Python', 'Windows', 'Linux', 'macOS', 'llama', 'Qwen',
    'Studio', 'Void', 'Entropia', 'GPU', 'CPU', 'ID', 'OK', 'PDF', 'DOCX', 'XLSX',
    'PPTX', 'OFD', 'UTF', 'SHA', 'SQL', 'local', 'auto', 'chat', 'contract', 'general',
    'light', 'dark', 'English',
    'Line up', 'Line down', 'Line left', 'Line right',
    'Page up', 'Page down', 'Page left', 'Page right', 'Position'
)
$LatinSentence = [regex]'^[A-Za-z][A-Za-z0-9 .,:;!?()\[\]/+\-–—_&%''"]*$'

# 像"英文文案"而不是路径/URL/类型名/版本号/ID 的文本
function Looks-LikeEnglish([string]$text) {
    $t = $text.Trim()
    if ($t.Length -lt 3) { return $false }
    if ($t -match '[\u4e00-\u9fa5]') { return $false }
    if ($t -match '^v?\d+(\.\d+)+$') { return $false }
    if ($t -match 'Avalonia\.|EchoDesk\.|System\.') { return $false }
    if (-not $LatinSentence.IsMatch($t)) { return $false }
    $word = $t.TrimEnd('.', ':', '!', '?', ' ')
    return ($LatinAllowed -notcontains $word)
}

function Allowed([string]$text) {
    if ($LogLine.IsMatch($text.TrimStart())) { return $true }
    $probe = $text
    foreach ($a in $Autonyms) { $probe = $probe.Replace($a, '') }
    foreach ($a in $BilingualLabels) { $probe = $probe.Replace($a, '') }
    $probe = $PathFrag.Replace($probe, '')
    # 插件显示名（专有名词）：整串就是"插件名"或"插件名 (id)"时直接放过。
    $trimmed = $text.Trim()
    foreach ($p in $PluginNames) {
        if ($trimmed -eq $p -or $trimmed.StartsWith($p)) { return $true }
    }
    if ($PluginNameWithId.IsMatch($trimmed)) { return $true }
    return -not $Cjk.IsMatch($probe)
}

# 把窗口按屏幕位置截一张图（与 check_ui_pixels.ps1 同一做法）
function Save-Shot($win, [string]$name) {
    try {
        $r = $win.Current.BoundingRectangle
        $w = [int]$r.Width; $h = [int]$r.Height
        if ($w -le 0 -or $h -le 0) { return 0 }
        $bmp = New-Object System.Drawing.Bitmap($w, $h)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.CopyFromScreen([int]$r.Left, [int]$r.Top, 0, 0, (New-Object System.Drawing.Size($w, $h)))
        $path = Join-Path $OutDir $name
        $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
        $g.Dispose(); $bmp.Dispose()
        return 1
    } catch {
        Info "截图失败 $name：$($_.Exception.Message)"
        return 0
    }
}

# 遍历一棵 UIA 子树，返回 @{Kind;Text} 列表
function Collect-Texts($root) {
    $list = New-Object System.Collections.ArrayList
    $all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($el in $all) {
        # 只统计**当前可见**的控件：设置窗口里未选中的页签内容也在控件树里，
        # 不滤掉就会把别的页签的文案算到当前页上（第一版就出现过每页都有插件名的假象）。
        if ($el.Current.IsOffscreen) { continue }
        $kind = $el.Current.ControlType.ProgrammaticName -replace '^ControlType\.', ''
        $name = $el.Current.Name
        if ($name) { [void]$list.Add(@{ Kind = "$kind.Name"; Text = $name }) }
        $help = $el.Current.HelpText
        if ($help) { [void]$list.Add(@{ Kind = "$kind.HelpText"; Text = $help }) }
        # ComboBox：把当前选中项也收进来（UIA 的 Name 常常是空的）
        if ($kind -eq 'ComboBox') {
            try {
                $sel = $el.GetCurrentPattern([System.Windows.Automation.SelectionPattern]::Pattern)
                foreach ($item in $sel.Current.GetSelection()) {
                    if ($item.Current.Name) { [void]$list.Add(@{ Kind = 'ComboBox.Selected'; Text = $item.Current.Name }) }
                }
            } catch { }
        }
    }
    return $list
}

function Sweep-Page($win, [string]$pageName, [string]$shotName) {
    $script:texts += (Collect-Texts $win).Count
    foreach ($row in (Collect-Texts $win)) {
        if (-not $row.Text) { continue }
        $clean = ($row.Text -replace '[\r\n]+', ' ').Trim()
        if ($clean.Length -gt 120) { $clean = $clean.Substring(0, 120) + '…' }
        if ($CheckLatin) {
            if (Looks-LikeEnglish $row.Text) {  [void]$script:latinCandidates.Add("【$pageName】的【$($row.Kind)】：$clean") }
            continue
        }
        if (Allowed $row.Text) { continue }
        Fail "【$pageName】的【$($row.Kind)】显示了中文：【$clean】"
    }
    $script:shots += (Save-Shot $win $shotName)
}

# ---------- 强制英文：用一个**独立的数据目录**（ECHODESK_DATA）----------
# 为什么不用"往程序目录写 ui_state.json"：开发构建 / 便携布局下用户数据目录并不总是程序目录，
# 上一版就是写错了地方，导致扫描到的是**中文界面**、把整屏文案当成了残留（全是误报）。
# ECHODESK_DATA 是程序自己提供的入口（ShortcutService.DataDirectoryEnvironmentVariable），最可靠。
$DataDir = Join-Path $OutDir "state-wpf"
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$statePath = Join-Path $DataDir "ui_state.json"
$backup = $null
if (Test-Path $statePath) {
    $backup = Get-Content $statePath -Raw -Encoding UTF8
}
$env:ECHODESK_DATA = $DataDir
# 首启标记一并写上：否则会弹"首次使用"对话框挡住设置按钮（ui_probe.ps1 里是先关对话框，这里直接置位更稳）。
$state = @{ Language = $Language; FirstRunDone = $true; ShortcutPromptDeclined = $true }
if ($backup) {
    try { $state = $backup | ConvertFrom-Json -AsHashtable } catch { $state = @{ Language = $Language } }
    $state['Language'] = $Language
}
($state | ConvertTo-Json -Depth 5) | Set-Content -Path $statePath -Encoding UTF8

$proc = $null
$settingsWin = $null
try {
    Info "被测程序：$Exe"
    $proc = Start-Process -FilePath $Exe -PassThru
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $mainWin = $null
    while ((Get-Date) -lt $deadline -and -not $mainWin) {
        Start-Sleep -Milliseconds 700
        $cond = New-Object System.Windows.Automation.AndCondition(
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $proc.Id)),
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::Window)))
        $mainWin = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
            [System.Windows.Automation.TreeScope]::Children, $cond)
    }
    if (-not $mainWin) { Fail "主窗口在 $TimeoutSec 秒内没出现"; exit 1 }
    Info "主窗口已就绪：$($mainWin.Current.Name)"
    Start-Sleep -Milliseconds 1500

    Sweep-Page $mainWin '主窗口' 'wpf-01-main.png'
    Info '主窗口扫描完成'

    # ---- 打开设置窗口 ----
    $btnCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'SettingsButton')
    $btn = $mainWin.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $btnCond)
    if (-not $btn) { Fail '未找到 SettingsButton，无法打开设置窗口' }
    else {
        $btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()

        # 设置窗口的稳定定位方式（与 ui_probe.ps1 一致）：先在**整个进程**里找 AutomationId='Tabs'，
        # 再沿控件树向上回溯到最近的那个 Window。设置窗口带 Owner，直接枚举桌面顶层窗口会漏掉它。
        $pidCond = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $proc.Id)
        $tabsCond = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'Tabs')
        $andCond = New-Object System.Windows.Automation.AndCondition($pidCond, $tabsCond)

        $settingsDeadline = (Get-Date).AddSeconds(20)
        $tabsEl = $null
        while ((Get-Date) -lt $settingsDeadline -and -not $tabsEl) {
            Start-Sleep -Milliseconds 400
            $tabsEl = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
                [System.Windows.Automation.TreeScope]::Descendants, $andCond)
        }
        if ($tabsEl) {
            $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
            $node = $tabsEl
            while ($node -and $node.Current.ControlType -ne [System.Windows.Automation.ControlType]::Window) {
                $node = $walker.GetParent($node)
            }
            $settingsWin = $node
        }
        if (-not $settingsWin) { Fail '设置窗口没出现（20 秒内找不到 Tabs）' }
        else {
            Info "设置窗口已就绪：$($settingsWin.Current.Name)"
            Start-Sleep -Milliseconds 1200
            $tabCond = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::TabItem)
            # ---- 前置断言：界面语言必须真的切到了英文，否则后面的"中文残留"全是误报 ----
            $langCond = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'LanguageCombo')
            $langCombo = $settingsWin.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $langCond)
            $langText = ""
            if ($langCombo) {
                try {
                    $sel = $langCombo.GetCurrentPattern([System.Windows.Automation.SelectionPattern]::Pattern)
                    foreach ($it in $sel.Current.GetSelection()) { $langText += $it.Current.Name }
                } catch { }
                if (-not $langText) { $langText = $langCombo.Current.Name }
            }
            $expectedLang = if ($Language -eq 'zh-Hans') { '简体中文' } else { 'English' }
            if ($langText -notmatch [regex]::Escape($expectedLang)) {
                Fail ("界面语言没有切成 {0}（LanguageCombo 选中项='{1}'）—— 扫描结果不可信，先查强制语言这条路" -f $expectedLang, $langText)
                Write-Output 'SWEEP-RESULT: FAIL'
                exit 1
            }
            Info "已确认界面语言为 $expectedLang（LanguageCombo='$langText'）"

            $tabs = $settingsWin.FindAll([System.Windows.Automation.TreeScope]::Descendants, $tabCond)
            $tabCount = $tabs.Count
            Info "设置窗口有 $($tabs.Count) 个页签"
            for ($i = 0; $i -lt $tabs.Count; $i++) {
                $tab = $tabs.Item($i)
                $tabName = $tab.Current.Name
                try {
                    $tab.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
                } catch {
                    Fail "页签 $tabName 无法选中：$($_.Exception.Message)"
                    continue
                }
                Start-Sleep -Milliseconds 900
                Sweep-Page $settingsWin "设置窗口/$tabName" ("wpf-settings-{0:d2}.png" -f ($i + 1))
                Info "设置窗口/$tabName 扫描完成"
            }
        }
    }
} finally {
    if ($proc -and -not $proc.HasExited) { try { $proc.Kill() } catch { } }
    if ($backup -ne $null) { Set-Content -Path $statePath -Value $backup -Encoding UTF8 -NoNewline }
    else { Remove-Item $statePath -Force -ErrorAction SilentlyContinue }
    Remove-Item Env:\ECHODESK_DATA -ErrorAction SilentlyContinue
}

Info "共扫描控件文本 $texts 条，截图 $shots 张 → $OutDir"
if ($CheckLatin) {
    Info "中文界面里疑似英文残留 $($latinCandidates.Count) 条（需人工确认）："
    foreach ($line in $latinCandidates) { Write-Output "    - $line" }
}
if ($failures.Count -eq 0) {
    if ($CheckLatin) {
        Write-Output ("[sweep] PASS 主窗口 + 设置窗口 {0} 页签：中文界面里未发现白名单外的英文残留；候选 {1} 条已列出供人工确认" -f $tabCount, $latinCandidates.Count)
    } else {
        Write-Output ("[sweep] PASS 主窗口 + 设置窗口 {0} 页签：全部控件文本均为英文（无中文字符）" -f $tabCount)
    }
    Write-Output 'SWEEP-RESULT: PASS'
    exit 0
}
Write-Output ("[sweep] FAIL 发现 {0} 处中文残留" -f $failures.Count)
Write-Output 'SWEEP-RESULT: FAIL'
exit 1
