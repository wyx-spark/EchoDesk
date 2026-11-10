# 适用平台：顶层编排，内含 Windows 专属项（安装包/UI 探针）
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
#   * 静态检查：语言包完整性、反向语言键审计、主题色板与对比度、emoji 字形覆盖、文档链接
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
    # 默认版本从 backend\version.py 读（v0.2.2 改造）：原来这里写死 "0.2.1"，
    # 版本一升，门禁就会去一个**不存在**的发布目录找打包好的后端，然后以退出码 2 结束并提示
    # "请先执行 build_release.ps1" —— 看起来像"还没打包"，实际是"路径过时了"。
    # 这类"默认值写死版本号"的地方，改一次版本就要全文搜一遍，所以统一改成派生。
    [string]$Version = "",
    [switch]$SkipGui,          # 跳过需要交互式桌面的两项（UI 探针 / 像素校验）
    [switch]$SkipE2E           # 跳过需要联网或真实 Edge 的端到端项
)

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Version) {
    $VersionFile = Join-Path $Root "backend\version.py"
    $match = Select-String -Path $VersionFile -Pattern 'APP_VERSION\s*=\s*["'']([^"'']+)["'']' |
             Select-Object -First 1
    if (-not $match) { throw "读不出 APP_VERSION：$VersionFile（用 -Version 显式指定）" }
    $Version = $match.Matches[0].Groups[1].Value.TrimStart('v')
    Write-Host "版本号（取自 backend\version.py）：$Version" -ForegroundColor DarkGray
}
# [EN] Four-part version number (added in v0.2.3): APP_VERSION allows a SemVer prerelease
# [EN] suffix such as `-alpha.1`, while a file-version resource accepts four purely numeric
# [EN] fields only. Building "$Version.0" directly would yield 0.2.3-alpha.1.0, so this check
# [EN] would compare against an expected value that can never occur — the gate would blind
# [EN] itself. The rule matches app_version_4() in backend\version.py; change both together.
# [ZH] 四段式版本号（v0.2.3 新增）：APP_VERSION 允许 `-alpha.1` 这类 SemVer 预发布后缀，
# [ZH] 而文件版本资源只能四段纯数字 —— 直接拼成 "$Version.0" 会变成 0.2.3-alpha.1.0，
# [ZH] 于是这条断言会拿一个**永远不可能出现**的期望值去比对（门禁自己变瞎）。
# [ZH] 口径与 backend\version.py 的 app_version_4() 一致，两处必须同步改。
$NumericVersion = ($Version -split '[-+]')[0].Trim()
$Out = Join-Path $Root "publish\EchoDesk-v$Version"
$AppOut = Join-Path $Out "app"
$BackendExe = Join-Path $AppOut "backend\EchoDeskBackend.exe"

# ---------------------------------------------------------------------------
# 可重入性隔离（v0.2.2 裁定②）
#
# 现象：界面类检查会**启动被测程序**，而便携布局下程序把 `ui_state.json`（窗口位置/字号/
# 主题/语言）写在发布树根 —— 这是设计行为（见 build_release.ps1 的目录说明）。
# 但后面"全英文命名 + 无过程文档 + 无运行期残留"那条断言要求发布树里没有运行期文件，
# 于是**跑过一次之后第二次跑就红**：门禁不可重入。
#
# 修法（裁定给的第二种做法：检查前后做隔离）：
#   * 在任何检查之前先清掉**程序自己该产生的那些状态文件**（白名单）；
#   * 界面检查跑完再清一次。
# 断言**没有放宽**：白名单之外的任何残留依旧会被那条断言抓住。
# 白名单必须与那条断言里列的名字一致（两处同源，改一处要改两处）。
$RuntimeStateNames = @('ui_state.json', 'snippets.json', 'browser_state.json',
                       'frontend_crash.log', 'backend_crash.log', 'automation_log.jsonl',
                       'skills_local.json', 'summary_cache.json', 'market_cache.json',
                       'skills_dev.log', 'automation_templates.json', 'models_registry.json')
function Clear-ReleaseRuntimeState {
    param([string]$ReleaseRoot)
    $removed = @()
    if (-not (Test-Path -LiteralPath $ReleaseRoot)) { return $removed }
    foreach ($name in $RuntimeStateNames) {
        # 三个位置都要看：发布树根、app\、以及 app\backend\（后端崩溃日志写在**后端 exe 旁边**，
        # 第一次实现只看了前两处，于是 `backend_crash.log` 没被清掉、断言继续红）。
        foreach ($candidate in @((Join-Path $ReleaseRoot $name),
                                 (Join-Path (Join-Path $ReleaseRoot "app") $name),
                                 (Join-Path (Join-Path (Join-Path $ReleaseRoot "app") "backend") $name))) {
            if (-not (Test-Path -LiteralPath $candidate)) { continue }
            # **崩溃日志只有是空的才删**：后端启动时就会建这个文件，正常情况 0 字节；
            # 一旦里面真有内容，那是**真实崩溃的证据**，必须留在原地让断言报出来，
            # 而不是被"清理"掉把问题抹平。
            if ($name -like "*crash.log") {
                $size = (Get-Item -LiteralPath $candidate).Length
                if ($size -gt 0) {
                    Write-Host "    [保留] $candidate 有内容（$size 字节）——疑似真实崩溃，不清理，留给断言报出" -ForegroundColor Yellow
                    continue
                }
            }
            Remove-Item -LiteralPath $candidate -Force -ErrorAction SilentlyContinue
            $removed += $candidate
        }
    }
    return $removed
}
$preClean = Clear-ReleaseRuntimeState -ReleaseRoot $Out
if ($preClean.Count -gt 0) {
    Write-Host "版本校验前：移除上一次运行留下的界面状态文件 $($preClean.Count) 个（保证可重入）" -ForegroundColor DarkGray
}

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

