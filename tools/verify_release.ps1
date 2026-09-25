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
    Invoke-Check "发布目录结构（修复七 + 发布任务书全局要求一/四/五）" {
        $bad = @(Get-ChildItem $Out -Recurse -File -Include *.exe, *.dll, *.pdb |
                 Where-Object { $_.FullName -notlike "$AppOut*" })
        if ($bad.Count -gt 0) { throw "外层出现程序文件: $($bad[0].FullName)" }

        # 必备文件（任务书「全局要求四」：根目录 + docs\ + 发布物额外必备）
        $need = @(
            "app\EchoDesk.exe", "app\EchoDesk.ico", "app\backend\EchoDeskBackend.exe",
            "app\lang\ui", "lang", "examples\skills", "results", "docs",
            "USAGE.txt", "install-complete.bat",
            "README.md", "README.zh-Hans.md", "CHANGELOG.md",
            "LICENSE", "LICENSE.zh-Hans.md", "NOTICE", "NOTICE.zh-Hans.md",
            "SECURITY.md", "SECURITY.zh-Hans.md",
            "config.example.json", "install-complete.bat",
            "docs\PRIVACY.md", "docs\PRIVACY.zh-Hans.md",
            "docs\EULA.md", "docs\EULA.zh-Hans.md", "docs\SECURITY.md",
            "docs\THIRD_PARTY_LICENSES.md", "docs\THIRD_PARTY_LICENSES.zh-Hans.md",
            "docs\SBOM.json", "docs\DEVELOPER.md",
            "docs\zh-Hans\COMPLIANCE.md", "docs\en\COMPLIANCE.md",
            "docs\zh-Hans\README.md", "docs\en\README.md",
            "docs\zh-Hans\USER_GUIDE.md", "docs\en\USER_GUIDE.md",
            "docs\zh-Hans\FAQ.md", "docs\en\FAQ.md",
            "docs\zh-Hans\AUTOMATION_GUIDE.md", "docs\en\AUTOMATION_GUIDE.md"
        )
        foreach ($n in $need) {
            if (-not (Test-Path -LiteralPath (Join-Path $Out $n))) { throw "缺少 $n" }
        }

        # 发布物里**不应该**有预置快捷方式：.lnk 记的是绝对路径，在构建机上生成
        # 就会把维护者的个人路径（C:\Users\<用户名>\...）带进发布物，
        # 而任务书「全局要求五」把个人绝对路径列为绝对禁止项。
        # 快捷方式由 install-complete.bat 在目标机器上创建（见第 12 节的功能验证）。
        $links = @(Get-ChildItem $Out -Recurse -File -Filter *.lnk -ErrorAction SilentlyContinue)
        if ($links.Count -gt 0) {
            throw "发布物内出现预置 .lnk（会泄露构建机绝对路径）: $($links[0].FullName)"
        }
        "结构正确（必备文件 $($need.Count) 项）· 无预置 .lnk"
    }

    Invoke-Check "全英文命名 + 无过程文档 + 无运行期残留" {
        $cjk = [regex]'[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]'
        $cn = @(Get-ChildItem $Out -Recurse -Force -ErrorAction SilentlyContinue |
                Where-Object { $cjk.IsMatch($_.Name) } |
                ForEach-Object { $_.Name })
        if ($cn.Count -gt 0) { throw "出现非 ASCII 文件名/目录名: $($cn[0])" }

        $rec = @(Get-ChildItem $Out -Recurse -File -ErrorAction SilentlyContinue |
                 Where-Object { $_.Name -match '^(v?0\.\d+\.\d+)-(研发记录|修复记录|发布记录|研发过程)\.md$' })
        if ($rec.Count -gt 0) { throw "发布物内含过程文档（任务书全局要求五）: $($rec[0].Name)" }

        $leftover = @(Get-ChildItem $Out -Recurse -File -ErrorAction SilentlyContinue |
                      Where-Object { $_.Name -in @('ui_state.json', 'snippets.json', 'browser_state.json',
                              'backend_crash.log', 'frontend_crash.log', 'automation_log.jsonl',
                              'skills_local.json', 'summary_cache.json', 'market_cache.json',
                              'skills_dev.log', 'automation_templates.json', 'models_registry.json') })
        if ($leftover.Count -gt 0) { throw "运行期残留: $($leftover[0].Name)" }
        "无中文名/无过程文档/无运行期残留"
    }

    Invoke-Check "版本资源 = 0.2.1.0" {
        $front = (Get-Item (Join-Path $AppOut "EchoDesk.exe")).VersionInfo.FileVersion
        $back = (Get-Item $BackendExe).VersionInfo.FileVersion
        if ($front -ne "0.2.1.0" -or $back -ne "0.2.1.0") {
            throw "版本不符：前端=$front 后端=$back"
        }
        "$front / $back"
    }

    # ---------- 1b. 自解压交付物（发布任务书 任务一/二/三 + 任务六自检） ----------
    # 这些只在跑过 -MakeSfx 之后存在；没跑就明确跳过（而不是静默通过）。
    $PublishDir = Join-Path $Root "publish"
    $SetupWith = Join-Path $PublishDir "EchoDesk-v$Version-Setup.exe"
    $SetupNo = Join-Path $PublishDir "EchoDesk-v$Version-NoModel-Setup.exe"
    $Sums = Join-Path $PublishDir "SHA256SUMS.txt"

    if (-not (Test-Path $SetupWith)) {
        Write-Host "[SKIP] 自解压包检查（未跑 -MakeSfx，缺 $SetupWith）" -ForegroundColor Yellow
    }
    else {
        Invoke-Check "自解压包：SHA256 与 SHA256SUMS.txt 一致" {
            if (-not (Test-Path $Sums)) { throw "缺少 SHA256SUMS.txt" }
            $changed = @()
            $checked = 0
            foreach ($line in Get-Content -LiteralPath $Sums -Encoding UTF8) {
                if ($line.Trim().Length -eq 0) { continue }
                $parts = $line -split '\s+', 2
                if ($parts.Count -lt 2) { throw "SHA256SUMS.txt 行格式不对: $line" }
                $want = $parts[0].Trim().ToLower()
                $name = $parts[1].Trim()
                # 清单里的资产可能在本机两个位置（publish\ 或 docs\），依次找
                $cand = @((Join-Path $PublishDir $name), (Join-Path $Root $name),
                          (Join-Path $Root "docs\$name")) | Where-Object { Test-Path $_ } | Select-Object -First 1
                if (-not $cand) { throw "清单里的资产找不到: $name" }
                $got = (Get-FileHash -LiteralPath $cand -Algorithm SHA256).Hash.ToLower()
                $checked++
                if ($got -ne $want) { $changed += $name }
            }
            if ($changed.Count -gt 0) { throw "哈希不一致: $($changed -join ', ')" }
            "校验 $checked 个资产，全部与清单一致"
        }

        Invoke-Check "自解压包：rcedit 九个字段与图标" {
            $rcedit = Join-Path $PublishDir "rcedit-x64.exe"
            if (-not (Test-Path $rcedit)) { $rcedit = Join-Path $Root "tools\rcedit-x64.exe" }
            foreach ($exe in @($SetupWith, $SetupNo)) {
                if (-not (Test-Path $exe)) { continue }
                $vi = (Get-Item $exe).VersionInfo
                $checks = @{
                    FileVersion    = "$Version.0"
                    ProductVersion = "$Version.0"
                    CompanyName    = "Void Entropia Studio"
                    LegalCopyright = "Copyright (C) Void Entropia Studio"
                    ProductName    = "EchoDesk"
                    InternalName   = "EchoDeskSetup"
                    Comments       = "Powered by Void Entropia Studio"
                }
                foreach ($k in $checks.Keys) {
                    if ("$($vi.$k)".Trim() -ne $checks[$k]) {
                        throw "$(Split-Path -Leaf $exe) 的 $k = '$($vi.$k)'，期望 '$($checks[$k])'"
                    }
                }
                if ("$($vi.FileDescription)" -notlike "EchoDesk v$Version Portable Setup*") {
                    throw "$(Split-Path -Leaf $exe) 的 FileDescription 不对: $($vi.FileDescription)"
                }
                if ("$($vi.OriginalFilename)" -ne (Split-Path -Leaf $exe)) {
                    throw "$(Split-Path -Leaf $exe) 的 OriginalFilename 不对: $($vi.OriginalFilename)"
                }
                # 图标：真的从 PE 里取一次关联图标，确认资源存在（不是只看 rcedit 没报错）
                Add-Type -AssemblyName System.Drawing -ErrorAction SilentlyContinue
                $ico = [System.Drawing.Icon]::ExtractAssociatedIcon($exe)
                if ($null -eq $ico) { throw "$(Split-Path -Leaf $exe) 取不到关联图标" }
                $ico.Dispose()
            }
            "八个字符串字段 + 两个版本字段 + 图标（实测可取）已写入"
        }

        Invoke-Check "自解压包：内含 install-complete.bat 且能解压" {
            $sevenZip = @(
                (Join-Path $Root "tools\7z.exe"),
                (Join-Path ${env:ProgramFiles} "7-Zip\7z.exe")
            ) | Where-Object { Test-Path $_ } | Select-Object -First 1
            if (-not $sevenZip) { throw "未找到 7z.exe，无法核对自解压包内容" }
            $tmp = Join-Path $env:TEMP ("echodesk_sfx_check_" + [guid]::NewGuid().ToString("N").Substring(0, 8))
            New-Item -ItemType Directory -Path $tmp -Force | Out-Null
            try {
                $prev = $ErrorActionPreference
                $ErrorActionPreference = "Continue"
                & $sevenZip x -y "-o$tmp" $SetupNo *> $null
                $code = $LASTEXITCODE
                $ErrorActionPreference = $prev
                if ($code -ne 0) { throw "解压不含模型版失败（退出码 $code）" }
                foreach ($n in @("install-complete.bat", "USAGE.txt", "app\EchoDesk.exe",
                                 "app\backend\EchoDeskBackend.exe", "docs\SBOM.json")) {
                    if (-not (Test-Path -LiteralPath (Join-Path $tmp $n))) { throw "解压后缺少 $n" }
                }
                # 不含模型版必须真的不含模型
                $m = @(Get-ChildItem (Join-Path $tmp "app\backend\models") -Filter *.gguf -File -ErrorAction SilentlyContinue)
                if ($m.Count -gt 0) { throw "不含模型版里仍有 $($m.Count) 个 .gguf" }
                "解压目录结构正确，且不含模型版确无 .gguf"
            }
            finally {
                Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
            }
        }

        Invoke-Check "自解压包：安装脚本真的能创建快捷方式" {
            # 任务二的功能验证：解压 → 运行 install-complete.bat → 快捷方式必须真的出现，
            # 且 TargetPath / WorkingDirectory / 图标都对。
            # 为什么必须真跑一次：v0.2.1 实测踩到过"生成的批处理里 PowerShell 变量被
            # here-string 吃掉"，语法坏掉、创建必然失败，而构建与静态检查全都报成功。
            # 静态断言（文件里有某段文字）挡不住这类问题，只有真跑才挡得住。
            $sevenZip = @(
                (Join-Path $Root "tools\7z.exe"),
                (Join-Path ${env:ProgramFiles} "7-Zip\7z.exe")
            ) | Where-Object { Test-Path $_ } | Select-Object -First 1
            if (-not $sevenZip) { throw "未找到 7z.exe" }
            $tmp = Join-Path $env:TEMP ("echodesk_install_check_" + [guid]::NewGuid().ToString("N").Substring(0, 8))
            New-Item -ItemType Directory -Path $tmp -Force | Out-Null
            try {
                $prev = $ErrorActionPreference
                $ErrorActionPreference = "Continue"
                & $sevenZip x -y "-o$tmp" $SetupNo *> $null
                $ErrorActionPreference = $prev
                if ($LASTEXITCODE -ne 0) { throw "解压失败" }

                $bat = Join-Path $tmp "install-complete.bat"
                if (-not (Test-Path $bat)) { throw "缺少 install-complete.bat" }
                # 用 < nul 喂掉末尾的 pause；只关注脚本是否真的建出快捷方式
                $out = & cmd /c "`"$bat`" < nul" 2>&1 | Out-String
                $lnk = Join-Path $tmp "Start-EchoDesk-v$Version.lnk"
                if (-not (Test-Path -LiteralPath $lnk)) {
                    throw "install-complete.bat 未创建快捷方式。输出尾部：`n" +
                    (($out -split "`n" | Select-Object -Last 6) -join "`n")
                }
                $shell = New-Object -ComObject WScript.Shell
                $sc = $shell.CreateShortcut($lnk)
                $wantTarget = (Join-Path $tmp "app\EchoDesk.exe")
                if ($sc.TargetPath -ne $wantTarget) {
                    throw "快捷方式目标不对：$($sc.TargetPath)（期望 $wantTarget）"
                }
                if ($sc.WorkingDirectory.TrimEnd('\') -ne $tmp.TrimEnd('\')) {
                    throw "快捷方式工作目录不对：$($sc.WorkingDirectory)（期望 $tmp）"
                }
                if ($sc.IconLocation -and $sc.IconLocation -notlike "*EchoDesk.ico*") {
                    throw "快捷方式图标不对：$($sc.IconLocation)"
                }
                "install-complete.bat 实测建出快捷方式，目标/工作目录/图标正确"
            }
            finally {
                Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }

    # ---------- 1c. 发布门禁（任务书 任务四 第 1–4 步的自动化部分） ----------
    Invoke-Check "反馈渠道与内容洁净度门禁" {
        Invoke-Native "反馈渠道门禁" "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass",
                                                  "-File", "tools\check_feedback.ps1")
        "通过"
    }

    Invoke-Check "依赖声明完整性（干净环境可导入）" {
        Invoke-Python "依赖自检" "tools\check_requirements.py"
        "通过"
    }

    Invoke-Check "发布物与合规清单口径一致（打包模块比对）" {
        # tools/check_bundled_packages.py：比对「干净 venv 声明的发行包」与
        # 「app\backend\_internal 里实际存在的顶层模块」。PyInstaller 按打包机环境
        # 分析，依赖里的可选 import 会被一并打进包 —— v0.2.1 就是这样混进了
        # cryptography（已改为显式声明）与 chardet/bidi（LGPL，已排除）。
        # 没有这项检查，这类"未声明组件"下次还会悄悄回来。
        Invoke-Python "打包模块比对" "tools\check_bundled_packages.py"
        "通过"
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
