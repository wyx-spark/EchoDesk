# EchoDesk 发布物验证编排（v0.2.1 修复版）
#
# 把"对着最终发布物跑一遍全部检查"固定成一条命令，避免人工漏项。
# 每项打印 [OK]/[FAIL] 与耗时，最后给一张汇总表；有任何 FAIL 时退出码非 0。
#
# 覆盖：
#   * 发布目录结构（修复七）：外层无 exe/dll、app\ 就位、快捷方式指向与工作目录正确
#   * 版本资源：前端 exe 与后端 exe 的文件版本
#   * 后端自检与各专题测试（selftest / 文件解析 / 纯文本 / 结果目录 / 技能 / 多语言 /
#     开发者模式 / 模型能力 / 后端协议端到端）
#   * 静态检查：语言包完整性、主题色板与对比度、emoji 字形覆盖、文档链接
#   * 图形与真实渲染：UI 探针、像素对比度校验（需交互式桌面）
#   * 需要真实浏览器/联网：自动化端到端、模型中心端到端
#
# 实现说明：判定用**抛异常**而不是 `exit`。在 scriptblock 里 `exit` 会终止整个
# PowerShell 会话（脚本会在这里静默结束，只剩第一项的结果），踩过一次。
#
# 用法：
#   powershell -File tools\verify_release.ps1
#   powershell -File tools\verify_release.ps1 -SkipGui -SkipE2E
param(
    [string]$Version = "0.2.1",
    [switch]$SkipGui,          # 跳过需要交互式桌面的两项（UI 探针 / 像素校验）
    [switch]$SkipE2E           # 跳过需要联网或真实 Edge 的端到端项
)

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
$Out = Join-Path $Root "publish\EchoDesk-v$Version"
$AppOut = Join-Path $Out "app"
$BackendExe = Join-Path $AppOut "backend\EchoDeskBackend.exe"

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$results = New-Object System.Collections.ArrayList

# 每个检查的输出都落一份日志：失败时打印尾部，成功也留着备查。
# 之前用 `> $null` 丢掉了子工具输出，结果只看到"退出码 1"而无法定位（实测踩到过）。
$LogDir = Join-Path $Root "artifacts\verify-logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# 断言外部命令成功（用退出码），失败即抛异常
function Assert-Exit([string]$What) {
    if ($LASTEXITCODE -ne 0) { throw "$What 退出码 $LASTEXITCODE" }
}

<#
    运行外部命令并把全部输出写进 artifacts\verify-logs\<名称>.log。
    失败时把日志尾部几行拼进异常消息 —— 这样"哪一项失败、失败在哪一行"一眼可见。
#>
function Invoke-Native([string]$Name, [string]$Exe, [string[]]$CmdArgs) {
    $log = Join-Path $LogDir (($Name -replace '[^\w\u4e00-\u9fff\-]', '_') + ".log")
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Exe @CmdArgs *> $log
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0) {
        $tail = @(Get-Content -LiteralPath $log -Tail 6 -ErrorAction SilentlyContinue |
                  Where-Object { $_.Trim().Length -gt 0 })
        throw "退出码 $code（日志 $log）：`n      " + (($tail | Select-Object -Last 4) -join "`n      ")
    }
    return $log
}

function Invoke-Python([string]$Name, [string]$Script) {
    return Invoke-Native $Name "python" @($Script)
}

function Invoke-Check {
    param([string]$Name, [scriptblock]$Body)
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $ok = $false
    $detail = ""
    try {
        $output = & $Body
        $ok = $true
        $detail = if ($output) { ($output | Select-Object -Last 1).ToString() } else { "通过" }
    }
    catch {
        $ok = $false
        $detail = $_.Exception.Message
    }
    $sw.Stop()
    $mark = if ($ok) { "[OK]  " } else { "[FAIL]" }
    $color = if ($ok) { "Gray" } else { "Red" }
    Write-Host ("{0} {1,-32} {2,6:N1}s  {3}" -f $mark, $Name, $sw.Elapsed.TotalSeconds, $detail) -ForegroundColor $color
    [void]$results.Add([pscustomobject]@{
        Name = $Name; Ok = $ok
        Seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1); Detail = $detail
    })
}

Write-Host "== EchoDesk 发布物验证（v$Version）==" -ForegroundColor Cyan
Write-Host "发布目录: $Out"
if (-not (Test-Path -LiteralPath $BackendExe)) {
    Write-Host "未找到打包后的后端: $BackendExe" -ForegroundColor Red
    Write-Host "请先执行: & tools\build_release.ps1 -IncludeModels -MigrateFrom publish\EchoDesk-v0.2.0" -ForegroundColor Red
    exit 2
}