function Invoke-Python([string]$Name, [string]$Script, [string[]]$Extra = @()) {
    # $Extra：给脚本传的附加参数（例如 --check）。加它是为了不必为"带参数跑一个 Python 门禁"
    # 另写一段 Invoke-Native —— 那种重复写法正是先前各处参数不一致的来源。
    return Invoke-Native $Name "python" (@($Script) + $Extra)
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

        # v0.2.4 补丁：开发者目录绝不进包（只检查顶层；app\backend\_internal 里
        # playwright 自带的 tools 目录是运行期依赖，不能误判）。
        $forbiddenTop = @("tools", "tests", "records", "prompts")
        $leaked = @(Get-ChildItem $Out -Directory -Force -ErrorAction SilentlyContinue |
                    Where-Object { $forbiddenTop -contains $_.Name.ToLowerInvariant() } |
                    ForEach-Object { $_.Name })
        if ($leaked.Count -gt 0) { throw "发布树顶层出现开发者目录: $($leaked -join ', ')" }

        # 必备文件（任务书「全局要求四」：根目录 + docs\ + 发布物额外必备）
        $need = @(
            "app\EchoDesk.exe", "app\EchoDesk.ico", "app\backend\EchoDeskBackend.exe",
            "app\lang\ui", "lang", "examples\skills", "results", "docs",
            "USAGE.txt", "install-complete.bat",
            "README.md", "README.zh-Hans.md",
            "CHANGELOG.md", "CHANGELOG.zh-Hans.md",
            "CONTRIBUTING.md", "CONTRIBUTING.zh-Hans.md",
            "LICENSE", "LICENSE.zh-Hans.md", "NOTICE", "NOTICE.zh-Hans.md",
            "SECURITY.md", "SECURITY.zh-Hans.md",
            "config.example.json", "install-complete.bat",
            "docs\PRIVACY.md", "docs\PRIVACY.zh-Hans.md",
            "docs\EULA.md", "docs\EULA.zh-Hans.md", "docs\SECURITY.md",
            "docs\THIRD_PARTY_LICENSES.md", "docs\THIRD_PARTY_LICENSES.zh-Hans.md",
            "docs\SBOM.json", "docs\DEVELOPER.md",
            "docs\en\DEVELOPER.md", "docs\zh-Hans\DEVELOPER.md",
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
                 Where-Object { $_.Name -match '^(v?0\.\d+\.\d+)-.*记录\.md$' })
        if ($rec.Count -gt 0) { throw "发布物内含过程文档（任务书全局要求五）: $($rec[0].Name)" }

        $leftover = @(Get-ChildItem $Out -Recurse -File -ErrorAction SilentlyContinue |
                      Where-Object { $_.Name -in @('ui_state.json', 'snippets.json', 'browser_state.json',
                              'backend_crash.log', 'frontend_crash.log', 'automation_log.jsonl',
                              'skills_local.json', 'summary_cache.json', 'market_cache.json',
                              'skills_dev.log', 'automation_templates.json', 'models_registry.json') })
        if ($leftover.Count -gt 0) { throw "运行期残留: $($leftover[0].Name)" }
        "无中文名/无过程文档/无运行期残留"
    }

    # 期望值**从 backend\version.py 派生**（v0.2.2 硬编码清查 §九 第 2 批）：
    # 这里原来写死 "0.2.1.0"，于是它断言的不是"版本对不对"，而是"版本资源跟我记的那个字面量一样"
    # —— 版本升到 v0.2.2 之后这条会**继续通过**（打包产物若仍是旧版号也照样绿），
    # 属于"写死的期望值掩盖真实差异"。改成读 version.py 之后，"产物版本 ≠ 源码版本"
    # 会被门禁抓到（这正是它该做的判断）。
    $expectedVersion = "$NumericVersion.0"      # 0.2.3 → 0.2.3.0（FileVersion 的四段式，去掉 -alpha.1）
    Invoke-Check "版本资源 = $expectedVersion（取自 backend\version.py）" {
        $front = (Get-Item (Join-Path $AppOut "EchoDesk.exe")).VersionInfo.FileVersion
        $back = (Get-Item $BackendExe).VersionInfo.FileVersion
        if ($front -ne $expectedVersion -or $back -ne $expectedVersion) {
            throw "版本不符：期望 $expectedVersion，前端=$front 后端=$back"
        }
        "$front / $back"
    }

    # ---------- 1b. 安装程序交付物（Inno Setup） ----------
    # 这些只在跑过 -MakeInstaller 之后存在；没跑就明确跳过（而不是静默通过）。
    $PublishDir = Join-Path $Root "publish"
    $SetupWith = Join-Path $PublishDir "EchoDesk-v$Version-Setup.exe"
    $Sums = Join-Path $PublishDir "SHA256SUMS.txt"

    if (-not (Test-Path $SetupWith)) {
        Write-Host "[SKIP] 安装程序检查（未跑 -MakeInstaller，缺 $SetupWith）" -ForegroundColor Yellow
    }
    else {
        Invoke-Check "安装程序：SHA256 与 SHA256SUMS.txt 一致" {
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

        Invoke-Check "安装程序：版本资源与图标（Inno 编译器写入）" {
            <#
                这里**故意不调用 rcedit**。版本资源由 tools\installer.iss 的
                [Setup] VersionInfo* 指令在编译时写入 —— 对最终 Setup.exe 跑 rcedit
                会重建 PE 映像并丢掉 Inno 追加在映像之后的安装数据
                （实测：2 MB 的安装包被削到 893,952 字节，运行时报错且什么都不安装）。
                Inno 没有 InternalName / Comments 指令，所以只校验它能设置的字段。
            #>
            foreach ($exe in @($SetupWith)) {
                if (-not (Test-Path $exe)) { continue }
                $vi = (Get-Item $exe).VersionInfo
                $checks = @{
                    # 版本号用 Raw（数值口径）：Inno 会把 FileVersion 字符串末尾的 ".0" 省掉，
                    # 例如 VersionInfoVersion=0.2.1.0 → 读回 FileVersion='0.2.1'，Raw 仍是 0.2.1.0。
                    FileVersionRaw    = "$NumericVersion.0"
                    ProductVersionRaw = "$NumericVersion.0"
                    CompanyName       = "Void Entropia Studio"
                    LegalCopyright    = "Copyright (C) Void Entropia Studio"
                    ProductName       = "EchoDesk"
                    OriginalFilename  = (Split-Path -Leaf $exe)
                }
                foreach ($k in $checks.Keys) {
                    # 归一化后比较：Inno Setup 编译时会把版权串里的 "(C)" 写成 "©"
                    # （实测），按原样比对会因这个已知的等价改写而误报失败。
                    $got = (("$($vi.$k)").Trim() -replace '\(C\)', '©')
                    $want = (("$($checks[$k])").Trim() -replace '\(C\)', '©')
                    if ($got -ne $want) {
                        throw "$(Split-Path -Leaf $exe) 的 $k = '$($vi.$k)'，期望 '$($checks[$k])'"
                    }
                }
                if ("$($vi.FileDescription)" -notlike "EchoDesk v$Version Portable Setup*") {
                    throw "$(Split-Path -Leaf $exe) 的 FileDescription 不对: $($vi.FileDescription)"
                }
                # 图标：真的从 PE 里取一次关联图标，确认资源存在
                Add-Type -AssemblyName System.Drawing -ErrorAction SilentlyContinue
                $ico = [System.Drawing.Icon]::ExtractAssociatedIcon($exe)
                if ($null -eq $ico) { throw "$(Split-Path -Leaf $exe) 取不到关联图标" }
                $ico.Dispose()
            }
            "6 个字段 + FileDescription + 图标（实测可取）已写入，版本号按 Raw 核对"
        }

        Invoke-Check "安装程序：安装数据仍追加在 PE 映像之后（未被 rcedit 削壳）" {
            <#
                Inno Setup 把安装数据**追加在 PE 映像之后**。若某次改动让
                "文件长度 - 最后一个段的结束偏移" 变成 0，说明 PE 被重建过，
                安装包已经是个空壳 —— 这条断言就是用来兜住这种回归的。
            #>
            $out = @()
            foreach ($exe in @($SetupWith)) {
                if (-not (Test-Path $exe)) { continue }
                $len = (Get-Item -LiteralPath $exe).Length
                $fs = [System.IO.File]::OpenRead($exe)
                try {
                    $br = New-Object System.IO.BinaryReader($fs)
                    $fs.Seek(0x3C, [System.IO.SeekOrigin]::Begin) | Out-Null
                    $e = $br.ReadInt32()
                    $fs.Seek($e + 6, [System.IO.SeekOrigin]::Begin) | Out-Null
                    $numSec = $br.ReadUInt16()
                    $fs.Seek($e + 20, [System.IO.SeekOrigin]::Begin) | Out-Null
                    $optSize = $br.ReadUInt16()
                    $fs.Seek($e + 24 + $optSize, [System.IO.SeekOrigin]::Begin) | Out-Null
                    $maxEnd = [int64]0
                    for ($i = 0; $i -lt $numSec; $i++) {
                        $null = $br.ReadBytes(8); $null = $br.ReadUInt32(); $null = $br.ReadUInt32()
                        $rawSize = $br.ReadUInt32(); $rawPtr = $br.ReadUInt32(); $null = $br.ReadBytes(16)
                        $end = [int64]$rawPtr + [int64]$rawSize
                        if ($end -gt $maxEnd) { $maxEnd = $end }
                    }
                }
                finally { $fs.Dispose() }
                $appended = $len - $maxEnd
                if ($appended -le 0) {
                    throw ("$(Split-Path -Leaf $exe) 不含追加数据（PE 末段结束于 $maxEnd，文件长 $len）—— " +
                        "PE 被重建过，安装包是空壳。最常见原因是对最终 Setup.exe 跑了 rcedit。")
                }
                if ($appended -lt 1MB) {
                    throw "$(Split-Path -Leaf $exe) 的追加数据只有 $([math]::Round($appended / 1KB, 1)) KB，明显不完整"
                }
                $out += "$(Split-Path -Leaf $exe) $([math]::Round($appended / 1MB, 1)) MB"
            }
            $out -join ' · '
        }

        Invoke-Check "安装程序：真装真卸（默认目录 / 结构 / 快捷方式 / %TEMP% / 卸载清缓存 / 保留用户数据与 results）" {
            <#
                这一项真的把安装程序装一遍、再卸一遍。验收内容：
                  * **不传 /DIR**，验证"默认装到安装程序自己所在的目录" —— 这是本方案的
                    核心行为，也正是虚拟机实测暴露的旧问题（用户双击后全盘找不到文件）；
                  * 安装后当前目录出现 app\ docs\ lang\ examples\ results\ 等结构；
                  * 安装程序里确无 .gguf（本版只有一份不含模型的安装程序）；
                  * 开始菜单快捷方式指向 {app}\app\EchoDesk.exe，工作目录 = {app}（外层，
                    保证 results\ 与用户数据生成在外层）；
                  * 安装后 %TEMP% 下无 EchoDesk 残留；
                  * 卸载后 app\ docs\ 与快捷方式全部清掉，**运行期新建的文件**
                    （session.json / models\ / ui_state.json / 缓存 / 日志…）一个不留，
                    而 **results\ 里的用户结果原样保留**；
                  * 不留孤儿安装记录。

                实现放在 tools\check_installer_deliverables.ps1，可独立运行：
                  powershell -NoProfile -ExecutionPolicy Bypass -File tools\check_installer_deliverables.ps1
            #>
            $checker = Join-Path $Root "tools\check_installer_deliverables.ps1"
            if (-not (Test-Path $checker)) { throw "未找到 $checker" }
            # 已知 Inno 卸载竞态（v0.2.2 收尾裁定）：卸载器退出瞬间后台进程未死透时，
            # 注册表可能残留一条安装记录。这**不影响实际使用**（重装覆盖、再卸载即清），
            # 按裁定不修 Inno 脚本，只在本门禁编排里把它降级为告警 —— 单独运行
            # check_installer_deliverables.ps1 时仍是硬失败，避免把真回归一起放过。
            $env:ECHODESK_KNOWN_UNINSTALL_LEFTOVER = "1"
            Invoke-Native "交付物验收" "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass",
                                                      "-File", "tools\check_installer_deliverables.ps1")
            "安装程序已真实安装并卸载：默认目录/结构/快捷方式/%TEMP%/卸载/运行期文件清理全部通过（results\ 保留）"
        }
    }

    # ---------- 1c. 发布门禁（任务书 任务四 第 1–4 步的自动化部分） ----------
    Invoke-Check "反馈渠道与内容洁净度门禁" {
        Invoke-Native "反馈渠道门禁" "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass",
                                                  "-File", "tools\check_feedback.ps1")
        "通过"
    }

    # v0.2.1 跨平台-2（任务书全局约定）：从本轮起所有 Git 远端操作统一 SSH。
    # 默认不联网（要真连 GitHub 得加 -TestConnection），因此它能在无人值守的
    # 验证套件里跑；检查的是 remote 形式、推送脚本默认值与文档示例。
    Invoke-Check "统一 SSH（remote / 推送脚本 / 文档示例）" {
        Invoke-Native "统一 SSH 门禁" "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass",
                                                  "-File", "tools\check_git_ssh.ps1")
        "通过"
    }

    Invoke-Check "依赖声明完整性（干净环境可导入）" {
        Invoke-Python "依赖自检" "tools\check_requirements.py"
        Invoke-Python "依赖锁校验" "tools\gen_requirements_lock.py" @("--check")
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
    # 放在最前面：这是最基础的一条 ——"后端起得来吗"（ready 握手 + 一条最小请求 + 干净退出）。
    # 为什么单独一条（v0.2.2 硬编码清查第 6 批（二）的真实事故）：那次把 platform_support.py 的
    # 平台名改成本地化时写成模块级 `_t(...)`，后端直接 `NameError`、连 ready 都发不出；
    # 而 selftest(155/155)、check_lang 当时**全是绿的** —— 没有一项断言"后端能不能起来"。
    # 唯一拦住它的是界面探针（要 Avalonia 产物、跑得慢），所以把这条断言摘出来单独跑。
    Invoke-Check "后端启动冒烟（ready 握手 + 最小请求 + 干净退出）" {
        Invoke-Python "后端启动" "tools\check_backend_boot.py"; "通过"
    }
    Invoke-Check "后端 selftest" { Invoke-Python "selftest" "tools\selftest.py"; "通过" }
    Invoke-Check "文件解析" { Invoke-Python "文件解析" "tools\test_file_parsers.py"; "通过" }
    Invoke-Check "纯文本落盘" { Invoke-Python "纯文本落盘" "tools\test_plaintext.py"; "通过" }
    Invoke-Check "结果目录结构" { Invoke-Python "结果目录" "tools\test_results_dir.py"; "通过" }
    Invoke-Check "技能运行期加载器" { Invoke-Python "技能" "tools\test_skills_runtime.py"; "通过" }
    Invoke-Check "多语言机制" { Invoke-Python "多语言" "tools\test_i18n.py"; "通过" }
    Invoke-Check "开发者模式（修复一）" { Invoke-Python "开发者模式" "tools\test_dev_mode.py"; "通过" }
    Invoke-Check "模型能力辅助（修复四）" { Invoke-Python "模型能力" "tools\test_capability.py"; "通过" }
    # [EN] v0.2.4: synthetic 8 GiB / 12 GiB profiles assert the GPU layer count,
    # [EN] the VRAM safety watermark and the silent CPU degradation path.
    # [ZH] v0.2.4：以合成的 8 GiB / 12 GiB 档案断言 GPU 层数、显存安全水位线与
    # [ZH] 静默降级为纯 CPU 的路径。
    Invoke-Check "硬件探测与推理规划（v0.2.4）" { Invoke-Python "硬件规划" "tools\test_hardware_profiler.py"; "通过" }
    # [EN] v0.2.4 task 3: explicit override, structured failure and the
    # [EN] child-process "test load" path are verified against a real model.
    # [ZH] v0.2.4 任务-3：显式覆盖、结构化失败与子进程「测试加载」路径用真实模型验证。
    Invoke-Check "推理覆盖与透明诊断（v0.2.4 任务-3）" {
        Invoke-Python "覆盖诊断" "tools\test_inference_override.py"; "通过"
    }
    # [EN] v0.2.4 patch: update asset guard, digest/size rejection and the isolated
    # [EN] user-data backup are verified without network access.
    # [ZH] v0.2.4 补丁：更新附件白名单、摘要/大小校验与隔离的用户数据备份，全部离线验证。
    Invoke-Check "自动更新与数据保护（v0.2.4 补丁）" {
        Invoke-Python "自动更新" "tools\test_update_check.py"; "通过"
    }
    Invoke-Check "后端协议端到端" { Invoke-Python "后端协议端到端" "tools\test_backend_e2e.py"; "通过" }
    # v0.2.1 模型市场优化：加载器（分页取全/上限/重试/缓存/离线降级）用本机 HTTP 桩验证，
    # 不依赖真实网络；再驱动**打包后的后端**验证流式加载与本地查询协议。
    # 这里必须用 --packaged：验证的是"发出去的那份后端"，而不是源码后端。
    Invoke-Check "模型市场（加载/分页/筛选/排序）" {
        Invoke-Native "模型市场" "python" @("tools\test_model_market.py", "--packaged")
        "通过"
    }
    # v0.2.1 模型思考内容分离：分离器边界 + 引擎双通道 + 流式协议（channel/reset/done）
    # + 历史持久化，全部在进程内驱动真后端代码，不依赖网络也不依赖模型。
    # 真实模型的端到端（用 --model 指定）在 CI 里跳过：机器上不一定有推理模型。
    Invoke-Check "思考内容分离" { Invoke-Python "思考分离" "tools\test_reasoning.py"; "通过" }

    # v0.2.1 优化已知问题-2 任务四：抽取式预筛（TextRank 选句 + 稀有度加权）。
    # 覆盖切句/特征/图/抽取边界/关键事实保留下界/密度自适应/性能上限/
    # 摘要器接线（可开关、可统计、异常安全降级）/配置项。纯统计，无模型无网络。
    Invoke-Check "抽取式预筛（任务四）" { Invoke-Python "抽取式预筛" "tools\test_extractive.py"; "通过" }

    # v0.2.1 优化已知问题-2 任务二：联网搜索链路（结构化结果 / 精简上限 /
    # 编号注入 + 引用指令 / 失败与空结果明确告知 / 四个环节可观测 / 双语文案）。
    # 用桩 DDGS，不联网。
    Invoke-Check "联网搜索链路（任务二）" { Invoke-Python "搜索链路" "tools\test_search.py"; "通过" }

    # [EN] v0.2.6 task 2.3: ordered provider cascade (DuckDuckGo -> Bing ->
    # [EN] SearXNG), silent fallback on timeout/429, combined failure report.
    # [ZH] v0.2.6 任务 2.3：有序提供方级联（DuckDuckGo → Bing → SearXNG），
    # [ZH] 超时/429 静默回退，全部失败时合并上报。
    Invoke-Check "多引擎级联搜索（v0.2.6）" { Invoke-Python "搜索级联" "tools\test_search_cascade.py"; "通过" }

    # v0.2.1 优化已知问题-2 任务九：上下文压缩策略（token 比例触发可配 / grace zone /
    # 摘要提示词与自定义重点 / 大块内容单独截断 / 压缩事件 JSONL 可追溯可回放 /
    # 手动触发与 focus 解析）。用桩引擎，不加载模型。
    Invoke-Check "上下文压缩（任务九）" { Invoke-Python "上下文压缩" "tools\test_compaction.py"; "通过" }

    # v0.2.1 优化已知问题-2 任务八：多文件上传（登记表/逐份状态/单份失败隔离/
    # 统一汇总/数量与总量上限/注入预算与如实提示/同名与不支持格式/会话持久化兼容/
    # /file 多路径与引号感知分词）。用桩引擎，不加载模型。
    Invoke-Check "多文件上传（任务八）" { Invoke-Python "多文件" "tools\test_multifile.py"; "通过" }

    # [EN] v0.2.6 task 2.1/2.2: asynchronous document processing and absolute
    # [EN] context isolation (50-page document, ping under 100 ms, one final
    # [EN] system summary only). Uses a stub engine; no model is loaded.
    # [ZH] v0.2.6 任务 2.1/2.2：文档异步处理与上下文绝对隔离
    # [ZH] （50 页长文档、ping 低于 100 毫秒、只注入一条最终 system 摘要）。
    # [ZH] 使用桩引擎，不加载模型。
    Invoke-Check "文档异步与上下文隔离（v0.2.6）" { Invoke-Python "文档异步" "tools\test_document_async.py"; "通过" }

    # v0.2.1 系统提示词重构：对比测试的**无模型**部分。客观指标函数（分点识别/
    # 引导话术判据/防编造判据/摘录重合度）+ 场景表覆盖 + extract 的硬格式不放松，
    # 并子进程跑 tools\prompt_compare.py --dry-run（从 git 历史取旧提示词做新旧对比）。
    # 真模型的对比要几百 MB GGUF 与数分钟，因此不在门禁里跑（见该文件头部说明）。
    Invoke-Check "提示词重构对比（无模型）" { Invoke-Python "提示词对比" "tools\test_prompt_compare.py"; "通过" }

    # v0.2.1 能力扩展-1：Agent 平台（工具接口/工具集/调用循环/最大步数/权限分级/
    # 可见性/可中断）+ MCP 客户端（真拉起 examples\mcp\echo_server.py 走 stdio JSON-RPC）
    # + 本地检索（BM25/引用溯源/多文档/增量索引）+ 模型管理写路径（采样参数、自定义档位）。
    # 四者都不联网、不加载模型；MCP 会起一个本机 python 子进程（标准库脚本）。
    Invoke-Check "Agent 平台（能力扩展-1 任务二）" { Invoke-Python "Agent" "tools\test_agent.py"; "通过" }
    Invoke-Check "MCP 客户端（任务三）" { Invoke-Python "MCP" "tools\test_mcp.py"; "通过" }
    Invoke-Check "本地检索（任务六）" { Invoke-Python "检索" "tools\test_retrieval.py"; "通过" }
    Invoke-Check "模型管理写路径（任务五）" { Invoke-Python "模型管理" "tools\test_model_admin.py"; "通过" }

    # v0.2.1 跨平台（任务三 / 任务五）：平台探测（kind/RID/数据目录）、能力清单、
    # Windows 专属能力的降级文案与替代做法、非 Windows 上读旧版 Office 格式的说明、
    # 自动化页面按平台禁用、以及 platform 动作的协议往返。
    # 本机只有 Windows，所以非 Windows 分支靠 ECHODESK_FORCE_PLATFORM 注入验证
    # （报告里会标记 forced，见 tools\test_platform.py 头部说明）。
    Invoke-Check "跨平台能力边界（任务三/五）" { Invoke-Python "跨平台" "tools\test_platform.py"; "通过" }

    # v0.2.1 跨平台-2（任务二/任务五）：standalone 后端产物的结构、启动脚本语法与本平台可运行性。
    # 只在**产物完整**时检查（有 site-packages 说明依赖装完了）；否则跳过并提示 ——
    # 跨平台产物需要从 GitHub 下载 python-build-standalone，网络不允许时产不出来，
    # 不该让整轮发布验证因此变红（但产物存在而不完整时**会**失败，那是真问题）。
    # --allow-missing：只产出了部分平台时，未产出的平台只提示不失败。
    $standaloneReady = @(Get-ChildItem -Path "publish" -Directory -Filter "backend-*" -ErrorAction SilentlyContinue |
                         Where-Object { Test-Path (Join-Path $_.FullName "site-packages") })
    if ($standaloneReady.Count -gt 0) {
        Invoke-Check "standalone 后端产物（结构/启动脚本/本平台可运行）" {
            Invoke-Native "standalone 后端" "python" @("tools\check_standalone_backend.py", "--allow-missing")
            "通过"
        }
    }
    else {
        Write-Host "  [--]  standalone 后端产物不存在或不完整，跳过（先跑 python tools\pack_backend.py）" -ForegroundColor DarkGray
    }

    # v0.2.1 跨平台-2（任务四/任务五）：**完整发布物**（前端 + 后端）的归档检查。
    # 与上一项的差别：上一项查后端产物目录，这一项查最终给用户的归档本身，四项都是
    # "日志一切正常、用户却用不了"的类型：
    #   * 归档格式必须匹配目标平台（Linux/macOS 要 tar.gz；zip 不保留 Unix 权限位）；
    #   * tar.gz 里关键文件必须真有可执行位（打包机是 Windows，拿不到真实权限位）；
    #   * 归档里每个文件的父目录条目要排在它前面（流式解压才不出错）；
    #   * SIGNING.txt 必须在**归档里**（文档让 macOS 用户去看它 —— 曾出现只写进目录没进归档）。
    $distReady = @(Get-ChildItem -Path "dist" -File -ErrorAction SilentlyContinue |
                   Where-Object { $_.Name -like "EchoDesk-*-*.tar.gz" -or $_.Name -like "EchoDesk-*-*.zip" })
    # 回退：归档后的三平台发布物不再留在 dist/（冻结进 publish/archive/<版本>/ 后即可删副本），
    # 那时这里改指归档目录 —— 否则"清理掉可重建的/散落的副本"会让这条门禁再也没得可查。
    if ($distReady.Count -eq 0) {
        $archived = Get-ChildItem -Path "publish\archive" -Directory -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -ne "superseded" -and
                        @(Get-ChildItem -Path $_.FullName -File -ErrorAction SilentlyContinue |
                          Where-Object { $_.Name -like "*-*.tar.gz" -or $_.Name -like "*-*.zip" }).Count -gt 0 } |
                    Sort-Object Name -Descending | Select-Object -First 1
        if ($archived) {
            $distDir = "publish\archive\$($archived.Name)"
            Write-Host "  [--]  dist\ 下没有发布物，改查归档目录：$distDir" -ForegroundColor DarkGray
        }
        else { $distDir = "dist" }
    }
    else { $distDir = "dist" }
    $distReady = @(Get-ChildItem -Path $distDir -File -ErrorAction SilentlyContinue |
                   Where-Object { $_.Name -like "*-*.tar.gz" -or $_.Name -like "*-*.zip" })
    if ($distReady.Count -gt 0) {
        Invoke-Check "跨平台完整发布物（归档格式/可执行位/目录顺序/签名说明）" {
            Invoke-Native "跨平台发布物" "python" @("tools\check_crossplatform_release.py", "--dir", $distDir, "--version", $Version, "--allow-missing")
            "通过"
        }
    }
    else {
        Write-Host "  [--]  跨平台发布物不存在，跳过（先跑 python tools\pack_crossplatform.py，见 docs\CROSS_PLATFORM.md）" -ForegroundColor DarkGray
    }

    # ---------- 3. 静态检查 ----------
    Invoke-Check "语言包完整性" { Invoke-Python "语言包" "tools\check_lang.py"; "通过" }

    # v0.2.2 反向语言键审计（本轮从 artifacts\_audit_avalonia_keys.py 移入 tools\ 并接上门禁）：
    # check_lang.py 查"语言包自身齐不齐"，方向相反；而 Avalonia 的 Loc.T("key","中文兜底")
    # 在**缺键时不报错**，只安静回落中文 —— 英文界面照样显示中文，check_lang.py 看不见这一类。
    # 判定门槛就是**缺失键数 = 0**（当前基线 807 个引用键、0 缺失），阈值未放松；
    # 脚本在输入目录缺失时以 2 退出（不静默通过）。
    # 另外两个前端**动作名差集**的清点脚本（tools\inventory_frontend_actions.py）不进这里：
    # 它输出的是预期存在的差异，没有可判定的通过/失败（理由写在脚本头部）。
    Invoke-Check "反向语言键审计（Avalonia 引用但语言包缺失的键）" {
        Invoke-Python "反向语言键审计" "tools\audit_lang_keys.py"
        "通过"
    }
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

    # v0.2.2 全面双语与三平台友好（任务一/五/七/八）新增的三条门禁。
    # 三条都是纯标准库 Python，因此**三平台都能跑** —— CI 里也放在 ubuntu 那一格跑了一遍
    # （见 .github/workflows/ci.yml 的 static-gates-linux），避免"为三平台准备的检查自己只能在 Windows 上跑"。
    Invoke-Check "双语文档成对性与 .ps1 等价物登记（任务一/四）" {
        Invoke-Python "双语审计" "tools\audit_repo.py" @("--check")
        "通过"
    }
    Invoke-Check "三平台图标与桌面条目（任务七）" {
        Invoke-Python "平台资源" "tools\gen_platform_icons.py" @("--check")
        "通过"
    }
    Invoke-Check "配置示例（双语说明 + 三平台路径，任务五 5.1）" {
        # 发布物里才会有 config.example.json；没构建过时脚本会明确打印"不存在，跳过"并以 0 退出。
        Invoke-Python "配置示例" "tools\gen_config_example.py" @("--check", "config.example.json")
        "通过"
    }
    Invoke-Check "换行符符合 .gitattributes（CRLF/LF）" {
        # 这条是拿真实事故换来的（v0.2.2）：`.bat` 必须是 CRLF，否则 cmd.exe 解析 label 与续行会出错，
        # 而**任何直接改写工作区文件的工具都会绕过 git 的规范化** —— 本轮就是被 Python 的
        # 文本模式读 + newline="" 写转成 LF，导致发布构建在"元数据写入"这一步失败。
        Invoke-Python "换行符" "tools\check_line_endings.py" @("--quiet")
        "通过"
    }

    # ---------- 4. 需要交互式桌面的项 ----------
    #
    # 可重入性隔离见文件头部 `Clear-ReleaseRuntimeState` 的说明：**跑前**那一次已经在
    # 所有检查之前做了（脚本开头、无条件），这里只负责**跑后**再清一次。
    if ($SkipGui) {
        Write-Host "[SKIP] UI 探针 / 像素校验（-SkipGui）" -ForegroundColor Yellow
    }
    else {
        # Avalonia 侧的门禁（本轮加回来，且**指向开发机构建而不是发布树**）。
        #
        # 为什么必须指向开发构建：本发布树里在 app\ 下的是 **WPF** 前端
        # （dotnet publish frontend\EchoDesk.csproj），Avalonia 是**另一条交付线**
        # （tools\pack_crossplatform.py 的三平台包，由 check_crossplatform_release.py 校验
        # start.sh / app/EchoDesk）。两者都在本仓库里，不是二选一。
        #
        # 为什么必须有这一条：记录 §7.4 的"Avalonia 前端这套代码此前从未被真正跑起来过"
        # 就是因为**没有任何门禁会去构造窗口** —— 而 GUI 缺陷全都是"一构造/一显示就死"。
        # 没有构建产物时跳过而不是失败：本机器的发布验证不该因为"没编过跨平台前端"而红。
        Invoke-Check "Avalonia 界面探针（跨平台前端：窗口 + 模型市场整条交互）" {
            $avExe = Join-Path $Root "frontend-avalonia\EchoDesk.Avalonia\bin\Debug\net10.0\EchoDesk.exe"
            if (-not (Test-Path $avExe)) {
                Write-Host "    [跳过] 没有跨平台前端的 Debug 构建：先 dotnet build frontend-avalonia\EchoDesk.Avalonia" -ForegroundColor Yellow
                return "跳过（未构建）"
            }
            Invoke-Native "ui-probe-avalonia" "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass",
                                                             "-File", "tools\ui_probe_avalonia.ps1",
                                                             "-Exe", $avExe, "-Mode", "both")
            # [EN] v0.2.4 patch: chat mode adds the render-only input-hover assertions and
            # [EN] the status-strip item/memory assertions. It must be part of the release
            # [EN] gate, otherwise those regressions could return unnoticed.
            # [ZH] v0.2.4 补丁：chat 模式新增"输入框悬停零布局重排"与状态区各项/内存断言。
            # [ZH] 它必须进入发布门禁，否则这些回归可以悄无声息地回来。
            Invoke-Native "ui-probe-avalonia-chat" "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass",
                                                                  "-File", "tools\ui_probe_avalonia.ps1",
                                                                  "-Exe", $avExe, "-Mode", "chat")
            # market 模式要真后端 + 一次远端搜索，单独给它更长的超时
            Invoke-Native "ui-probe-avalonia-market" "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass",
                                                                    "-File", "tools\ui_probe_avalonia.ps1",
                                                                    "-Exe", $avExe, "-Mode", "market",
                                                                    "-TimeoutSec", "300")
            # [EN] pages mode drives the settings pages against a real backend and
            # [EN] asserts the inference preset controls added in v0.2.4 task 2.
            # [ZH] pages 模式对着真后端逐页验证设置页，并断言 v0.2.4 任务-2 新增的推理档位控件。
            Invoke-Native "ui-probe-avalonia-pages" "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass",
                                                                   "-File", "tools\ui_probe_avalonia.ps1",
                                                                   "-Exe", $avExe, "-Mode", "pages",
                                                                   "-TimeoutSec", "300")
            "通过"
        }
        Invoke-Check "UI 探针（窗口/页签/字段帮助/思考区）" {
            <#
                发布物**不含模型**（本版安装程序与便携目录都不带 .gguf，见 tools\installer.iss
                头部说明），而"思考内容分离"那一节需要一个真模型才测得到折叠区。
                所以这里从开发机的 backend\models 里挑一个模型（优先默认模型）传给探针：
                ui_probe.ps1 自己有"临时装进被测程序 + 写 config.user.json 覆盖 + 收尾清理"
                的机制，跑完不会在发布目录里留下残留。
                找不到模型时照常跑（其余检查不受影响），并明确告警说明思考区测不到。
            #>
            $probeModel = ""
            $devModels = Join-Path $Root "backend\models"
            if (Test-Path $devModels) {
                $ggufs = @(Get-ChildItem $devModels -Filter *.gguf -File -ErrorAction SilentlyContinue |
                    Sort-Object Name)
                $preferred = @($ggufs | Where-Object { $_.Name -eq "Qwen3-0.6B-Q6_K.gguf" })
                if ($preferred.Count -gt 0) { $probeModel = $preferred[0].FullName }
                elseif ($ggufs.Count -gt 0) { $probeModel = $ggufs[0].FullName }
            }
            if ($probeModel) {
                Write-Host "    探针模型: $probeModel" -ForegroundColor DarkGray
                Invoke-Native "ui-probe" "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass",
                                                        "-File", "tools\ui_probe.ps1",
                                                        "-ThinkingModel", $probeModel)
            }
            else {
                Write-Host "    [警告] backend\models 下没有 .gguf：发布物不含模型，思考区检查将跳过" -ForegroundColor Yellow
                Invoke-Native "ui-probe" "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass",
                                                        "-File", "tools\ui_probe.ps1")
            }
            "通过"
        }
        Invoke-Check "像素校验（修复二/六）" {
            Invoke-Native "ui-pixels" "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass",
                                                     "-File", "tools\check_ui_pixels.ps1", "-Theme", "both")
            "通过"
        }

        # 跑后再清一次：上面几个检查都启动过被测程序，便携布局下会把界面状态写回发布树根。
        # 不清的话，**下一次**跑 verify_release 时第 167 行那条"无运行期残留"就会红 ——
        # 那正是裁定② 说的"门禁不可重入"。清掉的是程序**本来就该产生**的状态文件（白名单），
        # 白名单之外的残留依旧会被那条断言抓住。
        $afterProbe = Clear-ReleaseRuntimeState -ReleaseRoot $Out
        if ($afterProbe.Count -gt 0) {
            Write-Host "    [清理] 移除界面探针运行后产生的状态文件 $($afterProbe.Count) 个（下次运行可重入）" -ForegroundColor DarkGray
        }
    }

    # ---------- 5. 需要真实浏览器 / 联网的项 ----------
    if ($SkipE2E) {
        Write-Host "[SKIP] 自动化端到端 / 模型中心端到端（-SkipE2E）" -ForegroundColor Yellow
    }
    else {
        Invoke-Check "自动化端到端（真实 Edge）" { Invoke-Python "自动化端到端" "tools\test_automation_e2e.py"; "通过" }
        # v0.2.1 自动化优化：登录态复用 / href 提取 / 可视化选取语义。
        # 它自己起一个回环 HTTP 站点并在真实浏览器里跑，同样需要 -SkipE2E 之外的机器。
        Invoke-Check "自动化登录态与链接提取（真实 Edge）" { Invoke-Python "自动化登录态" "tools\test_automation_login.py"; "通过" }
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