Push-Location $Root
try {
    # ---------- 1. 目录结构与元数据 ----------
    Invoke-Check "发布目录结构（修复七）" {
        $bad = @(Get-ChildItem $Out -Recurse -File -Include *.exe, *.dll, *.pdb |
                 Where-Object { $_.FullName -notlike "$AppOut*" })
        if ($bad.Count -gt 0) { throw "外层出现程序文件: $($bad[0].FullName)" }
        foreach ($need in @("app\EchoDesk.exe", "app\EchoDesk.ico", "app\backend\EchoDeskBackend.exe",
                            "app\lang\ui", "lang", "examples\skills", "docs",
                            "EchoDesk-v$Version.lnk", "使用说明.txt",
                            # 修复五：合规文件必须随包分发（外层的许可证 + docs 下的合规文档）
                            "LICENSE", "NOTICE",
                            "docs\PRIVACY.md", "docs\EULA.md", "docs\SECURITY.md",
                            "docs\THIRD_PARTY_LICENSES.md", "docs\SBOM.json",
                            "docs\zh-Hans\COMPLIANCE.md", "docs\en\COMPLIANCE.md")) {
            if (-not (Test-Path -LiteralPath (Join-Path $Out $need))) { throw "缺少 $need" }
        }
        $link = Join-Path $Out "EchoDesk-v$Version.lnk"
        if (-not (Test-Path -LiteralPath $link)) { throw "缺少快捷方式 $link" }
        $shell = New-Object -ComObject WScript.Shell
        $sc = $shell.CreateShortcut($link)
        $wantTarget = (Join-Path $AppOut "EchoDesk.exe")
        if ($sc.TargetPath -ne $wantTarget) { throw "快捷方式目标不对: $($sc.TargetPath)" }
        if ($sc.WorkingDirectory.TrimEnd('\') -ne $Out.TrimEnd('\')) {
            throw "快捷方式工作目录不是外层: $($sc.WorkingDirectory)"
        }
        "结构/入口/工作目录正确"
    }

    Invoke-Check "版本资源 = 0.2.1.0" {
        $front = (Get-Item (Join-Path $AppOut "EchoDesk.exe")).VersionInfo.FileVersion
        $back = (Get-Item $BackendExe).VersionInfo.FileVersion
        if ($front -ne "0.2.1.0" -or $back -ne "0.2.1.0") {
            throw "版本不符：前端=$front 后端=$back"
        }
        "前端=$front 后端=$back"
    }

    # ---------- 2. 后端测试 ----------
    Invoke-Check "后端 selftest" { Invoke-Python "selftest" "tools\selftest.py"; "通过" }
    Invoke-Check "文件解析" { Invoke-Python "文件解析" "tools\test_file_parsers.py"; "通过" }
    Invoke-Check "纯文本落盘" { Invoke-Python "纯文本落盘" "tools\test_plaintext.py"; "通过" }
    Invoke-Check "结果目录结构" { Invoke-Python "结果目录" "tools\test_results_dir.py"; "通过" }
    Invoke-Check "技能运行期加载器" { Invoke-Python "技能" "tools\test_skills_runtime.py"; "通过" }
    Invoke-Check "多语言机制" { Invoke-Python "多语言" "tools\test_i18n.py"; "通过" }
    Invoke-Check "开发者模式（修复一）" { Invoke-Python "开发者模式" "tools\test_dev_mode.py"; "通过" }
    Invoke-Check "模型能力辅助（修复四）" { Invoke-Python "模型能力" "tools\test_capability.py"; "通过" }
    Invoke-Check "后端协议端到端" { Invoke-Python "后端协议端到端" "tools\test_backend_e2e.py"; "通过" }

    # ---------- 3. 静态检查 ----------
    Invoke-Check "语言包完整性" { Invoke-Python "语言包" "tools\check_lang.py"; "通过" }
    Invoke-Check "主题色板与对比度（修复二）" { Invoke-Python "主题" "tools\check_theme.py"; "通过" }
    Invoke-Check "emoji 字形覆盖（修复六）" {
        Invoke-Native "emoji" "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass",
                                             "-File", "tools\check_emoji.ps1")
        "通过"
    }
    Invoke-Check "文档链接与编码" {
        Invoke-Native "文档链接" "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass",
                                               "-File", "tools\check_docs_links.ps1")
        "通过"
    }

    # ---------- 4. 需要交互式桌面的项 ----------
    if ($SkipGui) {
        Write-Host "[SKIP] UI 探针 / 像素校验（-SkipGui）" -ForegroundColor Yellow
    }
    else {
        Invoke-Check "UI 探针（窗口/页签/字段帮助）" {
            Invoke-Native "ui-probe" "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass",
                                                   "-File", "tools\ui_probe.ps1")
            "通过"
        }
        Invoke-Check "像素校验（修复二/六）" {
            Invoke-Native "ui-pixels" "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass",
                                                     "-File", "tools\check_ui_pixels.ps1", "-Theme", "both")
            "通过"
        }
    }

    # ---------- 5. 需要真实浏览器 / 联网的项 ----------
    if ($SkipE2E) {
        Write-Host "[SKIP] 自动化端到端 / 模型中心端到端（-SkipE2E）" -ForegroundColor Yellow
    }
    else {
        Invoke-Check "自动化端到端（真实 Edge）" { Invoke-Python "自动化端到端" "tools\test_automation_e2e.py"; "通过" }
        Invoke-Check "模型中心端到端（联网）" { Invoke-Python "模型中心端到端" "tools\test_model_hub_e2e.py"; "通过" }
    }
}
finally {
    Pop-Location
}

# ---------- 汇总 ----------
$failed = @($results | Where-Object { -not $_.Ok })
Write-Host ""
Write-Host "===== 汇总 =====" -ForegroundColor Cyan
$results | ForEach-Object {
    $mark = if ($_.Ok) { "OK  " } else { "FAIL" }
    Write-Host ("  {0} {1,-32} {2,6:N1}s" -f $mark, $_.Name, $_.Seconds)
}
$total = ($results | Measure-Object -Property Seconds -Sum).Sum
Write-Host ("  共 {0} 项，通过 {1} 项，失败 {2} 项，总耗时 {3:N1}s" -f `
    $results.Count, ($results.Count - $failed.Count), $failed.Count, $total)

if ($failed.Count -gt 0) {
    Write-Host "`n失败项：" -ForegroundColor Red
    $failed | ForEach-Object { Write-Host "  - $($_.Name)：$($_.Detail)" -ForegroundColor Red }
    exit 1
}
Write-Host "`n✓ 发布物验证全部通过" -ForegroundColor Green
exit 0
